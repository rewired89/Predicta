"""
Portfolio Watch — on-demand news-based review for stocks a user already owns
or is watching (added 2026-07-19, direct user request).

Deliberately NOT a third systematic trading engine. High Value and Low Value
each have a backtestable formula behind every score; this has none — it
reads real news and gives a qualitative HOLD / WATCH_CLOSELY / TRIM_CANDIDATE
read with plain-English reasoning, and explicitly never outputs a numeric
"sell X%" (see ai_agent_portfolio.py's module docstring for why). Scoped to
individual stocks only, capped at 10 symbols per request, manual ticker entry
only for now (no image/screenshot upload yet — see CODEMAP.md).
"""
from __future__ import annotations
from datetime import datetime, timezone

from fetchers.alpaca import get_snapshot
from fetchers.finnhub import get_company_news, get_company_profile
from ai_agent_portfolio import generate_portfolio_review

MAX_SYMBOLS: int = 10


def _format_news(raw_news: list[dict]) -> list[dict]:
    """Finnhub's raw company-news fields -> what generate_portfolio_review expects."""
    formatted = []
    for n in raw_news:
        dt_str = "recent"
        ts = n.get("datetime")
        if ts:
            try:
                dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                dt_str = "recent"
        formatted.append({
            "headline": n.get("headline", ""),
            "summary": n.get("summary", ""),
            "source": n.get("source", "?"),
            "datetime_str": dt_str,
        })
    return formatted


def review_symbol(symbol: str) -> dict:
    """
    Full review for one symbol: live price snapshot, 7-day news, AI synthesis.
    Never raises — every fetcher here already fails safe (empty/None), and
    generate_portfolio_review has its own fallback for API failures.
    """
    symbol = symbol.strip().upper()
    snapshot = get_snapshot(symbol)
    if "error" in snapshot:
        return {
            "symbol": symbol,
            "found": False,
            "label": None,
            "reasoning": f"Could not find live price data for {symbol} — check the symbol is correct.",
        }

    profile = get_company_profile(symbol)
    company_name = profile.get("name") or symbol

    raw_news = get_company_news(symbol, days=7)
    news = _format_news(raw_news)

    review = generate_portfolio_review(symbol, company_name, snapshot, news)

    return {
        "symbol": symbol,
        "company_name": company_name,
        "found": True,
        "price": snapshot.get("price", 0),
        "change_pct": snapshot.get("change_pct", 0),
        "news_count": len(news),
        "label": review["label"],
        "reasoning": review["reasoning"],
    }


def review_watchlist(symbols: list[str]) -> dict:
    """
    Reviews up to MAX_SYMBOLS symbols. Extra symbols beyond the cap are
    dropped (not silently truncated without saying so — the caller sees
    truncated=True and how many were skipped).
    """
    cleaned = [s.strip().upper() for s in symbols if s and s.strip()]
    # de-dupe while preserving order
    seen = set()
    deduped = []
    for s in cleaned:
        if s not in seen:
            seen.add(s)
            deduped.append(s)

    truncated = len(deduped) > MAX_SYMBOLS
    to_review = deduped[:MAX_SYMBOLS]

    results = [review_symbol(sym) for sym in to_review]

    return {
        "requested": len(deduped),
        "reviewed": len(to_review),
        "truncated": truncated,
        "results": results,
    }
