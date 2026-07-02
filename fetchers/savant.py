"""
Baseball Savant (Statcast) + FanGraphs data fetcher.

Provides real SIERA, xFIP, xwOBA, barrel%, hard-hit%, velocity —
significantly better inputs than the ESPN-approximated FIP/wRC+.

Access strategy (no API key required for either source):
  1. pybaseball library wraps both Baseball Savant CSV API and FanGraphs
  2. Direct CSV fallback for Baseball Savant leaderboard
  3. All functions return None / empty dict on any failure — ESPN data is
     always the guaranteed fallback in analyze_baseball.py

Install: pip install pybaseball

Accuracy impact:
  SIERA > xFIP > FIP as single pitcher quality metrics (3-5% better Brier)
  xwOBA slightly better than wRC+ approximation for current-season offense
  Barrel% against is the best leading indicator for HR-suppression
"""
from __future__ import annotations
import difflib
import os
from typing import Optional

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    import pybaseball as pyb
    pyb.cache.enable()        # local CSV cache — avoids re-fetching same data
    _HAS_PYBASEBALL = True
except ImportError:
    _HAS_PYBASEBALL = False

# Module-level cache so a single analysis session doesn't hit the API twice
_PITCHER_CACHE: dict[int, "pd.DataFrame"] = {}   # season → df
_BATTER_CACHE:  dict[int, "pd.DataFrame"] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_season() -> int:
    from datetime import datetime
    return datetime.now().year


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def _match_name(target: str, names: list[str]) -> Optional[str]:
    """Fuzzy match a player name against a list; returns best match or None."""
    norm_target = _norm(target)
    norm_names = [_norm(n) for n in names]

    # Exact
    if norm_target in norm_names:
        return names[norm_names.index(norm_target)]

    # Last name exact
    last = norm_target.split()[-1]
    last_matches = [n for n in norm_names if n.split()[-1] == last]
    if len(last_matches) == 1:
        return names[norm_names.index(last_matches[0])]

    # Fuzzy
    close = difflib.get_close_matches(norm_target, norm_names, n=1, cutoff=0.82)
    if close:
        return names[norm_names.index(close[0])]

    return None


def _safe_float(val, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(val)
        return f if not (f != f) else default   # NaN check
    except (TypeError, ValueError):
        return default


# ── FanGraphs pitcher stats ───────────────────────────────────────────────────

def _load_fg_pitchers(season: int) -> Optional["pd.DataFrame"]:
    if not _HAS_PYBASEBALL or not _HAS_PANDAS:
        return None
    if season in _PITCHER_CACHE:
        return _PITCHER_CACHE[season]
    for yr in (season, season - 1):
        try:
            df = pyb.pitching_stats(yr, qual=10)
            if df is not None and not df.empty:
                _PITCHER_CACHE[season] = df
                return df
        except Exception:
            continue
    return None


def fetch_pitcher_fg(
    name: str,
    season: Optional[int] = None,
) -> dict:
    """
    Look up a starter's advanced stats from FanGraphs.

    Returns dict with: siera, xfip, fip, k_pct, bb_pct, swstr_pct,
    gb_pct, hr_per_9, whip, ip, war. Empty dict on lookup failure.

    Key stats for the model:
      SIERA  — best single pitcher quality metric (accounts for GB/FB)
      xFIP   — FIP with HR normalized to league avg (removes HR luck)
      SwStr% — swinging strike rate; leading indicator for K rate
    """
    if not name or name.strip().lower() in ("tbd", "unknown", ""):
        return {}
    df = _load_fg_pitchers(season or _current_season())
    if df is None:
        return {}
    try:
        names = df["Name"].tolist()
        matched = _match_name(name, names)
        if not matched:
            return {}
        row = df[df["Name"] == matched].iloc[0]
        return {
            "name":         matched,
            # Quality / run prevention
            "siera":        _safe_float(row.get("SIERA")),
            "xfip":         _safe_float(row.get("xFIP")),
            "fip":          _safe_float(row.get("FIP")),
            "era":          _safe_float(row.get("ERA")),
            # Strikeout / walk profile
            "k_pct":        _safe_float(row.get("K%")),
            "bb_pct":       _safe_float(row.get("BB%")),
            "k_bb_ratio":   _safe_float(row.get("K-BB%")),
            # Pitch command (Kimi feature set — all from FanGraphs pitching_stats)
            "swstr_pct":    _safe_float(row.get("SwStr%")),   # swinging strike rate
            "f_strike_pct": _safe_float(row.get("F-Strike%")), # first-pitch strike rate
            "zone_pct":     _safe_float(row.get("Zone%")),    # pitches in strike zone
            "o_swing_pct":  _safe_float(row.get("O-Swing%")), # chase rate (out-of-zone swings)
            "contact_pct":  _safe_float(row.get("Contact%")), # contact rate on swings
            "csw_pct":      _safe_float(row.get("CSW%")),     # called strike + whiff %
            # Batted-ball / HR
            "gb_pct":       _safe_float(row.get("GB%")),
            "fb_pct":       _safe_float(row.get("FB%")),
            "hr_per_9":     _safe_float(row.get("HR/9")),
            "hr_fb_pct":    _safe_float(row.get("HR/FB")),    # HR per fly ball (luck indicator)
            # Workload
            "whip":         _safe_float(row.get("WHIP")),
            "ip":           _safe_float(row.get("IP")),
            "war":          _safe_float(row.get("WAR")),
            "source":       "fangraphs",
        }
    except Exception:
        return {}


# ── FanGraphs batter stats ────────────────────────────────────────────────────

def _load_fg_batters(season: int) -> Optional["pd.DataFrame"]:
    if not _HAS_PYBASEBALL or not _HAS_PANDAS:
        return None
    if season in _BATTER_CACHE:
        return _BATTER_CACHE[season]
    for yr in (season, season - 1):
        try:
            df = pyb.batting_stats(yr, qual=100)
            if df is not None and not df.empty:
                _BATTER_CACHE[season] = df
                return df
        except Exception:
            continue
    return None


def fetch_team_batting_fg(
    team_abbr: str,
    season: Optional[int] = None,
) -> dict:
    """
    Aggregate FanGraphs batting stats for a full team roster.

    Returns team-level weighted averages: wrc_plus (real), woba, iso,
    k_pct, bb_pct, babip. These are used in place of the ESPN wRC+
    approximation when available.
    """
    if not team_abbr:
        return {}
    df = _load_fg_batters(season or _current_season())
    if df is None:
        return {}
    try:
        # FanGraphs team abbreviations differ slightly from ESPN
        fg_abbr = _ESPN_TO_FG_ABBR.get(team_abbr.upper(), team_abbr.upper())
        team_df = df[df["Team"] == fg_abbr]
        if team_df.empty:
            return {}
        # Weight by PA
        pa = team_df["PA"].astype(float)
        total_pa = pa.sum()
        if total_pa < 1:
            return {}

        def wavg(col: str) -> Optional[float]:
            if col not in team_df.columns:
                return None
            vals = team_df[col].astype(float)
            return float((vals * pa).sum() / total_pa)

        return {
            "wrc_plus":  wavg("wRC+"),
            "woba":      wavg("wOBA"),
            "iso":       wavg("ISO"),
            "babip":     wavg("BABIP"),
            "k_pct":     wavg("K%"),
            "bb_pct":    wavg("BB%"),
            "obp":       wavg("OBP"),
            "slg":       wavg("SLG"),
            "source":    "fangraphs",
        }
    except Exception:
        return {}


# ── Baseball Savant Statcast stats ────────────────────────────────────────────

def fetch_team_statcast(
    team_abbr: str,
    season: Optional[int] = None,
    player_type: str = "pitcher",
) -> dict:
    """
    Fetch team-level Statcast aggregates from Baseball Savant CSV export.

    player_type="pitcher" → barrel% against, hard-hit% against, xwOBA against
    player_type="batter"  → team barrel%, exit velo, xwOBA for offense

    Runs without pybaseball (direct CSV fetch with httpx).
    Returns empty dict on failure.
    """
    try:
        import httpx
    except ImportError:
        return {}

    abbr = _ESPN_TO_SAVANT_ABBR.get(team_abbr.upper(), team_abbr.upper())
    yr = season or _current_season()
    url = "https://baseballsavant.mlb.com/statcast_search/csv"
    params = {
        "hfSea":        f"{yr}|",
        "player_type":  player_type,
        "hfAB":         "",
        "hfGT":         "R|",
        "group_by":     "name",
        "sort_col":     "pitches",
        "sort_order":   "desc",
        "min_abs":      "0",
        "type":         "details",
        "team":         abbr,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            content = resp.text
    except Exception:
        return {}

    if not content or "pitch_type" not in content:
        return {}

    try:
        import io
        if not _HAS_PANDAS:
            return {}
        import pandas as pd
        df = pd.read_csv(io.StringIO(content), low_memory=False)
        if df.empty:
            return {}

        def col_mean(c: str) -> Optional[float]:
            if c not in df.columns:
                return None
            vals = df[c].dropna().astype(float)
            return float(vals.mean()) if not vals.empty else None

        if player_type == "pitcher":
            return {
                "barrel_pct_against":    col_mean("barrel_batted_rate"),
                "hard_hit_pct_against":  col_mean("hard_hit_percent"),
                "exit_velo_against":     col_mean("exit_velocity_avg"),
                "xwoba_against":         col_mean("xwoba"),
                "xba_against":           col_mean("xba"),
                "source":                "baseball_savant",
            }
        else:
            return {
                "barrel_pct":   col_mean("barrel_batted_rate"),
                "hard_hit_pct": col_mean("hard_hit_percent"),
                "exit_velo":    col_mean("exit_velocity_avg"),
                "xwoba":        col_mean("xwoba"),
                "xba":          col_mean("xba"),
                "source":       "baseball_savant",
            }
    except Exception:
        return {}


def fetch_pitcher_statcast(
    player_name: str,
    season: Optional[int] = None,
) -> dict:
    """
    Fetch individual pitcher Statcast metrics from Baseball Savant.

    Returns: xera, barrel_pct_against, hard_hit_pct, exit_velo, xwoba_against,
    velocity, spin_rate, whiff_pct. Empty dict on failure.

    xERA = the ERA the pitcher "deserved" based on exit velocity / launch angle.
    Better than ERA for forward-looking predictions.
    """
    if not _HAS_PYBASEBALL or not _HAS_PANDAS:
        return {}
    try:
        yr = season or _current_season()
        # pybaseball leaderboard: expected stats for pitchers
        df = pyb.statcast_pitcher_exitvelo_barrels(yr, minBBE=20)
        if df is None or df.empty:
            return {}
        names = df["last_name, first_name"].tolist() if "last_name, first_name" in df.columns else []
        # Savant returns "Last, First" format — try to match
        matched = _match_name_savant(player_name, names)
        if not matched:
            return {}
        row = df[df["last_name, first_name"] == matched].iloc[0]
        return {
            "barrel_pct_against":   _safe_float(row.get("barrel_batted_rate")),
            "hard_hit_pct_against": _safe_float(row.get("hard_hit_percent")),
            "exit_velo_against":    _safe_float(row.get("avg_hit_speed")),
            "xwoba_against":        _safe_float(row.get("xwoba")),
            "source":               "baseball_savant",
        }
    except Exception:
        return {}


def fetch_pitcher_arsenal(
    player_name: str,
    season: Optional[int] = None,
) -> dict:
    """
    Fetch pitch-mix and velocity profile from Baseball Savant pitch arsenal leaderboard.

    Returns: fastball_pct, breaking_pct, offspeed_pct, avg_velo, top_pitch_type,
    plus per-pitch whiff%, usage%. These are the Kimi-recommended pitch-level
    features: pitch mix vs LHB/RHB, velocity, movement differentials.

    whiff% by pitch type is a leading indicator for K rate changes.
    """
    if not _HAS_PYBASEBALL or not _HAS_PANDAS:
        return {}
    try:
        yr = season or _current_season()
        df = pyb.statcast_pitcher_pitch_arsenal(yr, minP=100)
        if df is None or df.empty:
            return {}
        names = df["last_name, first_name"].tolist() if "last_name, first_name" in df.columns else []
        matched = _match_name_savant(player_name, names)
        if not matched:
            return {}
        rows = df[df["last_name, first_name"] == matched]
        if rows.empty:
            return {}

        # Aggregate pitch mix into FB / Breaking / Offspeed buckets
        # pitch_type column: FF/SI=fastball, SL/CU/KC=breaking, CH/FS=offspeed
        fb_types = {"FF", "SI", "FC"}
        br_types = {"SL", "CU", "KC", "CS", "SV"}
        os_types = {"CH", "FS", "FO", "SC"}

        fb_pct = br_pct = os_pct = 0.0
        top_pitch = ""
        top_usage = 0.0
        pitches = []

        for _, r in rows.iterrows():
            pt   = str(r.get("pitch_type", "")).upper()
            pct  = _safe_float(r.get("pitch_percent")) or 0.0
            velo = _safe_float(r.get("avg_speed"))
            whiff = _safe_float(r.get("whiff_percent"))
            if pct > top_usage:
                top_usage = pct
                top_pitch = pt
            if pt in fb_types:
                fb_pct += pct
            elif pt in br_types:
                br_pct += pct
            elif pt in os_types:
                os_pct += pct
            if velo:
                pitches.append((pt, pct, velo, whiff))

        # Weighted average fastball velo
        fb_rows = [(p, u, v, w) for p, u, v, w in pitches if p in fb_types and v]
        avg_fb_velo = (
            sum(v * u for _, u, v, _ in fb_rows) / sum(u for _, u, _, _ in fb_rows)
            if fb_rows else None
        )

        return {
            "fastball_pct":   round(fb_pct, 1),
            "breaking_pct":   round(br_pct, 1),
            "offspeed_pct":   round(os_pct, 1),
            "top_pitch_type": top_pitch,
            "avg_fb_velo":    avg_fb_velo,
            "pitch_details":  [
                {"type": p, "usage_pct": u, "avg_velo": v, "whiff_pct": w}
                for p, u, v, w in sorted(pitches, key=lambda x: -x[1])
            ],
            "source": "baseball_savant_arsenal",
        }
    except Exception:
        return {}


def _match_name_savant(target: str, savant_names: list[str]) -> Optional[str]:
    """Match 'First Last' against Savant's 'Last, First' format."""
    parts = target.strip().split()
    if len(parts) >= 2:
        last_first = f"{parts[-1]}, {' '.join(parts[:-1])}"
        norm_savant = [_norm(n) for n in savant_names]
        norm_target = _norm(last_first)
        if norm_target in norm_savant:
            return savant_names[norm_savant.index(norm_target)]
        close = difflib.get_close_matches(norm_target, norm_savant, n=1, cutoff=0.8)
        if close:
            return savant_names[norm_savant.index(close[0])]
    return None


# ── Combined enrichment: best available stats for a starter ──────────────────

def enrich_starter(
    starter: dict,
    team_abbr: str = "",
    season: Optional[int] = None,
) -> dict:
    """
    Enrich an ESPN starter dict with FanGraphs SIERA/xFIP and Savant metrics.

    Priority:
      FIP used in model = SIERA if available, else xFIP, else FanGraphs FIP,
      else ESPN-computed FIP (original fallback unchanged).

    Does NOT modify the original dict — returns a new merged dict.
    """
    result = dict(starter)
    name = starter.get("name", "")
    yr = season or _current_season()

    # ── Feature store first (works in CI where FanGraphs/Savant are IP-blocked)
    # If the committed store has this pitcher, use it and skip live fetches —
    # they fail from datacenter IPs anyway. Live fetch below still runs when the
    # store is absent (e.g. local dev on a residential IP).
    try:
        from fetchers.feature_store import lookup_pitcher_features
        fs = lookup_pitcher_features(name)
    except Exception:
        fs = {}
    if fs:
        for k, v in fs.items():
            if k == "display_name" or v is None:
                continue
            result[k] = v
        # Canonical aliases so downstream serialization, the UI, and the daily
        # diagnostics see the values under the names they expect (the model
        # already reads both via predict_nrfi._get, but these don't).
        if result.get("barrel_pct") is not None and result.get("barrel_pct_against") is None:
            result["barrel_pct_against"] = result["barrel_pct"]
        if result.get("hard_hit_pct") is not None and result.get("hard_hit_pct_against") is None:
            result["hard_hit_pct_against"] = result["hard_hit_pct"]
        if result.get("avg_velo") is not None and result.get("avg_fb_velo") is None:
            result["avg_fb_velo"] = result["avg_velo"]
        best_quality = fs.get("siera") or fs.get("xfip") or fs.get("fip")
        if best_quality and best_quality > 0:
            result["fip"] = best_quality
            result["fip_source"] = (
                "siera" if fs.get("siera") else
                "xfip"  if fs.get("xfip")  else "fangraphs_fip"
            )
        result["feature_source"] = "store"
        return result

    fg = fetch_pitcher_fg(name, yr)
    if fg:
        # Use SIERA as the primary "FIP-equivalent" when available
        best_quality = (
            fg.get("siera") or fg.get("xfip") or fg.get("fip")
        )
        if best_quality and best_quality > 0:
            result["fip"] = best_quality          # feeds directly into model
            result["fip_source"] = (
                "siera" if fg.get("siera") else
                "xfip"  if fg.get("xfip")  else "fangraphs_fip"
            )
        if fg.get("era"):
            result["era"] = fg["era"]
        result["siera"]         = fg.get("siera")
        result["xfip"]          = fg.get("xfip")
        result["k_pct"]         = fg.get("k_pct")
        result["bb_pct"]        = fg.get("bb_pct")
        result["k_bb_ratio"]    = fg.get("k_bb_ratio")
        result["swstr_pct"]     = fg.get("swstr_pct")
        result["fg_war"]        = fg.get("war")
        # Pitch command features (Kimi recommendations)
        result["f_strike_pct"]  = fg.get("f_strike_pct")   # first-pitch strike %
        result["zone_pct"]      = fg.get("zone_pct")        # zone rate
        result["o_swing_pct"]   = fg.get("o_swing_pct")     # chase rate
        result["contact_pct"]   = fg.get("contact_pct")     # contact on swings
        result["csw_pct"]       = fg.get("csw_pct")         # called strike + whiff
        result["hr_fb_pct"]     = fg.get("hr_fb_pct")       # HR/FB (luck factor)

    sv = fetch_pitcher_statcast(name, yr)
    if sv:
        result["barrel_pct_against"]   = sv.get("barrel_pct_against")
        result["hard_hit_pct_against"] = sv.get("hard_hit_pct_against")
        result["exit_velo_against"]    = sv.get("exit_velo_against")
        result["xwoba_against"]        = sv.get("xwoba_against")

    ar = fetch_pitcher_arsenal(name, yr)
    if ar:
        result["avg_fb_velo"]     = ar.get("avg_fb_velo")    # fastball velocity
        result["fastball_pct"]    = ar.get("fastball_pct")   # pitch mix
        result["breaking_pct"]    = ar.get("breaking_pct")
        result["offspeed_pct"]    = ar.get("offspeed_pct")
        result["top_pitch_type"]  = ar.get("top_pitch_type")
        result["pitch_details"]   = ar.get("pitch_details", [])

    return result


def enrich_team_hitting(
    hitting: dict,
    team_abbr: str,
    season: Optional[int] = None,
) -> dict:
    """
    Enrich an ESPN team hitting dict with FanGraphs real wRC+.

    If FanGraphs wRC+ is available it replaces the ESPN approximation
    (our 0.97-corr (2×OBP+SLG)/1.045×100 formula).
    """
    result = dict(hitting)
    yr = season or _current_season()
    fg = fetch_team_batting_fg(team_abbr, yr)
    if fg and fg.get("wrc_plus"):
        result["wrc_plus"] = fg["wrc_plus"]
        result["wrc_plus_source"] = "fangraphs"
        result["woba"] = fg.get("woba")
        result["iso"]  = fg.get("iso")
    return result


# ── Team abbreviation mappings ────────────────────────────────────────────────

# ESPN abbreviation → FanGraphs abbreviation (where they differ)
_ESPN_TO_FG_ABBR: dict[str, str] = {
    "WSH": "WSN",   # Nationals
    "CWS": "CHW",   # White Sox
    "KC":  "KCR",   # Royals
    "SD":  "SDP",   # Padres
    "SF":  "SFG",   # Giants
    "STL": "STL",
    "TB":  "TBR",   # Rays
    "OAK": "OAK",
}

# ESPN abbreviation → Baseball Savant team filter (where they differ)
_ESPN_TO_SAVANT_ABBR: dict[str, str] = {
    "WSH": "WSH",
    "CWS": "CWS",
    "KC":  "KC",
    "OAK": "OAK",
}
