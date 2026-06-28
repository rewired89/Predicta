"""
fetchers/nrfi_lineup.py

Fetches today's confirmed batting lineup (top-3 spots) from MLB Stats API
and converts season OPS to approximate wRC+ for use in the NRFI model.

Called by analyze_baseball.py before predict_nrfi() so the model receives
actual lineup quality instead of defaulting to league average (100).

OPS → wRC+ approximation: wrc ≈ (ops / LEAGUE_OPS) × 100
Accurate to ~5-10 points; sufficient for a feature that defaults to 100.
"""
from __future__ import annotations

import time
from datetime import date as _date_cls

import requests

MLBAPI       = "https://statsapi.mlb.com/api/v1"
LEAGUE_OPS   = 0.710   # 2022-2024 MLB average; wRC+ = 100 at this level
TIMEOUT      = 8       # seconds per request
DELAY        = 0.25    # polite delay between stat lookups

# ESPN abbreviation → MLB Stats API abbreviation (only the ones that differ)
ESPN_TO_MLB: dict[str, str] = {
    "CHW": "CWS",
}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{MLBAPI}/{path}"
    r = requests.get(url, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _player_ops(player_id: int, season: int) -> float | None:
    """Return season OPS for a player, or None on failure / missing data."""
    try:
        time.sleep(DELAY)
        data = _get(
            f"people/{player_id}/stats",
            params={"stats": "season", "season": season, "group": "hitting"},
        )
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return None
        s = splits[0].get("stat", {})
        obp = s.get("obp")
        slg = s.get("slg")
        if obp is None or slg is None:
            return None
        return round(float(obp) + float(slg), 3)
    except Exception:
        return None


def _ops_to_wrc(ops: float | None) -> float | None:
    if ops is None:
        return None
    return round((ops / LEAGUE_OPS) * 100, 1)


def get_nrfi_lineup_wrc(
    home_team: str,
    away_team: str,
    game_date: str | None = None,
) -> tuple[float | None, float | None]:
    """
    Return (home_top3_wrc, away_top3_wrc) for today's confirmed batting order.

    Args:
        home_team:  ESPN/Predicta abbreviation (e.g. "HOU", "CHW")
        away_team:  ESPN/Predicta abbreviation
        game_date:  "YYYY-MM-DD"; defaults to today

    Returns:
        Tuple of approximate wRC+ for home and away top-3 lineup spots,
        or (None, None) if the lineup isn't posted yet or any API call fails.
    """
    if game_date is None:
        game_date = _date_cls.today().isoformat()

    season = int(game_date[:4])
    h_mlb = ESPN_TO_MLB.get(home_team, home_team)
    a_mlb = ESPN_TO_MLB.get(away_team, away_team)

    try:
        sched = _get("schedule", params={"sportId": 1, "date": game_date, "hydrate": "team"})
        dates = sched.get("dates", [])
        if not dates:
            return None, None

        game_pk: int | None = None
        for game in dates[0].get("games", []):
            ht = game.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "")
            at = game.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "")
            if ht == h_mlb and at == a_mlb:
                game_pk = game["gamePk"]
                break

        if game_pk is None:
            return None, None

        boxscore = _get(f"game/{game_pk}/boxscore")
        teams = boxscore.get("teams", {})

        def _side_wrc(side: str) -> float | None:
            players = teams.get(side, {}).get("players", {})
            slots: list[tuple[int, int]] = []
            for pdata in players.values():
                bo = pdata.get("battingOrder")
                pid = pdata.get("person", {}).get("id")
                if bo is None or pid is None:
                    continue
                try:
                    bo_int = int(bo)
                except (TypeError, ValueError):
                    continue
                if bo_int in (100, 200, 300):
                    slots.append((bo_int, pid))

            slots.sort()
            top3_ids = [pid for _, pid in slots[:3]]
            if not top3_ids:
                return None

            wrc_vals = []
            for pid in top3_ids:
                ops = _player_ops(pid, season)
                wrc = _ops_to_wrc(ops)
                if wrc is not None:
                    wrc_vals.append(wrc)

            return round(sum(wrc_vals) / len(wrc_vals), 1) if wrc_vals else None

        home_wrc = _side_wrc("home")
        away_wrc = _side_wrc("away")
        return home_wrc, away_wrc

    except Exception:
        return None, None
