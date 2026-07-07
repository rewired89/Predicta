"""
Yahoo Finance quote fetcher — backup market-cap source only, used when
Finnhub's profile2 endpoint misses a symbol. Fails safe (returns None) on
any request/parse error. No API key required (public quote endpoint).
"""
from __future__ import annotations
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"


def get_market_cap(symbol: str) -> Optional[float]:
    """Market cap in dollars from Yahoo's public quote endpoint, or None."""
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(
            YAHOO_QUOTE_URL,
            params={"symbols": symbol.upper()},
            headers={"User-Agent": "Mozilla/5.0 (Predicta market-cap backup)"},
            timeout=10,
        )
        r.raise_for_status()
        results = (r.json().get("quoteResponse") or {}).get("result") or []
        if not results:
            return None
        cap = results[0].get("marketCap")
        return float(cap) if cap is not None else None
    except Exception:
        return None
