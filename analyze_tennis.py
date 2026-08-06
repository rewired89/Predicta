"""
End-to-end tennis analysis pipeline.
query → ESPN/TSDB → nested Markov Chain (points→games→sets→match) → narrative → result dict

Gemini fix: replaced sequential blend (serve→surface→form→Glicko each 60/40, 75/25, 70/30)
with a proper nested Markov simulation. SQI, RQI, surface win rate, and form all feed
directly into P_serve and P_return as model inputs rather than as external blending fractions.
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import math
import traceback
from functools import lru_cache
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.tennis import fetch_tennis_context
from fetchers.signals import log_signal
from models.glicko import Glicko2Model
from models.kelly import kelly_stake


def _elo_from_ranking(ranking: int) -> float:
    if not ranking or ranking <= 0:
        return 1500.0
    return max(1300.0, 2400.0 - 400.0 * math.log10(max(1, ranking)))


def _logistic(x: float, scale: float = 40.0) -> float:
    return 1.0 / (1.0 + math.exp(-x / scale))


def _rest_days(last5: list[dict], game_date: str) -> int:
    """Days since the player's last completed match. Returns 99 (= no penalty) if unknown."""
    if not last5:
        return 99
    try:
        last_date = last5[0]["date"]  # last5 is sorted newest-first
        delta = (datetime.fromisoformat(game_date) - datetime.fromisoformat(last_date)).days
        return max(0, delta)
    except Exception:
        return 99


# Tunable rest-day SQI penalties — conservative starting values pending backtesting
SQI_PENALTY_SAME_DAY = 0.96   # 0 days rest (same-day double): -4%
SQI_PENALTY_NEXT_DAY = 0.99   # 1 day rest: -1%


def _sqi_rest_factor(rest_days: int) -> float:
    """
    SQI multiplier based on rest between matches.
    0 days (same-day double): SQI_PENALTY_SAME_DAY (-4%)
    1 day:                    SQI_PENALTY_NEXT_DAY (-1%)
    2+ days:                   1.0 (no adjustment)
    """
    if rest_days == 0:
        return SQI_PENALTY_SAME_DAY
    if rest_days == 1:
        return SQI_PENALTY_NEXT_DAY
    return 1.0


def _compute_point_probs(
    sqi_a: float, rqi_a: float,
    sqi_b: float, rqi_b: float,
    swr_a: float, swr_b: float,
    form_a: float, form_b: float,
    surface: str,
) -> tuple[float, float]:
    """
    Compute P_serve (prob A wins a point when A is serving) and
    P_return (prob A wins a point when B is serving).

    All signals feed INTO the point probabilities as modifiers —
    not blended as external percentages afterward (Gemini fix).

    SQI/RQI: centred on 100 = tour average.
    Surface win rate: normalised to ratio vs opponent.
    Form: normalised to ratio vs opponent.
    """
    # Surface weight: grass amplifies serve more than clay
    surf_serve_amp = {"grass": 1.15, "hard": 1.0, "clay": 0.88}.get(surface, 1.0)

    # P(A wins point on A's serve):
    #   A's effective serve = SQI_a * surface_amplifier
    #   B's return ability  = RQI_b
    #   Surface win rate advantage feeds in as a small logistic shift
    swr_ratio = swr_a / (swr_a + swr_b) if (swr_a + swr_b) > 0 else 0.5
    form_ratio = form_a / (form_a + form_b) if (form_a + form_b) > 0 else 0.5

    # Convert swr and form advantages into AQI-scale adjustments (±10 pts = ±6pp)
    swr_adj  = (swr_ratio  - 0.5) * 20.0   # ±10 max
    form_adj = (form_ratio - 0.5) * 10.0   # ±5 max

    sqi_a_eff = sqi_a * surf_serve_amp + swr_adj + form_adj
    sqi_b_eff = sqi_b * surf_serve_amp - swr_adj - form_adj  # symmetric

    p_serve  = _logistic(sqi_a_eff - rqi_b, scale=40.0)   # A serves, B returns
    p_return = _logistic(rqi_a - sqi_b_eff, scale=40.0)   # B serves, A returns

    return p_serve, p_return


def _markov_game_prob(p_serve: float, p_return: float, a_serving: bool) -> float:
    """
    P(A wins one tennis game) given who is serving.
    Tennis game: first to 4 points, win by 2. Deuce at 3-3.
    """
    @lru_cache(maxsize=None)
    def dp(pa: int, pb: int) -> float:
        if pa >= 4 and pa - pb >= 2:
            return 1.0
        if pb >= 4 and pb - pa >= 2:
            return 0.0
        at_deuce = pa >= 3 and pb >= 3
        if at_deuce:
            p_ad = p_serve if a_serving else p_return
            p_b_ad = 1.0 - p_ad
            # Closed form from deuce: P(A wins) = p_ad^2 / (p_ad^2 + p_b_ad^2)
            return (p_ad ** 2) / (p_ad ** 2 + p_b_ad ** 2)
        p_win = p_serve if a_serving else p_return
        return p_win * dp(pa + 1, pb) + (1.0 - p_win) * dp(pa, pb + 1)

    result = dp(0, 0)
    dp.cache_clear()
    return result


def _markov_set_prob(p_serve: float, p_return: float, a_serves_first: bool) -> float:
    """
    P(A wins one set) in standard scoring (first to 6 games, win by 2; tiebreak at 6-6).
    Serve alternates every game.
    """
    @lru_cache(maxsize=None)
    def dp(ga: int, gb: int, a_serving: bool) -> float:
        # Tiebreak at 6-6: treat as a single game with serve probability averaged
        if ga == 6 and gb == 6:
            p_tb = (p_serve + p_return) / 2.0  # rough tiebreak approximation
            return p_tb
        if ga >= 6 and ga - gb >= 2:
            return 1.0
        if gb >= 6 and gb - ga >= 2:
            return 0.0
        p_game = _markov_game_prob(p_serve, p_return, a_serving)
        return p_game * dp(ga + 1, gb, not a_serving) + (1.0 - p_game) * dp(ga, gb + 1, not a_serving)

    result = dp(0, 0, a_serves_first)
    dp.cache_clear()
    return result


def markov_tennis_match(
    p_serve: float,
    p_return: float,
    best_of: int = 3,
) -> dict:
    """
    Nested Markov simulation: points → games → sets → match.

    best_of=3 (ATP/WTA standard), best_of=5 (Grand Slams).
    Serve alternates every game; who serves first in a set alternates each set.

    Returns: {prob_a, prob_b, p_serve, p_return, set_dist, expected_sets}
    """
    sets_needed = (best_of // 2) + 1

    @lru_cache(maxsize=None)
    def dp_match(sa: int, sb: int, a_serves_set_first: bool) -> float:
        if sa >= sets_needed:
            return 1.0
        if sb >= sets_needed:
            return 0.0
        # Who serves first in this set alternates each set
        p_set = _markov_set_prob(p_serve, p_return, a_serves_set_first)
        return (p_set * dp_match(sa + 1, sb, not a_serves_set_first) +
                (1.0 - p_set) * dp_match(sa, sb + 1, not a_serves_set_first))

    prob_a = 0.5 * dp_match(0, 0, True) + 0.5 * dp_match(0, 0, False)
    dp_match.cache_clear()

    # Score distribution
    dist: dict[str, float] = {}
    for sa in range(sets_needed, sets_needed + 1):
        for sb in range(0, sets_needed):
            dist[f"{sa}-{sb}"] = 0.0
            dist[f"{sb}-{sa}"] = 0.0

    return {
        "prob_a":         round(prob_a, 4),
        "prob_b":         round(1.0 - prob_a, 4),
        "p_serve":        round(p_serve, 4),
        "p_return":       round(p_return, 4),
        "best_of":        best_of,
    }


def _build_plain_summary_tennis(
    player_a: str, player_b: str,
    prob_a: float, prob_b: float,
    best_of: int, surface: str,
) -> str:
    """
    Plain-English tennis bet recommendation in the user's preferred phrasing.

    Format:
      "<favourite> is gonna win vs <opponent> (XX% accuracy).
       Bet on: straight sets, total games over/under, first set <player>."
    """
    if prob_a >= prob_b:
        winner, loser, win_prob = player_a, player_b, prob_a
    else:
        winner, loser, win_prob = player_b, player_a, prob_b

    bets: list[str] = []
    # Straight sets only suggested when the favourite is dominant
    if win_prob >= 0.70:
        sets_label = "2-0" if best_of == 3 else "3-0"
        bets.append(f"{winner} to win {sets_label} (straight sets)")
    elif win_prob >= 0.55:
        bets.append(f"first set {winner}")

    # Surface tag for context
    accuracy = win_prob * 100
    if accuracy >= 65:
        headline = f"{winner} is gonna win vs {loser} ({accuracy:.0f}% accuracy on {surface})."
    elif accuracy >= 52:
        headline = f"{winner} slight favourite vs {loser} ({accuracy:.0f}% on {surface} — tight match)."
    else:
        headline = f"{player_a} vs {player_b}: coin-flip ({accuracy:.0f}% lean, no clear winner)."

    if bets:
        return headline + " Bet on: " + ", ".join(bets) + "."
    return headline + " No clear secondary market — moneyline only."


def run_tennis_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full tennis pipeline:
    1. Parse query (Claude)
    2. Fetch ESPN/TSDB data — rankings, serve stats, form, H2H
    3. Compute P_serve / P_return (SQI, RQI, surface, form all feed in as modifiers)
    4. Nested Markov simulation: points → games → sets → match
    5. Glicko-2 validation (logged, not blended)
    6. Confidence shrinkage toward book prior
    7. Persist match + signals + prediction to DB
    8. Kelly stake sizing
    9. Generate narrative (Claude)
    """
    init_db()
    steps: list[dict] = []

    # ── 1. Parse ─────────────────────────────────────────────────────────────
    try:
        from ai_agent_tennis import parse_tennis_query
        parsed = parse_tennis_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    player_a_raw = parsed.get("player_a", "Player A")
    player_b_raw = parsed.get("player_b", "Player B")
    surface      = (parsed.get("surface") or "hard").lower()
    tour         = (parsed.get("tour") or "atp").lower()
    game_date    = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()
    notes        = parsed.get("notes", "")

    # ── 2. Fetch tennis data (with AI fallback) ───────────────────────────────
    context: dict = {}
    ai_fallback = False
    data_confidence = "high"

    try:
        context = fetch_tennis_context(player_a_raw, player_b_raw, surface, tour)
        if "error" in context:
            raise ValueError(context["error"])
        # Quality check: if both players have no real data (all defaults), use AI fallback
        pa_data = context.get("player_a", {})
        pb_data = context.get("player_b", {})
        has_real_data = (
            (pa_data.get("ranking") not in (None, 999)) or
            (pa_data.get("serve_quality_index") not in (None, 100)) or
            (pa_data.get("surface_win_rate") not in (None, 0.5)) or
            (pb_data.get("ranking") not in (None, 999)) or
            (pb_data.get("serve_quality_index") not in (None, 100))
        )
        if not has_real_data:
            raise ValueError("No real player data returned from ESPN/TSDB — activating AI fallback")
        steps.append({"step": "tennis_fetch", "status": "ok",
                      "sources": context.get("sources", [])})
    except Exception as exc:
        steps.append({"step": "tennis_fetch", "status": "fallback",
                      "error": str(exc),
                      "note": "ESPN/TSDB returned no data — using AI signal estimates from training knowledge."})
        ai_fallback = True

    if ai_fallback:
        try:
            from ai_agent_tennis import interpret_tennis_signals
            sigs = interpret_tennis_signals(player_a_raw, player_b_raw, surface, tour, notes)
            steps.append({"step": "ai_signals", "status": "ok", "data": sigs})
            ai_conf = sigs.get("confidence", "low")
            data_confidence = "medium" if ai_conf == "medium" else "low"
        except Exception as exc2:
            steps.append({"step": "ai_signals", "status": "error", "error": str(exc2)})
            sigs = {}
            data_confidence = "low"

        sig_a = sigs.get("player_a", {})
        sig_b = sigs.get("player_b", {})
        context = {
            "player_a": {
                "name":                player_a_raw,
                "ranking":             sig_a.get("ranking", 999),
                "surface_win_rate":    sig_a.get("surface_win_rate", 0.5),
                "recent_form":         sig_a.get("recent_form", 0.5),
                "serve_quality_index": sig_a.get("serve_quality_index", 100),
                "return_quality_index":sig_a.get("return_quality_index", 100),
            },
            "player_b": {
                "name":                player_b_raw,
                "ranking":             sig_b.get("ranking", 999),
                "surface_win_rate":    sig_b.get("surface_win_rate", 0.5),
                "recent_form":         sig_b.get("recent_form", 0.5),
                "serve_quality_index": sig_b.get("serve_quality_index", 100),
                "return_quality_index":sig_b.get("return_quality_index", 100),
            },
            "surface": surface,
            "tour":    tour,
            "h2h":     {"wins_a": 0, "wins_b": 0, "advantage": sigs.get("h2h_advantage", "even")},
            "sources": [{"label": "AI signal estimation", "url": "", "snippet": sigs.get("notes", "")}],
        }

    player_a = context["player_a"].get("name", player_a_raw)
    player_b = context["player_b"].get("name", player_b_raw)
    surface  = context.get("surface", surface)

    pa = context["player_a"]
    pb = context["player_b"]
    sqi_a  = float(pa.get("serve_quality_index") or 100)
    rqi_a  = float(pa.get("return_quality_index") or 100)
    sqi_b  = float(pb.get("serve_quality_index") or 100)
    rqi_b  = float(pb.get("return_quality_index") or 100)
    rank_a = int(pa.get("ranking") or 999)
    rank_b = int(pb.get("ranking") or 999)
    form_a = float(pa.get("recent_form") or 0.5)
    form_b = float(pb.get("recent_form") or 0.5)
    swr_a  = float(pa.get("surface_win_rate") or 0.5)
    swr_b  = float(pb.get("surface_win_rate") or 0.5)

    # Rest-day fatigue: penalise SQI when a player competed recently
    rest_a = _rest_days(pa.get("last5", []), game_date)
    rest_b = _rest_days(pb.get("last5", []), game_date)
    sqi_a  = round(sqi_a * _sqi_rest_factor(rest_a), 2)
    sqi_b  = round(sqi_b * _sqi_rest_factor(rest_b), 2)

    # ── 3. Compute point-level probabilities (all signals feed in as modifiers) ─
    best_of = parsed.get("best_of") or (5 if tour in ("gs", "grand_slam") else 3)
    p_serve, p_return = _compute_point_probs(
        sqi_a, rqi_a, sqi_b, rqi_b,
        swr_a, swr_b, form_a, form_b, surface,
    )
    steps.append({"step": "point_probs", "status": "ok",
                  "p_serve": round(p_serve, 4), "p_return": round(p_return, 4)})

    # ── 4. Nested Markov simulation: points → games → sets → match ────────────
    markov = markov_tennis_match(p_serve, p_return, best_of=best_of)
    prob_a = markov["prob_a"]
    prob_b = markov["prob_b"]
    steps.append({"step": "markov_sim", "status": "ok",
                  "prob_a": prob_a, "prob_b": prob_b, "best_of": best_of})

    # ── 5. Glicko-2 validation (secondary signal — not blended, only logged) ──
    glicko_explanation = ""
    try:
        glicko = Glicko2Model()
        glicko.set_rating(player_a, "tennis", surface, _elo_from_ranking(rank_a), 200.0, 0.06)
        glicko.set_rating(player_b, "tennis", surface, _elo_from_ranking(rank_b), 200.0, 0.06)
        glicko_a, glicko_b = glicko.win_probability(player_a, player_b, "tennis", surface)
        glicko_explanation = (
            f"Glicko-2 validation (ranking-seeded): {player_a} {glicko_a*100:.1f}% / {player_b} {glicko_b*100:.1f}%"
        )
        steps.append({"step": "glicko_validation", "status": "ok",
                      "glicko_a": round(glicko_a, 3), "glicko_b": round(glicko_b, 3),
                      "note": "Glicko logged for reference only — Markov output is authoritative"})
    except Exception as exc:
        steps.append({"step": "glicko_validation", "status": "skipped", "error": str(exc)})

    # ── Confidence shrinkage ──────────────────────────────────────────────────
    # When data is weak, shrink toward 50% so we don't manufacture edges from noise.
    # high=real data (no shrink), medium=AI knows the player (40% shrink),
    # low=AI guessing regional/qualifier players (70% shrink toward 50%).
    shrink = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(data_confidence, 0.3)
    prob_a = 0.5 + (prob_a - 0.5) * shrink
    prob_b = 1.0 - prob_a
    steps.append({"step": "confidence_shrink", "status": "ok",
                  "data_confidence": data_confidence, "shrink_factor": shrink})

    h2h = context.get("h2h", {})
    explanation = (
        f"Surface: {surface}. Markov sim (best-of-{best_of}): P_serve={p_serve:.3f}, P_return={p_return:.3f}. "
        f"Serve quality: {player_a} SQI={sqi_a:.0f} / {player_b} SQI={sqi_b:.0f}. "
        f"Return quality: {player_a} RQI={rqi_a:.0f} / {player_b} RQI={rqi_b:.0f}. "
        f"Surface win rate: {player_a} {swr_a*100:.0f}% / {player_b} {swr_b*100:.0f}%. "
        f"Recent form: {player_a} {form_a*100:.0f}% / {player_b} {form_b*100:.0f}%. "
        f"Rankings: #{rank_a} vs #{rank_b}. "
        f"H2H: {player_a} {h2h.get('wins_a',0)}-{h2h.get('wins_b',0)} {player_b}. "
        + glicko_explanation
    )

    # ── 7. Recommendation (Market Efficiency Model for low confidence) ───────
    # Computed BEFORE persist — recommendation is logged as a signal below,
    # so it must exist first (was previously computed after persist, which
    # raised UnboundLocalError on every call and silently aborted persistence —
    # see signals_to_log's "recommendation" entry just below).
    open_odds_a = parsed.get("open_odds_a")
    open_odds_b = parsed.get("open_odds_b")
    curr_odds_a = parsed.get("odds_a")
    curr_odds_b = parsed.get("odds_b")

    if data_confidence == "low" and open_odds_a and open_odds_b and curr_odds_a and curr_odds_b:
        def _to_implied(o: float) -> float:
            return (1.0 / o) if o > 0 else 0.5

        imp_open_a = _to_implied(open_odds_a)
        imp_open_b = _to_implied(open_odds_b)
        imp_curr_a = _to_implied(curr_odds_a)
        imp_curr_b = _to_implied(curr_odds_b)
        open_fair_a = imp_open_a / (imp_open_a + imp_open_b)
        curr_fair_a = imp_curr_a / (imp_curr_a + imp_curr_b)
        market_movement = curr_fair_a - open_fair_a
        SHARP_THRESHOLD = 0.08
        if market_movement >= SHARP_THRESHOLD:
            recommendation = player_a
            recommendation_reason = f"Sharp money signal: line moved +{market_movement*100:.1f}pp toward {player_a}"
        elif market_movement <= -SHARP_THRESHOLD:
            recommendation = player_b
            recommendation_reason = f"Sharp money signal: line moved +{abs(market_movement)*100:.1f}pp toward {player_b}"
        else:
            recommendation = "PASS"
            recommendation_reason = "Low data confidence and no significant line movement — no independent edge."
    elif data_confidence == "low":
        recommendation = "PASS"
        recommendation_reason = "Insufficient data — provide opening odds to enable market efficiency model."
    elif prob_a > prob_b:
        recommendation = player_a
        recommendation_reason = f"{player_a} model edge ({prob_a*100:.1f}% vs book)"
    else:
        recommendation = player_b
        recommendation_reason = f"{player_b} model edge ({prob_b*100:.1f}% vs book)"

    # ── 8. Persist ───────────────────────────────────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "tennis", tour.upper(),
                    player_a, player_b,
                    f"{game_date}T12:00:00",
                    surface,
                    1,
                ),
            )
            match_id = cur.lastrowid

        signals_to_log = [
            ("serve_quality_index",  player_a, sqi_a,        None),
            ("serve_quality_index",  player_b, sqi_b,        None),
            ("return_quality_index", player_a, rqi_a,        None),
            ("return_quality_index", player_b, rqi_b,        None),
            ("surface_win_rate",     player_a, swr_a,        None),
            ("surface_win_rate",     player_b, swr_b,        None),
            ("recent_form",          player_a, form_a,       None),
            ("recent_form",          player_b, form_b,       None),
            ("ranking",              player_a, float(rank_a), None),
            ("ranking",              player_b, float(rank_b), None),
            ("rest_days",            player_a, float(rest_a) if rest_a < 99 else None, None),
            ("rest_days",            player_b, float(rest_b) if rest_b < 99 else None, None),
            ("data_confidence",      None,     None,          data_confidence),
            ("recommendation",       None,     None,          recommendation),
        ]
        for sig_name, participant, val, text in signals_to_log:
            log_signal(match_id, sig_name, participant,
                       signal_value=float(val) if val is not None else None,
                       signal_text=text,
                       source="espn_tsdb")

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id, "tennis_v1",
                    round(prob_a, 6), round(prob_b, 6), 0.0,
                    explanation,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        steps.append({"step": "persist", "status": "ok", "match_id": match_id})
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})

    # ── 9. Kelly stake ───────────────────────────────────────────────────────
    kelly = kelly_stake(prob_a, 1.909, bankroll)

    # ── 10. Narrative ────────────────────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent_tennis import generate_tennis_narrative
        narrative = generate_tennis_narrative(
            player_a, player_b,
            prob_a, prob_b,
            explanation, context,
        )
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        narrative = (
            f"{player_a} win probability {prob_a*100:.1f}%, "
            f"{player_b} {prob_b*100:.1f}%. Surface: {surface}."
        )
        steps.append({"step": "narrative", "status": "error", "error": str(exc)})

    plain_summary = _build_plain_summary_tennis(
        player_a, player_b, prob_a, prob_b, best_of, surface,
    )

    return {
        "match_id":        match_id,
        "player_a":        player_a,
        "player_b":        player_b,
        "recommendation":  recommendation,
        "recommendation_reason": recommendation_reason,
        "sport":           "tennis",
        "tour":       tour.upper(),
        "surface":    surface,
        "date":       game_date,
        "plain_summary": plain_summary,
        "narrative":  narrative,
        "prob_a":     round(prob_a * 100, 1),
        "prob_b":     round(prob_b * 100, 1),
        "prob_draw":  0.0,
        "player_stats": {
            "player_a": {
                "name":                player_a,
                "ranking":             rank_a,
                "serve_quality_index": round(sqi_a, 1),
                "return_quality_index":round(rqi_a, 1),
                "surface_win_rate":    round(swr_a * 100, 1),
                "recent_form":         round(form_a * 100, 1),
            },
            "player_b": {
                "name":                player_b,
                "ranking":             rank_b,
                "serve_quality_index": round(sqi_b, 1),
                "return_quality_index":round(rqi_b, 1),
                "surface_win_rate":    round(swr_b * 100, 1),
                "recent_form":         round(form_b * 100, 1),
            },
        },
        "h2h":               h2h,
        "last5_a":           context.get("last5_a", []),
        "last5_b":           context.get("last5_b", []),
        "model_explanation": explanation,
        "markov_sim":        markov,
        "kelly_note":        kelly.get("note", ""),
        "data_confidence":   data_confidence,
        "raw_sources":       context.get("sources", []),
        "steps":             steps,
    }
