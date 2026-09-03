"""
AI layer for Portfolio Watch (added 2026-07-19, direct user request).

Unlike ai_agent_trading.py's generate_trade_narrative() — which explains a
SYSTEMATIC signal score High Value already computed — this synthesizes real
news into a plain-English read for a stock the user already owns/watches,
with no systematic score behind it at all. Deliberately QUALITATIVE ONLY: no
numeric "sell X%" is ever asked for or parsed. Unlike High Value/Low Value,
there is no backtestable formula to anchor a percentage to here — a number
generated from news sentiment would be fabricated precision, not a real
signal, which is exactly the kind of guess this codebase has spent this
whole audit trying to stop making elsewhere. See portfolio_watch4kimi.md
(when it exists) for the fuller reasoning.
"""
from __future__ import annotations
import json
import re
import anthropic
from ai_client import get_client

MODEL = "claude-sonnet-5"

REVIEW_SYSTEM = """You are reviewing ONE stock for a personal investor who already
owns or is watching it. You will be given the company name, its current price and
today's change, and real recent news headlines/summaries (or a note that none were
found).

Your job: read the news and decide whether what's happening looks TEMPORARY
(a one-off event, priced in quickly, not a sign of an ongoing problem) or
STRUCTURAL (an ongoing issue that could keep pressuring the stock).

Respond with ONLY a JSON object, no markdown fences, no other text:
{
  "label": one of "HOLD" | "WATCH_CLOSELY" | "TRIM_CANDIDATE",
  "reasoning": "2-3 plain-English sentences, no jargon, citing the SPECIFIC news you were given (or stating clearly that no notable news was found and the move looks like normal daily fluctuation)"
}

Rules:
- NEVER include a percentage, a dollar target, or any numeric recommendation of
  any kind — you have no position size, no cost basis, and no systematic
  backtested signal to base one on. A number here would be a guess dressed up
  as precision. Say "hold," "watch," or "consider trimming" — never a percent.
- "HOLD": no notable news, or the news is neutral/positive, or a negative move
  reads as a clear one-off (e.g. broad market pullback, a single analyst note)
  that doesn't suggest an ongoing problem.
- "WATCH_CLOSELY": there's a real, specific negative development, but it's
  recent/unresolved enough that whether it's temporary or structural isn't
  clear yet.
- "TRIM_CANDIDATE": the news points to a real, likely-ongoing structural
  problem (e.g. repeated guidance cuts, a widening regulatory/legal issue,
  a structural shift in the business) — still phrased as "worth considering,"
  never as an instruction.
- If no news was found at all, say so directly and default to HOLD — absence
  of news is not evidence of a problem.
- Always end reasoning with exactly this sentence: "This is not financial
  advice — for your own research only."
"""


def _client() -> anthropic.Anthropic:
    return get_client()


def generate_portfolio_review(
    symbol: str,
    company_name: str,
    price_data: dict,
    news: list[dict],
) -> dict:
    """
    Returns {"label": "HOLD"|"WATCH_CLOSELY"|"TRIM_CANDIDATE", "reasoning": str}.
    Falls back to a safe HOLD/no-data response on any parse or API failure —
    never raises, never fabricates a label from nothing.
    """
    price = price_data.get("price", 0)
    change_pct = price_data.get("change_pct", 0)

    if news:
        news_lines = "\n".join(
            f"- {n.get('headline', '')} ({n.get('source', '?')}, "
            f"{n.get('datetime_str', 'recent')}): {n.get('summary', '')[:280]}"
            for n in news[:8]
        )
    else:
        news_lines = "No recent news found for this symbol in the last 7 days."

    prompt = f"""Symbol: {symbol} ({company_name or 'name unavailable'})
Current price: ${price:,.2f} ({change_pct:+.2f}% today)

Recent news:
{news_lines}

Write the review."""

    try:
        client = _client()
        msg = client.messages.create(
            model=MODEL,
            thinking={"type": "disabled"},
            max_tokens=300,
            system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)
        label = parsed.get("label", "HOLD")
        if label not in ("HOLD", "WATCH_CLOSELY", "TRIM_CANDIDATE"):
            label = "HOLD"
        return {
            "label": label,
            "reasoning": parsed.get("reasoning", "No reasoning returned."),
        }
    except Exception as exc:
        return {
            "label": "HOLD",
            "reasoning": (
                f"Could not generate a review for {symbol} right now ({exc}). "
                f"Defaulting to HOLD rather than guessing — try again shortly."
            ),
        }
