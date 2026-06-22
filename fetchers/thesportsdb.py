"""
TheSportsDB + ESPN public API fetcher (no paid key required).

TheSportsDB free key=1 is used for team/player search.
ESPN public API (no auth) is used for recent match results.
"""
from __future__ import annotations
import os
from datetime import datetime
from typing import Optional

import httpx

TSDB_BASE = "https://www.thesportsdb.com/api/v1/json"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
TIMEOUT = 12

_API_KEY = os.environ.get("SPORTSDB_API_KEY", "1")  # "1" is TheSportsDB free public key


def _tsdb_get(path: str, params: dict = {}) -> dict:
    url = f"{TSDB_BASE}/{_API_KEY}/{path}"
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _espn_get(url: str, params: dict = {}) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


# ── TheSportsDB helpers ───────────────────────────────────────────────────────

def search_team(name: str) -> Optional[dict]:
    """Return first matching soccer team dict or None."""
    data = _tsdb_get("searchteams.php", {"t": name})
    teams = data.get("teams") or []
    for t in teams:
        if t.get("strSport", "").lower() == "soccer":
            return t
    return teams[0] if teams else None


def last5_from_tsdb(team_id: str) -> list[dict]:
    """Try eventsseason.php (free tier) for the two most recent seasons."""
    year = datetime.now().year
    seasons = [f"{year-1}-{year}", str(year - 1), f"{year}-{year+1}"]
    for season in seasons:
        try:
            data = _tsdb_get("eventsseason.php", {"id": team_id, "s": season})
            events = data.get("events") or []
            completed = [
                e for e in events
                if e.get("intHomeScore") is not None and e.get("intAwayScore") is not None
            ]
            completed.sort(key=lambda x: x.get("dateEvent", ""), reverse=True)
            if completed:
                return completed[:5]
        except Exception:
            continue
    return []


def search_players(team_name: str) -> list[dict]:
    data = _tsdb_get("searchplayers.php", {"t": team_name})
    return data.get("player") or []


# ── ESPN helpers ──────────────────────────────────────────────────────────────

# Leagues to probe when looking for a national team on ESPN
_ESPN_LEAGUES = [
    "fifa.world",
    "conmebol.america",
    "uefa.nations",
    "concacaf.nations.league",
    "afc.championship",
    "caf.nations",
]


def _espn_find_team(name: str) -> Optional[dict]:
    """Search ESPN across major national-team competitions for the given team name."""
    name_lower = name.lower()
    for league in _ESPN_LEAGUES:
        try:
            data = _espn_get(f"{ESPN_BASE}/{league}/teams")
            sports = data.get("sports") or []
            for sport in sports:
                for lg in sport.get("leagues", []):
                    for entry in lg.get("teams", []):
                        t = entry.get("team", {})
                        display = t.get("displayName", "").lower()
                        short = t.get("shortDisplayName", "").lower()
                        if name_lower in display or name_lower in short:
                            return {
                                "id": t["id"],
                                "name": t.get("displayName", name),
                                "league": league,
                            }
        except Exception:
            continue
    return None


def _espn_last5(team_id: str, league: str) -> list[dict]:
    """Fetch recent completed matches from ESPN for a team."""
    try:
        url = f"{ESPN_BASE}/{league}/teams/{team_id}/schedule"
        data = _espn_get(url)
        events = data.get("events") or []
        completed = []
        for ev in events:
            comps = ev.get("competitions", [{}])
            comp = comps[0] if comps else {}
            status = comp.get("status", {}).get("type", {}).get("name", "")
            if status != "STATUS_FINAL":
                continue
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            h_score = int(home.get("score", 0))
            a_score = int(away.get("score", 0))
            completed.append({
                "dateEvent": ev.get("date", "")[:10],
                "strHomeTeam": home.get("team", {}).get("displayName", ""),
                "strAwayTeam": away.get("team", {}).get("displayName", ""),
                "intHomeScore": h_score,
                "intAwayScore": a_score,
            })
        # Most recent first
        completed.sort(key=lambda x: x["dateEvent"], reverse=True)
        return completed[:5]
    except Exception:
        return []


# ── Main context fetcher ──────────────────────────────────────────────────────

def fetch_match_context(team_a: str, team_b: str) -> dict:
    """
    Fetch all available context for a match. Tries TheSportsDB first, then ESPN.
    Every source is recorded so the UI transparency panel can display it.
    """
    sources = []
    result: dict = {
        "team_a": {"name": team_a},
        "team_b": {"name": team_b},
        "sources": sources,
    }

    for key, name in [("team_a", team_a), ("team_b", team_b)]:
        tid = None
        # ── TheSportsDB: team profile + players ───────────────────────────────
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
                    "url": f"{TSDB_BASE}/1/searchteams.php?t={name}",
                    "snippet": f"Found team ID {tid}, country={team.get('strCountry')}",
                })
        except Exception as exc:
            sources.append({"label": f"{name} TSDB profile", "url": "", "snippet": f"ERROR: {exc}"})

        # ── Last 5 results: try TheSportsDB eventsseason, then ESPN ───────────
        last5 = []
        if tid:
            try:
                raw = last5_from_tsdb(tid)
                if raw:
                    last5 = [
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
                        for e in raw
                    ]
                    sources.append({
                        "label": f"{name} last 5 (TheSportsDB)",
                        "url": f"{TSDB_BASE}/1/eventsseason.php?id={tid}",
                        "snippet": f"{len(last5)} results fetched",
                    })
            except Exception as exc:
                sources.append({"label": f"{name} TSDB results", "url": "", "snippet": f"ERROR: {exc}"})

        # Fall back to ESPN if TheSportsDB returned nothing
        if not last5:
            try:
                espn_team = _espn_find_team(name)
                if espn_team:
                    raw_espn = _espn_last5(espn_team["id"], espn_team["league"])
                    if raw_espn:
                        last5 = [
                            {
                                "date": e["dateEvent"],
                                "home": e["strHomeTeam"],
                                "away": e["strAwayTeam"],
                                "score_home": e["intHomeScore"],
                                "score_away": e["intAwayScore"],
                                "winner": (
                                    "home" if e["intHomeScore"] > e["intAwayScore"]
                                    else "away" if e["intAwayScore"] > e["intHomeScore"]
                                    else "draw"
                                ),
                            }
                            for e in raw_espn
                        ]
                        sources.append({
                            "label": f"{name} last 5 (ESPN)",
                            "url": f"{ESPN_BASE}/{espn_team['league']}/teams/{espn_team['id']}/schedule",
                            "snippet": f"{len(last5)} results from ESPN (no API key needed)",
                        })
            except Exception as exc:
                sources.append({"label": f"{name} ESPN results", "url": "", "snippet": f"ERROR: {exc}"})

        if last5:
            result[key]["last5"] = last5

        # ── Key players from TheSportsDB ──────────────────────────────────────
        try:
            players = search_players(name)
            forwards = [
                p for p in players
                if "forward" in (p.get("strPosition") or "").lower()
                or "striker" in (p.get("strPosition") or "").lower()
            ]
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
                    "url": f"{TSDB_BASE}/1/searchplayers.php?t={name}",
                    "snippet": f"{len(players)} players found; {len(forwards)} forwards",
                })
        except Exception as exc:
            sources.append({"label": f"{name} squad", "url": "", "snippet": f"ERROR: {exc}"})

    return result
