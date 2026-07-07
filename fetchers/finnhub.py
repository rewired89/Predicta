"""
Finnhub fetcher — free API (60 calls/min), requires FINNHUB_API_KEY env var
(register at finnhub.io). Fails safe (returns None/empty) when the key is
absent or a request fails, matching the FRED_API_KEY/OPENWEATHER_API_KEY/
PANDASCORE_API_KEY convention used elsewhere in this codebase.

Used by models/trading/low_value/ (Kimi review, Low Value engine) for:
  - company news headlines (news_overlay.py sentiment/keyword scan)
  - market cap (scanner.py universe filter)
  - basic fundamentals / cash burn proxy (thesis_tracker.py)
"""
from __future__ import annotations
import os
import time
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
MAX_CALLS_PER_MINUTE = 60


def _finnhub_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key or not _HAS_REQUESTS:
        return None
    try:
        p = dict(params or {})
        p["token"] = api_key
        r = requests.get(f"{FINNHUB_BASE_URL}/{path}", params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_company_news(symbol: str, days: int = 7) -> list[dict]:
    """
    Headlines for symbol over the last `days` days.
    Returns [] when FINNHUB_API_KEY is absent or the request fails.
    """
    from datetime import datetime, timezone, timedelta
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=days)
    data = _finnhub_get(
        "company-news",
        {"symbol": symbol, "from": from_date.isoformat(), "to": to_date.isoformat()},
    )
    if not isinstance(data, list):
        return []
    return data


def get_company_profile(symbol: str) -> dict:
    """Raw Finnhub company-profile record (marketCapitalization, finnhubIndustry, etc). {} on failure."""
    data = _finnhub_get("stock/profile2", {"symbol": symbol})
    return data or {}


def get_market_cap(symbol: str) -> Optional[float]:
    """Market cap in dollars (Finnhub returns it in millions — converted here), or None."""
    data = get_company_profile(symbol)
    cap_millions = data.get("marketCapitalization")
    if cap_millions is None:
        return None
    try:
        return float(cap_millions) * 1_000_000
    except Exception:
        return None


def get_basic_financials(symbol: str) -> dict:
    """
    Basic fundamentals metric block (Finnhub 'metric' series). Used for a
    cash-burn-months proxy in thesis_tracker.py. Returns {} on failure — the
    caller must treat missing fields as None, never fabricate a value.
    """
    data = _finnhub_get("stock/metric", {"symbol": symbol, "metric": "all"})
    if not data:
        return {}
    return data.get("metric", {}) or {}


def fetch_news_batch(symbols: list[str], days: int = 7, max_per_minute: int = MAX_CALLS_PER_MINUTE) -> dict[str, list[dict]]:
    """
    Fetches company news for a batch of symbols, rate-limited to Finnhub's
    free-tier 60-calls/minute ceiling. Sleeps 60s every `max_per_minute` calls.
    Returns {} entries fail safe to [] per-symbol, never raises.
    """
    results: dict[str, list[dict]] = {}
    for i, symbol in enumerate(symbols):
        if i > 0 and i % max_per_minute == 0:
            time.sleep(60)
        results[symbol] = get_company_news(symbol, days=days)
    return results
