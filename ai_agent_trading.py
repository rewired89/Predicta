"""
AI layer for trading analysis.
Uses Claude to parse ticker queries and generate trade narratives.
"""
from __future__ import annotations
import os
import re
import json
import anthropic
from ai_client import get_client

MODEL = "claude-sonnet-5"


def _client() -> anthropic.Anthropic:
    return get_client()


PARSE_SYSTEM = """Extract the stock/crypto/ETF ticker symbol from a user query.
Return ONLY the ticker symbol in uppercase (e.g. AAPL, BTC-USD, SPY, TSLA).
No explanation, no markdown, just the ticker."""


def parse_trade_query(query: str) -> str:
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=20,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": query}],
    )
    return msg.content[0].text.strip().upper()


NARRATIVE_SYSTEM = """You are a quantitative trading analyst. Write a clear, concise 3-sentence
trading analysis based on the technical signals provided.

Rules:
- State the overall signal (bullish/bearish/neutral) and the composite score
- Name the 2 strongest reasons driving the signal (trend, RSI, momentum, volatility)
- Give a specific actionable note: entry zone, key level to watch, or risk warning
- Mention the expected move range for the next session
- Tone: analytical, precise. No hype, no emojis.
- Under 90 words total.
- End with: "This is not financial advice — paper mode only." """


def generate_trade_narrative(
    symbol: str,
    market_data: dict,
    signals: dict,
    kelly: dict,
) -> str:
    client = _client()

    price    = market_data.get("price", {})
    fund     = market_data.get("fundamentals", {})
    score    = signals.get("score", {})
    trend    = signals.get("trend", {})
    mom      = signals.get("momentum", {})
    vol      = signals.get("volatility", {})
    em       = signals.get("expected_move", {})
    sr       = signals.get("support_resistance", {})

    prompt = f"""
Ticker: {symbol} ({fund.get('name','')}) | {fund.get('sector','')} | {fund.get('asset_type','')}
Current price: ${price.get('current', 0):.4f} ({price.get('change_pct', 0):+.2f}% today)
Composite score: {score.get('value', 0)} / 100 → {score.get('label', 'Neutral')}
Score reasons: {', '.join(score.get('reasons', []))}

Trend: {trend.get('direction','?')} ({trend.get('strength',0):.0f}% strength)
RSI(14): {mom.get('rsi', 50)} — {mom.get('rsi_signal','neutral')}
5d return: {mom.get('roc_5d', 0):+.1f}% | 20d return: {mom.get('roc_20d', 0):+.1f}%
ATR: {vol.get('atr_pct', 0):.2f}% | Annual HV: {vol.get('hv_annual', 0):.1f}%
Expected move next session: ±{em.get('pct_1sigma', 0):.2f}% (1σ range ${em.get('lower_1sigma',0):.2f}–${em.get('upper_1sigma',0):.2f})
Support: ${sr.get('support', 0):.2f} | Resistance: ${sr.get('resistance', 0):.2f}
Kelly position: {kelly.get('note', 'N/A')}

Write the trading analysis."""

    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=220,
        system=NARRATIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
