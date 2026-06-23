"""
Market data fetcher using yfinance (free, no API key required).
Fetches OHLCV, fundamentals, and options data for stocks, ETFs, crypto, forex.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional
import yfinance as yf
import pandas as pd


def fetch_ticker(symbol: str, period: str = "3mo") -> dict:
    """
    Fetch comprehensive market data for a ticker symbol.
    Returns OHLCV history, fundamentals, and metadata.
    period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y
    """
    sources = []
    result = {"symbol": symbol.upper(), "sources": sources}

    try:
        ticker = yf.Ticker(symbol)

        # ── Price history ─────────────────────────────────────────────────────
        hist = ticker.history(period=period)
        if hist.empty:
            result["error"] = f"No price data found for {symbol}"
            return result

        result["history"] = [
            {
                "date":   str(idx.date()),
                "open":   round(float(row["Open"]), 4),
                "high":   round(float(row["High"]), 4),
                "low":    round(float(row["Low"]), 4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            }
            for idx, row in hist.iterrows()
        ]
        sources.append({
            "label": f"{symbol} price history ({period})",
            "url": f"https://finance.yahoo.com/quote/{symbol}",
            "snippet": f"{len(result['history'])} trading days fetched",
        })

        # ── Current price snapshot ────────────────────────────────────────────
        latest = hist.iloc[-1]
        prev   = hist.iloc[-2] if len(hist) > 1 else latest
        result["price"] = {
            "current":  round(float(latest["Close"]), 4),
            "open":     round(float(latest["Open"]), 4),
            "high":     round(float(latest["High"]), 4),
            "low":      round(float(latest["Low"]), 4),
            "prev_close": round(float(prev["Close"]), 4),
            "change":   round(float(latest["Close"] - prev["Close"]), 4),
            "change_pct": round(float((latest["Close"] - prev["Close"]) / prev["Close"] * 100), 2),
            "volume":   int(latest["Volume"]),
        }

        # ── Fundamentals ──────────────────────────────────────────────────────
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            pass

        result["fundamentals"] = {
            "name":            info.get("longName") or info.get("shortName", symbol),
            "sector":          info.get("sector", ""),
            "industry":        info.get("industry", ""),
            "market_cap":      info.get("marketCap"),
            "pe_ratio":        info.get("trailingPE"),
            "forward_pe":      info.get("forwardPE"),
            "eps":             info.get("trailingEps"),
            "revenue_growth":  info.get("revenueGrowth"),
            "profit_margins":  info.get("profitMargins"),
            "debt_to_equity":  info.get("debtToEquity"),
            "52w_high":        info.get("fiftyTwoWeekHigh"),
            "52w_low":         info.get("fiftyTwoWeekLow"),
            "avg_volume":      info.get("averageVolume"),
            "beta":            info.get("beta"),
            "dividend_yield":  info.get("dividendYield"),
            "short_ratio":     info.get("shortRatio"),
            "analyst_target":  info.get("targetMeanPrice"),
            "currency":        info.get("currency", "USD"),
            "exchange":        info.get("exchange", ""),
            "asset_type":      _detect_asset_type(info, symbol),
        }
        sources.append({
            "label": f"{symbol} fundamentals",
            "url": f"https://finance.yahoo.com/quote/{symbol}/financials",
            "snippet": f"Sector: {result['fundamentals']['sector'] or 'N/A'} | "
                       f"P/E: {result['fundamentals']['pe_ratio'] or 'N/A'} | "
                       f"Beta: {result['fundamentals']['beta'] or 'N/A'}",
        })

        # ── Moving averages (pre-computed for efficiency) ─────────────────────
        closes = hist["Close"]
        result["moving_averages"] = {}
        for window in [20, 50, 200]:
            if len(closes) >= window:
                result["moving_averages"][f"ma{window}"] = round(float(closes.rolling(window).mean().iloc[-1]), 4)

        # ── Analyst recommendations ───────────────────────────────────────────
        try:
            recs = ticker.recommendations
            if recs is not None and not recs.empty:
                latest_rec = recs.iloc[-1]
                result["analyst"] = {
                    "rating": str(latest_rec.get("To Grade", "")),
                    "firm":   str(latest_rec.get("Firm", "")),
                }
        except Exception:
            pass

    except Exception as exc:
        result["error"] = str(exc)
        sources.append({"label": symbol, "url": "", "snippet": f"ERROR: {exc}"})

    return result


def _detect_asset_type(info: dict, symbol: str) -> str:
    qt = info.get("quoteType", "").upper()
    if qt == "CRYPTOCURRENCY":
        return "crypto"
    if qt == "ETF":
        return "etf"
    if qt == "MUTUALFUND":
        return "fund"
    if qt == "CURRENCY" or "=X" in symbol:
        return "forex"
    if qt == "FUTURE" or symbol.endswith("=F"):
        return "futures"
    return "stock"


def search_ticker(query: str) -> list[dict]:
    """Best-effort ticker search — returns top matches for a company name."""
    try:
        results = yf.Search(query, max_results=5)
        quotes = results.quotes if hasattr(results, "quotes") else []
        return [
            {
                "symbol":   q.get("symbol", ""),
                "name":     q.get("longname") or q.get("shortname", ""),
                "exchange": q.get("exchange", ""),
                "type":     q.get("quoteType", ""),
            }
            for q in quotes
        ]
    except Exception:
        return []
