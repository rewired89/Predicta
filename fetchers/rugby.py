"""
ESPN NRL (Rugby League, Australia) data fetcher.
Uses the same unofficial ESPN site API already used by fetchers/baseball.py
and fetchers/soccer_schedule.py. No API key or registration required.

Team stats (points-for/against, home/away splits, recent form) are computed
by aggregating each team's completed-game schedule — ESPN's rugby-league API
does not expose a season-stats endpoint as rich as Understat/Savant, so this
mirrors the "derive from game log" approach rather than reading pre-aggregated
splits.

NOTE: this repo's remote build/test containers cannot reach espn.com (same
"remote container egress policy" already documented in fetchers/baseball.py).
This fetcher follows the exact schema (events/competitions/competitors/status)
already verified working for MLB/soccer/tennis ESPN endpoints, but the
rugby-league/nrl slug itself has not been live-verified from this session —
confirm field names against a real response once deployed (Railway) or run
locally, and adjust if ESPN's schema differs for this sport.
"""
from __future__ import annotations
import difflib
from datetime import datetime, timezone
from typing import Optional

import httpx

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/rugby-league/nrl"
TIMEOUT = 15.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Recent-form weight when blending last-5 vs season-long points averages.
# Matches the DEFAULT_RECENT_WEIGHT convention already used in models/dixon_coles.py.
RECENT_WEIGHT = 0.6
RECENT_GAMES = 5


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET from ESPN NRL API. Returns None on any failure (network, 4xx/5xx, bad JSON)."""
    url = f"{ESPN_BASE}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def fetch_teams() -> list[dict]:
    """Return [{id, name, abbrev}] for all 17 NRL teams. Empty list on failure."""
    data = _get("/teams")
    if not data:
        return []
    out = []
    for entry in (data.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", []):
        t = entry.get("team", {})
        out.append({
            "id":      t.get("id"),
            "name":    t.get("displayName", ""),
            "abbrev":  t.get("abbreviation", ""),
        })
    return out


def lookup_team(name: str, teams: Optional[list[dict]] = None) -> Optional[dict]:
    """Fuzzy-match a free-text team name (e.g. 'Rabbitohs', 'South Sydney') to
    an ESPN team entry. Returns None if no reasonable match is found."""
    teams = teams if teams is not None else fetch_teams()
    if not teams:
        return None
    name_lo = name.lower().strip()

    # Exact / substring match first
    for t in teams:
        t_name = t["name"].lower()
        if name_lo == t_name or name_lo in t_name or t_name in name_lo:
            return t

    # Fuzzy fallback on last word (nickname) or full name similarity
    names = [t["name"].lower() for t in teams]
    close = difflib.get_close_matches(name_lo, names, n=1, cutoff=0.5)
    if close:
        return next((t for t in teams if t["name"].lower() == close[0]), None)
    return None


def fetch_team_schedule(team_id: str, limit: int = 20) -> list[dict]:
    """
    Return completed games for a team, most recent first:
      [{date, is_home, points_for, points_against, opponent}]
    Empty list on any failure.
    """
    data = _get(f"/teams/{team_id}/schedule")
    if not data:
        return []
    games = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        status = comp.get("status", {}).get("type", {}).get("name", "")
        if status != "STATUS_FINAL":
            continue
        competitors = comp.get("competitors", [])
        mine = next((c for c in competitors if str(c.get("id")) == str(team_id)
                     or str(c.get("team", {}).get("id")) == str(team_id)), None)
        opp = next((c for c in competitors if c is not mine), None)
        if not mine or not opp:
            continue
        try:
            pf = int(mine.get("score", {}).get("value", mine.get("score", 0)))
            pa = int(opp.get("score", {}).get("value", opp.get("score", 0)))
        except (TypeError, ValueError):
            continue
        games.append({
            "date":            ev.get("date", ""),
            "is_home":         mine.get("homeAway") == "home",
            "points_for":      pf,
            "points_against":  pa,
            "opponent":        opp.get("team", {}).get("displayName", ""),
        })
    games.sort(key=lambda g: g["date"], reverse=True)
    return games[:limit]


def _avg(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _blend(season_val: Optional[float], recent_val: Optional[float],
           recent_weight: float = RECENT_WEIGHT) -> Optional[float]:
    if season_val is None and recent_val is None:
        return None
    if recent_val is None:
        return season_val
    if season_val is None:
        return recent_val
    return recent_weight * recent_val + (1 - recent_weight) * season_val


def team_points_profile(team_id: str) -> dict:
    """
    Aggregate a team's schedule into a points-for/against profile:
      {matches, ppg_for, ppg_against, home_ppg_for, home_ppg_against,
       away_ppg_for, away_ppg_against, home_matches, away_matches,
       recent_ppg_for, recent_ppg_against, effective_n, wins, losses, draws}

    All values None when the schedule is empty (caller should treat this as
    "no live data" rather than defaulting to a fabricated league-average team).
    """
    games = fetch_team_schedule(team_id, limit=20)
    if not games:
        return {
            "matches": 0, "ppg_for": None, "ppg_against": None,
            "home_ppg_for": None, "home_ppg_against": None,
            "away_ppg_for": None, "away_ppg_against": None,
            "home_matches": 0, "away_matches": 0,
            "recent_ppg_for": None, "recent_ppg_against": None,
            "effective_n": 0, "wins": 0, "losses": 0, "draws": 0,
        }

    home_games = [g for g in games if g["is_home"]]
    away_games = [g for g in games if not g["is_home"]]
    recent = games[:RECENT_GAMES]

    wins = sum(1 for g in games if g["points_for"] > g["points_against"])
    losses = sum(1 for g in games if g["points_for"] < g["points_against"])
    draws = sum(1 for g in games if g["points_for"] == g["points_against"])

    return {
        "matches":            len(games),
        "ppg_for":            _avg([g["points_for"] for g in games]),
        "ppg_against":        _avg([g["points_against"] for g in games]),
        "home_ppg_for":       _avg([g["points_for"] for g in home_games]),
        "home_ppg_against":   _avg([g["points_against"] for g in home_games]),
        "away_ppg_for":       _avg([g["points_for"] for g in away_games]),
        "away_ppg_against":   _avg([g["points_against"] for g in away_games]),
        "home_matches":       len(home_games),
        "away_matches":       len(away_games),
        "recent_ppg_for":     _avg([g["points_for"] for g in recent]),
        "recent_ppg_against": _avg([g["points_against"] for g in recent]),
        "effective_n":        float(len(games)),
        "wins": wins, "losses": losses, "draws": draws,
    }


def fetch_standings() -> list[dict]:
    """Return NRL ladder: [{team, wins, losses, draws, points_for, points_against, ladder_position}].
    Empty list on failure."""
    data = _get("/standings")
    if not data:
        return []
    out = []
    for group in data.get("children", []) or [data]:
        entries = (group.get("standings", {}) or {}).get("entries", [])
        for i, e in enumerate(entries, start=1):
            team = e.get("team", {})
            stats = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            out.append({
                "team":            team.get("displayName", ""),
                "ladder_position": i,
                "wins":            stats.get("wins"),
                "losses":          stats.get("losses"),
                "draws":           stats.get("ties") or stats.get("draws"),
                "points_for":      stats.get("pointsFor"),
                "points_against":  stats.get("pointsAgainst"),
            })
    return out


def enrich_rugby_teams(home_name: str, away_name: str) -> dict:
    """
    Main entry point — resolve both team names against ESPN's team list and
    return {"home": {...profile, team_id, resolved_name}, "away": {...}}.
    A team dict is {} (not defaulted to league-average) when it can't be
    resolved or has no completed games, matching the soccer pipeline's
    "don't hallucinate from nothing" convention.
    """
    teams = fetch_teams()
    home_match = lookup_team(home_name, teams)
    away_match = lookup_team(away_name, teams)

    result: dict = {"home": {}, "away": {}}
    if home_match:
        profile = team_points_profile(home_match["id"])
        if profile["matches"] > 0:
            result["home"] = {**profile, "team_id": home_match["id"], "resolved_name": home_match["name"]}
    if away_match:
        profile = team_points_profile(away_match["id"])
        if profile["matches"] > 0:
            result["away"] = {**profile, "team_id": away_match["id"], "resolved_name": away_match["name"]}
    return result
