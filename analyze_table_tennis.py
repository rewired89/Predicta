"""
End-to-end table tennis analysis pipeline.
query → ITTF/WTT/TSDB → AQI/RQI + style + handedness + fatigue + line movement
      → Glicko-2 per tour → narrative → result dict
"""
from __future__ import annotations
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.table_tennis import fetch_table_tennis_context
from fetchers.signals import log_signal
from models.glicko import Glicko2Model
from models.kelly import kelly_stake


def _elo_from_ranking(ranking: int) -> float:
    if not ranking or ranking <= 0:
        return 1500.0
    return max(1300.0, 2400.0 - 400.0 * math.log10(max(1, ranking)))


def _attack_return_win_prob(aqi_a: float, rqi_b: float, aqi_b: float, rqi_a: float) -> float:
    """
    Estimate win probability from attack/return quality indices.
    100 = tour average. Higher AQI = better attacker; higher RQI = harder to attack against.
    Returns P(player_a wins) in (0, 1).
    """
    adv_a = (aqi_a - 100) - (rqi_b - 100)
    adv_b = (aqi_b - 100) - (rqi_a - 100)
    net   = adv_a - adv_b
    # ±40 ≈ 65%/35% — same sensitivity as tennis model
    return 1.0 / (1.0 + math.exp(-net / 40.0))


def _style_edge(style_a: str, style_b: str) -> float:
    """
    Prob nudge for style matchup — attacker vs defender/chopper is the key axis.
    Returns float in [-0.05, +0.05].
    """
    a = (style_a or "").lower()
    b = (style_b or "").lower()
    if "attack" in a and ("defend" in b or "chop" in b):
        return 0.04
    if ("defend" in a or "chop" in a) and "attack" in b:
        return -0.04
    return 0.0


def _handedness_edge(hand_a: str, hand_b: str) -> float:
    """
    Left-handed players have a structural crossover advantage in TT.
    Academic studies (ITTF analytics, 2019) show lefties beat righties ~54% at equal ranking.
    That is a +4pp edge for the lefty in a right vs left matchup.
    Returns prob nudge to add to prob_a.
    """
    a = (hand_a or "right").lower()
    b = (hand_b or "right").lower()
    a_left = "left" in a
    b_left = "left" in b
    if a_left and not b_left:
        return 0.04   # A is lefty vs righty
    if b_left and not a_left:
        return -0.04  # B is lefty vs righty
    return 0.0        # both same handedness — no edge


def _fatigue_adjustment(matches_today_a: int, matches_today_b: int) -> float:
    """
    Each extra match played earlier in the day costs roughly 3pp.
    Returns prob nudge to ADD to prob_a (positive = A is fresher).
    matches_today = number of matches already completed today BEFORE this one.
    """
    # Net fatigue delta: A's extra matches vs B's extra matches
    delta = matches_today_b - matches_today_a
    return delta * 0.03


def _line_movement_edge(
    open_a: Optional[float], open_b: Optional[float],
    curr_a: Optional[float], curr_b: Optional[float],
) -> float:
    """
    Sharp money signal from line movement (American odds).
    If the line on A moved from +150 to +120, the implied probability increased
    by ~4pp — sharp bettors pushed it. We interpret that as a real signal.

    Returns prob nudge to add to prob_a (+ve = sharp money on A).
    Threshold: only signal if |move| >= 10pp implied probability shift.
    """
    if None in (open_a, open_b, curr_a, curr_b):
        return 0.0

    def to_implied(american: float) -> float:
        if american >= 0:
            return 100.0 / (american + 100.0)
        return abs(american) / (abs(american) + 100.0)

    def devig(imp_a: float, imp_b: float):
        total = imp_a + imp_b
        return imp_a / total, imp_b / total

    open_imp_a, _ = devig(to_implied(open_a), to_implied(open_b))
    curr_imp_a, _ = devig(to_implied(curr_a), to_implied(curr_b))

    move = curr_imp_a - open_imp_a   # positive = money came in on A
    if abs(move) < 0.10:             # ignore noise below 10pp
        return 0.0
    # Cap nudge at ±8pp so one signal can't dominate
    return max(-0.08, min(0.08, move * 0.5))


def run_table_tennis_analysis(
    user_query: str,
    bankroll: float = 1000.0,
    open_odds_a: Optional[float] = None,
    open_odds_b: Optional[float] = None,
    curr_odds_a: Optional[float] = None,
    curr_odds_b: Optional[float] = None,
    matches_today_a: int = 0,
    matches_today_b: int = 0,
) -> dict:
    """
    Full table tennis pipeline:
    1.  Parse query (Claude)
    2.  Fetch ITTF/WTT/TSDB data — rankings, form, H2H
    3.  AQI/RQI model probability
    4.  Style matchup adjustment
    5.  Handedness matchup adjustment  ← NEW
    6.  Recent form adjustment
    7.  Fatigue adjustment             ← NEW
    8.  Line movement signal           ← NEW
    9.  Glicko-2 blend (seeded from ITTF ranking)
    10. Confidence shrinkage
    11. Persist match + signals + prediction to DB
    12. Kelly stake sizing
    13. Generate narrative (Claude)
    """
    init_db()
    steps: list[dict] = []

    # ── 1. Parse ─────────────────────────────────────────────────────────────
    try:
        from ai_agent_table_tennis import parse_table_tennis_query
        parsed = parse_table_tennis_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    player_a_raw = parsed.get("player_a", "Player A")
    player_b_raw = parsed.get("player_b", "Player B")
    tour         = (parsed.get("tour") or "ittf").lower()
    game_date    = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()
    notes        = parsed.get("notes", "")

    # ── 2. Fetch data (with AI fallback) ─────────────────────────────────────
    context: dict = {}
    ai_fallback    = False
    # "high" = real data fetched; "medium" = AI knows these players; "low" = AI guessing
    data_confidence = "high"

    try:
        context = fetch_table_tennis_context(player_a_raw, player_b_raw, tour)
        if "error" in context:
            raise ValueError(context["error"])
        pa_data = context.get("player_a", {})
        pb_data = context.get("player_b", {})
        has_real_data = (
            (pa_data.get("ranking") not in (None, 999)) or
            (pa_data.get("recent_form") not in (None, 0.5)) or
            (pb_data.get("ranking") not in (None, 999)) or
            (pb_data.get("recent_form") not in (None, 0.5))
        )
        if not has_real_data:
            raise ValueError("No real player data returned from TSDB — activating AI fallback")
        steps.append({"step": "tt_fetch", "status": "ok",
                      "sources": context.get("sources", [])})
    except Exception as exc:
        steps.append({"step": "tt_fetch", "status": "fallback",
                      "error": str(exc),
                      "note": "TSDB returned no data — using AI signal estimates from training knowledge."})
        ai_fallback = True

    if ai_fallback:
        try:
            from ai_agent_table_tennis import interpret_table_tennis_signals
            sigs = interpret_table_tennis_signals(player_a_raw, player_b_raw, tour, notes)
            steps.append({"step": "ai_signals", "status": "ok", "data": sigs})
            # Use AI's own confidence assessment
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
                "name":                 player_a_raw,
                "ranking":              sig_a.get("ranking", 999),
                "recent_form":          sig_a.get("recent_form", 0.5),
                "attack_quality_index": sig_a.get("attack_quality_index", 100),
                "return_quality_index": sig_a.get("return_quality_index", 100),
                "style":                sig_a.get("style", "all-round"),
                "handedness":           sig_a.get("handedness", "right"),
            },
            "player_b": {
                "name":                 player_b_raw,
                "ranking":              sig_b.get("ranking", 999),
                "recent_form":          sig_b.get("recent_form", 0.5),
                "attack_quality_index": sig_b.get("attack_quality_index", 100),
                "return_quality_index": sig_b.get("return_quality_index", 100),
                "style":                sig_b.get("style", "all-round"),
                "handedness":           sig_b.get("handedness", "right"),
            },
            "tour":    tour,
            "h2h":     {"wins_a": 0, "wins_b": 0, "advantage": sigs.get("h2h_advantage", "even")},
            "sources": [{"label": "AI signal estimation", "url": "", "snippet": sigs.get("notes", "")}],
        }

    player_a = context["player_a"].get("name", player_a_raw)
    player_b = context["player_b"].get("name", player_b_raw)

    pa     = context["player_a"]
    pb     = context["player_b"]
    aqi_a  = float(pa.get("attack_quality_index") or 100)
    rqi_a  = float(pa.get("return_quality_index") or 100)
    aqi_b  = float(pb.get("attack_quality_index") or 100)
    rqi_b  = float(pb.get("return_quality_index") or 100)
    rank_a = int(pa.get("ranking") or 999)
    rank_b = int(pb.get("ranking") or 999)
    form_a = float(pa.get("recent_form") or 0.5)
    form_b = float(pb.get("recent_form") or 0.5)
    style_a   = str(pa.get("style") or "all-round")
    style_b   = str(pb.get("style") or "all-round")
    hand_a    = str(pa.get("handedness") or "right")
    hand_b    = str(pb.get("handedness") or "right")

    # ── 3. AQI/RQI model ─────────────────────────────────────────────────────
    prob_a_attack = _attack_return_win_prob(aqi_a, rqi_b, aqi_b, rqi_a)
    steps.append({"step": "attack_model", "status": "ok",
                  "prob_a": round(prob_a_attack, 3), "prob_b": round(1 - prob_a_attack, 3)})

    # ── 4. Style matchup adjustment ───────────────────────────────────────────
    style_nudge  = _style_edge(style_a, style_b)
    prob_a_style = min(0.95, max(0.05, prob_a_attack + style_nudge))

    # ── 5. Handedness matchup adjustment ─────────────────────────────────────
    hand_nudge   = _handedness_edge(hand_a, hand_b)
    prob_a_hand  = min(0.95, max(0.05, prob_a_style + hand_nudge))
    steps.append({"step": "handedness", "status": "ok",
                  "hand_a": hand_a, "hand_b": hand_b, "nudge": round(hand_nudge, 3)})

    # ── 6. Recent form adjustment (25% weight) ────────────────────────────────
    form_prob_a = form_a / (form_a + form_b) if (form_a + form_b) > 0 else 0.5
    prob_a_form = 0.75 * prob_a_hand + 0.25 * form_prob_a
    prob_b_form = 1.0 - prob_a_form

    # ── 7. Fatigue adjustment ─────────────────────────────────────────────────
    fatigue_nudge = _fatigue_adjustment(matches_today_a, matches_today_b)
    prob_a_fatigue = min(0.95, max(0.05, prob_a_form + fatigue_nudge))
    prob_b_fatigue = 1.0 - prob_a_fatigue
    if fatigue_nudge != 0.0:
        steps.append({"step": "fatigue", "status": "ok",
                      "matches_today_a": matches_today_a,
                      "matches_today_b": matches_today_b,
                      "nudge": round(fatigue_nudge, 3)})

    # ── 8. Line movement signal ───────────────────────────────────────────────
    line_nudge = _line_movement_edge(open_odds_a, open_odds_b, curr_odds_a, curr_odds_b)
    prob_a_line = min(0.95, max(0.05, prob_a_fatigue + line_nudge))
    prob_b_line = 1.0 - prob_a_line
    if line_nudge != 0.0:
        steps.append({"step": "line_movement", "status": "ok",
                      "open_a": open_odds_a, "open_b": open_odds_b,
                      "curr_a": curr_odds_a, "curr_b": curr_odds_b,
                      "nudge": round(line_nudge, 3)})

    prob_a = prob_a_line
    prob_b = prob_b_line

    # ── 9. Glicko-2 blend (30% weight) ───────────────────────────────────────
    glicko_explanation = ""
    try:
        glicko = Glicko2Model()
        glicko.set_rating(player_a, "table_tennis", "all", _elo_from_ranking(rank_a), 200.0, 0.06)
        glicko.set_rating(player_b, "table_tennis", "all", _elo_from_ranking(rank_b), 200.0, 0.06)
        glicko_a, glicko_b = glicko.win_probability(player_a, player_b, "table_tennis", "all")
        prob_a = 0.7 * prob_a + 0.3 * glicko_a
        prob_b = 0.7 * prob_b + 0.3 * glicko_b
        total  = prob_a + prob_b
        prob_a /= total
        prob_b /= total
        glicko_explanation = (
            f"Glicko-2 (from ranking): {player_a} {glicko_a*100:.1f}% / {player_b} {glicko_b*100:.1f}%"
        )
        steps.append({"step": "glicko_blend", "status": "ok",
                      "glicko_a": round(glicko_a, 3), "glicko_b": round(glicko_b, 3)})
    except Exception as exc:
        steps.append({"step": "glicko_blend", "status": "skipped", "error": str(exc)})

    # ── Confidence shrinkage ──────────────────────────────────────────────────
    # When data is weak, shrink probabilities toward 50% so we don't manufacture
    # false edges against the book from noise. The book has more information than
    # we do on unknown regional players.
    shrink = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(data_confidence, 0.3)
    prob_a = 0.5 + (prob_a - 0.5) * shrink
    prob_b = 1.0 - prob_a
    steps.append({"step": "confidence_shrink", "status": "ok",
                  "data_confidence": data_confidence, "shrink_factor": shrink})

    h2h = context.get("h2h", {})
    line_note = (
        f" Line moved {round(line_nudge*100, 1):+.1f}pp (sharp signal)."
        if line_nudge != 0.0 else ""
    )
    fatigue_note = (
        f" Fatigue: {player_a} played {matches_today_a} match(es) today, "
        f"{player_b} played {matches_today_b}."
        if (matches_today_a + matches_today_b) > 0 else ""
    )
    explanation = (
        f"AQI: {player_a} {aqi_a:.0f} / {player_b} {aqi_b:.0f}. "
        f"RQI: {player_a} {rqi_a:.0f} / {player_b} {rqi_b:.0f}. "
        f"Style: {player_a} {style_a} ({hand_a}) vs {player_b} {style_b} ({hand_b}). "
        f"Recent form: {player_a} {form_a*100:.0f}% / {player_b} {form_b*100:.0f}%. "
        f"Rankings: #{rank_a} vs #{rank_b}. "
        f"H2H: {player_a} {h2h.get('wins_a',0)}-{h2h.get('wins_b',0)} {player_b}. "
        + glicko_explanation + line_note + fatigue_note
    )

    # ── 7. Persist ────────────────────────────────────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("table_tennis", tour.upper(), player_a, player_b,
                 f"{game_date}T12:00:00", "table", 1),
            )
            match_id = cur.lastrowid

        signals_to_log = [
            ("attack_quality_index",  player_a, aqi_a),
            ("attack_quality_index",  player_b, aqi_b),
            ("return_quality_index",  player_a, rqi_a),
            ("return_quality_index",  player_b, rqi_b),
            ("recent_form",           player_a, form_a),
            ("recent_form",           player_b, form_b),
            ("ranking",               player_a, float(rank_a)),
            ("ranking",               player_b, float(rank_b)),
        ]
        for sig_name, participant, val in signals_to_log:
            log_signal(match_id, sig_name, participant,
                       signal_value=float(val), source="tsdb")

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (match_id, "table_tennis_v1",
                 round(prob_a, 6), round(prob_b, 6), 0.0,
                 explanation,
                 datetime.now(timezone.utc).isoformat()),
            )
        steps.append({"step": "persist", "status": "ok", "match_id": match_id})
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})

    # ── 8. Kelly stake ────────────────────────────────────────────────────────
    kelly = kelly_stake(prob_a, 1.909, bankroll)

    # ── 9. Narrative ──────────────────────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent_table_tennis import generate_table_tennis_narrative
        narrative = generate_table_tennis_narrative(
            player_a, player_b, prob_a, prob_b, explanation, context,
        )
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        narrative = (
            f"{player_a} win probability {prob_a*100:.1f}%, "
            f"{player_b} {prob_b*100:.1f}%."
        )
        steps.append({"step": "narrative", "status": "error", "error": str(exc)})

    # ── 10. Recommendation ───────────────────────────────────────────────────
    # No-bet rule: when we have no independent data, defer to the book.
    if data_confidence == "low":
        recommendation = "PASS"
        recommendation_reason = "Insufficient data on these players — model cannot find an independent edge."
    elif prob_a > prob_b:
        recommendation = player_a
        recommendation_reason = f"{player_a} model edge ({prob_a*100:.1f}% vs book)"
    else:
        recommendation = player_b
        recommendation_reason = f"{player_b} model edge ({prob_b*100:.1f}% vs book)"

    return {
        "match_id":        match_id,
        "player_a":        player_a,
        "player_b":        player_b,
        "recommendation":  recommendation,
        "recommendation_reason": recommendation_reason,
        "sport":           "table_tennis",
        "tour":       tour.upper(),
        "date":       game_date,
        "narrative":  narrative,
        "prob_a":     round(prob_a * 100, 1),
        "prob_b":     round(prob_b * 100, 1),
        "prob_draw":  0.0,
        "player_stats": {
            "player_a": {
                "name":                 player_a,
                "ranking":              rank_a,
                "attack_quality_index": round(aqi_a, 1),
                "return_quality_index": round(rqi_a, 1),
                "recent_form":          round(form_a * 100, 1),
                "style":                style_a,
            },
            "player_b": {
                "name":                 player_b,
                "ranking":              rank_b,
                "attack_quality_index": round(aqi_b, 1),
                "return_quality_index": round(rqi_b, 1),
                "recent_form":          round(form_b * 100, 1),
                "style":                style_b,
            },
        },
        "h2h":               h2h,
        "last10_a":          context.get("player_a", {}).get("last10", []),
        "last10_b":          context.get("player_b", {}).get("last10", []),
        "model_explanation": explanation,
        "kelly_note":        kelly.get("note", ""),
        "data_confidence":   data_confidence,
        "raw_sources":       context.get("sources", []),
        "steps":             steps,
    }
