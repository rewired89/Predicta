"""
Copy Trades — High Value engine, user-requested 2026-09-03.

Lets a human browse today's/this week's real SEC Form 4 insider filings
(corporate insiders — CEOs, execs, board members — whose trades are public
record, disclosed within 2 business days by law) and paper-copy one at a
share quantity they choose. This is NOT a new autonomous strategy: nothing
here scans, decides, or places a real order on its own — a human reads a
filing off a list and clicks Copy, same authority model as every other
engine in this codebase (see CLAUDE.md's Authority model section).
"Copy this trade" logs a PAPER position only (is_hypothetical=1, no Alpaca
call) — turning it into a real order still goes through the existing
passcode-gated execute_high_value_trade flow, unchanged, if the user wants
that later for a specific position.

Data source: fetchers/openinsider.py's get_latest_filings() — free, no key,
scrapes openinsider.com's public "latest insider trading" page. Unverified
from this sandbox (openinsider.com is blocked by the egress proxy here,
the same class of block every other new source in this codebase hits
first) — see GET /copy-trades-diag for the first live check after deploy.

Politician (Congress) trades were explicitly considered and NOT built here
— see CLAUDE.md and openinsider.py's module docstring: real congressional
data needs a paid Quiver Quantitative API key, and disclosures lag up to
45 days by law, so "of the day" would misrepresent how fresh the data
actually is. Revisit if the user supplies a QUIVER_API_KEY.
"""
from __future__ import annotations
from typing import Optional

from db.database import get_db
from fetchers.openinsider import get_latest_filings
from fetchers.alpaca import get_snapshots
from fetchers.trading_logger import (
    log_copy_trade_entry, add_to_copy_trade, log_trade_exit,
    get_copy_trade_portfolio, get_closed_copy_trades,
)


def list_copy_trade_candidates(limit: int = 40, days: int = 7) -> list[dict]:
    """
    Recent insider Buy+Sell filings, enriched with a live price snapshot per
    symbol so the UI shows what copying it would cost RIGHT NOW — not the
    filing's original transaction price, which is often days old by the
    time it's disclosed and public.
    """
    filings = get_latest_filings(limit=limit, days=days)
    if not filings:
        return []
    symbols = sorted({f["ticker"] for f in filings if f.get("ticker")})
    try:
        snapshots = get_snapshots(symbols) if symbols else {}
    except Exception:
        snapshots = {}
    out = []
    for f in filings:
        snap = snapshots.get(f["ticker"]) or {}
        out.append({**f, "live_price": snap.get("price") or None})
    return out


def copy_insider_trade(
    symbol: str,
    trade_type: str,          # "Purchase" or "Sale", as returned by list_copy_trade_candidates
    qty: float,
    insider_name: str = "",
    insider_title: str = "",
    company: str = "",
    filing_date: str = "",
) -> dict:
    """
    Logs a new paper position mirroring an insider's disclosed trade — a
    Buy filing means you go long, a Sale filing means you go short (same
    'long'/'short' side convention as every other High Value row; going
    short to "copy" a sale is a real, explicit trade-off worth knowing —
    see the Copy Trades page's own note on this). No stop/target bracket:
    this isn't a scored model signal, nothing computed exit levels for it,
    so exiting is always a manual Sell from the Portfolio page. No Alpaca
    order is placed here — purely a logged paper position.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {"error": "Missing symbol."}
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return {"error": "Quantity must be a number."}
    if qty <= 0:
        return {"error": "Quantity must be greater than 0."}
    if trade_type not in ("Purchase", "Sale"):
        return {"error": "trade_type must be 'Purchase' or 'Sale'."}
    side = "long" if trade_type == "Purchase" else "short"

    try:
        snap = get_snapshots([symbol]).get(symbol) or {}
    except Exception:
        snap = {}
    price = snap.get("price")
    if not price:
        return {"error": f"Could not get a live price for {symbol} right now — try again in a moment."}

    trade_id = log_copy_trade_entry(
        symbol=symbol, side=side, entry_price=price, qty=qty,
        insider_name=insider_name, insider_title=insider_title,
        company=company, filing_date=filing_date,
    )
    return {
        "status": "COPIED", "trade_id": trade_id, "symbol": symbol,
        "side": side, "qty": qty, "entry_price": price,
    }


def buy_more(trade_id: int, additional_qty: float) -> dict:
    """Adds shares to an already-open copy-trade position at the current live price."""
    try:
        additional_qty = float(additional_qty)
    except (TypeError, ValueError):
        return {"error": "Quantity must be a number."}
    if additional_qty <= 0:
        return {"error": "Quantity must be greater than 0."}

    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol FROM intraday_trades WHERE id = ? AND is_copy_trade = 1 AND exit_time IS NULL",
            (trade_id,),
        ).fetchone()
    if not row:
        return {"error": "Open copy-trade position not found."}
    symbol = row["symbol"]

    try:
        snap = get_snapshots([symbol]).get(symbol) or {}
    except Exception:
        snap = {}
    price = snap.get("price")
    if not price:
        return {"error": f"Could not get a live price for {symbol} right now — try again in a moment."}

    return add_to_copy_trade(trade_id, additional_qty, price)


def sell_copy_trade(trade_id: int) -> dict:
    """Fully closes an open copy-trade position at the current live price — a paper sell, no Alpaca order."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol FROM intraday_trades WHERE id = ? AND is_copy_trade = 1 AND exit_time IS NULL",
            (trade_id,),
        ).fetchone()
    if not row:
        return {"error": "Open copy-trade position not found, or already closed."}
    symbol = row["symbol"]

    try:
        snap = get_snapshots([symbol]).get(symbol) or {}
    except Exception:
        snap = {}
    price = snap.get("price")
    if not price:
        return {"error": f"Could not get a live price for {symbol} right now — try again in a moment."}

    return log_trade_exit(trade_id, exit_price=price, exit_reason="MANUAL_COPY_SELL")


def get_portfolio_with_unrealized_pnl() -> list[dict]:
    """Open copy-trade positions with live price + unrealized P&L attached."""
    positions = get_copy_trade_portfolio()
    if not positions:
        return []
    symbols = sorted({p["symbol"] for p in positions})
    try:
        snapshots = get_snapshots(symbols)
    except Exception:
        snapshots = {}
    for p in positions:
        live_price = (snapshots.get(p["symbol"]) or {}).get("price")
        p["live_price"] = live_price
        if live_price and p.get("entry_price") and p.get("qty"):
            per_share = (live_price - p["entry_price"]) if p["side"] == "long" else (p["entry_price"] - live_price)
            p["unrealized_pnl"] = round(per_share * p["qty"], 2)
            p["unrealized_pnl_pct"] = round(per_share / p["entry_price"] * 100, 2) if p["entry_price"] else None
        else:
            p["unrealized_pnl"] = None
            p["unrealized_pnl_pct"] = None
    return positions
