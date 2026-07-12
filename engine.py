"""
Prediction engine: orchestrates Elo/Glicko/Dixon-Coles + signal data
into a single calibrated probability with a plain-language explanation.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db
from fetchers.signals import fetch_signals_for_match
from models.elo import EloModel
from models.glicko import Glicko2Model
from models.markets import compute_all_markets
from models.dixon_coles import predict as dc_predict, strengths_from_signals, explain as dc_explain
from models.devig import devig_market, american_to_decimal
from models.kelly import kelly_stake, explain_kelly
from models.calibration import compute_metrics_from_db


elo_model = EloModel()
glicko_model = Glicko2Model()


def _market_consensus(match_id: int) -> Optional[dict]:
    """Return average vig-free probabilities across all stored odds snapshots."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT price_a, price_b, price_draw FROM odds_snapshots
               WHERE match_id=? AND price_a IS NOT NULL AND price_b IS NOT NULL""",
            (match_id,),
        ).fetchall()
    if not rows:
        return None
    fairs = []
    for r in rows:
        prices = {"a": r["price_a"], "b": r["price_b"]}
        if r["price_draw"]:
            prices["draw"] = r["price_draw"]
        fairs.append(devig_market(prices))
    avg = {k: sum(f.get(k, 0) for f in fairs) / len(fairs) for k in fairs[0]}
    return {"fair_probs": avg, "n_books": len(rows)}


def _build_explanation(
    sport: str,
    participant_a: str,
    participant_b: str,
    prob_a: float,
    signals_a: dict,
    signals_b: dict,
    market: Optional[dict],
    rating_detail: str,
) -> str:
    form_a = signals_a.get("form_weighted10", "n/a")
    form_b = signals_b.get("form_weighted10", "n/a")
    injury_a = signals_a.get("key_player_out_flag", 0)
    injury_note_a = signals_a.get("injury_note", "")
    injury_b = signals_b.get("key_player_out_flag", 0)
    injury_note_b = signals_b.get("injury_note", "")

    injury_str_a = f"injury concern ({injury_note_a})" if injury_a else "no injury concerns"
    injury_str_b = f"injury concern ({injury_note_b})" if injury_b else "no injury concerns"

    form_str = ""
    if form_a != "n/a" and form_b != "n/a":
        try:
            form_str = f", form {float(form_a):.2f} vs {float(form_b):.2f}"
        except (ValueError, TypeError):
            pass

    market_str = ""
    if market:
        mp = market["fair_probs"].get("a", 0)
        agreement = "in agreement" if abs(prob_a - mp) < 0.05 else "diverging from model"
        market_str = (
            f" Market consensus across {market['n_books']} snapshot(s): "
            f"{mp*100:.1f}%, {agreement}."
        )

    return (
        f"{participant_a} favored at {prob_a*100:.1f}% based on {rating_detail}"
        f"{form_str}. "
        f"{participant_a}: {injury_str_a}; {participant_b}: {injury_str_b}."
        f"{market_str}"
    )


def predict_match(
    match_id: int,
    method: str = "model_v1",
    bankroll: float = 1000.0,
    surface: str = "all",
) -> dict:
    """
    Run the full prediction pipeline for a match.
    Persists a prediction record and returns the full result dict.
    """
    with get_db() as conn:
        match = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not match:
        return {"error": f"Match {match_id} not found."}

    sport = match["sport"]
    part_a = match["participant_a"]
    part_b = match["participant_b"]
    neutral = bool(match["neutral_site"])

    signals = fetch_signals_for_match(match_id)
    sig_a = signals.get(part_a, signals.get("_match", {}))
    sig_b = signals.get(part_b, {})

    prob_a = prob_b = prob_draw = None
    rating_detail = ""

    markets: dict = {}
    if sport == "soccer":
        # Elo-based win prob
        elo_pa, elo_pb = elo_model.win_probability(part_a, part_b)
        str_a = strengths_from_signals(sig_a)
        str_b = strengths_from_signals(sig_b)
        dc = dc_predict(
            attack_home=str_a["attack"],
            defense_home=str_a["defense"],
            attack_away=str_b["attack"],
            defense_away=str_b["defense"],
            neutral=neutral,
        )
        # Draw probability comes entirely from DC (Elo has no draw term).
        # Elo contributes as a conditional win-probability given the match is decisive.
        # This preserves the full DC draw without the 40% Elo weight suppressing it.
        prob_draw = dc["prob_draw"]
        p_decisive = 1.0 - prob_draw
        dc_home_ratio = dc["prob_home"] / (dc["prob_home"] + dc["prob_away"])
        blended_ratio = 0.4 * elo_pa + 0.6 * dc_home_ratio
        prob_a = p_decisive * blended_ratio
        prob_b = 1.0 - prob_a - prob_draw
        ra = elo_model.get_rating(part_a)
        rb = elo_model.get_rating(part_b)
        rating_detail = (
            f"a {abs(ra-rb):.0f}-point Elo {'advantage' if ra>rb else 'deficit'} "
            f"and Dixon-Coles xG model (μ_home={dc['mu_home']:.2f}, μ_away={dc['mu_away']:.2f})"
        )
        # Compute all sportsbook markets from the score matrix
        markets = compute_all_markets(
            str_a["attack"], str_a["defense"],
            str_b["attack"], str_b["defense"],
            neutral=neutral,
            home_corners_for=float(sig_a.get("corners_for_avg5") or 5.0),
            home_corners_against=float(sig_a.get("corners_against_avg5") or 4.5),
            away_corners_for=float(sig_b.get("corners_for_avg5") or 4.5),
            away_corners_against=float(sig_b.get("corners_against_avg5") or 5.0),
        )
    elif sport in ("tennis", "table_tennis"):
        glicko_pa, glicko_pb = glicko_model.win_probability(part_a, part_b, sport, surface)
        prob_a = glicko_pa
        prob_b = glicko_pb
        a = glicko_model.get_rating(part_a, sport, surface)
        b = glicko_model.get_rating(part_b, sport, surface)
        rating_detail = (
            f"a Glicko-2 rating of {a['rating']:.0f}±{a['rd']:.0f} vs "
            f"{b['rating']:.0f}±{b['rd']:.0f}"
        )
    else:
        return {"error": f"Unknown sport '{sport}'."}

    market = _market_consensus(match_id)
    explanation = _build_explanation(
        sport, part_a, part_b, prob_a, sig_a, sig_b, market, rating_detail
    )

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO predictions (match_id, method, prob_a, prob_b, prob_draw, explanation, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (match_id, method, prob_a, prob_b, prob_draw, explanation, now),
        )
        pred_id = cur.lastrowid

    kelly = kelly_stake(prob_a, 1.0 / prob_a if prob_a > 0 else 2.0, bankroll)

    return {
        "prediction_id": pred_id,
        "match_id": match_id,
        "sport": sport,
        "participant_a": part_a,
        "participant_b": part_b,
        "prob_a": prob_a,
        "prob_b": prob_b,
        "prob_draw": prob_draw,
        "explanation": explanation,
        "kelly": kelly,
        "kelly_note": explain_kelly(kelly),
        "market": market,
        "markets": markets,
    }


def record_outcome(
    match_id: int,
    result: str,
    score_a: Optional[int] = None,
    score_b: Optional[int] = None,
    update_ratings: bool = True,
    importance: str = "default",
    surface: str = "all",
) -> dict:
    """Record a match outcome and optionally update ratings."""
    if result not in ("a", "b", "draw"):
        return {"error": "result must be 'a', 'b', or 'draw'"}

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        match = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match:
            return {"error": f"Match {match_id} not found."}
        conn.execute(
            """INSERT OR REPLACE INTO outcomes (match_id, result, score_a, score_b, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (match_id, result, score_a, score_b, now),
        )
        conn.execute("UPDATE matches SET status='final' WHERE id=?", (match_id,))

    sport = match["sport"]
    part_a = match["participant_a"]
    part_b = match["participant_b"]

    if update_ratings and score_a is not None and score_b is not None:
        if sport in ("soccer", "rugby"):
            new_ra, new_rb = elo_model.update(part_a, part_b, score_a, score_b, importance)
            return {
                "status": "recorded",
                "result": result,
                "new_rating_a": new_ra,
                "new_rating_b": new_rb,
            }
        elif sport in ("tennis", "table_tennis"):
            winner = part_a if result == "a" else part_b
            loser = part_b if result == "a" else part_a
            glicko_model.update(winner, loser, sport, surface)
            return {"status": "recorded", "result": result}

    return {"status": "recorded", "result": result}
