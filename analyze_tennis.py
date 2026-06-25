"""
End-to-end tennis analysis pipeline.
query → ESPN/TSDB → Glicko-2 per-surface + serve model → narrative → result dict
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.tennis import fetch_tennis_context
from fetchers.signals import log_signal
from models.glicko import Glicko2Model
from models.kelly import kelly_stake


def _elo_from_ranking(ranking: int) -> float:
    """Seed Glicko-2 from ATP/WTA ranking. Rank 1 ≈ 2400, Rank 50 ≈ 2000, Rank 500+ → 1500."""
    if not ranking or ranking <= 0:
        return 1500.0
    return max(1300.0, 2400.0 - 400.0 * math.log10(max(1, ranking)))


def _serve_return_win_prob(sqi_a: float, rqi_b: float, sqi_b: float, rqi_a: float) -> float:
    """
    Estimate win probability from serve/return quality indices.
    Each index is 100 = tour average.  Higher serve quality and lower opponent
    return quality both help.  Returns P(player_a wins) in (0, 1).
    """
    # Advantage on serve games: (SQI_a - RQI_b) relative to centre
    adv_a = (sqi_a - 100) - (rqi_b - 100)   # positive → A has serve edge
    adv_b = (sqi_b - 100) - (rqi_a - 100)   # positive → B has serve edge
    net = adv_a - adv_b
    # Map net advantage to probability via logistic; ±40 ≈ 65%/35%
    prob_a = 1.0 / (1.0 + math.exp(-net / 40.0))
    return prob_a


def run_tennis_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full tennis pipeline:
    1. Parse query (Claude)
    2. Fetch ESPN/TSDB data — rankings, serve stats, form, H2H
    3. Serve/return model probability
    4. Blend with Glicko-2 seeded from ranking
    5. Persist match + signals + prediction to DB
    6. Kelly stake sizing
    7. Generate narrative (Claude)
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

    # ── 3. Serve/return model ─────────────────────────────────────────────────
    prob_a_serve = _serve_return_win_prob(sqi_a, rqi_b, sqi_b, rqi_a)
    prob_b_serve = 1.0 - prob_a_serve
    steps.append({"step": "serve_model", "status": "ok",
                  "prob_a": round(prob_a_serve, 3), "prob_b": round(prob_b_serve, 3)})

    # ── 4. Surface win-rate adjustment ────────────────────────────────────────
    # Blend serve model 60% + surface win rate difference 40%
    swr_prob_a = swr_a / (swr_a + swr_b) if (swr_a + swr_b) > 0 else 0.5
    prob_a_surface = 0.6 * prob_a_serve + 0.4 * swr_prob_a
    prob_b_surface = 1.0 - prob_a_surface

    # ── 5. Recent form adjustment ─────────────────────────────────────────────
    form_prob_a = form_a / (form_a + form_b) if (form_a + form_b) > 0 else 0.5
    prob_a_form = 0.75 * prob_a_surface + 0.25 * form_prob_a
    prob_b_form = 1.0 - prob_a_form

    prob_a = prob_a_form
    prob_b = prob_b_form

    # ── 6. Glicko-2 blend (30% weight) ───────────────────────────────────────
    glicko_explanation = ""
    try:
        glicko = Glicko2Model()
        glicko.set_rating(player_a, "tennis", surface, _elo_from_ranking(rank_a), 200.0, 0.06)
        glicko.set_rating(player_b, "tennis", surface, _elo_from_ranking(rank_b), 200.0, 0.06)
        glicko_a, glicko_b = glicko.win_probability(player_a, player_b, "tennis", surface)
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
        f"Surface: {surface}. "
        f"Serve quality: {player_a} SQI={sqi_a:.0f} / {player_b} SQI={sqi_b:.0f}. "
        f"Return quality: {player_a} RQI={rqi_a:.0f} / {player_b} RQI={rqi_b:.0f}. "
        f"Surface win rate: {player_a} {swr_a*100:.0f}% / {player_b} {swr_b*100:.0f}%. "
        f"Recent form: {player_a} {form_a*100:.0f}% / {player_b} {form_b*100:.0f}%. "
        f"Rankings: #{rank_a} vs #{rank_b}. "
        f"H2H: {player_a} {h2h.get('wins_a',0)}-{h2h.get('wins_b',0)} {player_b}. "
        + glicko_explanation
    )

    # ── 7. Persist ───────────────────────────────────────────────────────────
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
            ("serve_quality_index",  player_a, sqi_a),
            ("serve_quality_index",  player_b, sqi_b),
            ("return_quality_index", player_a, rqi_a),
            ("return_quality_index", player_b, rqi_b),
            ("surface_win_rate",     player_a, swr_a),
            ("surface_win_rate",     player_b, swr_b),
            ("recent_form",          player_a, form_a),
            ("recent_form",          player_b, form_b),
            ("ranking",              player_a, float(rank_a)),
            ("ranking",              player_b, float(rank_b)),
        ]
        for sig_name, participant, val in signals_to_log:
            log_signal(match_id, sig_name, participant,
                       signal_value=float(val), source="espn_tsdb")

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

    # ── 8. Kelly stake ───────────────────────────────────────────────────────
    kelly = kelly_stake(prob_a, 1.909, bankroll)

    # ── 9. Narrative ─────────────────────────────────────────────────────────
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

    # ── 10. Recommendation ───────────────────────────────────────────────────
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
        "sport":           "tennis",
        "tour":       tour.upper(),
        "surface":    surface,
        "date":       game_date,
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
        "kelly_note":        kelly.get("note", ""),
        "data_confidence":   data_confidence,
        "raw_sources":       context.get("sources", []),
        "steps":             steps,
    }
