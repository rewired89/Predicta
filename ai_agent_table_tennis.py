"""
AI orchestration layer for table tennis.
Uses Claude Haiku to parse queries, estimate signals, and generate narratives.
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


# ── 1. Parse query ─────────────────────────────────────────────────────────────

PARSE_SYSTEM = """You extract structured table tennis match information from a user query.
Return ONLY valid JSON with these keys:
  player_a  — first player mentioned (or favourite if clear)
  player_b  — second player mentioned
  tour      — "ittf" (World Tour) | "wtt" (World Team Table Tennis) | "local" — default "ittf"
  date      — ISO date YYYY-MM-DD, or null to mean today
  notes     — extra context: playing style (attacker/defender/all-round), handedness, tournament name
No markdown, no prose — raw JSON only."""


def parse_table_tennis_query(user_text: str) -> dict:
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


# ── 2. Fallback signal estimation ─────────────────────────────────────────────

SIGNALS_SYSTEM = """You are a table tennis analytics expert. Given two players, estimate
key model signals using your training knowledge (ITTF rankings, playing styles, recent form).

Return ONLY valid JSON with this exact schema:
{
  "player_a": {
    "ranking": <int — ITTF world ranking, 999 if unknown>,
    "recent_form": <float 0-1 — last-10-match win rate, 0.5 if unknown>,
    "attack_quality_index": <int — 100=tour average; 115=elite attacker like Ma Long, 85=defensive/passive>,
    "return_quality_index": <int — 100=tour average; 115=elite blocker/counter, 85=weak return>,
    "style": <"attacker"|"defender"|"all-round"|"chopper">,
    "handedness": <"right"|"left"|"unknown">
  },
  "player_b": { <same keys> },
  "h2h_advantage": <"player_a"|"player_b"|"even">,
  "style_edge": <"player_a"|"player_b"|"even" — who benefits from the style matchup>,
  "confidence": <"low"|"medium"|"high">,
  "notes": <string — sourced from training knowledge, key matchup factors>
}
attack_quality_index: 120=Ma Long/Fan Zhendong level, 110=top-20 attacker, 90=lower ranked attacker, 80=defensive player forced to attack.
return_quality_index: 115=elite blocker/counter-attacker, 85=struggles against heavy spin.
No markdown — raw JSON only."""


def interpret_table_tennis_signals(
    player_a: str,
    player_b: str,
    tour: str = "ittf",
    notes: str = "",
) -> dict:
    """Fallback when TSDB returns no data — Claude estimates signals from training knowledge."""
    client = _client()
    prompt = f"""
Matchup: {player_a} vs {player_b}
Tour: {tour.upper()}
Extra context: {notes or 'none'}

Estimate table tennis model signals for this matchup."""

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

NARRATIVE_SYSTEM = """You are a sharp table tennis prediction analyst. Write a clear, confident 2–3 sentence
prediction summary.

Rules:
- State the favourite and their win probability as a percentage
- Lead with the most decisive factor: ranking gap, playing style matchup, recent form, or H2H
- Mention the style matchup (attacker vs defender, left vs right) if relevant
- End with a one-sentence uncertainty caveat (form variance, serve rule changes, tournament format)
- Tone: analytical, no hype. No emojis. Under 90 words total."""


def generate_table_tennis_narrative(
    player_a: str,
    player_b: str,
    prob_a: float,
    prob_b: float,
    explanation: str,
    context: dict,
) -> str:
    client = _client()

    sa  = context.get("player_a", {})
    sb  = context.get("player_b", {})
    h2h = context.get("h2h", {})
    h2h_str = (
        f"H2H: {player_a} {h2h.get('wins_a', 0)}-{h2h.get('wins_b', 0)} {player_b}"
        if h2h else "H2H: unknown"
    )

    prompt = f"""
Table tennis matchup: {player_a} vs {player_b}
Win probabilities: {player_a} {prob_a*100:.1f}%  {player_b} {prob_b*100:.1f}%
{player_a}: ranking #{sa.get('ranking','?')}, AQI={sa.get('attack_quality_index','?')}, RQI={sa.get('return_quality_index','?')}, style={sa.get('style','?')}, form={sa.get('recent_form','?')}
{player_b}: ranking #{sb.get('ranking','?')}, AQI={sb.get('attack_quality_index','?')}, RQI={sb.get('return_quality_index','?')}, style={sb.get('style','?')}, form={sb.get('recent_form','?')}
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
