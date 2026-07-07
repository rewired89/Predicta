"""
SEC EDGAR fetcher — free, no API key, but requires a descriptive User-Agent
header per SEC's fair-access policy (https://www.sec.gov/os/webmaster-faq#developers).
Fails safe (returns None/False/[]) on any request error, matching every other
fetcher in this codebase.

Used by models/trading/low_value/scanner.py (Kimi review, Low Value engine)
to exclude symbols with a recent bankruptcy-related 8-K (Item 1.03) filing.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "Predicta contact@predicta.local")

_ticker_cik_cache: Optional[dict[str, str]] = None


def _headers() -> dict:
    return {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate"}


def _get_json(url: str) -> Optional[dict]:
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _load_ticker_cik_map() -> dict[str, str]:
    """
    Loads and caches SEC's ticker->CIK map for the life of the process.
    Returns {} on failure (fails safe — every lookup then returns None/False
    rather than raising).
    """
    global _ticker_cik_cache
    if _ticker_cik_cache is not None:
        return _ticker_cik_cache
    data = _get_json(SEC_TICKER_MAP_URL)
    if not data:
        _ticker_cik_cache = {}
        return _ticker_cik_cache
    mapping: dict[str, str] = {}
    for entry in data.values():
        try:
            ticker = entry["ticker"].upper()
            cik10 = str(entry["cik_str"]).zfill(10)
            mapping[ticker] = cik10
        except Exception:
            continue
    _ticker_cik_cache = mapping
    return mapping


def get_cik(symbol: str) -> Optional[str]:
    """10-digit zero-padded CIK for a ticker, or None if not found/unavailable."""
    return _load_ticker_cik_map().get(symbol.upper())


def has_recent_bankruptcy_filing(symbol: str, days: int = 90) -> bool:
    """
    True if symbol filed an 8-K with Item 1.03 (Bankruptcy or Receivership)
    in the last `days` days. Fails safe to False (never blocks the universe
    scanner on a data-source hiccup — a missed bankruptcy flag is a false
    negative, not a false positive that would corrupt collection).
    """
    cik10 = get_cik(symbol)
    if not cik10:
        return False
    data = _get_json(SEC_SUBMISSIONS_URL.format(cik10=cik10))
    if not data:
        return False
    recent = (data.get("filings") or {}).get("recent") or {}
    forms      = recent.get("form", [])
    items      = recent.get("items", [])
    filing_dts = recent.get("filingDate", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    for form, item_str, filing_dt in zip(forms, items, filing_dts):
        if form != "8-K" or "1.03" not in (item_str or ""):
            continue
        try:
            dt = datetime.strptime(filing_dt, "%Y-%m-%d").date()
        except Exception:
            continue
        if dt >= cutoff:
            return True
    return False
