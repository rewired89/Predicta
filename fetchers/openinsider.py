"""
OpenInsider fetcher — free, no API key, HTML screener scrape. Fails safe
(returns False/[]) on any request/parse error.

Used by models/trading/low_value/thesis_tracker.py for the insider_buying_30d
signal (Kimi review, Low Value engine) — same free source already used for
this purpose in the Nyx AI project's stock_scanner.py.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

OPENINSIDER_URL = "http://openinsider.com/screener"

_ROW_RE = re.compile(
    r"<tr[^>]*>.*?"
    r"(\d{4}-\d{2}-\d{2})\s*</td>.*?"          # filing date
    r"<td[^>]*>\s*P\s*-\s*Purchase\s*</td>",    # transaction type == Purchase
    re.IGNORECASE | re.DOTALL,
)


def get_recent_insider_purchases(symbol: str, days: int = 30) -> list[str]:
    """
    Filing dates (YYYY-MM-DD strings) of open-market insider Purchase (code P)
    transactions for symbol within the last `days` days. Returns [] on any
    fetch/parse failure or if no purchases are found — never fabricates data.
    """
    if not _HAS_REQUESTS:
        return []
    try:
        r = requests.get(
            OPENINSIDER_URL,
            params={"s": symbol.upper()},
            headers={"User-Agent": "Mozilla/5.0 (Predicta insider-buying scan)"},
            timeout=10,
        )
        r.raise_for_status()
        html = r.text
    except Exception:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    dates: list[str] = []
    for m in _ROW_RE.finditer(html):
        date_str = m.group(1)
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if dt >= cutoff:
            dates.append(date_str)
    return dates


def has_insider_buying(symbol: str, days: int = 30) -> bool:
    """Boolean convenience wrapper for thesis_tracker.py's insider_buying_30d signal."""
    return bool(get_recent_insider_purchases(symbol, days=days))
