"""
OpenInsider fetcher — free, no API key, HTML screener scrape. Fails safe
(returns False/[]/{} on any request/parse error.

Used by models/trading/low_value/thesis_tracker.py for the insider_buying_30d
signal (Kimi review, Low Value engine) — same free source already used for
this purpose in the Nyx AI project's stock_scanner.py.

2026-07-13 (user-requested): also extracts insider SELLING (Form 4, code S -
Sale), not just buying, from the same page fetch — one HTTP call answers
both questions instead of two. Rationale (user's own framing): a stock
that's been dumped by retail/fear-driven selling is a much stronger
contrarian "sellers overshot" case when the company's own insiders are NOT
also selling, versus a case where insiders are dumping right alongside
everyone else (real smart-money confirmation something's actually wrong,
not just panic). True institutional/13F ownership data would answer this
better but is 45-day-lagged by SEC rule and has no free source wired into
this codebase — insider Form-4 selling (disclosed within 2 business days)
is the closest fresh, free proxy available.

Same 2026-07-13 change also fixed a real correctness bug in the original
single-regex row matcher: the date capture and the "P - Purchase" type
check weren't scoped to the same <tr>, so a DOTALL non-greedy .*? between
them could walk past a row's </tr> and pair a date from one row with a
transaction type from a LATER row anywhere in the page (silently wrong
whenever the nearest matching type cell wasn't in the same row as the
date it got attributed to). Adding a second transaction type (Sale) made
this immediately visible in testing. Fixed by extracting each <tr>...</tr>
block first (bounded, can't cross a row boundary), then searching for date
and type within that isolated block.

Known limitation, not solved here: this does not distinguish a routine,
pre-scheduled 10b5-1 plan sale (common, usually not a signal — e.g. an
executive's standing plan to sell N shares/quarter for diversification)
from a discretionary, conviction-driven open-market sale. OpenInsider's
screener does expose a 10b5-1 flag per row, but parsing it reliably needs
more careful scraping than this v1 does — treat insider_sales_30d as a
noisier signal than insider_purchases_30d for that reason.
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

# Isolates one table row's inner HTML at a time — (?:(?!</tr>).)*? is a
# "tempered dot": match any character NOT at the start of a </tr>, so this
# can never walk past its own row's closing tag looking for the next thing.
_ROW_BLOCK_RE = re.compile(r"<tr[^>]*>((?:(?!</tr>).)*?)</tr>", re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_PURCHASE_TYPE_RE = re.compile(r"<td[^>]*>\s*P\s*-\s*Purchase\s*</td>", re.IGNORECASE)
_SALE_TYPE_RE = re.compile(r"<td[^>]*>\s*S\s*-\s*Sale\s*</td>", re.IGNORECASE)  # exact "S - Sale" only, not "S - Sale+OE" — v1, see module docstring


def _fetch_screener_html(symbol: str) -> Optional[str]:
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(
            OPENINSIDER_URL,
            params={"s": symbol.upper()},
            headers={"User-Agent": "Mozilla/5.0 (Predicta insider-activity scan)"},
            timeout=10,
        )
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def _dates_within(html: str, type_re: re.Pattern, days: int) -> list[str]:
    """Filing dates of rows whose own <tr> contains both a date and a match for type_re — never a date from one row paired with a type from another."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    dates: list[str] = []
    for row_match in _ROW_BLOCK_RE.finditer(html):
        row = row_match.group(1)
        if not type_re.search(row):
            continue
        date_match = _DATE_RE.search(row)
        if not date_match:
            continue
        try:
            dt = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
        except Exception:
            continue
        if dt >= cutoff:
            dates.append(date_match.group(1))
    return dates


def get_recent_insider_activity(symbol: str, days: int = 30) -> dict:
    """
    One HTTP call, both directions: {"purchases": [filing dates], "sales":
    [filing dates]} of open-market insider transactions for symbol within
    the last `days` days. Empty lists (not missing keys) on any fetch/parse
    failure — never fabricates data, matches every other fetcher's
    fail-safe convention in this codebase.
    """
    html = _fetch_screener_html(symbol)
    if not html:
        return {"purchases": [], "sales": []}
    return {
        "purchases": _dates_within(html, _PURCHASE_TYPE_RE, days),
        "sales":     _dates_within(html, _SALE_TYPE_RE, days),
    }


def get_recent_insider_purchases(symbol: str, days: int = 30) -> list[str]:
    """Filing dates of open-market insider Purchase transactions. Kept as a thin wrapper — was the original public entry point before insider selling was added."""
    return get_recent_insider_activity(symbol, days=days)["purchases"]


def has_insider_buying(symbol: str, days: int = 30) -> bool:
    """Boolean convenience wrapper for thesis_tracker.py's insider_buying_30d signal."""
    return bool(get_recent_insider_purchases(symbol, days=days))
