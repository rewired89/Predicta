"""
Setka Cup & TT Cup scrapers for Eastern European club table tennis.

These leagues are invisible to ITTF / WTT / TheSportsDB but have their own
public player/participant pages with form history and H2H.

Sources:
  Setka Cup:  tabletennis.setkacup.com/en/participants/
  TT Cup:     tt-cup.com

Strategy:
  1. Search the participants list for the player name
  2. Scrape their profile page for recent results and win rate
  3. Scrape H2H if both player IDs are found

Both sites render mostly server-side HTML so plain httpx + BeautifulSoup works.
"""
from __future__ import annotations
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

TIMEOUT = 20.0

SETKA_BASE = "https://tabletennis.setkacup.com"
TTCUP_BASE = "https://tt-cup.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _get(url: str, params: dict | None = None) -> str:
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS,
                          follow_redirects=True) as c:
            r = c.get(url, params=params or {})
            r.raise_for_status()
            return r.text
    except Exception:
        return ""


def _name_score(query: str, candidate: str) -> int:
    """Token overlap between search query and candidate string."""
    q_tokens = set(query.lower().split())
    c_tokens = set(candidate.lower().split())
    return len(q_tokens & c_tokens)


# ── Setka Cup ─────────────────────────────────────────────────────────────────

def setka_search_player(name: str) -> Optional[dict]:
    """
    Search tabletennis.setkacup.com/en/participants for a player.
    Returns {name, player_id, profile_url, source} or None.
    """
    html = _get(f"{SETKA_BASE}/en/participants", {"search": name})
    if not html:
        # Try without query param — some versions use JS filtering
        html = _get(f"{SETKA_BASE}/en/participants/")
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Player links: /en/participants/{id} or /en/player/{id}
    links = soup.find_all("a", href=re.compile(r"/en/(participants|player)/\d+"))
    if not links:
        # Try generic links containing player IDs
        links = soup.find_all("a", href=re.compile(r"/\d+$"))

    best = None
    best_score = 0
    for link in links:
        text = link.get_text(strip=True)
        score = _name_score(name, text)
        if score > best_score:
            best_score = score
            best = link

    if not best or best_score == 0:
        return None

    href = best["href"]
    pid_match = re.search(r"/(\d+)(?:/|$)", href)
    player_id = pid_match.group(1) if pid_match else None

    return {
        "name":        best.get_text(strip=True),
        "player_id":   player_id,
        "profile_url": f"{SETKA_BASE}{href}",
        "source":      "setkacup.com",
    }


def setka_player_profile(player_id: str) -> dict:
    """
    Scrape a Setka Cup player profile for recent form and stats.
    Returns {recent_form, recent_matches, win_rate, ranking} or {}.
    """
    html = _get(f"{SETKA_BASE}/en/participants/{player_id}")
    if not html:
        html = _get(f"{SETKA_BASE}/en/player/{player_id}")
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    # Win rate — look for W/L counts or a percentage
    wl = re.search(r"(\d+)\s*/\s*(\d+)", html)  # e.g. "47 / 23" or "W47 L23"
    if wl:
        w, l = int(wl.group(1)), int(wl.group(2))
        total = w + l
        if 5 <= total <= 500:  # sanity check
            data["recent_form"] = w / total
            data["recent_n"] = total

    # Win percentage displayed as "xx.x%"
    pct_match = re.search(r"(\d{1,3}\.\d)\s*%", html)
    if pct_match and "recent_form" not in data:
        pct = float(pct_match.group(1))
        if 0 < pct < 100:
            data["recent_form"] = pct / 100

    # Rankings / rating
    rank_match = re.search(r"[Rr]ank(?:ing)?\s*[:#]?\s*(\d+)", html)
    if rank_match:
        data["ranking"] = int(rank_match.group(1))

    # Recent match results — look for result rows with W/L
    matches = []
    rows = soup.select("table tr, .match-row, .result-row")
    for row in rows[:20]:
        text = row.get_text(separator=" ", strip=True)
        # Look for rows with win/loss indicators
        if re.search(r"\b[WwLl]\b", text) and re.search(r"\d+:\d+|\d+-\d+", text):
            result = "W" if re.search(r"\bW\b", text) else "L"
            score_m = re.search(r"(\d+)[:\-](\d+)", text)
            matches.append({
                "result": result,
                "score":  score_m.group(0) if score_m else "",
                "text":   text[:80],
            })

    if matches:
        data["recent_matches"] = matches
        if "recent_form" not in data:
            wins = sum(1 for m in matches if m["result"] == "W")
            data["recent_form"] = wins / len(matches)
            data["recent_n"] = len(matches)

    return data


def setka_h2h(player_id_a: str, player_id_b: str) -> dict:
    """
    Attempt to fetch H2H from Setka Cup.
    URL pattern: /en/head-to-head?player1={id}&player2={id}
    Returns {wins_a, wins_b, matches}.
    """
    html = _get(
        f"{SETKA_BASE}/en/head-to-head",
        {"player1": player_id_a, "player2": player_id_b},
    )
    if not html:
        # Try alternate patterns
        html = _get(f"{SETKA_BASE}/en/h2h/{player_id_a}/{player_id_b}")
    if not html:
        return {"wins_a": 0, "wins_b": 0, "matches": []}

    soup = BeautifulSoup(html, "html.parser")
    wins_a = wins_b = 0
    matches = []

    rows = soup.select("table tr")
    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) >= 2:
            text = " ".join(cells)
            if str(player_id_a) in text:
                wins_a += 1
            elif str(player_id_b) in text:
                wins_b += 1
            matches.append({"row": text[:100]})

    return {"wins_a": wins_a, "wins_b": wins_b, "matches": matches}


# ── TT Cup ────────────────────────────────────────────────────────────────────

def ttcup_search_player(name: str) -> Optional[dict]:
    """
    Search tt-cup.com for a player.
    Returns {name, player_id, profile_url, source} or None.
    """
    # TT Cup uses a search endpoint
    for search_url in [
        f"{TTCUP_BASE}/en/players",
        f"{TTCUP_BASE}/players",
        f"{TTCUP_BASE}/en/participants",
    ]:
        html = _get(search_url, {"search": name, "q": name, "name": name})
        if html:
            break
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=re.compile(r"/(player|participant|profile)/\d+"))
    if not links:
        links = soup.find_all("a", href=re.compile(r"/\d+$"))

    best = None
    best_score = 0
    for link in links:
        text = link.get_text(strip=True)
        score = _name_score(name, text)
        if score > best_score:
            best_score = score
            best = link

    if not best or best_score == 0:
        return None

    href = best["href"]
    pid_match = re.search(r"/(\d+)(?:/|$)", href)
    player_id = pid_match.group(1) if pid_match else None

    base = TTCUP_BASE if href.startswith("/") else ""
    return {
        "name":        best.get_text(strip=True),
        "player_id":   player_id,
        "profile_url": f"{base}{href}",
        "source":      "tt-cup.com",
    }


def ttcup_player_profile(player_id: str) -> dict:
    """Scrape TT Cup player profile for win rate and recent results."""
    html = ""
    for path in [f"/en/players/{player_id}", f"/players/{player_id}",
                 f"/en/participant/{player_id}", f"/player/{player_id}"]:
        html = _get(f"{TTCUP_BASE}{path}")
        if html:
            break
    if not html:
        return {}

    data: dict = {}

    wl = re.search(r"(\d+)\s*/\s*(\d+)", html)
    if wl:
        w, l = int(wl.group(1)), int(wl.group(2))
        total = w + l
        if 5 <= total <= 500:
            data["recent_form"] = w / total
            data["recent_n"] = total

    pct_match = re.search(r"(\d{1,3}\.\d)\s*%", html)
    if pct_match and "recent_form" not in data:
        pct = float(pct_match.group(1))
        if 0 < pct < 100:
            data["recent_form"] = pct / 100

    return data


# ── Unified lookup ────────────────────────────────────────────────────────────

def lookup_club_tt_player(name: str) -> dict:
    """
    Try Setka Cup then TT Cup for a club-circuit player.
    Returns merged dict: {name, recent_form, recent_n, ranking, source, profile_url}
    """
    result: dict = {"name": name, "recent_form": None, "source": None}

    # 1. Try Setka Cup
    setka = setka_search_player(name)
    if setka and setka.get("player_id"):
        profile = setka_player_profile(setka["player_id"])
        result.update({k: v for k, v in profile.items() if v is not None})
        result["source"] = "setkacup.com"
        result["profile_url"] = setka.get("profile_url", "")
        result["setka_id"] = setka["player_id"]
        if result.get("recent_form") is not None:
            return result

    # 2. Try TT Cup
    ttcup = ttcup_search_player(name)
    if ttcup and ttcup.get("player_id"):
        profile = ttcup_player_profile(ttcup["player_id"])
        result.update({k: v for k, v in profile.items() if v is not None})
        if not result.get("source"):
            result["source"] = "tt-cup.com"
        result["profile_url"] = result.get("profile_url") or ttcup.get("profile_url", "")
        result["ttcup_id"] = ttcup["player_id"]

    return result
