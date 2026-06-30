"""
End-to-end e-sports analysis pipeline.
query → PandaScore/Claude → Elo blend → form → H2H adjustment
      → market comparison → Kelly → narrative → result dict
"""
from __future__ import annotations
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

import env_loader  # noqa: F401

from db.database import get_db, init_db
from fetchers.signals import log_signal
from models.kelly import kelly_stake, american_to_decimal, market_edge_summary


def _elo_from_ranking(ranking: int) -> float:
    """Convert world ranking to an Elo-like rating. Rank 1 ≈ 2200, floors at 1300."""
    if not ranking or ranking <= 0:
        return 1500.0
    return max(1300.0, 2200.0 - 400.0 * math.log10(max(1, ranking)))


def _elo_prob(elo_a: float, elo_b: float) -> float:
    """Standard Elo win probability for team A."""
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def _h2h_adjustment(prob: float, wins_a: int, wins_b: int) -> float:
    """Nudge probability by head-to-head record (max ±3pp, needs ≥3 encounters)."""
    total = wins_a + wins_b
    if total < 3:
        return prob
    h2h_rate = wins_a / total
    nudge = (h2h_rate - 0.5) * 0.06   # max ±3pp
    return max(0.05, min(0.95, prob + nudge))


def _build_plain_summary_esports(
    team_a: str, team_b: str,
    prob_a: float, prob_b: float,
    game: str, fmt: str,
) -> str:
    """
    Plain-English e-sports bet recommendation in the user's preferred phrasing.

    Format:
      "<favourite> is gonna win vs <opponent> (XX% accuracy on <game>).
       Bet on: 2-0 map score, handicap -1.5 maps."
    """
    if prob_a >= prob_b:
        winner, loser, win_prob = team_a, team_b, prob_a
    else:
        winner, loser, win_prob = team_b, team_a, prob_b

    bets: list[str] = []
    fmt_upper = (fmt or "Bo3").upper()
    if win_prob >= 0.70:
        if "5" in fmt_upper:
            bets.append(f"{winner} 3-0 map sweep")
        else:
            bets.append(f"{winner} 2-0 map sweep")
        bets.append(f"{winner} -1.5 maps handicap")
    elif win_prob >= 0.58:
        bets.append(f"{winner} first map")

    accuracy = win_prob * 100
    game_label = (game or "").upper() or "esports"
    if accuracy >= 60:
        headline = f"{winner} is gonna win vs {loser} ({accuracy:.0f}% accuracy on {game_label})."
    elif accuracy >= 52:
        headline = f"{winner} slight favourite vs {loser} ({accuracy:.0f}% on {game_label} — tight match)."
    else:
        headline = f"{team_a} vs {team_b}: coin-flip ({accuracy:.0f}% lean, no clear winner)."

    if bets:
        return headline + " Bet on: " + ", ".join(bets) + "."
    return headline + " No clear secondary market — moneyline only."


def run_esports_analysis(
    user_query: str,
    bankroll: float = 1000.0,
    odds_a_american: Optional[float] = None,
    odds_b_american: Optional[float] = None,
) -> dict:
    steps = []
    init_db()

    # ── 1. Parse query ──────────────────────────────────────────────────────
    try:
        from ai_agent_esports import parse_esports_query
        parsed = parse_esports_query(user_query)
        steps.append({"step": "parse_query", "status": "ok", "data": parsed})
    except Exception as exc:
        return {"error": f"Could not parse query: {exc}", "steps": steps}

    team_a_raw = parsed.get("team_a", "Team A")
    team_b_raw = parsed.get("team_b", "Team B")
    game        = parsed.get("game", "cs2")
    fmt         = parsed.get("format", "Bo3")
    game_date   = parsed.get("date") or datetime.now(timezone.utc).date().isoformat()
    notes       = parsed.get("notes", "")

    # ── 2. Fetch context ────────────────────────────────────────────────────
    try:
        from fetchers.esports import fetch_esports_context
        context = fetch_esports_context(team_a_raw, team_b_raw, game)
        steps.append({"step": "fetch_context", "status": "ok", "data": {"sources": context.get("sources", [])}})
    except Exception as exc:
        steps.append({"step": "fetch_context", "status": "error", "data": str(exc)})
        context = {
            "team_a": team_a_raw, "team_b": team_b_raw, "game": game,
            "team_a_data": {"name": team_a_raw, "ranking": 999, "form": 0.5, "region": "Unknown"},
            "team_b_data": {"name": team_b_raw, "ranking": 999, "form": 0.5, "region": "Unknown"},
            "h2h": {"wins_a": 0, "wins_b": 0, "last_encounters": []},
            "sources": [],
        }

    team_a = context["team_a"]
    team_b = context["team_b"]
    ta     = context["team_a_data"]
    tb     = context["team_b_data"]
    h2h    = context.get("h2h", {"wins_a": 0, "wins_b": 0, "last_encounters": []})

    # ── 3. Elo model ────────────────────────────────────────────────────────
    rank_a = ta.get("ranking", 999)
    rank_b = tb.get("ranking", 999)
    elo_a  = _elo_from_ranking(rank_a)
    elo_b  = _elo_from_ranking(rank_b)
    prob_elo_a = _elo_prob(elo_a, elo_b)

    # ── 4. Form blend (60% Elo + 40% form) ─────────────────────────────────
    form_a = ta.get("form", 0.5)
    form_b = tb.get("form", 0.5)
    form_total = form_a + form_b
    prob_form_a = form_a / form_total if form_total > 0 else 0.5

    prob_blend = 0.60 * prob_elo_a + 0.40 * prob_form_a
    prob_blend = max(0.05, min(0.95, prob_blend))

    # ── 5. H2H adjustment ──────────────────────────────────────────────────
    prob_a = _h2h_adjustment(prob_blend, h2h.get("wins_a", 0), h2h.get("wins_b", 0))
    prob_b = 1.0 - prob_a

    steps.append({
        "step": "model",
        "status": "ok",
        "data": {
            "elo_a": round(elo_a, 1), "elo_b": round(elo_b, 1),
            "prob_elo_a": round(prob_elo_a, 4),
            "form_a": round(form_a, 4), "form_b": round(form_b, 4),
            "prob_blend": round(prob_blend, 4),
            "prob_a_final": round(prob_a, 4),
        },
    })

    # ── 6. Data confidence ──────────────────────────────────────────────────
    sources = context.get("sources", [])
    if "pandascore" in sources and rank_a < 500 and rank_b < 500:
        data_confidence = "High"
    elif "pandascore" in sources or ("ai_estimate" in sources and rank_a < 999):
        data_confidence = "Medium"
    else:
        data_confidence = "Low"

    # ── 7. Market comparison ────────────────────────────────────────────────
    market_comparison: dict = {}
    kelly: dict = {}

    if odds_a_american is not None and odds_b_american is not None:
        try:
            odds_a_dec = american_to_decimal(odds_a_american)
            odds_b_dec = american_to_decimal(odds_b_american)
            mc = market_edge_summary(prob_a, prob_b, odds_a_dec, odds_b_dec, bankroll=bankroll)
            market_comparison = mc
            kelly = {"note": mc.get("reason", ""), "edge": mc.get("edge_pct", 0.0)}
            steps.append({"step": "market_comparison", "status": "ok", "data": mc})
        except Exception as exc:
            steps.append({"step": "market_comparison", "status": "warn", "data": str(exc)})
    else:
        steps.append({"step": "market_comparison", "status": "ok", "data": "no odds provided"})

    # ── 8. Kelly stake ──────────────────────────────────────────────────────
    kelly_result: dict = {}
    if market_comparison:
        try:
            side_prob = prob_a if market_comparison.get("recommended_side") == team_a else prob_b
            side_odds = american_to_decimal(odds_a_american) if market_comparison.get("recommended_side") == team_a else american_to_decimal(odds_b_american)
            kelly_result = kelly_stake(side_prob, side_odds, bankroll)
        except Exception:
            pass

    # ── 9. Persist to DB ────────────────────────────────────────────────────
    match_id = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO matches (sport, league, participant_a, participant_b,
                       scheduled_at, venue, neutral_site)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("esports", game.upper(), team_a, team_b, game_date, fmt, 1),
            )
            match_id = cur.lastrowid
            conn.execute(
                "INSERT INTO predictions (match_id, prob_a, prob_b, method) VALUES (?, ?, ?, ?)",
                (match_id, round(prob_a, 4), round(prob_b, 4), "esports_elo_form_v1"),
            )
        for sig_name, sig_val, sig_text in [
            ("elo_a",          elo_a,     None),
            ("elo_b",          elo_b,     None),
            ("form_a",         form_a,    None),
            ("form_b",         form_b,    None),
            ("data_confidence", None,     data_confidence),
        ]:
            log_signal(match_id, sig_name, None, sig_val, sig_text, "esports_pipeline")
        steps.append({"step": "persist_db", "status": "ok", "data": {"match_id": match_id}})
    except Exception as exc:
        steps.append({"step": "persist_db", "status": "warn", "data": str(exc)})

    # ── 10. Recommendation ─────────────────────────────────────────────────
    mc_label = (market_comparison.get("label") or "").upper()
    if mc_label in ("VALUE", "SLIGHT EDGE"):
        recommendation        = market_comparison.get("recommended_side", team_a if prob_a > prob_b else team_b)
        recommendation_reason = market_comparison.get("reason", "")
    elif mc_label in ("AVOID",):
        recommendation        = "PASS"
        recommendation_reason = market_comparison.get("reason", "No value — model edge below threshold")
    elif mc_label == "PASS":
        recommendation        = "PASS"
        recommendation_reason = market_comparison.get("reason", "Model edge insufficient vs market")
    else:
        # No odds — lean on model
        if prob_a > prob_b:
            recommendation        = team_a
            recommendation_reason = f"{team_a} model edge ({prob_a*100:.1f}%) — add odds for value check"
        else:
            recommendation        = team_b
            recommendation_reason = f"{team_b} model edge ({prob_b*100:.1f}%) — add odds for value check"

    # ── 11. Narrative ───────────────────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent_esports import generate_esports_narrative
        narrative = generate_esports_narrative(
            team_a, team_b, game,
            prob_a * 100, prob_b * 100,
            ta, tb, recommendation, data_confidence, notes,
        )
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        steps.append({"step": "narrative", "status": "warn", "data": str(exc)})
        narrative = f"{team_a} vs {team_b} — {team_a} {prob_a*100:.1f}% / {team_b} {prob_b*100:.1f}%. Recommendation: {recommendation}."

    plain_summary = _build_plain_summary_esports(team_a, team_b, prob_a, prob_b, game, fmt)

    return {
        "match_id":             match_id,
        "team_a":               team_a,
        "team_b":               team_b,
        "game":                 game.upper(),
        "format":               fmt,
        "date":                 game_date,
        "recommendation":       recommendation,
        "recommendation_reason": recommendation_reason,
        "plain_summary":        plain_summary,
        "prob_a":               round(prob_a * 100, 1),
        "prob_b":               round(prob_b * 100, 1),
        "team_stats": {
            "team_a": {
                "name":    team_a,
                "ranking": rank_a,
                "elo":     round(elo_a, 1),
                "form":    round(form_a * 100, 1),
                "region":  ta.get("region", "Unknown"),
            },
            "team_b": {
                "name":    team_b,
                "ranking": rank_b,
                "elo":     round(elo_b, 1),
                "form":    round(form_b * 100, 1),
                "region":  tb.get("region", "Unknown"),
            },
        },
        "h2h":               h2h,
        "market_comparison": market_comparison,
        "kelly":             kelly_result,
        "kelly_note":        kelly.get("note", ""),
        "kelly_edge":        round(kelly.get("edge", 0.0) * 100, 2) if isinstance(kelly.get("edge"), float) else 0.0,
        "narrative":         narrative,
        "data_confidence":   data_confidence,
        "raw_sources":       sources,
        "steps":             steps,
    }
