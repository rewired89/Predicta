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
    try:
        import pybaseball as pyb
    except Exception as e:
        print(f"pybaseball import failed: {e}\n  → pip install pybaseball")
        sys.exit(1)
    from fetchers.savant import _current_season, _load_fg_pitchers, fetch_pitcher_arsenal

    yr = season or _current_season()
    print(f"Building feature store for season {yr} (Baseball Savant backbone)...\n")

    # ── Backbone: Baseball Savant exit-velo / barrels leaderboard (one bulk call)
    try:
        sv = pyb.statcast_pitcher_exitvelo_barrels(yr, minBBE=20)
    except Exception as e:
        print(f"Baseball Savant fetch FAILED: {type(e).__name__}: {str(e)[:200]}")
        print("\nSavant is MLB's official site and rarely blocks residential IPs.")
        print("If this failed from home, check: internet up? pybaseball installed?")
        print(f"Also try last season: --season {yr-1}")
        sys.exit(1)
    if sv is None or sv.empty:
        print(f"Savant returned no rows for {yr}. Try --season {yr-1}.")
        sys.exit(1)

    name_col = "last_name, first_name"
    if name_col not in sv.columns:
        print(f"Unexpected Savant columns: {list(sv.columns)[:15]}")
        sys.exit(1)

    pitchers: dict[str, dict] = {}
    for _, row in sv.iterrows():
        key, disp = _norm_savant(str(row.get(name_col, "")))
        if not key:
            continue
        pitchers[key] = {
            "display_name":         disp,
            "barrel_pct_against":   _safe(row.get("barrel_batted_rate")),
            "hard_hit_pct_against": _safe(row.get("hard_hit_percent")),
            "exit_velo_against":    _safe(row.get("avg_hit_speed")),
            "xwoba_against":        _safe(row.get("xwoba")),
        }
    print(f"Savant: {len(pitchers)} pitchers with barrel%/hard-hit%/velo/xwOBA.")

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

    # ── Optional: per-pitcher arsenal for fastball velo + whiff (slower)
    if with_arsenal:
        print("Fetching pitch arsenal (velo/whiff) per pitcher — this is slow...")
        for i, (key, p) in enumerate(list(pitchers.items()), 1):
            try:
                ar = fetch_pitcher_arsenal(p["display_name"], yr)
                if ar:
                    if ar.get("avg_fb_velo") is not None:
                        p["avg_fb_velo"] = ar["avg_fb_velo"]
                    if ar.get("whiff_pct") is not None:
                        p["whiff_pct"] = ar["whiff_pct"]
            except Exception:
                pass
            time.sleep(0.3)
            if i % 25 == 0:
                print(f"  arsenal {i}/{len(pitchers)}...")
        sources.append("savant_arsenal")

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

    n_barrel = sum(1 for p in pitchers.values() if p.get("barrel_pct_against") is not None)
    n_siera  = sum(1 for p in pitchers.values() if p.get("siera") is not None)
    print(f"\nSaved {len(pitchers)} pitchers → {OUT_PATH}")
    print(f"  with barrel%: {n_barrel}  |  with SIERA: {n_siera}  |  sources: {sources}")
    print("\nCommit it:  git add data/nrfi_feature_store.json && "
          "git commit -m 'nrfi: feature store' && git push")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--with-arsenal", action="store_true",
                    help="Also fetch fastball velo / whiff per pitcher (slower)")
    args = ap.parse_args()
    build(season=args.season, with_arsenal=args.with_arsenal)
