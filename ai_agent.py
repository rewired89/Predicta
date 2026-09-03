"""
AI orchestration layer.
Uses Claude to:
  1. Parse natural language match queries
  2. Interpret fetched web data into model signals
  3. Generate plain-language prediction narratives
"""
from __future__ import annotations
import json
import os
import re
from typing import Optional

import anthropic
from ai_client import get_client

MODEL = "claude-sonnet-5"


def _client() -> anthropic.Anthropic:
    return get_client()


# ── 1. Parse the natural-language query ──────────────────────────────────────

PARSE_SYSTEM = """You extract structured match information from a user query.
Return ONLY valid JSON with keys:
  team_a (string), team_b (string), date (ISO date string or null),
  sport (one of: soccer, tennis, table_tennis), notes (any extra context).
No markdown, no prose — raw JSON only."""


def parse_query(user_text: str) -> dict:
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=256,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── 2. Interpret fetched data into model signals ──────────────────────────────

SIGNALS_SYSTEM = """You are a sports analytics assistant. Given data about a match (which may be from
live APIs or may be empty if no live data was available), produce numerical signals for a
football/soccer prediction model.

If the fetched data is empty or sparse, use your training knowledge about the teams, their
typical recent form, key players, and historical Elo/Glicko levels — but set confidence="low"
and clearly state in signal_notes what is from training knowledge vs live data.

Return ONLY valid JSON with this exact schema (use null for any value you cannot determine):
{
  "team_a": {
    "xg_for_avg5": <float|null>,
    "xg_against_avg5": <float|null>,
    "form_weighted10": <float 0-1|null>,
    "rest_days": <int|null>,
    "key_player_out_flag": <0 or 1>,
    "injury_note": <string|null>,
    "elo_rating": <float|null>,
    "corners_for_avg5": <float|null>,
    "corners_against_avg5": <float|null>
  },
  "team_b": { <same keys> },
  "neutral_site": <0 or 1>,
  "likely_scorer_a": <single top player name string|null>,
  "likely_scorer_b": <single top player name string|null>,
  "top_scorers_a": [
    {"name": <string>, "position": <string>, "goal_prob": <float 0-1>},
    ...up to 6 players ranked by goal probability for team_a...
  ],
  "top_scorers_b": [
    {"name": <string>, "position": <string>, "goal_prob": <float 0-1>},
    ...up to 6 players ranked by goal probability for team_b...
  ],
  "confidence": <"low"|"medium"|"high">,
  "signal_notes": <string — must state which values came from live API data vs AI training knowledge>
}

For form_weighted10: 1.0=perfect form, 0.5=mixed, 0.0=terrible.
For xg estimates from training knowledge: top national team ~1.6-1.8 xG for, average ~1.2-1.4.
For goal_prob: probability the player scores at least one goal in THIS match (0-1).
  Typical ranges: elite striker 0.25-0.40, good forward 0.15-0.25, midfielder 0.05-0.15, defender 0.02-0.08.
  Distribute the team xG across players by their role, minutes, and historical scoring rate.
For corners_for_avg5: average corner kicks WON per game in last 5. Top teams ~6-8, average ~4-6, defensive ~3-4.
For corners_against_avg5: average corner kicks CONCEDED per game in last 5.
Set confidence="high" only if live API data was provided. "medium" if partial live data. "low" if all from training knowledge.
No markdown, raw JSON only."""


def interpret_signals(team_a: str, team_b: str, fetched_data: dict) -> dict:
    client = _client()
    prompt = f"""
Match: {team_a} vs {team_b}
Fetched data:
{json.dumps(fetched_data, indent=2, default=str)[:6000]}

Extract the signals."""

    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=800,
        system=SIGNALS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── 3. Generate narrative sentence ────────────────────────────────────────────

NARRATIVE_SYSTEM = """You are a sports prediction analyst. Write a clear, confident, 2-3 sentence
prediction summary based on the model output and match context.

Rules:
- Name the favourite team and their win probability (as a percentage)
- Name the most likely goal scorer for the favourite if available
- Briefly state the top reason (Elo advantage, form, xG strength, etc.)
- End with a one-sentence caveat about uncertainty
- Tone: analytical, not hype. No emojis.
- Keep it under 80 words total."""


def generate_narrative(
    team_a: str,
    team_b: str,
    prob_a: float,
    prob_b: float,
    prob_draw: Optional[float],
    explanation: str,
    signals: dict,
    fetched_context: dict,
) -> str:
    client = _client()

    scorer_a = signals.get("likely_scorer_a") or "unknown"
    scorer_b = signals.get("likely_scorer_b") or "unknown"
    signal_notes = signals.get("signal_notes", "")
    confidence = signals.get("confidence", "medium")

    prompt = f"""
Match: {team_a} vs {team_b}
Model probabilities: {team_a}={prob_a*100:.1f}%  Draw={prob_draw*100:.1f}%  {team_b}={prob_b*100:.1f}%
Model explanation: {explanation}
Likely scorer {team_a}: {scorer_a}
Likely scorer {team_b}: {scorer_b}
Signal notes: {signal_notes}
Data confidence: {confidence}

Write the prediction narrative."""

    msg = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=200,
        system=NARRATIVE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
