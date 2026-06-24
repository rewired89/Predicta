"""
ITTF data scrapers — results.ittf.link + worldtabletennis.com

Two complementary sources:
  results.ittf.link    — Player profiles, match history, H2H, ITTF rankings
  worldtabletennis.com — Official WTT player list + ITTF ranking numbers

Neither has a public JSON API; both render HTML/JS. We:
  1. Probe the known URL patterns to get player ID
  2. Fetch player profile page and scrape ranking + match stats
  3. Fetch H2H page between two player IDs
  4. Fall back gracefully if either site blocks or returns no data
"""
from __future__ import annotations
import re
import math
from typing import Optional

import httpx
from bs4 import BeautifulSoup

TIMEOUT = 20.0

ITTF_BASE = "https://results.ittf.link"
WTT_BASE  = "https://www.worldtabletennis.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _get(url: str, params: dict | None = None) -> str:
    """GET a page, return HTML string or '' on failure."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(url, params=params or {})
            r.raise_for_status()
            return r.text
    except Exception:
        return ""


def _f(val, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


# ── WTT Player Search ─────────────────────────────────────────────────────────

def wtt_search_player(name: str) -> Optional[dict]:
    """
    Search worldtabletennis.com/playerslist for a player by name.
    Returns dict {name, ittf_id, ranking, nationality} or None.

    The page at /playerslist accepts a query param 'search' with the player name.
    Example: /playerslist?search=Fan+Zhendong
    """
    html = _get(f"{WTT_BASE}/playerslist", {"search": name})
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Player rows are typically in a table or list with player links
    # Link pattern: /playerProfile/ITTF_ID
    player_links = soup.find_all("a", href=re.compile(r"/playerProfile/\d+"))
    if not player_links:
        return None

    # Find the closest name match
    name_lower = name.lower()
    best = None
    best_score = 0

    for link in player_links:
        link_text = link.get_text(strip=True).lower()
        # Simple token overlap score
        name_tokens = set(name_lower.split())
        link_tokens = set(link_text.split())
        score = len(name_tokens & link_tokens)
        if score > best_score:
            best_score = score
            best = link

    if not best or best_score == 0:
        return None

    href = best["href"]
    ittf_id_match = re.search(r"/playerProfile/(\d+)", href)
    ittf_id = ittf_id_match.group(1) if ittf_id_match else None

    # Try to get ranking from the same row
    row = best.find_parent("tr") or best.find_parent("li") or best.find_parent("div")
    ranking = None
    if row:
        rank_text = re.search(r"\b(\d{1,4})\b", row.get_text())
        if rank_text:
            ranking = int(rank_text.group(1))

    return {
        "name":        best.get_text(strip=True),
        "ittf_id":     ittf_id,
        "ranking":     ranking,
        "source":      "worldtabletennis.com",
    }


def wtt_player_profile(ittf_id: str) -> dict:
    """
    Scrape worldtabletennis.com/playerProfile/{id} for ranking + stats.
    Returns {ranking, nationality, style, win_rate_year} or {}.
    """
    html = _get(f"{WTT_BASE}/playerProfile/{ittf_id}")
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    # ITTF ranking — usually displayed as "World Ranking: #X"
    rank_tag = soup.find(string=re.compile(r"World Ranking", re.I))
    if rank_tag:
        m = re.search(r"#?(\d+)", rank_tag.find_next(string=True) or "")
        if m:
            data["ranking"] = int(m.group(1))

    # Nationality
    nat_tag = soup.find(string=re.compile(r"Nationality|Country|Association", re.I))
    if nat_tag:
        data["nationality"] = (nat_tag.find_next(string=True) or "").strip()

    # Win rate stats — some profiles show season W/L record
    wl = re.search(r"(\d+)\s*W\s*/\s*(\d+)\s*L", html)
    if wl:
        w, l = int(wl.group(1)), int(wl.group(2))
        total = w + l
        if total > 0:
            data["season_win_rate"] = w / total
            data["season_matches"]  = total

    return data


# ── ITTF Results — Player Search & H2H ───────────────────────────────────────

def ittf_search_player(name: str) -> Optional[dict]:
    """
    Search results.ittf.link for a player.
    The site uses a search form at /index.php/player-profile that POSTs the name.
    Returns dict {name, ittf_id, ranking} or None.
    """
    # Try POST search first
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as c:
            # The search form submits to the same page
            r = c.post(
                f"{ITTF_BASE}/index.php/player-profile",
                data={"searchPlayerName": name, "task": "search"},
            )
            r.raise_for_status()
            html = r.text
    except Exception:
        html = ""

    if not html:
        # Fallback: GET with name param
        html = _get(f"{ITTF_BASE}/index.php/player-profile", {"searchPlayerName": name})

    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Player results typically link to /player-profile/details/{ittf_num}/{player_id}
    links = soup.find_all("a", href=re.compile(r"/player-profile/details/"))
    if not links:
        return None

    name_lower = name.lower()
    best = None
    best_score = 0

    for link in links:
        text = link.get_text(strip=True).lower()
        tokens_q = set(name_lower.split())
        tokens_r = set(text.split())
        score = len(tokens_q & tokens_r)
        if score > best_score:
            best_score = score
            best = link

    if not best or best_score == 0:
        return None

    href = best["href"]
    # URL: /player-profile/details/{ittf_seq}/{player_db_id}
    m = re.search(r"/player-profile/details/(\d+)/(\d+)", href)
    if not m:
        return None

    return {
        "name":        best.get_text(strip=True),
        "ittf_seq":    m.group(1),
        "ittf_id":     m.group(2),
        "profile_url": f"{ITTF_BASE}{href}",
        "source":      "results.ittf.link",
    }


def ittf_player_profile(ittf_seq: str, ittf_id: str) -> dict:
    """
    Scrape the ITTF player profile page for ranking + recent results.
    URL: results.ittf.link/index.php/player-profile/details/{seq}/{id}

    Returns {ranking, nationality, recent_matches, win_rate, style}
    """
    html = _get(f"{ITTF_BASE}/index.php/player-profile/details/{ittf_seq}/{ittf_id}")
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    # ITTF ranking — labelled "World Ranking" or "ITTF Ranking"
    for label in soup.find_all(string=re.compile(r"World Rank|ITTF Rank", re.I)):
        parent = label.find_parent()
        if parent:
            m = re.search(r"(\d+)", parent.get_text())
            if m:
                data["ranking"] = int(m.group(1))
                break

    # Recent match results table
    matches = []
    result_rows = soup.select("table tr")
    for row in result_rows[1:21]:  # skip header, take up to 20 rows
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) >= 4:
            matches.append({
                "date":       cells[0] if cells else "",
                "tournament": cells[1] if len(cells) > 1 else "",
                "opponent":   cells[2] if len(cells) > 2 else "",
                "result":     cells[3] if len(cells) > 3 else "",  # W or L
                "score":      cells[4] if len(cells) > 4 else "",
            })

    if matches:
        wins = sum(1 for m in matches if m.get("result", "").upper().startswith("W"))
        data["recent_matches"]  = matches
        data["recent_form"]     = wins / len(matches)
        data["recent_n"]        = len(matches)

    return data


def ittf_h2h(ittf_id_a: str, ittf_id_b: str) -> dict:
    """
    Fetch H2H record from results.ittf.link/index.php/head-to-head
    Query params: player1={id_a}&player2={id_b}

    Returns {wins_a, wins_b, matches: list[dict]}
    """
    html = _get(
        f"{ITTF_BASE}/index.php/head-to-head",
        {"player1": ittf_id_a, "player2": ittf_id_b},
    )
    if not html:
        return {"wins_a": 0, "wins_b": 0, "matches": []}

    soup = BeautifulSoup(html, "html.parser")
    matches = []
    wins_a = wins_b = 0

    rows = soup.select("table tr")
    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) >= 3:
            winner_cell = cells[-1] if cells else ""
            matches.append({
                "date":    cells[0] if cells else "",
                "player1": cells[1] if len(cells) > 1 else "",
                "player2": cells[2] if len(cells) > 2 else "",
                "winner":  winner_cell,
            })
            # Count wins by checking winner cell content
            if ittf_id_a in winner_cell or (len(cells) > 3 and "1" in cells[3]):
                wins_a += 1
            elif ittf_id_b in winner_cell or (len(cells) > 3 and "2" in cells[3]):
                wins_b += 1

    return {"wins_a": wins_a, "wins_b": wins_b, "matches": matches}


# ── Unified lookup (tries WTT first, then ITTF) ───────────────────────────────

def lookup_tt_player(name: str) -> dict:
    """
    Try both sources for a player. Returns merged best data dict:
    {name, ittf_id, wtt_id, ranking, recent_form, nationality, source}
    """
    result: dict = {"name": name, "ranking": None, "recent_form": None}

    # 1. Try WTT (cleaner ranking data)
    wtt = wtt_search_player(name)
    if wtt and wtt.get("ittf_id"):
        result.update(wtt)
        profile = wtt_player_profile(wtt["ittf_id"])
        result.update({k: v for k, v in profile.items() if v is not None})

    # 2. Try ITTF results (has match history + H2H)
    ittf = ittf_search_player(name)
    if ittf:
        result["ittf_seq"] = ittf.get("ittf_seq")
        result["ittf_results_id"] = ittf.get("ittf_id")
        result["profile_url"] = ittf.get("profile_url", "")
        if not result.get("ranking"):
            result["source"] = "results.ittf.link"
        profile2 = ittf_player_profile(ittf["ittf_seq"], ittf["ittf_id"])
        # Only overwrite ranking if we didn't already have one
        if not result.get("ranking") and profile2.get("ranking"):
            result["ranking"] = profile2["ranking"]
        # Always take recent_form from ITTF if available (more match history)
        if profile2.get("recent_form") is not None:
            result["recent_form"]    = profile2["recent_form"]
            result["recent_matches"] = profile2.get("recent_matches", [])
            result["recent_n"]       = profile2.get("recent_n", 0)

    return result
