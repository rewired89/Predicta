"""
Odds fetcher using The Odds API (https://the-odds-api.com).
Verify ToS before enabling automated fetching.
Set ODDS_API_KEY in environment to enable.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from db.database import get_db

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# The Odds API sport keys — maps our internal sport name to a default league.
# For international matches (World Cup, Euros, Nations League) pass league_key
# explicitly to fetch_odds_snapshot; these defaults cover domestic club soccer.
SPORT_MAP = {
    "soccer": "soccer_epl",
    "tennis": "tennis_atp_us_open",
    "table_tennis": None,  # not covered by The Odds API
}

# International soccer competitions available on The Odds API
INTL_SOCCER_KEYS = [
    "soccer_fifa_world_cup",
    "soccer_uefa_euro_qualification",
    "soccer_uefa_nations_league",
    "soccer_conmebol_copa_america",
    "soccer_concacaf_gold_cup",
    "soccer_africa_cup_of_nations",
]


def _get_api_key() -> Optional[str]:
    return os.environ.get("ODDS_API_KEY")


def _fetch_from_key(api_key: str, sport_key: str, market: str) -> list:
    """Fetch raw event list from one Odds API sport key. Returns [] on error."""
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {"apiKey": api_key, "regions": "eu", "markets": market, "oddsFormat": "decimal"}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError:
        return []


def fetch_odds_snapshot(
    match_id: int,
    sport: str,
    league_key: Optional[str] = None,
    market: str = "h2h",
) -> list[dict]:
    """
    Fetch current odds from The Odds API and persist to odds_snapshots.
    For soccer with no explicit league_key, probes international competitions
    first (World Cup, Euros, Nations League) then falls back to EPL as default.
    Returns list of inserted snapshot dicts.
    """
    api_key = _get_api_key()
    if not api_key:
        return [{"error": "ODDS_API_KEY not set. Export it in your environment."}]

    if league_key:
        keys_to_try = [league_key]
    elif sport == "soccer":
        keys_to_try = INTL_SOCCER_KEYS + [SPORT_MAP["soccer"]]
    else:
        fallback = SPORT_MAP.get(sport)
        if not fallback:
            return [{"error": f"No Odds API key configured for sport '{sport}'."}]
        keys_to_try = [fallback]

    data = []
    for key in keys_to_try:
        data = _fetch_from_key(api_key, key, market)
        if data:
            break

    if not data:
        return [{"error": "No odds found across any configured sport keys."}]

    now = datetime.now(timezone.utc).isoformat()
    inserted = []
    with get_db() as conn:
        for event in data:
            for bookmaker in event.get("bookmakers", []):
                for mkt in bookmaker.get("markets", []):
                    outcomes = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                    keys = list(outcomes.keys())
                    price_a = outcomes.get(keys[0]) if len(keys) > 0 else None
                    price_b = outcomes.get(keys[1]) if len(keys) > 1 else None
                    price_draw = outcomes.get("Draw") if "Draw" in outcomes else None
                    conn.execute(
                        """INSERT INTO odds_snapshots
                               (match_id, book, market, price_a, price_b, price_draw, captured_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (match_id, bookmaker["key"], mkt["key"], price_a, price_b, price_draw, now),
                    )
                    inserted.append({
                        "book": bookmaker["key"],
                        "market": mkt["key"],
                        "price_a": price_a,
                        "price_b": price_b,
                        "price_draw": price_draw,
                    })
    return inserted


def log_manual_odds(
    match_id: int,
    book: str,
    market: str,
    price_a: float,
    price_b: float,
    price_draw: Optional[float] = None,
) -> int:
    """Persist manually entered odds snapshot and return its row ID."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO odds_snapshots
                   (match_id, book, market, price_a, price_b, price_draw, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (match_id, book, market, price_a, price_b, price_draw, now),
        )
        return cur.lastrowid
