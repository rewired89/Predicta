"""
Per-league constants for the soccer prediction model.

Hardcoded league averages exist so the model works even when Understat /
FBref are unreachable. When live data is available, fetch_league_avg_goals()
in fetchers/understat.py overrides these — but these stay as the
guaranteed fallback that keeps Bundesliga 1.55 not 1.35.

Sources:
  goals_per_team_per_game — 5-season average (2019-2024) from FBref league totals
  home_advantage          — multiplicative on home attack; derived from
                            league home win-rate vs neutral expectation
  open_play_xg_share      — fraction of league xG that comes from open play
                            (rest is set-piece + penalties); from Understat
                            shotsData situation aggregation, 5-season window.

Numbers are conservative midpoints; calibrate against the live Understat
league_avg_goals when available.
"""
from __future__ import annotations
from typing import Optional


# Sport-wide fallback when league is unknown
DEFAULT_LEAGUE_AVG_GOALS = 1.40
DEFAULT_LEAGUE_AVG_XG    = 1.47   # ~5% higher than goals (finishing variance, Kimi #6)
DEFAULT_HOME_ADVANTAGE   = 1.15
DEFAULT_OPEN_PLAY_SHARE  = 0.78


_LEAGUES: dict[str, dict[str, float]] = {
    "EPL": {
        "goals_per_team_per_game": 1.43,
        "xg_per_team_per_game":    1.51,
        "home_advantage":          1.13,
        "open_play_xg_share":      0.79,
    },
    "La_liga": {
        "goals_per_team_per_game": 1.31,
        "xg_per_team_per_game":    1.38,
        "home_advantage":          1.18,
        "open_play_xg_share":      0.76,
    },
    "Bundesliga": {
        "goals_per_team_per_game": 1.55,
        "xg_per_team_per_game":    1.62,
        "home_advantage":          1.10,
        "open_play_xg_share":      0.78,
    },
    "Serie_A": {
        "goals_per_team_per_game": 1.38,
        "xg_per_team_per_game":    1.45,
        "home_advantage":          1.16,
        "open_play_xg_share":      0.77,
    },
    "Ligue_1": {
        "goals_per_team_per_game": 1.28,
        "xg_per_team_per_game":    1.36,
        "home_advantage":          1.17,
        "open_play_xg_share":      0.79,
    },
    "RFPL": {
        "goals_per_team_per_game": 1.30,
        "xg_per_team_per_game":    1.37,
        "home_advantage":          1.20,
        "open_play_xg_share":      0.78,
    },
}


def league_avg_goals(league: Optional[str]) -> float:
    """Return goals-per-team-per-game for the named league, or default."""
    if not league:
        return DEFAULT_LEAGUE_AVG_GOALS
    return _LEAGUES.get(league, {}).get(
        "goals_per_team_per_game", DEFAULT_LEAGUE_AVG_GOALS
    )


def league_avg_xg(league: Optional[str]) -> float:
    """
    Return xG-per-team-per-game for the named league, or default.

    Preferred anchor over league_avg_goals because the model produces xG, not
    finished goals. Mis-anchoring on goals systematically overrates attack and
    underrates defense by ~5% (Kimi #6).
    """
    if not league:
        return DEFAULT_LEAGUE_AVG_XG
    return _LEAGUES.get(league, {}).get(
        "xg_per_team_per_game", DEFAULT_LEAGUE_AVG_XG
    )


def goals_to_xg_ratio(league: Optional[str]) -> float:
    """
    League-specific multiplier to convert a goals-anchor into an xG-anchor
    (Kimi #7 — dynamic ratio replacing the blanket 1.05).

    Computed from _LEAGUES table: xg_per_team_per_game / goals_per_team_per_game.
    Falls back to the global average (~1.05) for unknown leagues. Used when
    Understat returns live league_avg_goals but not live league_avg_xg.
    """
    if not league:
        return DEFAULT_LEAGUE_AVG_XG / DEFAULT_LEAGUE_AVG_GOALS
    entry = _LEAGUES.get(league, {})
    g = entry.get("goals_per_team_per_game")
    x = entry.get("xg_per_team_per_game")
    if g and x and g > 0:
        return x / g
    return DEFAULT_LEAGUE_AVG_XG / DEFAULT_LEAGUE_AVG_GOALS


def home_advantage(league: Optional[str]) -> float:
    """Return league-specific home-attack multiplier."""
    if not league:
        return DEFAULT_HOME_ADVANTAGE
    return _LEAGUES.get(league, {}).get("home_advantage", DEFAULT_HOME_ADVANTAGE)


def open_play_share(league: Optional[str]) -> float:
    """Return league-typical open-play xG share."""
    if not league:
        return DEFAULT_OPEN_PLAY_SHARE
    return _LEAGUES.get(league, {}).get("open_play_xg_share", DEFAULT_OPEN_PLAY_SHARE)


def known_leagues() -> list[str]:
    return list(_LEAGUES.keys())
