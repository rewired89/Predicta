"""
ESPN MLB data fetcher.
Uses the same unofficial ESPN API already used by fetchers/thesportsdb.py for soccer.
No API key or registration required.

When ESPN is unreachable (remote container egress policy), falls back to
pre-fetched data in data/live/ committed by the GitHub Actions workflow.
"""
from __future__ import annotations
import difflib
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from fetchers.data_cache import load_cached

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
TIMEOUT   = 15.0

LEAGUE_AVG_RUNS    = 4.5    # 2024 MLB runs per team per game
LEAGUE_AVG_FIP     = 4.00   # 2024 MLB FIP baseline
FIP_CONSTANT       = 3.20   # added to raw FIP to align with ERA scale
LEAGUE_AVG_WRC_PLUS = 100.0

# Multi-year park run factors by ESPN team abbreviation (1.0 = neutral).
# Coors Field is the most extreme — 1.38 per FanGraphs multi-year run factor.
PARK_FACTORS: dict[str, float] = {
    "COL": 1.38,
    "CIN": 1.10,
    "BOS": 1.08,
    "TEX": 1.07,
    "PHI": 1.05,
    "CHW": 1.04,
    "ATL": 1.03,
    "BAL": 1.02,
    "HOU": 1.01,
    "LAA": 1.00,
    "MIA": 0.99,
    "MIL": 1.00,
    "DET": 0.99,
    "PIT": 0.98,
    "MIN": 0.99,
    "KC":  0.98,
    "NYY": 0.99,
    "TOR": 0.98,
    "NYM": 0.97,
    "STL": 0.97,
    "CLE": 0.96,
    "WSH": 0.96,
    "TB":  0.95,
    "OAK": 0.96,
    "CHC": 1.02,
    "ARI": 1.03,
    "LAD": 0.96,
    "SD":  0.93,
    "SEA": 0.92,
    "SF":  0.91,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _espn_get(path: str, params: dict | None = None) -> dict:
    """GET from ESPN API; falls back to data/live/ cache on any failure."""
    url = f"{ESPN_BASE}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, params=params or {}, headers=_HEADERS)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return _cache_fallback(path)


def _cache_fallback(path: str) -> dict:
    """Return pre-fetched ESPN data from data/live/ when the live API is blocked."""
    if "/scoreboard" in path:
        return load_cached("mlb_scoreboard.json") or {}
    if "/standings" in path:
        return load_cached("mlb_standings.json") or {}
    if path.rstrip("/").endswith("/teams"):
        return load_cached("mlb_teams.json") or {}

    # /athletes/{id}/statistics  or  /athletes/{id}
    if "/athletes/" in path:
        aid = path.split("/athletes/")[1].split("/")[0]
        all_pitchers: dict = load_cached("mlb_pitcher_stats.json") or {}
        pitcher = all_pitchers.get(aid, {})
        if "/statistics" in path:
            return pitcher.get("statistics", {})
        return pitcher.get("profile", {})

    # /teams/{id}/statistics
    if "/teams/" in path and "/statistics" in path:
        tid = path.split("/teams/")[1].split("/")[0]
        all_team_stats: dict = load_cached("mlb_team_stats.json") or {}
        return all_team_stats.get(tid, {})

    return {}


def _f(val, default: float = 0.0) -> float:
    """Safe float — handles ESPN strings like '.265' and None."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _stat(stats_list: list, *names: str, default: float = 0.0) -> float:
    """Extract value from ESPN stats array by name (tries multiple aliases)."""
    lookup = {s.get("name", "").lower(): _f(s.get("value"), default) for s in stats_list}
    for name in names:
        val = lookup.get(name.lower())
        if val is not None:
            return val
    return default


def _extract_category(data: dict, keywords: list) -> list:
    """
    Extract a stats list from an ESPN team statistics response.
    Handles two formats:
      New: data["results"]["stats"]["categories"][n]["stats"]
      Old: data["statistics"][n]["stats"]  or  data["splits"]["categories"][n]["stats"]
    """
    # New ESPN format (seen in /teams/{id}/statistics as of 2026)
    for cat in data.get("results", {}).get("stats", {}).get("categories", []):
        if any(kw in cat.get("name", "").lower() for kw in keywords):
            return cat.get("stats", [])
    # Old flat format
    for section in data.get("statistics", []):
        if isinstance(section, dict) and any(kw in section.get("name", "").lower() for kw in keywords):
            return section.get("stats", [])
    # Old splits format
    for cat in data.get("splits", {}).get("categories", []):
        if any(kw in cat.get("name", "").lower() for kw in keywords):
            return cat.get("stats", [])
    return []


def _ip_from_espn(ip_val) -> float:
    """
    ESPN returns IP as a decimal where the tenths digit represents outs (0–2).
    e.g. 95.1 means 95 innings + 1 out = 95.333 decimal innings.
    If already a large decimal like 95.333, use directly.
    """
    try:
        v = float(ip_val or 0)
        tenths = round((v % 1) * 10)
        if tenths in (1, 2):          # MLB fractional format (95.1 or 95.2)
            return int(v) + tenths / 3
        return v                       # already decimal innings
    except (ValueError, TypeError):
        return 0.0


def compute_fip(stats: list) -> Optional[float]:
    """
    Compute FIP from ESPN pitching stats list.
    FIP = ((13×HR) + (3×BB) - (2×K)) / IP + FIP_constant
    Note: HBP usually absent from ESPN; excluded (minor effect).
    """
    hr  = _stat(stats, "homeRunsAllowed", "homeRuns", "hr")
    bb  = _stat(stats, "walks", "baseOnBalls", "bb")
    k   = _stat(stats, "strikeouts", "so", "k")
    ip  = _ip_from_espn(_stat(stats, "inningsPitched", "ip") or
                        _stat(stats, "inningsPitchedFull"))
    if ip < 1:
        return None
    return round(((13 * hr) + (3 * bb) - (2 * k)) / ip + FIP_CONSTANT, 2)


# ── Team list ─────────────────────────────────────────────────────────────────

def _all_teams() -> list[dict]:
    data = _espn_get("/teams")
    teams = []
    for sport in data.get("sports", []):
        for league in sport.get("leagues", []):
            for entry in league.get("teams", []):
                teams.append(entry.get("team", entry))
    return teams


def _match_team(name: str, teams: list[dict]) -> Optional[dict]:
    """Fuzzy-match a user-supplied name to an ESPN MLB team object."""
    candidates: dict[str, dict] = {}
    for t in teams:
        for key in ("displayName", "shortDisplayName", "abbreviation",
                    "location", "name", "nickname"):
            val = t.get(key, "")
            if val:
                candidates[val.lower()] = t

    q = name.lower().strip()
    if q in candidates:
        return candidates[q]

    close = difflib.get_close_matches(q, candidates.keys(), n=1, cutoff=0.45)
    if close:
        return candidates[close[0]]

    for k, t in candidates.items():
        if q in k or k in q:
            return t

    return None


def _match_teams(name: str, teams: list[dict], limit: int = 3) -> list[dict]:
    """
    Like _match_team but returns multiple plausible candidates, ranked by
    closeness. Needed because short city hints are genuinely ambiguous in
    ESPN's data — "LA" fuzzy-matches both LAD (Dodgers) and LAA (Angels)
    equally well, "NY" matches both NYY and NYM, "CHI" matches CHC and CWS.
    A single best-guess match can silently pick the wrong team and report
    "player not found" even though the player exists on the OTHER team that
    shares the hint. Callers should try each candidate's roster in turn.
    """
    candidates: dict[str, dict] = {}
    for t in teams:
        for key in ("displayName", "shortDisplayName", "abbreviation",
                    "location", "name", "nickname"):
            val = t.get(key, "")
            if val:
                candidates.setdefault(val.lower(), t)

    q = name.lower().strip()
    if q in candidates:
        return [candidates[q]]

    close = difflib.get_close_matches(q, candidates.keys(), n=limit, cutoff=0.45)
    if close:
        seen_ids: set = set()
        out = []
        for k in close:
            t = candidates[k]
            tid = t.get("id")
            if tid not in seen_ids:
                seen_ids.add(tid)
                out.append(t)
        return out

    out = []
    seen_ids = set()
    for k, t in candidates.items():
        if (q in k or k in q) and t.get("id") not in seen_ids:
            seen_ids.add(t.get("id"))
            out.append(t)
    return out


# ── Standings ─────────────────────────────────────────────────────────────────

def _get_all_records() -> dict[str, dict]:
    """
    Fetch all team W-L records.
    Primary: ESPN /standings endpoint.
    Fallback: parse recordSummary ("48-31") from cached team stats.
    Returns {team_id: {wins, losses, games_played, win_pct, run_differential}}.
    """
    data = _espn_get("/standings")
    records: dict[str, dict] = {}

    def _walk(node):
        if isinstance(node, dict):
            for entry in node.get("standings", {}).get("entries", []):
                tid = str(entry.get("team", {}).get("id", ""))
                if not tid:
                    continue
                stats = entry.get("stats", [])
                wins   = int(_stat(stats, "wins"))
                losses = int(_stat(stats, "losses"))
                gp = wins + losses
                rd = int(_stat(stats, "pointDifferential", "runDifferential"))
                records[tid] = {
                    "wins":             wins,
                    "losses":           losses,
                    "games_played":     gp,
                    "win_pct":          round(wins / gp, 3) if gp > 0 else 0.5,
                    "run_differential": rd,
                }
            for child in node.get("children", []):
                _walk(child)

    _walk(data)

    # Fallback: extract records from team stats cache when standings returns nothing useful
    if not records:
        all_team_stats: dict = load_cached("mlb_team_stats.json") or {}
        for tid, tdata in all_team_stats.items():
            rec_str = tdata.get("team", {}).get("recordSummary", "")
            if rec_str and "-" in rec_str:
                try:
                    w, l = map(int, rec_str.split("-"))
                    gp = w + l
                    records[tid] = {
                        "wins":             w,
                        "losses":           l,
                        "games_played":     gp,
                        "win_pct":          round(w / gp, 3) if gp > 0 else 0.5,
                        "run_differential": 0,
                    }
                except ValueError:
                    pass

    return records


# ── Scoreboard / game finder ──────────────────────────────────────────────────

def _get_scoreboard(date_str: str) -> list[dict]:
    """Get all MLB events for a given date (YYYY-MM-DD)."""
    date_compact = date_str.replace("-", "")
    data = _espn_get("/scoreboard", {"dates": date_compact})
    return data.get("events", [])


def _find_game(events: list[dict], team_a_id: str, team_b_id: str
               ) -> tuple[Optional[dict], Optional[dict]]:
    """
    Returns (event, competition) for the game between the two teams,
    or (None, None) if not found.
    """
    for event in events:
        for comp in event.get("competitions", []):
            ids = {str(c.get("team", {}).get("id", ""))
                   for c in comp.get("competitors", [])}
            if team_a_id in ids and team_b_id in ids:
                return event, comp
    return None, None


def _extract_probable(comp: dict, side: str) -> Optional[dict]:
    """Extract probable pitcher {id, name, era, stats_list} for home or away."""
    for c in comp.get("competitors", []):
        if c.get("homeAway", "").lower() != side:
            continue
        probables = c.get("probables", [])
        if not probables:
            return None
        p = probables[0]
        athlete = p.get("athlete", {})
        stats   = p.get("statistics", [])
        era     = _f(next(
            (s.get("value") for s in stats
             if s.get("name", "").upper() == "ERA"), None
        ))
        return {
            "id":   str(athlete.get("id", "") or p.get("id", "") or p.get("athleteId", "")),
            "name": athlete.get("displayName") or athlete.get("shortName") or "TBD",
            "era":  era or LEAGUE_AVG_FIP,
            "stats_from_scoreboard": stats,
        }
    return None


# ── Pitcher season stats ──────────────────────────────────────────────────────

def _get_pitcher_stats(athlete_id: str) -> dict:
    """
    Fetch detailed season pitching stats for a player from ESPN.
    Returns ERA, FIP (computed), WHIP, K/9, BB/9, IP, GS.
    """
    if not athlete_id:
        return {}
    data = _espn_get(f"/athletes/{athlete_id}/statistics")

    # ESPN wraps stats in various structures — try both
    stats: list = []
    for section in data.get("statistics", []):
        if isinstance(section, dict):
            inner = section.get("stats", section.get("statistics", []))
            if inner:
                stats = inner
                break
        # Sometimes it's a flat list
        if isinstance(section, list):
            stats = section
            break

    if not stats:
        # Fall back to splits structure
        splits = data.get("splits", {}).get("categories", [])
        for cat in splits:
            if "pitch" in cat.get("name", "").lower():
                stats = cat.get("stats", [])
                break

    if not stats:
        return {}

    era   = _stat(stats, "ERA", "era", default=LEAGUE_AVG_FIP)
    whip  = _stat(stats, "WHIP", "whip")
    k9    = _stat(stats, "strikeoutsPerNineInnings", "K9", "so9")
    bb9   = _stat(stats, "walksPerNineInnings", "BB9", "bb9")
    gs    = int(_stat(stats, "gamesStarted", "gs"))
    ip    = _ip_from_espn(_stat(stats, "inningsPitched", "ip"))
    fip   = compute_fip(stats)

    if not k9 and ip > 0:
        k = _stat(stats, "strikeouts", "so", "k")
        k9 = round(k / ip * 9, 2) if ip > 0 else 0.0
    if not bb9 and ip > 0:
        bb = _stat(stats, "walks", "baseOnBalls", "bb")
        bb9 = round(bb / ip * 9, 2) if ip > 0 else 0.0

    return {
        "era":             era,
        "fip":             fip if fip is not None else era,
        "whip":            whip,
        "k9":              k9,
        "bb9":             bb9,
        "innings_pitched": round(ip, 1),
        "games_started":   gs,
    }


# ── Team pitching stats (for bullpen FIP derivation) ─────────────────────────

def _get_team_pitching(team_id: str) -> dict:
    """
    Fetch team-level pitching stats from ESPN /teams/{id}/statistics.
    Returns ERA, WHIP, K/9 as proxies for overall pitching quality.
    Used to derive bullpen ERA = (team_ERA × 9 - starter_FIP × 5) / 4.
    Handles both old and new ESPN response formats.
    """
    data = _espn_get(f"/teams/{team_id}/statistics")
    pitch_stats  = _extract_category(data, ["pitching", "pitch"])
    field_stats  = _extract_category(data, ["fielding", "field"])

    if not pitch_stats:
        return {"era": LEAGUE_AVG_FIP, "whip": 1.30, "k9": 8.5}

    # In the new format ERA/WHIP aren't stored directly — calculate from components
    era  = _stat(pitch_stats, "ERA",  "era",  default=0.0)
    whip = _stat(pitch_stats, "WHIP", "whip", default=0.0)
    k9   = _stat(pitch_stats, "strikeoutsPerNineInnings", "K9", "so9", default=0.0)

    if era == 0.0 or whip == 0.0:
        # New format: calculate ERA/WHIP from components
        er   = _stat(pitch_stats, "earnedRuns")
        hits = _stat(pitch_stats, "hits")
        bb   = _stat(pitch_stats, "walks")
        # IP from fielding category (fullInningsPlayed = defensive innings faced = IP)
        ip   = _stat(field_stats, "fullInningsPlayed") if field_stats else 0.0
        if ip > 0:
            era  = round((er * 9) / ip, 2)
            whip = round((hits + bb) / ip, 3)
        if not k9:
            k = _stat(pitch_stats, "strikeouts")
            k9 = round((k * 9) / ip, 2) if ip > 0 else 8.5

    return {
        "era":  era  if era  > 0 else LEAGUE_AVG_FIP,
        "whip": whip if whip > 0 else 1.30,
        "k9":   k9   if k9  > 0 else 8.5,
    }


def _get_pitcher_handedness(athlete_id: str) -> str:
    """
    Fetch pitcher throwing hand from ESPN athlete profile.
    Returns 'R' or 'L'; defaults to 'R' on any failure or missing data.
    """
    if not athlete_id:
        return "R"
    data = _espn_get(f"/athletes/{athlete_id}")
    athlete = data.get("athlete", data)
    hand_obj = athlete.get("throws") or athlete.get("hand") or {}
    abbr = (hand_obj.get("abbreviation") or "").strip().upper()
    return abbr if abbr in ("L", "R") else "R"


# ── Team batting stats ────────────────────────────────────────────────────────

def _get_team_hitting(team_id: str) -> dict:
    """
    Fetch team batting season stats. Returns wRC+ (OPS-derived), OPS, runs/game.
    Handles both old and new ESPN /teams/{id}/statistics response formats.
    """
    data = _espn_get(f"/teams/{team_id}/statistics")
    stats = _extract_category(data, ["batting", "hitting", "offens"])
    if not stats:
        stats = data.get("stats", [])

    # ESPN field name varies by format: new=OPS/onBasePct/slugAvg, old=ops/onBasePercentage/sluggingPercentage
    ops  = _stat(stats, "OPS", "ops", "onBasePlusSlugging")
    avg  = _stat(stats, "avg", "battingAverage", "average")
    obp  = _stat(stats, "onBasePct", "onBasePercentage", "obp")
    slg  = _stat(stats, "slugAvg", "sluggingPercentage", "slg")
    runs = _stat(stats, "runs", "runsScored", "r")
    gp   = _stat(stats, "teamGamesPlayed", "gamesPlayed", "gp") or 1
    k_n  = _stat(stats, "strikeouts", "so", "k")
    bb_n = _stat(stats, "walks", "baseOnBalls", "bb")
    pa   = _stat(stats, "plateAppearances", "pa") or max(gp * 36, 1)

    # wRC+ approximation: 2×OBP+SLG correlates ~0.97 with true wRC+ (vs ~0.93 for raw OPS).
    # League avg 2×OBP+SLG ≈ 2×0.315+0.415 = 1.045 (2025-26 MLB).
    # Falls back to OPS/0.730 when OBP or SLG are missing.
    if obp > 0 and slg > 0:
        wrc_plus = round(((2 * obp + slg) / 1.045) * 100)
    elif ops > 0:
        wrc_plus = round((ops / 0.730) * 100)
    else:
        wrc_plus = 100

    return {
        "ops":           round(ops, 3),
        "avg":           round(avg, 3),
        "obp":           round(obp, 3),
        "slg":           round(slg, 3),
        "k_pct":         round(k_n / pa, 3) if pa > 0 else 0.0,
        "bb_pct":        round(bb_n / pa, 3) if pa > 0 else 0.0,
        "runs_per_game": round(runs / gp, 2) if gp > 0 else 0.0,
        "wrc_plus":      wrc_plus,
    }


# ── Individual batter lookup (Player Impact hitter fallback) ─────────────────

def _find_roster_athlete(team_id: str, name: str) -> Optional[dict]:
    """Fuzzy-match a player name within a team's roster. Returns {id, name, pos, bats}."""
    data = _espn_get(f"/teams/{team_id}/roster")
    groups = data.get("athletes", [])
    flat: list = []
    for g in groups:
        if isinstance(g, dict) and "items" in g:
            flat.extend(g["items"])
        elif isinstance(g, dict):
            flat.append(g)

    q = name.lower().strip()
    by_name = {(a.get("displayName") or a.get("fullName") or ""): a for a in flat}
    best = next((a for n, a in by_name.items() if n.lower() == q), None)
    if not best:
        close = difflib.get_close_matches(q, [n.lower() for n in by_name], n=1, cutoff=0.6)
        if close:
            best = next(a for n, a in by_name.items() if n.lower() == close[0])
    if not best:
        return None

    pos = (best.get("position") or {}).get("abbreviation", "")
    bats_abbr = (best.get("bats") or {}).get("abbreviation", "R").strip().upper()
    return {
        "id":   str(best.get("id", "")),
        "name": best.get("displayName") or best.get("fullName") or name,
        "pos":  pos,
        "bats": bats_abbr if bats_abbr in ("L", "R", "S") else "R",
    }


def _get_batter_stats(athlete_id: str) -> dict:
    """Fetch season batting stats for one player. Returns AVG/OPS/HR/SB and derived wRC+."""
    if not athlete_id:
        return {}
    data = _espn_get(f"/athletes/{athlete_id}/statistics")

    stats: list = []
    for section in data.get("statistics", []):
        if isinstance(section, dict):
            inner = section.get("stats", section.get("statistics", []))
            if inner:
                stats = inner
                break
        if isinstance(section, list):
            stats = section
            break
    if not stats:
        for cat in data.get("splits", {}).get("categories", []):
            cat_name = cat.get("name", "").lower()
            if "batt" in cat_name or "hit" in cat_name:
                stats = cat.get("stats", [])
                break
    if not stats:
        return {}

    avg = _stat(stats, "avg", "battingAverage", "average")
    obp = _stat(stats, "onBasePct", "onBasePercentage", "obp")
    slg = _stat(stats, "slugAvg", "sluggingPercentage", "slg")
    ops = _stat(stats, "OPS", "ops", "onBasePlusSlugging")
    hr  = int(_stat(stats, "homeRuns", "hr"))
    sb  = int(_stat(stats, "stolenBases", "sb"))
    gp  = int(_stat(stats, "gamesPlayed", "GP"))

    if obp > 0 and slg > 0:
        wrc_plus = round(((2 * obp + slg) / 1.045) * 100)
        ops = ops or round(obp + slg, 3)
    elif ops > 0:
        wrc_plus = round((ops / 0.730) * 100)
    else:
        return {}

    return {"avg": round(avg, 3), "ops": round(ops, 3), "hr": hr, "sb": sb, "wrc_plus": wrc_plus,
            "games_played": gp}


def lookup_batter(name: str, team_abbr: Optional[str] = None) -> dict:
    """
    Live ESPN fallback for hitters not in the Player Impact hardcoded table.
    ESPN has no public cross-league name search, so this requires a team hint
    (abbreviation, city, or nickname) to find the athlete on that team's roster.

    Tries every plausible team match, not just the single best guess — a short
    hint like "LA" fuzzy-matches both LAD and LAA equally well, so picking only
    one risks a false "not found" when the player is actually on the other
    team sharing that hint. Fixed 2026-07-05 (was: "Andy Pages" + "LA" failed
    because the fuzzy match could land on either LA team and only one root was
    ever tried).

    Returns {} if team_abbr is missing, no team matches at all, or the player
    isn't found on any matched team's roster.
    """
    if not name or not team_abbr:
        return {}
    candidates = _match_teams(team_abbr, _all_teams())
    if not candidates:
        return {}

    team = athlete = None
    for t in candidates:
        a = _find_roster_athlete(str(t.get("id", "")), name)
        if a:
            team, athlete = t, a
            break
    if not athlete:
        return {}
    batting = _get_batter_stats(athlete["id"])
    if not batting:
        return {}
    return {
        "display_name": athlete["name"],
        "team":         (team.get("abbreviation") or team_abbr).upper(),
        "bats":         athlete["bats"],
        "pos":          athlete["pos"],
        "war":          None,
        **batting,
    }


# ── Starter builder ───────────────────────────────────────────────────────────

def fetch_team_injuries(team_id: str) -> dict[str, dict]:
    """
    Fetch a team's current injury report from ESPN. Returns {athlete_id: {status,
    detail}} keyed by athlete id string. Fails open (returns {}) on any error —
    an ESPN injuries-endpoint hiccup should mean "no injury info available for
    this game," not "block/downgrade the whole slate." This is a data-quality
    gate, not a core input, so absence of data must never cascade into a
    pipeline failure (see 2026-07-05 Anthropic-outage incident for why that
    matters here).
    """
    if not team_id:
        return {}
    try:
        data = _espn_get(f"/teams/{team_id}/injuries")
    except Exception:
        return {}
    out: dict[str, dict] = {}
    items = data.get("injuries", data.get("items", []))
    if isinstance(items, dict):
        items = items.get("items", [])
    for entry in items or []:
        athlete = entry.get("athlete", {})
        aid = str(athlete.get("id") or entry.get("id") or "")
        if not aid:
            continue
        status = (entry.get("status") or entry.get("type", {}).get("description")
                  or "").strip()
        detail = entry.get("details", {}) if isinstance(entry.get("details"), dict) else {}
        out[aid] = {
            "status": status,
            "detail": detail.get("detail") or entry.get("longComment", ""),
        }
    return out


def _build_starter(probable: Optional[dict], team_id: str = "") -> dict:
    """Build complete starter dict, fetching detailed stats and handedness if we have an athlete ID."""
    _default = {
        "name": "TBD",
        "fip":  LEAGUE_AVG_FIP,
        "era":  LEAGUE_AVG_FIP,
        "whip": 0.0, "k9": 0.0, "bb9": 0.0,
        "innings_pitched": 0, "games_started": 0, "recent_games": [],
        "throws": "R",
        "injury_status": None,
    }
    if not probable:
        return _default

    result = {**_default,
              "name": probable["name"],
              "era":  probable["era"],
              "fip":  probable["era"]}

    athlete_id = probable.get("id", "")
    if athlete_id:
        detailed = _get_pitcher_stats(athlete_id)
        if detailed:
            result.update({
                "fip":             detailed.get("fip") or probable["era"],
                "era":             detailed.get("era") or probable["era"],
                "whip":            detailed.get("whip", 0.0),
                "k9":              detailed.get("k9", 0.0),
                "bb9":             detailed.get("bb9", 0.0),
                "innings_pitched": detailed.get("innings_pitched", 0),
                "games_started":   detailed.get("games_started", 0),
            })
        result["throws"] = _get_pitcher_handedness(athlete_id)

    # A probable starter appearing on the team's injury report AT ALL — any
    # status (Out/DTD/Questionable/IL-nn) — is inherently notable, unlike a
    # position player's routine DTD listing. Flag it; analyze_baseball.py
    # downgrades data_confidence to "low" when this is set, per Kimi's
    # recommendation: don't try to guess a replacement's stats, just flag
    # the prediction as unreliable.
    if athlete_id and team_id:
        injuries = fetch_team_injuries(team_id)
        info = injuries.get(str(athlete_id))
        if info:
            result["injury_status"] = info.get("status") or "Listed"
            result["injury_detail"] = info.get("detail", "")

    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_baseball_context(
    team_a: str,
    team_b: str,
    game_date: Optional[str] = None,
) -> dict:
    """
    Fetch all data for a baseball matchup from ESPN:
    team records, hitting stats, probable starters with FIP, park factor.
    Returns a structured dict compatible with analyze_baseball.py.
    """
    if not game_date:
        game_date = datetime.now(timezone.utc).date().isoformat()

    sources: list[dict] = []
    all_teams = _all_teams()

    if not all_teams:
        return {
            "error": (
                "ESPN MLB API unreachable and no cached data found. "
                "Run the GitHub Actions 'Fetch Live Sports Data' workflow to populate data/live/."
            ),
            "sources": sources,
        }

    mlb_a = _match_team(team_a, all_teams)
    mlb_b = _match_team(team_b, all_teams)

    if not mlb_a:
        return {"error": f"Could not find MLB team matching '{team_a}'", "sources": sources}
    if not mlb_b:
        return {"error": f"Could not find MLB team matching '{team_b}'", "sources": sources}

    id_a    = str(mlb_a["id"])
    id_b    = str(mlb_b["id"])
    name_a  = mlb_a.get("displayName", team_a)
    name_b  = mlb_b.get("displayName", team_b)
    abbr_a  = mlb_a.get("abbreviation", "")
    abbr_b  = mlb_b.get("abbreviation", "")

    sources.append({
        "label":   "ESPN MLB API – Teams",
        "url":     f"{ESPN_BASE}/teams",
        "snippet": f"Matched '{team_a}' → {name_a} ({abbr_a}),  '{team_b}' → {name_b} ({abbr_b})",
    })

    # ── Records ────────────────────────────────────────────────────────────
    all_records = _get_all_records()
    record_a = all_records.get(id_a,
               {"wins": 0, "losses": 0, "games_played": 0, "win_pct": 0.5, "run_differential": 0})
    record_b = all_records.get(id_b,
               {"wins": 0, "losses": 0, "games_played": 0, "win_pct": 0.5, "run_differential": 0})

    # ── Game / venue / park factor ─────────────────────────────────────────
    events = _get_scoreboard(game_date)
    event, comp = _find_game(events, id_a, id_b)

    venue        = ""
    park_factor  = 1.00
    home_team_id: Optional[str] = None

    if event and comp:
        venue = comp.get("venue", {}).get("fullName", "")
        for c in comp.get("competitors", []):
            if c.get("homeAway", "").lower() == "home":
                home_team_id = str(c.get("team", {}).get("id", ""))
                break
        home_abbr   = abbr_a if home_team_id == id_a else abbr_b
        park_factor = PARK_FACTORS.get(home_abbr, 1.00)
        sources.append({
            "label":   f"ESPN MLB API – Schedule {game_date}",
            "url":     f"{ESPN_BASE}/scoreboard?dates={game_date.replace('-','')}",
            "snippet": f"Game found: {name_a} vs {name_b} at {venue} (park factor {park_factor})",
        })
    else:
        home_team_id = id_a          # default team_a as home
        park_factor  = PARK_FACTORS.get(abbr_a, 1.00)
        sources.append({
            "label":   f"ESPN MLB API – Schedule {game_date}",
            "url":     f"{ESPN_BASE}/scoreboard?dates={game_date.replace('-','')}",
            "snippet": f"No game found on {game_date}. Assuming {name_a} is home (park factor {park_factor}).",
        })

    is_home_a = (home_team_id == id_a)

    # ── Probable starters ──────────────────────────────────────────────────
    if comp:
        side_a = "home" if is_home_a else "away"
        side_b = "away" if is_home_a else "home"
        starter_a = _build_starter(_extract_probable(comp, side_a), id_a)
        starter_b = _build_starter(_extract_probable(comp, side_b), id_b)
    else:
        starter_a = _build_starter(None)
        starter_b = _build_starter(None)

    sources.append({
        "label":   "ESPN MLB API – Probable Pitchers",
        "url":     f"{ESPN_BASE}/scoreboard",
        "snippet": (
            f"{name_a} starter: {starter_a['name']} "
            f"(FIP {starter_a['fip']}, ERA {starter_a['era']}) | "
            f"{name_b} starter: {starter_b['name']} "
            f"(FIP {starter_b['fip']}, ERA {starter_b['era']})"
        ),
    })

    # ── Team hitting + pitching ────────────────────────────────────────────
    hitting_a  = _get_team_hitting(id_a)
    hitting_b  = _get_team_hitting(id_b)
    pitching_a = _get_team_pitching(id_a)
    pitching_b = _get_team_pitching(id_b)

    sources.append({
        "label":   "ESPN MLB API – Team Stats",
        "url":     f"{ESPN_BASE}/teams/statistics",
        "snippet": (
            f"{name_a}: wRC+ {hitting_a.get('wrc_plus','?')}, OPS {hitting_a.get('ops','?')}, "
            f"team ERA {pitching_a.get('era','?')} | "
            f"{name_b}: wRC+ {hitting_b.get('wrc_plus','?')}, OPS {hitting_b.get('ops','?')}, "
            f"team ERA {pitching_b.get('era','?')}"
        ),
    })

    return {
        "team_a": {
            "name":          name_a,
            "id":            id_a,
            "abbreviation":  abbr_a,
            "is_home":       is_home_a,
            "record":        record_a,
            "hitting":       hitting_a,
            "team_pitching": pitching_a,
            "starter":       starter_a,
        },
        "team_b": {
            "name":          name_b,
            "id":            id_b,
            "abbreviation":  abbr_b,
            "is_home":       not is_home_a,
            "record":        record_b,
            "hitting":       hitting_b,
            "team_pitching": pitching_b,
            "starter":       starter_b,
        },
        "game": {
            "game_pk":    event.get("id") if event else None,
            "date":       game_date,
            "venue":      venue,
            "park_factor": park_factor,
            "home_team":  name_a if is_home_a else name_b,
            "away_team":  name_b if is_home_a else name_a,
        },
        "sources": sources,
    }
