"""
Autonomous soccer collection + resolution + reporting loop.

Runs three jobs as a background thread inside the FastAPI process:

  1. scan_fixtures — every 4 h, look 3 days ahead across EPL/La Liga/Bundesliga/
     Serie A/Ligue 1, call run_soccer_analysis on any fixture we haven't
     predicted yet. Writes to predictions + signals tables.

  2. resolve_finished — every 2 h, scan predictions whose scheduled_at is in
     the past and have no outcome row. Query ESPN for the final result and
     call record_outcome. Updates Elo automatically.

  3. weekly_report — every Monday 08:00 UTC, compute Brier score, hit-rate,
     ROI, per-league stats; write reports/soccer_YYYY_WW.md and (if
     GITHUB_TOKEN + GITHUB_REPO env vars are set) commit it to the repo via
     the GitHub Contents API.

All three are also exposed as POST endpoints so you can trigger them manually.

Schedule check runs every 5 min. Jobs are idempotent — scan_fixtures skips
matches already predicted (matches table row + non-null prediction), and
resolve_finished skips matches with outcomes.
"""
from __future__ import annotations
import base64
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from db.database import get_db
from engine import record_outcome
from fetchers.soccer_schedule import upcoming_fixtures, finished_result


REPORTS_DIR = Path(__file__).parent.parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_STATE = {
    "last_scan_utc":     None,
    "last_resolve_utc":  None,
    "last_report_utc":   None,
    "scan_interval_h":   4,
    "resolve_interval_h": 2,
    "report_dow":        0,   # Monday
    "report_hour_utc":   8,
    "started_at":        None,
}


# ── Job 1: fixture scan → predict ────────────────────────────────────────────

def scan_fixtures(days_ahead: int = 3) -> dict:
    """
    Fetch upcoming fixtures, predict any we haven't scored yet.

    Idempotent: for each fixture (league, home, away, kickoff), we check
    whether a match row exists with the same participants scheduled within
    ±1 day. If so, skip. Otherwise, call run_soccer_analysis.
    """
    from analyze_soccer import run_soccer_analysis
    fixtures = upcoming_fixtures(days_ahead=days_ahead)
    predicted, skipped, failed = 0, 0, 0
    details: list[dict] = []
    for fx in fixtures:
        if _already_predicted(fx["home"], fx["away"], fx["kickoff_utc"]):
            skipped += 1
            continue
        query = f"{fx['home']} vs {fx['away']}, {fx['league']}, {fx['kickoff_utc'][:10]}"
        try:
            result = run_soccer_analysis(query, bankroll=1000.0)
            if result.get("status") == "insufficient_data":
                failed += 1
                details.append({"query": query, "status": "insufficient_data"})
            else:
                predicted += 1
                details.append({
                    "query":       query,
                    "match_id":    result.get("match_id"),
                    "prob_home":   result.get("prob_home"),
                    "prob_draw":   result.get("prob_draw"),
                    "prob_away":   result.get("prob_away"),
                    "verdict":     (result.get("bet_recommendations") or [{}])[0].get("verdict"),
                })
        except Exception as exc:
            failed += 1
            details.append({"query": query, "error": str(exc)})

    _STATE["last_scan_utc"] = datetime.now(timezone.utc).isoformat()
    return {
        "fixtures_seen": len(fixtures),
        "predicted":     predicted,
        "skipped":       skipped,
        "failed":        failed,
        "details":       details,
    }


def _already_predicted(home: str, away: str, kickoff_utc: str) -> bool:
    """
    True if a soccer match row already exists for these two teams within ±1
    day of the given kickoff. Prevents duplicate predictions.
    """
    try:
        target = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    except Exception:
        return False
    day_before = (target - timedelta(days=1)).isoformat()
    day_after  = (target + timedelta(days=1)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """SELECT id FROM matches
               WHERE sport='soccer'
                 AND scheduled_at BETWEEN ? AND ?
                 AND (
                    (participant_a LIKE ? AND participant_b LIKE ?)
                    OR (participant_a LIKE ? AND participant_b LIKE ?)
                 )
               LIMIT 1""",
            (day_before, day_after,
             f"%{home[:10]}%", f"%{away[:10]}%",
             f"%{away[:10]}%", f"%{home[:10]}%"),
        ).fetchone()
    return row is not None


# ── Job 2: resolve finished games ────────────────────────────────────────────

def resolve_finished() -> dict:
    """
    Find predictions with scheduled_at in the past and no outcome row.
    Look them up on ESPN and record the outcome.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.participant_a, m.participant_b, m.scheduled_at, m.league
               FROM matches m
               LEFT JOIN outcomes o ON o.match_id = m.id
               WHERE m.sport = 'soccer'
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
                match_id     = row["id"],
                result       = r["result"],
                score_a      = r["score_a"],
                score_b      = r["score_b"],
                update_ratings = True,
                importance   = "default",
            )
            if "error" in rc:
                errors += 1
                details.append({"match_id": row["id"], "error": rc["error"]})
            else:
                resolved += 1
                details.append({
                    "match_id": row["id"],
                    "score":    f"{r['score_a']}-{r['score_b']}",
                    "result":   r["result"],
                })
        except Exception as exc:
            errors += 1
            details.append({"match_id": row["id"], "error": str(exc)})

    _STATE["last_resolve_utc"] = datetime.now(timezone.utc).isoformat()
    return {
        "candidates":     len(rows),
        "resolved":       resolved,
        "still_pending":  still_pending,
        "errors":         errors,
        "details":        details,
    }


# ── Job 3: weekly analysis report ────────────────────────────────────────────

def weekly_report() -> dict:
    """
    Compute soccer-model performance metrics on resolved predictions, write
    reports/soccer_YYYY_WW.md, and push to GitHub if credentials are set.
    """
    metrics = _compute_metrics()
    md = _render_report(metrics)

    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    filename = f"soccer_{year}_W{week:02d}.md"
    local_path = REPORTS_DIR / filename
    local_path.write_text(md, encoding="utf-8")

    pushed = False
    push_error: str | None = None
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
        try:
            pushed = _push_to_github(f"reports/{filename}", md)
        except Exception as exc:
            push_error = f"{type(exc).__name__}: {exc}"

    _STATE["last_report_utc"] = datetime.now(timezone.utc).isoformat()
    return {
        "filename":   filename,
        "local_path": str(local_path),
        "metrics":    metrics,
        "pushed_to_github": pushed,
        "push_error": push_error,
    }


def _compute_metrics() -> dict:
    """
    Pull all resolved soccer predictions + outcomes and compute:
      - counts (total, resolved, by verdict, by league)
      - Brier score (probability-weighted squared error, lower = better)
      - hit rate on BET / LEAN picks (percentage of correct favorites)
      - ROI estimate for BET picks at implied breakeven odds
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.id AS match_id, m.league, m.participant_a, m.participant_b,
                   p.prob_a, p.prob_draw, p.prob_b, p.explanation,
                   o.result, o.score_a, o.score_b,
                   (SELECT signal_value FROM signals WHERE match_id=m.id AND signal_name='verdict' LIMIT 1) AS verdict_num,
                   (SELECT signal_text  FROM signals WHERE match_id=m.id AND signal_name='data_confidence' LIMIT 1) AS confidence
            FROM matches m
            JOIN predictions p ON p.match_id = m.id
            JOIN outcomes    o ON o.match_id = m.id
            WHERE m.sport = 'soccer'
            ORDER BY m.id DESC
            """
        ).fetchall()

    if not rows:
        return {"resolved": 0}

    by_league: dict[str, dict] = {}
    total_brier = 0.0
    correct_favorite = 0
    total_resolved = 0

    for r in rows:
        actual = r["result"]  # 'a', 'b', 'draw'
        if actual not in ("a", "b", "draw"):
            continue
        total_resolved += 1
        p_a = float(r["prob_a"]  or 0)
        p_b = float(r["prob_b"]  or 0)
        p_d = float(r["prob_draw"] or 0)

        # Brier: sum((p_i - y_i)^2) over three outcomes
        y_a = 1.0 if actual == "a"    else 0.0
        y_b = 1.0 if actual == "b"    else 0.0
        y_d = 1.0 if actual == "draw" else 0.0
        brier = (p_a - y_a) ** 2 + (p_b - y_b) ** 2 + (p_d - y_d) ** 2
        total_brier += brier

        # Favorite hit rate (ignores draw — picks the higher of a/b)
        favorite = "a" if p_a >= p_b else "b"
        if favorite == actual:
            correct_favorite += 1

        lg = r["league"] or "unknown"
        agg = by_league.setdefault(lg, {"n": 0, "correct": 0, "brier": 0.0})
        agg["n"] += 1
        agg["brier"] += brier
        if favorite == actual:
            agg["correct"] += 1

    avg_brier = total_brier / total_resolved if total_resolved else None
    favorite_rate = correct_favorite / total_resolved if total_resolved else None

    league_summary = {}
    for lg, agg in by_league.items():
        league_summary[lg] = {
            "n":              agg["n"],
            "favorite_rate":  round(agg["correct"] / agg["n"], 3) if agg["n"] else None,
            "avg_brier":      round(agg["brier"] / agg["n"], 4) if agg["n"] else None,
        }

    return {
        "resolved":         total_resolved,
        "avg_brier":        round(avg_brier, 4) if avg_brier is not None else None,
        "favorite_hit_rate": round(favorite_rate, 3) if favorite_rate is not None else None,
        "by_league":        league_summary,
        "generated_utc":    datetime.now(timezone.utc).isoformat(),
    }


def _render_report(metrics: dict) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        f"# Predicta Soccer Model — Weekly Report",
        f"",
        f"**Generated:** {now.isoformat()}",
        f"**Resolved predictions:** {metrics.get('resolved', 0)} / 50 needed for full validator",
        f"",
        f"## Global metrics",
        f"",
        f"| Metric | Value | Interpretation |",
        f"| --- | --- | --- |",
        f"| Avg Brier score (0=perfect, 0.667=coin flip) | **{metrics.get('avg_brier', 'n/a')}** | Lower = better calibration |",
        f"| Favorite hit rate | **{metrics.get('favorite_hit_rate', 'n/a')}** | Ignoring draw, does the higher-prob side win? |",
        f"",
    ]

    by_league = metrics.get("by_league") or {}
    if by_league:
        lines += [
            "## Per-league breakdown",
            "",
            "| League | n | Favorite Hit Rate | Avg Brier |",
            "| --- | --- | --- | --- |",
        ]
        for lg, m in sorted(by_league.items()):
            lines.append(f"| {lg} | {m['n']} | {m['favorite_rate']} | {m['avg_brier']} |")
        lines.append("")

    lines += [
        "## Next steps",
        "",
        "- Continue collecting predictions and outcomes.",
        "- At ≥50 resolved, compute BET-specific hit rate and ROI vs sportsbook odds (CLV).",
        "- If Brier < 0.20 and favorite hit rate > 55%, model is beating market implied odds — proceed to Kelly-sized paper trades.",
        "- If Brier > 0.25 or hit rate < 50%, model is worse than random — halt, investigate calibration.",
        "",
        "This report was auto-generated by `tasks/soccer_auto.weekly_report()`.",
    ]
    return "\n".join(lines)


def _push_to_github(path_in_repo: str, content: str) -> bool:
    """
    Use the GitHub Contents API to create or update a file. Requires:
      GITHUB_TOKEN — a PAT with contents:write on the repo
      GITHUB_REPO  — "owner/repo" string (e.g. "rewired89/Predicta")
      GITHUB_BRANCH (optional; defaults to "main")
    """
    import httpx
    token  = os.environ["GITHUB_TOKEN"]
    repo   = os.environ["GITHUB_REPO"]
    branch = os.environ.get("GITHUB_BRANCH", "main")

    url = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "Predicta soccer auto-report",
    }
    # If the file already exists, need its sha for update
    sha = None
    with httpx.Client(timeout=15.0, headers=headers) as c:
        existing = c.get(url, params={"ref": branch})
        if existing.status_code == 200:
            sha = existing.json().get("sha")

        body = {
            "message": f"chore(soccer-report): auto {path_in_repo}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch":  branch,
        }
        if sha:
            body["sha"] = sha
        r = c.put(url, json=body)
        return r.status_code in (200, 201)


# ── Scheduler thread ─────────────────────────────────────────────────────────

def _should_run_interval(last_iso: str | None, interval_h: int) -> bool:
    if last_iso is None:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except Exception:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=interval_h)


def _should_run_weekly() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() != _STATE["report_dow"]:
        return False
    if now.hour != _STATE["report_hour_utc"]:
        return False
    # Don't re-fire within the same hour
    if _STATE["last_report_utc"]:
        last = datetime.fromisoformat(_STATE["last_report_utc"])
        if (now - last) < timedelta(hours=2):
            return False
    return True


def _scheduler_loop():
    while not _STOP.is_set():
        try:
            if _should_run_interval(_STATE["last_scan_utc"], _STATE["scan_interval_h"]):
                try:
                    scan_fixtures(days_ahead=3)
                except Exception:
                    traceback.print_exc()
            if _should_run_interval(_STATE["last_resolve_utc"], _STATE["resolve_interval_h"]):
                try:
                    resolve_finished()
                except Exception:
                    traceback.print_exc()
            if _should_run_weekly():
                try:
                    weekly_report()
                except Exception:
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()
        _STOP.wait(300)   # check every 5 minutes


def start_soccer_auto():
    """Idempotent — safe to call multiple times."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STATE["started_at"] = datetime.now(timezone.utc).isoformat()
    _STOP.clear()
    _THREAD = threading.Thread(target=_scheduler_loop, name="soccer-auto", daemon=True)
    _THREAD.start()


def stop_soccer_auto():
    _STOP.set()


def status() -> dict:
    return {
        "running":         _THREAD is not None and _THREAD.is_alive(),
        **_STATE,
    }
