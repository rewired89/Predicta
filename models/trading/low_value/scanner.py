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


def _avg_daily_volume_20d(symbol: str) -> float:
    bars = get_daily_bars(symbol, days=20)
    if not bars:
        return 0.0
    vols = [b.get("v", 0) for b in bars]
    return sum(vols) / len(vols) if vols else 0.0


def _get_market_cap(symbol: str) -> Optional[float]:
    """Finnhub primary, Yahoo backup per spec ('market cap from Finnhub or Yahoo')."""
    cap = finnhub_market_cap(symbol)
    if cap is not None:
        return cap
    return yahoo_market_cap(symbol)


def _is_earnings_blackout(symbol: str, date_str: str) -> bool:
    return date_str in EARNINGS_BLACKOUT.get(symbol, [])


def build_low_value_universe(today_str: str, max_candidates: Optional[int] = None) -> list[str]:
    """
    Full daily scan pipeline:
      1. All active, tradable, non-OTC US equities (Alpaca).
      2. Cheap price filter (batch snapshots) -> last_close < $20.
      3. Per-candidate: avg_daily_volume_20d > 100k, market_cap > $50M,
         no earnings-blackout match today, no bankruptcy 8-K in last 90 days.
    Returns a sorted list of 50-200 symbols. Order of the expensive checks
    (volume before market cap before bankruptcy) minimizes wasted API calls
    on candidates that fail cheaper checks first.

    max_candidates caps how many price-filtered symbols get the expensive
    per-symbol checks — a safety valve against a very large Alpaca universe
    burning through Finnhub's/SEC's rate limits in one scan.
    """
    all_symbols = get_all_active_assets()
    if not all_symbols:
        return []

    price_ok = _cheap_price_filter(all_symbols)
    if max_candidates:
        price_ok = price_ok[:max_candidates]

    universe: list[str] = []
    for sym in price_ok:
        if _is_earnings_blackout(sym, today_str):
            continue
        if _avg_daily_volume_20d(sym) <= MIN_AVG_DAILY_VOLUME_20D:
            continue
        cap = _get_market_cap(sym)
        if cap is None or cap <= MIN_MARKET_CAP:
            continue
        if has_recent_bankruptcy_filing(sym, days=BANKRUPTCY_LOOKBACK_DAYS):
            continue
        universe.append(sym)
        if len(universe) >= UNIVERSE_MAX_SIZE:
            break

    return sorted(universe)
