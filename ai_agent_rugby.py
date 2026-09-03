"""
AI orchestration layer for rugby (NRL).
Parses match queries and generates prediction narratives, mirroring
ai_agent_soccer.py's structure. NRL has no meaningful league/season
disambiguation the way soccer does (single competition), so the schema
is simpler.
"""
from __future__ import annotations
import json
import re

import anthropic
from ai_client import get_client

MODEL = "claude-sonnet-5"


def _client() -> anthropic.Anthropic:
    return get_client()


# ── 1. Parse query ────────────────────────────────────────────────────────────

PARSE_SYSTEM = """You extract structured NRL (rugby league) match information from a user query.
Return ONLY valid JSON with these keys:
  home_team       — home side (or first team mentioned if home/away unclear)
  away_team       — away side
  date            — ISO date YYYY-MM-DD, or null to mean today
  odds_home_decimal — decimal odds for home win, e.g. 1.85, null if not in query
  odds_away_decimal — decimal odds for away win, null if not in query
  odds_home_american — American odds for home if stated as +/- integer, null otherwise
  odds_away_american — American odds for away, null otherwise
  handicap_line   — the points handicap line if stated (e.g. "Rabbitohs -8.5" → -8.5 assigned to whichever team it's attached to; put the home team's line here, negate if it was stated for the away team), null if not in query
  total_line      — total points line if stated (e.g. "over 42.5"), null if not in query
  notes           — any extra context (injuries, suspensions, wet weather)

Team names are NRL clubs — common short names map to full names: "Rabbitohs"/"Souths"→South Sydney Rabbitohs,
"Roosters"→Sydney Roosters, "Storm"→Melbourne Storm, "Broncos"→Brisbane Broncos, "Knights"→Newcastle Knights,
"Panthers"→Penrith Panthers, "Eels"→Parramatta Eels, "Sharks"→Cronulla Sharks, "Bulldogs"→Canterbury Bulldogs,
"Tigers"→Wests Tigers, "Raiders"→Canberra Raiders, "Cowboys"→North Queensland Cowboys, "Titans"→Gold Coast Titans,
"Dragons"→St George Illawarra Dragons, "Sea Eagles"/"Manly"→Manly Sea Eagles, "Warriors"→New Zealand Warriors,
"Dolphins"→Redcliffe Dolphins.

Examples of odds in queries:
  "Rabbitohs -130 vs Knights +110"       → home_american=-130, away_american=110
  "Storm 1.45 / Broncos 2.60"            → home_decimal=1.45, away_decimal=2.60
  "Panthers -8.5 over Eels"              → handicap_line=-8.5
  "Roosters vs Sharks Friday night"      → all odds/lines null

No markdown, no prose — raw JSON only."""


def parse_rugby_query(user_text: str) -> dict:
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=384,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── 2. Narrative ──────────────────────────────────────────────────────────────

NARRATIVE_SYSTEM = """You are an NRL (rugby league) prediction analyst. Write a clear, confident
2-3 sentence prediction summary based on the model's outputs.

Rules:
- Open with the favourite team and their win probability (as a percentage).
- Mention the most decisive driver (points-for/against edge, recent form, home advantage).
- If a verdict ("BET" / "LEAN" / "PASS") is supplied, surface it.
- End with a brief uncertainty caveat tied to data confidence.
- No emojis, no hype, under 90 words total."""


def generate_rugby_narrative(
    home_team: str,
    away_team: str,
    prob_home: float,
    prob_draw: float,
    prob_away: float,
    explanation: str,
    verdict: str = "",
    confidence: str = "medium",
) -> str:
    client = _client()
    prompt = f"""
Match: {home_team} (home) vs {away_team} (away)
Model: home {prob_home*100:.1f}%, draw {prob_draw*100:.1f}%, away {prob_away*100:.1f}%
Engine notes: {explanation}
Verdict: {verdict or "n/a"}
Data confidence: {confidence}

Write the narrative."""
    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=220,
        system=NARRATIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
