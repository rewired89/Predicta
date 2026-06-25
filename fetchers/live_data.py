"""
Live data fetcher — runs in GitHub Actions to pre-fetch sports data.
GitHub Actions has full internet access; this script fetches from SportsData.io
(primary) and ESPN (fallback/tennis) then commits results to data/live/ so the
remote container can read them without hitting the APIs directly.

Usage:
  python fetchers/live_data.py [--date YYYY-MM-DD]

GitHub secrets used (all optional — section is skipped if key missing):
  SPORTSDATA_API_KEY  — SportsData.io (MLB scores, standings, player stats)
  ODDS_API_KEY        — The Odds API (moneyline, spreads, totals)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

LIVE_DIR = Path(__file__).parent.parent / "data" / "live"
TIMEOUT  = 20.0

# SportsData.io MLB v3
SD_MLB_BASE   = "https://api.sportsdata.io/v3/mlb"
SD_MLB_SCORES = f"{SD_MLB_BASE}/scores/json"
SD_MLB_STATS  = f"{SD_MLB_BASE}/stats/json"

# ESPN (free, no key — used for tennis and MLB fallback)
ESPN_MLB = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
ESPN_ATP = "https://site.api.espn.com/apis/site/v2/sports/tennis/atp"
ESPN_WTA = "https://site.api.espn.com/apis/site/v2/sports/tennis/wta"

# The Odds API
ODDS_BASE = "https://api.the-odds-api.com/v4"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _get(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list | None:
    try:
        r = httpx.get(
            url,
            params=params or {},
            headers={**_BROWSER_HEADERS, **(headers or {})},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  WARN {url} → {e}", file=sys.stderr)
        return None


def _save(name: str, data: dict | list) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = LIVE_DIR / name
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    path.write_text(json.dumps(payload, indent=2))
    size = len(json.dumps(data))
    print(f"  ✓ {name} ({size:,} bytes)")


# ── SportsData.io MLB ─────────────────────────────────────────────────────────

def _sd_headers(api_key: str) -> dict:
    return {"Ocp-Apim-Subscription-Key": api_key}


def fetch_mlb_sportsdata(date_str: str, api_key: str) -> bool:
    """
    Fetch MLB data from SportsData.io. Returns True if successful.
    Saves: mlb_scoreboard.json, mlb_standings.json, mlb_player_stats.json
    """
    print(f"\nMLB via SportsData.io ({date_str})")
    h = _sd_headers(api_key)

    # Today's games with box scores and probable starters
    # SportsData.io date format: YYYY-MMM-DD e.g. 2026-JUN-25
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    sd_date = dt.strftime("%Y-%b-%d").upper()  # e.g. 2026-JUN-25

    games = _get(f"{SD_MLB_SCORES}/GamesByDate/{sd_date}", headers=h)
    standings = _get(f"{SD_MLB_SCORES}/Standings/{dt.year}", headers=h)
    teams = _get(f"{SD_MLB_SCORES}/Teams", headers=h)

    if not games and not standings:
        print("  SportsData.io returned no data — check API key", file=sys.stderr)
        return False

    _save("mlb_scoreboard_sd.json", games or [])
    _save("mlb_standings_sd.json",  standings or [])
    _save("mlb_teams_sd.json",       teams or [])

    # Pitcher season stats for all starters in today's games
    pitcher_ids: set[int] = set()
    for game in (games or []):
        for field in ("StartingPitcherID", "AwayStartingPitcherID", "HomeStartingPitcherID"):
            pid = game.get(field)
            if pid:
                pitcher_ids.add(int(pid))

    pitcher_stats: dict[str, dict] = {}
    if pitcher_ids:
        all_stats = _get(f"{SD_MLB_STATS}/PlayerSeasonStats/{dt.year}", headers=h) or []
        for p in all_stats:
            pid = p.get("PlayerID")
            if pid and int(pid) in pitcher_ids:
                pitcher_stats[str(pid)] = p
    _save("mlb_pitcher_stats_sd.json", pitcher_stats)

    print(f"  Games: {len(games or [])}, Pitchers: {len(pitcher_stats)}")
    return True


# ── ESPN MLB fallback ─────────────────────────────────────────────────────────

def fetch_mlb_espn(date_str: str) -> None:
    """Fetch MLB data from ESPN (free, no key). Saves to mlb_*_espn.json files."""
    print(f"\nMLB via ESPN fallback ({date_str})")
    date_compact = date_str.replace("-", "")

    scoreboard = _get(f"{ESPN_MLB}/scoreboard", {"dates": date_compact})
    standings  = _get(f"{ESPN_MLB}/standings")
    teams      = _get(f"{ESPN_MLB}/teams")

    _save("mlb_scoreboard.json", scoreboard or {})
    _save("mlb_standings.json",  standings  or {})
    _save("mlb_teams.json",      teams      or {})

    team_ids: set[str] = set()
    pitcher_ids: set[str] = set()
    if scoreboard:
        for event in scoreboard.get("events", []):
            for comp in event.get("competitions", []):
                for competitor in comp.get("competitors", []):
                    tid = str(competitor.get("team", {}).get("id", ""))
                    if tid:
                        team_ids.add(tid)
                    for probable in competitor.get("probables", []):
                        aid = str(
                            probable.get("athlete", {}).get("id")
                            or probable.get("athleteId", "")
                            or ""
                        )
                        if aid:
                            pitcher_ids.add(aid)

    team_stats: dict[str, dict] = {}
    for tid in sorted(team_ids):
        stats = _get(f"{ESPN_MLB}/teams/{tid}/statistics")
        if stats:
            team_stats[tid] = stats
    _save("mlb_team_stats.json", team_stats)

    pitcher_stats: dict[str, dict] = {}
    for aid in sorted(pitcher_ids):
        entry: dict = {}
        stats_data = _get(f"{ESPN_MLB}/athletes/{aid}/statistics")
        if stats_data:
            entry["statistics"] = stats_data
        profile = _get(f"{ESPN_MLB}/athletes/{aid}")
        if profile:
            entry["profile"] = profile
        if entry:
            pitcher_stats[aid] = entry
    _save("mlb_pitcher_stats.json", pitcher_stats)

    print(f"  Teams: {len(team_ids)}, Pitchers: {len(pitcher_ids)}")


# ── Tennis ────────────────────────────────────────────────────────────────────

def fetch_tennis(date_str: str) -> None:
    print(f"\nTennis via ESPN ({date_str})")
    date_compact = date_str.replace("-", "")

    atp = _get(f"{ESPN_ATP}/scoreboard", {"dates": date_compact})
    wta = _get(f"{ESPN_WTA}/scoreboard", {"dates": date_compact})
    atp_recent = _get(f"{ESPN_ATP}/scoreboard") or {}
    wta_recent = _get(f"{ESPN_WTA}/scoreboard") or {}

    if not (atp or {}).get("events"):
        atp = atp_recent
    if not (wta or {}).get("events"):
        wta = wta_recent

    _save("tennis_atp.json", atp or {})
    _save("tennis_wta.json", wta or {})

    atp_ev = len((atp or {}).get("events", []))
    wta_ev = len((wta or {}).get("events", []))
    print(f"  ATP events: {atp_ev}, WTA events: {wta_ev}")


# ── Odds ─────────────────────────────────────────────────────────────────────

def fetch_odds() -> None:
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print("\nOdds: skipped (ODDS_API_KEY not set)")
        return

    print("\nOdds via The Odds API")
    sport_keys = [
        ("baseball_mlb",          "odds_mlb.json"),
        ("tennis_atp_french_open", "odds_tennis_atp.json"),
        ("tennis_wta_french_open", "odds_tennis_wta.json"),
    ]
    for sport_key, fname in sport_keys:
        data = _get(f"{ODDS_BASE}/sports/{sport_key}/odds", {
            "apiKey":     api_key,
            "regions":    "us",
            "markets":    "h2h,spreads,totals",
            "oddsFormat": "american",
        })
        if data:
            _save(fname, data)
        else:
            print(f"  – {sport_key}: no data")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch live sports data for Predicta")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Predicta live data fetch — {datetime.now(timezone.utc).isoformat()}")
    print(f"Target date: {date_str}")

    # MLB: fetch from SportsData.io (richer stats) + always ESPN (ESPN cache files
    # are what the app's _cache_fallback() reads, so they must always exist)
    sd_key = os.environ.get("SPORTSDATA_API_KEY", "").strip()
    if sd_key:
        fetch_mlb_sportsdata(date_str, sd_key)
    else:
        print("\nSportsData.io: skipped (SPORTSDATA_API_KEY not set)")
    fetch_mlb_espn(date_str)

    fetch_tennis(date_str)
    fetch_odds()

    print("\nDone.")


if __name__ == "__main__":
    main()
