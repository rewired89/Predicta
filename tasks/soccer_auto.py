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
    Compute soccer model performance (Round 6 rewrite for Kimi P4-P6):

      Global:
        - resolved (n), avg Brier score
        - BET pick hit rate + avg odds + implied ROI
        - LEAN pick hit rate + avg odds + implied ROI
        - Calibration buckets (50-60%, 60-70%, 70-80%, 80%+)
      Per-league:
        - n, BET hit rate, LEAN hit rate, avg Brier

    Depends on Round 6 P4-P6 signal persistence in analyze_soccer.py:
    verdict / bet_side / bet_model_prob / bet_decimal_odds / bet_edge_pp are
    logged per prediction.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT m.id AS match_id, m.league,
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
            WHERE m.sport = 'soccer'
            ORDER BY m.id DESC
            """
        ).fetchall()

    if not rows:
        return {"resolved": 0}

    total_brier    = 0.0
    total_resolved = 0
    by_league: dict[str, dict] = {}

    # BET / LEAN pick trackers
    bet_picks:  list[dict] = []
    lean_picks: list[dict] = []

    # Calibration buckets — model prob buckets and actual hit rate
    # (only when the model made a BET or LEAN pick — most-confident calls)
    buckets: dict[str, dict] = {
        "50-60%": {"n": 0, "hits": 0},
        "60-70%": {"n": 0, "hits": 0},
        "70-80%": {"n": 0, "hits": 0},
        "80%+":   {"n": 0, "hits": 0},
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
        p_a = float(r["prob_a"]  or 0)
        p_b = float(r["prob_b"]  or 0)
        p_d = float(r["prob_draw"] or 0)

        # Brier score
        y_a = 1.0 if actual == "a" else 0.0
        y_b = 1.0 if actual == "b" else 0.0
        y_d = 1.0 if actual == "draw" else 0.0
        brier = (p_a - y_a) ** 2 + (p_b - y_b) ** 2 + (p_d - y_d) ** 2
        total_brier += brier

        lg = r["league"] or "unknown"
        agg = by_league.setdefault(lg, {"n": 0, "brier": 0.0,
                                       "bet_n": 0, "bet_hits": 0,
                                       "lean_n": 0, "lean_hits": 0})
        agg["n"] += 1
        agg["brier"] += brier

        # Verdict-based tracking
        verdict = (r["verdict"] or "").upper()
        bet_side = r["bet_side"]
        model_prob = float(r["bet_model_prob"] or 0) if r["bet_model_prob"] is not None else None
        decimal_odds = float(r["bet_decimal_odds"] or 0) if r["bet_decimal_odds"] is not None else None

        if verdict in ("BET", "LEAN") and bet_side in ("home", "away", "draw"):
            actual_side = {"a": "home", "b": "away", "draw": "draw"}[actual]
            hit = 1 if actual_side == bet_side else 0
            entry = {
                "match_id":     r["match_id"],
                "league":       lg,
                "bet_side":     bet_side,
                "model_prob":   model_prob,
                "decimal_odds": decimal_odds,
                "actual":       actual_side,
                "hit":          hit,
                "edge_pp":      float(r["bet_edge_pp"] or 0) if r["bet_edge_pp"] is not None else None,
            }
            if verdict == "BET":
                bet_picks.append(entry)
                agg["bet_n"] += 1
                agg["bet_hits"] += hit
            else:
                lean_picks.append(entry)
                agg["lean_n"] += 1
                agg["lean_hits"] += hit

            # Calibration on picked side's model prob
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
        # Implied ROI at avg odds: hit_rate × (avg_odds - 1) - (1 - hit_rate)
        # Only compute when we have odds for every pick (else it's misleading)
        implied_roi = None
        if odds_available and len(odds_available) == n:
            hr = hits / n
            implied_roi = hr * (avg_odds - 1) - (1 - hr)
        return {
            "n":              n,
            "hits":           hits,
            "hit_rate":       round(hits / n, 3),
            "avg_odds":       round(avg_odds, 3) if avg_odds else None,
            "implied_roi":    round(implied_roi, 3) if implied_roi is not None else None,
        }

    league_summary = {}
    for lg, agg in by_league.items():
        league_summary[lg] = {
            "n":            agg["n"],
            "avg_brier":    round(agg["brier"] / agg["n"], 4) if agg["n"] else None,
            "bet_n":        agg["bet_n"],
            "bet_hit_rate": round(agg["bet_hits"] / agg["bet_n"], 3) if agg["bet_n"] else None,
            "lean_n":       agg["lean_n"],
            "lean_hit_rate": round(agg["lean_hits"] / agg["lean_n"], 3) if agg["lean_n"] else None,
        }

    calib = {}
    for name, b in buckets.items():
        calib[name] = {
            "n":        b["n"],
            "hit_rate": round(b["hits"] / b["n"], 3) if b["n"] else None,
        }

    return {
        "resolved":     total_resolved,
        "avg_brier":    round(total_brier / total_resolved, 4) if total_resolved else None,
        "bet":          _pick_summary(bet_picks),
        "lean":         _pick_summary(lean_picks),
        "calibration":  calib,
        "by_league":    league_summary,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }


def _render_report(metrics: dict) -> str:
    """
    Kimi Round 6 P4-P6 rewrite. Report structure:

      1. Header (n resolved, avg Brier)
      2. BET picks table: n, hit rate, avg odds, implied ROI
      3. LEAN picks table: same
      4. Calibration buckets (does 60-70% predicted happen 60-70% of the time?)
      5. Per-league breakdown (n, avg Brier, BET hit rate, LEAN hit rate)
      6. Ship/halt thresholds

    Favorite hit rate has been removed — it was Kimi-flagged as misleading for
    a probabilistic model. BET/LEAN hit rate + ROI is what actually matters.
    """
    now = datetime.now(timezone.utc)
    resolved = metrics.get("resolved", 0)
    lines = [
        f"# Predicta Soccer Model — Weekly Report",
        f"",
        f"**Generated:** {now.isoformat()}",
        f"**Resolved predictions:** {resolved} / 50 needed for full validator",
        f"**Global avg Brier:** {metrics.get('avg_brier', 'n/a')}   *(0 = perfect, 0.667 = coin flip)*",
        f"",
    ]

    bet  = metrics.get("bet")  or {"n": 0}
    lean = metrics.get("lean") or {"n": 0}

    lines += [
        "## BET picks — the money metric",
        "",
        "| Metric | Value | Notes |",
        "| --- | --- | --- |",
        f"| n | **{bet.get('n', 0)}** | Number of BET recommendations |",
        f"| Hit rate | **{bet.get('hit_rate', 'n/a')}** | Fraction that won |",
        f"| Avg odds | **{bet.get('avg_odds', 'n/a')}** | Mean decimal odds when supplied |",
        f"| Implied ROI | **{bet.get('implied_roi', 'n/a')}** | hit × (odds−1) − (1−hit). Positive = profitable. |",
        "",
        "## LEAN picks — lower confidence, watch only",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| n | {lean.get('n', 0)} |",
        f"| Hit rate | {lean.get('hit_rate', 'n/a')} |",
        f"| Avg odds | {lean.get('avg_odds', 'n/a')} |",
        f"| Implied ROI | {lean.get('implied_roi', 'n/a')} |",
        "",
    ]

    calib = metrics.get("calibration") or {}
    if any(v.get("n", 0) > 0 for v in calib.values()):
        lines += [
            "## Calibration — does the model's probability match reality?",
            "",
            "*Only picks where verdict = BET or LEAN. Well-calibrated model has hit rate close to the bucket midpoint.*",
            "",
            "| Model prob bucket | n | Actual hit rate |",
            "| --- | --- | --- |",
        ]
        for name in ["50-60%", "60-70%", "70-80%", "80%+"]:
            b = calib.get(name, {})
            lines.append(f"| {name} | {b.get('n', 0)} | {b.get('hit_rate', 'n/a')} |")
        lines.append("")

    by_league = metrics.get("by_league") or {}
    if by_league:
        lines += [
            "## Per-league breakdown",
            "",
            "| League | n | Avg Brier | BET n | BET hit rate | LEAN n | LEAN hit rate |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for lg, m in sorted(by_league.items()):
            lines.append(
                f"| {lg} | {m['n']} | {m['avg_brier']} | "
                f"{m['bet_n']} | {m['bet_hit_rate']} | "
                f"{m['lean_n']} | {m['lean_hit_rate']} |"
            )
        lines.append("")

    lines += [
        "## Ship / halt thresholds",
        "",
        "- **Ship it** if all three hold: Brier < 0.20, BET hit rate > 55%, BET implied ROI > 0.",
        "- **Watch** if two of three hold — collect more data before scaling.",
        "- **Halt** if any of these: Brier > 0.25, BET hit rate < 45%, BET implied ROI < −0.05.",
        "",
        "**CLV check (manual for now):** For the first 20 BET picks, look up Pinnacle's closing line and compare against the odds we used. If we beat the close ≥55% of the time, the edge is real. If not, we're getting lucky.",
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
