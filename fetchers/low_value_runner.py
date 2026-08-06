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
from fetchers.alpaca import get_daily_bars, get_snapshots, get_asset_shortability
from fetchers.trading_logger import log_low_value_trade, log_trade_exit, log_universe_snapshot, promote_trade_to_real
from fetchers import hsip_client
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

# Data-collection sprint mode (2026-07-12, same pattern and rationale as
# fetchers/high_value_runner.py's DATA_COLLECTION_SPRINT_MODE): at the real
# ENTRY_THRESHOLD (40, thesis_tracker.py), a live production scan found only
# 2 candidate symbols out of 500 evaluated even before scoring — at that
# rate, reaching the 20/50/100-closed-trade calibration tiers
# (signal_calibration.py) this engine needs to be tunable at all could take
# months of mostly-empty days. Lossless and reversible: log_low_value_trade
# always stores the real composite score regardless of which bar let it in,
# so post-hoc analysis can always re-filter to |score|>=40 even with sprint
# mode on. Flip LOW_VALUE_DATA_COLLECTION_SPRINT_MODE back to False to
# return to the strict bar once there's enough volume to tune against.
# Does NOT change thesis_tracker.ENTRY_THRESHOLD itself (that still drives
# label_for_score's BUY/SELL/STRONG_BUY/STRONG_SELL text and entry_eligible)
# — only how low a score run_low_value_scan() is willing to log a trade at.
LOW_VALUE_DATA_COLLECTION_SPRINT_MODE: bool = True
LOW_VALUE_SPRINT_MIN_SCORE: float = 15.0

# NYSE market holidays (Kimi review, round-2 follow-up — the prior weekday-only
# approximation held a Thursday-before-a-3-day-weekend entry for 7 calendar
# days instead of 5 trading days). Real dates, same convention as
# MACRO_EVENT_DATES in high_value_runner.py — extend yearly as needed.
US_MARKET_HOLIDAYS: set[str] = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

_runner_thread: Optional[threading.Thread] = None
_runner_active: bool = False
_run_log: list[dict] = []
_universe_cache: dict[str, list[str]] = {}   # {date_str: [symbols]}

# Serializes the actual build_low_value_universe() call inside
# get_daily_universe() (see 2026-07-12 comment there) — a scan-now trigger
# and a universe-refresh trigger both ultimately call get_daily_universe(),
# and without this lock a race between them ran two full, independent
# universe builds at once.
_build_lock = threading.Lock()

# 2026-07-07 production bug: get_daily_universe() called build_low_value_universe()
# with no max_candidates, so EVERY US equity under $20 (realistically several
# thousand, not a couple hundred) got the expensive per-symbol Alpaca/Finnhub/SEC
# checks sequentially — nowhere near the "~200 stocks, 2-10 min" estimate the UI
# quoted. 500 keeps a real day's scan bounded and predictable while still leaving
# enough headroom to reach the 50-200 target universe size after all the filters.
UNIVERSE_SCAN_MAX_CANDIDATES: int = 500

_scan_lock = threading.Lock()
_scan_in_progress: bool = False
_scan_thread: Optional[threading.Thread] = None
_last_scan_started_at: Optional[str] = None
_last_scan_completed_at: Optional[str] = None
_last_scan_trade_ids: list[int] = []
_last_scan_error: Optional[str] = None

# Same fire-and-forget pattern as the scan state above, for
# analyze_low_value_tickers (photo-upload / pasted-ticker-list analysis) —
# a real watchlist photo can have 20-50 tickers, each needing 2 Alpaca
# calls, which is the exact same "blocks the HTTP request for minutes"
# problem the scan trigger was fixed for on 2026-07-07. A user reported the
# Analyze button's loading message just sitting there forever with no
# result ever appearing — this is that bug, fixed the same way.
_analysis_lock = threading.Lock()
_analysis_in_progress: bool = False
_analysis_thread: Optional[threading.Thread] = None
_last_analysis_started_at: Optional[str] = None
_last_analysis_completed_at: Optional[str] = None
_last_analysis_tickers_found: list[str] = []
_last_analysis_results: list[dict] = []
_last_analysis_error: Optional[str] = None
MAX_ANALYSIS_SECONDS: float = 300.0

# Same fire-and-forget treatment for a universe-only refresh (GET
# /trade/low-value/universe?force_refresh=true) — building the universe
# alone is the same expensive operation as the first half of a scan, so it
# gets the same "never block an HTTP request" fix.
_universe_lock = threading.Lock()
_universe_build_in_progress: bool = False
_last_universe_build_started_at: Optional[str] = None
_last_universe_build_error: Optional[str] = None

# 2026-07-12 production report: universe_build_in_progress observed stuck
# True for 10+ minutes with universe_size still 0 and no error anywhere —
# scanner.py's own SCAN_TIME_BUDGET_SEC/PRICE_FILTER_TIME_BUDGET_SEC
# deadlines are only checked *between* loop iterations, so one slow/hanging
# call inside a single iteration (or any future dependency change that
# reintroduces an unbounded wait) can still starve them indefinitely — the
# internal budget alone isn't a real ceiling. This is a second, outer
# watchdog: it can't force-kill a genuinely hung thread (Python has no API
# for that), but it stops the *observable* state from lying forever — once
# a build has claimed to be "in progress" longer than this, both the
# trigger endpoint and the status/dashboard endpoints treat it as failed,
# clear the flag, and surface an error instead of silence, so a fresh
# attempt is actually allowed to run. Set well above the scanner's own
# ~300s worst case (240s scan + 60s price-filter budgets) to leave slack
# for one in-flight call finishing plus the DB write.
MAX_UNIVERSE_BUILD_SECONDS: float = 360.0


def _seconds_since(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        started = datetime.fromisoformat(iso_ts)
        return (datetime.now(started.tzinfo) - started).total_seconds()
    except Exception:
        return None


def _clear_stale_universe_build() -> None:
    """Unsticks _universe_build_in_progress if it's been true for longer than any legitimate build could take — see MAX_UNIVERSE_BUILD_SECONDS above."""
    global _universe_build_in_progress, _last_universe_build_error
    if not _universe_build_in_progress:
        return
    elapsed = _seconds_since(_last_universe_build_started_at)
    if elapsed is not None and elapsed > MAX_UNIVERSE_BUILD_SECONDS:
        log.error(
            f"[LOW_VALUE] Universe build watchdog: still 'in progress' after {elapsed:.0f}s "
            f"(budget {MAX_UNIVERSE_BUILD_SECONDS:.0f}s) — treating as hung/failed, clearing the flag"
        )
        _universe_build_in_progress = False
        _last_universe_build_error = (
            f"Watchdog: build exceeded {MAX_UNIVERSE_BUILD_SECONDS:.0f}s without completing "
            "(a dependency call likely hung past its own timeout) — treated as failed, safe to retry"
        )


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
    Today's Low Value universe, cached in-memory for the day AND persisted
    to the DB (see the 2026-07-14 note below) so a same-day rebuild can't
    happen just because the process restarted. Logs a snapshot (including
    the volatility-floor stagnant_filtered_count, Kimi review 2026-07-07
    follow-up) to low_value_universe_snapshot on every fresh scan (not on
    cache hits or DB-snapshot reuse).
    """
    today_str = _et_now().strftime("%Y-%m-%d")
    if not force_refresh and today_str in _universe_cache:
        return _universe_cache[today_str]

    # 2026-07-12 production report: a manual scan-now and a manual
    # universe-refresh landed ~90s apart while the first was still running —
    # trigger_scan_async's run_low_value_scan() calls this with
    # force_refresh=False (cache miss -> build), and trigger_universe_refresh_async
    # calls it with force_refresh=True independently. Neither path coordinated
    # with the other, so both ran their own full build_low_value_universe()
    # pass concurrently — doubling load on Finnhub's 60-calls/min ceiling at
    # exactly the moment a clean scan mattered most. _build_lock serializes
    # the actual build; the re-check after acquiring it means a second caller
    # that only wanted "today's universe, cache is fine" (force_refresh=False)
    # picks up the first caller's now-fresh result instead of redoing the
    # work. An explicit force_refresh=True caller still gets a real rebuild
    # (respecting the "refresh" intent), just serialized rather than parallel.
    with _build_lock:
        if not force_refresh and today_str in _universe_cache:
            return _universe_cache[today_str]

        # 2026-07-14 production report: a user ran two manual scans ~20
        # minutes apart, same calendar day, market closed, and got a
        # different set of stocks each time — looked like "the model
        # rescans and changes its mind for no reason." Real cause: this
        # repo's Railway deployment redeploys (restarting the whole
        # process) far more often than once a day, and _universe_cache is
        # an in-memory dict — a redeploy between the two scans silently
        # wiped it, so the second scan saw an empty cache and built a
        # genuinely new universe from scratch, even though it was still
        # "today" in ET. log_universe_snapshot() already writes every real
        # build to the DB — check there first (DB-durable, survives a
        # restart) before paying for a whole new build_low_value_universe()
        # pass just because the in-memory cache happens to be cold.
        if not force_refresh:
            from fetchers.trading_logger import get_universe_snapshot_for_date
            snap = get_universe_snapshot_for_date(today_str)
            if snap and snap.get("symbols_json"):
                import json as _json
                try:
                    universe = _json.loads(snap["symbols_json"])
                except (TypeError, ValueError):
                    universe = None
                if universe is not None:
                    _universe_cache.clear()
                    _universe_cache[today_str] = universe
                    log.info(
                        f"[LOW_VALUE] Reusing DB-persisted universe snapshot for {today_str} "
                        f"({len(universe)} symbols) — in-memory cache was cold, likely a recent restart"
                    )
                    return universe

        universe, stats = build_low_value_universe(today_str, max_candidates=UNIVERSE_SCAN_MAX_CANDIDATES)
        _universe_cache.clear()   # only ever keep today's entry
        _universe_cache[today_str] = universe
        log_universe_snapshot(today_str, universe, filter_stats=stats)
        log.info(
            f"[LOW_VALUE] Universe scan for {today_str} — {len(universe)} symbols "
            f"({stats.get('stagnant_filtered_count', 0)} excluded as stagnant, "
            f"{stats.get('candidates_evaluated', 0)} evaluated, {stats.get('elapsed_sec', 0)}s elapsed"
            f"{', TIME BUDGET EXCEEDED' if stats.get('time_budget_exceeded') else ''})"
        )
        return universe


def _universe_build_worker() -> None:
    """Background-thread body for trigger_universe_refresh_async — calls the blocking get_daily_universe safely off the request thread."""
    global _universe_build_in_progress, _last_universe_build_error
    try:
        get_daily_universe(force_refresh=True)
    except Exception as exc:
        _last_universe_build_error = str(exc)
        log.error(f"[LOW_VALUE] Async universe build failed: {exc}")
    finally:
        _universe_build_in_progress = False


def trigger_universe_refresh_async() -> dict:
    """
    Fire-and-forget universe rebuild (2026-07-07 production fix — same class
    of bug as trigger_scan_async, for GET /trade/low-value/universe?force_refresh=true
    specifically). Starts the build in a background thread and returns
    immediately; poll get_runner_status() or re-GET /trade/low-value/universe
    without force_refresh for the cached result once it lands.
    """
    global _universe_build_in_progress, _last_universe_build_started_at, _last_universe_build_error
    with _universe_lock:
        _clear_stale_universe_build()
        if _universe_build_in_progress:
            return {"status": "already_running", "started_at": _last_universe_build_started_at}
        _universe_build_in_progress = True
        _last_universe_build_started_at = _et_now().isoformat()
        _last_universe_build_error = None
        threading.Thread(target=_universe_build_worker, daemon=True, name="low-value-universe-build").start()
    return {"status": "started", "started_at": _last_universe_build_started_at}


def _load_open_positions() -> list[dict]:
    """
    All open Low Value positions, hypothetical AND real — a real one
    (alpaca_order_id set) only ever exists because a human clicked Buy via
    execute_low_value_trade, never the autonomous scan below. Both kinds
    share the same daily exit check (check_low_value_exits): the exit rule
    itself (target/stop/time/thesis) is identical either way, only whether
    a real closing order also needs to be placed differs.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, entry_price, entry_time, lv_thesis_type,
                   qty, alpaca_order_id
            FROM intraday_trades
            WHERE engine = 'low_value' AND exit_time IS NULL
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def analyze_low_value_tickers(symbols: list[str]) -> list[dict]:
    """
    Pure, read-only Low Value analysis for an explicit ticker list — e.g.
    tickers extracted from an uploaded watchlist photo (fetchers.ticker_vision).
    Computes the real 8-signal composite (thesis_tracker.compute_thesis_score)
    for every symbol given, regardless of whether it crosses ENTRY_THRESHOLD.

    Deliberately never writes a hypothetical trade row — unlike
    run_low_value_scan, this is a lookup a human asked for, not the daily
    scan, so it must not pollute calibration data with speculative checks
    on tickers that were never actually part of a real scan cycle.

    low_value_universe_eligible flags whether the symbol is even in Low
    Value's actual scope (sub-$20) — the composite score is still computed
    and returned either way, but a mega-cap or ETF ticker's score should be
    read as "what the formula outputs if you feed it this," not "a real Low
    Value signal," since this engine's own daily scanner would never have
    considered it in the first place.
    """
    if not symbols:
        return []
    symbols = sorted({s.strip().upper() for s in symbols if s.strip()})
    news_by_symbol = scan_universe_news(symbols, days=7)
    results = []
    for sym in symbols:
        try:
            daily_bars = get_daily_bars(sym, days=25)
            if not daily_bars:
                results.append({
                    "symbol": sym,
                    "error": "No price data found — may not be a valid US-listed equity ticker, or Alpaca has no data for it.",
                })
                continue
            current_price = daily_bars[-1].get("c")
            sector_etf = sector_etf_for_symbol(sym)
            sector_bars = get_daily_bars(sector_etf, days=25)
            news_result = news_by_symbol.get(sym)
            thesis = compute_thesis_score(sym, daily_bars, sector_bars, news_result)
            composite = thesis.get("composite")
            results.append({
                "symbol": sym,
                "current_price": current_price,
                "low_value_universe_eligible": bool(current_price and current_price < 20),
                "composite_score": composite,
                "label": thesis.get("label"),
                "entry_eligible": thesis.get("entry_eligible"),
                "thesis_type": dominant_thesis_type(thesis, news_result) if composite is not None else None,
                "signals": thesis.get("signals", {}),
                "missing_signals": thesis.get("missing_signals", []),
                "news": {
                    "flags": (news_result or {}).get("flags", []),
                    "sentiment": (news_result or {}).get("sentiment"),
                    "headline_count": (news_result or {}).get("headline_count"),
                },
            })
        except Exception as exc:
            results.append({"symbol": sym, "error": str(exc)})
    return results


def _clear_stale_analysis() -> None:
    global _analysis_in_progress, _last_analysis_error, _last_analysis_completed_at
    if not _analysis_in_progress:
        return
    elapsed = _seconds_since(_last_analysis_started_at)
    if elapsed is not None and elapsed > MAX_ANALYSIS_SECONDS:
        log.error(
            f"[LOW_VALUE] Analysis watchdog: still 'in progress' after {elapsed:.0f}s "
            f"(budget {MAX_ANALYSIS_SECONDS:.0f}s) — treating as hung/failed, clearing the flag"
        )
        _analysis_in_progress = False
        _last_analysis_completed_at = _et_now().isoformat()
        _last_analysis_error = (
            f"Watchdog: analysis exceeded {MAX_ANALYSIS_SECONDS:.0f}s without completing "
            "(a dependency call likely hung past its own timeout) — treated as failed, safe to retry"
        )


def _analysis_worker(tickers: list[str]) -> None:
    """Background-thread body for trigger_ticker_analysis_async — never runs inside an HTTP request."""
    global _analysis_in_progress, _last_analysis_completed_at, _last_analysis_results, _last_analysis_error
    _last_analysis_error = None
    try:
        _last_analysis_results = analyze_low_value_tickers(tickers)
    except Exception as exc:
        _last_analysis_error = str(exc)
        log.error(f"[LOW_VALUE] Async ticker analysis failed: {exc}")
    finally:
        _last_analysis_completed_at = _et_now().isoformat()
        _analysis_in_progress = False


def trigger_ticker_analysis_async(tickers: list[str]) -> dict:
    """
    Fire-and-forget analyze_low_value_tickers trigger (same reasoning as
    trigger_scan_async above) — starts the real per-ticker analysis in a
    background thread and returns immediately. Poll get_analysis_status()
    for in_progress / results. Returns {"status": "started"} or
    {"status": "already_running"} if a previous analysis hasn't finished yet.
    """
    global _analysis_in_progress, _analysis_thread, _last_analysis_started_at, _last_analysis_tickers_found
    with _analysis_lock:
        _clear_stale_analysis()
        if _analysis_in_progress:
            return {"status": "already_running", "started_at": _last_analysis_started_at}
        _analysis_in_progress = True
        _last_analysis_started_at = _et_now().isoformat()
        _last_analysis_tickers_found = tickers
        _analysis_thread = threading.Thread(target=_analysis_worker, args=(tickers,), daemon=True, name="low-value-ticker-analysis")
        _analysis_thread.start()
    return {"status": "started", "started_at": _last_analysis_started_at}


def get_analysis_status() -> dict:
    """Current state of the most recent ticker/photo analysis — polled by the Analyze button's JS."""
    _clear_stale_analysis()
    return {
        "in_progress":   _analysis_in_progress,
        "started_at":    _last_analysis_started_at,
        "completed_at":  _last_analysis_completed_at,
        "tickers_found": _last_analysis_tickers_found,
        "results":       _last_analysis_results,
        "error":         _last_analysis_error,
    }


def run_low_value_scan(symbols: Optional[list[str]] = None) -> list[int]:
    """
    Scans the daily universe (or an explicit override list), logs any
    |composite| >= the effective logging bar as a hypothetical trade, capped
    at LOW_VALUE_MAX_CONCURRENT_POSITIONS total open positions. Returns the
    list of trade_ids created.

    The effective bar is thesis_tracker.ENTRY_THRESHOLD (40) normally, or
    LOW_VALUE_SPRINT_MIN_SCORE (15) while LOW_VALUE_DATA_COLLECTION_SPRINT_MODE
    is on — see that constant's comment above for why. entry_score is stored
    on every logged trade either way, so which bar let a given trade in is
    always reconstructable after the fact.
    """
    syms = symbols if symbols is not None else get_daily_universe()
    if not syms:
        return []

    open_positions = _load_open_positions()
    existing_open = len(open_positions)
    # 2026-07-14: nothing previously stopped run_low_value_scan from
    # re-evaluating and re-logging a BRAND NEW trade for a symbol that
    # already has an open Low Value position — the loop only tracked a
    # total count against LOW_VALUE_MAX_CONCURRENT_POSITIONS, never which
    # specific symbols were already held. Running the scan twice in the
    # same day (nothing about the universe or the open positions has
    # changed) could double up on the same symbol instead of leaving it
    # alone until it actually closes.
    already_open_symbols = {p["symbol"] for p in open_positions}
    news_by_symbol = scan_universe_news(syms)
    trade_ids: list[int] = []
    min_score = LOW_VALUE_SPRINT_MIN_SCORE if LOW_VALUE_DATA_COLLECTION_SPRINT_MODE else ENTRY_THRESHOLD

    for sym in syms:
        if sym in already_open_symbols:
            continue
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
            if composite is None or abs(composite) < min_score:
                continue

            side = "long" if composite > 0 else "short"
            entry_price = daily_bars[-1].get("c")
            if not entry_price or entry_price <= 0:
                continue

            # Borrow-availability gate (2026-07-17, Tier 2 of the trading-model
            # audit) — sub-$20 micro-caps are exactly the kind of stock that's
            # frequently not shortable at all. Unlike a signal weight, this is
            # a hard tradability fact (Alpaca either will or won't let the
            # short execute), not a guessed threshold, so it's a real skip —
            # not logged as a hypothetical trade, same as the existing
            # portfolio-cap and already-open-symbol skips below. shortable is
            # None (unknown — API error or missing field) is NOT blocked here,
            # only an explicit False — an unknown answer isn't evidence the
            # trade is impossible, only that Alpaca didn't confirm it either way.
            if side == "short":
                shortability = get_asset_shortability(sym)
                if shortability.get("shortable") is False:
                    _run_log.append({
                        "ts": _et_now().isoformat(), "event": "SHORT_BLOCKED_NOT_SHORTABLE", "sym": sym,
                        "note": f"Composite {composite} signaled SHORT but Alpaca reports {sym} is not shortable — skipped, not logged as a hypothetical trade.",
                    })
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
    """
    Trading-day count since entry — skips weekends AND US_MARKET_HOLIDAYS
    (Kimi review, round-2 follow-up; previously weekday-only, which held a
    Thursday-before-a-3-day-weekend entry for 7 calendar days instead of 5
    trading days). Falls back to counting only known holiday years correctly;
    a date past the last populated year just isn't skipped as a holiday.
    """
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
        if cursor.weekday() < 5 and cursor.isoformat() not in US_MARKET_HOLIDAYS:
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
            # A real (human-bought) position also needs the actual Alpaca
            # position liquidated — this executes the exit rule the human
            # already agreed to at buy time (target/stop/time/thesis), it
            # does not decide a new trade. A hypothetical candidate has
            # nothing to liquidate and behaves exactly as before.
            if pos.get("alpaca_order_id"):
                from fetchers.alpaca import close_position
                liq = close_position(sym)
                if "error" in liq:
                    log.warning(f"[LOW_VALUE] Real close failed for {sym} tid={pos['id']}: {liq['error']}")
                    continue

            result = log_trade_exit(pos["id"], current_price, exit_reason)
            closed.append({"symbol": sym, "trade_id": pos["id"], "exit_reason": exit_reason, **result})
            _run_log.append({
                "ts": now_et.isoformat(), "event": "EXIT", "sym": sym,
                "reason": exit_reason, "tid": pos["id"],
            })
            log.info(f"[LOW_VALUE] Closed {sym} tid={pos['id']} reason={exit_reason}")

            if pos.get("alpaca_order_id"):
                alpaca_side = "buy" if side == "long" else "sell"
                close_side = "sell" if alpaca_side == "buy" else "buy"
                hsip_client.attest_transaction(
                    decision_type = close_side,
                    strategy_id   = "low_value_swing",
                    model_version = "v1",
                    payload = {
                        "predicta_trade_id": pos["id"],
                        "alpaca_order_id":   pos["alpaca_order_id"],
                        "symbol":            sym,
                        "side":              close_side,
                        "qty":               pos.get("qty"),
                        "exit_price":        current_price,
                        "exit_reason":       exit_reason,
                        "closed_at":         now_et.isoformat(),
                    },
                )

    return {"checked": len(open_positions), "closed": closed}


# ── Human-triggered manual execution ────────────────────────────────────────
#
# The autonomous scan (run_low_value_scan) only ever logs a hypothetical
# candidate. These two functions are the ONLY place in this file that place
# or close a real order, and only ever run from an explicit human click
# (POST /trade/low-value/execute/:id and /trade/low-value/close/:id in
# app.py). Predicta analyzes and suggests; the human decides to buy or sell.

def execute_low_value_trade(trade_id: int) -> dict:
    """
    Places a real Alpaca paper order for an existing hypothetical Low Value
    candidate. Unlike High Value, this is a plain market order, not a
    bracket — Low Value's fixed $25/trade sizing produces fractional share
    counts (models.trading.shared.kelly.low_value_position_size), and
    Alpaca's bracket order type does not support fractional quantities.
    Fractional quantities also cannot be shorted on Alpaca — a SHORT
    candidate's qty is floored to a whole share and rejected outright if
    that rounds to zero, rather than silently placing a different-sized
    order than what the human saw.

    Fixed 2026-08-06 (real bug, live-confirmed): the LONG/buy path submitted
    the raw fractional qty unconditionally, but Alpaca only accepts
    fractional orders for assets flagged `fractionable: true` on their own
    asset record — most of Low Value's sub-$20, thinly-covered universe is
    NOT on that list. A user reported one BUY click working and the next
    two silently doing nothing; root cause was Alpaca rejecting the
    fractional order for those symbols with no clear error surfaced. Now
    checks get_asset_fractionability first and floors to whole shares for a
    non-fractionable symbol — same "floor and reject if it rounds to 0"
    pattern already used for shorts, just gated on the real per-symbol flag
    instead of assuming every buy can be fractional.

    Tightened same day, same user report (a THIRD candidate still failed
    after the first fix — two of three worked): the original gate only
    floored on a CONFIRMED `fractionable: false`. If get_asset_fractionability
    itself failed/timed out for a given symbol (returns "unknown"), the
    fractional qty went out anyway — reintroducing the exact same bug for
    that one symbol. A whole-share quantity is valid on Alpaca for every
    asset regardless of its fractionable flag, so "unknown" now floors too;
    only a POSITIVELY CONFIRMED `fractionable: true` still uses the raw
    fractional qty. Fails toward the safe side instead of the risky one.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, side, entry_price, qty
            FROM intraday_trades
            WHERE id = ? AND engine = 'low_value' AND is_hypothetical = 1 AND exit_time IS NULL
            """,
            (trade_id,),
        ).fetchone()
    if not row:
        return {"error": "Candidate not found, already executed, or already closed."}

    pos = dict(row)
    symbol = pos["symbol"]
    qty = pos["qty"]
    if not qty or qty <= 0:
        return {"error": "This candidate has no valid position size and cannot be executed."}

    alpaca_side = "buy" if pos["side"] == "long" else "sell"
    from fetchers.alpaca import place_order, get_asset_fractionability, friendly_order_error

    if pos["side"] == "short":
        qty = float(int(qty))  # floor to whole shares — fractional shorting isn't supported by Alpaca
        if qty < 1:
            return {"error": f"Position size ({pos['qty']} shares) rounds to 0 whole shares — too small to short."}
    elif qty != int(qty):
        # fractional long — only valid on Alpaca if this specific symbol is
        # POSITIVELY CONFIRMED fractionable. A whole-share quantity is
        # always valid on Alpaca regardless of fractionability, so "unknown"
        # (get_asset_fractionability failed/timed out for this symbol) now
        # floors too, instead of falling through to the original fractional
        # qty — that fallback was the reason a second symbol could still hit
        # the exact same silent rejection this fix was meant to close: if
        # the fractionability lookup itself failed, the old `is False` check
        # never fired and the raw fractional order went out anyway.
        frac = get_asset_fractionability(symbol)
        if frac.get("fractionable") is not True:
            qty = float(int(qty))
            if qty < 1:
                return {
                    "error": (
                        f"{symbol} doesn't support fractional-share orders on Alpaca, and "
                        f"${pos['entry_price'] * pos['qty']:,.2f} isn't enough for 1 whole share "
                        f"at ${pos['entry_price']:,.2f} — too small to buy a whole share."
                    )
                }

    order_result = place_order(
        symbol         = symbol,
        qty            = qty,
        side           = alpaca_side,
        order_type     = "market",
        time_in_force  = "day",
    )
    if "error" in order_result:
        return {"error": friendly_order_error(order_result)}

    alpaca_order_id = order_result.get("id")
    promote_trade_to_real(trade_id, alpaca_order_id)
    log.info(f"[LOW_VALUE MANUAL] Human executed {alpaca_side.upper()} {symbol} tid={trade_id} order_id={alpaca_order_id}")

    hsip_client.attest_transaction(
        decision_type = alpaca_side,
        strategy_id   = "low_value_swing",
        model_version = "v1",
        payload = {
            "predicta_trade_id": trade_id,
            "alpaca_order_id":   alpaca_order_id,
            "symbol":            symbol,
            "side":              alpaca_side,
            "qty":               qty,
            "entry_price":       pos.get("entry_price"),
            "placed_at":         _et_now().isoformat(),
        },
    )
    return {
        "status":            "SUBMITTED",
        "predicta_trade_id": trade_id,
        "alpaca_order_id":   alpaca_order_id,
        "symbol":            symbol,
        "side":              alpaca_side,
        "qty":               qty,
    }


def close_low_value_trade(trade_id: int) -> dict:
    """Human clicked Sell on a real, still-open Low Value position — liquidates it at Alpaca, records the close, attests it."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, symbol, side, qty, alpaca_order_id
            FROM intraday_trades
            WHERE id = ? AND engine = 'low_value' AND is_hypothetical = 0 AND exit_time IS NULL
            """,
            (trade_id,),
        ).fetchone()
    if not row:
        return {"error": "Real open position not found, or already closed."}

    pos = dict(row)
    symbol = pos["symbol"]
    snap = get_snapshots([symbol]).get(symbol, {})
    current_price = snap.get("price", 0)
    if current_price <= 0:
        return {"error": f"Could not get a current price for {symbol} — try again."}

    from fetchers.alpaca import close_position
    result = close_position(symbol)
    if "error" in result:
        if result.get("status_code") == 404:
            return {"error": f"Alpaca has no open position for {symbol} yet — your buy order likely hasn't filled yet (fractional orders like this can queue outside regular market hours). Check Orders in your Alpaca paper dashboard, then try Sell again once it shows filled."}
        return {"error": result["error"]}

    exit_result = log_trade_exit(trade_id, current_price, "MANUAL_CLOSE")
    alpaca_side = "buy" if pos["side"] == "long" else "sell"
    close_side  = "sell" if alpaca_side == "buy" else "buy"
    hsip_client.attest_transaction(
        decision_type = close_side,
        strategy_id   = "low_value_swing",
        model_version = "v1",
        payload = {
            "predicta_trade_id": trade_id,
            "alpaca_order_id":   pos.get("alpaca_order_id"),
            "symbol":            symbol,
            "side":              close_side,
            "qty":               pos.get("qty"),
            "exit_price":        current_price,
            "exit_reason":       "MANUAL_CLOSE",
            "closed_at":         _et_now().isoformat(),
        },
    )
    log.info(f"[LOW_VALUE MANUAL] Human closed {symbol} tid={trade_id} exit={current_price}")
    return {
        "status":            "CLOSED",
        "predicta_trade_id": trade_id,
        "symbol":            symbol,
        "exit_price":        current_price,
        **exit_result,
    }


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


def _scan_worker(symbols: Optional[list[str]]) -> None:
    """Background-thread body for trigger_scan_async — never runs inside an HTTP request."""
    global _scan_in_progress, _last_scan_completed_at, _last_scan_trade_ids, _last_scan_error
    _last_scan_error = None
    try:
        ids = run_low_value_scan(symbols=symbols)
        _last_scan_trade_ids = ids
    except Exception as exc:
        _last_scan_error = str(exc)
        log.error(f"[LOW_VALUE] Async scan failed: {exc}")
    finally:
        _last_scan_completed_at = _et_now().isoformat()
        _scan_in_progress = False


def trigger_scan_async(symbols: Optional[list[str]] = None) -> dict:
    """
    Fire-and-forget scan trigger (2026-07-07 production fix) — a manual scan
    can legitimately take several minutes (hundreds of sequential Alpaca/
    Finnhub/SEC calls), and any code that blocks an HTTP request for that
    long is at the mercy of Railway's/the browser's own timeout, which is
    exactly what silently killed the query-box scan with a blank error.
    Starts the scan in a background thread and returns immediately;
    poll get_runner_status() for scan_in_progress / last_scan_completed_at.
    Returns {"status": "started"} or {"status": "already_running"} if a
    scan is already in flight (prevents duplicate concurrent scans from a
    double-click or the scheduled 8 AM tick overlapping a manual trigger).
    """
    global _scan_in_progress, _scan_thread, _last_scan_started_at
    with _scan_lock:
        _clear_stale_scan()
        if _scan_in_progress:
            return {"status": "already_running", "started_at": _last_scan_started_at}
        _scan_in_progress = True
        _last_scan_started_at = _et_now().isoformat()
        _scan_thread = threading.Thread(target=_scan_worker, args=(symbols,), daemon=True, name="low-value-scan")
        _scan_thread.start()
    return {"status": "started", "started_at": _last_scan_started_at}


# Same outer-watchdog treatment as _clear_stale_universe_build (see comment
# there) for the scan trigger — run_low_value_scan() has no internal time
# budget of its own at all (unlike build_low_value_universe), so it's even
# more exposed to the same "in_progress stuck true forever" failure mode.
# Generous ceiling: a full ~200-symbol universe, two Alpaca daily-bar calls
# each plus a news lookup, all at a 10s per-call timeout.
MAX_SCAN_SECONDS: float = 900.0


def _clear_stale_scan() -> None:
    global _scan_in_progress, _last_scan_error, _last_scan_completed_at
    if not _scan_in_progress:
        return
    elapsed = _seconds_since(_last_scan_started_at)
    if elapsed is not None and elapsed > MAX_SCAN_SECONDS:
        log.error(
            f"[LOW_VALUE] Scan watchdog: still 'in progress' after {elapsed:.0f}s "
            f"(budget {MAX_SCAN_SECONDS:.0f}s) — treating as hung/failed, clearing the flag"
        )
        _scan_in_progress = False
        _last_scan_completed_at = _et_now().isoformat()
        _last_scan_error = (
            f"Watchdog: scan exceeded {MAX_SCAN_SECONDS:.0f}s without completing "
            "(a dependency call likely hung past its own timeout) — treated as failed, safe to retry"
        )


def get_runner_status() -> dict:
    """Current Low Value runner state for GET /trade/low-value/runner/status."""
    _clear_stale_universe_build()
    _clear_stale_scan()
    open_pos = _load_open_positions()
    today_str = _et_now().strftime("%Y-%m-%d")
    return {
        "active":          _runner_active and bool(_runner_thread and _runner_thread.is_alive()),
        "scan_hour_et":    LOW_VALUE_SCAN_HOUR_ET,
        "max_positions":   LOW_VALUE_MAX_CONCURRENT_POSITIONS,
        "entry_threshold": ENTRY_THRESHOLD,
        "data_collection_sprint_mode": LOW_VALUE_DATA_COLLECTION_SPRINT_MODE,
        "sprint_min_score": LOW_VALUE_SPRINT_MIN_SCORE if LOW_VALUE_DATA_COLLECTION_SPRINT_MODE else None,
        "open_positions":  len(open_pos),
        "open_symbols":    [p["symbol"] for p in open_pos],
        "universe_cached_today": today_str in _universe_cache,
        "universe_size":   len(_universe_cache.get(today_str, [])),
        "et_now":          _et_now().isoformat(),
        "recent_log":      list(reversed(_run_log[-20:])),
        "scan_in_progress":       _scan_in_progress,
        "last_scan_started_at":   _last_scan_started_at,
        "last_scan_completed_at": _last_scan_completed_at,
        "last_scan_trade_ids":    _last_scan_trade_ids,
        "last_scan_error":        _last_scan_error,
        "universe_build_in_progress":      _universe_build_in_progress,
        "last_universe_build_started_at":  _last_universe_build_started_at,
        "last_universe_build_error":       _last_universe_build_error,
    }
