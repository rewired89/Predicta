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

# Low Value round-2 Kimi review (2026-07-07): market cap and fundamentals
# change slowly — cache them 7 days so a daily universe scan only re-fetches
# the fast-moving piece (news) every day, cutting a ~200-symbol scan from
# ~10 min to ~3 min at the free tier's 60-calls/min ceiling.
CACHE_TTL_DAYS = 7


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


def _cache_read(symbol: str, kind: str) -> Optional[dict]:
    """{"data": dict, "fresh": bool, "cached_at": iso str} or None if no cache row. Same shape as fbref.py's cache."""
    try:
        from db.database import get_db
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        with get_db() as conn:
            row = conn.execute(
                "SELECT data_json, cached_at FROM finnhub_cache WHERE symbol=? AND kind=?",
                (symbol, kind),
            ).fetchone()
        if not row:
            return None
        try:
            data = _json.loads(row["data_json"])
        except Exception:
            return None
        try:
            cached_at = _dt.fromisoformat(row["cached_at"].replace("Z", "+00:00"))
        except Exception:
            cached_at = _dt.now(_tz.utc)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=_tz.utc)
        fresh = (_dt.now(_tz.utc) - cached_at) < _td(days=CACHE_TTL_DAYS)
        return {"data": data, "fresh": fresh, "cached_at": cached_at.isoformat()}
    except Exception:
        return None


def _cache_write(symbol: str, kind: str, data: dict) -> None:
    """Insert or update the cache row. Empty dicts aren't cached — only persist successful fetches."""
    if not data:
        return
    try:
        from db.database import get_db
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO finnhub_cache (symbol, kind, data_json, cached_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, kind) DO UPDATE SET
                    data_json = excluded.data_json, cached_at = excluded.cached_at
                """,
                (symbol, kind, _json.dumps(data), _dt.now(_tz.utc).isoformat()),
            )
    except Exception:
        pass


def _cached_or_fetch(symbol: str, kind: str, fetch_fn) -> dict:
    """Fresh cache -> return it. Else live fetch -> cache + return. Else stale cache (better than nothing) -> return. Else {}."""
    cache = _cache_read(symbol, kind)
    if cache and cache["fresh"]:
        return cache["data"]
    fresh_data = fetch_fn()
    if fresh_data:
        _cache_write(symbol, kind, fresh_data)
        return fresh_data
    if cache and cache["data"]:
        return cache["data"]
    return {}


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
    """
    Raw Finnhub company-profile record (marketCapitalization, finnhubIndustry,
    etc). {} on failure. Cached 7 days (CACHE_TTL_DAYS) — profile/market-cap
    data doesn't change daily, so the universe scanner isn't re-fetching it
    for every symbol every morning.
    """
    return _cached_or_fetch(symbol, "profile", lambda: _finnhub_get("stock/profile2", {"symbol": symbol}) or {})


def get_cached_company_name(symbol: str) -> Optional[str]:
    """
    Company display name (e.g. "FIGS, Inc." for symbol FIGS) — 2026-07-13,
    user-requested, so the dashboard can show more than a bare ticker.
    Cache-only, deliberately: reads whatever profile row get_company_profile()
    already cached during the scan (every symbol that made it into the final
    universe passed the market-cap check, so if Finnhub was the source for
    that, its profile — including "name" — is already sitting in
    finnhub_cache). Never triggers a live fetch itself, matching the
    long-standing "a dashboard view must never make a live API call" rule
    (fetchers/low_value_dashboard.py) — a stale/even-days-old name is fine
    for display, company names don't change, but a live Finnhub/Yahoo call
    on every dashboard page load is not fine. Returns None (not the ticker)
    when nothing was ever cached, e.g. Yahoo was the market-cap source
    instead of Finnhub for this symbol — the caller decides the fallback.
    """
    cache = _cache_read(symbol, "profile")
    if not cache or not cache.get("data"):
        return None
    name = cache["data"].get("name")
    return name or None


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
    Cached 7 days (CACHE_TTL_DAYS) — fundamentals don't move daily.
    """
    def _fetch():
        data = _finnhub_get("stock/metric", {"symbol": symbol, "metric": "all"})
        return (data.get("metric") or {}) if data else {}
    return _cached_or_fetch(symbol, "fundamentals", _fetch)


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
