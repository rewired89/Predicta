"""
Live data fetcher — runs in GitHub Actions to pre-fetch sports data.
GitHub Actions has full internet access; this script fetches from ESPN
and The Odds API and commits results to data/live/ so the remote
container can read them without hitting the APIs directly.

Usage:
  python fetchers/live_data.py [--date YYYY-MM-DD]

GitHub Actions sets ODDS_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY
as repository secrets. The script skips any fetch whose key is missing.
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

ESPN_MLB = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
ESPN_ATP = "https://site.api.espn.com/apis/site/v2/sports/tennis/atp"
ESPN_WTA = "https://site.api.espn.com/apis/site/v2/sports/tennis/wta"
ODDS_BASE = "https://api.the-odds-api.com/v4"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _get(url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = httpx.get(url, params=params or {}, headers=_HEADERS, timeout=TIMEOUT, follow_redirects=True)
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


# ── MLB ──────────────────────────────────────────────────────────────────────

def fetch_mlb(date_str: str) -> None:
    print(f"\nMLB ({date_str})")
    date_compact = date_str.replace("-", "")

    scoreboard = _get(f"{ESPN_MLB}/scoreboard", {"dates": date_compact})
    standings  = _get(f"{ESPN_MLB}/standings")
    teams      = _get(f"{ESPN_MLB}/teams")

    _save("mlb_scoreboard.json", scoreboard or {})
    _save("mlb_standings.json",  standings  or {})
    _save("mlb_teams.json",      teams      or {})

    # Collect team IDs and probable pitcher IDs from today's games
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

    # Fetch team stats (hitting + pitching) for each team playing today
    team_stats: dict[str, dict] = {}
    for tid in sorted(team_ids):
        stats = _get(f"{ESPN_MLB}/teams/{tid}/statistics")
        if stats:
            team_stats[tid] = stats
    _save("mlb_team_stats.json", team_stats)

    # Fetch detailed pitcher stats and profile for each probable starter
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

    print(f"  Teams fetched: {len(team_ids)}, Pitchers fetched: {len(pitcher_ids)}")


# ── Tennis ────────────────────────────────────────────────────────────────────

def fetch_tennis(date_str: str) -> None:
    print(f"\nTennis ({date_str})")
    date_compact = date_str.replace("-", "")

    atp = _get(f"{ESPN_ATP}/scoreboard", {"dates": date_compact})
    wta = _get(f"{ESPN_WTA}/scoreboard", {"dates": date_compact})

    # Also fetch recent days in case today's events haven't populated yet
    atp_recent = _get(f"{ESPN_ATP}/scoreboard") or {}
    wta_recent = _get(f"{ESPN_WTA}/scoreboard") or {}

    # Merge: prefer today's events, fall back to recent
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

    print("\nOdds")
    sport_keys = [
        ("baseball_mlb",         "odds_mlb.json"),
        ("tennis_atp_french_open","odds_tennis_atp.json"),
        ("tennis_wta_french_open","odds_tennis_wta.json"),
    ]
    for sport_key, fname in sport_keys:
        data = _get(f"{ODDS_BASE}/sports/{sport_key}/odds", {
            "apiKey":      api_key,
            "regions":     "us",
            "markets":     "h2h,spreads,totals",
            "oddsFormat":  "american",
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

    fetch_mlb(date_str)
    fetch_tennis(date_str)
    fetch_odds()

    print("\nDone.")


if __name__ == "__main__":
    main()
