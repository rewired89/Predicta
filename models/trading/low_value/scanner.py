"""
Low Value engine — universe scanner (Kimi review round 6 follow-up).

Builds the daily sub-$20 contrarian universe: all active US-equity Alpaca
symbols, filtered down to a liquid, non-distressed, non-earnings-blackout
50-200 symbol list. Runs once daily at 4:00 AM ET (fetchers/low_value_runner.py
schedules the call); results are cached in-memory for the day and logged to
low_value_universe_snapshot for after-the-fact auditability.

Independent of the High Value engine — shares no signal logic, only the
Alpaca market-data fetcher and (for the earnings-blackout list only) a
read-only import of a High Value constant.
"""
from __future__ import annotations
import logging
import time
from typing import Optional

from fetchers.alpaca import get_all_active_assets, get_snapshots, get_daily_bars
from fetchers.finnhub import get_market_cap as finnhub_market_cap
from fetchers.yahoo_quote import get_market_cap as yahoo_market_cap
from fetchers.sec_edgar import has_recent_bankruptcy_filing
from fetchers.high_value_runner import EARNINGS_BLACKOUT

log = logging.getLogger("low_value_scanner")

PRICE_CEILING: float = 20.0
MIN_AVG_DAILY_VOLUME_20D: int = 100_000
MIN_MARKET_CAP: float = 50_000_000.0
UNIVERSE_MIN_SIZE: int = 50
UNIVERSE_MAX_SIZE: int = 200
BANKRUPTCY_LOOKBACK_DAYS: int = 90

# Kimi review (2026-07-07 follow-up) — "volatility floor": excludes stagnant
# stocks with no recent price action before they ever reach the expensive
# Finnhub/SEC checks. Rationale: a flat, ignored stock has no fear to buy —
# the contrarian thesis needs a falling knife or a gapper, not a zombie.
# A candidate passes if EITHER condition holds (only fails both = excluded):
#   at least one of the last 5 daily bars moved >= 2% intraday (open->close), OR
#   the 5-day high-low range is >= 5% of the 5-day-ago open.
VOLATILITY_FLOOR_ENABLED: bool = True
VOLATILITY_FLOOR_MIN_DAY_MOVE_PCT: float = 0.02
VOLATILITY_FLOOR_MIN_5D_RANGE_PCT: float = 0.05

_SNAPSHOT_BATCH_SIZE = 200

# 2026-07-11 production issue: a scan ran for 5+ minutes with zero visibility
# into whether it was working or stuck — no time limit anywhere in the
# pipeline, and no progress logging at all. Both are fixed here. Root cause
# is still unconfirmed (most likely candidate: one of the newer data sources
# — Finnhub, SEC EDGAR, Nasdaq short-interest — being slow/rate-limited from
# Railway's datacenter IP, the exact same failure mode this codebase already
# hit with FanGraphs/Baseball Savant, documented in CLAUDE.md), but a hard
# time budget means a slow/throttled dependency degrades the scan instead of
# hanging it indefinitely, and the new log lines let us actually see where
# time goes on the next attempt.
SCAN_TIME_BUDGET_SEC: float = 240.0        # 4 minutes, hard ceiling on the whole build
PRICE_FILTER_TIME_BUDGET_SEC: float = 60.0  # 1 minute of the above, reserved for the price-filter pass


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _cheap_price_filter(symbols: list[str], deadline: float) -> list[str]:
    """
    First-pass filter: batch snapshots (Alpaca) for every active symbol,
    keep only last_close < PRICE_CEILING. Normally cheap — a handful of
    batched API calls regardless of universe size — but bails out at
    `deadline` (time.monotonic() timestamp) if Alpaca's snapshot endpoint is
    unexpectedly slow, returning whatever survived so far rather than
    hanging the whole scan.
    """
    survivors: list[str] = []
    batches = list(_batched(symbols, _SNAPSHOT_BATCH_SIZE))
    log.info(f"[LOW_VALUE_SCANNER] price filter: {len(symbols)} candidates, {len(batches)} batches")
    for i, chunk in enumerate(batches):
        if time.monotonic() > deadline:
            log.warning(f"[LOW_VALUE_SCANNER] price filter time budget exceeded at batch {i}/{len(batches)} — returning {len(survivors)} survivors so far")
            break
        snaps = get_snapshots(chunk)
        for sym, snap in snaps.items():
            price = snap.get("price") or 0
            if 0 < price < PRICE_CEILING:
                survivors.append(sym)
        if i % 10 == 0:
            log.info(f"[LOW_VALUE_SCANNER] price filter batch {i}/{len(batches)} done — {len(survivors)} survivors so far")
    log.info(f"[LOW_VALUE_SCANNER] price filter complete — {len(survivors)} symbols under ${PRICE_CEILING}")
    return survivors


def _avg_daily_volume_20d(daily_bars: list[dict]) -> float:
    """Takes already-fetched bars (see build_low_value_universe) rather than re-fetching — one Alpaca call per candidate, not two."""
    if not daily_bars:
        return 0.0
    vols = [b.get("v", 0) for b in daily_bars]
    return sum(vols) / len(vols) if vols else 0.0


def _has_meaningful_volatility(daily_bars: list[dict]) -> bool:
    """
    Volatility floor (Kimi review, 2026-07-07 follow-up): True if the last 5
    daily bars show at least one >=2% single-day move OR a 5-day range
    >=5% of the 5-day-ago open. False (excluded) only when BOTH conditions
    miss — a stock that's been genuinely flat all week, not one that just
    lacks a single dramatic day. Fails open (True — don't exclude) when
    there isn't enough history to judge, same "don't guess" convention as
    every other signal in this engine.
    """
    if not daily_bars or len(daily_bars) < 5:
        return True
    last5 = daily_bars[-5:]
    day_moves = [
        abs((b.get("c", 0) - b.get("o", 0)) / b["o"])
        for b in last5 if b.get("o")
    ]
    if day_moves and max(day_moves) > VOLATILITY_FLOOR_MIN_DAY_MOVE_PCT:
        return True
    open_5d_ago = last5[0].get("o")
    if not open_5d_ago:
        return True
    high_5d = max(b.get("h", 0) for b in last5)
    low_5d = min(b.get("l", 0) for b in last5 if b.get("l"))
    five_day_range_pct = (high_5d - low_5d) / open_5d_ago
    return five_day_range_pct > VOLATILITY_FLOOR_MIN_5D_RANGE_PCT


def _get_market_cap(symbol: str) -> Optional[float]:
    """Finnhub primary, Yahoo backup per spec ('market cap from Finnhub or Yahoo')."""
    cap = finnhub_market_cap(symbol)
    if cap is not None:
        return cap
    return yahoo_market_cap(symbol)


def _is_earnings_blackout(symbol: str, date_str: str) -> bool:
    return date_str in EARNINGS_BLACKOUT.get(symbol, [])


def build_low_value_universe(today_str: str, max_candidates: Optional[int] = None) -> tuple[list[str], dict]:
    """
    Full daily scan pipeline:
      1. All active, tradable, non-OTC US equities (Alpaca).
      2. Cheap price filter (batch snapshots) -> last_close < $20.
      3. Per-candidate: earnings-blackout (free, checked first), volatility
         floor (Kimi review 2026-07-07 follow-up — excludes stocks flat all
         week, before any paid API call), avg_daily_volume_20d > 100k
         (reuses the same daily bars the volatility check just fetched —
         one Alpaca call, not two), market_cap > $50M, no bankruptcy 8-K in
         last 90 days. Order minimizes wasted API calls on candidates that
         fail cheaper checks first.
    Returns (sorted list of 50-200 symbols, stats dict) — stats has
    stagnant_filtered_count (candidates excluded by the volatility floor),
    candidates_evaluated, elapsed_sec, and time_budget_exceeded (True if
    SCAN_TIME_BUDGET_SEC was hit before finishing the candidate pool — a
    partial, non-empty result in that case is expected and fine, not a bug).

    max_candidates caps how many price-filtered symbols get the expensive
    per-symbol checks — a safety valve against a very large Alpaca universe
    burning through Finnhub's/SEC's rate limits in one scan. SCAN_TIME_BUDGET_SEC
    is the second, independent safety valve — bounds wall-clock time even if
    max_candidates is generous, in case any one dependency is slow/throttled.
    """
    start = time.monotonic()
    log.info(f"[LOW_VALUE_SCANNER] build_low_value_universe starting for {today_str}")

    all_symbols = get_all_active_assets()
    log.info(f"[LOW_VALUE_SCANNER] {len(all_symbols)} active Alpaca assets")
    if not all_symbols:
        return [], {"stagnant_filtered_count": 0, "candidates_evaluated": 0, "elapsed_sec": 0, "time_budget_exceeded": False}

    price_filter_deadline = start + PRICE_FILTER_TIME_BUDGET_SEC
    price_ok = _cheap_price_filter(all_symbols, price_filter_deadline)
    if max_candidates:
        price_ok = price_ok[:max_candidates]

    scan_deadline = start + SCAN_TIME_BUDGET_SEC
    universe: list[str] = []
    stagnant_filtered_count = 0
    candidates_evaluated = 0
    time_budget_exceeded = False

    for sym in price_ok:
        if time.monotonic() > scan_deadline:
            time_budget_exceeded = True
            log.warning(
                f"[LOW_VALUE_SCANNER] time budget ({SCAN_TIME_BUDGET_SEC:.0f}s) exceeded after "
                f"{candidates_evaluated}/{len(price_ok)} candidates — returning {len(universe)} symbols found so far"
            )
            break

        if _is_earnings_blackout(sym, today_str):
            continue

        candidates_evaluated += 1
        if candidates_evaluated % 25 == 0:
            elapsed = time.monotonic() - start
            log.info(
                f"[LOW_VALUE_SCANNER] progress: {candidates_evaluated}/{len(price_ok)} candidates evaluated, "
                f"{len(universe)} passed, {elapsed:.0f}s elapsed"
            )

        daily_bars = get_daily_bars(sym, days=20)
        if not daily_bars:
            continue  # no bar data for this symbol (thin/new listing) — can't evaluate volatility or volume

        if VOLATILITY_FLOOR_ENABLED and not _has_meaningful_volatility(daily_bars):
            stagnant_filtered_count += 1
            continue
        if _avg_daily_volume_20d(daily_bars) <= MIN_AVG_DAILY_VOLUME_20D:
            continue
        cap = _get_market_cap(sym)
        if cap is None or cap <= MIN_MARKET_CAP:
            continue
        if has_recent_bankruptcy_filing(sym, days=BANKRUPTCY_LOOKBACK_DAYS):
            continue
        universe.append(sym)
        if len(universe) >= UNIVERSE_MAX_SIZE:
            break

    elapsed_sec = round(time.monotonic() - start, 1)
    log.info(
        f"[LOW_VALUE_SCANNER] build_low_value_universe complete — {len(universe)} symbols, "
        f"{candidates_evaluated} evaluated, {stagnant_filtered_count} stagnant, {elapsed_sec}s elapsed"
    )
    return sorted(universe), {
        "stagnant_filtered_count": stagnant_filtered_count,
        "candidates_evaluated": candidates_evaluated,
        "elapsed_sec": elapsed_sec,
        "time_budget_exceeded": time_budget_exceeded,
    }
