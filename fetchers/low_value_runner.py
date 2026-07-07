"""
Low Value engine — paper trading runner (Kimi review round 6 follow-up).

Independent background thread from fetchers/high_value_runner.py — shares no
signal logic, only read-only time-of-day helpers (_et_now/_et_minutes,
imported, never mutated) and the same intraday_trades table (scoped by
engine='low_value'). Daily cadence, not intraday:

  8:00 AM ET (pre-market, after news scan): scan the daily universe, log any
  |composite| >= thesis_tracker.ENTRY_THRESHOLD as a hypothetical trade.
  Same 8:00 AM tick: check open positions for exit (thesis resolved, +50%,
  -50%, or 5 trading days elapsed) — once daily, no intraday checks, no
  force-close at 3:50 PM (that's a High Value concept; Low Value holds
  overnight/multi-day by design).

Position sizing: fixed $25/trade (models.trading.shared.kelly.
low_value_position_size). Max 3 concurrent positions.
"""
from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fetchers.high_value_runner import _et_now, _et_minutes
from fetchers.alpaca import get_daily_bars, get_snapshots
from fetchers.trading_logger import log_low_value_trade, log_trade_exit, log_universe_snapshot
from db.database import get_db

from models.trading.low_value.scanner import build_low_value_universe
from models.trading.low_value.news_overlay import scan_universe_news
from models.trading.low_value.thesis_tracker import (
    compute_thesis_score, dominant_thesis_type, sector_etf_for_symbol, ENTRY_THRESHOLD,
)
from models.trading.shared.kelly import LOW_VALUE_MAX_CONCURRENT_POSITIONS

log = logging.getLogger("low_value_runner")

LOW_VALUE_SCAN_HOUR_ET: int = 8      # 8:00 AM ET
LOW_VALUE_HOLD_DAYS: int = 5          # max hold, trading days
LOW_VALUE_TARGET_PCT: float = 0.50    # +50% exit
LOW_VALUE_STOP_PCT: float = 0.50      # -50% exit
NEGATIVE_THESES: set[str] = {"EARNINGS_MISS", "ANALYST_DOWNGRADE", "REGULATORY_RISK", "OPERATIONAL_CRISIS"}

_runner_thread: Optional[threading.Thread] = None
_runner_active: bool = False
_run_log: list[dict] = []
_universe_cache: dict[str, list[str]] = {}   # {date_str: [symbols]}


def _in_scan_window() -> bool:
    """True between 08:00 and 08:14 ET on weekdays — the daily pre-market scan window."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    t = _et_minutes()
    scan_start = LOW_VALUE_SCAN_HOUR_ET * 60
    return scan_start <= t < scan_start + 15


def get_daily_universe(force_refresh: bool = False) -> list[str]:
    """
    Today's Low Value universe, cached in-memory for the day. Logs a snapshot
    to low_value_universe_snapshot on every fresh scan (not on cache hits).
    """
    today_str = _et_now().strftime("%Y-%m-%d")
    if not force_refresh and today_str in _universe_cache:
        return _universe_cache[today_str]

    universe = build_low_value_universe(today_str)
    _universe_cache.clear()   # only ever keep today's entry
    _universe_cache[today_str] = universe
    log_universe_snapshot(today_str, universe)
    log.info(f"[LOW_VALUE] Universe scan for {today_str} — {len(universe)} symbols")
    return universe


def _load_open_positions() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, entry_price, entry_time, lv_thesis_type
            FROM intraday_trades
            WHERE engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NULL
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def run_low_value_scan(symbols: Optional[list[str]] = None) -> list[int]:
    """
    Scans the daily universe (or an explicit override list), logs any
    |composite| >= ENTRY_THRESHOLD symbol as a hypothetical trade, capped at
    LOW_VALUE_MAX_CONCURRENT_POSITIONS total open positions. Returns the list
    of trade_ids created.
    """
    syms = symbols if symbols is not None else get_daily_universe()
    if not syms:
        return []

    existing_open = len(_load_open_positions())
    news_by_symbol = scan_universe_news(syms)
    trade_ids: list[int] = []

    for sym in syms:
        if existing_open + len(trade_ids) >= LOW_VALUE_MAX_CONCURRENT_POSITIONS:
            _run_log.append({
                "ts": _et_now().isoformat(), "event": "PORTFOLIO_CAP_SKIP", "sym": sym,
                "note": f"Cap ({LOW_VALUE_MAX_CONCURRENT_POSITIONS}) reached — skipping",
            })
            break
        try:
            daily_bars = get_daily_bars(sym, days=25)
            if not daily_bars:
                continue
            sector_etf = sector_etf_for_symbol(sym)
            sector_bars = get_daily_bars(sector_etf, days=25)
            news_result = news_by_symbol.get(sym)

            thesis = compute_thesis_score(sym, daily_bars, sector_bars, news_result)
            composite = thesis.get("composite")
            if composite is None or not thesis.get("entry_eligible"):
                continue

            side = "long" if composite > 0 else "short"
            entry_price = daily_bars[-1].get("c")
            if not entry_price or entry_price <= 0:
                continue

            thesis_type = dominant_thesis_type(thesis, news_result)
            tid = log_low_value_trade(
                symbol=sym, side=side, entry_price=entry_price,
                score_value=composite, thesis_result=thesis, thesis_type=thesis_type,
                news_result=news_result, hold_days=LOW_VALUE_HOLD_DAYS,
            )
            trade_ids.append(tid)
            _run_log.append({
                "ts": _et_now().isoformat(), "event": "ENTRY", "sym": sym,
                "score": composite, "side": side, "thesis_type": thesis_type, "tid": tid,
            })
            log.info(f"[LOW_VALUE] Logged {side.upper()} {sym} composite={composite} thesis={thesis_type} tid={tid}")
        except Exception as exc:
            log.warning(f"[LOW_VALUE] scan error for {sym}: {exc}")

    if len(_run_log) > 100:
        _run_log[:] = _run_log[-100:]
    return trade_ids


def _trading_days_elapsed(entry_time_iso: str, now_et: datetime) -> int:
    """Weekday-only day count since entry — approximation (no market-holiday calendar)."""
    try:
        entry_dt = datetime.fromisoformat(entry_time_iso)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    days = 0
    cursor = entry_dt.astimezone(now_et.tzinfo).date()
    today = now_et.date()
    while cursor < today:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def check_low_value_exits() -> dict:
    """
    Daily exit check (called once per scan tick, not intraday):
      thesis resolved  — entry thesis was a NEGATIVE_THESES flag and a fresh
                          POSITIVE_CATALYST flag now appears in the news scan
      target hit       — price moved +50% (long) / -50% (short) in the
                          favorable direction
      stop hit         — price moved -50% (long) / +50% (short) against
      time exit        — LOW_VALUE_HOLD_DAYS trading days elapsed
    Returns {checked, closed: [...]}.
    """
    open_positions = _load_open_positions()
    if not open_positions:
        return {"checked": 0, "closed": []}

    symbols = [p["symbol"] for p in open_positions]
    snaps = get_snapshots(symbols)
    news_by_symbol = scan_universe_news(symbols, days=2)  # only need very recent headlines for resolution check
    now_et = _et_now()
    closed = []

    for pos in open_positions:
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        side = pos["side"]
        current_price = (snaps.get(sym) or {}).get("price") or 0
        if not entry_price or not current_price:
            continue

        pct_move = (current_price - entry_price) / entry_price
        if side == "short":
            pct_move = -pct_move

        exit_reason = None
        if pct_move >= LOW_VALUE_TARGET_PCT:
            exit_reason = "TARGET"
        elif pct_move <= -LOW_VALUE_STOP_PCT:
            exit_reason = "STOP"
        elif pos.get("lv_thesis_type") in NEGATIVE_THESES:
            fresh_flags = (news_by_symbol.get(sym) or {}).get("flags", [])
            if "POSITIVE_CATALYST" in fresh_flags:
                exit_reason = "THESIS_RESOLVED"
        if exit_reason is None:
            days_held = _trading_days_elapsed(pos["entry_time"], now_et)
            if days_held >= LOW_VALUE_HOLD_DAYS:
                exit_reason = "TIME"

        if exit_reason:
            result = log_trade_exit(pos["id"], current_price, exit_reason)
            closed.append({"symbol": sym, "trade_id": pos["id"], "exit_reason": exit_reason, **result})
            _run_log.append({
                "ts": now_et.isoformat(), "event": "EXIT", "sym": sym,
                "reason": exit_reason, "tid": pos["id"],
            })
            log.info(f"[LOW_VALUE] Closed {sym} tid={pos['id']} reason={exit_reason}")

    return {"checked": len(open_positions), "closed": closed}


def _runner_loop() -> None:
    """Background thread body. Sleeps 60s between ticks; scans once per day in the 8:00-8:14 ET window."""
    global _runner_active
    today_scanned: Optional[str] = None
    log.info("[LOW_VALUE] Background loop started")

    while _runner_active:
        try:
            now_et = _et_now()
            today = now_et.strftime("%Y-%m-%d")
            if now_et.weekday() < 5 and _in_scan_window() and today_scanned != today:
                log.info(f"[LOW_VALUE] Daily scan for {today}")
                check_low_value_exits()
                ids = run_low_value_scan()
                today_scanned = today
                log.info(f"[LOW_VALUE] Scan complete — {len(ids)} trades logged")
        except Exception as exc:
            log.error(f"[LOW_VALUE] Loop error: {exc}")
        time.sleep(60)

    log.info("[LOW_VALUE] Background loop exited")


def start_runner() -> bool:
    """Start the background Low Value runner thread. Returns True if started fresh, False if already running."""
    global _runner_thread, _runner_active
    if _runner_active and _runner_thread and _runner_thread.is_alive():
        return False
    _runner_active = True
    _runner_thread = threading.Thread(target=_runner_loop, daemon=True, name="low-value-runner")
    _runner_thread.start()
    log.info("[LOW_VALUE] Started")
    return True


def stop_runner() -> None:
    """Signal the Low Value runner loop to stop on its next tick."""
    global _runner_active
    _runner_active = False
    log.info("[LOW_VALUE] Stop signalled")


def get_runner_status() -> dict:
    """Current Low Value runner state for GET /trade/low-value/runner/status."""
    open_pos = _load_open_positions()
    today_str = _et_now().strftime("%Y-%m-%d")
    return {
        "active":          _runner_active and bool(_runner_thread and _runner_thread.is_alive()),
        "scan_hour_et":    LOW_VALUE_SCAN_HOUR_ET,
        "max_positions":   LOW_VALUE_MAX_CONCURRENT_POSITIONS,
        "open_positions":  len(open_pos),
        "open_symbols":    [p["symbol"] for p in open_pos],
        "universe_cached_today": today_str in _universe_cache,
        "universe_size":   len(_universe_cache.get(today_str, [])),
        "et_now":          _et_now().isoformat(),
        "recent_log":      list(reversed(_run_log[-20:])),
    }
