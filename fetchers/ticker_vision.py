"""
Extracts stock/ETF ticker symbols from an uploaded image (a watchlist
screenshot, a photo of a stock list, etc.) using Claude's vision
capability — the server-side counterpart to a human reading tickers off a
photo by eye. Reuses the same Anthropic client/model convention as
ai_agent_trading.py.
"""
from __future__ import annotations
import base64
import json
import re

from ai_client import get_client

MODEL = "claude-haiku-4-5-20251001"

EXTRACT_SYSTEM = """You extract stock/ETF ticker symbols from images of trading apps, watchlists, or stock lists.
Return ONLY a JSON array of uppercase ticker symbols, nothing else — no markdown fences, no explanation.
Example: ["AAPL", "TSLA", "SPY"]
If you see no tickers, return []."""


def extract_tickers_from_image(image_bytes: bytes, media_type: str) -> list[str]:
    """
    Returns a deduped, uppercase list of ticker symbols Claude found in the
    image. Never raises on a parsing hiccup — falls back to a regex scan of
    the model's raw text for plausible ticker-shaped tokens (1-5 uppercase
    letters) rather than returning nothing just because the model didn't
    return clean JSON this time.
    """
    client = get_client()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=EXTRACT_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Extract every stock/ETF ticker symbol visible in this image."},
            ],
        }],
    )
    raw = msg.content[0].text.strip()
    try:
        tickers = json.loads(raw)
        if isinstance(tickers, list):
            return sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    except (json.JSONDecodeError, ValueError):
        pass
    candidates = re.findall(r"\b[A-Z]{1,5}\b", raw)
    return sorted(set(candidates))
