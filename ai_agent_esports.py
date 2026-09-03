"""
AI agent for e-sports — query parsing and narrative generation.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone

import env_loader  # noqa: F401


def parse_esports_query(user_text: str) -> dict:
    """
    Extract team_a, team_b, game, format, date, notes from free-text.

    Tries Claude first, falls back to heuristic regex.
    """
    try:
        import anthropic, json as _json
        client = anthropic.Anthropic()
        today = datetime.now(timezone.utc).date().isoformat()
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": (
                    f"Today is {today}. Extract e-sports match info from this query: '{user_text}'\n"
                    "Reply with JSON only, keys: team_a (str), team_b (str), "
                    "game (one of cs2/lol/dota2/valorant, default cs2), "
                    "format (str, e.g. 'Bo3'), date (YYYY-MM-DD or null), notes (str)."
                ),
            }],
        )
        text = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            parsed = _json.loads(m.group())
            # Sanitise
            parsed.setdefault("game", "cs2")
            parsed.setdefault("format", "Bo3")
            parsed.setdefault("date", today)
            parsed.setdefault("notes", "")
            return parsed
    except Exception:
        pass

    # Heuristic fallback
    vs_match = re.search(r'(.+?)\s+vs\.?\s+(.+?)(?:\s+(?:in\s+|at\s+|on\s+|[-–]|bo[135]).*)?$', user_text, re.IGNORECASE)
    team_a = vs_match.group(1).strip() if vs_match else "Team A"
    team_b = vs_match.group(2).strip() if vs_match else "Team B"

    game = "cs2"
    lc = user_text.lower()
    if "valorant" in lc or "val " in lc:
        game = "valorant"
    elif "dota" in lc:
        game = "dota2"
    elif "league" in lc or " lol" in lc:
        game = "lol"

    bo_match = re.search(r'bo([135])', lc)
    fmt = f"Bo{bo_match.group(1)}" if bo_match else "Bo3"

    return {
        "team_a": team_a,
        "team_b": team_b,
        "game":   game,
        "format": fmt,
        "date":   datetime.now(timezone.utc).date().isoformat(),
        "notes":  "",
    }


def generate_esports_narrative(
    team_a: str, team_b: str, game: str,
    prob_a: float, prob_b: float,
    team_a_data: dict, team_b_data: dict,
    recommendation: str, data_confidence: str,
    notes: str = "",
) -> str:
    """Generate a 2-3 sentence match narrative using Claude."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        rank_a = team_a_data.get("ranking", 999)
        rank_b = team_b_data.get("ranking", 999)
        form_a = round(team_a_data.get("form", 0.5) * 100, 1)
        form_b = round(team_b_data.get("form", 0.5) * 100, 1)
        region_a = team_a_data.get("region", "")
        region_b = team_b_data.get("region", "")
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": (
                    f"Write a 2-3 sentence e-sports match preview for {team_a} vs {team_b} in {game}. "
                    f"{team_a} (rank #{rank_a}, {region_a}, {form_a}% form) vs "
                    f"{team_b} (rank #{rank_b}, {region_b}, {form_b}% form). "
                    f"Model gives {team_a} {prob_a:.1f}% win probability. "
                    f"Recommendation: {recommendation}. "
                    f"{'Notes: ' + notes if notes else ''} "
                    "Be concise and analytical. No hype. Focus on the key factor."
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception:
        leader = team_a if prob_a >= prob_b else team_b
        prob_leader = max(prob_a, prob_b)
        return (
            f"{team_a} vs {team_b} in {game} — model gives {team_a} a {prob_a:.1f}% win probability. "
            f"{leader} is the projected favourite at {prob_leader:.1f}% based on Elo rating and recent form. "
            f"Data confidence: {data_confidence}."
        )
