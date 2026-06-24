"""
End-to-end table tennis analysis pipeline.
query → ITTF/WTT/TSDB → AQI/RQI + style + handedness + fatigue + line movement
      → Markov Chain match simulation → Glicko-2 blend → narrative → result dict
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


def _logistic(x: float, scale: float = 40.0) -> float:
    return 1.0 / (1.0 + math.exp(-x / scale))


def _style_aqi_modifier(style_attacker: str, style_defender: str) -> float:
    """
    Style interacts with AQI at the input level, not as a flat nudge.
    A chopper/defender suppresses the opponent's effective AQI because they
    force high-error underspin rallies — the attacker's loop is less effective.
    Returns a multiplier applied to the opponent's AQI before it enters the model.
    1.0 = no effect. >1.0 = style boosts AQI. <1.0 = style suppresses AQI.
    """
    a = (style_attacker or "").lower()
    b = (style_defender or "").lower()
    # Defender/chopper facing an attacker: attacker's AQI is suppressed ~10%
    if ("defend" in b or "chop" in b) and "attack" in a:
        return 0.90   # attacker's AQI is 10% less effective vs defender
    # Attacker facing a blocker: small suppression
    if "block" in b and "attack" in a:
        return 0.95
    return 1.0


def _attack_return_win_prob(
    aqi_a: float, rqi_a: float,
    aqi_b: float, rqi_b: float,
    style_a: str = "all-round", style_b: str = "all-round",
) -> tuple[float, float, float]:
    """
    TRUE matchup model — separates serve points from return points.

    In table tennis serve alternates every 2 points, so each player
    initiates attack ~50% of the time. We model both halves separately:

      P(A wins serve point)   = logistic(AQI_a_eff - RQI_b)
        — how A's attack pierces B's defense when A serves
      P(A wins return point)  = logistic(RQI_a - AQI_b_eff)
        — how A's defense handles B's attack when B serves

    Style interacts at the AQI input: a chopper suppresses opponent's AQI
    because they force underspin errors, reducing the attacker's effectiveness.

    Returns (prob_a, p_serve_a, p_return_a) for full transparency.
    """
    # Apply style modifier to the ATTACKING player's AQI
    aqi_a_eff = aqi_a * _style_aqi_modifier(style_a, style_b)
    aqi_b_eff = aqi_b * _style_aqi_modifier(style_b, style_a)

    # Normalise to 0-centred (100 = average)
    p_a_serves  = _logistic(aqi_a_eff - rqi_b, scale=40.0)   # A attacks vs B defends
    p_a_returns = _logistic(rqi_a - aqi_b_eff, scale=40.0)   # A defends vs B attacks

    # Equal serve distribution (50/50 in TT — serve alternates)
    prob_a = 0.5 * p_a_serves + 0.5 * p_a_returns
    return prob_a, p_a_serves, p_a_returns


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


def _fatigue_decay(matches_played: int) -> float:
    """
    Exponential performance decay from intraday match load.
    f(n) = exp(-0.12 * max(0, n-2))
    n=0,1,2 → 1.0 | n=3 → 0.887 | n=4 → 0.787 | n=5 → 0.698
    """
    return math.exp(-0.12 * max(0, matches_played - 2))


def _fatigue_adjustment(matches_today_a: int, matches_today_b: int) -> float:
    """
    Intraday fatigue nudge. Converts per-player exponential decay to a prob shift.
    Returns prob nudge to ADD to prob_a (positive = A is fresher than B).
    matches_today = matches completed BEFORE this match.
    """
    perf_a = _fatigue_decay(matches_today_a)
    perf_b = _fatigue_decay(matches_today_b)
    total = perf_a + perf_b
    if total <= 0:
        return 0.0
    return (perf_a / total) - 0.5  # centred on 0


def _line_movement_edge(
    open_a: Optional[float], open_b: Optional[float],
    curr_a: Optional[float], curr_b: Optional[float],
    circuit: str = "ittf",
) -> float:
    """
    Sharp money signal from line movement (American odds).

    Threshold: 10pp for ITTF/WTT (liquid markets, algorithmic makers).
               5pp for club circuits (Setka/TT Cup) — bookies use basic
               automated pricing; early local syndicate money moves lines fast.

    Returns prob nudge to add to prob_a (+ve = sharp money on A).
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

    move = curr_imp_a - open_imp_a
    threshold = 0.05 if circuit in ("setka", "ttcup", "club", "ukr_dl", "czk_dl") else 0.10
    if abs(move) < threshold:
        return 0.0
    return max(-0.08, min(0.08, move * 0.5))


def _markov_game_prob(p_serve: float, p_return: float) -> float:
    """
    Markov Chain probability that player A wins one game to 11.

    State: (points_a, points_b). From each state:
      - If A is serving: A wins the point with probability p_serve
      - If B is serving: A wins the point with probability p_return
    Serve alternates every 2 points (standard TT rules).
    Deuce (≥10-10) handled with recursive formula.

    We use dynamic programming over the (0..10) x (0..10) grid,
    tracking whose serve it is based on total points played mod 4.
    At deuce we compute the closed-form probability for the infinite game.
    """
    # Memoisation table: dp[a][b][serve_turn] = P(A wins from state (a,b))
    # serve_turn: 0 = A serves, 1 = B serves
    # In standard TT: player serves 2 consecutive points, then switch.
    # Who serves first is random (or decided by toss) — we average both starts.

    from functools import lru_cache

    @lru_cache(maxsize=None)
    def dp(a: int, b: int, served_this_stint: int, server: int) -> float:
        """
        a, b        — current score
        server      — 0 = A serving, 1 = B serving
        served_this_stint — how many points have been served in this 2-point stint
        Returns P(A wins the game from here).
        """
        # Game over conditions
        if a >= 11 and a - b >= 2:
            return 1.0
        if b >= 11 and b - a >= 2:
            return 0.0

        # At deuce (both >= 10), serve alternates every point (1-point stints)
        at_deuce = (a >= 10 and b >= 10)
        if at_deuce:
            # Closed form: from deuce with player X serving one point then switching
            # p_a_deuce = prob A wins a 2-point segment starting from deuce
            # Alternating serve: first point A serves, second point B serves
            # P(A wins segment) = p_serve * p_return  (A wins both)
            #                   + (1-p_serve)*(1-p_return) is P(B wins segment)
            # Remaining: (p_serve*(1-p_return) + (1-p_serve)*p_return) → back to deuce
            p_a_wins_seg = p_serve * p_return
            p_b_wins_seg = (1.0 - p_serve) * (1.0 - p_return)
            p_deuce_again = 1.0 - p_a_wins_seg - p_b_wins_seg
            if p_deuce_again >= 1.0:
                return 0.5  # degenerate
            return p_a_wins_seg / (p_a_wins_seg + p_b_wins_seg)

        # Probability A wins this point
        p_win_point = p_serve if server == 0 else p_return

        # After this point, update stint counter and possibly switch server
        next_stint = served_this_stint + 1
        if next_stint >= 2:
            next_server = 1 - server
            next_stint = 0
        else:
            next_server = server

        p_a_wins  = p_win_point  * dp(a + 1, b, next_stint, next_server)
        p_a_loses = (1.0 - p_win_point) * dp(a, b + 1, next_stint, next_server)
        return p_a_wins + p_a_loses

    # Average over both possible starting servers (toss is 50/50)
    p_a_serves_first  = dp(0, 0, 0, 0)
    p_a_returns_first = dp(0, 0, 0, 1)
    dp.cache_clear()
    return 0.5 * p_a_serves_first + 0.5 * p_a_returns_first


def markov_match_prob(
    p_serve: float,
    p_return: float,
    best_of: int = 7,
) -> dict:
    """
    Simulate a best-of-N match using Markov Chain game probabilities.

    Returns:
      prob_a       — P(A wins match)
      prob_b       — P(B wins match)
      game_prob    — P(A wins any individual game)
      dist         — score distribution {(a_games, b_games): probability}
      expected_games — expected total games in match
    """
    games_needed = (best_of // 2) + 1   # e.g. 4 for best-of-7

    # Per-game probability
    p_game = _markov_game_prob(p_serve, p_return)

    # Build score distribution via DP over (games_a, games_b)
    # dp_match[(ga, gb)] = probability of reaching that score
    dp_match: dict[tuple[int, int], float] = {(0, 0): 1.0}
    finished: dict[tuple[int, int], float] = {}

    while dp_match:
        new_dp: dict[tuple[int, int], float] = {}
        for (ga, gb), prob in dp_match.items():
            # A wins next game
            nga, ngb = ga + 1, gb
            if nga >= games_needed or ngb >= games_needed:
                finished[(nga, ngb)] = finished.get((nga, ngb), 0.0) + prob * p_game
            else:
                new_dp[(nga, ngb)] = new_dp.get((nga, ngb), 0.0) + prob * p_game

            # B wins next game
            nga, ngb = ga, gb + 1
            if nga >= games_needed or ngb >= games_needed:
                finished[(nga, ngb)] = finished.get((nga, ngb), 0.0) + prob * (1.0 - p_game)
            else:
                new_dp[(nga, ngb)] = new_dp.get((nga, ngb), 0.0) + prob * (1.0 - p_game)
        dp_match = new_dp

    prob_a = sum(p for (ga, gb), p in finished.items() if ga > gb)
    prob_b = 1.0 - prob_a
    expected_games = sum((ga + gb) * p for (ga, gb), p in finished.items())

    return {
        "prob_a":          round(prob_a, 4),
        "prob_b":          round(prob_b, 4),
        "game_prob_a":     round(p_game, 4),
        "dist":            {f"{ga}-{gb}": round(p, 4) for (ga, gb), p in sorted(finished.items())},
        "expected_games":  round(expected_games, 2),
    }


def _first_time_premium(h2h_wins_a: int, h2h_wins_b: int,
                         style_a: str, style_b: str) -> float:
    """
    First-time premium: when two players have never met (H2H=0),
    unconventional styles (penhold, chopper, long pips) gain an extra edge
    because the opponent has no film to adapt their game plan.
    Returns prob nudge to add to prob_a (positive = A benefits).
    """
    total_h2h = h2h_wins_a + h2h_wins_b
    if total_h2h > 0:
        return 0.0  # only applies when no prior meetings

    UNCONVENTIONAL = {"penhold", "chopper", "defender", "long pips", "anti"}
    a_unc = any(s in (style_a or "").lower() for s in UNCONVENTIONAL)
    b_unc = any(s in (style_b or "").lower() for s in UNCONVENTIONAL)

    if a_unc and not b_unc:
        return 0.03   # A's style is unfamiliar to B
    if b_unc and not a_unc:
        return -0.03  # B's style is unfamiliar to A
    return 0.0


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
    # ── 2b. Enrich style/hand from local profile DB if context returned defaults ─
    circuit = tour.lower()
    is_club_circuit = circuit in ("setka", "ttcup", "club", "ukr_dl", "czk_dl",
                                   "ukrainian dl", "czech dl", "ittf_club")
    try:
        from fetchers.setka import lookup_player_profile, get_matches_today
        for key, player_name, pdata in [("player_a", player_a, pa), ("player_b", player_b, pb)]:
            if pdata.get("style", "all-round") == "all-round":
                profile = lookup_player_profile(player_name)
                if profile:
                    pdata["style"]     = profile.get("style", pdata.get("style", "all-round"))
                    pdata["handedness"]= profile.get("hand",  pdata.get("handedness", "right"))
                    steps.append({"step": "profile_lookup", "status": "ok",
                                  "player": player_name, "profile": profile})

        # Auto-fetch intraday match count if caller didn't supply it
        if matches_today_a == 0 and is_club_circuit:
            matches_today_a = get_matches_today(player_a, game_date)
        if matches_today_b == 0 and is_club_circuit:
            matches_today_b = get_matches_today(player_b, game_date)
    except Exception as exc:
        steps.append({"step": "profile_lookup", "status": "error", "error": str(exc)})

    style_a = str(pa.get("style") or "all-round")
    style_b = str(pb.get("style") or "all-round")
    hand_a  = str(pa.get("handedness") or "right")
    hand_b  = str(pb.get("handedness") or "right")

    # ── 3. True serve/return matchup model (Gemini fix) ──────────────────────
    # Style now modifies AQI inputs, not a post-hoc nudge.
    prob_a_matchup, p_serves, p_returns = _attack_return_win_prob(
        aqi_a, rqi_a, aqi_b, rqi_b, style_a, style_b,
    )
    steps.append({"step": "attack_model", "status": "ok",
                  "prob_a": round(prob_a_matchup, 3),
                  "p_a_serves": round(p_serves, 3),
                  "p_a_returns": round(p_returns, 3)})

    # ── 4. Markov Chain match simulation ─────────────────────────────────────
    # Uses p_serves and p_returns directly (already style-adjusted above).
    # Simulates exact point-by-point game transitions → match win probability.
    markov_result: dict = {}
    markov_prob_a = prob_a_matchup  # fallback in case simulation errors
    try:
        markov_result = markov_match_prob(p_serves, p_returns, best_of=7)
        markov_prob_a = markov_result["prob_a"]
        steps.append({"step": "markov_sim", "status": "ok",
                      "prob_a": markov_result["prob_a"],
                      "prob_b": markov_result["prob_b"],
                      "game_prob_a": markov_result["game_prob_a"],
                      "expected_games": markov_result["expected_games"]})
    except Exception as exc:
        steps.append({"step": "markov_sim", "status": "error", "error": str(exc)})

    # ── 5. Handedness nudge (additive to Markov prob) ─────────────────────────
    hand_nudge  = _handedness_edge(hand_a, hand_b)
    prob_a_hand = min(0.95, max(0.05, markov_prob_a + hand_nudge))
    steps.append({"step": "handedness", "status": "ok",
                  "hand_a": hand_a, "hand_b": hand_b, "nudge": round(hand_nudge, 3)})

    # ── 6. First-time premium ─────────────────────────────────────────────────
    h2h_wins_a = context.get("h2h", {}).get("wins_a", 0)
    h2h_wins_b = context.get("h2h", {}).get("wins_b", 0)
    first_time_nudge = _first_time_premium(h2h_wins_a, h2h_wins_b, style_a, style_b)
    prob_a_hand = min(0.95, max(0.05, prob_a_hand + first_time_nudge))
    if first_time_nudge != 0.0:
        steps.append({"step": "first_time_premium", "status": "ok",
                      "nudge": round(first_time_nudge, 3),
                      "note": "No prior H2H — unconventional style premium applied"})

    # ── 7–9. Single weighted blend (Gemini fix — no more sequential dampening)
    # All signals fed into one blend rather than chained 75/25 then 70/30.
    # Weights: Markov+handedness 40%, form 20%, Glicko 30%, fatigue+line 10%

    # Compute each signal as a probability in [0,1]
    form_prob_a   = form_a / (form_a + form_b) if (form_a + form_b) > 0 else 0.5
    fatigue_nudge = _fatigue_adjustment(matches_today_a, matches_today_b)
    line_nudge    = _line_movement_edge(open_odds_a, open_odds_b, curr_odds_a, curr_odds_b, circuit=circuit)

    if matches_today_a > 0 or matches_today_b > 0:
        steps.append({"step": "fatigue", "status": "ok",
                      "matches_today_a": matches_today_a,
                      "matches_today_b": matches_today_b,
                      "decay_a": round(_fatigue_decay(matches_today_a), 3),
                      "decay_b": round(_fatigue_decay(matches_today_b), 3),
                      "nudge": round(fatigue_nudge, 3)})
    if line_nudge != 0.0:
        steps.append({"step": "line_movement", "status": "ok",
                      "open_a": open_odds_a, "open_b": open_odds_b,
                      "curr_a": curr_odds_a, "curr_b": curr_odds_b,
                      "nudge": round(line_nudge, 3)})

    # Glicko signal computed here so it can enter the unified blend
    glicko_a_prob = 0.5
    glicko_explanation = ""
    try:
        glicko = Glicko2Model()
        glicko.set_rating(player_a, "table_tennis", "all", _elo_from_ranking(rank_a), 200.0, 0.06)
        glicko.set_rating(player_b, "table_tennis", "all", _elo_from_ranking(rank_b), 200.0, 0.06)
        glicko_a_prob, glicko_b_prob = glicko.win_probability(player_a, player_b, "table_tennis", "all")
        glicko_explanation = (
            f"Glicko-2 (from ranking): {player_a} {glicko_a_prob*100:.1f}% / "
            f"{player_b} {glicko_b_prob*100:.1f}%"
        )
        steps.append({"step": "glicko_blend", "status": "ok",
                      "glicko_a": round(glicko_a_prob, 3), "glicko_b": round(glicko_b_prob, 3)})
    except Exception as exc:
        steps.append({"step": "glicko_blend", "status": "skipped", "error": str(exc)})

    # Fatigue and line movement as probability signals (centred on 0.5)
    fatigue_prob = min(0.95, max(0.05, 0.5 + fatigue_nudge))
    line_prob    = min(0.95, max(0.05, 0.5 + line_nudge))

    # Unified weighted blend — prevents the extreme-damping Gemini identified
    # Markov sim replaces the plain AQI matchup in the 40% slot
    W_MATCHUP = 0.40   # Markov chain simulation + handedness + first-time
    W_FORM    = 0.20   # recent win rate
    W_GLICKO  = 0.30   # ranking-based Glicko-2
    W_CONTEXT = 0.10   # fatigue + line movement (split equally)

    prob_a = (
        W_MATCHUP * prob_a_hand +
        W_FORM    * form_prob_a +
        W_GLICKO  * glicko_a_prob +
        W_CONTEXT * 0.5 * (fatigue_prob + line_prob)
    )
    prob_b = 1.0 - prob_a

    steps.append({"step": "unified_blend", "status": "ok",
                  "matchup": round(prob_a_hand, 3),
                  "form": round(form_prob_a, 3),
                  "glicko": round(glicko_a_prob, 3),
                  "fatigue_prob": round(fatigue_prob, 3),
                  "line_prob": round(line_prob, 3),
                  "blended_prob_a": round(prob_a, 3)})


    # ── Bayesian prior + confidence shrinkage (Gemini fix) ───────────────────
    # When structural data is missing, use the de-vigged book line as the
    # Bayesian prior instead of 0.50. The book has already priced unknown
    # club players; we use that consensus and only adjust for signals we
    # have independent evidence for.
    book_prior_a = 0.5  # default: no book line available

    def _to_implied(american: float) -> float:
        if american >= 0:
            return 100.0 / (american + 100.0)
        return abs(american) / (abs(american) + 100.0)

    if open_odds_a is not None and open_odds_b is not None:
        imp_a = _to_implied(open_odds_a)
        imp_b = _to_implied(open_odds_b)
        total = imp_a + imp_b
        book_prior_a = imp_a / total  # de-vigged opening line
        steps.append({"step": "book_prior", "status": "ok",
                      "book_prior_a": round(book_prior_a, 3),
                      "note": "De-vigged opening line used as Bayesian prior"})
    elif curr_odds_a is not None and curr_odds_b is not None:
        imp_a = _to_implied(curr_odds_a)
        imp_b = _to_implied(curr_odds_b)
        total = imp_a + imp_b
        book_prior_a = imp_a / total
        steps.append({"step": "book_prior", "status": "ok",
                      "book_prior_a": round(book_prior_a, 3),
                      "note": "De-vigged current line used as Bayesian prior (no opening line)"})

    shrink = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(data_confidence, 0.3)
    # Shrinkage now draws toward the book prior, not a flat 0.50
    prob_a = book_prior_a + (prob_a - book_prior_a) * shrink
    prob_b = 1.0 - prob_a
    steps.append({"step": "confidence_shrink", "status": "ok",
                  "data_confidence": data_confidence,
                  "shrink_factor": shrink,
                  "book_prior_a": round(book_prior_a, 3)})

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
    markov_note = (
        f" Markov sim (best-of-7): {player_a} {markov_result.get('prob_a',0)*100:.1f}%"
        f" | game win prob {markov_result.get('game_prob_a',0)*100:.1f}%"
        f" | expected {markov_result.get('expected_games','?')} games."
        if markov_result else ""
    )
    explanation = (
        f"AQI: {player_a} {aqi_a:.0f} / {player_b} {aqi_b:.0f}. "
        f"RQI: {player_a} {rqi_a:.0f} / {player_b} {rqi_b:.0f}. "
        f"Style: {player_a} {style_a} ({hand_a}) vs {player_b} {style_b} ({hand_b}). "
        f"Recent form: {player_a} {form_a*100:.0f}% / {player_b} {form_b*100:.0f}%. "
        f"Rankings: #{rank_a} vs #{rank_b}. "
        f"H2H: {player_a} {h2h.get('wins_a',0)}-{h2h.get('wins_b',0)} {player_b}. "
        + glicko_explanation + markov_note + line_note + fatigue_note
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
    # For club circuit players (low confidence + no structural data):
    # Switch from Statistical Model to Market Efficiency Model.
    # Tail significant line movement (≥8pp implied prob shift) as sharp signal.
    # The book set early lines algorithmically; sharp money knows things we don't.
    if data_confidence == "low":
        # Compute line movement if we have both open and current
        market_movement = 0.0
        if (open_odds_a is not None and open_odds_b is not None and
                curr_odds_a is not None and curr_odds_b is not None):
            open_imp_a  = _to_implied(open_odds_a)
            open_imp_b  = _to_implied(open_odds_b)
            open_fair_a = open_imp_a / (open_imp_a + open_imp_b)
            curr_imp_a  = _to_implied(curr_odds_a)
            curr_imp_b  = _to_implied(curr_odds_b)
            curr_fair_a = curr_imp_a / (curr_imp_a + curr_imp_b)
            market_movement = curr_fair_a - open_fair_a  # + = money on A

        # Club circuits: 5pp threshold (algorithmic bookmakers, local syndicates move fast)
        # Liquid markets: 8pp threshold
        SHARP_THRESHOLD = 0.05 if is_club_circuit else 0.08

        if market_movement >= SHARP_THRESHOLD:
            recommendation = player_a
            recommendation_reason = (
                f"Sharp money signal: line moved {market_movement*100:+.1f}pp toward "
                f"{player_a} (market efficiency model — no structural data available)"
            )
        elif market_movement <= -SHARP_THRESHOLD:
            recommendation = player_b
            recommendation_reason = (
                f"Sharp money signal: line moved {abs(market_movement)*100:.1f}pp toward "
                f"{player_b} (market efficiency model — no structural data available)"
            )
        else:
            recommendation = "PASS"
            recommendation_reason = (
                "No structural data and no significant line movement — "
                "book line unchanged, no detectable sharp signal."
            )
        steps.append({"step": "market_efficiency", "status": "ok",
                      "market_movement": round(market_movement, 4),
                      "sharp_threshold": SHARP_THRESHOLD})
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
        "markov_sim":        markov_result,
        "h2h":               h2h,
        "last10_a":          context.get("player_a", {}).get("last10", []),
        "last10_b":          context.get("player_b", {}).get("last10", []),
        "model_explanation": explanation,
        "kelly_note":        kelly.get("note", ""),
        "data_confidence":   data_confidence,
        "raw_sources":       context.get("sources", []),
        "steps":             steps,
    }
