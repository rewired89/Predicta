"""
AI orchestration layer for soccer.
Parses match queries and generates prediction narratives, using a
soccer-specific schema (league, season, sportsbook odds) instead of the
generic ai_agent.parse_query.
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

PARSE_SYSTEM = """You extract structured soccer match information from a user query.
Return ONLY valid JSON with these keys:
  home_team       — home side (or first team mentioned if home/away unclear)
  away_team       — away side
  league          — one of: EPL, La_liga, Bundesliga, Serie_A, Ligue_1, RFPL, or null if unknown
  season          — season start year as int (e.g. 2025 for 2025/26), null if not stated
  date            — ISO date YYYY-MM-DD, or null to mean today
  neutral         — true if the match is on a neutral ground (cup final etc.), else false
  odds_home_decimal — decimal odds for home win, e.g. 1.85, null if not in query
  odds_draw_decimal — decimal odds for draw, null if not in query
  odds_away_decimal — decimal odds for away win, null if not in query
  odds_home_american — American odds for home if stated as +/- integer, null otherwise
  odds_draw_american — American odds for draw, null otherwise
  odds_away_american — American odds for away, null otherwise
  notes           — any extra context (injuries, weather, suspensions)

League slugs map common names: "Premier League"→EPL, "La Liga"→La_liga,
"Bundesliga"→Bundesliga, "Serie A"→Serie_A, "Ligue 1"→Ligue_1.

Examples of odds in queries:
  "Arsenal -130 vs Spurs +280, draw +250"   → home_american=-130, draw_american=250, away_american=280
  "Bayern 1.45 / X 4.30 / Dortmund 6.50"    → home_decimal=1.45, draw_decimal=4.30, away_decimal=6.50
  "Real Madrid vs Atletico Saturday"         → all odds null

No markdown, no prose — raw JSON only."""


def parse_soccer_query(user_text: str) -> dict:
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
# NOTE: The previous Claude-based xG fallback (interpret_soccer_signals_fallback)
# was removed in Round 3 — for betting use, hallucinating signals from training
# data is unsafe. The pipeline now returns {"status": "insufficient_data"} when
# both Understat and FBref are unreachable. Reintroduce only as a separate
# explicit /research-mode endpoint if needed in the future.


NARRATIVE_SYSTEM = """You are a soccer prediction analyst. Write a clear, confident
2-3 sentence prediction summary based on the model's outputs.

Rules:
- Open with the favourite team and their win probability (as a percentage).
- Mention the most decisive driver (xG edge, GK quality, recent form swing, set-piece advantage).
- If a verdict ("BET" / "PASS") is supplied, surface it.
- End with a brief uncertainty caveat tied to data confidence.
- No emojis, no hype, under 90 words total."""


def generate_soccer_narrative(
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
