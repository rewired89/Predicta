"""
fetchers/nrfi_odds.py

Fetch first-inning (NRFI/YRFI) odds from The Odds API for MLB games, so we can
measure Closing Line Value (CLV) — the industry-standard proof of edge that
sharp bettors and syndicates trust more than raw win rate.

Market used: `totals_1st_1_innings` (total runs in the 1st inning, line 0.5).
  Under 0.5  → NRFI (no run first inning)
  Over  0.5  → YRFI (yes run first inning)

Prefers Pinnacle (the sharpest book) as the reference line; falls back to the
median across all available books when Pinnacle isn't quoting the market.

Requires ODDS_API_KEY in the environment. Returns {} (graceful no-op) when the
key is absent or the API is unreachable — the pipeline runs fine without it,
CLV columns just stay NULL.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from statistics import median
from typing import Optional

import httpx

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY     = "baseball_mlb"
NRFI_MARKET   = "totals_1st_1_innings"
# Pinnacle first, then other sharp/major books as reference preference order.
SHARP_BOOKS   = ["pinnacle", "betonlineag", "lowvig", "bovada"]


def _api_key() -> Optional[str]:
    return os.environ.get("ODDS_API_KEY")


def _norm(name: str) -> str:
    """Normalize a team name for fuzzy matching (lowercase, alnum only)."""
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _match_event(events: list[dict], home_name: str, away_name: str) -> Optional[dict]:
    """Find the Odds API event whose home/away teams match our game."""
    hn, an = _norm(home_name), _norm(away_name)
    for ev in events:
        eh, ea = _norm(ev.get("home_team", "")), _norm(ev.get("away_team", ""))
        # Odds API sometimes flips home/away vs our source — accept either order.
        home_hit = eh and (eh in hn or hn in eh)
        away_hit = ea and (ea in an or an in ea)
        if home_hit and away_hit:
            return ev
        # try the flipped orientation
        if eh and ea and (eh in an or an in eh) and (ea in hn or hn in ea):
            return ev
    return None


def _extract_nrfi_prices(event_odds: dict) -> Optional[dict]:
    """
    From a single event-odds payload, pull the NRFI (Under 0.5) and YRFI
    (Over 0.5) decimal prices. Prefers a sharp book; else uses the median
    across all books quoting the 0.5 line.
    Returns {nrfi_dec, yrfi_dec, book} or None.
    """
    # Collect per-book {book: (under_dec, over_dec)} at point 0.5
    per_book: dict[str, tuple[float, float]] = {}
    for bm in event_odds.get("bookmakers", []):
        book = bm.get("key", "")
        for mkt in bm.get("markets", []):
            if mkt.get("key") != NRFI_MARKET:
                continue
            under = over = None
            for oc in mkt.get("outcomes", []):
                pt = oc.get("point")
                if pt is not None and abs(float(pt) - 0.5) > 1e-6:
                    continue
                name = (oc.get("name") or "").lower()
                price = oc.get("price")
                if price is None:
                    continue
                if name == "under":
                    under = float(price)
                elif name == "over":
                    over = float(price)
            if under and over:
                per_book[book] = (under, over)

    if not per_book:
        return None

    # Prefer the sharpest available book.
    for pref in SHARP_BOOKS:
        if pref in per_book:
            u, o = per_book[pref]
            return {"nrfi_dec": u, "yrfi_dec": o, "book": pref}

    # Otherwise use the median across all books.
    unders = median(v[0] for v in per_book.values())
    overs  = median(v[1] for v in per_book.values())
    return {"nrfi_dec": round(unders, 4), "yrfi_dec": round(overs, 4),
            "book": f"median({len(per_book)})"}


def fetch_nrfi_odds(games: list[dict]) -> dict[str, dict]:
    """
    Fetch current NRFI/YRFI odds for each game.

    games: list of dicts with keys home_abbr, away_abbr, home_name, away_name
           (as produced by scripts/daily_nrfi.py fetch_schedule()).

    Returns {"{away_abbr}@{home_abbr}": {nrfi_dec, yrfi_dec, book, captured_at}}.
    Empty dict when ODDS_API_KEY is unset or the API is unreachable.
    """
    key = _api_key()
    if not key:
        return {}

    try:
        with httpx.Client(timeout=15) as client:
            ev_resp = client.get(
                f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events",
                params={"apiKey": key, "dateFormat": "iso"},
            )
            ev_resp.raise_for_status()
            events = ev_resp.json()
    except (httpx.HTTPError, ValueError):
        return {}

    now = datetime.now(timezone.utc).isoformat()
    out: dict[str, dict] = {}

    for g in games:
        ev = _match_event(events, g.get("home_name", ""), g.get("away_name", ""))
        if not ev:
            continue
        try:
            with httpx.Client(timeout=15) as client:
                od_resp = client.get(
                    f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{ev['id']}/odds",
                    params={
                        "apiKey":     key,
                        "regions":    "us,eu",
                        "markets":    NRFI_MARKET,
                        "oddsFormat": "decimal",
                    },
                )
                od_resp.raise_for_status()
                prices = _extract_nrfi_prices(od_resp.json())
        except (httpx.HTTPError, ValueError):
            prices = None

        if prices:
            prices["captured_at"] = now
            out[f"{g['away_abbr']}@{g['home_abbr']}"] = prices

    return out


def diagnose() -> dict:
    """
    One-shot diagnostic that reports EXACTLY why odds capture works or fails —
    key present? events endpoint status + quota remaining? does the requested
    first-inning market come back, or does the API reject it (wrong market /
    plan not included)? Surfaces the raw status + error body instead of the
    silent empty-dict the normal path returns.
    """
    key = _api_key()
    if not key:
        return {"key_present": False,
                "error": "ODDS_API_KEY not visible to this process. It is set in "
                         "Railway but the running deploy may predate it — redeploy."}
    out: dict = {"key_present": True, "market_requested": NRFI_MARKET}
    try:
        with httpx.Client(timeout=25) as c:
            ev = c.get(f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events",
                       params={"apiKey": key, "dateFormat": "iso"})
            out["events_status"] = ev.status_code
            out["requests_remaining"] = ev.headers.get("x-requests-remaining")
            out["requests_used"] = ev.headers.get("x-requests-used")
            if ev.status_code != 200:
                out["events_error_body"] = ev.text[:400]
                return out
            events = ev.json()
            out["n_events"] = len(events) if isinstance(events, list) else 0
            if not out["n_events"]:
                out["note"] = "No MLB events returned by The Odds API right now."
                return out
            e0 = events[0]
            out["sample_event"] = {k: e0.get(k) for k in
                                   ("home_team", "away_team", "commence_time")}
            od = c.get(f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{e0['id']}/odds",
                       params={"apiKey": key, "regions": "us,eu",
                               "markets": NRFI_MARKET, "oddsFormat": "decimal"})
            out["odds_status"] = od.status_code
            if od.status_code != 200:
                # 422 = market not available / not on your plan; 401 = bad key
                out["odds_error_body"] = od.text[:500]
                return out
            data = od.json()
            markets_seen = sorted({m.get("key")
                                   for b in data.get("bookmakers", [])
                                   for m in b.get("markets", [])})
            out["n_bookmakers"] = len(data.get("bookmakers", []))
            out["markets_returned"] = markets_seen
            out["nrfi_prices_parsed"] = _extract_nrfi_prices(data)
    except Exception as exc:
        out["exception"] = f"{type(exc).__name__}: {exc}"
    return out
