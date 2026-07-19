"""
Schedule context: bullpen fatigue and rest-day adjustment for MLB.

Kimi (and the research) identifies bullpen fatigue as a near-term +1-2% feature.
Approach: for each team, count how many games they played in the last 3 days.
More games = higher bullpen fatigue multiplier applied to bullpen_fip.

Data source: ESPN /scoreboard (same as results_collector — no new API required).

Fatigue model (conservative/evidence-based):
  0 games in 3 days (3+ days rest)  → fatigue_mult = 0.97  (well-rested, -3% FIP)
  1 game  in 3 days (normal)        → fatigue_mult = 1.00
  2 games in 3 days                 → fatigue_mult = 1.04  (+4% worse bullpen)
  3 games in 3 days                 → fatigue_mult = 1.08  (+8% worse bullpen)

Rest days affect offense slightly (fresher hitters, better approach):
  0 rest days (back-to-back)        → off_rest_mult = 0.99
  1 rest day (normal)               → off_rest_mult = 1.00
  2+ rest days                      → off_rest_mult = 1.01
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional


def _scoreboard_for(date_str: str) -> list[dict]:
    """Get ESPN MLB scoreboard events for a date, returning [] on failure."""
    try:
        from fetchers.baseball import _get_scoreboard
        return _get_scoreboard(date_str)
    except Exception:
        return []


def _team_played_on(team_id: str, events: list[dict]) -> bool:
    """Return True if team_id appears in any game on this scoreboard."""
    for event in events:
        for comp in event.get("competitions", []):
            ids = {str(c.get("team", {}).get("id", ""))
                   for c in comp.get("competitors", [])}
            if team_id in ids:
                return True
    return False


def _days_since_last_game(team_id: str, game_date: str) -> Optional[int]:
    """
    Walk back from the day before game_date to find the most recent game.
    Returns number of days since that game (0 = played yesterday), or None.
    Looks back up to 10 days.
    """
    today = date.fromisoformat(game_date)
    for offset in range(1, 11):
        d = (today - timedelta(days=offset)).isoformat()
        events = _scoreboard_for(d)
        if _team_played_on(team_id, events):
            return offset - 1   # 0 = yesterday, 1 = day before, etc.
    return None


def fetch_team_fatigue(
    team_id: str,
    game_date: str,
) -> dict:
    """
    Compute bullpen fatigue and rest context for a team ahead of a game.

    Returns:
      games_last_3:       number of games played in last 3 calendar days
      rest_days:          days since most recent game (None if unknown)
      bullpen_fatigue_mult: multiplier to apply to bullpen_fip (>1 = tired)
      off_rest_mult:      multiplier to apply to wrc_plus (>1 = fresh offense)
      source:             "espn_schedule"

    Falls back to neutral values (all multipliers = 1.0) on any failure.
    """
    neutral = {
        "games_last_3":          None,
        "rest_days":             None,
        "bullpen_fatigue_mult":  1.0,
        "off_rest_mult":         1.0,
        "source":                "schedule_unavailable",
    }

    if not team_id:
        return neutral

    try:
        today = date.fromisoformat(game_date)
        games_last_3 = 0
        last_game_days = None

        for offset in range(1, 4):
            d = (today - timedelta(days=offset)).isoformat()
            events = _scoreboard_for(d)
            if _team_played_on(team_id, events):
                games_last_3 += 1
                if last_game_days is None:
                    last_game_days = offset - 1  # 0=yesterday, 1=day before

        # Bullpen fatigue multiplier. MLB teams play close to daily (an off day
        # roughly once a week), so "played on all 3 of the last 3 days" is the
        # *typical* case, not an outlier — the old table {0:0.97,1:1.00,2:1.04,
        # 3:1.08} scored that common case as max fatigue, inflating bullpen_fip
        # on most games. This boolean-per-day count also can't see doubleheaders
        # or extra-inning marathons (the real overwork signals), so it can only
        # honestly support "extra rest = fresher," not "normal cadence = tired."
        # Rescaled so normal cadence (2-3 games) is ~neutral and the signal only
        # moves toward "fresh" when a team got unusual rest.
        fatigue_table = {0: 0.96, 1: 0.98, 2: 1.00, 3: 1.02}
        fatigue_mult = fatigue_table.get(games_last_3, 1.02)

        # Offensive rest multiplier
        if last_game_days is None:
            off_mult = 1.00
        elif last_game_days == 0:   # played yesterday
            off_mult = 0.99
        elif last_game_days == 1:   # normal 1 day rest
            off_mult = 1.00
        else:                        # 2+ days rest
            off_mult = 1.01

        return {
            "games_last_3":          games_last_3,
            "rest_days":             last_game_days,
            "bullpen_fatigue_mult":  fatigue_mult,
            "off_rest_mult":         off_mult,
            "source":                "espn_schedule",
        }
    except Exception:
        return neutral
