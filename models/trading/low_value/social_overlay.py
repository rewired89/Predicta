"""
Low Value engine — social sentiment layer (added 2026-09-11, see
fetchers/stocktwits.py's docstring for why this exists — the TradingAgents
review that prompted it).

Read-only enrichment, same shape as news_overlay.py: feeds
thesis_tracker.py's composite score as a 9th signal, never gates the
universe scan itself. Fails safe throughout — get_symbol_sentiment()
already returns an all-zero result on any StockTwits failure, so
score_symbol_social() just passes that through as "no signal."
"""
from __future__ import annotations
from typing import Optional

from fetchers.stocktwits import fetch_social_batch

# Below this many tagged (Bullish/Bearish) messages, the sample is too thin
# to mean anything — one bearish comment out of one tagged message would
# otherwise swing to a -100 score. Same "don't fake a signal from noise"
# discipline as thesis_tracker.py's other None-on-insufficient-data signals.
MIN_TAGGED_MESSAGES = 5


def score_symbol_social(social_result: Optional[dict]) -> dict:
    """
    Aggregates one symbol's StockTwits tally (fetchers.stocktwits.
    get_symbol_sentiment output) into a single sentiment score.

    Returns {sentiment: float (-1..+1, (bullish-bearish)/tagged_count),
    bullish: int, bearish: int, tagged_count: int}. tagged_count is forced
    to 0 (sentiment 0.0) when the real tagged count is below
    MIN_TAGGED_MESSAGES — thesis_tracker.py's _score_social_sentiment treats
    tagged_count == 0 as "no signal" (returns None, excluded from the
    composite), same as news_overlay.score_symbol_news's headline_count == 0
    case.
    """
    if not social_result:
        return {"sentiment": 0.0, "bullish": 0, "bearish": 0, "tagged_count": 0}

    bullish = social_result.get("bullish", 0)
    bearish = social_result.get("bearish", 0)
    tagged = social_result.get("tagged_count", 0)
    if tagged < MIN_TAGGED_MESSAGES:
        return {"sentiment": 0.0, "bullish": bullish, "bearish": bearish, "tagged_count": 0}

    sentiment = round((bullish - bearish) / tagged, 4)
    return {"sentiment": sentiment, "bullish": bullish, "bearish": bearish, "tagged_count": tagged}


def scan_universe_social(symbols: list[str]) -> dict[str, dict]:
    """Fetches + scores StockTwits sentiment for a Low Value/Automaton universe in one capped batch."""
    social_by_symbol = fetch_social_batch(symbols)
    return {sym: score_symbol_social(social_by_symbol.get(sym)) for sym in symbols}
