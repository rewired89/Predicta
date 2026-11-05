"""
OANDA connector for the FX Forecast research engine (read-only prices).

Uses OANDA's free PRACTICE (demo) account: real prices, fake money. This file
can only READ candles. It has no order code at all, by design.
Env: OANDA_API_KEY (required), OANDA_ENV = "practice" (default) or "live"
(prices only; still no orders).
"""
import logging
import os
from datetime import datetime, timezone

import requests

from db.database import get_db

log = logging.getLogger("oanda")
PAIRS = {"EUR_USD": 0.0001, "GBP_USD": 0.0001, "USD_JPY": 0.01}  # pair -> pip size

_DDL = """CREATE TABLE IF NOT EXISTS fx_daily_bars (
    pair TEXT NOT NULL, date TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, spread REAL,
    PRIMARY KEY (pair, date))"""


def _base() -> str:
    return ("https://api-fxtrade.oanda.com" if os.environ.get("OANDA_ENV", "practice") == "live"
            else "https://api-fxpractice.oanda.com")


def has_key() -> bool:
    return bool(os.environ.get("OANDA_API_KEY"))


def parse_candles(payload: dict) -> list[dict]:
    """OANDA candles (price=MBA) -> rows. Only completed daily candles."""
    out = []
    for cd in payload.get("candles", []):
        if not cd.get("complete"):
            continue
        try:
            m, b, a = cd["mid"], cd["bid"], cd["ask"]
            out.append({
                "date": cd["time"][:10],
                "o": float(m["o"]), "h": float(m["h"]), "l": float(m["l"]), "c": float(m["c"]),
                "spread": max(float(a["c"]) - float(b["c"]), 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_daily(pair: str, count: int = 5000) -> list[dict]:
    key = os.environ.get("OANDA_API_KEY")
    if not key:
        return []
    r = requests.get(
        f"{_base()}/v3/instruments/{pair}/candles",
        headers={"Authorization": f"Bearer {key}"},
        params={"granularity": "D", "count": min(count, 5000), "price": "MBA"},
        timeout=30,
    )
    r.raise_for_status()
    return parse_candles(r.json())


def save_bars(pair: str, rows: list[dict]) -> int:
    with get_db() as conn:
        conn.execute(_DDL)
        for x in rows:
            conn.execute("INSERT OR REPLACE INTO fx_daily_bars (pair,date,o,h,l,c,spread) VALUES (?,?,?,?,?,?,?)",
                         (pair, x["date"], x["o"], x["h"], x["l"], x["c"], x["spread"]))
    return len(rows)


def backfill(pairs=None) -> dict:
    res = {}
    for p in (pairs or list(PAIRS)):
        try:
            rows = fetch_daily(p)
            res[p] = {"saved": save_bars(p, rows) if rows else 0}
            if not rows:
                res[p]["note"] = "no candles (missing key or empty response)"
        except Exception as e:
            res[p] = {"error": str(e)[:200]}
    return res


def load_bars(pair: str):
    import pandas as pd
    with get_db() as conn:
        conn.execute(_DDL)
        rows = conn.execute("SELECT date,o,h,l,c,spread FROM fx_daily_bars WHERE pair=? ORDER BY date", (pair,)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def status() -> dict:
    with get_db() as conn:
        conn.execute(_DDL)
        rows = conn.execute("SELECT pair, COUNT(*) n, MIN(date) first, MAX(date) last FROM fx_daily_bars GROUP BY pair").fetchall()
    return {"key_set": has_key(), "mode": os.environ.get("OANDA_ENV", "practice"), "pairs": [dict(r) for r in rows]}
