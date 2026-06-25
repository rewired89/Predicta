"""
Auto-resolve match results from Setka Cup and TT Cup.

After a match's scheduled_at time passes, this scraper looks up the actual
result by checking the H2H page for the two players. If a result is found
within the last 24 hours, it records the outcome automatically.

Sources in priority order:
  1. Setka Cup H2H page (most reliable for Ukrainian/Czech DL)
  2. TT Cup H2H page
  3. Player recent-match list (fallback if H2H page not found)
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fetchers.setka import (
    setka_search_player, setka_h2h, setka_player_profile,
    ttcup_search_player, _get,
    SETKA_BASE, TTCUP_BASE,
)

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False


def _norm_name(name: str) -> str:
    """Lowercase, strip extra spaces."""
    return " ".join(name.lower().split())


def _names_match(name_a: str, candidate: str, threshold: int = 1) -> bool:
    """True if at least `threshold` tokens from name_a appear in candidate."""
    a_tokens = set(_norm_name(name_a).split())
    c_tokens = set(_norm_name(candidate).split())
    return len(a_tokens & c_tokens) >= threshold


def _parse_score(text: str) -> tuple[int, int] | None:
    """
    Extract the first game-score pattern from text.
    Handles: "3:1", "3-1", "11:5 11:7 11:3" (set scores — takes count of sets won).
    Returns (score_a, score_b) as games/sets won, or None.
    """
    # Match score like 3:1 or 3-1 (match score, not individual game)
    m = re.search(r'\b([0-9])\s*[:\-]\s*([0-9])\b', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _result_from_score(score_a: int, score_b: int) -> str:
    """Convert numeric scores to 'a', 'b', or 'draw'."""
    if score_a > score_b:
        return "a"
    if score_b > score_a:
        return "b"
    return "draw"


# ── Setka Cup result lookup ───────────────────────────────────────────────────

def setka_fetch_result(
    player_a: str,
    player_b: str,
    match_date: str,          # ISO "YYYY-MM-DD"
    tolerance_days: int = 1,
) -> Optional[dict]:
    """
    Look up the result of a specific Setka Cup match between two players.

    Strategy:
      1. Find both player IDs via search
      2. Fetch their H2H page
      3. Find the match closest to match_date within tolerance_days
      4. Return {result, score_a, score_b, source}

    Returns None if no result found.
    """
    pa_info = setka_search_player(player_a)
    pb_info = setka_search_player(player_b)

    if not pa_info or not pb_info:
        return None

    pid_a = pa_info.get("player_id")
    pid_b = pb_info.get("player_id")
    if not pid_a or not pid_b:
        return None

    h2h = setka_h2h(pid_a, pid_b)
    matches = h2h.get("matches", [])

    # Try to find the specific match by date
    target = datetime.fromisoformat(match_date).date()
    for m in matches:
        row_text = m.get("row", "")
        # Look for a date in the row
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})|(\d{2}[./]\d{2}[./]\d{4})', row_text)
        row_date = None
        if date_match:
            raw = date_match.group(0)
            try:
                if "-" in raw:
                    row_date = datetime.fromisoformat(raw).date()
                else:
                    # DD.MM.YYYY or DD/MM/YYYY
                    parts = re.split(r'[./]', raw)
                    row_date = datetime(int(parts[2]), int(parts[1]), int(parts[0])).date()
            except Exception:
                pass

        # Accept if within tolerance or no date found (use most recent)
        if row_date is None or abs((row_date - target).days) <= tolerance_days:
            score = _parse_score(row_text)
            if score:
                sa, sb = score
                # Determine whose score is whose from the row text
                # If player_a's name appears before the score, sa belongs to A
                a_pos = row_text.lower().find(_norm_name(player_a).split()[0])
                b_pos = row_text.lower().find(_norm_name(player_b).split()[0])
                if b_pos != -1 and a_pos != -1 and b_pos < a_pos:
                    # B appears first in the row — swap
                    sa, sb = sb, sa
                return {
                    "result":   _result_from_score(sa, sb),
                    "score_a":  sa,
                    "score_b":  sb,
                    "source":   f"setkacup.com H2H ({pid_a} vs {pid_b})",
                    "raw":      row_text[:120],
                }

    # Fallback: check player A's recent matches for a match vs player B
    profile_a = setka_player_profile(pid_a)
    for m in profile_a.get("recent_matches", []):
        text = m.get("text", "")
        if _names_match(player_b, text):
            score = _parse_score(text)
            if score:
                sa, sb = score
                won = m.get("result") == "W"
                if not won:
                    sa, sb = sb, sa
                return {
                    "result":   _result_from_score(sa, sb),
                    "score_a":  sa,
                    "score_b":  sb,
                    "source":   f"setkacup.com profile ({pid_a})",
                    "raw":      text[:120],
                }

    return None


# ── TT Cup result lookup ──────────────────────────────────────────────────────

def ttcup_fetch_result(
    player_a: str,
    player_b: str,
    match_date: str,
    tolerance_days: int = 1,
) -> Optional[dict]:
    """Look up a TT Cup match result between two players."""
    pa_info = ttcup_search_player(player_a)
    pb_info = ttcup_search_player(player_b)

    if not pa_info or not pa_info.get("player_id"):
        return None

    pid_a = pa_info["player_id"]

    # Try H2H endpoint
    for path in [
        f"/en/head-to-head?player1={pid_a}&player2={pb_info.get('player_id','')}",
        f"/en/h2h/{pid_a}/{pb_info.get('player_id','')}",
    ]:
        html = _get(f"{TTCUP_BASE}{path}")
        if not html or not _BS4:
            continue
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table tr")
        target = datetime.fromisoformat(match_date).date()
        for row in rows[1:]:
            text = row.get_text(separator=" ", strip=True)
            score = _parse_score(text)
            if score:
                sa, sb = score
                a_pos = text.lower().find(_norm_name(player_a).split()[0])
                b_pos = text.lower().find(_norm_name(player_b).split()[0])
                if b_pos != -1 and a_pos != -1 and b_pos < a_pos:
                    sa, sb = sb, sa
                return {
                    "result":  _result_from_score(sa, sb),
                    "score_a": sa,
                    "score_b": sb,
                    "source":  f"tt-cup.com H2H",
                    "raw":     text[:120],
                }

    # Fallback: player A's recent matches page
    for path in [f"/en/players/{pid_a}/matches", f"/players/{pid_a}"]:
        html = _get(f"{TTCUP_BASE}{path}")
        if not html:
            continue
        for line in html.split("\n"):
            if _names_match(player_b, line):
                score = _parse_score(line)
                if score:
                    sa, sb = score
                    return {
                        "result":  _result_from_score(sa, sb),
                        "score_a": sa,
                        "score_b": sb,
                        "source":  "tt-cup.com profile",
                        "raw":     line[:120],
                    }
    return None


# ── Unified result fetcher ────────────────────────────────────────────────────

def fetch_match_result(
    player_a: str,
    player_b: str,
    match_date: str,
    sport: str = "table_tennis",
    tour: str = "",
) -> Optional[dict]:
    """
    Try all available sources for a match result.
    Returns {result, score_a, score_b, source} or None.
    """
    if sport != "table_tennis":
        return None  # only TT auto-resolve implemented so far

    # Try Setka first
    res = setka_fetch_result(player_a, player_b, match_date)
    if res:
        return res

    # Try TT Cup
    res = ttcup_fetch_result(player_a, player_b, match_date)
    if res:
        return res

    return None
