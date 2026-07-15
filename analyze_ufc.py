"""
End-to-end UFC analysis pipeline.

  query → Sherdog record/bio/fight history + FightMatrix ranking (both fighters)
        → Glicko-2 rating (models/glicko.py, sport="ufc", surface=weight_class)
        → blend with a stats-based logistic (models/ufc_model.py)
        → method-of-victory model (KO/TKO, submission, decision)
        → market comparison + Kelly stake
        → narrative
        → result dict

CHANGED 2026-07-15 (third time same day): Sherdog is now the ONLY
fight-history source (fetchers/ufc.py:enrich_ufc_fighters) — a same-day
Wikipedia fallback was tried and then dropped per direct user request
("real-time stats, not non-updated shit from Wikipedia"). If Sherdog can't
resolve either fighter, this pipeline halts and returns an explicit
"Unable to fetch data" response rather than proceeding on FightMatrix
ranking alone. ESPN is dropped from the active pipeline too (its code
stays in fetchers/ufc.py, just unused here) after its /scoreboard
endpoint's historical-range behavior stayed unconfirmed through repeated
live tests. FightMatrix ranking is still layered in as a supplementary
signal on top of real Sherdog data; Tapology stays disabled.

Mirrors analyze_rugby.py's shape. Key differences from every score-based
sport in this repo (soccer/baseball/rugby):
  - No score to model at all — win probability comes from Glicko-2 + stats,
    not a Poisson/NB matrix.
  - No home/away, no draw handling (UFC draws are rare and NOT modeled here —
    prob_draw is always reported as 0.0, an explicit "not modeled" choice,
    not a fabricated near-zero estimate).
  - A whole second model (method_of_victory) for HOW the fight ends, which
    has no equivalent anywhere else in this codebase.
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.signals import log_signal
from fetchers.ufc import enrich_ufc_fighters
from models.glicko import Glicko2Model
from models.ufc_model import (
    composite_win_prob, method_of_victory, explain, MODEL_PROB_CAP,
)
from models.kelly import kelly_stake, american_to_decimal, market_edge_summary


def _safe_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _bet_recommendations(
    prob_a: float, prob_b: float,
    fighter_a: str, fighter_b: str,
    edge_summary: Optional[dict],
    partial_data: bool = False,
) -> list[dict]:
    """
    Same conservative-by-default posture as analyze_rugby.py's
    _bet_recommendations (65%/62pp no-odds, 3.5pp/1pp with odds) — this is a
    brand-new, zero-resolved-predictions model, so start cautious rather than
    tightening thresholds after early losses (CLAUDE.md Bug 3).
    """
    leader_prob = max(prob_a, prob_b)
    leader = fighter_a if prob_a >= prob_b else fighter_b

    edge_bet   = 0.05 if partial_data else 0.035
    edge_lean  = 0.02 if partial_data else 0.01
    no_odds_bet  = 0.68 if partial_data else 0.65
    no_odds_lean = 0.60 if partial_data else 0.58

    recs: list[dict] = []
    if edge_summary and edge_summary.get("has_real_odds"):
        edge_a = edge_summary["edge_a"]
        edge_b = edge_summary["edge_b"]
        best_edge = max(edge_a, edge_b)
        best_side = fighter_a if edge_a >= edge_b else fighter_b
        if best_edge >= edge_bet:
            verdict, confidence = "BET", ("high" if best_edge >= edge_bet + 0.03 else "medium")
            reasons = [f"+{best_edge*100:.1f}pp edge vs market on {best_side}"]
            skip = ""
        elif best_edge >= edge_lean:
            verdict, confidence = "LEAN", "low"
            reasons = [f"+{best_edge*100:.1f}pp slight edge on {best_side}"]
            skip = ""
        else:
            verdict, confidence = "PASS", None
            reasons = []
            skip = f"No {edge_bet*100:.1f}pp edge vs market (best {best_edge*100:.1f}pp on {best_side})"
        recs.append({
            "market": "Moneyline (vs market)", "verdict": verdict,
            "bet": f"{best_side} moneyline" if verdict in ("BET", "LEAN") else "",
            "edge_pp": round(best_edge * 100, 2), "threshold_pp": edge_bet * 100,
            "partial_data": partial_data, "confidence": confidence,
            "reasons": reasons, "skip_reason": skip,
        })
    else:
        if leader_prob >= no_odds_bet:
            verdict, confidence = "BET", ("high" if leader_prob >= no_odds_bet + 0.08 else "medium")
            reasons = [f"{leader} {leader_prob*100:.1f}% model win probability"]
            skip = ""
        elif leader_prob >= no_odds_lean:
            verdict, confidence = "LEAN", "low"
            reasons = [f"{leader} marginal favourite, {leader_prob*100:.1f}%"]
            skip = ""
        else:
            verdict, confidence = "PASS", None
            reasons = []
            skip = f"Coin-flip range — {leader} only {leader_prob*100:.1f}% (need {no_odds_bet*100:.0f}%)"
        recs.append({
            "market": "Moneyline (no market odds)", "verdict": verdict,
            "bet": f"{leader} moneyline" if verdict in ("BET", "LEAN") else "",
            "model_prob_pct": round(leader_prob * 100, 1),
            "threshold_pct": no_odds_bet * 100, "partial_data": partial_data,
            "confidence": confidence, "reasons": reasons, "skip_reason": skip,
        })
    return recs


def run_ufc_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full UFC pipeline.
      1. Parse query → fighter_a/fighter_b/weight_class/odds via Claude
      2. Resolve both fighters against FightMatrix (ranking) + Tapology (record/bio/history)
      3. Glicko-2 rating (scoped by weight_class) + stats-based logistic blend
      4. Method-of-victory model for both possible winners
      5. Compute markets (moneyline, method of victory, goes-the-distance) + bet recs
      6. Persist match + signals + prediction
      7. Narrate
    """
    init_db()
    steps: list[dict] = []

    try:
        from ai_agent_ufc import parse_ufc_query
        parsed = parse_ufc_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    fighter_a = parsed.get("fighter_a") or "Fighter A"
    fighter_b = parsed.get("fighter_b") or "Fighter B"
    weight_class = parsed.get("weight_class") or "all"
    match_date = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()
    notes = parsed.get("notes") or ""

    def _as_decimal(american, decimal_):
        if decimal_ is not None:
            try: return float(decimal_)
            except Exception: pass
        if american is not None:
            try: return american_to_decimal(float(american))
            except Exception: pass
        return None

    odds_a = _as_decimal(parsed.get("odds_a_american"), parsed.get("odds_a_decimal"))
    odds_b = _as_decimal(parsed.get("odds_b_american"), parsed.get("odds_b_decimal"))
    has_odds = odds_a is not None and odds_b is not None

    # ── Sherdog record + FightMatrix ranking enrichment ─────────────────────
    # CHANGED 2026-07-15 (per direct user request — "real-time stats, not
    # non-updated shit from Wikipedia"): Sherdog is the ONLY fight-history
    # source, no fallback. enrich_ufc_fighters reports sherdog_resolved per
    # fighter; this halts with an explicit "Unable to fetch data" response
    # when either side is False, instead of quietly proceeding on
    # FightMatrix ranking alone.
    try:
        enriched = enrich_ufc_fighters(fighter_a, fighter_b, before_date=match_date)
        a_resolved = enriched["a"].get("sherdog_resolved", False)
        b_resolved = enriched["b"].get("sherdog_resolved", False)
        steps.append({
            "step": "fighter_enrich",
            "status": "ok" if (a_resolved and b_resolved) else "partial",
            "a_sherdog_resolved": a_resolved, "b_sherdog_resolved": b_resolved,
        })
    except Exception as exc:
        enriched = {"a": {}, "b": {}}
        a_resolved = b_resolved = False
        steps.append({"step": "fighter_enrich", "status": "error", "error": str(exc)})

    if not a_resolved or not b_resolved:
        missing = [n for n, ok in ((fighter_a, a_resolved), (fighter_b, b_resolved)) if not ok]
        steps.append({
            "step": "insufficient_data", "status": "halt",
            "reason": f"Sherdog could not fetch real fight-history data for: {', '.join(missing)}.",
        })
        return {
            "status": "insufficient_data", "sport": "ufc",
            "fighter_a": fighter_a, "fighter_b": fighter_b, "date": match_date,
            "recommendation": "PASS",
            "narrative": (
                f"Unable to fetch data for {', '.join(missing)}. "
                "Recommended action: PASS. We do not generate predictions from "
                "training-data hallucinations or stale fallback sources because "
                "they cannot be verified against real, current career stats."
            ),
            "data_confidence": "none", "steps": steps,
        }

    # FIXED 2026-07-15 (live-tested — user reported PASS on every fight
    # regardless of how lopsided the win probability looked): this used to
    # key "full" vs "minimal" off fm_rank, but FightMatrix's ranking table
    # is confirmed JS-rendered/unscrapeable (see fetchers/ufc.py), so
    # fm_rank is essentially NEVER resolved — meaning every prediction was
    # silently landing in the "minimal" bucket and getting the escalated
    # 68%/60% BET/LEAN thresholds instead of the intended 65%/58%, no
    # matter how much real Sherdog data was actually available. Now keyed
    # off Sherdog's own fight_history_count (a real signal we control and
    # that's actually populated), with fm_rank as a bonus, not a gate.
    MIN_FIGHTS_FOR_FULL_CONFIDENCE = 5
    a_hist = enriched["a"].get("fight_history_count") or 0
    b_hist = enriched["b"].get("fight_history_count") or 0
    a_complete = "full" if a_hist >= MIN_FIGHTS_FOR_FULL_CONFIDENCE else "minimal"
    b_complete = "full" if b_hist >= MIN_FIGHTS_FOR_FULL_CONFIDENCE else "minimal"
    data_completeness = {
        "a": a_complete, "b": b_complete,
        "either_partial": a_complete != "full" or b_complete != "full",
    }
    steps.append({"step": "completeness", "status": "ok", **data_completeness})

    # ── Glicko-2 rating ──────────────────────────────────────────────────────
    glicko = Glicko2Model()
    glicko_prob_a, glicko_prob_b = glicko.win_probability(fighter_a, fighter_b, "ufc", weight_class)
    rating_a = glicko.get_rating(fighter_a, "ufc", weight_class)
    rating_b = glicko.get_rating(fighter_b, "ufc", weight_class)
    steps.append({
        "step": "glicko", "status": "ok",
        "rating_a": round(rating_a["rating"]), "rd_a": round(rating_a["rd"]),
        "rating_b": round(rating_b["rating"]), "rd_b": round(rating_b["rd"]),
    })

    # ── Blend with stats-based logistic ─────────────────────────────────────
    blend = composite_win_prob(
        enriched["a"], enriched["b"], glicko_prob_a, rating_a["rd"], rating_b["rd"],
    )
    prob_a, prob_b = blend["prob_a"], blend["prob_b"]

    # Provisional safety cap (models/ufc_model.MODEL_PROB_CAP)
    if prob_a > MODEL_PROB_CAP:
        prob_b += (prob_a - MODEL_PROB_CAP)
        prob_a = MODEL_PROB_CAP
    elif prob_b > MODEL_PROB_CAP:
        prob_a += (prob_b - MODEL_PROB_CAP)
        prob_b = MODEL_PROB_CAP

    steps.append({
        "step": "win_prob_blend", "status": "ok",
        "glicko_weight": round(blend["glicko_weight"], 3),
        "stats_weight": round(blend["stats_weight"], 3),
        "avg_rd": round(blend["avg_rd"], 1),
    })

    # ── Method of victory ────────────────────────────────────────────────────
    method_a = method_of_victory(enriched["a"], enriched["b"])
    method_b = method_of_victory(enriched["b"], enriched["a"])

    p_a_ko  = prob_a * method_a["p_ko"]
    p_a_sub = prob_a * method_a["p_sub"]
    p_a_dec = prob_a * method_a["p_dec"]
    p_b_ko  = prob_b * method_b["p_ko"]
    p_b_sub = prob_b * method_b["p_sub"]
    p_b_dec = prob_b * method_b["p_dec"]
    p_finish = p_a_ko + p_a_sub + p_b_ko + p_b_sub
    p_distance = p_a_dec + p_b_dec

    # ── Market comparison ────────────────────────────────────────────────────
    edge_summary = None
    if has_odds:
        edge_summary = market_edge_summary(
            model_prob_a=prob_a, model_prob_b=prob_b,
            decimal_a=odds_a, decimal_b=odds_b,
        )
        steps.append({
            "step": "market_compare", "status": "ok",
            "edge_a_pp": round(edge_summary["edge_a"] * 100, 2),
            "edge_b_pp": round(edge_summary["edge_b"] * 100, 2),
            "vig": edge_summary["vig"],
        })

    recs = _bet_recommendations(prob_a, prob_b, fighter_a, fighter_b,
                                 edge_summary, partial_data=data_completeness["either_partial"])
    kelly = None
    if has_odds:
        first = recs[0]
        if first.get("verdict") in ("BET", "LEAN"):
            if first.get("bet", "").startswith(fighter_a):
                kelly = kelly_stake(prob_a, odds_a, bankroll)
            else:
                kelly = kelly_stake(prob_b, odds_b, bankroll)

    # ── Persist ──────────────────────────────────────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("ufc", weight_class if weight_class != "all" else "UFC",
                 fighter_a, fighter_b, f"{match_date}T00:00:00", "", 1),
            )
            match_id = cur.lastrowid

        # Sherdog-derived record/finish-rate/bio fields + FightMatrix
        # ranking (switched 2026-07-15, third time same day — Wikipedia
        # fallback dropped per direct user request, Sherdog is now the sole
        # fight-history source, see fetchers/ufc.py docstring). reach_in/
        # age/height_in ARE available here when Sherdog's bio parse
        # succeeds, but still fall back gracefully to None when it doesn't.
        sigs_to_log = [
            ("fm_rank", fighter_a, enriched["a"].get("fm_rank"), "fightmatrix"),
            ("win_ko_rate", fighter_a, enriched["a"].get("win_ko_rate"), "sherdog"),
            ("win_sub_rate", fighter_a, enriched["a"].get("win_sub_rate"), "sherdog"),
            ("fight_history_count", fighter_a, enriched["a"].get("fight_history_count"), "sherdog"),
            ("reach_in", fighter_a, enriched["a"].get("reach_in"), "sherdog"),
            ("age", fighter_a, enriched["a"].get("age"), "sherdog"),
            ("fm_rank", fighter_b, enriched["b"].get("fm_rank"), "fightmatrix"),
            ("win_ko_rate", fighter_b, enriched["b"].get("win_ko_rate"), "sherdog"),
            ("win_sub_rate", fighter_b, enriched["b"].get("win_sub_rate"), "sherdog"),
            ("fight_history_count", fighter_b, enriched["b"].get("fight_history_count"), "sherdog"),
            ("reach_in", fighter_b, enriched["b"].get("reach_in"), "sherdog"),
            ("age", fighter_b, enriched["b"].get("age"), "sherdog"),
        ]
        for name, participant, val, source in sigs_to_log:
            v = _safe_float(val)
            if v is not None:
                log_signal(match_id, name, participant, signal_value=v, source=source)

        top_rec = (recs or [{}])[0]
        if top_rec.get("verdict"):
            log_signal(match_id, "verdict", None, signal_text=top_rec["verdict"], source="ufc_v1")
        log_signal(match_id, "data_completeness_a", None, signal_text=data_completeness["a"], source="ufc_v1")
        log_signal(match_id, "data_completeness_b", None, signal_text=data_completeness["b"], source="ufc_v1")

        explanation = explain(fighter_a, fighter_b, blend, method_a, method_b)

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (match_id, "ufc_v1_glicko_stats", prob_a, prob_b, 0.0,
                 explanation, datetime.now(timezone.utc).isoformat()),
            )
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc), "trace": traceback.format_exc()})
        explanation = "Persist failure — model output below."

    confidence = "high" if (a_complete == "full" and b_complete == "full") else "low"
    try:
        from ai_agent_ufc import generate_ufc_narrative
        narrative = generate_ufc_narrative(
            fighter_a, fighter_b, prob_a, prob_b, explanation,
            verdict=recs[0].get("verdict", "") if recs else "", confidence=confidence,
        )
    except Exception:
        narrative = explanation

    return {
        "match_id": match_id, "sport": "ufc", "weight_class": weight_class,
        "fighter_a": fighter_a, "fighter_b": fighter_b, "date": match_date,
        "narrative": narrative,
        "prob_a": round(prob_a * 100, 1), "prob_b": round(prob_b * 100, 1),
        "data_confidence": confidence, "data_completeness": data_completeness,
        "data_sources": ["sherdog", "fightmatrix", "glicko2"],
        "model_explanation": explanation,
        "bet_recommendations": recs, "kelly": kelly,
        "market_comparison": edge_summary,
        "markets": {
            "method_of_victory": {
                f"{fighter_a}_ko": round(p_a_ko * 100, 1),
                f"{fighter_a}_sub": round(p_a_sub * 100, 1),
                f"{fighter_a}_dec": round(p_a_dec * 100, 1),
                f"{fighter_b}_ko": round(p_b_ko * 100, 1),
                f"{fighter_b}_sub": round(p_b_sub * 100, 1),
                f"{fighter_b}_dec": round(p_b_dec * 100, 1),
            },
            "fight_finishes_pct": round(p_finish * 100, 1),
            "goes_the_distance_pct": round(p_distance * 100, 1),
        },
        "ai_signals": {"a": enriched["a"], "b": enriched["b"]},
        "notes": notes,
        "steps": steps,
    }
