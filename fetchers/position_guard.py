"""
Broker-position safety + real fill prices (2026-10-31, Kimi review).

Two problems this fixes, both found by reading the code:
1. close_position(symbol) liquidates the WHOLE broker position for a symbol.
   All three engines share one paper account, so if two engines held the same
   stock, one engine's exit sold the other's shares too. close_for_trade()
   sells only this trade's quantity when another engine still holds the stock.
2. Entry and exit prices were recorded from a snapshot, not the real fill, so
   reported P&L was not what the broker actually did. fill_price() and
   reconcile_entry_fills() use Alpaca's filled_avg_price.
"""
from __future__ import annotations
import logging
import time
from typing import Optional

import requests

from db.database import get_db
from fetchers import alpaca as _alp

log = logging.getLogger("position_guard")


def other_open_broker_qty(symbol: str, exclude_trade_id: int) -> float:
    """Shares of `symbol` held by OTHER open rows that have a real broker order."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS q FROM intraday_trades "
            "WHERE symbol = ? AND id != ? AND is_hypothetical = 0 AND exit_time IS NULL "
            "AND alpaca_order_id IS NOT NULL",
            (symbol, exclude_trade_id),
        ).fetchone()
    return float(row["q"] or 0)


def fill_price(order_id: Optional[str], tries: int = 5, wait: float = 1.5) -> Optional[float]:
    """Real average fill price for an order, or None if it has not filled yet."""
    if not order_id:
        return None
    for i in range(tries):
        o = _alp.get_order(order_id)
        if isinstance(o, dict) and o.get("status") == "filled":
            try:
                px = float(o.get("filled_avg_price") or 0)
            except (TypeError, ValueError):
                px = 0.0
            return px if px > 0 else None
        if i < tries - 1:
            time.sleep(wait)
    return None


def close_for_trade(symbol: str, trade_id: int, qty: Optional[float], side: str) -> dict:
    """
    Close exactly this trade at the broker.
    - No other engine holds the symbol: close the whole position (also cancels
      leftover bracket legs), same as before.
    - Another engine holds it too: sell/cover only this trade's qty, so the
      other engine's shares are untouched.
    Returns {"mode", "order_id", "fill_price"} or {"error", "status_code"?}.
    """
    _alp._assert_paper_mode()
    others = other_open_broker_qty(symbol, trade_id)
    if others > 0 and qty and qty > 0:
        close_side = "sell" if side == "long" else "buy"
        order = _alp.place_order(symbol, qty, close_side, "market")
        if isinstance(order, dict) and "error" in order:
            return order
        oid = order.get("id") if isinstance(order, dict) else None
        log.warning(f"[GUARD] {symbol} also held by another open trade ({others} sh) - "
                    f"closed only this trade's {qty} sh (tid={trade_id})")
        return {"mode": "partial", "order_id": oid, "fill_price": fill_price(oid)}

    try:
        r = requests.delete(f"{_alp.PAPER_BASE_URL}/v2/positions/{symbol}", headers=_alp._headers(), timeout=10)
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            body = {}
    except requests.exceptions.HTTPError as e:
        return {"error": str(e), "status_code": r.status_code}
    except Exception as e:
        return {"error": str(e)}
    oid = body.get("id") if isinstance(body, dict) else None
    return {"mode": "full", "order_id": oid, "fill_price": fill_price(oid)}


def reconcile_entry_fills(engine: str) -> int:
    """
    For open real positions, replace the recorded entry price/qty with what
    Alpaca actually filled (idempotent; only writes when they differ).
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, symbol, entry_price, qty, alpaca_order_id FROM intraday_trades "
            "WHERE engine = ? AND is_hypothetical = 0 AND exit_time IS NULL AND alpaca_order_id IS NOT NULL",
            (engine,),
        ).fetchall()
    fixed = 0
    for r in rows:
        o = _alp.get_order(r["alpaca_order_id"])
        if not isinstance(o, dict) or o.get("status") != "filled":
            continue
        try:
            px = float(o.get("filled_avg_price") or 0)
            fq = float(o.get("filled_qty") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if abs(px - (r["entry_price"] or 0)) > 0.005 or (fq > 0 and abs(fq - (r["qty"] or 0)) > 1e-6):
            with get_db() as conn:
                conn.execute("UPDATE intraday_trades SET entry_price = ?, qty = ? WHERE id = ?",
                             (px, fq if fq > 0 else r["qty"], r["id"]))
            log.info(f"[GUARD] {r['symbol']} tid={r['id']} entry reconciled to real fill {px} x {fq}")
            fixed += 1
    return fixed
