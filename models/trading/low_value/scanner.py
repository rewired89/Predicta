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
from typing import Optional

from fetchers.alpaca import get_all_active_assets, get_snapshots, get_daily_bars
from fetchers.finnhub import get_market_cap as finnhub_market_cap
from fetchers.yahoo_quote import get_market_cap as yahoo_market_cap
from fetchers.sec_edgar import has_recent_bankruptcy_filing
from fetchers.high_value_runner import EARNINGS_BLACKOUT

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


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _cheap_price_filter(symbols: list[str]) -> list[str]:
    """
    First-pass filter: batch snapshots (Alpaca) for every active symbol,
    keep only last_close < PRICE_CEILING. Cheap — a handful of batched API
    calls regardless of universe size (Alpaca allows large symbol batches).
    """
    survivors: list[str] = []
    for chunk in _batched(symbols, _SNAPSHOT_BATCH_SIZE):
        snaps = get_snapshots(chunk)
        for sym, snap in snaps.items():
            price = snap.get("price") or 0
            if 0 < price < PRICE_CEILING:
                survivors.append(sym)
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
    if len(daily_bars) < 5:
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
    Returns (sorted list of 50-200 symbols, stats dict) — stats currently
    has stagnant_filtered_count (candidates excluded by the volatility
    floor), logged into low_value_universe_snapshot for visibility into how
    much of the daily candidate pool is "zombie" stocks.

    max_candidates caps how many price-filtered symbols get the expensive
    per-symbol checks — a safety valve against a very large Alpaca universe
    burning through Finnhub's/SEC's rate limits in one scan.
    """
    all_symbols = get_all_active_assets()
    if not all_symbols:
        return [], {"stagnant_filtered_count": 0}

    price_ok = _cheap_price_filter(all_symbols)
    if max_candidates:
        price_ok = price_ok[:max_candidates]

    universe: list[str] = []
    stagnant_filtered_count = 0
    for sym in price_ok:
        if _is_earnings_blackout(sym, today_str):
            continue

        daily_bars = get_daily_bars(sym, days=20)

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

    return sorted(universe), {"stagnant_filtered_count": stagnant_filtered_count}
