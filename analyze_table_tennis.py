"""
End-to-end table tennis analysis pipeline.
query → TSDB → AQI/RQI + style model → Glicko-2 per tour → narrative → result dict
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
    Return a prob nudge for style matchup. Attacker vs Defender is the key axis in TT.
    Returns float in [-0.05, +0.05] to add to prob_a.
    """
    a = (style_a or "").lower()
    b = (style_b or "").lower()
    # Attacker beats chopper/defender slightly more often historically
    if "attack" in a and ("defend" in b or "chop" in b):
        return 0.04
    if ("defend" in a or "chop" in a) and "attack" in b:
        return -0.04
    return 0.0


def run_table_tennis_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full table tennis pipeline:
    1. Parse query (Claude)
    2. Fetch TSDB data — rankings, form, H2H
    3. AQI/RQI model probability
    4. Style matchup adjustment
    5. Recent form adjustment
    6. Glicko-2 blend (seeded from ITTF ranking)
    7. Persist match + signals + prediction to DB
    8. Kelly stake sizing
    9. Generate narrative (Claude)
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
    style_a = str(pa.get("style") or "all-round")
    style_b = str(pb.get("style") or "all-round")

    # ── 3. AQI/RQI model ─────────────────────────────────────────────────────
    prob_a_attack = _attack_return_win_prob(aqi_a, rqi_b, aqi_b, rqi_a)
    steps.append({"step": "attack_model", "status": "ok",
                  "prob_a": round(prob_a_attack, 3), "prob_b": round(1 - prob_a_attack, 3)})

    # ── 4. Style matchup adjustment ───────────────────────────────────────────
    style_nudge = _style_edge(style_a, style_b)
    prob_a_style = min(0.95, max(0.05, prob_a_attack + style_nudge))
    prob_b_style = 1.0 - prob_a_style

    # ── 5. Recent form adjustment (25% weight) ────────────────────────────────
    form_prob_a = form_a / (form_a + form_b) if (form_a + form_b) > 0 else 0.5
    prob_a_form = 0.75 * prob_a_style + 0.25 * form_prob_a
    prob_b_form = 1.0 - prob_a_form

    prob_a = prob_a_form
    prob_b = prob_b_form

    # ── 6. Glicko-2 blend (30% weight) ───────────────────────────────────────
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
    explanation = (
        f"AQI: {player_a} {aqi_a:.0f} / {player_b} {aqi_b:.0f}. "
        f"RQI: {player_a} {rqi_a:.0f} / {player_b} {rqi_b:.0f}. "
        f"Style: {player_a} {style_a} vs {player_b} {style_b}. "
        f"Recent form: {player_a} {form_a*100:.0f}% / {player_b} {form_b*100:.0f}%. "
        f"Rankings: #{rank_a} vs #{rank_b}. "
        f"H2H: {player_a} {h2h.get('wins_a',0)}-{h2h.get('wins_b',0)} {player_b}. "
        + glicko_explanation
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

    return {
        "match_id":   match_id,
        "player_a":   player_a,
        "player_b":   player_b,
        "sport":      "table_tennis",
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
