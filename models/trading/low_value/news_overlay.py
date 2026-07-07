"""
Low Value engine — news layer (Kimi review round 6 follow-up).

Pulls last-7-day company news from Finnhub for each symbol in the daily
universe, flags category keywords in headlines, and runs VADER sentiment
on article summaries. Read-only enrichment — flags/sentiment feed into
thesis_tracker.py's composite score, never gate the universe scan itself.

Fails safe throughout: missing FINNHUB_API_KEY or a vaderSentiment import
failure both degrade to "no signal" (empty flags, sentiment 0.0), never a
crash — the Low Value engine must never block on a data-source hiccup any
more than the High Value engine does.
"""
from __future__ import annotations
from typing import Optional

from fetchers.finnhub import fetch_news_batch

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _analyzer = SentimentIntensityAnalyzer()
    _HAS_VADER = True
except ImportError:
    _analyzer = None
    _HAS_VADER = False

NEWS_FLAG_KEYWORDS: dict[str, list[str]] = {
    "EARNINGS_MISS":        ["miss", "misses", "below estimate", "guidance cut"],
    "ANALYST_DOWNGRADE":    ["downgrade", "cut to sell", "price target lowered"],
    "REGULATORY_RISK":      ["sec investigation", "fda rejects", "lawsuit", "fraud"],
    "OPERATIONAL_CRISIS":   ["layoff", "layoffs", "restructuring", "bankruptcy filing"],
    "POSITIVE_CATALYST":    ["contract win", "partnership", "fda approval", "breakthrough"],
}


def _flag_headline(headline: str) -> list[str]:
    """Case-insensitive keyword match against NEWS_FLAG_KEYWORDS. A headline can carry multiple flags."""
    h = (headline or "").lower()
    return [flag for flag, keywords in NEWS_FLAG_KEYWORDS.items() if any(kw in h for kw in keywords)]


def _sentiment_score(text: str) -> float:
    """VADER compound sentiment (-1..+1). Returns 0.0 (neutral) if vaderSentiment isn't installed."""
    if not _HAS_VADER or not text:
        return 0.0
    try:
        return round(_analyzer.polarity_scores(text)["compound"], 4)
    except Exception:
        return 0.0


def score_symbol_news(articles: list[dict]) -> dict:
    """
    Aggregates one symbol's news articles (Finnhub company-news records) into
    flags + a single sentiment score.

    Returns {flags: list[str] (deduped, order-preserving), sentiment: float
    (-1..+1, mean compound score across articles, 0.0 if no articles),
    headline_count: int}.
    """
    if not articles:
        return {"flags": [], "sentiment": 0.0, "headline_count": 0}

    seen_flags: list[str] = []
    sentiments: list[float] = []
    for art in articles:
        headline = art.get("headline", "")
        summary  = art.get("summary", "") or headline
        for flag in _flag_headline(headline):
            if flag not in seen_flags:
                seen_flags.append(flag)
        sentiments.append(_sentiment_score(summary))

    avg_sentiment = round(sum(sentiments) / len(sentiments), 4) if sentiments else 0.0
    return {"flags": seen_flags, "sentiment": avg_sentiment, "headline_count": len(articles)}


def scan_universe_news(symbols: list[str], days: int = 7) -> dict[str, dict]:
    """
    Fetches + scores news for every symbol in the Low Value universe in one
    rate-limited batch (fetch_news_batch handles the 60-calls/min Finnhub
    free-tier throttle). Returns {symbol: score_symbol_news() result}.
    """
    news_by_symbol = fetch_news_batch(symbols, days=days)
    return {sym: score_symbol_news(articles) for sym, articles in news_by_symbol.items()}
