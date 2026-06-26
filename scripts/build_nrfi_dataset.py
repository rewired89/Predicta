"""
scripts/build_nrfi_dataset.py

Builds a historical NRFI training dataset from:
  - MLB Stats API  : game schedule, per-game linescore (NRFI outcome), boxscore (starters)
  - FanGraphs      : starter SIERA, xFIP, CSW%, O-Swing%, K%, BB%, GB% (via pybaseball)
  - Park factors   : from fetchers/baseball.py PARK_FACTORS dict

RUN THIS LOCALLY (requires full internet access — proxy blocks external calls in container):
  cd /path/to/Predicta
  python scripts/build_nrfi_dataset.py

  # specific seasons:
  python scripts/build_nrfi_dataset.py --seasons 2023 2024

  # custom output path:
  python scripts/build_nrfi_dataset.py --output data/nrfi_custom.csv

Output: data/nrfi_dataset.csv
  ~7,300 rows (2,430 games/season × 3 seasons), one row per game.
  Runtime: ~45-60 min (rate-limited at 1 req/sec for MLB Stats API).
  Checkpoint: data/nrfi_checkpoint_{year}.csv written per-season so
              you can resume if interrupted.

After running, train the model:
  python models/nrfi_model.py --train
"""
from __future__ import annotations
import argparse
import csv
import difflib
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── allow running from repo root OR scripts/ dir ──────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import requests

try:
    import pybaseball as pyb
    pyb.cache.enable()
    _HAS_PYB = True
except ImportError:
    print("ERROR: pybaseball not installed. Run: pip install pybaseball")
    sys.exit(1)

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

MLB_API       = "https://statsapi.mlb.com/api/v1"
REQUEST_DELAY = 1.0        # seconds between MLB Stats API calls
DEFAULT_SEASONS = [2022, 2023, 2024]
OUTPUT_PATH   = _REPO / "data" / "nrfi_dataset.csv"
MIN_IP        = 5          # minimum innings pitched to include a pitcher in FG cache

# MLB Stats API team abbreviations → ESPN/park-factor abbreviations
MLB_TO_ESPN: dict[str, str] = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
    "CHC": "CHC", "CIN": "CIN", "CLE": "CLE", "COL": "COL",
    "CWS": "CHW",  # MLB Stats uses CWS; park factor dict uses CHW
    "DET": "DET", "HOU": "HOU", "KC": "KC",   "LAA": "LAA",
    "LAD": "LAD", "MIA": "MIA", "MIL": "MIL", "MIN": "MIN",
    "NYM": "NYM", "NYY": "NYY", "OAK": "OAK", "PHI": "PHI",
    "PIT": "PIT", "SD": "SD",   "SEA": "SEA", "SF": "SF",
    "STL": "STL", "TB": "TB",   "TEX": "TEX", "TOR": "TOR",
    "WSH": "WSH",
}

# Fixed / retractable-roof stadiums (NRFI unaffected by wind/rain)
DOME_TEAMS = {"TB", "MIA", "MIL", "ARI", "HOU", "SEA", "TOR", "TEX"}

# Park run factors (3-year average — matches fetchers/baseball.py)
PARK_FACTORS: dict[str, float] = {
    "COL": 1.19, "CIN": 1.08, "BOS": 1.06, "TEX": 1.05,
    "PHI": 1.04, "CHW": 1.03, "ATL": 1.02, "BAL": 1.01,
    "HOU": 1.01, "LAA": 1.00, "MIA": 1.00, "MIL": 1.00,
    "DET": 0.99, "PIT": 0.99, "MIN": 0.99, "KC":  0.98,
    "NYY": 0.98, "TOR": 0.98, "NYM": 0.97, "STL": 0.97,
    "CLE": 0.97, "WSH": 0.96, "TB":  0.96, "OAK": 0.96,
    "CHC": 0.96, "ARI": 0.95, "LAD": 0.95, "SD":  0.94,
    "SEA": 0.93, "SF":  0.92,
}

CSV_COLUMNS = [
    # metadata (not model features)
    "game_pk", "game_date", "season",
    "home_team", "away_team",
    "home_starter", "away_starter",
    "home_1st_runs", "away_1st_runs",
    # target
    "nrfi",
    # home starter features
    "home_siera", "home_xfip", "home_fip",
    "home_csw_pct", "home_o_swing_pct", "home_k_pct",
    "home_bb_pct", "home_gb_pct", "home_hr_fb_pct",
    # away starter features
    "away_siera", "away_xfip", "away_fip",
    "away_csw_pct", "away_o_swing_pct", "away_k_pct",
    "away_bb_pct", "away_gb_pct", "away_hr_fb_pct",
    # game context
    "park_factor", "is_dome",
    # enrichment flags
    "home_fg_found", "away_fg_found",
]


# ── MLB Stats API helpers ─────────────────────────────────────────────────────

_session = requests.Session()
_session.headers["User-Agent"] = "Predicta-NRFI-Dataset-Builder/1.0"


def _mlb_get(path: str, params: dict | None = None) -> dict:
    url = f"{MLB_API}/{path}"
    resp = _session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_season_game_pks(season: int) -> list[dict]:
    """Return list of {gamePk, gameDate, status} for regular-season games."""
    print(f"  Fetching {season} schedule…")
    data = _mlb_get("schedule", {
        "sportId": 1,
        "season": season,
        "gameType": "R",          # regular season only
        "fields": "dates,date,games,gamePk,status,abstractGameState",
    })
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            state = g.get("status", {}).get("abstractGameState", "")
            if state == "Final":   # only completed games have linescores
                games.append({"gamePk": g["gamePk"], "gameDate": date_entry["date"]})
    print(f"  Found {len(games)} completed games in {season}")
    return games


def get_linescore(game_pk: int) -> Optional[dict]:
    """Return first-inning runs for home and away, or None if data missing."""
    try:
        time.sleep(REQUEST_DELAY)
        data = _mlb_get(f"game/{game_pk}/linescore")
        innings = data.get("innings", [])
        if not innings:
            return None
        first = innings[0]
        return {
            "home_1st": first.get("home", {}).get("runs", None),
            "away_1st": first.get("away", {}).get("runs", None),
        }
    except Exception as e:
        print(f"    linescore {game_pk}: {e}")
        return None


def get_starters(game_pk: int) -> Optional[dict]:
    """Return {home_team, away_team, home_starter_name, away_starter_name}."""
    try:
        time.sleep(REQUEST_DELAY)
        data = _mlb_get(f"game/{game_pk}/boxscore")
        teams = data.get("teams", {})

        def _starter(side: str) -> tuple[str, str]:
            t        = teams.get(side, {})
            abbr_raw = t.get("team", {}).get("abbreviation", "")
            abbr     = MLB_TO_ESPN.get(abbr_raw, abbr_raw)
            pitchers = t.get("pitchers", [])
            if not pitchers:
                return abbr, ""
            starter_id = pitchers[0]
            players    = t.get("players", {})
            name = players.get(f"ID{starter_id}", {}).get("person", {}).get("fullName", "")
            return abbr, name

        home_abbr, home_name = _starter("home")
        away_abbr, away_name = _starter("away")
        return {
            "home_team":    home_abbr,
            "away_team":    away_abbr,
            "home_starter": home_name,
            "away_starter": away_name,
        }
    except Exception as e:
        print(f"    boxscore {game_pk}: {e}")
        return None


# ── FanGraphs pitcher lookup ──────────────────────────────────────────────────

_FG_CACHE: dict[int, "pd.DataFrame"] = {}   # season → DataFrame


def _load_fg_season(season: int) -> Optional["pd.DataFrame"]:
    if season in _FG_CACHE:
        return _FG_CACHE[season]
    print(f"  Loading FanGraphs pitcher stats for {season}…")
    try:
        df = pyb.pitching_stats(season, qual=MIN_IP)
        if df is not None and not df.empty:
            # Normalise name for matching
            df["_norm"] = df["Name"].str.lower().str.split().str.join(" ")
            _FG_CACHE[season] = df
            print(f"  FanGraphs {season}: {len(df)} pitchers loaded")
            return df
    except Exception as e:
        print(f"  FanGraphs {season} failed: {e}")
    return None


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def _safe(val, default: float = float("nan")) -> float:
    try:
        f = float(val)
        return f if f == f else default   # NaN check
    except (TypeError, ValueError):
        return default


def lookup_pitcher_fg(name: str, season: int) -> dict:
    """Return FanGraphs stats dict for a pitcher in a given season."""
    if not name:
        return {"found": False}
    df = _load_fg_season(season)
    if df is None:
        return {"found": False}

    norm_target  = _norm(name)
    norm_names   = df["_norm"].tolist()
    fg_names     = df["Name"].tolist()

    # 1. Exact normalised match
    if norm_target in norm_names:
        idx = norm_names.index(norm_target)
    else:
        # 2. Last-name exact
        last = norm_target.split()[-1]
        last_matches = [i for i, n in enumerate(norm_names) if n.split()[-1] == last]
        if len(last_matches) == 1:
            idx = last_matches[0]
        else:
            # 3. Fuzzy
            close = difflib.get_close_matches(norm_target, norm_names, n=1, cutoff=0.82)
            if not close:
                return {"found": False, "searched": name}
            idx = norm_names.index(close[0])

    row = df.iloc[idx]
    return {
        "found":        True,
        "name_matched": fg_names[idx],
        "siera":        _safe(row.get("SIERA")),
        "xfip":         _safe(row.get("xFIP")),
        "fip":          _safe(row.get("FIP")),
        "csw_pct":      _safe(row.get("CSW%")),
        "o_swing_pct":  _safe(row.get("O-Swing%")),
        "k_pct":        _safe(row.get("K%")),
        "bb_pct":       _safe(row.get("BB%")),
        "gb_pct":       _safe(row.get("GB%")),
        "hr_fb_pct":    _safe(row.get("HR/FB")),
    }


# ── Per-season scrape ─────────────────────────────────────────────────────────

def build_season(season: int, checkpoint_path: Path) -> list[dict]:
    """Scrape one season and return rows. Resumes from checkpoint if present."""
    already_done: set[int] = set()
    rows: list[dict] = []

    if checkpoint_path.exists():
        with checkpoint_path.open() as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
                already_done.add(int(r["game_pk"]))
        print(f"  Resumed: {len(already_done)} games already in checkpoint")

    games = get_season_game_pks(season)
    todo  = [g for g in games if g["gamePk"] not in already_done]
    print(f"  Games to process: {len(todo)}")

    for i, game in enumerate(todo, 1):
        pk   = game["gamePk"]
        date = game["gameDate"]

        ls = get_linescore(pk)
        if ls is None or ls["home_1st"] is None or ls["away_1st"] is None:
            continue   # incomplete game data

        starters = get_starters(pk)
        if starters is None:
            continue

        home_team = starters["home_team"]
        away_team = starters["away_team"]
        h_name    = starters["home_starter"]
        a_name    = starters["away_starter"]

        h_fg = lookup_pitcher_fg(h_name, season)
        a_fg = lookup_pitcher_fg(a_name, season)

        park_factor = PARK_FACTORS.get(home_team, 1.0)
        is_dome     = 1 if home_team in DOME_TEAMS else 0
        h1 = ls["home_1st"]
        a1 = ls["away_1st"]

        row = {
            "game_pk":        pk,
            "game_date":      date,
            "season":         season,
            "home_team":      home_team,
            "away_team":      away_team,
            "home_starter":   h_name,
            "away_starter":   a_name,
            "home_1st_runs":  h1,
            "away_1st_runs":  a1,
            "nrfi":           1 if (h1 == 0 and a1 == 0) else 0,
            "home_siera":     h_fg.get("siera", float("nan")),
            "home_xfip":      h_fg.get("xfip",  float("nan")),
            "home_fip":       h_fg.get("fip",   float("nan")),
            "home_csw_pct":   h_fg.get("csw_pct", float("nan")),
            "home_o_swing_pct": h_fg.get("o_swing_pct", float("nan")),
            "home_k_pct":     h_fg.get("k_pct", float("nan")),
            "home_bb_pct":    h_fg.get("bb_pct", float("nan")),
            "home_gb_pct":    h_fg.get("gb_pct", float("nan")),
            "home_hr_fb_pct": h_fg.get("hr_fb_pct", float("nan")),
            "away_siera":     a_fg.get("siera", float("nan")),
            "away_xfip":      a_fg.get("xfip",  float("nan")),
            "away_fip":       a_fg.get("fip",   float("nan")),
            "away_csw_pct":   a_fg.get("csw_pct", float("nan")),
            "away_o_swing_pct": a_fg.get("o_swing_pct", float("nan")),
            "away_k_pct":     a_fg.get("k_pct", float("nan")),
            "away_bb_pct":    a_fg.get("bb_pct", float("nan")),
            "away_gb_pct":    a_fg.get("gb_pct", float("nan")),
            "away_hr_fb_pct": a_fg.get("hr_fb_pct", float("nan")),
            "park_factor":    park_factor,
            "is_dome":        is_dome,
            "home_fg_found":  int(h_fg.get("found", False)),
            "away_fg_found":  int(a_fg.get("found", False)),
        }
        rows.append(row)

        # Write checkpoint every 50 games
        if i % 50 == 0 or i == len(todo):
            with checkpoint_path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            pct = i / len(todo) * 100
            nrfi_count = sum(1 for r in rows if int(r.get("nrfi", 0)) == 1)
            nrfi_pct   = nrfi_count / len(rows) * 100 if rows else 0
            print(f"  [{i:>4}/{len(todo)}  {pct:.0f}%]  "
                  f"game {pk} ({date})  "
                  f"NRFI so far: {nrfi_pct:.1f}%  "
                  f"h_pitcher: {h_name[:20]:<20}  a_pitcher: {a_name[:20]}")

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global REQUEST_DELAY

    parser = argparse.ArgumentParser(description="Build historical NRFI training dataset")
    parser.add_argument("--seasons", nargs="+", type=int, default=DEFAULT_SEASONS,
                        help="MLB seasons to scrape (default: 2022 2023 2024)")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH,
                        help="Output CSV path (default: data/nrfi_dataset.csv)")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY,
                        help="Seconds between MLB API calls (default: 1.0)")
    args = parser.parse_args()

    REQUEST_DELAY = args.delay

    args.output.parent.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []

    for season in args.seasons:
        checkpoint = args.output.parent / f"nrfi_checkpoint_{season}.csv"
        print(f"\n{'='*60}")
        print(f"Season {season}")
        print(f"{'='*60}")
        rows = build_season(season, checkpoint)
        all_rows.extend(rows)
        nrfi_n = sum(1 for r in rows if int(r.get("nrfi", 0)) == 1)
        print(f"  Season {season}: {len(rows)} games, "
              f"NRFI {nrfi_n}/{len(rows)} ({nrfi_n/len(rows)*100:.1f}%)")

    # Write final merged CSV
    with args.output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    total  = len(all_rows)
    nrfi_n = sum(1 for r in all_rows if int(r.get("nrfi", 0)) == 1)
    fg_both = sum(1 for r in all_rows
                  if int(r.get("home_fg_found", 0)) and int(r.get("away_fg_found", 0)))

    print(f"\n{'='*60}")
    print(f"DONE")
    print(f"  Total games:          {total}")
    print(f"  NRFI outcomes:        {nrfi_n} ({nrfi_n/total*100:.1f}%)")
    print(f"  YRFI outcomes:        {total-nrfi_n} ({(total-nrfi_n)/total*100:.1f}%)")
    print(f"  Both starters found:  {fg_both} ({fg_both/total*100:.1f}%)")
    print(f"  Output:               {args.output}")
    print(f"\nNext step: python models/nrfi_model.py --train")


if __name__ == "__main__":
    main()
