"""
StockTwits fetcher — public, no-key symbol sentiment stream
(https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json).

Added 2026-09-11 after reviewing TauricResearch/TradingAgents (a multi-agent
LLM trading framework the user asked about) — its Sentiment Analyst role
rolls Reddit/StockTwits chatter into the trade decision. Adopted here as a
calibratable signal (models/trading/low_value/social_overlay.py) rather than
an LLM judgment call, matching this codebase's existing signal discipline.
Reddit has no equivalent free, structured, per-symbol sentiment endpoint
without its own API key and app-review process, so it's left out rather
than half-built.

Unauthenticated and rate-limited (~200 requests/hour per IP, StockTwits'
long-standing anonymous-access ceiling) — there's no API key to fail safe
without, so this fails safe on ANY error (network, 403, rate limit, bad
JSON) by returning an all-zero result, same discipline as every other
best-effort source in this codebase. Unverified from this sandbox (same
proxy-block limitation documented in CLAUDE.md for every external source
here) — treat the first live scan as the real test.
"""
from __future__ import annotations
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

STOCKTWITS_BASE_URL = "https://api.stocktwits.com/api/2"

# StockTwits' long-standing unauthenticated ceiling. Unlike Finnhub's
# 60-calls/min (which refills fast enough to sleep-and-continue mid-scan),
# an hourly window makes sleeping impractical for a scan that needs to
# finish in minutes — see fetch_social_batch, which caps rather than sleeps.
MAX_CALLS_PER_HOUR = 200

_EMPTY_RESULT = {"bullish": 0, "bearish": 0, "tagged_count": 0, "total_count": 0}


def get_symbol_sentiment(symbol: str, limit: int = 30) -> dict:
    """
    Aggregates the most recent `limit` public messages for `symbol` into a
    bullish/bearish tally. Only messages a user explicitly tagged
    (entities.sentiment.basic) count — untagged chatter is real noise, not
    a weak signal, so it's excluded rather than treated as neutral.

    Returns {bullish, bearish, tagged_count, total_count} — all zero on any
    failure (missing `requests`, network error, non-200, malformed JSON).
    Never raises.
    """
    if not _HAS_REQUESTS:
        return dict(_EMPTY_RESULT)
    try:
        r = requests.get(
            f"{STOCKTWITS_BASE_URL}/streams/symbol/{symbol}.json",
            params={"limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return dict(_EMPTY_RESULT)

    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return dict(_EMPTY_RESULT)

    bullish = bearish = 0
    for msg in messages:
        basic = ((msg.get("entities") or {}).get("sentiment") or {}).get("basic")
        if basic == "Bullish":
            bullish += 1
        elif basic == "Bearish":
            bearish += 1

    return {
        "bullish": bullish,
        "bearish": bearish,
        "tagged_count": bullish + bearish,
        "total_count": len(messages),
    }


def fetch_social_batch(symbols: list[str], max_calls: int = MAX_CALLS_PER_HOUR) -> dict[str, dict]:
    """
    Batch version, capped at MAX_CALLS_PER_HOUR calls per invocation.
    Symbols beyond the cap simply get no result this scan — treated exactly
    like any other unavailable source (missing signal, excluded and
    reweighted, never faked) rather than blocking a whole scan for up to an
    hour over one 5%-weighted signal.
    """
    results: dict[str, dict] = {}
    for i, symbol in enumerate(symbols):
        if i >= max_calls:
            break
        results[symbol] = get_symbol_sentiment(symbol)
    return results
