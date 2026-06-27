"""
Market data fetcher using Alpaca Data API v2.
Fetches OHLCV, snapshot, and computed fundamentals for stocks and ETFs.
"""
from __future__ import annotations

from fetchers.alpaca import get_daily_bars, get_snapshot

# Map period strings to approximate calendar days to fetch
_PERIOD_TO_DAYS = {
    "1d":  5,
    "5d":  12,
    "1mo": 35,
    "3mo": 100,
    "6mo": 190,
    "1y":  380,
    "2y":  760,
    "5y":  1850,
}


def fetch_ticker(symbol: str, period: str = "3mo") -> dict:
    """
    Fetch comprehensive market data for a ticker symbol via Alpaca.
    Returns OHLCV history, snapshot price, computed fundamentals, and moving averages.
    period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y
    """
    symbol = symbol.upper()
    sources: list[dict] = []
    result: dict = {"symbol": symbol, "sources": sources}

    cal_days = _PERIOD_TO_DAYS.get(period, 100)
    bars = get_daily_bars(symbol, days=cal_days)
    if not bars:
        result["error"] = f"No price data for '{symbol}' from Alpaca"
        return result

    result["history"] = [
        {
            "date":   b["t"][:10],
            "open":   round(float(b["o"]), 4),
            "high":   round(float(b["h"]), 4),
            "low":    round(float(b["l"]), 4),
            "close":  round(float(b["c"]), 4),
            "volume": int(b.get("v", 0)),
        }
        for b in bars
    ]
    sources.append({
        "label":   f"{symbol} price history ({period})",
        "url":     "",
        "snippet": f"{len(result['history'])} trading days fetched via Alpaca",
    })

    # Snapshot for live price (falls back to last bar on error)
    snap = get_snapshot(symbol)
    if snap.get("error"):
        last = bars[-1]
        prev = bars[-2] if len(bars) > 1 else last
        snap_price      = round(float(last["c"]), 4)
        snap_prev       = round(float(prev["c"]), 4)
        snap_change_pct = round((snap_price - snap_prev) / snap_prev * 100, 2) if snap_prev else 0
        snap_open  = round(float(last["o"]), 4)
        snap_high  = round(float(last["h"]), 4)
        snap_low   = round(float(last["l"]), 4)
        snap_vol   = int(last.get("v", 0))
    else:
        snap_price      = snap.get("price", 0)
        snap_prev       = snap.get("prev_close", 0)
        snap_change_pct = snap.get("change_pct", 0)
        snap_open  = snap.get("open", 0)
        snap_high  = snap.get("high", 0)
        snap_low   = snap.get("low",  0)
        snap_vol   = snap.get("volume", 0)

    result["price"] = {
        "current":    snap_price,
        "open":       snap_open,
        "high":       snap_high,
        "low":        snap_low,
        "prev_close": snap_prev,
        "change":     round(snap_price - snap_prev, 4) if snap_prev else 0,
        "change_pct": snap_change_pct,
        "volume":     snap_vol,
    }

    # 52-week high/low and 20-day avg volume computed from bar history
    w52     = bars[-252:] if len(bars) >= 252 else bars
    closes  = [float(b["c"]) for b in bars]
    avg_vol = int(sum(int(b.get("v", 0)) for b in bars[-20:]) / min(20, len(bars)))

    result["fundamentals"] = {
        "name":           symbol,
        "sector":         "",
        "industry":       "",
        "market_cap":     None,
        "pe_ratio":       None,
        "forward_pe":     None,
        "eps":            None,
        "revenue_growth": None,
        "profit_margins": None,
        "debt_to_equity": None,
        "52w_high":       round(max(float(b["h"]) for b in w52), 4),
        "52w_low":        round(min(float(b["l"]) for b in w52), 4),
        "avg_volume":     avg_vol,
        "beta":           None,
        "dividend_yield": None,
        "short_ratio":    None,
        "analyst_target": None,
        "currency":       "USD",
        "exchange":       "",
        "asset_type":     "stock",
    }

    # Moving averages computed from closes
    result["moving_averages"] = {}
    for window in [20, 50, 200]:
        if len(closes) >= window:
            result["moving_averages"][f"ma{window}"] = round(
                sum(closes[-window:]) / window, 4
            )

    return result


def search_ticker(query: str) -> list[dict]:
    """
    Best-effort ticker lookup — validates via Alpaca snapshot.
    Alpaca has no search API so only exact/known symbols work.
    """
    symbol = query.strip().upper().split()[0]
    snap = get_snapshot(symbol)
    if not snap.get("error") and snap.get("price", 0) > 0:
        return [{"symbol": symbol, "name": symbol, "exchange": "", "type": "stock"}]
    return []
