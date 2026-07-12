"""
Rugby (NRL) resolution + performance reporting.

Deliberately smaller than tasks/soccer_auto.py — no background scheduler
thread, no auto-predict fixture scan, no weekly markdown report pushed to
GitHub. Rugby has no automation task yet (nobody asked for one); what's
needed right now is exactly what was asked for: fill in real outcomes for
past predictions, and report BET/LEAN hit-rate + calibration so there's
real evidence before staking real money. Both are exposed as manually
triggered endpoints (POST /rugby-auto/resolve, GET /rugby-performance).
Add the scheduler-thread version later if/when rugby gets its own
auto-predict job, mirroring soccer's pattern.
"""
from __future__ import annotations
from datetime import datetime, timezone

from db.database import get_db
from engine import record_outcome
from fetchers.rugby import finished_result


def resolve_finished() -> dict:
    """
    Find rugby predictions with scheduled_at in the past and no outcome row,
    look up the real result via ESPN, and record it (updates Elo too).
    Mirrors tasks/soccer_auto.py's resolve_finished().
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.participant_a, m.participant_b, m.scheduled_at
               FROM matches m
               LEFT JOIN outcomes o ON o.match_id = m.id
               WHERE m.sport = 'rugby'
                 AND o.match_id IS NULL
                 AND m.scheduled_at < ?
               ORDER BY m.scheduled_at ASC
               LIMIT 100""",
            (now,),
        ).fetchall()

    resolved, still_pending, errors = 0, 0, 0
    details: list[dict] = []
    for row in rows:
        try:
            r = finished_result(row["participant_a"], row["participant_b"], row["scheduled_at"])
            if not r:
                still_pending += 1
                details.append({"match_id": row["id"], "status": "not_final_yet"})
                continue
            rc = record_outcome(
                match_id=row["id"], result=r["result"],
                score_a=r["score_a"], score_b=r["score_b"],
                update_ratings=True, importance="default",
            )
            if "error" in rc:
                errors += 1
                details.append({"match_id": row["id"], "error": rc["error"]})
            else:
                resolved += 1
                details.append({
                    "match_id": row["id"],
                    "score": f"{r['score_a']}-{r['score_b']}",
                    "result": r["result"],
                })
        except Exception as exc:
            errors += 1
            details.append({"match_id": row["id"], "error": str(exc)})

    return {
        "candidates": len(rows), "resolved": resolved,
        "still_pending": still_pending, "errors": errors, "details": details,
    }


def compute_metrics() -> dict:
    """
    Rugby model performance — resolved predictions, Brier score, BET/LEAN
    hit rate + implied ROI, and calibration buckets. Same structure as
    tasks/soccer_auto.py's _compute_metrics(), minus the per-league
    breakdown (rugby only has one league — NRL — right now).

    Depends on the bet_side/bet_model_prob/bet_decimal_odds/bet_edge_pp
    signals analyze_rugby.py logs per BET/LEAN prediction.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.id AS match_id,
                   p.prob_a, p.prob_draw, p.prob_b,
                   o.result,
                   (SELECT signal_text  FROM signals WHERE match_id=m.id AND signal_name='verdict'          LIMIT 1) AS verdict,
                   (SELECT signal_text  FROM signals WHERE match_id=m.id AND signal_name='bet_side'         LIMIT 1) AS bet_side,
                   (SELECT signal_value FROM signals WHERE match_id=m.id AND signal_name='bet_model_prob'   LIMIT 1) AS bet_model_prob,
                   (SELECT signal_value FROM signals WHERE match_id=m.id AND signal_name='bet_decimal_odds' LIMIT 1) AS bet_decimal_odds,
                   (SELECT signal_value FROM signals WHERE match_id=m.id AND signal_name='bet_edge_pp'      LIMIT 1) AS bet_edge_pp
            FROM matches m
            JOIN predictions p ON p.match_id = m.id
            JOIN outcomes    o ON o.match_id = m.id
            WHERE m.sport = 'rugby'
            ORDER BY m.id DESC
            """
        ).fetchall()

    if not rows:
        return {"resolved": 0}

    total_brier = 0.0
    total_resolved = 0
    bet_picks: list[dict] = []
    lean_picks: list[dict] = []
    buckets: dict[str, dict] = {
        "50-60%": {"n": 0, "hits": 0}, "60-70%": {"n": 0, "hits": 0},
        "70-80%": {"n": 0, "hits": 0}, "80%+":   {"n": 0, "hits": 0},
    }

    def _bucket_for(prob: float) -> str:
        if prob < 0.60: return "50-60%"
        if prob < 0.70: return "60-70%"
        if prob < 0.80: return "70-80%"
        return "80%+"

    for r in rows:
        actual = r["result"]
        if actual not in ("a", "b", "draw"):
            continue
        total_resolved += 1
        p_a = float(r["prob_a"] or 0)
        p_b = float(r["prob_b"] or 0)
        p_d = float(r["prob_draw"] or 0)
        y_a = 1.0 if actual == "a" else 0.0
        y_b = 1.0 if actual == "b" else 0.0
        y_d = 1.0 if actual == "draw" else 0.0
        total_brier += (p_a - y_a) ** 2 + (p_b - y_b) ** 2 + (p_d - y_d) ** 2

        verdict = (r["verdict"] or "").upper()
        bet_side = r["bet_side"]
        model_prob = float(r["bet_model_prob"]) if r["bet_model_prob"] is not None else None
        decimal_odds = float(r["bet_decimal_odds"]) if r["bet_decimal_odds"] is not None else None

        if verdict in ("BET", "LEAN") and bet_side in ("home", "away"):
            actual_side = {"a": "home", "b": "away"}.get(actual)
            hit = 1 if actual_side == bet_side else 0
            entry = {
                "match_id": r["match_id"], "bet_side": bet_side,
                "model_prob": model_prob, "decimal_odds": decimal_odds,
                "hit": hit,
            }
            (bet_picks if verdict == "BET" else lean_picks).append(entry)
            if model_prob is not None:
                bk = _bucket_for(model_prob)
                buckets[bk]["n"] += 1
                buckets[bk]["hits"] += hit

    def _pick_summary(picks: list[dict]) -> dict:
        if not picks:
            return {"n": 0}
        n = len(picks)
        hits = sum(p["hit"] for p in picks)
        odds_available = [p["decimal_odds"] for p in picks if p.get("decimal_odds")]
        avg_odds = sum(odds_available) / len(odds_available) if odds_available else None
        implied_roi = None
        if odds_available and len(odds_available) == n:
            hr = hits / n
            implied_roi = hr * (avg_odds - 1) - (1 - hr)
        return {
            "n": n, "hits": hits, "hit_rate": round(hits / n, 3),
            "avg_odds": round(avg_odds, 3) if avg_odds else None,
            "implied_roi": round(implied_roi, 3) if implied_roi is not None else None,
        }

    calib = {name: {"n": b["n"], "hit_rate": round(b["hits"] / b["n"], 3) if b["n"] else None}
             for name, b in buckets.items()}

    return {
        "resolved": total_resolved,
        "avg_brier": round(total_brier / total_resolved, 4) if total_resolved else None,
        "bet": _pick_summary(bet_picks),
        "lean": _pick_summary(lean_picks),
        "calibration": calib,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
