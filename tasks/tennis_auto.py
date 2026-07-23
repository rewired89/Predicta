"""
Tennis resolution + performance reporting.

Mirrors tasks/rugby_auto.py's shape (no background scheduler, no auto-predict
scan — just fill in real outcomes for past predictions and report accuracy
so there's real evidence instead of a guess). Exposed as manually triggered
endpoints (POST /tennis-auto/resolve, GET /tennis-performance).

Tennis has no BET/LEAN/PASS three-way verdict like soccer/rugby — analyze_tennis.py
logs a single "recommendation" signal per match (a player name, or "PASS" when
data confidence is too low to call). compute_metrics() treats any non-PASS
recommendation as the model's pick and grades it against the real winner.

Depends on a same-day bug fix in analyze_tennis.py: run_tennis_analysis() used
to reference the `recommendation` variable inside its persist step before that
variable was ever assigned (it was computed in a later step), raising
UnboundLocalError on every single call. That exception was swallowed by the
persist step's try/except, so the `matches` row was written but the
`predictions` row and every signal (including `recommendation` itself) never
were — meaning no tennis prediction has ever had a gradable row in the DB.
Fixed by moving the recommendation computation before persist. This resolver
only works for predictions logged after that fix.
"""
from __future__ import annotations
from datetime import datetime, timezone

from db.database import get_db
from engine import record_outcome
from fetchers.tennis import finished_result


def resolve_finished() -> dict:
    """
    Find tennis predictions with scheduled_at in the past and no outcome row,
    look up the real result via ESPN, and record it (updates Glicko-2 too).
    Mirrors tasks/rugby_auto.py's resolve_finished().
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.participant_a, m.participant_b, m.scheduled_at,
                      m.league, m.venue
               FROM matches m
               LEFT JOIN outcomes o ON o.match_id = m.id
               WHERE m.sport = 'tennis'
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
            tour = (row["league"] or "atp").lower()
            r = finished_result(row["participant_a"], row["participant_b"],
                                 row["scheduled_at"], tour=tour)
            if not r:
                still_pending += 1
                details.append({"match_id": row["id"], "status": "not_final_yet"})
                continue
            rc = record_outcome(
                match_id=row["id"], result=r["result"],
                score_a=r["score_a"], score_b=r["score_b"],
                update_ratings=True, importance="default",
                surface=row["venue"] or "all",
            )
            if "error" in rc:
                errors += 1
                details.append({"match_id": row["id"], "error": rc["error"]})
            else:
                resolved += 1
                details.append({
                    "match_id": row["id"],
                    "result": r["result"],
                    "score_detail": r.get("score_detail", ""),
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
    Tennis model performance — resolved predictions, Brier score, pick hit
    rate (any non-PASS recommendation vs. the real winner), calibration
    buckets, and a breakdown by data_confidence tier (high/medium/low —
    the shrinkage tier analyze_tennis.py already assigns per prediction).
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.id AS match_id, m.participant_a, m.participant_b,
                   p.prob_a, p.prob_b,
                   o.result,
                   (SELECT signal_text FROM signals WHERE match_id=m.id AND signal_name='recommendation'  LIMIT 1) AS recommendation,
                   (SELECT signal_text FROM signals WHERE match_id=m.id AND signal_name='data_confidence' LIMIT 1) AS data_confidence
            FROM matches m
            JOIN predictions p ON p.match_id = m.id
            JOIN outcomes    o ON o.match_id = m.id
            WHERE m.sport = 'tennis'
            ORDER BY m.id DESC
            """
        ).fetchall()

    if not rows:
        return {"resolved": 0}

    total_brier = 0.0
    total_resolved = 0
    picks: list[dict] = []
    conf_buckets: dict[str, dict] = {
        "high": {"n": 0, "hits": 0}, "medium": {"n": 0, "hits": 0}, "low": {"n": 0, "hits": 0},
    }
    prob_buckets: dict[str, dict] = {
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
        if actual not in ("a", "b"):
            continue
        total_resolved += 1
        p_a = float(r["prob_a"] or 0)
        p_b = float(r["prob_b"] or 0)
        y_a = 1.0 if actual == "a" else 0.0
        y_b = 1.0 if actual == "b" else 0.0
        total_brier += (p_a - y_a) ** 2 + (p_b - y_b) ** 2

        rec = (r["recommendation"] or "").strip()
        conf = (r["data_confidence"] or "").strip().lower()
        if rec and rec.upper() != "PASS":
            picked_a = rec == r["participant_a"]
            picked_b = rec == r["participant_b"]
            if not (picked_a or picked_b):
                continue  # recommendation text didn't match either participant name
            picked_side = "a" if picked_a else "b"
            model_prob = p_a if picked_a else p_b
            hit = 1 if picked_side == actual else 0
            picks.append({"match_id": r["match_id"], "pick": rec, "model_prob": model_prob, "hit": hit})

            if conf in conf_buckets:
                conf_buckets[conf]["n"] += 1
                conf_buckets[conf]["hits"] += hit

            bk = _bucket_for(model_prob)
            prob_buckets[bk]["n"] += 1
            prob_buckets[bk]["hits"] += hit

    def _pick_summary(entries: list[dict]) -> dict:
        if not entries:
            return {"n": 0}
        n = len(entries)
        hits = sum(e["hit"] for e in entries)
        return {"n": n, "hits": hits, "hit_rate": round(hits / n, 3)}

    conf_summary = {name: {"n": b["n"], "hit_rate": round(b["hits"] / b["n"], 3) if b["n"] else None}
                     for name, b in conf_buckets.items()}
    calib = {name: {"n": b["n"], "hit_rate": round(b["hits"] / b["n"], 3) if b["n"] else None}
             for name, b in prob_buckets.items()}

    return {
        "resolved": total_resolved,
        "avg_brier": round(total_brier / total_resolved, 4) if total_resolved else None,
        "picks": _pick_summary(picks),
        "by_data_confidence": conf_summary,
        "calibration": calib,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
