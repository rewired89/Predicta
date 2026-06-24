"""
Table tennis data fetcher.
Source priority:
  1. results.ittf.link  — player profile, ranking, match history, H2H
  2. worldtabletennis.com — ITTF ranking, season win rate
  3. TheSportsDB (free tier) — fallback last10, player metadata

Key signals:
  Attack Quality Index (AQI)  — composite attack win rate + 3rd ball attack rate
  Return Quality Index (RQI)  — return point win rate
  Recent form                 — last-10/20 win rate from ITTF match history
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
    Fetch context for a table tennis match.
    Source priority: ITTF results.ittf.link → WTT worldtabletennis.com → TSDB fallback.

    Returns dict with:
      player_a / player_b: {name, ranking, last10, aqi, rqi, recent_form}
      h2h: {wins_a, wins_b, advantage, matches}
      sources: list[dict]
    """
    from fetchers.ittf import lookup_tt_player, ittf_h2h

    sources: list[dict] = []
    result: dict = {
        "player_a": {"name": player_a},
        "player_b": {"name": player_b},
        "tour":     tour,
        "sources":  sources,
    }

    ittf_ids: dict[str, str] = {}  # key → ittf_results_id for H2H

    for key, name in [("player_a", player_a), ("player_b", player_b)]:
        player_data = result[key]
        last10: list[dict] = []

        # ── 1. Try ITTF + WTT ─────────────────────────────────────────────
        ittf_data: dict = {}
        try:
            ittf_data = lookup_tt_player(name)
        except Exception as exc:
            sources.append({"label": f"{name} ITTF", "url": "", "snippet": f"ERROR: {exc}"})

        if ittf_data.get("ranking") or ittf_data.get("recent_form") is not None:
            player_data["ranking"]     = ittf_data.get("ranking", 999)
            player_data["nationality"] = ittf_data.get("nationality", "")
            player_data["style"]       = ittf_data.get("style", "all-round")
            if ittf_data.get("ittf_results_id"):
                ittf_ids[key] = ittf_data["ittf_results_id"]
            if ittf_data.get("recent_form") is not None:
                player_data["recent_form"] = ittf_data["recent_form"]
                last10 = ittf_data.get("recent_matches", [])
            sources.append({
                "label":   f"{name} ({ittf_data.get('source', 'ITTF/WTT')})",
                "url":     ittf_data.get("profile_url", ""),
                "snippet": (
                    f"ITTF rank #{ittf_data.get('ranking', '?')} | "
                    f"recent form {round((ittf_data.get('recent_form') or 0)*100)}%"
                ),
            })
        else:
            sources.append({"label": f"{name} ITTF/WTT", "url": "", "snippet": "Not found"})

        # ── 2. Setka Cup / TT Cup (club circuit) ─────────────────────────
        if not last10 and not ittf_data.get("recent_form"):
            try:
                from fetchers.setka import lookup_club_tt_player
                club_data = lookup_club_tt_player(name)
                if club_data.get("recent_form") is not None:
                    player_data["recent_form"] = club_data["recent_form"]
                    sources.append({
                        "label":   f"{name} ({club_data.get('source', 'Setka/TT Cup')})",
                        "url":     club_data.get("profile_url", ""),
                        "snippet": (
                            f"Club form: {round(club_data['recent_form']*100)}% win rate "
                            f"({club_data.get('recent_n', '?')} matches)"
                        ),
                    })
            except Exception as exc:
                sources.append({"label": f"{name} Setka/TT Cup", "url": "", "snippet": f"ERROR: {exc}"})

        # ── 3. TSDB fallback for last10 if ITTF had no match history ──────
        if not last10:
            try:
                tsdb_player = _tsdb_search_player(name)
                if tsdb_player:
                    pid = tsdb_player.get("idPlayer")
                    player_data.setdefault("nationality", tsdb_player.get("strNationality", ""))
                    player_data.setdefault("style", tsdb_player.get("strPosition", ""))
                    if pid:
                        raw10 = _tsdb_player_last10(pid)
                        if raw10:
                            last10 = _normalize_tt_results(raw10, name)
                            sources.append({
                                "label":   f"{name} recent results (TSDB fallback)",
                                "url":     f"{TSDB_BASE}/eventsplayer.php?id={pid}",
                                "snippet": f"{len(last10)} matches found",
                            })
            except Exception as exc:
                sources.append({"label": f"{name} TSDB", "url": "", "snippet": f"ERROR: {exc}"})

        player_data["last10"] = last10

        # Compute recent_form from last10 if ITTF didn't provide it
        if player_data.get("recent_form") is None and last10:
            wins = sum(1 for m in last10 if name.lower() in m.get("winner", "").lower())
            player_data["recent_form"] = wins / len(last10)

        # AQI/RQI — ITTF/TSDB don't expose granular serve stats for TT
        # so these remain at 100 (tour average) unless AI fallback enriches them
        player_data["attack_quality_index"] = attack_quality_index(None, None)
        player_data["return_quality_index"] = return_quality_index(None)
        player_data.setdefault("ranking", 999)

    # ── Head-to-head — try ITTF first, then TSDB ─────────────────────────────
    h2h: list[dict] = []
    wins_a = wins_b = 0

    ittf_id_a = ittf_ids.get("player_a")
    ittf_id_b = ittf_ids.get("player_b")

    if ittf_id_a and ittf_id_b:
        try:
            h2h_data = ittf_h2h(ittf_id_a, ittf_id_b)
            wins_a   = h2h_data.get("wins_a", 0)
            wins_b   = h2h_data.get("wins_b", 0)
            h2h      = h2h_data.get("matches", [])
            if h2h:
                sources.append({
                    "label":   f"H2H: {player_a} vs {player_b} (ITTF)",
                    "url":     f"https://results.ittf.link/index.php/head-to-head?player1={ittf_id_a}&player2={ittf_id_b}",
                    "snippet": f"{len(h2h)} meetings: {wins_a}-{wins_b}",
                })
        except Exception as exc:
            sources.append({"label": "H2H ITTF", "url": "", "snippet": f"ERROR: {exc}"})

    if not h2h:
        try:
            tid_a = result["player_a"].get("tsdb_id")
            tid_b = result["player_b"].get("tsdb_id")
            if tid_a and tid_b:
                data   = _tsdb_get("eventsh2h.php", {"idTeam1": tid_a, "idTeam2": tid_b})
                events = data.get("results") or data.get("events") or []
                h2h    = _normalize_tt_results(events[:10], player_a)
                if h2h:
                    wins_a = sum(1 for m in h2h if player_a.lower() in m.get("winner", "").lower())
                    wins_b = sum(1 for m in h2h if player_b.lower() in m.get("winner", "").lower())
                    sources.append({
                        "label":   f"H2H: {player_a} vs {player_b} (TSDB)",
                        "url":     f"{TSDB_BASE}/eventsh2h.php?idTeam1={tid_a}&idTeam2={tid_b}",
                        "snippet": f"{len(h2h)} previous meetings",
                    })
        except Exception as exc:
            sources.append({"label": "H2H TSDB", "url": "", "snippet": f"ERROR: {exc}"})

    result["h2h"] = {
        "wins_a":    wins_a,
        "wins_b":    wins_b,
        "advantage": "player_a" if wins_a > wins_b else ("player_b" if wins_b > wins_a else "even"),
        "matches":   h2h,
    }
    return result
