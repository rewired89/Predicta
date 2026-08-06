"""
ESPN ATP/WTA + TheSportsDB tennis fetcher.
No API key required — same free-tier approach as soccer and baseball fetchers.

Surfaces inferred from tournament name. Serve stats fetched from ESPN athlete
statistics endpoint; falls back gracefully when unavailable.
"""
from __future__ import annotations
import difflib
import math
from datetime import datetime, timedelta
from typing import Optional

import httpx

from fetchers.data_cache import load_cached

ESPN_ATP_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis/atp"
ESPN_WTA_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis/wta"
TSDB_BASE     = "https://www.thesportsdb.com/api/v1/json/1"
TIMEOUT       = 15.0

# ATP tour averages (used to build quality indices)
ATP_AVG_FIRST_SERVE_PCT       = 0.62
ATP_AVG_FIRST_WON_PCT         = 0.73
ATP_AVG_SECOND_WON_PCT        = 0.54
ATP_AVG_BP_CONVERTED          = 0.40
ATP_AVG_RETURN_POINTS_WON_PCT = 0.32
ATP_AVG_ACES_PER_MATCH        = 7.0

# WTA tour averages. WTA_AVG_RETURN_POINTS_WON_PCT is an estimate (no published
# WTA figure found alongside the ATP one) — women's tour return games run stronger
# relative to serve than ATP's, so this is set a few points above the ATP average
# rather than reused as-is. Flagged uncalibrated like the rest of the tennis model.
WTA_AVG_FIRST_SERVE_PCT       = 0.60
WTA_AVG_FIRST_WON_PCT         = 0.68
WTA_AVG_SECOND_WON_PCT        = 0.51
WTA_AVG_BP_CONVERTED          = 0.42
WTA_AVG_RETURN_POINTS_WON_PCT = 0.37

# Surface inference from tournament/event name keywords
SURFACE_KEYWORDS: dict[str, list[str]] = {
    "clay":  [
        "clay", "roland garros", "french open", "monte-carlo", "monte carlo",
        "madrid", "rome", "barcelona", "hamburg", "estoril", "bucharest",
        "munich", "lyon", "geneva", "marrakech", "houston", "sao paulo",
    ],
    "grass": [
        "grass", "wimbledon", "queen's", "queens", "halle", "hertogenbosch",
        "eastbourne", "birmingham", "bad homburg", "newport", "nottingham",
        "mallorca",
    ],
    "hard":  [
        "hard", "australian open", "us open", "indian wells", "miami",
        "montreal", "toronto", "cincinnati", "paris", "bercy", "atp finals",
        "nitto", "united cup", "doha", "dubai", "acapulco", "rotterdam",
        "dubai", "adelaide", "brisbane", "washington", "los cabos",
    ],
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _espn_get(url: str, params: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS) as c:
            r = c.get(url, params=params or {})
            r.raise_for_status()
            return r.json()
    except Exception:
        if ESPN_ATP_BASE in url:
            return load_cached("tennis_atp.json") or {}
        if ESPN_WTA_BASE in url:
            return load_cached("tennis_wta.json") or {}
        return {}


def _tsdb_get(path: str, params: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(f"{TSDB_BASE}/{path}", params=params or {})
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


def _f(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── Surface helpers ───────────────────────────────────────────────────────────

def infer_surface(text: str) -> str:
    """Return 'clay', 'grass', or 'hard' from a tournament/event name."""
    t = (text or "").lower()
    for surface, keywords in SURFACE_KEYWORDS.items():
        if any(k in t for k in keywords):
            return surface
    return "hard"   # default — most common tour surface


def _elo_from_ranking(ranking: int) -> float:
    """Seed Glicko/Elo from ATP/WTA ranking so new players aren't stuck at 1500.
    Rank 1 ≈ 2400, Rank 50 ≈ 2000, Rank 200 ≈ 1700, Rank 500+ → 1500 default."""
    if ranking <= 0:
        return 1500.0
    return max(1300.0, 2400.0 - 400.0 * math.log10(max(1, ranking)))


def serve_quality_index(
    first_serve_pct: Optional[float],
    first_won_pct:   Optional[float],
    second_won_pct:  Optional[float],
    tour: str = "atp",
) -> float:
    """Composite serve quality index — 100 = tour average, higher is better.
    Based on first serve %, first serve won %, second serve won %."""
    avg_fs  = ATP_AVG_FIRST_SERVE_PCT if tour == "atp" else WTA_AVG_FIRST_SERVE_PCT
    avg_fw  = ATP_AVG_FIRST_WON_PCT   if tour == "atp" else WTA_AVG_FIRST_WON_PCT
    avg_sw  = ATP_AVG_SECOND_WON_PCT  if tour == "atp" else WTA_AVG_SECOND_WON_PCT
    scores  = []
    if first_serve_pct:
        scores.append(first_serve_pct / avg_fs)
    if first_won_pct:
        scores.append(first_won_pct / avg_fw)
    if second_won_pct:
        scores.append(second_won_pct / avg_sw)
    if not scores:
        return 100.0
    return round((sum(scores) / len(scores)) * 100, 1)


def return_quality_index(
    bp_converted_pct: Optional[float],
    return_points_won_pct: Optional[float] = None,
    tour: str = "atp",
) -> float:
    """Return quality index — 100 = tour average.
    Two-component blend: break point conversion % (60%) + return points won % (40%).
    Falls back to whichever component is available if only one is present."""
    avg_bp  = ATP_AVG_BP_CONVERTED          if tour == "atp" else WTA_AVG_BP_CONVERTED
    avg_rpw = ATP_AVG_RETURN_POINTS_WON_PCT if tour == "atp" else WTA_AVG_RETURN_POINTS_WON_PCT
    bp_score  = (bp_converted_pct / avg_bp) if bp_converted_pct else None
    rpw_score = (return_points_won_pct / avg_rpw) if return_points_won_pct else None
    if bp_score is None and rpw_score is None:
        return 100.0
    if rpw_score is None:
        return round(bp_score * 100, 1)
    if bp_score is None:
        return round(rpw_score * 100, 1)
    return round((bp_score * 0.6 + rpw_score * 0.4) * 100, 1)


# ── ESPN scoreboard — recent match history ────────────────────────────────────

def _parse_espn_tennis_events(data: dict, name_lower: str) -> list[dict]:
    """Extract completed tennis events for a named player from ESPN scoreboard."""
    results = []
    for ev in data.get("events") or []:
        comps = ev.get("competitions", [{}])
        comp  = comps[0] if comps else {}
        status = comp.get("status", {}).get("type", {}).get("name", "")
        if status != "STATUS_FINAL":
            continue
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue
        p1 = competitors[0]
        p2 = competitors[1]
        n1 = p1.get("athlete", {}).get("displayName", "") or p1.get("team", {}).get("displayName", "")
        n2 = p2.get("athlete", {}).get("displayName", "") or p2.get("team", {}).get("displayName", "")
        if name_lower not in n1.lower() and name_lower not in n2.lower():
            continue
        winner_id = comp.get("winnerId") or ""
        p1_won = str(p1.get("id", "")) == str(winner_id)
        tournament = ev.get("name", "") or ev.get("shortName", "")
        surface = infer_surface(tournament)
        results.append({
            "date":        (ev.get("date") or "")[:10],
            "player_a":   n1,
            "player_b":   n2,
            "winner":     n1 if p1_won else n2,
            "loser":      n2 if p1_won else n1,
            "tournament": tournament,
            "surface":    surface,
            "score":      comp.get("status", {}).get("type", {}).get("shortDetail", ""),
        })
    return results


def _espn_recent_matches(name: str, tour: str = "atp", days: int = 180) -> list[dict]:
    """Fetch last 5 completed matches for a player from ESPN ATP or WTA scoreboard."""
    base      = ESPN_ATP_BASE if tour == "atp" else ESPN_WTA_BASE
    name_low  = name.lower()
    today     = datetime.now()
    # Try two 90-day windows
    windows = [
        (today - timedelta(days=90),  today),
        (today - timedelta(days=days), today - timedelta(days=90)),
    ]
    found: list[dict] = []
    for start, end in windows:
        if len(found) >= 5:
            break
        date_str = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
        data = _espn_get(f"{base}/scoreboard", {"dates": date_str, "limit": 100})
        found.extend(_parse_espn_tennis_events(data, name_low))
    found.sort(key=lambda x: x["date"], reverse=True)
    # Deduplicate
    seen, unique = set(), []
    for m in found:
        key = (m["date"], m["player_a"], m["player_b"])
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique[:5]


# ── ESPN athlete search & stats ───────────────────────────────────────────────

def _search_espn_athlete(name: str, tour: str = "atp") -> Optional[dict]:
    """Find ESPN athlete ID and ranking by fuzzy-matching name."""
    base = ESPN_ATP_BASE if tour == "atp" else ESPN_WTA_BASE
    for page in range(1, 4):
        data = _espn_get(f"{base}/athletes", {"limit": 100, "page": page})
        athletes = data.get("athletes") or []
        if not athletes:
            break
        names = [a.get("displayName", "") for a in athletes]
        matches = difflib.get_close_matches(name, names, n=1, cutoff=0.55)
        if matches:
            for a in athletes:
                if a.get("displayName") == matches[0]:
                    return a
    return None


def _espn_athlete_stats(athlete_id: str, tour: str = "atp") -> dict:
    """Fetch season serve/return stats for an ESPN athlete. Returns empty dict on failure."""
    base = ESPN_ATP_BASE if tour == "atp" else ESPN_WTA_BASE
    data = _espn_get(f"{base}/athletes/{athlete_id}/statistics")
    stats: dict = {}
    for cat in data.get("splits", {}).get("categories") or []:
        for s in cat.get("stats") or []:
            name = (s.get("name") or s.get("displayName") or "").lower().replace(" ", "_")
            val  = _f(s.get("value"), default=None)
            if val is not None:
                stats[name] = val
    return stats


def _extract_serve_stats(raw: dict) -> dict:
    """Normalize ESPN stat keys to our serve/return stat dict."""
    def _pick(*keys):
        for k in keys:
            v = raw.get(k)
            if v is not None:
                return v
        return None

    return {
        "first_serve_pct":  _pick("first_serve_percentage", "firstservepct", "first_serve_pct"),
        "first_won_pct":    _pick("first_serve_points_won_pct", "firstservewon", "first_serve_won"),
        "second_won_pct":   _pick("second_serve_points_won_pct", "secondservewon", "second_serve_won"),
        "bp_converted_pct": _pick("break_points_converted_pct", "bpconverted", "break_point_pct"),
        "return_points_won_pct": _pick(
            "return_points_won_percentage", "returnpointswon", "return_points_won"
        ),
        "aces":             _pick("aces", "ace"),
        "double_faults":    _pick("double_faults", "double_fault", "doublefaults"),
        "ranking":          _pick("ranking", "rank", "current_rank"),
    }


# ── TheSportsDB player lookup ─────────────────────────────────────────────────

def _tsdb_search_player(name: str) -> Optional[dict]:
    data    = _tsdb_get("searchplayers.php", {"p": name})
    players = data.get("player") or []
    if not players:
        return None
    # Filter to tennis players if sport field is present
    tennis = [p for p in players if "tennis" in (p.get("strSport") or "").lower()]
    return tennis[0] if tennis else players[0]


def _tsdb_player_last5(player_id: str) -> list[dict]:
    """Retrieve last 5 completed matches for a TSDB player ID."""
    year  = datetime.now().year
    attempts = [
        ("eventsplayer.php", str(year)),
        ("eventsplayer.php", str(year - 1)),
        ("eventsseason.php", str(year)),
    ]
    for endpoint, season in attempts:
        try:
            data   = _tsdb_get(endpoint, {"id": player_id, "s": season})
            events = data.get("events") or []
            done   = [
                e for e in events
                if e.get("intHomeScore") is not None or e.get("strResult")
            ]
            done.sort(key=lambda x: x.get("dateEvent", ""), reverse=True)
            if done:
                return done[:5]
        except Exception:
            continue
    return []


def _normalize_tennis_results(raw: list[dict]) -> list[dict]:
    """Convert TheSportsDB tennis events to consistent format."""
    out = []
    for e in raw:
        surface = infer_surface(
            (e.get("strLeague") or "") + " " + (e.get("strVenue") or "")
        )
        home = e.get("strHomeTeam") or e.get("strEvent", "")
        away = e.get("strAwayTeam") or ""
        out.append({
            "date":       (e.get("dateEvent") or "")[:10],
            "player_a":   home,
            "player_b":   away,
            "winner":     home if (e.get("intHomeScore") or 0) > (e.get("intAwayScore") or 0) else away,
            "tournament": e.get("strLeague", ""),
            "surface":    surface,
        })
    return out


# ── Main context fetcher ──────────────────────────────────────────────────────

def fetch_tennis_context(
    player_a: str,
    player_b: str,
    surface:  str = "hard",
    tour:     str = "atp",
) -> dict:
    """
    Fetch all available context for a tennis match. Sources recorded for
    UI transparency. Falls back gracefully at every step.

    Returns dict with:
      player_a / player_b: {name, ranking, last5, serve_stats, serve_quality,
                             return_quality, surface_win_rate}
      h2h: list[dict]
      surface: str
      sources: list[dict]
    """
    sources: list[dict] = []
    result: dict = {
        "player_a": {"name": player_a},
        "player_b": {"name": player_b},
        "surface":  surface,
        "sources":  sources,
    }

    for key, name in [("player_a", player_a), ("player_b", player_b)]:
        player_data = result[key]

        # ── ESPN athlete search + stats ───────────────────────────────────────
        espn_id   = None
        ranking   = 0
        raw_stats = {}
        try:
            athlete = _search_espn_athlete(name, tour)
            if athlete:
                espn_id = str(athlete.get("id", ""))
                ranking = int(athlete.get("rank", 0) or 0)
                player_data["ranking"]     = ranking
                player_data["nationality"] = athlete.get("citizenship") or athlete.get("country", {}).get("displayName", "")
                sources.append({
                    "label":   f"{name} ESPN profile",
                    "url":     f"{ESPN_ATP_BASE if tour == 'atp' else ESPN_WTA_BASE}/athletes/{espn_id}",
                    "snippet": f"Rank #{ranking}",
                })
                if espn_id:
                    raw_stats = _espn_athlete_stats(espn_id, tour)
                    if raw_stats:
                        sources.append({
                            "label":   f"{name} serve stats (ESPN)",
                            "url":     f"{ESPN_ATP_BASE if tour == 'atp' else ESPN_WTA_BASE}/athletes/{espn_id}/statistics",
                            "snippet": f"{len(raw_stats)} stat keys retrieved",
                        })
        except Exception as exc:
            sources.append({"label": f"{name} ESPN", "url": "", "snippet": f"ERROR: {exc}"})

        # ── Serve / return stats ──────────────────────────────────────────────
        serve = _extract_serve_stats(raw_stats)
        player_data["serve_stats"]      = serve
        player_data["serve_quality"]    = serve_quality_index(
            serve.get("first_serve_pct"),
            serve.get("first_won_pct"),
            serve.get("second_won_pct"),
            tour,
        )
        player_data["return_quality"]   = return_quality_index(
            serve.get("bp_converted_pct"), serve.get("return_points_won_pct"), tour
        )

        # ── Recent form: ESPN scoreboard ──────────────────────────────────────
        last5: list[dict] = []
        try:
            last5 = _espn_recent_matches(name, tour)
            if last5:
                sources.append({
                    "label":   f"{name} recent matches (ESPN)",
                    "url":     f"{ESPN_ATP_BASE if tour == 'atp' else ESPN_WTA_BASE}/scoreboard",
                    "snippet": f"{len(last5)} matches found",
                })
        except Exception as exc:
            sources.append({"label": f"{name} ESPN scoreboard", "url": "", "snippet": f"ERROR: {exc}"})

        # ── Fallback: TheSportsDB ─────────────────────────────────────────────
        if not last5:
            try:
                tsdb_player = _tsdb_search_player(name)
                if tsdb_player:
                    pid = tsdb_player.get("idPlayer")
                    player_data["tsdb_id"] = pid
                    if not ranking:
                        player_data["nationality"] = tsdb_player.get("strNationality", "")
                    raw5 = _tsdb_player_last5(pid) if pid else []
                    if raw5:
                        last5 = _normalize_tennis_results(raw5)
                        sources.append({
                            "label":   f"{name} recent matches (TheSportsDB)",
                            "url":     f"{TSDB_BASE}/eventsplayer.php?id={pid}",
                            "snippet": f"{len(last5)} matches found",
                        })
                    else:
                        sources.append({
                            "label":   f"{name} TheSportsDB",
                            "url":     "",
                            "snippet": "No recent results found",
                        })
            except Exception as exc:
                sources.append({"label": f"{name} TheSportsDB", "url": "", "snippet": f"ERROR: {exc}"})

        player_data["last5"] = last5

        # ── Surface win rate from recent results ──────────────────────────────
        surface_wins   = sum(1 for m in last5 if m.get("surface") == surface and name.lower() in m.get("winner", "").lower())
        surface_played = sum(1 for m in last5 if m.get("surface") == surface)
        overall_wins   = sum(1 for m in last5 if name.lower() in m.get("winner", "").lower())
        player_data["surface_win_rate"] = surface_wins / surface_played if surface_played > 0 else None
        player_data["overall_win_rate"] = overall_wins / len(last5) if last5 else None

    # ── Head-to-head from TheSportsDB ────────────────────────────────────────
    h2h: list[dict] = []
    try:
        tid_a = result["player_a"].get("tsdb_id")
        tid_b = result["player_b"].get("tsdb_id")
        if tid_a and tid_b:
            data = _tsdb_get("eventsh2h.php", {"idTeam1": tid_a, "idTeam2": tid_b})
            events = data.get("results") or data.get("events") or []
            h2h = _normalize_tennis_results(events[:5])
            if h2h:
                sources.append({
                    "label":   f"H2H: {player_a} vs {player_b}",
                    "url":     f"{TSDB_BASE}/eventsh2h.php?idTeam1={tid_a}&idTeam2={tid_b}",
                    "snippet": f"{len(h2h)} previous meetings",
                })
    except Exception as exc:
        sources.append({"label": "Head-to-Head", "url": "", "snippet": f"ERROR: {exc}"})

    # Convert h2h list → summary dict expected by analyze_tennis
    wins_a = sum(1 for m in h2h if player_a.lower() in m.get("winner", "").lower())
    wins_b = sum(1 for m in h2h if player_b.lower() in m.get("winner", "").lower())
    result["h2h"] = {
        "wins_a":    wins_a,
        "wins_b":    wins_b,
        "advantage": "player_a" if wins_a > wins_b else ("player_b" if wins_b > wins_a else "even"),
        "matches":   h2h,
    }
    return result


# ── Result resolution (for tasks/tennis_auto.py) ───────────────────────────────

def finished_result(
    player_a: str,
    player_b: str,
    on_or_after: str,
    tour: str = "atp",
) -> Optional[dict]:
    """
    Look up a completed match's final result by player names + the date it
    was scheduled for. Mirrors fetchers/soccer_schedule.py / fetchers/rugby.py's
    finished_result() so tasks/tennis_auto.py's resolve step can follow the
    same pattern already used for soccer/rugby.

    `tour` should be matches.league lowercased ("atp"/"wta").

    ESPN's tennis scoreboard doesn't expose a simple match-level score (sets
    are nested linescores, not a top-level int), so score_a/score_b are
    returned as a 1/0 win-loss proxy rather than a real score — enough for
    record_outcome()'s tennis branch (which only needs score_a != score_b to
    trigger the Glicko-2 update) without fabricating a game/set count that
    was never actually parsed.

    Returns {result: 'a'|'b', score_a, score_b, kickoff_utc, matched_a,
    matched_b, score_detail} or None if no STATUS_FINAL match involving both
    players is found within the ±3 day window around on_or_after.
    """
    a_lo, b_lo = player_a.lower().strip(), player_b.lower().strip()
    try:
        base_date = datetime.strptime(on_or_after[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

    base = ESPN_ATP_BASE if tour.lower() == "atp" else ESPN_WTA_BASE
    window_start = (base_date - timedelta(days=1)).strftime("%Y%m%d")
    window_end = (base_date + timedelta(days=3)).strftime("%Y%m%d")
    data = _espn_get(f"{base}/scoreboard",
                      {"dates": f"{window_start}-{window_end}", "limit": 100})

    for ev in data.get("events") or []:
        comps = ev.get("competitions", [{}])
        comp = comps[0] if comps else {}
        status = comp.get("status", {}).get("type", {}).get("name", "")
        if status != "STATUS_FINAL":
            continue
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue
        p1, p2 = competitors[0], competitors[1]
        n1 = p1.get("athlete", {}).get("displayName", "") or p1.get("team", {}).get("displayName", "")
        n2 = p2.get("athlete", {}).get("displayName", "") or p2.get("team", {}).get("displayName", "")
        n1_lo, n2_lo = n1.lower(), n2.lower()

        if (a_lo in n1_lo or n1_lo in a_lo) and (b_lo in n2_lo or n2_lo in b_lo):
            a_id, b_id = p1.get("id"), p2.get("id")
        elif (a_lo in n2_lo or n2_lo in a_lo) and (b_lo in n1_lo or n1_lo in b_lo):
            a_id, b_id = p2.get("id"), p1.get("id")
        else:
            continue

        winner_id = str(comp.get("winnerId") or "")
        if not winner_id:
            continue
        a_won = str(a_id) == winner_id
        return {
            "result":       "a" if a_won else "b",
            "score_a":      1 if a_won else 0,
            "score_b":      0 if a_won else 1,
            "kickoff_utc":  ev.get("date", ""),
            "matched_a":    player_a,
            "matched_b":    player_b,
            "score_detail": comp.get("status", {}).get("type", {}).get("shortDetail", ""),
        }
    return None
