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
SPORT_MAP = {
    "soccer": "soccer_epl",
    "tennis": "tennis_atp_french_open",
    "table_tennis": None,  # not covered by The Odds API
}


def _get_api_key() -> Optional[str]:
    return os.environ.get("ODDS_API_KEY")


def fetch_odds_snapshot(
    match_id: int,
    sport: str,
    league_key: Optional[str] = None,
    market: str = "h2h",
) -> list[dict]:
    """
    Fetch current odds from The Odds API and persist to odds_snapshots.
    Returns list of inserted snapshot dicts.
    """
    api_key = _get_api_key()
    if not api_key:
        return [{"error": "ODDS_API_KEY not set. Export it in your environment."}]

    sport_key = league_key or SPORT_MAP.get(sport)
    if not sport_key:
        return [{"error": f"No Odds API key configured for sport '{sport}'."}]

    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {"apiKey": api_key, "regions": "eu", "markets": market, "oddsFormat": "decimal"}

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        return [{"error": str(exc)}]

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
