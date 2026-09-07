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

2026-09-03 (user-requested — High Value "Copy Trades" feature): added
get_latest_filings(), a MARKET-WIDE feed (openinsider.com's own
"latest-insider-trading" page, not the per-symbol screener above) so the
UI can browse today's/this week's real Form 4 filings across every
company, not just check one symbol at a time. Same fail-safe convention
and same tempered-dot per-row isolation as the rest of this file, but the
exact 13-column layout this assumes (Filing Date / Trade Date / Ticker /
Company / Insider Name / Title / Trade Type / Price / Qty / Owned / ΔOwn /
Value) has NOT been live-verified from this sandbox — openinsider.com is
blocked by this environment's egress proxy, the same class of block every
other new data source in this codebase has hit first (see CLAUDE.md's UFC
section for the identical pattern: ship best-effort, confirm via a live
diagnostic after deploy). Use GET /copy-trades-diag on Railway to check
the very next real fetch before trusting this in production.

Congress/politician trades were considered and deliberately NOT added
here — real congressional data needs a paid Quiver Quantitative API key
(no free tier with full history), and by law disclosures lag up to 45
days, so "today's trades" would misleadingly describe filings that could
be over a month old. Corporate insiders (this function) file within 2
business days, which is the closest genuinely "recent" free public-record
data available. Revisit if the user supplies a QUIVER_API_KEY.
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


# ── Market-wide latest filings (Copy Trades feature, 2026-09-03) ────────────

LATEST_FILINGS_URL = "http://openinsider.com/latest-insider-trading"

_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"[-+]?[\d,]*\.?\d+")


def _cell_text(raw: str) -> str:
    return _TAG_RE.sub("", raw).replace("&nbsp;", " ").strip()


def _parse_number(text: str) -> Optional[float]:
    """Strips $, commas, %% from a cell and returns the first number found, or None."""
    m = _NUM_RE.search((text or "").replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _fetch_latest_html() -> Optional[str]:
    if not _HAS_REQUESTS:
        return None
    try:
        r = requests.get(
            LATEST_FILINGS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Predicta copy-trades scan)"},
            timeout=10,
        )
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def _parse_latest_filings_html(html: str) -> list[dict]:
    """
    Parses openinsider.com's "latest-insider-trading" table. Column layout
    (0-indexed, 13 cells per real data row): 0=row#, 1=Filing Date,
    2=Trade Date, 3=Ticker, 4=Company Name, 5=Insider Name, 6=Title,
    7=Trade Type, 8=Price, 9=Qty, 10=Owned, 11=ΔOwn, 12=Value.
    Rows with fewer cells (header/footer/ad rows) are skipped, not guessed.
    """
    out: list[dict] = []
    for row_match in _ROW_BLOCK_RE.finditer(html or ""):
        row_html = row_match.group(1)
        cells = [_cell_text(c) for c in _CELL_RE.findall(row_html)]
        if len(cells) < 13:
            continue
        trade_type_raw = cells[7].strip()
        if trade_type_raw.upper().startswith("P - PURCHASE"):
            trade_type = "Purchase"
        elif trade_type_raw.upper() == "S - SALE":  # exact match only, excludes "S - Sale+OE" — same convention as the per-symbol screener above
            trade_type = "Sale"
        else:
            continue
        ticker = cells[3].strip().upper()
        if not ticker or not ticker.isalnum():
            continue
        out.append({
            "filing_date":  cells[1].strip(),
            "trade_date":   cells[2].strip(),
            "ticker":       ticker,
            "company":      cells[4].strip(),
            "insider_name": cells[5].strip(),
            "title":        cells[6].strip(),
            "trade_type":   trade_type,
            "filed_price":  _parse_number(cells[8]),
            "qty":          _parse_number(cells[9]),
            "value":        _parse_number(cells[12]),
        })
    return out


def get_latest_filings(limit: int = 40, days: int = 7) -> list[dict]:
    """
    Most recent market-wide Form 4 open-market Buy/Sell filings (corporate
    insiders — CEOs, execs, board members), newest first, capped at `limit`
    and filtered to filings within the last `days` days. Empty list on any
    fetch/parse failure — fails safe, never fabricates data.
    """
    html = _fetch_latest_html()
    if not html:
        return []
    rows = _parse_latest_filings_html(html)
    if not rows:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    def _within_window(r: dict) -> bool:
        try:
            return datetime.strptime(r["filing_date"][:10], "%Y-%m-%d").date() >= cutoff
        except Exception:
            return True  # unparseable date — don't silently drop real data, let the limit/sort handle it

    filtered = [r for r in rows if _within_window(r)]
    filtered.sort(key=lambda r: r.get("filing_date") or "", reverse=True)
    return filtered[:limit]


def diagnose_latest_filings() -> dict:
    """
    Live diagnostic for get_latest_filings() — raw fetch status plus the
    first few parsed rows, so a real deploy can confirm (or disprove) the
    column-layout assumption in _parse_latest_filings_html() against the
    live page, the same way every other new source in this codebase gets
    its first real check via a /*-diag endpoint.

    2026-09-07: parsed_row_count came back 0 in production with a real,
    full-size page fetched OK (html_length ~126KB) — meaning the 13-cells-
    per-row assumption in _parse_latest_filings_html() doesn't match the
    live markup (a redesign, or the table isn't <table>/<tr>/<td> at all
    anymore). Added a raw-structure probe below that doesn't depend on that
    assumption being right, so the actual live cell counts/content can be
    read back directly instead of guessing again.
    """
    html = _fetch_latest_html()
    if not html:
        return {"fetch_ok": False, "reason": "request failed or requests not installed"}
    rows = _parse_latest_filings_html(html)

    row_blocks = list(_ROW_BLOCK_RE.finditer(html))
    cell_counts: dict[int, int] = {}
    best_row_html = ""
    best_row_cells: list[str] = []
    for m in row_blocks:
        row_html = m.group(1)
        cells = [_cell_text(c) for c in _CELL_RE.findall(row_html)]
        cell_counts[len(cells)] = cell_counts.get(len(cells), 0) + 1
        if len(cells) > len(best_row_cells):
            best_row_cells = cells
            best_row_html = row_html

    return {
        "fetch_ok": True,
        "html_length": len(html),
        "parsed_row_count": len(rows),
        "sample_rows": rows[:5],
        "raw_tr_count": len(row_blocks),
        "raw_cell_count_histogram": cell_counts,
        "raw_best_row_cells": best_row_cells,
        "raw_best_row_html_snippet": best_row_html[:1500],
        "raw_html_has_table_tag": "<table" in html.lower(),
        "raw_html_head_snippet": html[:800],
    }
