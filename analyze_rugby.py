"""
End-to-end NRL (Rugby League) analysis pipeline.

  query → ESPN NRL schedule aggregation (points-for/against, home/away splits, form)
        → Negative-Binomial split score model (models/rugby_model.py)
        → Elo blend
        → market comparison + Kelly stake
        → narrative
        → result dict

Mirrors analyze_soccer.py's shape. Key differences from soccer:
  - No xG-equivalent third-party source — attack/defense strengths are built
    directly from ESPN schedule results (fetchers/rugby.py).
  - No Dixon-Coles low-score correction (rugby has no soccer-style 0-0/1-0
    clustering problem) — models/rugby_model.py uses Negative Binomial instead.
  - Draws are rare (NRL golden-point extra time) but not hard-coded to zero;
    they simply fall out of the score matrix diagonal, usually near 0.
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import traceback
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db, init_db
from fetchers.signals import log_signal
from fetchers.rugby import enrich_rugby_teams
from models.rugby_model import (
    strengths_from_points, predict_score, margin_buckets, totals_over_under,
    explain, LEAGUE_AVG_POINTS, HOME_ADVANTAGE, MODEL_PROB_CAP,
)
from models.elo import EloModel
from models.kelly import kelly_stake, american_to_decimal, market_edge_summary


def _safe_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _build_strengths(team_data: dict, is_home: bool) -> dict:
    """Convert an enrich_rugby_teams() profile into NB attack/defense strengths.
    Returns the league-average prior (1.0/1.0) when the team has no live data —
    same 'don't hallucinate a specific number, just flag it as unknown' stance
    as soccer's promoted-team prior, but without a directional bias since we
    have no equivalent of "just got promoted" signal for rugby."""
    if not team_data:
        return {
            "attack": 1.0, "defense": 1.0,
            "components": {"prior_used": "no_data", "note": "No live ESPN schedule data"},
        }
    venue_pf = team_data.get("home_ppg_for") if is_home else team_data.get("away_ppg_for")
    venue_pa = team_data.get("home_ppg_against") if is_home else team_data.get("away_ppg_against")
    venue_matches = team_data.get("home_matches", 0) if is_home else team_data.get("away_matches", 0)
    return strengths_from_points(
        season_pf=team_data.get("ppg_for"),
        season_pa=team_data.get("ppg_against"),
        recent_pf=team_data.get("recent_ppg_for"),
        recent_pa=team_data.get("recent_ppg_against"),
        venue_pf=venue_pf,
        venue_pa=venue_pa,
        league_avg_points=LEAGUE_AVG_POINTS,
        matches_played=team_data.get("matches", 0),
        venue_matches=venue_matches,
        effective_n=team_data.get("effective_n"),
        is_home=is_home,
    )


def _bet_recommendations(
    prob_home: float, prob_away: float,
    home_team: str, away_team: str,
    edge_summary: Optional[dict],
    partial_data: bool = False,
) -> list[dict]:
    """
    Mirrors analyze_soccer.py's _bet_recommendations, but with baseball-style
    conservative thresholds (65%/62pp) since this is a brand-new, uncalibrated
    model with zero resolved predictions — CLAUDE.md's own history here
    (Bug 3: "model confidence was too high / thresholds too low") argues for
    starting cautious rather than starting loose and tightening after losses.
    """
    decisive = prob_home + prob_away
    p_home = prob_home / decisive if decisive else 0.5
    p_away = prob_away / decisive if decisive else 0.5
    leader_prob = max(p_home, p_away)
    leader_team = home_team if p_home >= p_away else away_team

    edge_bet   = 0.05 if partial_data else 0.035
    edge_lean  = 0.02 if partial_data else 0.01
    no_odds_bet  = 0.68 if partial_data else 0.65
    no_odds_lean = 0.60 if partial_data else 0.58

    recs: list[dict] = []
    if edge_summary and edge_summary.get("has_real_odds"):
        edge_h = edge_summary["edge_a"]
        edge_a = edge_summary["edge_b"]
        best_edge = max(edge_h, edge_a)
        best_side = home_team if edge_h >= edge_a else away_team
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
            reasons = [f"{leader_team} {leader_prob*100:.1f}% conditional-on-decisive"]
            skip = ""
        elif leader_prob >= no_odds_lean:
            verdict, confidence = "LEAN", "low"
            reasons = [f"{leader_team} marginal favourite, {leader_prob*100:.1f}% conditional"]
            skip = ""
        else:
            verdict, confidence = "PASS", None
            reasons = []
            skip = f"Coin-flip range — {leader_team} only {leader_prob*100:.1f}% (need {no_odds_bet*100:.0f}%)"
        recs.append({
            "market": "Win (no market odds)", "verdict": verdict,
            "bet": f"{leader_team} moneyline" if verdict in ("BET", "LEAN") else "",
            "model_prob_pct": round(leader_prob * 100, 1),
            "threshold_pct": no_odds_bet * 100, "partial_data": partial_data,
            "confidence": confidence, "reasons": reasons, "skip_reason": skip,
        })
    return recs


def run_rugby_analysis(user_query: str, bankroll: float = 1000.0) -> dict:
    """
    Full NRL pipeline.
      1. Parse query → home/away/date/odds/lines via Claude
      2. Enrich via ESPN schedule aggregation (points-for/against, splits, form)
      3. Build attack/defense strengths (shrunk, venue + recency blended)
      4. Run Negative-Binomial split score model
      5. Blend with Elo (dynamic weight by rating delta)
      6. Compute markets (moneyline, margin buckets, totals) + bet recs
      7. Persist match + signals + prediction
      8. Narrate
    """
    init_db()
    steps: list[dict] = []

    try:
        from ai_agent_rugby import parse_rugby_query
        parsed = parse_rugby_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    home_team = parsed.get("home_team") or "Home"
    away_team = parsed.get("away_team") or "Away"
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

    odds_home = _as_decimal(parsed.get("odds_home_american"), parsed.get("odds_home_decimal"))
    odds_away = _as_decimal(parsed.get("odds_away_american"), parsed.get("odds_away_decimal"))
    has_odds = odds_home is not None and odds_away is not None
    handicap_line = _safe_float(parsed.get("handicap_line"))
    total_line = _safe_float(parsed.get("total_line"))

    # ── ESPN enrichment ──────────────────────────────────────────────────────
    # before_date=match_date excludes that day's games from both teams' stats
    # (fixed 2026-07-12 — an already-finished same-day game was otherwise
    # leaking its own result into the "prediction" of itself).
    try:
        enriched = enrich_rugby_teams(home_team, away_team, before_date=match_date)
        steps.append({
            "step": "espn_enrich",
            "status": "ok" if (enriched["home"] and enriched["away"]) else "partial",
            "home_fields": len(enriched["home"]), "away_fields": len(enriched["away"]),
        })
    except Exception as exc:
        enriched = {"home": {}, "away": {}}
        steps.append({"step": "espn_enrich", "status": "error", "error": str(exc)})

    if not enriched["home"] and not enriched["away"]:
        steps.append({
            "step": "insufficient_data", "status": "halt",
            "reason": "No live ESPN schedule data reachable for either team.",
        })
        return {
            "status": "insufficient_data", "sport": "rugby",
            "home_team": home_team, "away_team": away_team, "date": match_date,
            "recommendation": "PASS",
            "narrative": (
                f"No data available for {home_team} vs {away_team}. "
                "Recommended action: PASS. We do not generate predictions from "
                "training-data hallucinations because they cannot be verified "
                "against current form or team news."
            ),
            "data_confidence": "none", "steps": steps,
        }

    home_complete = "full" if enriched["home"] else "minimal"
    away_complete = "full" if enriched["away"] else "minimal"
    data_completeness = {
        "home": home_complete, "away": away_complete,
        "either_partial": home_complete != "full" or away_complete != "full",
    }
    steps.append({"step": "completeness", "status": "ok", **data_completeness})

    # ── Strengths ────────────────────────────────────────────────────────────
    str_h = _build_strengths(enriched["home"], is_home=True)
    str_a = _build_strengths(enriched["away"], is_home=False)
    steps.append({
        "step": "strengths", "status": "ok",
        "home_attack": round(str_h["attack"], 3), "home_defense": round(str_h["defense"], 3),
        "away_attack": round(str_a["attack"], 3), "away_defense": round(str_a["defense"], 3),
        "league_avg_points": LEAGUE_AVG_POINTS,
    })

    # ── Score model ──────────────────────────────────────────────────────────
    score = predict_score(
        home_attack=str_h["attack"], home_defense=str_h["defense"],
        away_attack=str_a["attack"], away_defense=str_a["defense"],
        league_avg_points=LEAGUE_AVG_POINTS, home_advantage=HOME_ADVANTAGE,
    )

    # ── Elo blend (dynamic weight by rating delta, mirrors analyze_soccer.py) ─
    elo = EloModel()
    ra_raw = elo.get_rating(home_team)
    rb_raw = elo.get_rating(away_team)
    elo_home, elo_away = elo.win_probability(home_team, away_team)
    rating_delta = abs(ra_raw - rb_raw)
    elo_weight = 0.35 * min(1.0, rating_delta / 50.0)
    nb_weight = 1.0 - elo_weight

    p_decisive = 1.0 - score["prob_draw"]
    nb_home_ratio = (score["prob_home"] / (score["prob_home"] + score["prob_away"])
                     if (score["prob_home"] + score["prob_away"]) > 0 else 0.5)
    blended_ratio = nb_weight * nb_home_ratio + elo_weight * elo_home
    prob_home = p_decisive * blended_ratio
    prob_away = 1.0 - prob_home - score["prob_draw"]
    prob_draw = score["prob_draw"]

    # Provisional safety cap (models/rugby_model.MODEL_PROB_CAP) — same
    # "no model should claim near-certainty" stance as baseball's 72% cap.
    if prob_home > MODEL_PROB_CAP:
        overflow = prob_home - MODEL_PROB_CAP
        prob_home = MODEL_PROB_CAP
        prob_away += overflow
    elif prob_away > MODEL_PROB_CAP:
        overflow = prob_away - MODEL_PROB_CAP
        prob_away = MODEL_PROB_CAP
        prob_home += overflow

    steps.append({
        "step": "elo_blend", "status": "ok",
        "elo_home": round(elo_home, 3), "elo_away": round(elo_away, 3),
        "rating_delta": rating_delta, "elo_weight": round(elo_weight, 4),
        "nb_weight": round(nb_weight, 4),
        "blend_weights": f"{nb_weight*100:.0f}% NB / {elo_weight*100:.0f}% Elo (Δrating={rating_delta:.0f})",
    })

    # ── Markets ──────────────────────────────────────────────────────────────
    matrix = score["score_matrix"]
    margins = margin_buckets(matrix)
    default_total_line = total_line if total_line is not None else round((score["mu_home"] + score["mu_away"]) * 2) / 2
    totals = totals_over_under(matrix, default_total_line)

    edge_summary = None
    if has_odds:
        edge_summary = market_edge_summary(
            model_prob_a=prob_home, model_prob_b=prob_away,
            decimal_a=odds_home, decimal_b=odds_away,
        )
        steps.append({
            "step": "market_compare", "status": "ok",
            "edge_home_pp": round(edge_summary["edge_a"] * 100, 2),
            "edge_away_pp": round(edge_summary["edge_b"] * 100, 2),
            "vig": edge_summary["vig"],
        })

    recs = _bet_recommendations(prob_home, prob_away, home_team, away_team,
                                 edge_summary, partial_data=data_completeness["either_partial"])
    kelly = None
    if has_odds:
        first = recs[0]
        if first.get("verdict") in ("BET", "LEAN"):
            if first.get("bet", "").startswith(home_team):
                kelly = kelly_stake(prob_home, odds_home, bankroll)
            else:
                kelly = kelly_stake(prob_away, odds_away, bankroll)

    handicap_cover = None
    if handicap_line is not None:
        n = matrix.shape[0]
        p_cover = 0.0
        for i in range(n):
            for j in range(n):
                if (i - j) + handicap_line > 0:
                    p_cover += matrix[i, j]
        handicap_cover = {"line": handicap_line, "prob_home_covers": p_cover, "prob_away_covers": 1.0 - p_cover}

    # ── Persist ──────────────────────────────────────────────────────────────
    match_id: Optional[int] = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                       (sport, league, participant_a, participant_b,
                        scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("rugby", "NRL", home_team, away_team, f"{match_date}T00:00:00", "", 0),
            )
            match_id = cur.lastrowid

        sigs_to_log = [
            ("ppg_for", home_team, enriched["home"].get("ppg_for")),
            ("ppg_against", home_team, enriched["home"].get("ppg_against")),
            ("recent_ppg_for", home_team, enriched["home"].get("recent_ppg_for")),
            ("recent_ppg_against", home_team, enriched["home"].get("recent_ppg_against")),
            ("ppg_for", away_team, enriched["away"].get("ppg_for")),
            ("ppg_against", away_team, enriched["away"].get("ppg_against")),
            ("recent_ppg_for", away_team, enriched["away"].get("recent_ppg_for")),
            ("recent_ppg_against", away_team, enriched["away"].get("recent_ppg_against")),
        ]
        for name, team, val in sigs_to_log:
            v = _safe_float(val)
            if v is not None:
                log_signal(match_id, name, team, signal_value=v, source="espn_nrl")

        top_rec = (recs or [{}])[0]
        if top_rec.get("verdict"):
            log_signal(match_id, "verdict", None, signal_text=top_rec["verdict"], source="rugby_v1")
        log_signal(match_id, "data_completeness_home", None, signal_text=data_completeness["home"], source="rugby_v1")
        log_signal(match_id, "data_completeness_away", None, signal_text=data_completeness["away"], source="rugby_v1")

        explanation = explain(score, home_team, away_team) + (
            f" Elo win-prob: {home_team} {elo_home*100:.1f}% / {away_team} {elo_away*100:.1f}%."
        )

        with get_db() as conn:
            conn.execute(
                """INSERT INTO predictions
                       (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (match_id, "rugby_v1_nb_elo", prob_home, prob_away, prob_draw,
                 explanation, datetime.now(timezone.utc).isoformat()),
            )
    except Exception as exc:
        steps.append({"step": "persist", "status": "error", "error": str(exc), "trace": traceback.format_exc()})
        explanation = "Persist failure — model output below."

    confidence = "high" if (home_complete == "full" and away_complete == "full") else "low"
    try:
        from ai_agent_rugby import generate_rugby_narrative
        narrative = generate_rugby_narrative(
            home_team, away_team, prob_home, prob_draw, prob_away, explanation,
            verdict=recs[0].get("verdict", "") if recs else "", confidence=confidence,
        )
    except Exception:
        narrative = explanation

    return {
        "match_id": match_id, "sport": "rugby", "league": "NRL",
        "home_team": home_team, "away_team": away_team, "date": match_date,
        "narrative": narrative,
        "prob_home": round(prob_home * 100, 1),
        "prob_draw": round(prob_draw * 100, 1),
        "prob_away": round(prob_away * 100, 1),
        "mu_home": round(score["mu_home"], 1), "mu_away": round(score["mu_away"], 1),
        "data_confidence": confidence, "data_completeness": data_completeness,
        "data_sources": ["espn_nrl", "elo" if elo_weight > 0 else None],
        "model_explanation": explanation,
        "bet_recommendations": recs, "kelly": kelly,
        "market_comparison": edge_summary,
        "markets": {
            "margin_buckets": {k: round(v * 100, 1) for k, v in margins.items()},
            "totals": {"line": totals["line"], "prob_over": round(totals["prob_over"] * 100, 1),
                       "prob_under": round(totals["prob_under"] * 100, 1)},
            "handicap": handicap_cover,
        },
        "ai_signals": {"home": enriched["home"], "away": enriched["away"]},
        "notes": notes,
        "steps": steps,
    }
