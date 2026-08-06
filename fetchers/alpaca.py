"""
Alpaca Markets API wrapper.
Handles market data (bars, quotes, snapshots) and paper trading orders.
Set ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL in .env
"""
from __future__ import annotations
import os
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

DATA_BASE_URL = "https://data.alpaca.markets"
PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _get(url: str, params: dict = None) -> dict:
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        return {"error": str(e), "status_code": r.status_code}
    except Exception as e:
        return {"error": str(e)}


def _post(url: str, body: dict) -> dict:
    try:
        r = requests.post(url, headers=_headers(), json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        return {"error": str(e), "detail": detail}
    except Exception as e:
        return {"error": str(e)}


def friendly_order_error(result: dict) -> str:
    """
    Alpaca's HTTPError responses put a JSON body — e.g.
    {"code": 40310000, "message": "insufficient day trading buying power..."} —
    in result["detail"], a raw dict, not a string. Passed straight through
    to a caller (as a route's HTTPException detail, or a JSON {"error": ...}
    field), that became a literal "[object Object]" in the dashboard's alert
    dialog once the frontend tried to display it as text. Always returns a
    real string: Alpaca's own "message" field when detail is a dict, the raw
    detail otherwise, or the original exception text as a last resort.
    """
    detail = result.get("detail")
    if isinstance(detail, dict):
        return detail.get("message") or str(detail)
    if detail:
        return str(detail)
    return result.get("error", "Unknown Alpaca error")


def _delete(url: str) -> dict:
    try:
        r = requests.delete(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return {"status": "ok"}
    except requests.exceptions.HTTPError as e:
        return {"error": str(e), "status_code": r.status_code}
    except Exception as e:
        return {"error": str(e)}


def readable_order_error(order_result: dict) -> str:
    """
    Added 2026-08-06 — _post's {"error": ..., "detail": <Alpaca's raw JSON
    error body or plain text>} was being passed straight into HTTPException
    by execute_low_value_trade/execute_high_value_trade. When `detail` is
    Alpaca's JSON object (e.g. {"code": ..., "message": "qty must be
    integer..."}), that landed in the frontend as the literal string
    "[object Object]" — a real order rejection with no readable reason
    shown, which is exactly what made a genuinely-failing buy button look
    like it silently "did nothing." Pulls out Alpaca's own `message` field
    when present; falls back to the raw detail/error, always as a string.
    """
    detail = order_result.get("detail")
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"])
    if detail:
        return str(detail)
    return str(order_result.get("error", "Unknown order error."))


# ── Market data ───────────────────────────────────────────────────────────────

def get_bars(symbol: str, timeframe: str = "5Min", limit: int = 78) -> list[dict]:
    """
    Fetch OHLCV bars. timeframe: 1Min, 5Min, 15Min, 1Hour, 1Day.
    Default 78 bars = ~1 full trading day of 5-min candles.
    """
    url = f"{DATA_BASE_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "feed": "iex",
        "sort": "asc",
    }
    data = _get(url, params)
    if "error" in data:
        return []
    # Alpaca sometimes returns {"bars": null} (not a missing key) for a
    # symbol with no data in range — .get(key, default) only falls back on
    # a MISSING key, not a null value, so this crashed downstream callers
    # with "NoneType has no len()" on thinly-traded Low Value candidates
    # (fixed 2026-07-08; High Value's fixed watchlist never hit this since
    # its symbols always have bar data).
    bars = data.get("bars") or []
    return [
        {
            "t": b["t"],
            "o": b["o"],
            "h": b["h"],
            "l": b["l"],
            "c": b["c"],
            "v": b["v"],
            "vw": b.get("vw", b["c"]),  # vwap per bar
        }
        for b in bars
    ]


def get_daily_bars(symbol: str, days: int = 60) -> list[dict]:
    """Fetch daily OHLCV bars for swing/context analysis."""
    start = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    url = f"{DATA_BASE_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": "1Day",
        "start": start,
        "limit": days,
        "feed": "iex",
        "sort": "asc",
    }
    data = _get(url, params)
    if "error" in data:
        return []
    # See get_bars() above — Alpaca can return {"bars": null} explicitly,
    # which .get(key, default) does NOT catch (only a missing key does).
    return data.get("bars") or []


def get_snapshot(symbol: str) -> dict:
    """
    Get latest quote, trade, and daily bar for a symbol.
    Returns: price, change_pct, volume, prev_close, pre_market_price
    """
    url = f"{DATA_BASE_URL}/v2/stocks/{symbol}/snapshot"
    data = _get(url, {"feed": "iex"})
    if "error" in data:
        return {"error": data["error"], "symbol": symbol}

    lt = data.get("latestTrade", {})
    lq = data.get("latestQuote", {})
    db = data.get("dailyBar", {})
    pb = data.get("prevDailyBar", {})
    mb = data.get("minuteBar", {})

    price = lt.get("p") or mb.get("c") or db.get("c") or 0
    prev_close = pb.get("c", price)
    change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

    return {
        "symbol": symbol,
        "price": round(price, 4),
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 2),
        "open": db.get("o", 0),
        "high": db.get("h", 0),
        "low": db.get("l", 0),
        "volume": db.get("v", 0),
        "vwap_day": db.get("vw", 0),
        "bid": lq.get("bp", 0),
        "ask": lq.get("ap", 0),
    }


def get_snapshots(symbols: list[str]) -> dict[str, dict]:
    """Batch snapshot for multiple symbols (up to 1000)."""
    if not symbols:
        return {}
    url = f"{DATA_BASE_URL}/v2/stocks/snapshots"
    params = {"symbols": ",".join(symbols), "feed": "iex"}
    data = _get(url, params)
    if "error" in data:
        return {}
    result = {}
    for sym, snap in data.items():
        lt = snap.get("latestTrade", {})
        pb = snap.get("prevDailyBar", {})
        db = snap.get("dailyBar", {})
        price = lt.get("p") or db.get("c") or 0
        prev_close = pb.get("c", price)
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
        result[sym] = {
            "symbol": sym,
            "price": round(price, 4),
            "prev_close": round(prev_close, 4),
            "change_pct": round(change_pct, 2),
            "open": db.get("o", 0),
            "high": db.get("h", 0),
            "low": db.get("l", 0),
            "volume": db.get("v", 0),
            "vwap_day": db.get("vw", 0),
        }
    return result


def get_top_movers(limit: int = 20) -> dict:
    """Get top gainers and losers for today (market movers endpoint)."""
    url = f"{DATA_BASE_URL}/v1beta1/screener/stocks/movers"
    params = {"top": limit}
    data = _get(url, params)
    if "error" in data:
        return {"gainers": [], "losers": [], "error": data["error"]}
    return {
        "gainers": data.get("gainers", []),
        "losers": data.get("losers", []),
    }


def get_most_active(limit: int = 20) -> list[dict]:
    """Get most active stocks by volume today."""
    url = f"{DATA_BASE_URL}/v1beta1/screener/stocks/most-actives"
    params = {"top": limit, "by": "volume"}
    data = _get(url, params)
    if "error" in data:
        return []
    return data.get("most_actives", [])


def get_all_active_assets(asset_class: str = "us_equity") -> list[str]:
    """
    All tradable, active US-equity symbols on Alpaca (v2/assets), excluding
    OTC (Low Value universe scanner needs a listed-exchange starting point —
    OTC tickers have unreliable data and are excluded by the scanner anyway).
    Returns [] on any API error (fails safe, same convention as every other
    fetcher in this module).
    """
    url = f"{PAPER_BASE_URL}/v2/assets"
    params = {"status": "active", "asset_class": asset_class}
    data = _get(url, params)
    if isinstance(data, dict) and "error" in data:
        return []
    if not isinstance(data, list):
        return []
    return [
        a["symbol"] for a in data
        if a.get("tradable") and a.get("exchange") != "OTC" and a.get("symbol")
    ]


def get_asset_shortability(symbol: str) -> dict:
    """
    Alpaca's per-symbol asset record (v2/assets/{symbol}) includes real
    `shortable`/`easy_to_borrow` booleans — whether the stock can be shorted
    at all, and whether it's cheap/easy to borrow vs. hard-to-borrow.

    Added 2026-07-17 (Tier 2 of the trading-model audit — CODEMAP.md). Low
    Value's whole universe (sub-$20, thinly-covered names) is exactly the
    kind of stock that's frequently NOT shortable, and nothing previously
    checked this before logging a hypothetical short — meaning some of Low
    Value's own "short" trade history could represent trades that could
    never actually have been placed in real life. Unlike a signal WEIGHT
    (a guess this audit has been trying not to make more of), shortability
    is a hard, binary tradability fact — Alpaca either will or won't let you
    short a stock — so this is a legitimate gate, not another guessed
    threshold.

    Fails safe to {"shortable": None, "easy_to_borrow": None} on any error
    or missing field — "unknown," never a false assumption in either
    direction — same convention as every other fetcher in this module.
    """
    url = f"{PAPER_BASE_URL}/v2/assets/{symbol}"
    data = _get(url)
    if not isinstance(data, dict) or "error" in data:
        return {"shortable": None, "easy_to_borrow": None}
    return {
        "shortable": data.get("shortable"),
        "easy_to_borrow": data.get("easy_to_borrow"),
    }


def get_asset_fractionability(symbol: str) -> dict:
    """
    Added 2026-08-06 — real bug: Low Value's fixed $25/trade sizing produces
    a fractional share count for virtually every symbol, and Alpaca only
    accepts fractional orders for assets where `fractionable: true` on the
    asset record (v2/assets/{symbol}) — most small/thinly-covered names,
    exactly Low Value's universe, are NOT on that list. execute_low_value_trade
    was submitting the raw fractional qty regardless, so Alpaca silently
    rejected the order for any non-fractionable symbol (confirmed live: a
    user's BUY worked for one candidate and did nothing — no visible
    error — for the next two). Also surfaces `tradable` so a caller can
    distinguish "not fractionable" from "not tradable at all right now."

    Fails safe to {"fractionable": None, "tradable": None} on any error —
    "unknown," never a false assumption — same convention as
    get_asset_shortability right above.
    """
    url = f"{PAPER_BASE_URL}/v2/assets/{symbol}"
    data = _get(url)
    if not isinstance(data, dict) or "error" in data:
        return {"fractionable": None, "tradable": None}
    return {
        "fractionable": data.get("fractionable"),
        "tradable": data.get("tradable"),
    }


# ── Paper trading orders ──────────────────────────────────────────────────────

def _assert_paper_mode() -> None:
    """Raise if PAPER_BASE_URL does not point to Alpaca paper trading endpoint."""
    if "paper" not in PAPER_BASE_URL:
        raise RuntimeError(
            f"Live trading not supported. PAPER_BASE_URL must contain 'paper' "
            f"(current: {PAPER_BASE_URL!r}). Check your configuration."
        )


def place_order(
    symbol: str,
    qty: float,
    side: str,           # "buy" or "sell"
    order_type: str,     # "market", "limit", "stop", "stop_limit"
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    time_in_force: str = "day",
    client_order_id: Optional[str] = None,
) -> dict:
    """Place a paper trading order."""
    _assert_paper_mode()
    body: dict = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if limit_price is not None:
        body["limit_price"] = str(round(limit_price, 2))
    if stop_price is not None:
        body["stop_price"] = str(round(stop_price, 2))
    if client_order_id:
        body["client_order_id"] = client_order_id

    url = f"{PAPER_BASE_URL}/v2/orders"
    return _post(url, body)


def place_bracket_order(
    symbol: str,
    qty: float,
    side: str,
    entry_price: Optional[float],   # None = market entry
    take_profit: float,
    stop_loss: float,
) -> dict:
    """
    Bracket order: entry + take_profit limit + stop_loss stop.
    Best way to set entry/target/stop in one shot.
    """
    _assert_paper_mode()
    body: dict = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit" if entry_price else "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(round(take_profit, 2))},
        "stop_loss": {"stop_price": str(round(stop_loss, 2))},
    }
    if entry_price:
        body["limit_price"] = str(round(entry_price, 2))

    url = f"{PAPER_BASE_URL}/v2/orders"
    return _post(url, body)


def get_orders(status: str = "open") -> list[dict]:
    """List paper trading orders. status: open, closed, all"""
    url = f"{PAPER_BASE_URL}/v2/orders"
    data = _get(url, {"status": status, "limit": 50})
    if isinstance(data, list):
        return data
    return []


def cancel_order(order_id: str) -> dict:
    url = f"{PAPER_BASE_URL}/v2/orders/{order_id}"
    return _delete(url)


def get_order(order_id: str) -> dict:
    """
    Fetch one order by id, including its current status and (for a bracket
    order) the current fill status of each child leg (legs[].status /
    legs[].filled_avg_price / legs[].filled_at) — the ground truth for
    whether a real paper position's stop or target actually filled, rather
    than re-deriving it from local bar data.
    """
    url = f"{PAPER_BASE_URL}/v2/orders/{order_id}"
    return _get(url)


def close_position(symbol: str) -> dict:
    """
    Liquidate an open paper position at market and cancel any open orders
    tied to it (Alpaca's own DELETE /v2/positions/:symbol — the safe way to
    force-flatten a real bracket-order position, e.g. at end of day or a
    time-stop, without manually reasoning about which bracket leg to cancel
    first).
    """
    _assert_paper_mode()
    url = f"{PAPER_BASE_URL}/v2/positions/{symbol}"
    return _delete(url)


def get_positions() -> list[dict]:
    """Get all open paper trading positions."""
    url = f"{PAPER_BASE_URL}/v2/positions"
    data = _get(url)
    if isinstance(data, list):
        return [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "side": p["side"],
                "avg_entry": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
                "market_value": float(p["market_value"]),
            }
            for p in data
        ]
    return []


def get_account() -> dict:
    """
    Get paper trading account summary.

    pattern_day_trader/daytrade_count/daytrading_buying_power were removed
    from Alpaca's /v2/account response (FINRA replaced the PDT rule with the
    intraday margin framework on 2026-06-04 — accounts are no longer
    classified as PDT at all). Per Alpaca's migration notice, buying_power
    is the replacement for all of them; the deprecated fields are no longer
    requested or returned here.
    """
    url = f"{PAPER_BASE_URL}/v2/account"
    data = _get(url)
    if "error" in data:
        return data
    return {
        "equity": float(data.get("equity", 0)),
        "cash": float(data.get("cash", 0)),
        "buying_power": float(data.get("buying_power", 0)),
        "portfolio_value": float(data.get("portfolio_value", 0)),
    }
