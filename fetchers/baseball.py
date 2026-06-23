"""
MLB Stats API fetcher.
Public API — no key required.
"""
from __future__ import annotations
import difflib
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

BASE_URL = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 15.0

LEAGUE_AVG_RUNS = 4.5       # 2024 MLB runs per team per game
LEAGUE_AVG_FIP = 4.00       # 2024 MLB FIP baseline
FIP_CONSTANT = 3.20         # constant added to raw FIP to align with ERA scale

# 3-year park run factors by team abbreviation (1.0 = perfectly neutral)
PARK_FACTORS: dict[str, float] = {
    "COL": 1.19,
    "CIN": 1.08,
    "BOS": 1.06,
    "TEX": 1.05,
    "PHI": 1.04,
    "CHW": 1.03,
    "ATL": 1.02,
    "BAL": 1.01,
    "HOU": 1.01,
    "LAA": 1.00,
    "MIA": 1.00,
    "MIL": 1.00,
    "DET": 0.99,
    "PIT": 0.99,
    "MIN": 0.99,
    "KC":  0.98,
    "NYY": 0.98,
    "TOR": 0.98,
    "NYM": 0.97,
    "STL": 0.97,
    "CLE": 0.97,
    "WSH": 0.96,
    "TB":  0.96,
    "OAK": 0.96,
    "CHC": 0.96,
    "ARI": 0.95,
    "LAD": 0.95,
    "SD":  0.94,
    "SEA": 0.93,
    "SF":  0.92,
}


def _f(val, default: float = 0.0) -> float:
    """Safe float conversion — MLB API mixes strings and numbers."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _ip(ip_str) -> float:
    """Convert MLB IP string '100.2' (100 innings + 2 outs) to decimal innings."""
    try:
        s = str(ip_str or "0").strip()
        parts = s.split(".")
        full = int(parts[0]) if parts[0] else 0
        outs = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        return full + outs / 3
    except (ValueError, IndexError):
        return 0.0


def _mlb_get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return {}


def _all_teams() -> list[dict]:
    data = _mlb_get("/teams", {"sportId": 1, "season": datetime.now(timezone.utc).year})
    return data.get("teams", [])


def _match_team(name: str, teams: list[dict]) -> Optional[dict]:
    """Fuzzy-match a user-supplied name to an MLB team object."""
    candidates: dict[str, dict] = {}
    for t in teams:
        for key in ("name", "teamName", "abbreviation", "locationName", "shortName", "clubName"):
            val = t.get(key, "")
            if val:
                candidates[val.lower()] = t

    q = name.lower().strip()
    if q in candidates:
        return candidates[q]

    close = difflib.get_close_matches(q, candidates.keys(), n=1, cutoff=0.45)
    if close:
        return candidates[close[0]]

    for k, t in candidates.items():
        if q in k or k in q:
            return t

    return None


def _current_season() -> int:
    return datetime.now(timezone.utc).year


def compute_fip(stat: dict) -> Optional[float]:
    """
    FIP = ((13×HR) + (3×(BB+HBP)) - (2×K)) / IP + FIP_constant
    Returns None when IP < 1.
    """
    try:
        hr  = _f(stat.get("homeRuns", 0))
        bb  = _f(stat.get("baseOnBalls", 0))
        hbp = _f(stat.get("hitBatsmen", 0))
        k   = _f(stat.get("strikeOuts", 0))
        ip  = _ip(stat.get("inningsPitched", "0"))
        if ip < 1:
            return None
        return round(((13 * hr) + (3 * (bb + hbp)) - (2 * k)) / ip + FIP_CONSTANT, 2)
    except ZeroDivisionError:
        return None


def get_pitcher_season_stats(person_id: int, season: int) -> dict:
    data = _mlb_get(f"/people/{person_id}/stats", {
        "stats": "season",
        "group": "pitching",
        "season": season,
    })
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    raw = splits[0].get("stat", {})
    ip = _ip(raw.get("inningsPitched"))
    fip = compute_fip(raw)
    era = _f(raw.get("era"))
    return {
        "era":           era,
        "fip":           fip if fip is not None else era,
        "whip":          _f(raw.get("whip")),
        "k9":            _f(raw.get("strikeoutsPer9Inn")),
        "bb9":           _f(raw.get("walksPer9Inn")),
        "k_bb":          _f(raw.get("strikeoutWalkRatio")),
        "innings_pitched": round(ip, 1),
        "games_started": int(_f(raw.get("gamesStarted"))),
    }


def get_pitcher_recent_games(person_id: int, season: int, limit: int = 5) -> list[dict]:
    data = _mlb_get(f"/people/{person_id}/stats", {
        "stats": "gameLog",
        "group": "pitching",
        "season": season,
        "gameType": "R",
    })
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    starts = [s for s in splits if int(_f(s.get("stat", {}).get("gamesStarted"))) >= 1]
    starts = starts[-limit:]
    result = []
    for s in reversed(starts):
        stat = s.get("stat", {})
        result.append({
            "date": s.get("date", ""),
            "ip":   round(_ip(stat.get("inningsPitched")), 1),
            "era_game": _f(stat.get("era")),
            "k":    int(_f(stat.get("strikeOuts"))),
            "bb":   int(_f(stat.get("baseOnBalls"))),
            "hr":   int(_f(stat.get("homeRuns"))),
            "runs": int(_f(stat.get("runs"))),
        })
    return result


def get_team_hitting_stats(team_id: int, season: int) -> dict:
    data = _mlb_get(f"/teams/{team_id}/stats", {
        "stats": "season",
        "group": "hitting",
        "season": season,
    })
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    raw = splits[0].get("stat", {})
    ops  = _f(raw.get("ops"))
    runs = _f(raw.get("runs"))
    gp   = max(_f(raw.get("gamesPlayed"), 1), 1)
    pa   = max(_f(raw.get("plateAppearances"), 1), 1)
    # wRC+ approximation: scale OPS against 2024 league avg (.730)
    wrc_plus = round((ops / 0.730) * 100) if ops > 0 else 100
    return {
        "ops":           round(ops, 3),
        "avg":           _f(raw.get("avg")),
        "obp":           _f(raw.get("obp")),
        "slg":           _f(raw.get("slg")),
        "k_pct":         round(_f(raw.get("strikeOuts")) / pa, 3),
        "bb_pct":        round(_f(raw.get("baseOnBalls")) / pa, 3),
        "runs_per_game": round(runs / gp, 2),
        "wrc_plus":      wrc_plus,
    }


def get_team_pitching_stats(team_id: int, season: int) -> dict:
    data = _mlb_get(f"/teams/{team_id}/stats", {
        "stats": "season",
        "group": "pitching",
        "season": season,
    })
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    raw = splits[0].get("stat", {})
    era = _f(raw.get("era"))
    fip = compute_fip(raw)
    return {
        "era":  era,
        "fip":  fip if fip is not None else era,
        "whip": _f(raw.get("whip")),
        "k9":   _f(raw.get("strikeoutsPer9Inn")),
        "bb9":  _f(raw.get("walksPer9Inn")),
    }


def get_team_record(team_id: int, season: int) -> dict:
    data = _mlb_get("/standings", {
        "leagueId":      "103,104",
        "season":         season,
        "standingsTypes": "regularSeason",
    })
    for division in data.get("records", []):
        for rec in division.get("teamRecords", []):
            if rec.get("team", {}).get("id") == team_id:
                w  = int(rec.get("wins", 0))
                l  = int(rec.get("losses", 0))
                gp = w + l
                pct = w / gp if gp > 0 else 0.5
                return {
                    "wins":            w,
                    "losses":          l,
                    "games_played":    gp,
                    "win_pct":         round(pct, 3),
                    "run_differential": int(_f(rec.get("runDifferential"))),
                }
    return {"wins": 0, "losses": 0, "games_played": 0, "win_pct": 0.500, "run_differential": 0}


def get_schedule(date_str: str) -> list[dict]:
    data = _mlb_get("/schedule", {
        "sportId": 1,
        "date":    date_str,
        "hydrate": "probablePitcher,team,venue",
    })
    games = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))
    return games


def _find_game(team_a_id: int, team_b_id: int, date_str: str) -> Optional[dict]:
    for game in get_schedule(date_str):
        home_id = game.get("teams", {}).get("home", {}).get("team", {}).get("id")
        away_id = game.get("teams", {}).get("away", {}).get("team", {}).get("id")
        if {home_id, away_id} == {team_a_id, team_b_id}:
            return game
    return None


def _extract_pitcher(game: dict, side: str) -> Optional[dict]:
    p = game.get("teams", {}).get(side, {}).get("probablePitcher")
    if p:
        return {"id": p.get("id"), "name": p.get("fullName", "TBD")}
    return None


def _build_starter(pitcher_info: Optional[dict], season: int) -> dict:
    if not pitcher_info or not pitcher_info.get("id"):
        return {"name": "TBD", "fip": LEAGUE_AVG_FIP, "era": LEAGUE_AVG_FIP,
                "whip": 0.0, "k9": 0.0, "bb9": 0.0, "k_bb": 0.0,
                "innings_pitched": 0, "games_started": 0, "recent_games": []}
    pid = pitcher_info["id"]
    stats = get_pitcher_season_stats(pid, season)
    recent = get_pitcher_recent_games(pid, season, limit=5)
    return {
        "name":            pitcher_info["name"],
        "id":              pid,
        "fip":             stats.get("fip") or LEAGUE_AVG_FIP,
        "era":             stats.get("era", LEAGUE_AVG_FIP),
        "whip":            stats.get("whip", 0.0),
        "k9":              stats.get("k9", 0.0),
        "bb9":             stats.get("bb9", 0.0),
        "k_bb":            stats.get("k_bb", 0.0),
        "innings_pitched": stats.get("innings_pitched", 0),
        "games_started":   stats.get("games_started", 0),
        "recent_games":    recent,
    }


def fetch_baseball_context(
    team_a: str,
    team_b: str,
    game_date: Optional[str] = None,
) -> dict:
    """
    Main entry point — fetches all data for a baseball matchup:
    team records, hitting stats, probable starters (with FIP), park factor.
    Returns a structured dict ready for the analysis pipeline.
    """
    season = _current_season()
    if not game_date:
        game_date = datetime.now(timezone.utc).date().isoformat()

    sources: list[dict] = []
    all_teams = _all_teams()

    mlb_a = _match_team(team_a, all_teams)
    mlb_b = _match_team(team_b, all_teams)

    if not mlb_a:
        return {"error": f"Could not find MLB team matching '{team_a}'", "sources": sources}
    if not mlb_b:
        return {"error": f"Could not find MLB team matching '{team_b}'", "sources": sources}

    id_a, id_b     = mlb_a["id"], mlb_b["id"]
    name_a, name_b = mlb_a["name"], mlb_b["name"]
    abbr_a, abbr_b = mlb_a.get("abbreviation", ""), mlb_b.get("abbreviation", "")

    sources.append({
        "label":   "MLB Stats API – Teams",
        "url":     f"{BASE_URL}/teams?sportId=1",
        "snippet": f"Matched '{team_a}' → {name_a} (ID {id_a}),  '{team_b}' → {name_b} (ID {id_b})",
    })

    # ── Game / schedule ──────────────────────────────────────────────────────
    game = _find_game(id_a, id_b, game_date)
    venue       = ""
    park_factor = 1.00
    home_team_id: Optional[int] = None

    if game:
        venue        = game.get("venue", {}).get("name", "")
        home_team_id = game.get("teams", {}).get("home", {}).get("team", {}).get("id")
        home_abbr    = abbr_a if home_team_id == id_a else abbr_b
        park_factor  = PARK_FACTORS.get(home_abbr, 1.00)
        sources.append({
            "label":   f"MLB Stats API – Schedule {game_date}",
            "url":     f"{BASE_URL}/schedule?sportId=1&date={game_date}",
            "snippet": f"Game found: {name_a} vs {name_b} at {venue} (park factor {park_factor})",
        })
    else:
        # No scheduled game found — default home to team_a
        home_team_id = id_a
        park_factor  = PARK_FACTORS.get(abbr_a, 1.00)
        sources.append({
            "label":   f"MLB Stats API – Schedule {game_date}",
            "url":     f"{BASE_URL}/schedule?sportId=1&date={game_date}",
            "snippet": f"No game found on {game_date}. Assuming {name_a} is home (park factor {park_factor}).",
        })

    is_home_a = (home_team_id == id_a)

    # ── Team stats ───────────────────────────────────────────────────────────
    hitting_a  = get_team_hitting_stats(id_a, season)
    hitting_b  = get_team_hitting_stats(id_b, season)
    pitching_a = get_team_pitching_stats(id_a, season)
    pitching_b = get_team_pitching_stats(id_b, season)
    record_a   = get_team_record(id_a, season)
    record_b   = get_team_record(id_b, season)

    sources.append({
        "label":   "MLB Stats API – Team Stats",
        "url":     f"{BASE_URL}/teams/stats",
        "snippet": (
            f"{name_a}: wRC+ {hitting_a.get('wrc_plus','?')}, "
            f"OPS {hitting_a.get('ops','?')}, Team ERA {pitching_a.get('era','?')} | "
            f"{name_b}: wRC+ {hitting_b.get('wrc_plus','?')}, "
            f"OPS {hitting_b.get('ops','?')}, Team ERA {pitching_b.get('era','?')}"
        ),
    })

    # ── Probable starters ────────────────────────────────────────────────────
    if game:
        side_a = "home" if is_home_a else "away"
        side_b = "away" if is_home_a else "home"
        starter_a = _build_starter(_extract_pitcher(game, side_a), season)
        starter_b = _build_starter(_extract_pitcher(game, side_b), season)
    else:
        starter_a = _build_starter(None, season)
        starter_b = _build_starter(None, season)

    sources.append({
        "label":   "MLB Stats API – Probable Pitchers",
        "url":     f"{BASE_URL}/people/stats",
        "snippet": (
            f"{name_a} starter: {starter_a['name']} "
            f"(FIP {starter_a['fip']}, ERA {starter_a['era']}) | "
            f"{name_b} starter: {starter_b['name']} "
            f"(FIP {starter_b['fip']}, ERA {starter_b['era']})"
        ),
    })

    return {
        "team_a": {
            "name":         name_a,
            "id":           id_a,
            "abbreviation": abbr_a,
            "is_home":      is_home_a,
            "record":       record_a,
            "hitting":      hitting_a,
            "team_pitching": pitching_a,
            "starter":      starter_a,
        },
        "team_b": {
            "name":         name_b,
            "id":           id_b,
            "abbreviation": abbr_b,
            "is_home":      not is_home_a,
            "record":       record_b,
            "hitting":      hitting_b,
            "team_pitching": pitching_b,
            "starter":      starter_b,
        },
        "game": {
            "game_pk":    game.get("gamePk") if game else None,
            "date":       game_date,
            "venue":      venue,
            "park_factor": park_factor,
            "home_team":  name_a if is_home_a else name_b,
            "away_team":  name_b if is_home_a else name_a,
        },
        "sources": sources,
    }
