"""
tasks/nrfi_auto.py — always-on NRFI daily pipeline (Railway).

GitHub Actions' `schedule` cron is unreliable (it delayed/skipped the 9 AM
predict run). This runs the same predict / capture-odds / resolve steps from an
in-process daemon thread inside the always-on Railway web app — the same proven
pattern the soccer auto-collector uses — and pushes the JSON + markdown report
to GitHub via the Contents API so results persist and stay readable by the /v1
API, Kimi, and Claude.

Schedule (UTC): predict 13:00 (9 AM ET) · capture-odds 23:00 (7 PM ET) ·
resolve 05:00 (1 AM ET, for the prior day). Each job runs at most once per UTC
day (first scheduler tick after its hour). Disable with NRFI_AUTO_DISABLED=1.

Requires (set on Railway) for results to persist:
  GITHUB_TOKEN — PAT with contents:write on the repo
  GITHUB_REPO  — "rewired89/Predicta"
  GITHUB_BRANCH (optional, default "main")
Without them the jobs still run and log to the DB, but files won't be committed.
"""
from __future__ import annotations
import base64
import json
import os
import threading
import traceback
from datetime import datetime, timezone, date, timedelta

PREDICT_HOUR_UTC = 13   # 9 AM ET
CAPTURE_HOUR_UTC = 23   # 7 PM ET
RESOLVE_HOUR_UTC = 5    # 1 AM ET (resolves the prior day)

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_STATE: dict = {
    "started_at":   None,
    "last_predict": None,   # UTC date string, once per day
    "last_capture": None,
    "last_resolve": None,
    "last_run":     None,    # human log of the most recent action
    "last_error":   None,
    "last_push":    None,
}


# ── GitHub Contents API push (works from Railway, no git CLI) ─────────────────

def _push_file(path_in_repo: str, content: str, message: str) -> bool:
    token = os.environ.get("GITHUB_TOKEN")
    repo  = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return False
    import httpx
    branch = os.environ.get("GITHUB_BRANCH", "main")
    url = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "Predicta nrfi-auto",
    }
    try:
        with httpx.Client(timeout=20.0, headers=headers) as c:
            existing = c.get(url, params={"ref": branch})
            sha = existing.json().get("sha") if existing.status_code == 200 else None
            body = {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch":  branch,
            }
            if sha:
                body["sha"] = sha
            r = c.put(url, json=body)
            return r.status_code in (200, 201)
    except Exception as exc:
        _STATE["last_error"] = f"push {path_in_repo}: {type(exc).__name__}: {exc}"
        return False


def _push_day(target_date: str) -> list[str]:
    from scripts.daily_nrfi import PRED_DIR, RPT_DIR
    pushed = []
    pj = PRED_DIR / f"{target_date}.json"
    if pj.exists() and _push_file(
        f"data/nrfi_predictions/{target_date}.json", pj.read_text(),
        f"nrfi(railway): predictions {target_date}"):
        pushed.append("json")
    rp = RPT_DIR / f"{target_date}.md"
    if rp.exists() and _push_file(
        f"data/nrfi_reports/{target_date}.md", rp.read_text(),
        f"nrfi(railway): report {target_date}"):
        pushed.append("report")
    if pushed:
        _STATE["last_push"] = f"{datetime.now(timezone.utc).isoformat()} → {target_date} {pushed}"
    return pushed


# ── The three jobs (reuse scripts/daily_nrfi) ────────────────────────────────

def run_predict(target_date: str | None = None) -> dict:
    from scripts.daily_nrfi import (fetch_schedule, run_predictions,
                                    capture_odds, write_report, PRED_DIR)
    td = target_date or date.today().isoformat()
    games = fetch_schedule(td)
    if not games:
        return {"date": td, "games": 0}
    preds = run_predictions(games, td)
    capture_odds(preds, phase="entry")
    PRED_DIR.mkdir(parents=True, exist_ok=True)
    (PRED_DIR / f"{td}.json").write_text(json.dumps(preds, indent=2))
    write_report(preds, td)
    pushed = _push_day(td)
    return {"date": td, "games": len(games), "pushed": pushed}


def run_capture(target_date: str | None = None) -> dict:
    from scripts.daily_nrfi import capture_odds, PRED_DIR
    td = target_date or date.today().isoformat()
    pj = PRED_DIR / f"{td}.json"
    if not pj.exists():
        return {"date": td, "captured": 0, "note": "no prediction file"}
    preds = json.loads(pj.read_text())
    n = capture_odds(preds, phase="closing")
    pj.write_text(json.dumps(preds, indent=2))
    pushed = _push_day(td)
    return {"date": td, "captured": n, "pushed": pushed}


def run_resolve(target_date: str | None = None) -> dict:
    from scripts.daily_nrfi import resolve_predictions, write_report, PRED_DIR
    td = target_date or (date.today() - timedelta(days=1)).isoformat()
    preds = resolve_predictions(td)
    if not preds:
        return {"date": td, "resolved": 0}
    (PRED_DIR / f"{td}.json").write_text(json.dumps(preds, indent=2))
    write_report(preds, td, is_resolve=True)
    pushed = _push_day(td)
    return {"date": td, "resolved": len(preds), "pushed": pushed}


# ── Scheduler ────────────────────────────────────────────────────────────────

def _due(job_key: str, hour_utc: int) -> bool:
    now = datetime.now(timezone.utc)
    if now.hour < hour_utc:
        return False
    return _STATE.get(job_key) != now.date().isoformat()


def _mark(job_key: str) -> None:
    _STATE[job_key] = datetime.now(timezone.utc).date().isoformat()


def _run_job(name: str, fn, job_key: str) -> None:
    try:
        result = fn()
        _STATE["last_run"] = f"{datetime.now(timezone.utc).isoformat()} {name}: {result}"
    except Exception as exc:
        _STATE["last_error"] = f"{name}: {type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        _mark(job_key)   # once/day even on failure — no retry storms


def _loop() -> None:
    while not _STOP.is_set():
        try:
            if _due("last_predict", PREDICT_HOUR_UTC):
                _run_job("predict", run_predict, "last_predict")
            if _due("last_capture", CAPTURE_HOUR_UTC):
                _run_job("capture", run_capture, "last_capture")
            if _due("last_resolve", RESOLVE_HOUR_UTC):
                _run_job("resolve", run_resolve, "last_resolve")
        except Exception:
            traceback.print_exc()
        _STOP.wait(300)   # check every 5 minutes


def start_nrfi_auto() -> None:
    """Start the always-on NRFI scheduler. Idempotent. Disable via NRFI_AUTO_DISABLED=1."""
    global _THREAD
    if os.environ.get("NRFI_AUTO_DISABLED"):
        return
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STATE["started_at"] = datetime.now(timezone.utc).isoformat()
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="nrfi-auto", daemon=True)
    _THREAD.start()


def status() -> dict:
    return {**_STATE,
            "running": _THREAD is not None and _THREAD.is_alive(),
            "github_push_configured": bool(os.environ.get("GITHUB_TOKEN")
                                           and os.environ.get("GITHUB_REPO")),
            "schedule_utc": {"predict": PREDICT_HOUR_UTC,
                             "capture": CAPTURE_HOUR_UTC,
                             "resolve": RESOLVE_HOUR_UTC}}
