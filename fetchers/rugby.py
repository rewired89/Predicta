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


CORE_API_BASE = "https://sports.core.api.espn.com/v2/sports"

# Manual fallback guesses, tried only if the core-API league discovery
# (below) itself fails to return anything usable.
_CANDIDATE_SLUGS = [
    ("rugby-league", "super-league"),
    ("rugby-league", "nrl-premiership"),
    ("rugby-league", "nrl.1"),
    ("rugby-league", "aus.1"),
    ("rugby", "nrl"),
]


def discover_leagues(sport: str = "rugby-league") -> dict:
    """
    Query ESPN's separate 'core' API (sports.core.api.espn.com) for the list
    of leagues it knows about under a given sport — this is a discovery
    endpoint, not a guess. Added 2026-07-12 after /teams returned a specific
    "League not found" 404 for 'nrl' (confirming 'rugby-league' itself IS a
    recognised ESPN sport category, just not under that league code).
    Returns {status, leagues: [{slug, name}], raw_error} — raw_error set on
    any non-200 so we can see exactly what this endpoint says too.
    """
    out: dict = {"sport": sport}
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{CORE_API_BASE}/{sport}/leagues", params={"limit": 100})
            out["status"] = resp.status_code
            if resp.status_code != 200:
                out["raw_error"] = resp.text[:500]
                return out
            data = resp.json()
            items = data.get("items", [])
            out["league_count"] = len(items)
            out["leagues"] = [
                {"slug": item.get("slug") or item.get("abbreviation"), "name": item.get("name")}
                for item in items
                if isinstance(item, dict)
            ]
    except Exception as exc:
        out["exception"] = str(exc)
    return out


def diagnose(sample_team_query: str = "Rabbitohs") -> dict:
    """
    One-shot diagnostic that reports EXACTLY why ESPN enrichment works or
    fails — raw HTTP status + response body for /teams and today's
    /scoreboard, instead of the silent {} the normal path returns on any
    failure. Built after the first live Railway test came back empty for
    both teams in a real, in-progress NRL fixture (2026-07-12) — that's
    strong evidence the assumed rugby-league/nrl slug or JSON shape is
    wrong, not that ESPN itself is down, so this surfaces the real response
    instead of guessing again.
    """
    out: dict = {"espn_base": ESPN_BASE}

    # 1. Raw /teams probe
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{ESPN_BASE}/teams")
            out["teams_status"] = resp.status_code
            out["teams_url"] = str(resp.url)
            if resp.status_code != 200:
                out["teams_error_body"] = resp.text[:500]
            else:
                data = resp.json()
                out["teams_top_level_keys"] = list(data.keys())
    except Exception as exc:
        out["teams_exception"] = str(exc)

    parsed_teams = fetch_teams()
    out["parsed_teams_count"] = len(parsed_teams)
    out["parsed_teams_sample"] = parsed_teams[:5]

    # 1b. League discovery — if /teams 404'd with "League not found" (sport
    # recognised, league code wrong), ask ESPN's core API what leagues it
    # actually has under this sport instead of guessing candidate slugs.
    if out.get("teams_status") == 404:
        out["league_discovery"] = discover_leagues("rugby-league")
        # Cheap fallback: also try a short list of plausible alternate slugs
        # directly, in case the discovery endpoint itself is empty/blocked.
        candidate_results = []
        for sport, league in _CANDIDATE_SLUGS:
            try:
                with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams"
                    resp = client.get(url)
                    candidate_results.append({
                        "sport": sport, "league": league, "status": resp.status_code,
                        "looks_ok": resp.status_code == 200,
                    })
            except Exception as exc:
                candidate_results.append({"sport": sport, "league": league, "exception": str(exc)})
        out["candidate_slug_probe"] = candidate_results

    # 2. Raw /scoreboard probe for today — if the slug is right, an
    # in-progress or scheduled fixture should show up here directly.
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{ESPN_BASE}/scoreboard", params={"dates": today})
            out["scoreboard_status"] = resp.status_code
            out["scoreboard_url"] = str(resp.url)
            if resp.status_code != 200:
                out["scoreboard_error_body"] = resp.text[:500]
            else:
                data = resp.json()
                events = data.get("events") or []
                out["scoreboard_event_count"] = len(events)
                out["scoreboard_sample"] = [
                    {
                        "name": ev.get("name"),
                        "status": (ev.get("competitions") or [{}])[0]
                                    .get("status", {}).get("type", {}).get("name"),
                        "date": ev.get("date"),
                    }
                    for ev in events[:10]
                ]
    except Exception as exc:
        out["scoreboard_exception"] = str(exc)

    # 3. If any team resolved, probe its schedule endpoint raw too.
    match = lookup_team(sample_team_query, parsed_teams) if parsed_teams else None
    if match:
        out["sample_team_matched"] = match
        try:
            with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                resp = client.get(f"{ESPN_BASE}/teams/{match['id']}/schedule")
                out["schedule_status"] = resp.status_code
                if resp.status_code != 200:
                    out["schedule_error_body"] = resp.text[:500]
                else:
                    data = resp.json()
                    events = data.get("events") or []
                    out["schedule_event_count"] = len(events)
                    statuses = [
                        (ev.get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("name")
                        for ev in events
                    ]
                    out["schedule_status_breakdown"] = {
                        s: statuses.count(s) for s in set(statuses)
                    }
        except Exception as exc:
            out["schedule_exception"] = str(exc)
    else:
        out["sample_team_matched"] = None

    return out
