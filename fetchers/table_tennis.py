"""
TheSportsDB + RapidAPI ITTF table tennis fetcher.
No paid API key required — uses TSDB free tier (key=1).

Key signals:
  Attack Quality Index (AQI)  — composite attack win rate + 3rd ball attack rate
  Return Quality Index (RQI)  — return point win rate
  Recent form                 — last-10 win rate
  ITTF ranking                → Glicko-2 seed
  H2H                         — style matchups are highly predictive in TT
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import Optional

import httpx

TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/1"
TIMEOUT   = 15.0

# Tour averages (used for index normalisation)
# Attack win rate tour averages (approximate WTT/ITTF data)
AVG_ATTACK_WIN_RATE    = 0.55   # % of rallies won when attacking
AVG_3RD_BALL_WIN_RATE  = 0.60   # % of points won on serve+3rd ball
AVG_RETURN_WIN_RATE    = 0.45   # % of return points won
AVG_LONG_RALLY_WIN     = 0.50   # win % on 5+ shot rallies (neutral baseline)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _tsdb_get(path: str, params: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS) as c:
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


def _elo_from_ranking(ranking: int) -> float:
    """Seed Glicko-2 from ITTF ranking. Rank 1 ≈ 2400, Rank 50 ≈ 2000, Rank 500+ → 1500."""
    if ranking <= 0:
        return 1500.0
    return max(1300.0, 2400.0 - 400.0 * math.log10(max(1, ranking)))


def attack_quality_index(
    attack_win_rate: Optional[float],
    third_ball_win_rate: Optional[float],
) -> float:
    """
    Composite AQI — 100 = tour average, higher is better attacker.
    If only one component available, uses that alone.
    """
    scores = []
    if attack_win_rate is not None:
        scores.append(attack_win_rate / AVG_ATTACK_WIN_RATE)
    if third_ball_win_rate is not None:
        scores.append(third_ball_win_rate / AVG_3RD_BALL_WIN_RATE)
    if not scores:
        return 100.0
    return round((sum(scores) / len(scores)) * 100, 1)


def return_quality_index(return_win_rate: Optional[float]) -> float:
    """Return quality index — 100 = tour average. Uses return point win rate."""
    if return_win_rate is None:
        return 100.0
    return round((return_win_rate / AVG_RETURN_WIN_RATE) * 100, 1)


# ── TheSportsDB helpers ───────────────────────────────────────────────────────

def _tsdb_search_player(name: str) -> Optional[dict]:
    data = _tsdb_get("searchplayers.php", {"p": name})
    players = data.get("player") or []
    if not players:
        return None
    tt = [p for p in players if "table" in (p.get("strSport") or "").lower()]
    return tt[0] if tt else players[0]


def _tsdb_player_last10(player_id: str) -> list[dict]:
    year = datetime.now().year
    for season in [str(year), str(year - 1)]:
        try:
            data   = _tsdb_get("eventsplayer.php", {"id": player_id, "s": season})
            events = data.get("events") or []
            done   = [e for e in events if e.get("intHomeScore") is not None or e.get("strResult")]
            done.sort(key=lambda x: x.get("dateEvent", ""), reverse=True)
            if done:
                return done[:10]
        except Exception:
            continue
    return []


def _normalize_tt_results(raw: list[dict], player_name: str) -> list[dict]:
    out = []
    for e in raw:
        home  = e.get("strHomeTeam") or e.get("strEvent", "")
        away  = e.get("strAwayTeam") or ""
        hs    = _f(e.get("intHomeScore"), -1)
        aws   = _f(e.get("intAwayScore"), -1)
        winner = home if hs > aws else (away if aws > hs else "")
        out.append({
            "date":       (e.get("dateEvent") or "")[:10],
            "player_a":   home,
            "player_b":   away,
            "winner":     winner,
            "tournament": e.get("strLeague", ""),
            "score":      f"{int(hs)}-{int(aws)}" if hs >= 0 else "",
        })
    return out


# ── Main context fetcher ──────────────────────────────────────────────────────

def fetch_table_tennis_context(
    player_a: str,
    player_b: str,
    tour: str = "ittf",
) -> dict:
    """
    Fetch context for a table tennis match from TheSportsDB.

    Returns dict with:
      player_a / player_b: {name, ranking, last10, aqi, rqi, recent_form}
      h2h: {wins_a, wins_b, advantage, matches}
      sources: list[dict]
    """
    sources: list[dict] = []
    result: dict = {
        "player_a": {"name": player_a},
        "player_b": {"name": player_b},
        "tour":     tour,
        "sources":  sources,
    }

    for key, name in [("player_a", player_a), ("player_b", player_b)]:
        player_data = result[key]
        last10: list[dict] = []

        try:
            tsdb_player = _tsdb_search_player(name)
            if tsdb_player:
                pid = tsdb_player.get("idPlayer")
                player_data["tsdb_id"]     = pid
                player_data["nationality"] = tsdb_player.get("strNationality", "")
                player_data["style"]       = tsdb_player.get("strPosition", "")
                player_data["birth_year"]  = (tsdb_player.get("dateBorn") or "")[:4]
                sources.append({
                    "label":   f"{name} (TheSportsDB)",
                    "url":     f"{TSDB_BASE}/lookupplayer.php?id={pid}",
                    "snippet": f"Found: {tsdb_player.get('strPlayer', name)}",
                })
                if pid:
                    raw10 = _tsdb_player_last10(pid)
                    if raw10:
                        last10 = _normalize_tt_results(raw10, name)
                        sources.append({
                            "label":   f"{name} recent results (TSDB)",
                            "url":     f"{TSDB_BASE}/eventsplayer.php?id={pid}",
                            "snippet": f"{len(last10)} matches found",
                        })
            else:
                sources.append({"label": f"{name} TSDB", "url": "", "snippet": "Not found"})
        except Exception as exc:
            sources.append({"label": f"{name} TSDB", "url": "", "snippet": f"ERROR: {exc}"})

        player_data["last10"] = last10

        # Recent form from last 10
        wins = sum(1 for m in last10 if name.lower() in m.get("winner", "").lower())
        player_data["recent_form"] = wins / len(last10) if last10 else None

        # AQI/RQI default (TSDB doesn't have granular TT stats → AI fallback fills these)
        player_data["attack_quality_index"]  = attack_quality_index(None, None)
        player_data["return_quality_index"]  = return_quality_index(None)
        player_data["ranking"]               = player_data.get("ranking", 999)

    # ── Head-to-head ─────────────────────────────────────────────────────────
    h2h: list[dict] = []
    try:
        tid_a = result["player_a"].get("tsdb_id")
        tid_b = result["player_b"].get("tsdb_id")
        if tid_a and tid_b:
            data   = _tsdb_get("eventsh2h.php", {"idTeam1": tid_a, "idTeam2": tid_b})
            events = data.get("results") or data.get("events") or []
            h2h    = _normalize_tt_results(events[:10], player_a)
            if h2h:
                sources.append({
                    "label":   f"H2H: {player_a} vs {player_b}",
                    "url":     f"{TSDB_BASE}/eventsh2h.php?idTeam1={tid_a}&idTeam2={tid_b}",
                    "snippet": f"{len(h2h)} previous meetings",
                })
    except Exception as exc:
        sources.append({"label": "H2H", "url": "", "snippet": f"ERROR: {exc}"})

    wins_a = sum(1 for m in h2h if player_a.lower() in m.get("winner", "").lower())
    wins_b = sum(1 for m in h2h if player_b.lower() in m.get("winner", "").lower())
    result["h2h"] = {
        "wins_a":    wins_a,
        "wins_b":    wins_b,
        "advantage": "player_a" if wins_a > wins_b else ("player_b" if wins_b > wins_a else "even"),
        "matches":   h2h,
    }
    return result
