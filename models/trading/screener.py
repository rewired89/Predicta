"""
Stock screener — scans a watchlist and ranks by intraday signal score.
Uses Alpaca batch snapshots + intraday bars for each symbol.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from fetchers.alpaca import get_snapshots, get_bars, get_daily_bars, get_top_movers, get_most_active
from models.trading.intraday import compute_intraday_signals

# Default watchlist — large-cap liquid names good for day trading
DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD",
    "SPY", "QQQ", "SOFI", "PLTR", "RIVN", "COIN", "MSTR", "HOOD",
]


def _avg_daily_volume(daily_bars: list[dict]) -> float:
    if not daily_bars:
        return 1_000_000
    vols = [b.get("v", 0) for b in daily_bars[-20:]]
    return sum(vols) / len(vols) if vols else 1_000_000


def _analyze_one(symbol: str, snapshot: dict) -> Optional[dict]:
    """Fetch bars and compute signals for a single symbol."""
    try:
        intraday = get_bars(symbol, timeframe="5Min", limit=78)
        daily = get_daily_bars(symbol, days=60)
        if not intraday:
            return None
        avg_vol = _avg_daily_volume(daily)
        result = compute_intraday_signals(intraday, daily, snapshot, avg_vol)
        if "error" in result:
            return None
        score = result["score"]
        levels = result.get("levels", {})
        snap = snapshot
        return {
            "symbol": symbol,
            "price": snap.get("price", 0),
            "change_pct": snap.get("change_pct", 0),
            "volume": snap.get("volume", 0),
            "score": score["value"],
            "label": score["label"],
            "reasons": score["reasons"],
            "side": levels.get("side", "long"),
            "entry": levels.get("entry"),
            "stop": levels.get("stop"),
            "target1": levels.get("target1"),
            "target2": levels.get("target2"),
            "rr_ratio": levels.get("rr_ratio"),
            "atr": levels.get("atr"),
            "signals": result["signals"],
        }
    except Exception:
        return None


def run_screener(
    symbols: Optional[list[str]] = None,
    use_movers: bool = False,
    max_workers: int = 8,
) -> dict:
    """
    Scan symbols and return ranked list by signal score.
    use_movers=True fetches today's top gainers/losers from Alpaca instead.
    """
    if use_movers:
        movers = get_top_movers(20)
        active = get_most_active(10)
        syms = set()
        for g in movers.get("gainers", []):
            syms.add(g.get("symbol", ""))
        for l in movers.get("losers", []):
            syms.add(l.get("symbol", ""))
        for a in active:
            syms.add(a.get("symbol", ""))
        syms.discard("")
        symbols = list(syms)[:30]
    else:
        symbols = symbols or DEFAULT_WATCHLIST

    # Batch snapshot first (one API call for all)
    snapshots = get_snapshots(symbols)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futures = {
            exe.submit(_analyze_one, sym, snapshots.get(sym, {})): sym
            for sym in symbols
            if sym in snapshots
        }
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                results.append(res)

    # Sort: Strong Buy first, Strong Sell last; within same label by abs(score)
    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "count": len(results),
        "symbols_scanned": symbols,
        "results": results,
    }
