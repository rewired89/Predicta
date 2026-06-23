"""
TheSportsDB + ESPN public API fetcher (no paid key required).

TheSportsDB free key=1 is used for team/player search and H2H.
ESPN public API (no auth) is used for recent match results via
scoreboard date-range search — more reliable than team ID lookup.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx

TSDB_BASE = "https://www.thesportsdb.com/api/v1/json"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
TIMEOUT = 15

_API_KEY = os.environ.get("SPORTSDB_API_KEY", "1")


def _tsdb_get(path: str, params: dict = {}) -> dict:
    url = f"{TSDB_BASE}/{_API_KEY}/{path}"
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _espn_get(url: str, params: dict = {}) -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=TIMEOUT, headers=headers) as c:
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
    """Try multiple TSDB endpoints and seasons to get last 5 results."""
    year = datetime.now().year
    attempts = [
        ("eventsseason.php", f"{year-1}-{year}"),
        ("eventsseason.php", str(year - 1)),
        ("eventsseason.php", f"{year}-{year+1}"),
        ("eventsteam.php",   f"{year-1}-{year}"),
        ("eventsteam.php",   str(year - 1)),
        ("eventsteam.php",   f"{year}-{year+1}"),
    ]
    for endpoint, season in attempts:
        try:
            data = _tsdb_get(endpoint, {"id": team_id, "s": season})
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


def fetch_h2h(team_id_a: str, team_id_b: str) -> list[dict]:
    """Return last 5 head-to-head results between two teams."""
    try:
        data = _tsdb_get("eventsh2h.php", {"idTeam1": team_id_a, "idTeam2": team_id_b})
        events = data.get("results") or data.get("events") or []
        completed = [
            e for e in events
            if e.get("intHomeScore") is not None and e.get("intAwayScore") is not None
        ]
        completed.sort(key=lambda x: x.get("dateEvent", ""), reverse=True)
        return completed[:5]
    except Exception:
        return []


def search_players(team_name: str) -> list[dict]:
    data = _tsdb_get("searchplayers.php", {"t": team_name})
    return data.get("player") or []


# ── ESPN scoreboard-based search (most reliable free approach) ────────────────

# League slugs to probe — national competitions first, then major clubs
_ESPN_LEAGUES = [
    "fifa.world",
    "conmebol.america",
    "uefa.nations",
    "uefa.euro",
    "concacaf.gold",
    "caf.nations",
    "afc.championship",
    "eng.1",        # Premier League
    "esp.1",        # La Liga
    "ger.1",        # Bundesliga
    "fra.1",        # Ligue 1
    "ita.1",        # Serie A
    "uefa.champions",
    "uefa.europa",
]


def _parse_espn_events(data: dict, name_lower: str) -> list[dict]:
    """Extract completed events for a named team from an ESPN scoreboard response."""
    results = []
    events = data.get("events") or []
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
        home_name = home.get("team", {}).get("displayName", "")
        away_name = away.get("team", {}).get("displayName", "")
        # Only include if this team played
        if name_lower not in home_name.lower() and name_lower not in away_name.lower():
            continue
        try:
            h_score = int(home.get("score", 0))
            a_score = int(away.get("score", 0))
        except (ValueError, TypeError):
            continue
        results.append({
            "dateEvent": ev.get("date", "")[:10],
            "strHomeTeam": home_name,
            "strAwayTeam": away_name,
            "intHomeScore": h_score,
            "intAwayScore": a_score,
        })
    return results


def _espn_last5_by_scoreboard(name: str) -> list[dict]:
    """
    Search ESPN scoreboards over the past 12 months across all major leagues.
    Uses date-range scoreboard endpoint — no team ID needed.
    """
    name_lower = name.lower()
    today = datetime.now()
    # Build two 6-month date windows
    date_ranges = [
        (today - timedelta(days=180), today),
        (today - timedelta(days=365), today - timedelta(days=180)),
    ]

    found = []
    for league in _ESPN_LEAGUES:
        if len(found) >= 5:
            break
        for start, end in date_ranges:
            if len(found) >= 5:
                break
            date_str = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
            try:
                data = _espn_get(
                    f"{ESPN_BASE}/{league}/scoreboard",
                    {"dates": date_str, "limit": 100},
                )
                matches = _parse_espn_events(data, name_lower)
                found.extend(matches)
            except Exception:
                continue

    found.sort(key=lambda x: x["dateEvent"], reverse=True)
    # Deduplicate by date+teams
    seen = set()
    unique = []
    for m in found:
        key = (m["dateEvent"], m["strHomeTeam"], m["strAwayTeam"])
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique[:5]


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

    tid_a = None
    tid_b = None

    for key, name in [("team_a", team_a), ("team_b", team_b)]:
        # ── TheSportsDB: team profile ─────────────────────────────────────────
        try:
            team = search_team(name)
            if team:
                tid = team.get("idTeam")
                if key == "team_a":
                    tid_a = tid
                else:
                    tid_b = tid
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

        # ── Last 5: TheSportsDB season endpoint ───────────────────────────────
        last5 = []
        tid = result[key].get("id")
        if tid:
            try:
                raw = last5_from_tsdb(tid)
                if raw:
                    last5 = _normalize_last5(raw)
                    sources.append({
                        "label": f"{name} last 5 (TheSportsDB)",
                        "url": f"{TSDB_BASE}/1/eventsseason.php?id={tid}",
                        "snippet": f"{len(last5)} results fetched",
                    })
            except Exception as exc:
                sources.append({"label": f"{name} TSDB results", "url": "", "snippet": f"ERROR: {exc}"})

        # ── Last 5 fallback: ESPN scoreboard ──────────────────────────────────
        if not last5:
            try:
                raw_espn = _espn_last5_by_scoreboard(name)
                if raw_espn:
                    last5 = _normalize_last5(raw_espn)
                    sources.append({
                        "label": f"{name} last 5 (ESPN)",
                        "url": f"{ESPN_BASE}/fifa.world/scoreboard",
                        "snippet": f"{len(last5)} results from ESPN scoreboard",
                    })
                else:
                    sources.append({
                        "label": f"{name} last 5",
                        "url": "",
                        "snippet": "No recent results found on TheSportsDB or ESPN.",
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

    # ── Head-to-Head ──────────────────────────────────────────────────────────
    if tid_a and tid_b:
        try:
            h2h_raw = fetch_h2h(tid_a, tid_b)
            if h2h_raw:
                result["h2h"] = _normalize_last5(h2h_raw)
                sources.append({
                    "label": f"Head-to-Head: {team_a} vs {team_b}",
                    "url": f"{TSDB_BASE}/1/eventsh2h.php?idTeam1={tid_a}&idTeam2={tid_b}",
                    "snippet": f"{len(result['h2h'])} previous meetings found",
                })
            else:
                result["h2h"] = []
        except Exception as exc:
            result["h2h"] = []
            sources.append({"label": "Head-to-Head", "url": "", "snippet": f"ERROR: {exc}"})

    return result


def _normalize_last5(raw: list[dict]) -> list[dict]:
    """Convert TSDB/ESPN event dicts into a consistent frontend format."""
    out = []
    for e in raw:
        try:
            h = int(e.get("intHomeScore") or 0)
            a = int(e.get("intAwayScore") or 0)
            out.append({
                "date": e.get("dateEvent", ""),
                "home": e.get("strHomeTeam", ""),
                "away": e.get("strAwayTeam", ""),
                "score_home": h,
                "score_away": a,
                "winner": "home" if h > a else "away" if a > h else "draw",
            })
        except (TypeError, ValueError):
            continue
    return out
