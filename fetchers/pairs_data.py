"""
Batch historical price fetcher for pairs trading.

Fetches daily closing prices for a list of symbols over a lookback window,
returning a aligned DataFrame suitable for cointegration analysis.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
import os
import requests


DATA_BASE_URL = "https://data.alpaca.markets"


def _headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _fetch_daily_bars(symbol: str, days: int) -> list[dict]:
    start = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"{DATA_BASE_URL}/v2/stocks/{symbol}/bars",
            headers=_headers(),
            params={"timeframe": "1Day", "start": start, "limit": days, "feed": "iex", "sort": "asc"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("bars", [])
    except Exception:
        return []


def fetch_pair_history(symbols: list[str], days: int = 120) -> dict[str, dict[str, float]]:
    """
    Fetch daily close prices for each symbol over `days` trading days.

    Returns a dict mapping date string → {symbol: close_price} for dates
    present in ALL symbols (intersection), so the resulting series are
    always the same length — required for cointegration tests.

    Example:
        prices = fetch_pair_history(["XOM", "CVX"], days=120)
        # {"2024-01-02": {"XOM": 99.5, "CVX": 148.0}, ...}
    """
    raw: dict[str, dict[str, float]] = {}  # symbol → {date: close}

    for sym in symbols:
        bars = _fetch_daily_bars(sym, days)
        if not bars:
            continue
        raw[sym] = {}
        for b in bars:
            date = b["t"][:10]  # "2024-01-02T00:00:00Z" → "2024-01-02"
            raw[sym][date] = b["c"]

    if not raw:
        return {}

    # Intersection of dates across all fetched symbols
    common_dates = sorted(
        set.intersection(*[set(dates.keys()) for dates in raw.values()])
    )

    result: dict[str, dict[str, float]] = {}
    for date in common_dates:
        result[date] = {sym: raw[sym][date] for sym in raw}

    return result


def prices_to_series(history: dict[str, dict[str, float]]) -> dict[str, list[float]]:
    """
    Convert fetch_pair_history output to {symbol: [close, ...]} aligned lists.
    Dates are sorted ascending; both lists have identical length.
    """
    if not history:
        return {}
    dates = sorted(history.keys())
    if not dates:
        return {}
    symbols = list(history[dates[0]].keys())
    return {sym: [history[d][sym] for d in dates] for sym in symbols}
