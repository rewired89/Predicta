"""
Order ledger + per-symbol lock (Kimi round-3 hardening).

Every real (paper-broker) order any engine sends goes through guarded_order():
- one lock per symbol, so two engines/threads cannot send orders on the same
  stock at the same moment,
- an entry is refused if that trade row already has a broker order id
  (stops a double-click or a retry from buying twice),
- every attempt (ok or error) is written to `order_ledger`, the single place
  to answer "what did we actually send to the broker, and when".
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from db.database import get_db

log = logging.getLogger("order_ledger")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_DDL = """CREATE TABLE IF NOT EXISTS order_ledger (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    engine TEXT,
    trade_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT,
    qty REAL,
    kind TEXT NOT NULL,
    alpaca_order_id TEXT,
    status TEXT NOT NULL,
    detail TEXT
)"""


def _lock_for(symbol: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(symbol.upper(), threading.Lock())


def _write(engine, trade_id, symbol, side, qty, kind, order_id, status, detail=""):
    try:
        with get_db() as conn:
            conn.execute(_DDL)
            conn.execute(
                "INSERT INTO order_ledger (ts, engine, trade_id, symbol, side, qty, kind, alpaca_order_id, status, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), engine, trade_id, symbol, side, qty,
                 kind, order_id, status, str(detail)[:500]),
            )
    except Exception as e:  # ledger must never block a trade or hide its error
        log.error(f"[LEDGER] write failed: {e}")


def guarded_order(engine: str, trade_id, symbol: str, side: str, qty, kind: str, send: Callable[[], dict]) -> dict:
    """kind: 'entry' or 'exit'. `send` performs the broker call and returns its dict."""
    with _lock_for(symbol):
        if kind == "entry" and trade_id:
            with get_db() as conn:
                row = conn.execute("SELECT alpaca_order_id FROM intraday_trades WHERE id = ?", (trade_id,)).fetchone()
            if row and row["alpaca_order_id"]:
                _write(engine, trade_id, symbol, side, qty, kind, row["alpaca_order_id"], "REFUSED_DUPLICATE")
                return {"error": f"{symbol}: this trade already has a broker order ({row['alpaca_order_id']}); not sending a second one."}
        res = send()
        if isinstance(res, dict) and "error" in res:
            _write(engine, trade_id, symbol, side, qty, kind, None, "ERROR", res.get("error"))
        else:
            oid = res.get("id") or res.get("order_id") if isinstance(res, dict) else None
            _write(engine, trade_id, symbol, side, qty, kind, oid, "SENT")
        return res
