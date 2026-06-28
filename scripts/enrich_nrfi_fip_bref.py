"""
scripts/enrich_nrfi_fip_bref.py

Backfills home_fip / away_fip in data/nrfi_dataset.csv using Baseball Reference
pitcher data (via pybaseball).  Run this when the existing CSV has home_fip = 0/7412
(i.e., the column exists but is all NaN) without needing a full dataset rebuild.

FIP formula: (13*HR + 3*(BB+HBP) - 2*SO) / IP + 3.15
Used when BRef returns the raw components instead of a pre-computed FIP column.

Run AFTER enrich_nrfi_fi_rates.py and enrich_nrfi_savant.py:
  python scripts/enrich_nrfi_fip_bref.py

Then retrain:
  python models/nrfi_model.py --train
"""
from __future__ import annotations
import difflib
import sys
from pathlib import Path

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

CSV_PATH     = _REPO / "data" / "nrfi_dataset.csv"
FIP_CONSTANT = 3.15    # stable year-to-year; minor variation (~0.05) doesn't matter here
FIP_DEFAULT  = 4.00    # league-average fallback

_BREF_CACHE: dict[int, pd.DataFrame | None] = {}


def _norm(name: str) -> str:
    return " ".join(str(name).lower().split())


def _load_bref_season(season: int) -> pd.DataFrame | None:
    if season in _BREF_CACHE:
        return _BREF_CACHE[season]

    print(f"  Loading BRef pitching data for {season}…")
    try:
        df = pyb.pitching_stats_bref(season)
        if df is None or df.empty:
            print(f"    BRef returned empty for {season}")
            _BREF_CACHE[season] = None
            return None
    except Exception as e:
        print(f"    BRef fetch failed for {season}: {e}")
        _BREF_CACHE[season] = None
        return None

    # Compute K% and BB% from BF (batters faced) if not already present
    if "BF" in df.columns:
        bf = pd.to_numeric(df["BF"], errors="coerce").replace(0, float("nan"))
        if "K%" not in df.columns and "SO" in df.columns:
            df["K%"] = pd.to_numeric(df["SO"], errors="coerce") / bf
        if "BB%" not in df.columns and "BB" in df.columns:
            df["BB%"] = pd.to_numeric(df["BB"], errors="coerce") / bf

    # Compute FIP from components if not directly available
    if "FIP" not in df.columns:
        try:
            ip  = pd.to_numeric(df.get("IP",  pd.Series()), errors="coerce").replace(0, float("nan"))
            hr  = pd.to_numeric(df.get("HR",  pd.Series(dtype=float)), errors="coerce").fillna(0)
            bb  = pd.to_numeric(df.get("BB",  pd.Series(dtype=float)), errors="coerce").fillna(0)
            so  = pd.to_numeric(df.get("SO",  pd.Series(dtype=float)), errors="coerce").fillna(0)
            hbp = pd.to_numeric(df.get("HBP", pd.Series(dtype=float)), errors="coerce").fillna(0)
            df["FIP"] = (13 * hr + 3 * (bb + hbp) - 2 * so) / ip + FIP_CONSTANT
            print(f"    FIP computed from components: HR/BB/SO/IP")
        except Exception as fip_err:
            print(f"    FIP computation failed: {fip_err}")

    # Normalise pitcher name for matching
    name_col = next((c for c in ("Name", "player_name", "name") if c in df.columns), None)
    if name_col is None:
        print(f"    Could not find name column in BRef data — columns: {list(df.columns)}")
        _BREF_CACHE[season] = None
        return None

    df["_norm"] = df[name_col].apply(_norm)
    available = [c for c in ["K%", "BB%", "FIP"] if c in df.columns]
    print(f"    BRef {season}: {len(df)} pitchers | available: {available}")
    _BREF_CACHE[season] = df
    return df


def _lookup_pitcher_fip(name: str, season: int) -> dict:
    if not name or not name.strip():
        return {}
    df = _load_bref_season(season)
    if df is None:
        return {}

    norm_names = df["_norm"].tolist()
    target = _norm(name)

    def _extract(idx: int) -> dict:
        row = df.iloc[idx]
        result = {}
        for src, dst in [("FIP", "fip"), ("K%", "k_pct"), ("BB%", "bb_pct")]:
            val = row.get(src)
            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                try:
                    result[dst] = float(val)
                except (TypeError, ValueError):
                    pass
        return result

    # Exact match
    if target in norm_names:
        return _extract(norm_names.index(target))

    # Last-name exact
    last = target.split()[-1] if target else ""
    last_matches = [i for i, n in enumerate(norm_names) if n.split()[-1] == last]
    if len(last_matches) == 1:
        return _extract(last_matches[0])

    # Fuzzy
    close = difflib.get_close_matches(target, norm_names, n=1, cutoff=0.82)
    if close:
        return _extract(norm_names.index(close[0]))

    return {}


def enrich_fip(csv_path: Path = CSV_PATH) -> None:
    if not csv_path.exists():
        print(f"Dataset not found: {csv_path}")
        sys.exit(1)

    df = pd.read_csv(csv_path, encoding="latin-1")
    print(f"Loaded {len(df)} rows from {csv_path}")

    seasons = sorted(df["season"].unique())
    metrics = ["fip", "k_pct", "bb_pct"]

    for side in ("home", "away"):
        for m in metrics:
            col = f"{side}_{m}"
            if col not in df.columns:
                df[col] = float("nan")

    for season in seasons:
        mask = df["season"] == season
        rows  = df[mask].index
        print(f"\nSeason {season}: {mask.sum()} games")

        found_h = found_a = 0
        for idx in rows:
            h_name = str(df.at[idx, "home_starter"] or "").strip()
            a_name = str(df.at[idx, "away_starter"] or "").strip()

            h_stats = _lookup_pitcher_fip(h_name, season)
            a_stats = _lookup_pitcher_fip(a_name, season)

            for m in metrics:
                # Only overwrite if currently NaN — don't clobber FanGraphs values
                if m in h_stats and pd.isna(df.at[idx, f"home_{m}"]):
                    df.at[idx, f"home_{m}"] = h_stats[m]
                if m in a_stats and pd.isna(df.at[idx, f"away_{m}"]):
                    df.at[idx, f"away_{m}"] = a_stats[m]

            if h_stats:
                found_h += 1
            if a_stats:
                found_a += 1

        total = mask.sum()
        print(f"  home starters matched: {found_h}/{total} ({found_h/total*100:.1f}%)")
        print(f"  away starters matched: {found_a}/{total} ({found_a/total*100:.1f}%)")

    print("\nCoverage after BRef FIP enrichment:")
    for side in ("home", "away"):
        for m in metrics:
            col = f"{side}_{m}"
            n   = df[col].notna().sum()
            print(f"  {col:<25} {n:>5}/{len(df)}  ({n/len(df)*100:.1f}%)")

    df["game_date"] = pd.to_datetime(df["game_date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"\nSaved enriched dataset → {csv_path}")
    print("Next step: python models/nrfi_model.py --train")


if __name__ == "__main__":
    enrich_fip()
