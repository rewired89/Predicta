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


def _decayed_xg_from_history(completed: list, half_life_days: float) -> dict:
    """
    Inner: given a list of completed match dicts, compute time-decayed averages.

    Kimi #1 correction: effective_n now uses Kish's effective-sample-size
    formula  n_eff = (Σw)² / Σ(w²)  instead of plain Σw. The Σw form
    over-counts when weights are dispersed; Kish n_eff equals raw n at equal
    weights and shrinks as weights spread out — the right quantity for
    Bayesian shrinkage and variance bounds.
    """
    from datetime import datetime as _dt
    today = _dt.utcnow()
    sum_w = 0.0
    sum_w2 = 0.0
    sum_xg = 0.0
    sum_xga = 0.0
    sum_g = 0.0
    sum_ga = 0.0
    for g in completed:
        try:
            d = _dt.strptime(str(g.get("date", "")).split(" ")[0], "%Y-%m-%d")
            days_ago = max(0.0, (today - d).total_seconds() / 86400.0)
        except Exception:
            days_ago = 0.0
        w = 0.5 ** (days_ago / max(1.0, half_life_days))
        sum_w  += w
        sum_w2 += w * w
        sum_xg  += w * float(g.get("xG",  0) or 0)
        sum_xga += w * float(g.get("xGA", 0) or 0)
        sum_g   += w * float(g.get("scored", 0) or 0)
        sum_ga  += w * float(g.get("missed", 0) or 0)
    if sum_w <= 0 or sum_w2 <= 0:
        return {}
    effective_n_kish = (sum_w * sum_w) / sum_w2
    return {
        "xg_per_game":    sum_xg  / sum_w,
        "xga_per_game":   sum_xga / sum_w,
        "goals_per_game": sum_g   / sum_w,
        "ga_per_game":    sum_ga  / sum_w,
        "matches_used":   len(completed),
        "effective_n":    round(effective_n_kish, 2),
        "sum_weights":    round(sum_w, 3),
        "half_life_days": half_life_days,
    }


def fetch_team_decayed_xg(
    team_name: str,
    league: str,
    season: int,
    half_life_days: float = 90.0,
    adaptive: bool = True,
) -> dict:
    """
    Time-decayed xG/xGA over the full available season history.

    Each match weight = 0.5 ** (days_ago / half_life_days). 90-day half-life:
    a 3-month-old match counts half a recent match, 6-month half is a quarter.

    adaptive=True (Kimi #4 + own elaboration): if Kish effective_n < 12 at the
    requested half-life, retry at 150 days to lengthen the window for sparse
    data (early season / mid-season for promoted teams). If effective_n stays
    < 8 even at 150d, fall back to 240d. Never goes below the input half-life.

    Returns: xg_per_game, xga_per_game, goals_per_game, ga_per_game,
             matches_used, effective_n (Kish formula), sum_weights,
             half_life_days_used, source. Empty dict on failure.
    """
    canonical = _ESPN_TO_UNDERSTAT.get(team_name, team_name)
    url = f"{UNDERSTAT_BASE}/team/{canonical.replace(' ', '_')}/{season}"
    html = _get(url)
    if not html:
        return {}

    history = _extract_json_var(html, "datesData")
    if not history or not isinstance(history, list):
        return {}

    completed = [g for g in history if g.get("isResult") and g.get("xG") is not None]
    if not completed:
        return {}

    hl_candidates = [half_life_days] if not adaptive else [half_life_days, 150.0, 240.0]
    best = None
    for hl in hl_candidates:
        if hl < half_life_days:
            continue
        r = _decayed_xg_from_history(completed, hl)
        if not r:
            continue
        best = r
        if r["effective_n"] >= 12:
            break

    if best is None:
        return {}

    return {
        "xg_per_game":          round(best["xg_per_game"],    3),
        "xga_per_game":         round(best["xga_per_game"],   3),
        "goals_per_game":       round(best["goals_per_game"], 3),
        "ga_per_game":          round(best["ga_per_game"],    3),
        "matches_used":         best["matches_used"],
        "effective_n":          best["effective_n"],
        "sum_weights":          best["sum_weights"],
        "half_life_days_used":  best["half_life_days"],
        "source":               f"understat/{league}/{season} decayed (HL={best['half_life_days']:.0f}d, n_eff={best['effective_n']:.1f})",
    }


def fetch_league_avg_xg(league: str, season: int) -> Optional[float]:
    """
    League-wide avg expected goals per team per game from the Understat table.

    Goals (the existing fetch_league_avg_goals) under-anchor the model because
    xG is ~5% higher than goals (finishing variance). Using xG as the anchor
    makes attack/defense multipliers correctly scaled — was Kimi's #6 fix.

    Returns None on failure so callers can fall back to a constant.
    """
    rows = fetch_league_xg(league, season)
    if not rows:
        return None
    total_xg = sum(float(r.get("xg", 0) or 0) for r in rows)
    total_games = sum(int(r.get("matches", 0) or 0) for r in rows)
    if total_games <= 0:
        return None
    return round(total_xg / total_games, 3)


def fetch_team_venue_splits(
    team_name: str,
    league: str,
    season: int,
) -> dict:
    """
    Home/away xG splits derived from the team's per-game datesData.

    Returns:
      home_xg_per_game, home_xga_per_game, home_npxg_per_game, home_npxga_per_game,
      away_xg_per_game, away_xga_per_game, away_npxg_per_game, away_npxga_per_game,
      home_matches, away_matches.

    Empty dict on failure. Critical for proper Dixon-Coles — home advantage is
    not a single multiplier; teams differ wildly in venue-specific xG.
    """
    canonical = _ESPN_TO_UNDERSTAT.get(team_name, team_name)
    url = f"{UNDERSTAT_BASE}/team/{canonical.replace(' ', '_')}/{season}"
    html = _get(url)
    if not html:
        return {}

    history = _extract_json_var(html, "datesData")
    if not history or not isinstance(history, list):
        return {}

    home_games = [g for g in history if g.get("isResult") and g.get("h_a") == "h"]
    away_games = [g for g in history if g.get("isResult") and g.get("h_a") == "a"]

    def _avg(games: list, key: str) -> Optional[float]:
        if not games:
            return None
        try:
            return sum(float(g.get(key, 0) or 0) for g in games) / len(games)
        except Exception:
            return None

    out: dict = {
        "home_xg_per_game":     _avg(home_games, "xG"),
        "home_xga_per_game":    _avg(home_games, "xGA"),
        "home_npxg_per_game":   _avg(home_games, "npxG"),
        "home_npxga_per_game":  _avg(home_games, "npxGA"),
        "away_xg_per_game":     _avg(away_games, "xG"),
        "away_xga_per_game":    _avg(away_games, "xGA"),
        "away_npxg_per_game":   _avg(away_games, "npxG"),
        "away_npxga_per_game":  _avg(away_games, "npxGA"),
        "home_matches":         len(home_games),
        "away_matches":         len(away_games),
        "source":               f"understat/{league}/{season} venue splits",
    }
    # Drop None entries so downstream `.get(... , default)` works cleanly
    return {k: v for k, v in out.items() if v is not None or k in ("home_matches", "away_matches")}


def fetch_team_situation_split(
    team_name: str,
    league: str,
    season: int,
) -> dict:
    """
    Split team's offensive xG by Understat shot `situation` field.

    Understat tags every shot with one of:
      OpenPlay, FromCorner, SetPiece, DirectFreekick, Penalty.

    Open-play xG is materially more predictive of future scoring than the
    full season figure because set pieces and penalties are noisy and
    over-represent good or bad luck on dead-ball routines / referee decisions.

    Returns:
      open_play_xg_share, set_piece_xg_share, corner_xg_share,
      direct_fk_xg_share, penalty_xg_share, total_shots,
      open_play_xg_per_shot — quality of build-up chances.

    Empty dict on failure.
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
        all_shots: list = []
        if isinstance(shots, dict):
            all_shots = list(shots.get("h", [])) + list(shots.get("a", []))
        elif isinstance(shots, list):
            all_shots = list(shots)

        # Filter to this team's OWN shots
        own_shots = [
            s for s in all_shots
            if (_norm(s.get("h_team", "")) == _norm(canonical) and s.get("h_a") == "h")
            or (_norm(s.get("a_team", "")) == _norm(canonical) and s.get("h_a") == "a")
        ]
        if not own_shots:
            return {}

        buckets: dict[str, float] = {
            "OpenPlay":       0.0,
            "FromCorner":     0.0,
            "SetPiece":       0.0,
            "DirectFreekick": 0.0,
            "Penalty":        0.0,
        }
        open_play_shots = 0
        for s in own_shots:
            sit = s.get("situation", "")
            xg  = float(s.get("xG", 0) or 0)
            if sit in buckets:
                buckets[sit] += xg
            if sit == "OpenPlay":
                open_play_shots += 1

        total_xg = sum(buckets.values())
        if total_xg <= 0:
            return {}

        return {
            "open_play_xg_share":   round(buckets["OpenPlay"]       / total_xg, 4),
            "corner_xg_share":      round(buckets["FromCorner"]     / total_xg, 4),
            "set_piece_xg_share":   round((buckets["SetPiece"] + buckets["FromCorner"] + buckets["DirectFreekick"]) / total_xg, 4),
            "direct_fk_xg_share":   round(buckets["DirectFreekick"] / total_xg, 4),
            "penalty_xg_share":     round(buckets["Penalty"]        / total_xg, 4),
            "total_shots":          len(own_shots),
            "open_play_xg_per_shot": round(buckets["OpenPlay"] / open_play_shots, 4) if open_play_shots else None,
            "source":               f"understat/{league}/{season} situation split",
        }
    except Exception:
        return {}


def fetch_league_avg_goals(league: str, season: int) -> Optional[float]:
    """
    Compute league-wide avg goals per team per game from the league table.

    Critical because hardcoded 1.35 over-fits EPL/La Liga and badly mis-scales
    Bundesliga (~1.55) and Ligue 1 (~1.20). Returns None on failure so callers
    can fall back to a sport-wide constant.
    """
    rows = fetch_league_xg(league, season)
    if not rows:
        return None
    total_goals = sum(int(r.get("goals", 0)) for r in rows)
    total_games = sum(int(r.get("matches", 0)) for r in rows)
    if total_games <= 0:
        return None
    # Each match contributes 2 team-games to the matches counter
    return round(total_goals / total_games, 3)


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
    Enrich both teams with Understat xG + shot quality + venue + situation splits.

    Returns {
        "home": {... see fields below ...},
        "away": {... same ...},
        "league_avg_goals": float | None,
    }
    Both team dicts are empty on failure — caller should fall back to AI signals.

    Fields per team (when fetch succeeds):
      Season:        xg, xga, npxg, npxga, xg_per_game, xga_per_game,
                     goals, goals_against, matches, position
      Per-game derived: npxg_per_game, npxga_per_game
      Recent (last 5): recent_xg_per_game, recent_xga_per_game
      Shot quality:  xg_per_shot, xga_per_shot, sot_pct, goal_overperform
      Venue splits:  home_xg_per_game / home_xga_per_game / away_xg_per_game /
                     away_xga_per_game (+ npxg variants), home_matches, away_matches
      Situation:     open_play_xg_share, set_piece_xg_share, corner_xg_share,
                     penalty_xg_share, open_play_xg_per_shot

    league_avg_goals reflects the actual scoring environment for this league/season
    (e.g. Bundesliga ~1.55, Ligue 1 ~1.25) — feeds Dixon-Coles directly.
    """
    home = fetch_team_xg(home_name, league, season)
    away = fetch_team_xg(away_name, league, season)
    # Prefer exponentially time-decayed xG (Kimi #2). Falls back to fixed last-5
    # if the decayed fetcher fails (no datesData, parse error, etc.).
    home_recent = fetch_team_decayed_xg(home_name, league, season) \
                  or fetch_team_recent_xg(home_name, league, season)
    away_recent = fetch_team_decayed_xg(away_name, league, season) \
                  or fetch_team_recent_xg(away_name, league, season)
    home_shots  = fetch_team_shot_quality(home_name, league, season)
    away_shots  = fetch_team_shot_quality(away_name, league, season)
    home_venue  = fetch_team_venue_splits(home_name, league, season)
    away_venue  = fetch_team_venue_splits(away_name, league, season)
    home_sit    = fetch_team_situation_split(home_name, league, season)
    away_sit    = fetch_team_situation_split(away_name, league, season)

    # Per-game npxG (penalty-stripped — sportsbook standard input)
    if home and home.get("matches"):
        home["npxg_per_game"]  = round(home.get("npxg", 0)  / home["matches"], 3)
        home["npxga_per_game"] = round(home.get("npxga", 0) / home["matches"], 3)
    if away and away.get("matches"):
        away["npxg_per_game"]  = round(away.get("npxg", 0)  / away["matches"], 3)
        away["npxga_per_game"] = round(away.get("npxga", 0) / away["matches"], 3)

    if home_recent:
        home["recent_xg_per_game"]  = home_recent.get("xg_per_game")
        home["recent_xga_per_game"] = home_recent.get("xga_per_game")
        # Propagate Kish effective_n + chosen half-life from the time-decayed
        # fetcher so downstream shrinkage uses the right denominator.
        if home_recent.get("effective_n") is not None:
            home["effective_n"]         = home_recent["effective_n"]
            home["half_life_days_used"] = home_recent.get("half_life_days_used")
    if away_recent:
        away["recent_xg_per_game"]  = away_recent.get("xg_per_game")
        away["recent_xga_per_game"] = away_recent.get("xga_per_game")
        if away_recent.get("effective_n") is not None:
            away["effective_n"]         = away_recent["effective_n"]
            away["half_life_days_used"] = away_recent.get("half_life_days_used")

    for k in ("xg_per_shot", "xga_per_shot", "sot_pct", "goal_overperform"):
        if home_shots.get(k) is not None:
            home[k] = home_shots[k]
        if away_shots.get(k) is not None:
            away[k] = away_shots[k]

    for k, v in home_venue.items():
        if v is not None and k != "source":
            home[k] = v
    for k, v in away_venue.items():
        if v is not None and k != "source":
            away[k] = v

    for k in ("open_play_xg_share", "set_piece_xg_share", "corner_xg_share",
              "direct_fk_xg_share", "penalty_xg_share", "open_play_xg_per_shot",
              "total_shots"):
        if home_sit.get(k) is not None:
            home[k] = home_sit[k]
        if away_sit.get(k) is not None:
            away[k] = away_sit[k]

    league_avg_goals_live = fetch_league_avg_goals(league, season)
    league_avg_xg_live    = fetch_league_avg_xg(league, season)

    return {
        "home": home,
        "away": away,
        "league_avg_goals": league_avg_goals_live,
        "league_avg_xg":    league_avg_xg_live,
    }


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
