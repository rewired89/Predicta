"""
Trading analysis pipeline.
Takes a ticker symbol or natural language query → fetches data →
computes signals → AI narrative → returns full result dict.
"""
from __future__ import annotations
import env_loader  # noqa: F401 — loads .env on import
import re
import os
import json
import traceback

from fetchers.market_data import fetch_ticker, search_ticker
from models.trading.high_value.signals import compute_signals
from models.trading.shared.kelly import kelly_from_signals


def _parse_ticker(query: str) -> str:
    """Extract ticker symbol from natural language or return as-is."""
    query = query.strip().upper()
    # Already looks like a ticker (1-5 chars, letters only or with - or .)
    if re.match(r'^[A-Z]{1,5}(-[A-Z]{1,3})?$', query):
        return query
    if re.match(r'^[A-Z]{1,5}\.[A-Z]{1,2}$', query):
        return query
    # Try to extract a ticker from natural language using AI
    try:
        from ai_agent_trading import parse_trade_query
        return parse_trade_query(query)
    except Exception:
        # Fallback: take first word
        return query.split()[0].upper()


def run_trade_analysis(query: str, bankroll: float = 10000.0) -> dict:
    """
    Full trading analysis pipeline.
    Returns a dict ready to JSON-serialize and send to the frontend.
    """
    steps = []

    # ── Step 1: Parse query ───────────────────────────────────────────────────
    symbol = _parse_ticker(query)
    steps.append({"step": "parse", "status": "ok", "symbol": symbol})

    # ── Step 2: Fetch market data ─────────────────────────────────────────────
    try:
        market_data = fetch_ticker(symbol, period="6mo")
        if market_data.get("error"):
            # Try search if direct fetch failed
            matches = search_ticker(query)
            if matches:
                symbol = matches[0]["symbol"]
                market_data = fetch_ticker(symbol, period="6mo")
            if market_data.get("error"):
                return {"error": f"Could not fetch data for '{query}': {market_data['error']}", "steps": steps}
        steps.append({
            "step": "fetch",
            "status": "ok",
            "days": len(market_data.get("history", [])),
        })
    except Exception as exc:
        return {"error": f"Data fetch failed: {exc}", "steps": steps}

    # ── Step 3: Compute signals ───────────────────────────────────────────────
    signals = {}
    try:
        history = market_data.get("history", [])
        signals = compute_signals(history)
        steps.append({"step": "signals", "status": "ok",
                      "score": signals.get("score", {}).get("value")})
    except Exception as exc:
        steps.append({"step": "signals", "status": "error", "error": str(exc),
                      "trace": traceback.format_exc()})
        return {"error": f"Signal computation failed: {exc}", "steps": steps}

    # ── Step 4: Kelly position sizing ─────────────────────────────────────────
    kelly = {}
    try:
        score   = signals.get("score", {}).get("value", 0)
        atr_pct = signals.get("volatility", {}).get("atr_pct", 1.5)
        kelly   = kelly_from_signals(score, atr_pct, bankroll)
        steps.append({"step": "kelly", "status": "ok"})
    except Exception as exc:
        steps.append({"step": "kelly", "status": "error", "error": str(exc)})

    # ── Step 5: AI narrative ──────────────────────────────────────────────────
    narrative = ""
    try:
        from ai_agent_trading import generate_trade_narrative
        narrative = generate_trade_narrative(symbol, market_data, signals, kelly)
        steps.append({"step": "narrative", "status": "ok"})
    except Exception as exc:
        # Fallback narrative from signals
        score_label = signals.get("score", {}).get("label", "Neutral")
        price = market_data.get("price", {}).get("current", 0)
        narrative = (
            f"{symbol} is trading at ${price:.2f}. "
            f"Signal: {score_label}. "
            f"AI narrative unavailable: {exc}"
        )
        steps.append({"step": "narrative", "status": "error", "error": str(exc)})

    # ── Build result ──────────────────────────────────────────────────────────
    price      = market_data.get("price", {})
    fund       = market_data.get("fundamentals", {})
    score_obj  = signals.get("score", {})
    em         = signals.get("expected_move", {})
    sr         = signals.get("support_resistance", {})
    trend      = signals.get("trend", {})
    mom        = signals.get("momentum", {})
    vol        = signals.get("volatility", {})
    volume     = signals.get("volume", {})
    mas        = market_data.get("moving_averages", {})

    return {
        "symbol":        symbol,
        "name":          fund.get("name", symbol),
        "asset_type":    fund.get("asset_type", "stock"),
        "currency":      fund.get("currency", "USD"),
        "exchange":      fund.get("exchange", ""),
        "price":         price,
        "fundamentals":  fund,
        "moving_averages": mas,
        "trend":         trend,
        "momentum":      mom,
        "volatility":    vol,
        "volume":        volume,
        "expected_move": em,
        "support_resistance": sr,
        "score":         score_obj,
        "kelly":         kelly,
        "narrative":     narrative,
        "raw_sources":   market_data.get("sources", []),
        "history":       market_data.get("history", [])[-30:],  # last 30 days for chart
        "steps":         steps,
    }
