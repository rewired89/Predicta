"""
Short-interest fetcher — free, no API key. FINRA's own short-interest files
are bi-monthly fixed-width bulk downloads with no per-symbol endpoint, so
this uses Nasdaq's public short-interest API (api.nasdaq.com), which republishes
the same FINRA settlement-cycle data (~2-week lag) per symbol. Fails safe
(returns None) on any request/parse error.

Used by models/trading/low_value/thesis_tracker.py for the short_interest_pct
signal (Kimi review, Low Value engine) — the 2-week lag is explicitly
acceptable per spec ("delayed, 2-week lag is fine").
"""
from __future__ import annotations
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

NASDAQ_SHORT_INTEREST_URL = "https://api.nasdaq.com/api/quote/{symbol}/short-interest"


def get_short_interest_pct(symbol: str) -> Optional[float]:
    """
    Most recent short-interest as a percent of float, or None if unavailable
    (missing data, rate limit, or request failure — never fabricated).
    """
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(
            NASDAQ_SHORT_INTEREST_URL.format(symbol=symbol.upper()),
            headers={"User-Agent": "Mozilla/5.0 (Predicta short-interest scan)", "Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        rows = (((data.get("data") or {}).get("shortInterestTable") or {}).get("rows")) or []
        if not rows:
            return None
        latest = rows[0]
        pct_str = latest.get("percentFloat") or latest.get("daysToCoverShares")
        if pct_str in (None, "", "N/A"):
            return None
        return float(str(pct_str).replace("%", "").replace(",", ""))
    except Exception:
        return None
