"""Heartbeat, daily DB backup, and the honest evaluation view (Kimi round 3)."""
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from db.database import DB_PATH, get_db

log = logging.getLogger("maintenance")
BACKUP_KEEP = 14
_beats: dict[str, float] = {}


def heartbeat(name: str) -> None:
    _beats[name] = time.time()


def get_heartbeats() -> dict:
    now = time.time()
    return {k: {"seconds_ago": round(now - v)} for k, v in _beats.items()}


def backup_db(keep: int = BACKUP_KEEP) -> dict:
    """One consistent copy per day in <db folder>/backups/, newest `keep` kept."""
    d = Path(DB_PATH).parent / "backups"
    d.mkdir(exist_ok=True)
    dest = d / f"predicta-{datetime.now(timezone.utc).strftime('%Y%m%d')}.db"
    if dest.exists():
        return {"status": "already_done", "file": str(dest)}
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    for old in sorted(d.glob("predicta-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return {"status": "ok", "file": str(dest), "kept": len(list(d.glob('predicta-*.db')))}


_CAND_DDL = """CREATE TABLE IF NOT EXISTS candidate_log (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, engine TEXT NOT NULL, symbol TEXT NOT NULL,
    composite REAL, threshold REAL, outcome TEXT NOT NULL)"""


def log_candidate(engine: str, symbol: str, composite, threshold, outcome: str) -> None:
    """Record a scored symbol that did NOT become a trade (so rejected ones can be audited)."""
    try:
        with get_db() as conn:
            conn.execute(_CAND_DDL)
            conn.execute("INSERT INTO candidate_log (ts, engine, symbol, composite, threshold, outcome) VALUES (?,?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), engine, symbol, composite, threshold, outcome))
    except Exception as e:
        log.error(f"[CANDIDATE] write failed: {e}")


def _boot_ci(xs, n=2000, seed=1):
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def evaluation() -> dict:
    """Per engine, REAL fills only, closed trades: win rate, mean return, 95% bootstrap interval."""
    out = {}
    for eng in ("high_value", "low_value", "automaton"):
        with get_db() as c:
            rows = c.execute(
                "SELECT pnl_pct, is_hypothetical FROM intraday_trades WHERE engine=? "
                "AND exit_time IS NOT NULL AND pnl_pct IS NOT NULL", (eng,)).fetchall()
        for label, sel in (("suggested_and_real", rows), ("real_only", [r for r in rows if not r["is_hypothetical"]])):
            xs = [r["pnl_pct"] for r in sel]
            e = {"closed_trades": len(xs)}
            if len(xs) >= 5:
                lo, hi = _boot_ci(xs)
                e.update(win_rate=round(sum(x > 0 for x in xs) / len(xs), 3),
                         mean_return_pct=round(sum(xs) / len(xs), 3),
                         ci95_low=round(lo, 3), ci95_high=round(hi, 3),
                         verdict=("UNPROVEN: interval includes zero" if lo <= 0 <= hi
                                  else ("POSITIVE (still needs forward confirmation)" if lo > 0 else "NEGATIVE")))
            else:
                e["verdict"] = "TOO FEW TRADES (need 5+; real decisions need 100+)"
            out.setdefault(eng, {})[label] = e
    return {"note": "Baseline is zero return. Not compared to SPY yet; stored P&L is close-to-close per trade.", "engines": out}
