"""
TheSportsDB free-tier fetcher (no API key required).
Used for: team search, recent results, squad/player lookup.
"""
from __future__ import annotations
import httpx
from typing import Optional

BASE = "https://www.thesportsdb.com/api/v1/json/3"
TIMEOUT = 12

# TheSportsDB free tier switched to requiring a key in 2024.
# Register at https://www.thesportsdb.com/api.php and set SPORTSDB_API_KEY.
# Without a key the fetcher returns empty results and the AI uses training knowledge.
import os
_API_KEY = os.environ.get("SPORTSDB_API_KEY", "3")  # "3" is their test key


def _get(url: str, params: dict = {}) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(url.replace("/json/3/", f"/json/{_API_KEY}/"), params=params)
        r.raise_for_status()
        return r.json()


def search_team(name: str) -> Optional[dict]:
    """Return first matching team dict or None."""
    data = _get(f"{BASE}/searchteams.php", {"t": name})
    teams = data.get("teams") or []
    # Prefer national teams (sport=Soccer, country matches name)
    for t in teams:
        if t.get("strSport", "").lower() == "soccer":
            return t
    return teams[0] if teams else None


def last5_results(team_id: str) -> list[dict]:
    """Return up to 5 recent match results for a team."""
    data = _get(f"{BASE}/eventslast5.php", {"id": team_id})
    return data.get("results") or []


def search_players(team_name: str) -> list[dict]:
    """Return player list for a team (top results)."""
    data = _get(f"{BASE}/searchplayers.php", {"t": team_name})
    return data.get("player") or []


def team_details(team_id: str) -> Optional[dict]:
    data = _get(f"{BASE}/lookupteam.php", {"id": team_id})
    teams = data.get("teams") or []
    return teams[0] if teams else None


def fetch_match_context(team_a: str, team_b: str) -> dict:
    """
    Fetch all available context for a match and return a structured dict.
    Every field records its source URL so the UI can show it.
    """
    sources = []
    result: dict = {
        "team_a": {"name": team_a},
        "team_b": {"name": team_b},
        "sources": sources,
    }

    for key, name in [("team_a", team_a), ("team_b", team_b)]:
        try:
            team = search_team(name)
            if team:
                tid = team.get("idTeam")
                result[key]["id"] = tid
                result[key]["badge"] = team.get("strTeamBadge")
                result[key]["country"] = team.get("strCountry")
                result[key]["description"] = (team.get("strDescriptionEN") or "")[:400]
                sources.append({
                    "label": f"{name} team profile",
                    "url": f"{BASE}/searchteams.php?t={name}",
                    "snippet": f"Found team ID {tid}, country={team.get('strCountry')}",
                })

                # Last 5 results
                last5 = last5_results(tid)
                if last5:
                    result[key]["last5"] = [
                        {
                            "date": e.get("dateEvent"),
                            "home": e.get("strHomeTeam"),
                            "away": e.get("strAwayTeam"),
                            "score_home": e.get("intHomeScore"),
                            "score_away": e.get("intAwayScore"),
                            "winner": (
                                "home" if int(e.get("intHomeScore") or 0) > int(e.get("intAwayScore") or 0)
                                else "away" if int(e.get("intAwayScore") or 0) > int(e.get("intHomeScore") or 0)
                                else "draw"
                            ),
                        }
                        for e in last5
                        if e.get("intHomeScore") is not None
                    ]
                    sources.append({
                        "label": f"{name} last 5 results",
                        "url": f"{BASE}/eventslast5.php?id={tid}",
                        "snippet": f"{len(result[key].get('last5', []))} results fetched",
                    })

                # Key players (top 8 by position)
                players = search_players(name)
                forwards = [p for p in players if "forward" in (p.get("strPosition") or "").lower()
                            or "striker" in (p.get("strPosition") or "").lower()]
                result[key]["key_players"] = [
                    {
                        "name": p.get("strPlayer"),
                        "position": p.get("strPosition"),
                        "nationality": p.get("strNationality"),
                        "birth_year": (p.get("dateBorn") or "")[:4],
                    }
                    for p in (forwards or players)[:6]
                ]
                if players:
                    sources.append({
                        "label": f"{name} squad",
                        "url": f"{BASE}/searchplayers.php?t={name}",
                        "snippet": f"{len(players)} players found; {len(forwards)} forwards",
                    })
        except Exception as exc:
            result[key]["fetch_error"] = str(exc)
            sources.append({"label": f"{name} fetch", "url": "", "snippet": f"ERROR: {exc}"})

    return result
