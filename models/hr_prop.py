"""
Experimental, standalone home-run probability estimate for one hitter in one game.

Scope, deliberately kept small per the 2026-07-11 request: "a batting stat that
could be calculated plus how often they usually make home runs" — nothing more.
NOT wired into run_baseball_analysis, the Poisson/Elo run model, any market,
_log_prediction, or the daily NRFI automation — it's an isolated function a
caller invokes on its own. Adding it cannot change any existing prediction or
accuracy number because nothing else in the codebase calls it.

Model: Poisson process over the player's own established home-run rate.
    lambda = season_hr / games_played
    P(at least one HR today) = 1 - e^-lambda
No opposing-pitcher, park, or weather adjustment — those exist elsewhere in the
codebase (park factors in this module's PARK_FACTORS-equivalent, wind factor in
fetchers/weather.py) but wiring them in is a separate, bigger step not taken
here. This has not been backtested and carries no data_confidence gate like the
moneyline/NRFI/F5/O-U markets — treat it as a rough, informational number only.
"""
import math
from typing import Optional

from models.player_impact import KNOWN_HITTERS
from fetchers.baseball import lookup_batter


FULL_SEASON_GAMES = 162  # denominator for the KNOWN_HITTERS fallback — see note below


def estimate_hr_probability(player_name: str, team_abbr: Optional[str] = None) -> dict:
    """
    Rough per-game home-run probability for a named hitter.

    Prefers a live ESPN lookup (lookup_batter) because its hr and games_played
    come from the SAME current-season snapshot — numerator and denominator are
    consistent. KNOWN_HITTERS is only a fallback for when the live lookup fails
    (no team_abbr, ESPN unreachable, player not found): its "hr" field is a
    static, established-quality reference blend (see the "2024-25 blend"
    comment near TEAM_WRC_PLUS in player_impact.py), not a live in-season
    total, so it must NOT be divided by the live team games_played — that
    would mix a full-season-level number with a partial-season denominator
    and overstate the rate. It's divided by an assumed full 162-game season
    instead, and the result is labeled as an established-level estimate rather
    than this year's actual pace.
    """
    hr: Optional[int] = None
    games_played: Optional[int] = None
    team_resolved: Optional[str] = None
    games_played_source = None

    live = lookup_batter(player_name, team_abbr) if team_abbr else {}
    if live and live.get("hr") and live.get("games_played"):
        hr = live["hr"]
        games_played = live["games_played"]
        games_played_source = "player_live"
        team_resolved = live.get("team") or (team_abbr.upper() if team_abbr else None)
    else:
        known = KNOWN_HITTERS.get(player_name)
        if known and known.get("hr") is not None:
            hr = known["hr"]
            games_played = FULL_SEASON_GAMES
            games_played_source = "known_hitters_full_season_assumed"
            team_resolved = known.get("team") or (team_abbr.upper() if team_abbr else None)

    if not hr or not games_played:
        return {"error": f"Could not find {player_name}"
                         + (f" on team {team_abbr}" if team_abbr else " — team_abbr is required to look up players not in the known-hitters list")}

    lam = hr / games_played
    prob = 1 - math.exp(-lam)

    return {
        "player": player_name,
        "team": team_resolved,
        "season_hr": hr,
        "games_played": games_played,
        "games_played_source": games_played_source,
        "hr_rate_per_game": round(lam, 4),
        "prob_hr_today_pct": round(prob * 100, 1),
        "method": "poisson_season_rate",
        "caveats": [
            "Player-intrinsic rate only — no opposing pitcher, park, or weather adjustment applied.",
            "Not backtested and has no data_confidence gate, unlike the moneyline/NRFI/F5/O-U markets.",
        ] + ([
            "Based on an established-quality reference stat line (not this year's live pace) divided by an assumed 162-game season — treat as a career-level estimate, not today's actual odds.",
        ] if games_played_source == "known_hitters_full_season_assumed" else []),
    }
