"""
scripts/enrich_nrfi_savant.py

Enriches data/nrfi_dataset.csv with Baseball Savant Statcast pitcher metrics.
Savant is publicly accessible (unlike FanGraphs which returns 403).

Columns added:
  home_xwoba_against   — expected wOBA allowed by home starter (lower = better)
  away_xwoba_against
  home_barrel_pct      — barrel rate allowed (hard contact; lower = better)
  away_barrel_pct
  home_hard_hit_pct    — hard-hit % allowed (exit velo >= 95 mph; lower = better)
  away_hard_hit_pct
  home_whiff_pct       — swing-and-miss rate (higher = better for pitcher)
  away_whiff_pct
  home_avg_velo        — average fastball velocity
  away_avg_velo

Run AFTER build_nrfi_dataset.py and enrich_nrfi_fi_rates.py:
  python scripts/enrich_nrfi_savant.py

Then retrain:
  python models/nrfi_model.py --train
"""
from __future__ import annotations
import difflib
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

try:
    import pybaseball as pyb
    pyb.cache.enable()
except ImportError:
    print("ERROR: pybaseball not installed. Run: pip install pybaseball")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas")
    sys.exit(1)

CSV_PATH = _REPO / "data" / "nrfi_dataset.csv"

# League-average fallbacks when a pitcher can't be matched
SAVANT_DEFAULTS = {
    "xwoba_against":  0.315,   # MLB average xwOBA against
    "barrel_pct":     0.085,   # MLB average barrel rate against
    "hard_hit_pct":   0.370,   # MLB average hard-hit % against
    "whiff_pct":      0.245,   # MLB average whiff %
    "avg_velo":       93.5,    # MLB average fastball velocity
}

_SAVANT_CACHE: dict[int, dict] = {}   # season → {normalized_name: stats_dict}


def _norm(name: str) -> str:
    return " ".join(str(name).lower().split())


def _load_savant_season(season: int) -> dict[str, dict]:
    if season in _SAVANT_CACHE:
        return _SAVANT_CACHE[season]

    print(f"  Loading Savant data for {season}…")
    lookup: dict[str, dict] = {}

    # ── Exit velo / barrels / xwOBA ──────────────────────────────────────────
    try:
        ev = pyb.statcast_pitcher_exitvelo_barrels(season)
        if ev is not None and not ev.empty:
            print(f"    Exit velo columns: {list(ev.columns)}")
            name_col = next((c for c in ("last_name, first_name", "player_name", "name")
                             if c in ev.columns), None)

            # Savant column names shift across pybaseball versions — try all known variants
            COL_MAP = [
                # (list of candidate src names, dst feature name)
                (["xwoba", "est_woba", "xwoba_against"],          "xwoba_against"),
                (["brl_percent", "barrel_batted_rate", "brl_pa"], "barrel_pct"),
                (["ev95percent", "hard_hit_percent",
                  "hard_hit_pct", "ev95plus"],                     "hard_hit_pct"),
            ]

            written = 0
            for _, row in ev.iterrows():
                if name_col:
                    raw_name = str(row.get(name_col, ""))
                    if "," in raw_name:
                        parts = raw_name.split(",", 1)
                        full  = f"{parts[1].strip()} {parts[0].strip()}"
                    else:
                        full = raw_name
                else:
                    first = str(row.get("first_name", "")).strip()
                    last  = str(row.get("last_name", "")).strip()
                    full  = f"{first} {last}"

                key = _norm(full)
                if not key:
                    continue
                if key not in lookup:
                    lookup[key] = {}

                row_wrote = False
                for candidates, dst in COL_MAP:
                    for src in candidates:
                        val = row.get(src)
                        if val is not None and not (isinstance(val, float) and pd.isna(val)):
                            try:
                                lookup[key][dst] = float(val)
                                row_wrote = True
                                break
                            except (TypeError, ValueError):
                                pass
                if row_wrote:
                    written += 1

            print(f"    Exit velo/barrels: {len(lookup)} pitchers found, {written} with metrics written")
    except Exception as e:
        print(f"    statcast_pitcher_exitvelo_barrels({season}) failed: {e}")

    # ── Whiff % and velocity ──────────────────────────────────────────────────
    try:
        pr = pyb.statcast_pitcher_percentile_ranks(season)
        if pr is not None and not pr.empty:
            name_col = next((c for c in ("player_name", "name") if c in pr.columns), None)
            for _, row in pr.iterrows():
                raw_name = str(row.get(name_col or "player_name", ""))
                if "," in raw_name:
                    parts = raw_name.split(",", 1)
                    full  = f"{parts[1].strip()} {parts[0].strip()}"
                else:
                    full = raw_name

                key = _norm(full)
                if not key:
                    continue
                if key not in lookup:
                    lookup[key] = {}

                for src, dst in [
                    ("whiff_percent", "whiff_pct"),
                    ("n_fastball",    None),         # ignore
                ]:
                    if dst is None:
                        continue
                    val = row.get(src)
                    if val is not None:
                        try:
                            lookup[key][dst] = float(val)
                        except (TypeError, ValueError):
                            pass
            print(f"    Percentile ranks: {len(pr)} pitchers")
    except Exception as e:
        print(f"    statcast_pitcher_percentile_ranks({season}) failed: {e}")

    # ── Pitch arsenal (avg velocity) ─────────────────────────────────────────
    try:
        pa = pyb.statcast_pitcher_pitch_arsenal(season, minP=25)
        if pa is not None and not pa.empty:
            print(f"    Pitch arsenal columns: {list(pa.columns)}")
            # Filter to fastballs only for average velocity
            fb_types = {"FF", "FT", "SI", "FA"}   # four-seam, two-seam, sinker, generic
            fb = pa[pa["pitch_type"].isin(fb_types)] if "pitch_type" in pa.columns else pa
            if fb.empty:
                fb = pa   # fallback: use all pitches

            name_col = next((c for c in ("last_name, first_name", "pitcher_name",
                                          "player_name", "name")
                             if c in pa.columns), None)
            # Savant arsenal returns per-pitch-type speed columns (ff_avg_speed, si_avg_speed…)
            # Use ff_avg_speed (four-seam) as primary; fall back to si/fa/ft
            VELO_CANDIDATES = ["ff_avg_speed", "si_avg_speed", "fa_avg_speed",
                               "ft_avg_speed", "avg_speed", "mph", "release_speed"]
            vel_col = next((c for c in VELO_CANDIDATES if c in pa.columns), None)
            print(f"    Pitch arsenal: name_col={name_col!r}, vel_col={vel_col!r}")

            if name_col and vel_col:
                # Work on the full pa dataframe (not fastball-filtered) since columns
                # are already per-pitch-type — just take the fastball speed column
                grouped = pa.groupby(name_col)[vel_col].mean()
                added = 0
                for raw_name, avg_vel in grouped.items():
                    if pd.isna(avg_vel):
                        continue
                    raw_name = str(raw_name)
                    if "," in raw_name:
                        parts = raw_name.split(",", 1)
                        full  = f"{parts[1].strip()} {parts[0].strip()}"
                    else:
                        full = raw_name
                    key = _norm(full)
                    if not key:
                        continue
                    if key not in lookup:
                        lookup[key] = {}
                    try:
                        lookup[key]["avg_velo"] = float(avg_vel)
                        added += 1
                    except (TypeError, ValueError):
                        pass
                print(f"    Pitch arsenal: avg_velo added for {added} pitchers")
            else:
                print(f"    Pitch arsenal: could not find name/vel columns — skipping")
        else:
            print(f"    Pitch arsenal: empty result for {season}")
    except Exception as e:
        print(f"    statcast_pitcher_pitch_arsenal({season}) failed: {e}")

    _SAVANT_CACHE[season] = lookup
    return lookup


def _lookup_pitcher(name: str, season: int) -> dict:
    if not name or not name.strip():
        return {}
    lookup = _load_savant_season(season)
    norm_names = list(lookup.keys())
    target = _norm(name)

    if target in lookup:
        return lookup[target]

    # Last-name exact
    last = target.split()[-1] if target else ""
    last_matches = [n for n in norm_names if n.split()[-1] == last]
    if len(last_matches) == 1:
        return lookup[last_matches[0]]

    # Fuzzy
    close = difflib.get_close_matches(target, norm_names, n=1, cutoff=0.82)
    if close:
        return lookup[close[0]]

    return {}


def enrich_savant(csv_path: Path = CSV_PATH) -> None:
    if not csv_path.exists():
        print(f"Dataset not found: {csv_path}")
        sys.exit(1)

    df = pd.read_csv(csv_path, encoding="latin-1")
    print(f"Loaded {len(df)} rows from {csv_path}")

    metrics = ["xwoba_against", "barrel_pct", "hard_hit_pct", "whiff_pct", "avg_velo"]

    # Initialise columns with NaN
    for side in ("home", "away"):
        for m in metrics:
            df[f"{side}_{m}"] = float("nan")

    seasons = sorted(df["season"].unique())

    for season in seasons:
        mask = df["season"] == season
        rows_for_season = df[mask].index
        print(f"\nSeason {season}: {mask.sum()} games")

        found_h = found_a = 0
        for idx in rows_for_season:
            h_name = str(df.at[idx, "home_starter"] or "").strip()
            a_name = str(df.at[idx, "away_starter"] or "").strip()

            h_stats = _lookup_pitcher(h_name, season)
            a_stats = _lookup_pitcher(a_name, season)

            for m in metrics:
                if m in h_stats:
                    df.at[idx, f"home_{m}"] = h_stats[m]
                if m in a_stats:
                    df.at[idx, f"away_{m}"] = a_stats[m]

            if h_stats:
                found_h += 1
            if a_stats:
                found_a += 1

        total = mask.sum()
        print(f"  home starters matched: {found_h}/{total} ({found_h/total*100:.1f}%)")
        print(f"  away starters matched: {found_a}/{total} ({found_a/total*100:.1f}%)")

    # Coverage summary
    print("\nCoverage after Savant enrichment:")
    for side in ("home", "away"):
        for m in metrics:
            col = f"{side}_{m}"
            n   = df[col].notna().sum()
            print(f"  {col:<30} {n:>5}/{len(df)}  ({n/len(df)*100:.1f}%)")

    df["game_date"] = pd.to_datetime(df["game_date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"\nSaved enriched dataset → {csv_path}")
    print("Next step: python models/nrfi_model.py --train")


if __name__ == "__main__":
    enrich_savant()
