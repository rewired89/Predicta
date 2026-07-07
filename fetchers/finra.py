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


def get_short_interest(symbol: str) -> dict:
    """
    Most recent short-interest pct-of-float plus its settlement date, or {}
    if unavailable. as_of_date is logged (Kimi review round-2 follow-up)
    so it's always visible how stale a given short_interest_pct reading is
    — FINRA's settlement cycle means this can be up to ~2 weeks old, which
    is expected/acceptable for this signal, not a bug to fix.
    """
    if not _HAS_REQUESTS:
        return {}
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
            return {}
        latest = rows[0]
        pct_str = latest.get("percentFloat") or latest.get("daysToCoverShares")
        if pct_str in (None, "", "N/A"):
            return {}
        pct = float(str(pct_str).replace("%", "").replace(",", ""))
        as_of_date = latest.get("settlementDate") or None
        return {"pct": pct, "as_of_date": as_of_date}
    except Exception:
        return {}


def get_short_interest_pct(symbol: str) -> Optional[float]:
    """Backward-compatible pct-only accessor. Prefer get_short_interest() for the as_of_date too."""
    return get_short_interest(symbol).get("pct")
