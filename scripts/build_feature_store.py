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

Data sources (in order of reliability from a residential IP):
  - Baseball Savant (MLB official)  → barrel%, hard-hit%, exit velo, xwOBA
                                       (the backbone — one bulk leaderboard call)
  - FanGraphs (best-effort)         → SIERA, xFIP, CSW%, O-Swing%, K%, BB%,
                                       GB%, HR/FB — SKIPPED automatically if the
                                       endpoint 403s (it currently does)
  - Savant pitch arsenal (optional) → fastball velo, whiff% (--with-arsenal;
                                       per-pitcher, slower)

Usage:
  python scripts/build_feature_store.py                 # Savant backbone (+FG if it works)
  python scripts/build_feature_store.py --with-arsenal  # also fastball velo / whiff (slower)
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

    # ── Best-effort: FanGraphs for SIERA/xFIP/CSW%/O-Swing% (skipped if 403)
    sources = ["baseball_savant"]
    fg_df = _load_fg_pitchers(yr)
    if fg_df is not None and not fg_df.empty:
        sources.append("fangraphs")
        n_fg = 0
        for _, r in fg_df.iterrows():
            key = _norm(str(r.get("Name", "")))
            if key not in pitchers:
                pitchers[key] = {"display_name": str(r.get("Name", ""))}
            p = pitchers[key]
            for src_col, dst in [
                ("SIERA", "siera"), ("xFIP", "xfip"), ("FIP", "fip"),
                ("CSW%", "csw_pct"), ("O-Swing%", "o_swing_pct"),
                ("K%", "k_pct"), ("BB%", "bb_pct"),
                ("GB%", "gb_pct"), ("HR/FB", "hr_fb_pct"),
            ]:
                v = _safe(r.get(src_col))
                if v is not None:
                    p[dst] = v
            n_fg += 1
        print(f"FanGraphs: merged advanced stats for {n_fg} pitchers.")
    else:
        print("FanGraphs: unavailable (endpoint 403) — SIERA/CSW%/O-Swing% will "
              "default; ESPN FIP + Savant Statcast still differentiate games.")

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
