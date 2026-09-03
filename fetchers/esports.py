"""
E-Sports data fetcher — PandaScore API primary, Claude AI fallback.
Returns structured context for analyze_esports.py.
"""
from __future__ import annotations
import os
import math
import time
import traceback
from typing import Optional

import env_loader  # noqa: F401

PANDASCORE_API_KEY = os.getenv("PANDASCORE_API_KEY", "")
_PANDASCORE_BASE = "https://api.pandascore.co"

SUPPORTED_GAMES = {
    "cs2":      "cs2",
    "csgo":     "cs2",
    "cs:go":    "cs2",
    "lol":      "lol",
    "league":   "lol",
    "league of legends": "lol",
    "dota":     "dota2",
    "dota2":    "dota2",
    "dota 2":   "dota2",
    "valorant": "valorant",
    "val":      "valorant",
}


def _normalise_game(raw: str) -> str:
    """Normalise free-text game name to a PandaScore slug."""
    r = (raw or "").strip().lower()
    return SUPPORTED_GAMES.get(r, r or "cs2")


def _pandascore_get(path: str, params: Optional[dict] = None, retries: int = 2) -> list | dict:
    if not PANDASCORE_API_KEY:
        return []
    try:
        import requests
        headers = {"Authorization": f"Bearer {PANDASCORE_API_KEY}"}
        url = f"{_PANDASCORE_BASE}{path}"
        for attempt in range(retries + 1):
            try:
                resp = requests.get(url, headers=headers, params=params or {}, timeout=8)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return []
            except Exception:
                if attempt < retries:
                    time.sleep(1)
    except ImportError:
        pass
    return []


def _search_team(name: str, game: str) -> dict:
    """Search PandaScore for a team; returns best match or empty dict."""
    slug = _normalise_game(game)
    data = _pandascore_get(f"/{slug}/teams", {"search[name]": name, "per_page": 5})
    if isinstance(data, list) and data:
        # prefer exact slug match, fall back to first result
        for t in data:
            if (t.get("name") or "").lower() == name.lower():
                return t
        return data[0]
    return {}


def _team_recent_matches(team_id: int, game: str, n: int = 10) -> list:
    """Fetch last n completed matches for a team."""
    slug = _normalise_game(game)
    data = _pandascore_get(
        f"/{slug}/matches/past",
        {"filter[team_id]": team_id, "sort": "-begin_at", "per_page": n},
    )
    return data if isinstance(data, list) else []


def _compute_form(team_id: int, matches: list) -> float:
    """Win rate from recent matches (0.0–1.0)."""
    if not matches:
        return 0.5
    wins = 0
    total = 0
    for m in matches:
        winner = m.get("winner") or {}
        if isinstance(winner, dict) and winner.get("id") is not None:
            total += 1
            if winner.get("id") == team_id:
                wins += 1
    return round(wins / total, 4) if total else 0.5


def _h2h_from_pandascore(team_a_id: int, team_b_id: int, game: str, n: int = 10) -> dict:
    slug = _normalise_game(game)
    data = _pandascore_get(
        f"/{slug}/matches/past",
        {"filter[opponent_id]": f"{team_a_id},{team_b_id}", "sort": "-begin_at", "per_page": n * 2},
    )
    wins_a = wins_b = 0
    encounters = []
    if isinstance(data, list):
        for m in data:
            opponents = [o.get("id") for o in (m.get("opponents") or []) if isinstance(o, dict) and o.get("type") == "Team"]
            if team_a_id in opponents and team_b_id in opponents:
                winner = (m.get("winner") or {}).get("id")
                if winner == team_a_id:
                    wins_a += 1
                elif winner == team_b_id:
                    wins_b += 1
                encounters.append({
                    "date":   (m.get("begin_at") or "")[:10],
                    "winner": m.get("winner", {}).get("name", ""),
                    "score":  m.get("results", ""),
                })
    return {"wins_a": wins_a, "wins_b": wins_b, "last_encounters": encounters[:5]}


def _fallback_team_info(name: str, game: str) -> dict:
    """Claude AI fallback — estimates ranking/form when PandaScore unavailable."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": (
                    f"Estimate the current world ranking and recent form (win rate last 10 matches, 0.0-1.0) "
                    f"for e-sports team '{name}' in {game}. "
                    "Reply with JSON only: {\"ranking\": int, \"form\": float, \"region\": str}. "
                    "Use 999 for ranking if unknown."
                ),
            }],
        )
        import json, re
        text = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return {"ranking": 999, "form": 0.5, "region": "Unknown"}


def fetch_esports_context(team_a: str, team_b: str, game: str) -> dict:
    """
    Main entry — returns context dict with team data, H2H, and sources.

    Keys: team_a, team_b, game, team_a_data, team_b_data, h2h, sources
    """
    game_slug = _normalise_game(game)
    sources = []

    # Try PandaScore
    ta_ps = _search_team(team_a, game_slug)
    tb_ps = _search_team(team_b, game_slug)
    pandascore_available = bool(PANDASCORE_API_KEY) and bool(ta_ps or tb_ps)

    def _build_team_data(ps_team: dict, raw_name: str, game_s: str) -> dict:
        if ps_team:
            team_id = ps_team.get("id")
            matches = _team_recent_matches(team_id, game_s) if team_id else []
            form = _compute_form(team_id, matches) if team_id else 0.5
            return {
                "name":     ps_team.get("name", raw_name),
                "slug":     ps_team.get("slug", ""),
                "image":    ps_team.get("image_url", ""),
                "ranking":  ps_team.get("ranking") or 999,
                "region":   (ps_team.get("location") or "Unknown"),
                "form":     round(form, 4),
                "recent_matches": matches[:5],
                "source":   "pandascore",
            }
        # Claude fallback
        info = _fallback_team_info(raw_name, game_s)
        return {
            "name":     raw_name,
            "slug":     "",
            "image":    "",
            "ranking":  info.get("ranking", 999),
            "region":   info.get("region", "Unknown"),
            "form":     info.get("form", 0.5),
            "recent_matches": [],
            "source":   "ai_estimate",
        }

    team_a_data = _build_team_data(ta_ps, team_a, game_slug)
    team_b_data = _build_team_data(tb_ps, team_b, game_slug)

    if pandascore_available:
        sources.append("pandascore")
    if team_a_data.get("source") == "ai_estimate" or team_b_data.get("source") == "ai_estimate":
        sources.append("ai_estimate")

    # H2H
    h2h = {"wins_a": 0, "wins_b": 0, "last_encounters": []}
    if ta_ps.get("id") and tb_ps.get("id"):
        try:
            h2h = _h2h_from_pandascore(ta_ps["id"], tb_ps["id"], game_slug)
        except Exception:
            pass

    return {
        "team_a":      team_a_data["name"],
        "team_b":      team_b_data["name"],
        "game":        game_slug,
        "team_a_data": team_a_data,
        "team_b_data": team_b_data,
        "h2h":         h2h,
        "sources":     sources,
    }
