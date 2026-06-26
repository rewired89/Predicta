"""
Understat soccer xG data fetcher.

Understat (understat.com) publishes expected goals (xG), xGA, and shot maps
for 6 European leagues: EPL, La Liga, Bundesliga, Serie A, Ligue 1, RFPL.
Data is embedded as JSON in the page HTML — no API key required.

Access strategy:
  1. Fetch team page HTML (understat.com/team/<team>/<year>)
  2. Extract JSON from window.datesData / window.statisticsData via regex
  3. Parse into structured dicts
  4. All functions return empty dict / None on failure

Leagues supported:
  EPL, La_liga, Bundesliga, Serie_A, Ligue_1, RFPL (Russian Premier)

Accuracy impact:
  xG per game (rolling 5) is a better predictor than goals scored (0.62 vs 0.54 R²)
  xGA (expected goals against) better than GA for goalkeeper/defense quality
  NPxG (non-penalty xG) removes noise from penalty frequency
"""
from __future__ import annotations
import json
import re
from typing import Optional

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

UNDERSTAT_BASE = "https://understat.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Module-level cache keyed by (league, season)
_LEAGUE_CACHE: dict[tuple, list] = {}

# ESPN/common name → Understat team name (where they differ)
_ESPN_TO_UNDERSTAT: dict[str, str] = {
    # EPL
    "Man United":     "Manchester United",
    "Man City":       "Manchester City",
    "Spurs":          "Tottenham",
    "Tottenham Hotspur": "Tottenham",
    "Newcastle":      "Newcastle United",
    "Brighton":       "Brighton",
    "Wolves":         "Wolverhampton Wanderers",
    "West Ham":       "West Ham",
    "Nott'm Forest":  "Nottingham Forest",
    "Nottingham Forest": "Nottingham Forest",
    # La Liga
    "Atletico":       "Atletico Madrid",
    "Atletico Madrid": "Atletico Madrid",
    "Betis":          "Real Betis",
    # Bundesliga
    "Bayern":         "Bayern Munich",
    "Dortmund":       "Borussia Dortmund",
    "BVB":            "Borussia Dortmund",
    "Gladbach":       "Borussia M'gladbach",
    "M'gladbach":     "Borussia M'gladbach",
    # Serie A
    "Inter":          "Internazionale",
    "Inter Milan":    "Internazionale",
    "AC Milan":       "AC Milan",
    "Milan":          "AC Milan",
    "Roma":           "AS Roma",
    # Ligue 1
    "PSG":            "Paris Saint-Germain",
    "Saint-Etienne":  "Saint-Etienne",
}

# League slug → ESPN competition name (used by analyze_soccer.py)
LEAGUE_SLUGS = {
    "EPL":        "EPL",
    "La_liga":    "La_liga",
    "Bundesliga": "Bundesliga",
    "Serie_A":    "Serie_A",
    "Ligue_1":    "Ligue_1",
    "RFPL":       "RFPL",
}


def _get(url: str) -> Optional[str]:
    """Fetch HTML with retry; returns None on failure."""
    if not _HAS_HTTPX:
        return None
    try:
        with httpx.Client(timeout=15.0, headers=_HEADERS, follow_redirects=True) as c:
            resp = c.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception:
        return None


def _extract_json_var(html: str, var_name: str) -> Optional[list | dict]:
    """Extract a JSON variable embedded in Understat HTML via regex."""
    pattern = rf"var\s+{re.escape(var_name)}\s*=\s*JSON\.parse\('(.+?)'\)"
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return None
    try:
        raw = m.group(1)
        # Understat unicode-escapes the JSON string
        unescaped = raw.encode("utf-8").decode("unicode_escape")
        return json.loads(unescaped)
    except Exception:
        pass
    # Fallback: direct assignment (some pages)
    pattern2 = rf"var\s+{re.escape(var_name)}\s*=\s*(\[.+?\]|\{{.+?\}})\s*;"
    m2 = re.search(pattern2, html, re.DOTALL)
    if m2:
        try:
            return json.loads(m2.group(1))
        except Exception:
            pass
    return None


def fetch_league_xg(
    league: str,
    season: int,
) -> list[dict]:
    """
    Fetch all teams' xG table for a league/season from Understat.

    Returns list of dicts with: team, xG, xGA, npxG, npxGA, matches, goals,
    goals_against, pts, position. Empty list on failure.

    league: one of EPL | La_liga | Bundesliga | Serie_A | Ligue_1 | RFPL
    season: year the season started (e.g. 2023 for 2023/24)
    """
    cache_key = (league, season)
    if cache_key in _LEAGUE_CACHE:
        return _LEAGUE_CACHE[cache_key]

    html = _get(f"{UNDERSTAT_BASE}/league/{league}/{season}")
    if not html:
        return []

    data = _extract_json_var(html, "teamsData")
    if not data:
        return []

    rows: list[dict] = []
    try:
        for team_id, team_obj in (data.items() if isinstance(data, dict) else []):
            info = team_obj if isinstance(team_obj, dict) else {}
            title = info.get("title", "")
            history = info.get("history", [])
            if not history:
                continue
            # Aggregate season totals from per-game history
            totals = {
                "xg":          sum(float(g.get("xG", 0)) for g in history),
                "xga":         sum(float(g.get("xGA", 0)) for g in history),
                "npxg":        sum(float(g.get("npxG", 0)) for g in history),
                "npxga":       sum(float(g.get("npxGA", 0)) for g in history),
                "goals":       sum(int(g.get("scored", 0)) for g in history),
                "goals_against": sum(int(g.get("missed", 0)) for g in history),
                "matches":     len(history),
                "pts":         sum(int(g.get("pts", 0)) for g in history),
            }
            totals["team"] = title
            totals["xg_per_game"]  = totals["xg"] / totals["matches"] if totals["matches"] else 0
            totals["xga_per_game"] = totals["xga"] / totals["matches"] if totals["matches"] else 0
            rows.append(totals)
    except Exception:
        return []

    # Sort by pts desc to approximate league table position
    rows.sort(key=lambda r: r.get("pts", 0), reverse=True)
    for i, r in enumerate(rows, 1):
        r["position"] = i

    _LEAGUE_CACHE[cache_key] = rows
    return rows


def fetch_team_xg(
    team_name: str,
    league: str,
    season: int,
) -> dict:
    """
    Fetch a single team's xG stats for a league/season.

    team_name: any common variant; matched via _ESPN_TO_UNDERSTAT mapping + fuzzy.
    Returns dict with xg, xga, npxg, npxga, xg_per_game, xga_per_game,
    goals, goals_against, matches, position, source. Empty dict on failure.
    """
    rows = fetch_league_xg(league, season)
    if not rows:
        return {}

    canonical = _ESPN_TO_UNDERSTAT.get(team_name, team_name)
    teams = [r["team"] for r in rows]

    matched = _fuzzy_match(canonical, teams)
    if not matched:
        matched = _fuzzy_match(team_name, teams)
    if not matched:
        return {}

    row = next((r for r in rows if r["team"] == matched), None)
    if not row:
        return {}

    return {**row, "source": f"understat/{league}/{season}"}


def fetch_team_recent_xg(
    team_name: str,
    league: str,
    season: int,
    last_n: int = 5,
) -> dict:
    """
    Rolling xG/xGA averages over the last N matches (form indicator).

    More predictive than season totals when teams have changed form mid-season.
    Returns: xg_per_game, xga_per_game, goals_per_game, ga_per_game, matches_used.
    Empty dict on failure.
    """
    canonical = _ESPN_TO_UNDERSTAT.get(team_name, team_name)
    url = f"{UNDERSTAT_BASE}/team/{canonical.replace(' ', '_')}/{season}"
    html = _get(url)
    if not html:
        return {}

    history = _extract_json_var(html, "datesData")
    if not history or not isinstance(history, list):
        return {}

    # Filter to completed home/away matches and take last N
    completed = [
        g for g in history
        if g.get("isResult") and g.get("xG") is not None
    ][-last_n:]
    if not completed:
        return {}

    n = len(completed)
    return {
        "xg_per_game":    sum(float(g.get("xG", 0)) for g in completed) / n,
        "xga_per_game":   sum(float(g.get("xGA", 0)) for g in completed) / n,
        "goals_per_game": sum(int(g.get("scored", 0)) for g in completed) / n,
        "ga_per_game":    sum(int(g.get("missed", 0)) for g in completed) / n,
        "matches_used":   n,
        "source":         f"understat/{league}/{season} last {n}",
    }


def fetch_team_shot_quality(
    team_name: str,
    league: str,
    season: int,
) -> dict:
    """
    Fetch shot-level quality metrics from Understat team page.

    Parses shotData JSON embedded in the team page to compute:
      xg_per_shot       — shot quality (higher = better chances created)
      xga_per_shot      — quality conceded per shot
      shots_per_game    — volume (high volume + high xg_per_shot = dominant)
      sot_pct           — shots on target % (finishing quality proxy)
      goal_overperform  — goals / xG ratio > 1 means exceeding expectations
                          (regression candidate; <0.85 = unlucky, >1.15 = lucky)
      npxg_per_shot     — non-penalty xG per shot (removes set-piece inflation)

    These are the Kimi-recommended event-level features — more predictive than
    raw goals because they measure process quality not just outcomes.
    """
    canonical = _ESPN_TO_UNDERSTAT.get(team_name, team_name)
    url = f"{UNDERSTAT_BASE}/team/{canonical.replace(' ', '_')}/{season}"
    html = _get(url)
    if not html:
        return {}

    shots = _extract_json_var(html, "shotsData")
    if not shots:
        return {}

    try:
        # shotsData is {"h": [...shots...], "a": [...shots...]}
        all_shots: list[dict] = []
        conceded_shots: list[dict] = []
        if isinstance(shots, dict):
            all_shots     = shots.get("h", []) + shots.get("a", [])
            # For team shots: filter where team == canonical title
            # For conceded: the opponent's shots
            team_shots    = [s for s in all_shots if _norm(s.get("h_team", "")) == _norm(canonical)
                             or _norm(s.get("a_team", "")) == _norm(canonical)]
            # Separate into "team's own shots" vs "shots against"
            own_shots = [
                s for s in team_shots
                if (_norm(s.get("h_team", "")) == _norm(canonical) and s.get("h_a") == "h")
                or (_norm(s.get("a_team", "")) == _norm(canonical) and s.get("h_a") == "a")
            ]
            against_shots = [
                s for s in team_shots
                if s not in own_shots
            ]
        elif isinstance(shots, list):
            own_shots     = shots
            against_shots = []

        if not own_shots:
            return {}

        xg_vals  = [float(s.get("xG", 0)) for s in own_shots]
        g_vals   = [int(s.get("result", "") == "Goal") for s in own_shots]
        sot_vals = [1 for s in own_shots if s.get("result", "") not in ("MissedShots", "BlockedShot")]

        total_xg    = sum(xg_vals)
        total_goals = sum(g_vals)
        n_shots     = len(own_shots)
        n_sot       = len(sot_vals)

        xga_vals = [float(s.get("xG", 0)) for s in against_shots] if against_shots else []

        result = {
            "xg_per_shot":      round(total_xg / n_shots, 4) if n_shots else None,
            "shots_per_game":   None,    # needs match count — filled below if available
            "sot_pct":          round(n_sot / n_shots * 100, 1) if n_shots else None,
            "goal_overperform": round(total_goals / total_xg, 3) if total_xg > 0 else None,
            "source":           f"understat/{league}/{season} shots",
        }
        if xga_vals:
            result["xga_per_shot"] = round(sum(xga_vals) / len(xga_vals), 4)

        return result
    except Exception:
        return {}


def enrich_soccer_teams(
    home_name: str,
    away_name: str,
    league: str,
    season: int,
) -> dict:
    """
    Enrich both teams with Understat xG + shot quality data.

    Returns {
        "home": {xg, xga, xg_per_game, xga_per_game, recent_xg, recent_xga,
                 xg_per_shot, sot_pct, goal_overperform, ...},
        "away": {...},
    }
    Both values are empty dicts on failure — soccer pipeline falls back to ESPN.

    Key signals for Dixon-Coles model:
      recent_xg_per_game  — last-5 attack form (more predictive than season avg)
      recent_xga_per_game — last-5 defense form
      goal_overperform    — regression signal: >1.15 likely to regress down
      xg_per_shot         — shot quality; high xg teams hit fewer big chances
    """
    home = fetch_team_xg(home_name, league, season)
    away = fetch_team_xg(away_name, league, season)
    home_recent = fetch_team_recent_xg(home_name, league, season)
    away_recent = fetch_team_recent_xg(away_name, league, season)
    home_shots  = fetch_team_shot_quality(home_name, league, season)
    away_shots  = fetch_team_shot_quality(away_name, league, season)

    if home_recent:
        home["recent_xg_per_game"]  = home_recent.get("xg_per_game")
        home["recent_xga_per_game"] = home_recent.get("xga_per_game")
    if away_recent:
        away["recent_xg_per_game"]  = away_recent.get("xg_per_game")
        away["recent_xga_per_game"] = away_recent.get("xga_per_game")

    # Merge shot quality into main team dict
    for k in ("xg_per_shot", "xga_per_shot", "sot_pct", "goal_overperform"):
        if home_shots.get(k) is not None:
            home[k] = home_shots[k]
        if away_shots.get(k) is not None:
            away[k] = away_shots[k]

    return {"home": home, "away": away}


# ── Name matching ─────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _fuzzy_match(target: str, names: list[str], cutoff: float = 0.78) -> Optional[str]:
    """Case-insensitive fuzzy match; returns best match or None."""
    import difflib
    norm_target = _norm(target)
    norm_names  = [_norm(n) for n in names]

    if norm_target in norm_names:
        return names[norm_names.index(norm_target)]

    close = difflib.get_close_matches(norm_target, norm_names, n=1, cutoff=cutoff)
    if close:
        return names[norm_names.index(close[0])]
    return None
