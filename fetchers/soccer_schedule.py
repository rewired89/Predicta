"""
Soccer fixture + result fetcher for the auto-collection loop.

Uses ESPN's public site API (no key needed). Provides:
  - upcoming_fixtures(days_ahead=3): scheduled matches for the 5 big leagues
  - finished_result(home, away, date): 3-way result for a completed match

The five Understat-supported leagues map to ESPN slugs:
  EPL         → eng.1
  La_liga     → esp.1
  Bundesliga  → ger.1
  Serie_A     → ita.1
  Ligue_1     → fra.1
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

_LEAGUE_TO_ESPN: dict[str, str] = {
    "EPL":        "eng.1",
    "La_liga":    "esp.1",
    "Bundesliga": "ger.1",
    "Serie_A":    "ita.1",
    "Ligue_1":    "fra.1",
}

_ESPN_TO_LEAGUE = {v: k for k, v in _LEAGUE_TO_ESPN.items()}

_HEADERS = {"User-Agent": "Mozilla/5.0 Predicta soccer bot", "Accept": "application/json"}


def _get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    if not _HAS_HTTPX:
        return None
    try:
        with httpx.Client(timeout=15.0, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(url, params=params or {})
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def upcoming_fixtures(days_ahead: int = 3) -> list[dict]:
    """
    Return scheduled matches across the 5 leagues within the next `days_ahead`
    days. Each entry: {league, home, away, kickoff_utc, espn_id}. Empty list on
    total network failure.

    Uses ESPN scoreboard with date range in YYYYMMDD format.
    """
    today = datetime.now(timezone.utc).date()
    date_start = today.strftime("%Y%m%d")
    date_end   = (today + timedelta(days=days_ahead)).strftime("%Y%m%d")

    out: list[dict] = []
    for our_slug, espn_slug in _LEAGUE_TO_ESPN.items():
        data = _get(f"{ESPN_BASE}/{espn_slug}/scoreboard",
                    params={"dates": f"{date_start}-{date_end}"})
        if not data:
            continue
        for ev in data.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            status = comp.get("status", {}).get("type", {}).get("name", "")
            if status not in ("STATUS_SCHEDULED", "STATUS_PRE"):
                continue
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            out.append({
                "league":      our_slug,
                "home":        home.get("team", {}).get("displayName", ""),
                "away":        away.get("team", {}).get("displayName", ""),
                "kickoff_utc": ev.get("date", ""),
                "espn_id":     ev.get("id"),
            })
    return out


def finished_result(home: str, away: str, on_or_after: str) -> Optional[dict]:
    """
    Look up a match's final score by team names + earliest possible date.
    `on_or_after` is YYYY-MM-DD (typically the scheduled_at date).

    Returns {result: "a"|"b"|"draw", score_a, score_b, kickoff_utc} or None
    if no finished match is found in the ±3 day window.
    """
    home_lo = home.lower().strip()
    away_lo = away.lower().strip()
    try:
        base_date = datetime.strptime(on_or_after[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    window_start = (base_date - timedelta(days=1)).strftime("%Y%m%d")
    window_end   = (base_date + timedelta(days=3)).strftime("%Y%m%d")

    for espn_slug in _LEAGUE_TO_ESPN.values():
        data = _get(f"{ESPN_BASE}/{espn_slug}/scoreboard",
                    params={"dates": f"{window_start}-{window_end}"})
        if not data:
            continue
        for ev in data.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            status = comp.get("status", {}).get("type", {}).get("name", "")
            if status != "STATUS_FINAL":
                continue
            competitors = comp.get("competitors", [])
            h = next((c for c in competitors if c.get("homeAway") == "home"), None)
            a = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not h or not a:
                continue
            h_name = h.get("team", {}).get("displayName", "").lower()
            a_name = a.get("team", {}).get("displayName", "").lower()
            # Fuzzy substring match either direction
            home_match = (home_lo in h_name or h_name in home_lo
                          or _last_word_match(home_lo, h_name))
            away_match = (away_lo in a_name or a_name in away_lo
                          or _last_word_match(away_lo, a_name))
            if not (home_match and away_match):
                continue
            try:
                score_a = int(h.get("score", 0))
                score_b = int(a.get("score", 0))
            except (ValueError, TypeError):
                continue
            if score_a > score_b:
                result = "a"
            elif score_a < score_b:
                result = "b"
            else:
                result = "draw"
            return {
                "result":       result,
                "score_a":      score_a,
                "score_b":      score_b,
                "kickoff_utc":  ev.get("date", ""),
                "matched_home": h.get("team", {}).get("displayName", ""),
                "matched_away": a.get("team", {}).get("displayName", ""),
            }
    return None


def _last_word_match(a: str, b: str) -> bool:
    """Match the last word (surname / short name) between two team strings."""
    aw = a.split()
    bw = b.split()
    if not aw or not bw:
        return False
    return aw[-1] == bw[-1] and len(aw[-1]) >= 4
