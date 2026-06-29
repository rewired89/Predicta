"""
FBref soccer enrichment fetcher.

FBref.com (Sports Reference) publishes per-team squad tables for all major
European leagues with significantly richer process metrics than Understat:

  Goalkeeper:   PSxG, PSxG-GA  (post-shot xG vs goals allowed — best public GK metric)
  Possession:   touches in def/mid/att third, progressive passes received
  Defense:      tackles, pressures, blocks, interceptions
  Misc:         aerials_won, aerials_won_pct, fouls, yellow/red cards

Derived metrics:
  PSxG-GA per 90        — goalkeeper quality (positive = saves above expected)
  PPDA                  — passes allowed per defensive action (pressing intensity)
  Field tilt            — % of final-third touches (territorial dominance)
  Aerial duel win pct   — set-piece input

Access strategy:
  Public HTML tables under https://fbref.com/en/comps/<id>/<season>/...
  No API key. Tables are embedded in HTML inside <!-- ... --> comments to
  reduce scraper load; we strip the comments before parsing with pandas.read_html.

All functions return empty dict / None on failure. Soccer pipeline must
fall back to Understat-only enrichment.
"""
from __future__ import annotations
import difflib
import re
from typing import Optional

try:
    import httpx
    import pandas as pd
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False

FBREF_BASE = "https://fbref.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Module cache keyed by (league, season, table_type) → DataFrame
_FBREF_CACHE: dict[tuple, "pd.DataFrame"] = {}

# League slug → (fbref competition id, slug-name) — only the leagues Understat covers
# so we don't promise data we can't deliver.
_LEAGUE_TO_FBREF: dict[str, tuple[int, str]] = {
    "EPL":         (9,  "Premier-League"),
    "La_liga":     (12, "La-Liga"),
    "Bundesliga":  (20, "Bundesliga"),
    "Serie_A":     (11, "Serie-A"),
    "Ligue_1":     (13, "Ligue-1"),
}

# Common-name → FBref canonical name (where they differ)
_TEAM_ALIASES: dict[str, str] = {
    "Man United":      "Manchester Utd",
    "Manchester United": "Manchester Utd",
    "Man City":        "Manchester City",
    "Spurs":           "Tottenham",
    "Newcastle":       "Newcastle Utd",
    "Wolves":          "Wolves",
    "Nott'm Forest":   "Nott'ham Forest",
    "Nottingham Forest": "Nott'ham Forest",
    "Brighton":        "Brighton",
    "West Ham":        "West Ham",
    "Atletico":        "Atlético Madrid",
    "Atletico Madrid": "Atlético Madrid",
    "Real Madrid":     "Real Madrid",
    "Bayern":          "Bayern Munich",
    "Dortmund":        "Dortmund",
    "BVB":             "Dortmund",
    "Gladbach":        "M'Gladbach",
    "Inter":           "Inter",
    "Inter Milan":     "Inter",
    "AC Milan":        "Milan",
    "Milan":           "Milan",
    "Roma":            "Roma",
    "PSG":             "Paris S-G",
}


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _fuzzy_match(target: str, names: list[str], cutoff: float = 0.78) -> Optional[str]:
    norm_target = _norm(target)
    norm_names = [_norm(n) for n in names]
    if norm_target in norm_names:
        return names[norm_names.index(norm_target)]
    close = difflib.get_close_matches(norm_target, norm_names, n=1, cutoff=cutoff)
    if close:
        return names[norm_names.index(close[0])]
    return None


def _get(url: str) -> Optional[str]:
    if not _HAS_DEPS:
        return None
    try:
        with httpx.Client(timeout=20.0, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        return None


def _strip_comments(html: str) -> str:
    """FBref hides several tables inside HTML comments — pandas needs them visible."""
    return re.sub(r"<!--|-->", "", html)


def _fetch_table(league: str, season: int, kind: str) -> Optional["pd.DataFrame"]:
    """
    Fetch a single squad-level FBref table. kind:
      'standard'  — team xG / poss / etc
      'keepers_adv' — PSxG / PSxG-GA
      'defense'   — tackles, pressures, blocks
      'misc'      — aerials, fouls, cards
      'possession' — touches by third, progressive passes
    """
    if not _HAS_DEPS or league not in _LEAGUE_TO_FBREF:
        return None
    key = (league, season, kind)
    if key in _FBREF_CACHE:
        return _FBREF_CACHE[key]

    comp_id, slug = _LEAGUE_TO_FBREF[league]
    season_str = f"{season}-{season + 1}"
    page_paths = {
        "standard":   f"/en/comps/{comp_id}/{season_str}/stats/{season_str}-{slug}-Stats",
        "keepers_adv": f"/en/comps/{comp_id}/{season_str}/keepersadv/{season_str}-{slug}-Stats",
        "defense":    f"/en/comps/{comp_id}/{season_str}/defense/{season_str}-{slug}-Stats",
        "misc":       f"/en/comps/{comp_id}/{season_str}/misc/{season_str}-{slug}-Stats",
        "possession": f"/en/comps/{comp_id}/{season_str}/possession/{season_str}-{slug}-Stats",
    }
    path = page_paths.get(kind)
    if not path:
        return None

    html = _get(FBREF_BASE + path)
    if not html:
        return None

    try:
        tables = pd.read_html(_strip_comments(html), match="Squad")
    except Exception:
        return None

    # Take the first "for" (team-stats) table — FBref renders both "for" and
    # "against" panels; "for" is always first.
    for t in tables:
        if "Squad" in [c if isinstance(c, str) else c[-1] for c in t.columns]:
            # Flatten multi-index columns
            if hasattr(t.columns, "levels"):
                t.columns = [c[-1] if isinstance(c, tuple) else c for c in t.columns]
            t = t.dropna(subset=["Squad"]).copy()
            t["Squad"] = t["Squad"].astype(str).str.strip()
            _FBREF_CACHE[key] = t
            return t
    return None


def _resolve_team(team_name: str, table: "pd.DataFrame") -> Optional[str]:
    candidate = _TEAM_ALIASES.get(team_name, team_name)
    squads = table["Squad"].tolist()
    match = _fuzzy_match(candidate, squads)
    if match:
        return match
    return _fuzzy_match(team_name, squads)


def _safe_float(val) -> Optional[float]:
    try:
        f = float(val)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def fetch_team_gk(team_name: str, league: str, season: int) -> dict:
    """
    Goalkeeper quality from FBref advanced keepers table.

    Returns:
      psxg          — total post-shot xG faced
      psxg_minus_ga — keeper's shot-stopping above expected (per 90; positive = good GK)
      saves_pct     — save %
      goals_against — total goals allowed
    Empty dict on failure.
    """
    df = _fetch_table(league, season, "keepers_adv")
    if df is None or df.empty:
        return {}
    squad = _resolve_team(team_name, df)
    if not squad:
        return {}
    row = df[df["Squad"] == squad].iloc[0]
    psxg = _safe_float(row.get("PSxG"))
    psxg_ga = _safe_float(row.get("PSxG+/-"))
    minutes = _safe_float(row.get("90s")) or 1.0
    return {
        "psxg":           psxg,
        "psxg_minus_ga":  psxg_ga,
        "psxg_ga_per_90": round(psxg_ga / minutes, 3) if (psxg_ga is not None and minutes) else None,
        "ga":             _safe_float(row.get("GA")),
        "saves_pct":      _safe_float(row.get("Save%")),
        "source":         f"fbref/{league}/{season} keepers_adv",
    }


def fetch_team_pressing(team_name: str, league: str, season: int) -> dict:
    """
    Pressing intensity proxy from FBref possession + defense tables.

    PPDA (passes allowed per defensive action) is the league-standard pressing
    metric but FBref doesn't publish it directly. We approximate it from:

      defensive_actions = tackles + interceptions + fouls_committed (opp half)
      For a free public proxy we use total tackles + interceptions vs the
      opponent's possession metrics.

    Returns:
      ppda_proxy   — lower = more pressing (top pressers ~7-9, low blocks ~15-18)
      tkl_int      — tackles + interceptions per 90
      pressures_def_3rd_pct — % of pressures in own third (low-block signal)
      challenge_pct — successful tackle %

    Empty dict on failure.
    """
    df = _fetch_table(league, season, "defense")
    if df is None or df.empty:
        return {}
    squad = _resolve_team(team_name, df)
    if not squad:
        return {}
    row = df[df["Squad"] == squad].iloc[0]
    minutes = _safe_float(row.get("90s")) or 1.0
    tkl = _safe_float(row.get("Tkl")) or 0.0
    inter = _safe_float(row.get("Int")) or 0.0
    tkl_pct = _safe_float(row.get("Tkl%"))
    tkl_int_per_90 = (tkl + inter) / minutes if minutes else None
    # PPDA proxy: typical EPL opponent attempts ~370 passes/game. Defensive
    # actions in opp half ≈ 60% of (tkl+int). PPDA = passes_allowed / actions.
    actions_per_90 = (tkl + inter) / minutes if minutes else 0.0
    opp_actions_in_opp_half = actions_per_90 * 0.55
    ppda_proxy = (370.0 / opp_actions_in_opp_half) if opp_actions_in_opp_half > 0 else None
    return {
        "ppda_proxy":      round(ppda_proxy, 2) if ppda_proxy else None,
        "tkl_int_per_90":  round(tkl_int_per_90, 2) if tkl_int_per_90 else None,
        "challenge_pct":   tkl_pct,
        "source":          f"fbref/{league}/{season} defense",
    }


def fetch_team_possession(team_name: str, league: str, season: int) -> dict:
    """
    Field tilt + possession metrics from FBref possession table.

    Field tilt approximation: share of touches in attacking third
    (att_3rd_touches / total_touches). Top teams ~32-38%, bottom ~22-27%.

    Returns:
      att_3rd_touch_pct — % of touches in attacking third
      def_3rd_touch_pct — % of touches in defensive third
      possession_pct    — overall ball possession %
      progressive_passes — progressive passes per 90

    Empty dict on failure.
    """
    df = _fetch_table(league, season, "possession")
    if df is None or df.empty:
        return {}
    squad = _resolve_team(team_name, df)
    if not squad:
        return {}
    row = df[df["Squad"] == squad].iloc[0]
    poss = _safe_float(row.get("Poss"))
    touches = _safe_float(row.get("Touches")) or 0.0
    def_3rd = _safe_float(row.get("Def 3rd")) or 0.0
    att_3rd = _safe_float(row.get("Att 3rd")) or 0.0
    minutes = _safe_float(row.get("90s")) or 1.0
    prg = _safe_float(row.get("PrgP"))
    return {
        "possession_pct":     poss,
        "att_3rd_touch_pct":  round(att_3rd / touches * 100, 1) if touches else None,
        "def_3rd_touch_pct":  round(def_3rd / touches * 100, 1) if touches else None,
        "progressive_passes": round(prg / minutes, 2) if (prg and minutes) else None,
        "source":             f"fbref/{league}/{season} possession",
    }


def fetch_team_aerials(team_name: str, league: str, season: int) -> dict:
    """
    Aerial duel win % from FBref misc table — feeds the set-piece model.

    Returns aerials_won_pct, fouls_per_90, yellow_per_90.
    Empty dict on failure.
    """
    df = _fetch_table(league, season, "misc")
    if df is None or df.empty:
        return {}
    squad = _resolve_team(team_name, df)
    if not squad:
        return {}
    row = df[df["Squad"] == squad].iloc[0]
    won = _safe_float(row.get("Won"))
    lost = _safe_float(row.get("Lost"))
    minutes = _safe_float(row.get("90s")) or 1.0
    fouls = _safe_float(row.get("Fls"))
    yel = _safe_float(row.get("CrdY"))
    aerial_total = (won or 0) + (lost or 0)
    return {
        "aerials_won_pct": round(won / aerial_total * 100, 1) if aerial_total else None,
        "fouls_per_90":    round(fouls / minutes, 2) if (fouls and minutes) else None,
        "yellow_per_90":   round(yel   / minutes, 2) if (yel   and minutes) else None,
        "source":          f"fbref/{league}/{season} misc",
    }


def enrich_soccer_advanced(
    home_name: str,
    away_name: str,
    league: str,
    season: int,
) -> dict:
    """
    Combined FBref enrichment — pressing + possession + aerial + GK quality.

    Returns {
      "home": {psxg_ga_per_90, ppda_proxy, att_3rd_touch_pct, possession_pct,
               progressive_passes, aerials_won_pct, ...},
      "away": {... same ...},
    }
    Both empty dict on failure. Designed to layer on top of enrich_soccer_teams()
    in analyze_soccer.py — Understat provides xG, FBref provides process.
    """
    if league not in _LEAGUE_TO_FBREF:
        return {"home": {}, "away": {}}

    def _build(team: str) -> dict:
        out: dict = {}
        for fn in (fetch_team_gk, fetch_team_pressing, fetch_team_possession, fetch_team_aerials):
            try:
                d = fn(team, league, season)
                for k, v in d.items():
                    if k != "source" and v is not None:
                        out[k] = v
            except Exception:
                continue
        return out

    return {"home": _build(home_name), "away": _build(away_name)}
