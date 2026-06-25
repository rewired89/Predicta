"""
Auto-resolve pending match outcomes.

Scans the DB for predictions with no recorded outcome where the match
scheduled_at time has already passed. For each, attempts to fetch the
actual result from Setka Cup / TT Cup and records it automatically.

Can be triggered:
  - Via POST /resolve-pending (on-demand from the UI)
  - Via a cron job (e.g. every hour)
  - Manually: python -m tasks.auto_resolve
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from db.database import get_db
from engine import record_outcome
from fetchers.results_collector import fetch_match_result


# Only attempt to resolve matches that finished at least this many minutes ago
# (gives the website time to post the result)
RESOLVE_DELAY_MINUTES = 30

# Don't try to resolve matches older than this — too stale, data may be gone
MAX_AGE_DAYS = 7


def _pending_matches() -> list[dict]:
    """Return matches that have a prediction but no outcome, past their scheduled time."""
    cutoff_early = (
        datetime.now(timezone.utc) - timedelta(minutes=RESOLVE_DELAY_MINUTES)
    ).isoformat()
    cutoff_old = (
        datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    ).isoformat()

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                m.id, m.sport, m.league, m.participant_a, m.participant_b,
                m.scheduled_at
            FROM matches m
            JOIN  predictions p ON p.match_id = m.id
            LEFT JOIN outcomes o ON o.match_id = m.id
            WHERE o.match_id IS NULL
              AND m.scheduled_at <= ?
              AND m.scheduled_at >= ?
            ORDER BY m.scheduled_at ASC
        """, (cutoff_early, cutoff_old)).fetchall()
    return [dict(r) for r in rows]


def run_auto_resolve(dry_run: bool = False) -> dict:
    """
    Attempt to resolve all pending matches.

    dry_run=True: fetch results but don't write to DB (useful for testing).

    Returns a summary dict:
      {
        attempted: int,
        resolved:  int,
        failed:    int,
        skipped:   int,   # sport not yet supported
        details:   list[dict]
      }
    """
    pending = _pending_matches()
    summary = {
        "attempted": len(pending),
        "resolved":  0,
        "failed":    0,
        "skipped":   0,
        "details":   [],
    }

    for match in pending:
        mid       = match["id"]
        sport     = match["sport"]
        player_a  = match["participant_a"]
        player_b  = match["participant_b"]
        sched_at  = match["scheduled_at"]
        tour      = match.get("league", "") or ""

        # Extract date from scheduled_at (ISO string)
        match_date = sched_at[:10]  # "YYYY-MM-DD"

        detail: dict = {
            "match_id": mid,
            "sport":    sport,
            "player_a": player_a,
            "player_b": player_b,
            "date":     match_date,
            "status":   None,
            "result":   None,
            "source":   None,
        }

        if sport not in ("table_tennis", "baseball"):
            detail["status"] = "skipped"
            detail["note"]   = f"Auto-resolve not yet implemented for {sport}"
            summary["skipped"] += 1
            summary["details"].append(detail)
            continue

        result_data = fetch_match_result(
            player_a, player_b, match_date, sport=sport, tour=tour
        )

        if result_data is None:
            source_label = "ESPN MLB scoreboard" if sport == "baseball" else "Setka Cup or TT Cup"
            detail["status"] = "failed"
            detail["note"]   = f"No result found on {source_label}"
            summary["failed"] += 1
        else:
            detail["result"]  = result_data["result"]
            detail["score_a"] = result_data.get("score_a")
            detail["score_b"] = result_data.get("score_b")
            detail["source"]  = result_data.get("source", "")
            detail["raw"]     = result_data.get("raw", "")

            if not dry_run:
                rec = record_outcome(
                    mid,
                    result_data["result"],
                    result_data.get("score_a"),
                    result_data.get("score_b"),
                    update_ratings=True,
                )
                if "error" in rec:
                    detail["status"] = "error"
                    detail["note"]   = rec["error"]
                    summary["failed"] += 1
                else:
                    detail["status"] = "resolved"
                    summary["resolved"] += 1
            else:
                detail["status"] = "dry_run"
                summary["resolved"] += 1  # count as would-be resolved

        summary["details"].append(detail)

    return summary


def _signal_accuracy_summary() -> dict:
    """
    After auto-resolve runs, compute per-signal accuracy correlation.
    Shows which signals are most predictive — useful for weight tuning.

    Returns dict: {signal_name: {n, accuracy_when_high, accuracy_when_low}}
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                s.signal_name,
                s.signal_value,
                s.participant,
                p.prob_a,
                o.result,
                m.participant_a
            FROM signals s
            JOIN matches m     ON m.id = s.match_id
            JOIN predictions p ON p.match_id = s.match_id
            JOIN outcomes o    ON o.match_id = s.match_id
            WHERE s.signal_value IS NOT NULL
              AND s.signal_name NOT IN ('ranking','data_confidence','recommendation')
            ORDER BY s.signal_name, s.match_id
        """).fetchall()

    if not rows:
        return {}

    # Group by signal name
    from collections import defaultdict
    by_signal: dict[str, list] = defaultdict(list)
    for r in rows:
        correct_a = (r["result"] == "a")
        is_player_a = (r["participant"] == r["participant_a"])
        # Normalise: was the high-signal player predicted correctly?
        by_signal[r["signal_name"]].append({
            "value":     r["signal_value"],
            "is_a":      is_player_a,
            "result_a":  correct_a,
        })

    result = {}
    for sig_name, entries in by_signal.items():
        if len(entries) < 5:
            continue
        vals = [e["value"] for e in entries]
        median = sorted(vals)[len(vals) // 2]
        high = [e for e in entries if e["value"] >= median]
        low  = [e for e in entries if e["value"] <  median]

        def acc(group):
            if not group:
                return None
            # Accuracy = high-signal player won
            correct = sum(
                1 for e in group
                if (e["is_a"] and e["result_a"]) or (not e["is_a"] and not e["result_a"])
            )
            return round(correct / len(group), 3)

        result[sig_name] = {
            "n":               len(entries),
            "accuracy_high":   acc(high),
            "accuracy_low":    acc(low),
            "lift":            round((acc(high) or 0) - (acc(low) or 0), 3),
        }

    return dict(sorted(result.items(), key=lambda x: -abs(x[1].get("lift", 0))))


if __name__ == "__main__":
    import json
    print("Running auto-resolve...")
    summary = run_auto_resolve(dry_run=False)
    print(f"Attempted: {summary['attempted']} | Resolved: {summary['resolved']} | Failed: {summary['failed']} | Skipped: {summary['skipped']}")
    for d in summary["details"]:
        status = d["status"].upper()
        print(f"  [{status}] {d['player_a']} vs {d['player_b']} ({d['date']}) → {d.get('result','?')} {d.get('score_a','')}-{d.get('score_b','')} via {d.get('source','')}")

    if summary["resolved"] > 0:
        print("\nSignal accuracy lift:")
        sig_acc = _signal_accuracy_summary()
        print(json.dumps(sig_acc, indent=2))
