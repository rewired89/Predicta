"""
AI orchestration layer for tennis.
Uses Claude to parse queries and generate prediction narratives.
"""
from __future__ import annotations
import json
import os
import re

import anthropic
from ai_client import get_client

MODEL = "claude-sonnet-5"


def _client() -> anthropic.Anthropic:
    return get_client()


# ── 1. Parse query ────────────────────────────────────────────────────────────

PARSE_SYSTEM = """You extract structured tennis match information from a user query.
Return ONLY valid JSON with these keys:
  player_a  — first player mentioned (or favourite if clear)
  player_b  — second player mentioned
  surface   — "clay", "grass", "hard", or null if unknown
  tour      — "atp" or "wta" (infer from players; default "atp" if unclear)
  date      — ISO date YYYY-MM-DD, or null to mean today
  notes     — any extra context (tournament name, round, injuries, head-to-head)
No markdown, no prose — raw JSON only."""


def parse_tennis_query(user_text: str) -> dict:
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=256,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── 2. Fallback signal estimation (when ESPN/TSDB is unreachable) ─────────────

SIGNALS_SYSTEM = """You are a tennis analytics assistant. Given two players, estimate the
key model inputs using your training knowledge (ATP/WTA rankings, recent form, surface records).

Return ONLY valid JSON with this exact schema:
{
  "player_a": {
    "ranking": <int — estimated ATP/WTA ranking, 999 if unknown>,
    "surface_win_rate": <float 0-1 — win rate on given surface, 0.5 if unknown>,
    "recent_form": <float 0-1 — last-5-match win rate, 0.5 if unknown>,
    "serve_quality_index": <int — 100=tour average, higher=better server>,
    "return_quality_index": <int — 100=tour average, higher=better returner>
  },
  "player_b": { <same keys> },
  "h2h_advantage": <"player_a"|"player_b"|"even">,
  "confidence": <"low"|"medium">,
  "notes": <string — what is from training knowledge vs live data>
}
serve_quality_index: 115=elite server (Isner/Opelka level), 85=weak server.
return_quality_index: 115=elite returner (Djokovic/Alcaraz level), 85=poor returner.
No markdown — raw JSON only."""


def interpret_tennis_signals(
    player_a: str,
    player_b: str,
    surface: str = "hard",
    tour: str = "atp",
    notes: str = "",
) -> dict:
    """Fallback when ESPN/TSDB is unreachable — Claude estimates signals from training knowledge."""
    client = _client()
    prompt = f"""
Matchup: {player_a} vs {player_b}
Surface: {surface or 'hard'}
Tour: {tour.upper()}
Extra context: {notes or 'none'}

Estimate tennis model signals for this matchup."""

    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=512,
        system=SIGNALS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── 3. Prediction narrative ───────────────────────────────────────────────────

NARRATIVE_SYSTEM = """You are a sharp tennis prediction analyst. Write a clear, confident 2–3 sentence
prediction summary.

Rules:
- State the favourite and their win probability as a percentage
- Lead with the most important factor: surface advantage, ranking gap, serve quality, or H2H record
- Mention the surface and why it favours (or doesn't favour) the favourite
- End with a one-sentence uncertainty caveat (injury, form variance, weather for outdoor, etc.)
- Tone: analytical, no hype. No emojis. Under 90 words total."""


def generate_tennis_narrative(
    player_a: str,
    player_b: str,
    prob_a: float,
    prob_b: float,
    explanation: str,
    context: dict,
) -> str:
    client = _client()

    sa = context.get("player_a", {})
    sb = context.get("player_b", {})
    surface = context.get("surface", "hard")
    h2h = context.get("h2h", {})
    h2h_str = (
        f"H2H: {player_a} {h2h.get('wins_a', 0)}-{h2h.get('wins_b', 0)} {player_b}"
        if h2h else "H2H: unknown"
    )

    prompt = f"""
Tennis matchup: {player_a} vs {player_b}
Surface: {surface}
Win probabilities: {player_a} {prob_a*100:.1f}%  {player_b} {prob_b*100:.1f}%
{player_a}: ranking #{sa.get('ranking','?')}, serve quality {sa.get('serve_quality_index','?')}, return quality {sa.get('return_quality_index','?')}, surface win rate {sa.get('surface_win_rate','?')}
{player_b}: ranking #{sb.get('ranking','?')}, serve quality {sb.get('serve_quality_index','?')}, return quality {sb.get('return_quality_index','?')}, surface win rate {sb.get('surface_win_rate','?')}
{h2h_str}
Model notes: {explanation}

Write the prediction narrative."""

    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=200,
        system=NARRATIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
