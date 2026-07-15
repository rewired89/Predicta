"""
UFC data scraper. History: ufcstats.com (JS anti-bot proof-of-work
challenge, declined to solve) → fightmatrix.com + tapology.com (Tapology
confirmed Cloudflare-blocked; FightMatrix's ranking table confirmed
JS-rendered, not in static HTML) → ESPN (2026-07-15, per direct user
request — "Did we have ESPN's MMA all along?"). See CLAUDE.md's UFC
section for the full story.

CURRENT PRIMARY SOURCE (2026-07-15): ESPN's /scoreboard endpoint, NOT
/athletes. Live testing found /athletes (fighter bio/profile lookups)
404s for MMA on both the "ufc" slug and the numeric league id (3321) —
ESPN's site API appears to simply not expose per-fighter profiles for MMA.
/scoreboard (event/fight results), by contrast, is CONFIRMED working (200,
real event data) with the "ufc" slug. So fighter data is derived by
aggregating real fight results league-wide (fetch_espn_scoreboard_range)
and deriving each fighter's record + finish-method rates from it
client-side (_fighter_fights_from_scoreboard, _method_rates) — the same
architecture fix that rescued rugby's broken per-team /schedule endpoint
in fetchers/rugby.py.

This gives real win/loss record + method-of-victory rates, but NOT
reach/age bio data (ESPN has no working bio endpoint for MMA). FightMatrix
ranking is layered in as a supplementary signal (its own lookup doesn't
depend on the broken ranking-table scrape) for models/ufc_model.py's
stats_win_prob, which degrades gracefully to ranking + Glicko-2 alone when
reach/age are None. Tapology's call path stays disabled (confirmed
Cloudflare-blocked, 403 "Just a moment…").
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

FIGHTMATRIX_BASE = "https://www.fightmatrix.com"
TAPOLOGY_BASE = "https://www.tapology.com"

# ESPN — added 2026-07-15 as the new PRIMARY source, after FightMatrix's
# ranking table turned out to be JS-rendered (not in static HTML) and
# Tapology turned out to be Cloudflare-blocked. ESPN's site API is already
# proven reliable in this repo for baseball/soccer/tennis/rugby, but the
# exact sport/league slug for MMA has NOT been live-verified — same
# situation the rugby build was in before discovering the real code was a
# numeric ID, not "nrl". "mma"/"ufc" below is a guess based on ESPN's site
# structure (espn.com/mma/), tested via diagnose() with core-API discovery
# as a fallback rather than assumed correct.
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc"
CORE_API_BASE = "https://sports.core.api.espn.com/v2/sports"
_ESPN_CANDIDATE_SLUGS = [("mma", "ufc"), ("mma", "mma"), ("ufc", "ufc")]

TIMEOUT = 20.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_FM_PROFILE_RE = re.compile(r"/fighter-profile/[^\"'>\s]+")
_TAP_PROFILE_RE = re.compile(r"/fightcenter/fighters/\d+-[^\"'>\s]+")


def _get(url: str, params: Optional[dict] = None) -> str:
    """GET a page, return HTML string or '' on any failure."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(url, params=params or {})
            r.raise_for_status()
            return r.text
    except Exception:
        return ""


def _f(val, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    s = str(val).strip().rstrip("%").rstrip('"')
    if not s or s == "--":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _espn_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET from ESPN's site API. Returns None on any failure (network, 4xx/5xx, bad JSON)."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{ESPN_BASE}{path}", params=params or {})
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def fetch_espn_athletes(pages: int = 3) -> list[dict]:
    """
    Paginated /athletes listing — same pattern fetchers/tennis.py already
    uses successfully for ATP/WTA. Returns [{id, name}]; empty list on
    failure or if the sport/league slug is wrong.
    """
    out = []
    for page in range(1, pages + 1):
        data = _espn_get("/athletes", {"limit": 100, "page": page})
        if not data:
            break
        athletes = data.get("athletes") or []
        if not athletes:
            break
        for a in athletes:
            name = a.get("displayName", "")
            if name:
                out.append({"id": a.get("id"), "name": name})
    return out


def lookup_espn_athlete(name: str, athletes: Optional[list[dict]] = None) -> Optional[dict]:
    """Fuzzy-match a fighter name against fetch_espn_athletes()."""
    athletes = athletes if athletes is not None else fetch_espn_athletes()
    if not athletes:
        return None
    for a in athletes:
        if a["name"].lower() == name.lower().strip():
            return a
    names = [a["name"].lower() for a in athletes]
    import difflib
    close = difflib.get_close_matches(name.lower().strip(), names, n=1, cutoff=0.6)
    if close:
        return next((a for a in athletes if a["name"].lower() == close[0]), None)
    return None


def fetch_espn_athlete_bio(athlete_id: str) -> dict:
    """
    /athletes/{id} — bio fields (height, weight, DOB → age, etc.). Field
    names are a best guess mirroring ESPN's standard athlete object shape
    used elsewhere in this repo (fetchers/tennis.py); not live-verified for
    MMA specifically. Returns {} on failure.
    """
    data = _espn_get(f"/athletes/{athlete_id}")
    if not data:
        return {}
    athlete = data.get("athlete", data)
    age = None
    dob = athlete.get("dateOfBirth")
    if dob:
        try:
            age = (datetime.now() - datetime.strptime(dob[:10], "%Y-%m-%d")).days / 365.25
        except Exception:
            age = None
    reach_in = None
    for stat_key in ("reach", "reachIn"):
        if athlete.get(stat_key):
            reach_in = _f(athlete[stat_key])
            break
    return {
        "name": athlete.get("displayName", ""),
        "age": age,
        "reach_in": reach_in,
        "wins": (athlete.get("wins") or {}).get("value") if isinstance(athlete.get("wins"), dict) else athlete.get("wins"),
        "losses": (athlete.get("losses") or {}).get("value") if isinstance(athlete.get("losses"), dict) else athlete.get("losses"),
        "draws": (athlete.get("draws") or {}).get("value") if isinstance(athlete.get("draws"), dict) else athlete.get("draws"),
    }


_METHOD_RE = re.compile(r"\b(KO/TKO|TKO|KO|Submission|Decision|DQ)\b", re.IGNORECASE)


def _extract_method(text: str) -> Optional[str]:
    """Pull a finish method out of ESPN's shortDetail text (e.g. 'Final -
    Submission, Rd 2, 2:34'). Returns None when no recognizable method
    keyword is present, rather than guessing."""
    if not text:
        return None
    m = _METHOD_RE.search(text)
    return m.group(1) if m else None


def fetch_espn_scoreboard_range(days_back: int = 400, before_date: Optional[str] = None) -> list[dict]:
    """
    Pull completed UFC bouts from ESPN's /scoreboard across a date range —
    ports the exact same architecture fix that rescued rugby's broken
    per-team endpoint: /athletes (fighter bio/profile lookups) is confirmed
    404 on both the "ufc" slug and the numeric league id 3321 (see
    diagnose()), but /scoreboard is CONFIRMED working (200, real event data)
    with the "ufc" slug. So fighter data here is derived from aggregating
    real fight results league-wide, not from a per-fighter profile call.

    Each ESPN "competition" under an MMA event is a single bout between two
    individual athletes — same competitor shape fetchers/tennis.py already
    parses successfully for ATP/WTA (competitor["athlete"]["displayName"],
    comp["winnerId"]), reused here rather than guessed fresh.

    days_back defaults to 400 (vs rugby's 200) because UFC fighters
    typically compete only 2-3 times a year — a shorter window would starve
    most fighters of any fight history at all.

    `before_date` ('YYYY-MM-DD', the match being predicted) excludes events
    on or after that calendar day — same data-leakage guard as rugby's
    fetch_scoreboard_range (an already-finished same-day fight shouldn't
    feed the "prediction" of itself).

    Each entry: {date, event_name, fighter_a_id, fighter_a_name,
    fighter_b_id, fighter_b_name, winner_id, method, short_detail}.
    Empty list on failure.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    data = _espn_get("/scoreboard", {
        "dates": f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}",
        "limit": 300,
    })
    if not data:
        return []
    cutoff = None
    if before_date:
        try:
            cutoff = datetime.strptime(before_date[:10], "%Y-%m-%d").date()
        except ValueError:
            cutoff = None

    out = []
    for ev in data.get("events") or []:
        event_name = ev.get("name", "") or ev.get("shortName", "")
        ev_date_str = (ev.get("date") or "")[:10]
        if cutoff is not None:
            try:
                if datetime.strptime(ev_date_str, "%Y-%m-%d").date() >= cutoff:
                    continue
            except ValueError:
                pass
        for comp in ev.get("competitions") or []:
            status = comp.get("status", {}).get("type", {}).get("name", "")
            if status != "STATUS_FINAL":
                continue
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            a, b = competitors[0], competitors[1]
            a_athlete = a.get("athlete") or a.get("team") or {}
            b_athlete = b.get("athlete") or b.get("team") or {}
            a_name = a_athlete.get("displayName", "")
            b_name = b_athlete.get("displayName", "")
            if not a_name or not b_name:
                continue

            winner_id = comp.get("winnerId")
            if winner_id is None:
                winner_id = next((c.get("id") for c in competitors if c.get("winner") is True), None)

            short_detail = comp.get("status", {}).get("type", {}).get("shortDetail", "") or ""
            out.append({
                "date": ev_date_str,
                "event_name": event_name,
                "fighter_a_id": str(a.get("id") or a_athlete.get("id") or ""),
                "fighter_a_name": a_name,
                "fighter_b_id": str(b.get("id") or b_athlete.get("id") or ""),
                "fighter_b_name": b_name,
                "winner_id": str(winner_id) if winner_id is not None else None,
                "method": _extract_method(short_detail),
                "short_detail": short_detail,
            })
    return out


def _fighter_fights_from_scoreboard(name: str, events: list[dict], limit: int = 20) -> list[dict]:
    """
    Filter a shared fetch_espn_scoreboard_range() result down to one
    fighter's bouts, matched by name, most recent first:
    [{date, opponent, result: 'W'|'L', method}].

    Bouts where the winner can't be resolved (winner_id missing — could be
    a draw or no-contest, ESPN doesn't distinguish here) are excluded rather
    than guessed, mirroring this repo's "don't fabricate from ambiguous
    data" convention.
    """
    name_lo = name.lower().strip()
    fights = []
    for ev in events:
        a_lo, b_lo = ev["fighter_a_name"].lower(), ev["fighter_b_name"].lower()
        if name_lo == a_lo or name_lo in a_lo or a_lo in name_lo:
            is_a = True
        elif name_lo == b_lo or name_lo in b_lo or b_lo in name_lo:
            is_a = False
        else:
            continue
        if ev["winner_id"] is None:
            continue
        self_id = ev["fighter_a_id"] if is_a else ev["fighter_b_id"]
        opponent = ev["fighter_b_name"] if is_a else ev["fighter_a_name"]
        result = "W" if ev["winner_id"] == self_id else "L"
        fights.append({
            "date": ev["date"], "opponent": opponent,
            "result": result, "method": ev["method"],
        })
    fights.sort(key=lambda f: f["date"], reverse=True)
    return fights[:limit]


def discover_espn_leagues(sport: str = "mma") -> dict:
    """
    Query ESPN's separate core API for the leagues it knows about under a
    sport — same proven pattern fetchers/rugby.py:discover_leagues used to
    find NRL's real numeric league ID after "nrl" 404'd. Returns
    {sport, status, league_count, leagues: [{slug_from_ref, name, abbreviation}]}.
    """
    out: dict = {"sport": sport}
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{CORE_API_BASE}/{sport}/leagues", params={"limit": 100})
            out["status"] = resp.status_code
            if resp.status_code != 200:
                out["raw_error"] = resp.text[:500]
                return out
            data = resp.json()
            items = data.get("items", [])
            out["league_count"] = len(items)
            leagues = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                ref = item.get("$ref")
                entry = {"ref": ref}
                if ref:
                    slug = ref.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                    entry["slug_from_ref"] = slug
                    try:
                        ref_resp = client.get(ref)
                        if ref_resp.status_code == 200:
                            ref_data = ref_resp.json()
                            entry["name"] = ref_data.get("name")
                            entry["abbreviation"] = ref_data.get("abbreviation")
                            # FIXED 2026-07-15 (same bug class as rugby's ESPN
                            # league-ID fix): the readable slug_from_ref
                            # ("ufc") is NOT what the site API needs — like
                            # rugby-league needing numeric "3" instead of
                            # "nrl", the site API 404s on "mma/ufc" even
                            # though this slug resolves fine here. Capture
                            # the numeric id too so diagnose() can test it.
                            entry["id"] = ref_data.get("id")
                    except Exception as exc:
                        entry["ref_fetch_exception"] = str(exc)
                leagues.append(entry)
            out["leagues"] = leagues
    except Exception as exc:
        out["exception"] = str(exc)
    return out


def _name_matches(query: str, candidate: str) -> bool:
    q, c = query.lower().strip(), candidate.lower().strip()
    if q == c or q in c or c in q:
        return True
    import difflib
    return difflib.SequenceMatcher(None, q, c).ratio() >= 0.72


# ── FightMatrix: Elo-style ranking (secondary signal, not per-fight stats) ────

def fetch_fightmatrix_rankings() -> list[dict]:
    """
    Scrape fightmatrix.com's main rankings page for fighter profile links.
    Finds candidates by URL PATTERN (/fighter-profile/...) rather than a
    guessed CSS class, then best-effort extracts a rank + rating from the
    enclosing table row's text (both may be None if the row shape isn't
    what's expected — the caller treats a fighter with no rating as
    "unranked", not an error).
    Each entry: {name, url, rank, rating}. Empty list on failure.
    """
    html = _get(f"{FIGHTMATRIX_BASE}/mma-ranks/")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen_urls = set()
    for a in soup.find_all("a", href=_FM_PROFILE_RE):
        href = a.get("href", "")
        if href in seen_urls:
            continue
        seen_urls.add(href)
        name = a.get_text(strip=True)
        if not name:
            continue
        row = a.find_parent("tr")
        rank, rating = None, None
        if row:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            for cell in cells:
                if rank is None and re.fullmatch(r"#?\d{1,3}", cell):
                    rank = int(cell.lstrip("#"))
                elif rating is None and re.fullmatch(r"\d{3,4}", cell):
                    rating = int(cell)
        out.append({
            "name": name,
            "url": href if href.startswith("http") else f"{FIGHTMATRIX_BASE}{href}",
            "rank": rank, "rating": rating,
        })
    return out


def lookup_fightmatrix_fighter(name: str, rankings: Optional[list[dict]] = None) -> Optional[dict]:
    """Fuzzy-match a fighter name against fetch_fightmatrix_rankings()."""
    rankings = rankings if rankings is not None else fetch_fightmatrix_rankings()
    if not rankings:
        return None
    for r in rankings:
        if r["name"].lower() == name.lower().strip():
            return r
    for r in rankings:
        if _name_matches(name, r["name"]):
            return r
    return None


# ── Tapology: record, bio, fight history with method-of-victory ─────────────

def search_tapology_fighter(name: str) -> Optional[dict]:
    """
    Search tapology.com for a fighter by name. Finds candidate profile links
    by URL PATTERN (/fightcenter/fighters/{id}-{slug}) rather than a guessed
    CSS class. Returns {name, url} for the best fuzzy-match, or None.
    """
    html = _get(f"{TAPOLOGY_BASE}/search", {"term": name})
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen_urls = set()
    for a in soup.find_all("a", href=_TAP_PROFILE_RE):
        href = a.get("href", "")
        if href in seen_urls:
            continue
        seen_urls.add(href)
        text = a.get_text(strip=True)
        if text:
            candidates.append({"name": text, "url": href if href.startswith("http") else f"{TAPOLOGY_BASE}{href}"})
    if not candidates:
        return None
    for c in candidates:
        if c["name"].lower() == name.lower().strip():
            return c
    for c in candidates:
        if _name_matches(name, c["name"]):
            return c
    return candidates[0]


def fetch_tapology_profile(url: str) -> dict:
    """
    Scrape a Tapology fighter profile for record + bio. Best-effort text
    pattern matching rather than strict CSS selectors, since the exact
    markup hasn't been live-verified — returns {} on failure, and individual
    fields are None when their pattern isn't found rather than guessed.
    Keys: name, wins, losses, draws, height_in, reach_in, age.
    """
    html = _get(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    title_el = soup.find("h1") or soup.find("title")
    name = title_el.get_text(strip=True) if title_el else ""

    wins = losses = draws = None
    m = re.search(r"Pro\s+MMA\s+Record[:\s]*(\d+)-(\d+)-(\d+)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,2})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        wins, losses, draws = int(m.group(1)), int(m.group(2)), int(m.group(3))

    reach_in = None
    m = re.search(r'Reach[:\s]*(\d{2,3})\s*"?', text, re.IGNORECASE)
    if m:
        reach_in = float(m.group(1))

    height_in = None
    m = re.search(r"Height[:\s]*(\d)['’]\s*(\d{1,2})", text, re.IGNORECASE)
    if m:
        height_in = int(m.group(1)) * 12 + int(m.group(2))

    age = None
    m = re.search(r"Age[:\s]*(\d{2})", text, re.IGNORECASE)
    if m:
        age = float(m.group(1))
    if age is None:
        m = re.search(r"\b(19|20)\d{2}[.\-/](0[1-9]|1[0-2])[.\-/](0[1-9]|[12]\d|3[01])\b", text)
        if m:
            try:
                dob = datetime.strptime(m.group(0).replace("/", "-").replace(".", "-"), "%Y-%m-%d")
                age = (datetime.now() - dob).days / 365.25
            except Exception:
                age = None

    return {
        "name": name, "wins": wins, "losses": losses, "draws": draws,
        "height_in": height_in, "reach_in": reach_in, "age": age,
    }


def fetch_tapology_fight_history(url: str, limit: int = 15) -> list[dict]:
    """
    Best-effort extraction of a fighter's fight history from their Tapology
    profile page — scans for "Result via Method" style text patterns
    (e.g. "Win via Submission", "Loss via KO/TKO", "Win via Decision").
    Returns [{result: 'W'|'L', method}]. Empty list if the pattern isn't
    found (fails soft — models/ufc_model.py falls back to league-average
    finish rates when this is empty, same as a fighter with no history).
    """
    html = _get(url)
    if not html:
        return []
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    fights = []
    for m in re.finditer(
        r"\b(Win|Loss)\b[^.]{0,40}?\b(Decision|Submission|KO|TKO|DQ)\b",
        text, re.IGNORECASE,
    ):
        result = "W" if m.group(1).lower() == "win" else "L"
        fights.append({"result": result, "method": m.group(2)})
    return fights[:limit]


def _method_rates(fights: list[dict]) -> dict:
    """Same shape/logic as before — win/loss method shares, None (not 0) when
    a fighter has zero fights of that outcome type. Fights with no resolved
    method (ESPN's shortDetail didn't match a known keyword) are excluded
    from the share's denominator rather than treated as a decision."""
    wins = [f for f in fights if f["result"] == "W"]
    losses = [f for f in fights if f["result"] == "L"]

    def _share(items: list[dict], keyword: str) -> Optional[float]:
        known = [f for f in items if f.get("method")]
        if not known:
            return None
        n = sum(1 for f in known if keyword in f["method"].lower())
        return n / len(known)

    return {
        "win_ko_rate":  _share(wins, "ko"),
        "win_sub_rate": _share(wins, "submission"),
        "loss_ko_rate":  _share(losses, "ko"),
        "loss_sub_rate": _share(losses, "submission"),
        "n_wins": len(wins), "n_losses": len(losses),
    }


def enrich_ufc_fighters(name_a: str, name_b: str, before_date: Optional[str] = None) -> dict:
    """
    Main entry point. PRIMARY source is ESPN's /scoreboard (switched
    2026-07-15, same day as the ESPN-primary switch itself) — NOT /athletes.
    Live testing found ESPN's /athletes endpoint (fighter bio/profile
    lookups) 404s for MMA on both the "ufc" slug and the numeric league id
    3321, while /scoreboard (event/fight results) is CONFIRMED working (200,
    real event data) with the "ufc" slug. Rather than keep depending on the
    dead endpoint, this ports the exact fix that rescued rugby's broken
    per-team /schedule endpoint: pull a shared league-wide /scoreboard range
    once (fetch_espn_scoreboard_range) and derive each fighter's real
    win/loss record + finish-method rates from it client-side
    (_fighter_fights_from_scoreboard + _method_rates), instead of a
    per-fighter profile call.

    This gives a real record and method-of-victory signal (feeds
    models/ufc_model.py:method_of_victory directly) but NOT reach/age bio
    data — ESPN doesn't expose that without a working /athletes endpoint.
    FightMatrix ranking is still layered in as a supplementary signal for
    the win-probability side (models/ufc_model.py:stats_win_prob degrades
    gracefully when reach/age are None, relying on ranking + Glicko-2
    instead). Tapology stays disabled (confirmed Cloudflare-blocked).

    A fighter's dict is {} only when neither ESPN scoreboard history nor
    FightMatrix ranking resolves them.
    """
    result: dict = {"a": {}, "b": {}}
    events = fetch_espn_scoreboard_range(before_date=before_date)
    for key, name in (("a", name_a), ("b", name_b)):
        profile: dict = {}

        fights = _fighter_fights_from_scoreboard(name, events)
        if fights:
            profile["wins"] = sum(1 for f in fights if f["result"] == "W")
            profile["losses"] = sum(1 for f in fights if f["result"] == "L")
            # ESPN's scoreboard doesn't distinguish a draw/no-contest from an
            # unresolved winner_id (excluded above) — reporting 0 rather than
            # guessing at a real draw count.
            profile["draws"] = 0
            profile.update(_method_rates(fights))
            profile["espn_fight_count"] = len(fights)
            profile["name"] = name

        fm_match = lookup_fightmatrix_fighter(name)
        if fm_match:
            profile["fm_rank"] = fm_match.get("rank")
            profile["fm_rating"] = fm_match.get("rating")
            profile.setdefault("name", fm_match["name"])

        if profile:
            result[key] = profile
    return result


def diagnose(sample_fighter: str = "Jon Jones") -> dict:
    """
    One-shot diagnostic. Extended 2026-07-15 to test ESPN FIRST (added as
    the new primary source after FightMatrix's ranking table turned out
    JS-rendered and Tapology turned out Cloudflare-blocked) — same
    "test the URL before trusting it" approach that found and fixed the
    rugby ESPN league-ID bug: probes /athletes with the current ESPN_BASE
    guess, and if that 404s, runs discover_espn_leagues() plus a few
    candidate (sport, league) slugs automatically instead of guessing once
    across another round-trip. Still also reports FightMatrix/Tapology
    status for completeness.
    """
    out: dict = {
        "espn_base": ESPN_BASE,
        "fightmatrix_base": FIGHTMATRIX_BASE, "tapology_base": TAPOLOGY_BASE,
    }

    # 0. ESPN /athletes probe (new primary source)
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{ESPN_BASE}/athletes", params={"limit": 100, "page": 1})
            out["espn_athletes_status"] = resp.status_code
            out["espn_athletes_url"] = str(resp.url)
            if resp.status_code != 200:
                out["espn_athletes_error_body"] = resp.text[:500]
            else:
                data = resp.json()
                out["espn_athletes_top_level_keys"] = list(data.keys())
                out["espn_athletes_count"] = len(data.get("athletes") or [])
    except Exception as exc:
        out["espn_athletes_exception"] = str(exc)

    if out.get("espn_athletes_status") == 404:
        discovery = discover_espn_leagues("mma")
        out["espn_league_discovery"] = discovery
        candidate_results = []
        for sport, league in _ESPN_CANDIDATE_SLUGS:
            try:
                with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/athletes"
                    resp = client.get(url, params={"limit": 10})
                    candidate_results.append({
                        "sport": sport, "league": league, "status": resp.status_code,
                        "looks_ok": resp.status_code == 200,
                    })
            except Exception as exc:
                candidate_results.append({"sport": sport, "league": league, "exception": str(exc)})
        out["espn_candidate_slug_probe"] = candidate_results

        # FIXED 2026-07-15 (same bug class as rugby's ESPN league-ID fix):
        # the readable slug ("ufc") resolves fine via the core API but the
        # site API 404s on it anyway — like rugby-league needing numeric "3"
        # instead of "nrl", the site API likely needs the league's numeric
        # id. Auto-test the UFC entry's numeric id directly instead of
        # guessing again across another round-trip.
        ufc_entry = next(
            (l for l in discovery.get("leagues", []) if l.get("abbreviation") == "UFC" or l.get("slug_from_ref") == "ufc"),
            None,
        )
        out["espn_ufc_league_entry"] = ufc_entry
        if ufc_entry and ufc_entry.get("id"):
            try:
                with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                    url = f"https://site.api.espn.com/apis/site/v2/sports/mma/{ufc_entry['id']}/athletes"
                    resp = client.get(url, params={"limit": 10})
                    out["espn_numeric_id_probe"] = {
                        "id": ufc_entry["id"], "status": resp.status_code,
                        "looks_ok": resp.status_code == 200,
                        "body_snippet": resp.text[:500] if resp.status_code == 200 else resp.text[:300],
                    }
            except Exception as exc:
                out["espn_numeric_id_probe_exception"] = str(exc)

            # /athletes (fighter profiles/rosters) 404ing on both the slug
            # and the numeric id is a different signal than rugby's bug —
            # ESPN may simply not expose per-fighter profiles for MMA at
            # all, even though the league itself is registered. /scoreboard
            # (event schedules/results) is a DIFFERENT endpoint that may
            # still work — worth checking before concluding ESPN has
            # nothing usable here.
            for label, path in (("slug", "ufc"), ("numeric_id", ufc_entry["id"])):
                try:
                    with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                        url = f"https://site.api.espn.com/apis/site/v2/sports/mma/{path}/scoreboard"
                        resp = client.get(url)
                        out[f"espn_scoreboard_probe_{label}"] = {
                            "path": str(path), "status": resp.status_code,
                            "looks_ok": resp.status_code == 200,
                            "event_count": len(resp.json().get("events", [])) if resp.status_code == 200 else None,
                            "body_snippet": resp.text[:300] if resp.status_code != 200 else None,
                        }
                except Exception as exc:
                    out[f"espn_scoreboard_probe_{label}_exception"] = str(exc)

    try:
        espn_athletes = fetch_espn_athletes(pages=1)
        out["espn_parsed_athlete_count"] = len(espn_athletes)
        out["espn_parsed_athlete_sample"] = espn_athletes[:5]
        espn_match = lookup_espn_athlete(sample_fighter, espn_athletes)
        out["espn_sample_matched"] = espn_match
        if espn_match and espn_match.get("id"):
            out["espn_parsed_bio"] = fetch_espn_athlete_bio(espn_match["id"])
    except Exception as exc:
        out["espn_parse_exception"] = str(exc)

    # 0b. Confirm the /scoreboard-based derivation that enrich_ufc_fighters
    # now actually uses (added 2026-07-15, same day /athletes was confirmed
    # dead for MMA) — parses real fight history for sample_fighter instead
    # of just probing raw HTTP status.
    try:
        events = fetch_espn_scoreboard_range()
        out["espn_scoreboard_range_event_count"] = len(events)
        out["espn_scoreboard_range_sample"] = events[:3]
        fights = _fighter_fights_from_scoreboard(sample_fighter, events)
        out["espn_sample_fighter_fights"] = fights
        if fights:
            out["espn_sample_fighter_method_rates"] = _method_rates(fights)
    except Exception as exc:
        out["espn_scoreboard_range_exception"] = str(exc)

    # 1. FightMatrix rankings page
    fm_html = ""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{FIGHTMATRIX_BASE}/mma-ranks/")
            fm_html = resp.text
            out["fm_status"] = resp.status_code
            out["fm_content_length"] = len(resp.text)
            out["fm_has_profile_links"] = bool(_FM_PROFILE_RE.search(resp.text))
            if resp.status_code != 200 or not out["fm_has_profile_links"]:
                out["fm_raw_snippet"] = resp.text[:1500]
    except Exception as exc:
        out["fm_exception"] = str(exc)

    try:
        rankings = fetch_fightmatrix_rankings()
        out["fm_parsed_count"] = len(rankings)
        out["fm_parsed_sample"] = rankings[:5]

        # Suspicious: technically found >=1 profile link, but too few for a
        # real rankings page, or the "name" text looks like a URL (both
        # signs the real ranking table isn't in this static HTML at all —
        # likely rendered client-side via JS). Capture real structural
        # evidence instead of guessing why.
        looks_url = any(r["name"].startswith("http") for r in rankings)
        if fm_html and (len(rankings) < 10 or looks_url):
            soup = BeautifulSoup(fm_html, "html.parser")
            tables = soup.find_all("table")
            out["fm_table_count"] = len(tables)
            out["fm_table_row_counts"] = [len(t.find_all("tr")) for t in tables[:5]]
            out["fm_looks_js_rendered"] = bool(
                re.search(r"\breact\b|\bvue\b|__NEXT_DATA__|application/json", fm_html, re.IGNORECASE)
            )
            # If the table is JS-rendered, it's often populated by a plain
            # JSON/AJAX endpoint referenced in a <script> tag — worth finding
            # directly rather than fighting the rendered HTML.
            api_hints = re.findall(
                r"[\"'](/[^\"'\s]*(?:api|ajax|json|data)[^\"'\s]*)[\"']",
                fm_html, re.IGNORECASE,
            )
            out["fm_possible_api_endpoints"] = list(dict.fromkeys(api_hints))[:10]
            # Raw HTML around each matched anchor, so we can see its real
            # surrounding markup instead of just the extracted (wrong) text.
            anchor_context = []
            for a in soup.find_all("a", href=_FM_PROFILE_RE)[:3]:
                anchor_context.append(str(a.parent)[:400])
            out["fm_anchor_context"] = anchor_context
            # Snippet around the first occurrence of "rank" (case-insensitive)
            # to see whatever static table markup does exist, if any.
            m = re.search(r"rank", fm_html, re.IGNORECASE)
            if m:
                start = max(0, m.start() - 200)
                out["fm_rank_keyword_snippet"] = fm_html[start:start + 800]
    except Exception as exc:
        out["fm_parse_exception"] = str(exc)

    # 2. Tapology search
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{TAPOLOGY_BASE}/search", params={"term": sample_fighter})
            out["tap_search_status"] = resp.status_code
            out["tap_search_url"] = str(resp.url)
            out["tap_content_length"] = len(resp.text)
            out["tap_has_profile_links"] = bool(_TAP_PROFILE_RE.search(resp.text))
            if resp.status_code != 200 or not out["tap_has_profile_links"]:
                out["tap_raw_snippet"] = resp.text[:1500]
    except Exception as exc:
        out["tap_exception"] = str(exc)

    try:
        match = search_tapology_fighter(sample_fighter)
        out["tap_sample_matched"] = match
        if match:
            out["tap_parsed_profile"] = fetch_tapology_profile(match["url"])
            out["tap_parsed_history_sample"] = fetch_tapology_fight_history(match["url"], limit=3)
    except Exception as exc:
        out["tap_parse_exception"] = str(exc)

    return out
