"""
scripts/build_feature_store.py

Build the committed pitcher feature store (data/nrfi_feature_store.json) that
lets the NRFI model get real advanced pitcher features in CI, where the live
stat sources are unreachable.

WHY: FanGraphs retired the legacy leaderboard endpoint pybaseball uses (it now
returns HTTP 403 for everyone, on any IP), and both FanGraphs and Baseball
Savant block/deny datacenter IPs (GitHub Actions, Railway). So live enrichment
fails in production and every starter defaults to ESPN — flattening the NRFI
model to ~51% on every game. Run THIS locally (residential IP), commit the
JSON, and the pipeline reads it at runtime with no live fetch.

Data sources:
  - Baseball Savant (MLB official)  → barrel%, hard-hit%, whiff%, velo, xwOBA
                                       (auto-fetched — the backbone)
  - FanGraphs advanced stats        → SIERA, xFIP, FIP, CSW%, O-Swing%, K%, BB%,
                                       GB%, HR/FB. FanGraphs blocks scripts
                                       (Cloudflare 403), so we can't fetch these
                                       live. Instead, a FanGraphs MEMBER exports
                                       the pitching leaderboard(s) to CSV and
                                       drops them in data/ (see below). This
                                       script reads any data/fangraphs*.csv and
                                       merges the columns it finds, converting
                                       percentages to the decimals the model
                                       expects. Optional but recommended.

How to add FanGraphs stats (needs a FanGraphs membership):
  1. FanGraphs → Leaders → Pitching → your season, qualified (or low IP filter).
  2. Pick the "Advanced" view (SIERA, xFIP, FIP, K%, BB%, HR/FB) → Export Data →
     save as  data/fangraphs_advanced.csv
  3. (optional) Pick "Plate Discipline" (O-Swing%, CSW% if shown) → Export Data →
     save as  data/fangraphs_plate.csv
  4. Re-run this script — it auto-detects and merges those files by pitcher name.

Usage:
  python scripts/build_feature_store.py            # Savant + any data/fangraphs*.csv
  python scripts/build_feature_store.py --season 2025

Then commit data/nrfi_feature_store.json.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

OUT_PATH = _REPO / "data" / "nrfi_feature_store.json"


def _norm(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _norm_savant(last_first: str) -> tuple[str, str]:
    """'Valdez, Framber' → (normalized key 'frambervaldez', display 'Framber Valdez')."""
    if "," in last_first:
        last, first = [p.strip() for p in last_first.split(",", 1)]
        disp = f"{first} {last}".strip()
    else:
        disp = last_first.strip()
    return _norm(disp), disp


def _safe(v):
    try:
        f = float(v)
        return f if f == f else None   # drop NaN
    except (TypeError, ValueError):
        return None


# FanGraphs CSV column → (store field, is_percentage). Percentages are
# normalized to the decimals the model expects (CSW% 28.5 -> 0.285).
_FG_COLS = [
    ("SIERA", "siera", False), ("xFIP", "xfip", False), ("FIP", "fip", False),
    ("CSW%", "csw_pct", True), ("O-Swing%", "o_swing_pct", True),
    ("K%", "k_pct", True), ("BB%", "bb_pct", True),
    ("GB%", "gb_pct", True), ("HR/FB", "hr_fb_pct", True),
]


def _to_decimal(v):
    """A percent value in either form (28.5 or 0.285) → decimal 0.285."""
    f = _safe(str(v).replace("%", "").strip())
    if f is None:
        return None
    return round(f / 100.0, 4) if f > 1.5 else round(f, 4)


def _merge_fangraphs_csv(pitchers: dict, data_dir) -> int:
    """
    Merge FanGraphs member-exported CSV(s) into the pitcher store. Reads every
    file matching data/fangraphs*.csv (so you can drop 'Advanced' and 'Plate
    Discipline' exports separately). Matches columns case-insensitively and
    normalizes percentages to decimals. Returns number of pitchers touched.
    """
    from pathlib import Path
    try:
        import pandas as pd
    except ImportError:
        return 0

    files = sorted(Path(data_dir).glob("fangraphs*.csv"))
    if not files:
        return 0

    touched: set[str] = set()
    for f in files:
        try:
            df = pd.read_csv(f)
        except Exception as e:
            print(f"  could not read {f.name}: {e}")
            continue
        # case-insensitive header lookup
        cols = {str(c).strip().lower(): c for c in df.columns}
        name_col = next((cols[c] for c in ("name", "playername", "player_name", "player")
                         if c in cols), None)
        if not name_col:
            print(f"  {f.name}: no Name column — skipping")
            continue
        matched = [dst for (src, dst, _p) in _FG_COLS if src.lower() in cols]
        print(f"  {f.name}: {len(df)} rows, columns matched → {matched or 'none'}")
        for _, r in df.iterrows():
            key = _norm(str(r.get(name_col, "")))
            if not key:
                continue
            if key not in pitchers:
                pitchers[key] = {"display_name": str(r.get(name_col, ""))}
            p = pitchers[key]
            for src, dst, is_pct in _FG_COLS:
                if src.lower() not in cols:
                    continue
                raw = r.get(cols[src.lower()])
                val = _to_decimal(raw) if is_pct else _safe(raw)
                if val is not None:
                    p[dst] = val
                    touched.add(key)
    if touched:
        print(f"FanGraphs CSV: merged advanced stats for {len(touched)} pitchers.")
    return len(touched)


def build(season: int | None = None, with_arsenal: bool = False) -> None:
    # Reuse the EXACT Savant fetch the training data was built from
    # (scripts/enrich_nrfi_savant._load_savant_season), so the store's columns
    # and units match what the model learned on — no risk of a whole-number vs
    # decimal (100x) mismatch from re-implementing the column mapping.
    from scripts.enrich_nrfi_savant import _load_savant_season, _norm as _sv_norm  # noqa: F401
    from fetchers.savant import _current_season, _load_fg_pitchers

    yr = season or _current_season()
    print(f"Building feature store for season {yr} (Baseball Savant backbone, "
          f"training-consistent fetch)...\n")

    lookup = _load_savant_season(yr)   # {"first last": {barrel_pct, hard_hit_pct, whiff_pct, avg_velo, xwoba_against}}
    if not lookup:
        print(f"\nSavant returned nothing for {yr}. Try --season {yr-1}. "
              "(Savant is MLB-official and rarely blocks residential IPs — check "
              "internet / pybaseball install.)")
        sys.exit(1)

    pitchers: dict[str, dict] = {}
    for spaced_name, feats in lookup.items():
        key = _norm(spaced_name)
        if not key:
            continue
        rec = {"display_name": " ".join(w.capitalize() for w in spaced_name.split())}
        for k, v in feats.items():
            fv = _safe(v)
            if fv is not None:
                rec[k] = fv
        if len(rec) > 1:            # keep only pitchers with >=1 real metric
            pitchers[key] = rec
    print(f"\nSavant: {len(pitchers)} pitchers with real Statcast metrics "
          "(barrel%/hard-hit%/whiff%/velo).")

    # ── FanGraphs advanced stats (SIERA/xFIP/CSW%/O-Swing%) ───────────────────
    # Live scraping is blocked (Cloudflare 403). Instead, a FanGraphs *member*
    # exports the pitching leaderboard(s) to CSV in their browser and drops the
    # file(s) in data/ as fangraphs*.csv — this reads + merges them. Values are
    # normalized to the DECIMAL units the model was trained on (CSW% 28.5 -> 0.285).
    sources = ["baseball_savant"]
    n_fg = _merge_fangraphs_csv(pitchers, _REPO / "data")
    if n_fg:
        sources.append("fangraphs_csv")
    else:
        # Fall back to the (usually blocked) pybaseball path just in case.
        fg_df = _load_fg_pitchers(yr)
        if fg_df is not None and not fg_df.empty:
            sources.append("fangraphs")
            for _, r in fg_df.iterrows():
                key = _norm(str(r.get("Name", "")))
                if key not in pitchers:
                    pitchers[key] = {"display_name": str(r.get("Name", ""))}
                p = pitchers[key]
                for src_col, dst, is_pct in _FG_COLS:
                    v = _safe(r.get(src_col))
                    if v is not None:
                        p[dst] = _to_decimal(v) if is_pct else v
            print(f"FanGraphs (pybaseball): merged {len(fg_df)} pitchers.")
        else:
            print("FanGraphs: no CSV in data/ and live endpoint 403 — SIERA/CSW%/"
                  "O-Swing% will default. To add them: export the pitching "
                  "leaderboard from FanGraphs to data/fangraphs_advanced.csv (see "
                  "the script header) and re-run.")

    store = {
        "season":     yr,
        "built_at":   datetime.now(timezone.utc).isoformat(),
        "sources":    sources,
        "n_pitchers": len(pitchers),
        "pitchers":   pitchers,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(store, f, indent=2)

    def _cov(field):
        return sum(1 for p in pitchers.values() if p.get(field) is not None)
    print(f"\nSaved {len(pitchers)} pitchers → {OUT_PATH}")
    print(f"  barrel%: {_cov('barrel_pct')}  hard-hit%: {_cov('hard_hit_pct')}  "
          f"whiff%: {_cov('whiff_pct')}  velo: {_cov('avg_velo')}  "
          f"SIERA: {_cov('siera')}  | sources: {sources}")
    print("\nCommit it:  git add data/nrfi_feature_store.json && "
          "git commit -m 'nrfi: feature store' && git push")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    args = ap.parse_args()
    build(season=args.season)
