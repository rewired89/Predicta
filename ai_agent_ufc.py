"""
AI orchestration layer for UFC.
Parses matchup queries and generates prediction narratives, mirroring
ai_agent_soccer.py / ai_agent_rugby.py. UFC has no home/away or league
concept (neutral venue, single promotion), so the schema is simpler —
but it needs a method-of-victory odds slot that team sports don't have.
"""
from __future__ import annotations
import json
import re

import anthropic
from ai_client import get_client

MODEL = "claude-haiku-4-5-20251001"


def _client() -> anthropic.Anthropic:
    return get_client()


PARSE_SYSTEM = """You extract structured UFC matchup information from a user query.
Return ONLY valid JSON with these keys:
  fighter_a       — first fighter mentioned
  fighter_b       — second fighter mentioned
  weight_class    — e.g. "Lightweight", "Welterweight", "Heavyweight", null if not stated
  date            — ISO date YYYY-MM-DD, or null to mean today/next event
  odds_a_decimal  — decimal moneyline odds for fighter_a, null if not in query
  odds_b_decimal  — decimal moneyline odds for fighter_b, null if not in query
  odds_a_american — American moneyline odds for fighter_a, null otherwise
  odds_b_american — American moneyline odds for fighter_b, null otherwise
  notes           — any extra context (layoff, weight cut issues, injury)

Examples:
  "Islam Makhachev -450 vs Dustin Poirier +350"  → odds_a_american=-450, odds_b_american=350
  "Jones 1.25 / Aspinall 3.80 heavyweight"        → odds_a_decimal=1.25, odds_b_decimal=3.80, weight_class=Heavyweight
  "Pereira vs Ankalaev at UFC 313"                → all odds null

No markdown, no prose — raw JSON only."""


def parse_ufc_query(user_text: str) -> dict:
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=384,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


NARRATIVE_SYSTEM = """You are a UFC/MMA prediction analyst. Write a clear, confident
2-3 sentence prediction summary based on the model's outputs.

Rules:
- Open with the favoured fighter and their win probability (as a percentage).
- Mention the most decisive driver (striking differential, takedown edge, Glicko-2 rating gap).
- Mention the most likely method of victory if one method is clearly favoured (>40%).
- If a verdict ("BET" / "LEAN" / "PASS") is supplied, surface it.
- End with a brief uncertainty caveat tied to data confidence.
- No emojis, no hype, under 90 words total."""


def generate_ufc_narrative(
    fighter_a: str,
    fighter_b: str,
    prob_a: float,
    prob_b: float,
    explanation: str,
    verdict: str = "",
    confidence: str = "medium",
) -> str:
    client = _client()
    prompt = f"""
Matchup: {fighter_a} vs {fighter_b}
Model: {fighter_a} {prob_a*100:.1f}%, {fighter_b} {prob_b*100:.1f}%
Engine notes: {explanation}
Verdict: {verdict or "n/a"}
Data confidence: {confidence}

Write the narrative."""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=220,
        system=NARRATIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
