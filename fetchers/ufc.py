"""
UFC data scraper. History: ufcstats.com (JS anti-bot proof-of-work
challenge, declined to solve) → fightmatrix.com + tapology.com (Tapology
confirmed Cloudflare-blocked; FightMatrix's ranking table confirmed
JS-rendered, not in static HTML) → ESPN (2026-07-15, per direct user
request) → ESPN's /athletes confirmed dead, /scoreboard confirmed live but
its historical-range query behavior was still unconfirmed after hours of
live-testing → Sherdog + Wikipedia fallback (2026-07-15) → Wikipedia
dropped same day, per direct user request ("real-time stats, not
non-updated shit from Wikipedia") — Sherdog is now the ONLY fight-history
source, no fallback. See CLAUDE.md's UFC section for the full story.

CURRENT PRIMARY (AND ONLY) FIGHT-HISTORY SOURCE: sherdog.com — the
dedicated MMA stats database most public MMA-scraping projects target;
plain server-rendered HTML, not a JS SPA like FightMatrix, no known
Cloudflare/bot-wall like Tapology. Not live-verified from this sandbox
(same limitation as every source in this file — this repo's dev
environment can't reach any external site), so it's built diagnostic-first
(see diagnose()) and leans on best-effort TEXT PATTERN extraction
(numbers/keywords) rather than assuming exact CSS classes, the same
resilience approach already used for Tapology.

If Sherdog doesn't resolve BOTH fighters' fight history, enrich_ufc_fighters
reports `sherdog_resolved: False` on the missing side and
run_ufc_analysis (analyze_ufc.py) halts with an explicit "Unable to fetch
data" response rather than quietly proceeding on FightMatrix ranking alone
— no fallback source, by design, per direct user request.

FightMatrix ranking stays layered in as a supplementary signal for
models/ufc_model.py's stats_win_prob (its own lookup doesn't depend on the
broken ranking-table scrape) — but only ever a supplement to real Sherdog
data, never a substitute for it. ESPN's fetch functions
(fetch_espn_athletes, fetch_espn_scoreboard_range, etc.), Tapology's
functions, and fetch_wikipedia_mma_record/_wikipedia_search are LEFT IN THE
FILE but NOT called from enrich_ufc_fighters — parked in case a future fix
or a future ask brings one of them back, not deleted, per this repo's
"don't erase work that might matter later" convention.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

FIGHTMATRIX_BASE = "https://www.fightmatrix.com"
TAPOLOGY_BASE = "https://www.tapology.com"

# Sherdog + Wikipedia — added 2026-07-15 as the new PRIMARY sources, after
# hours of live-testing left ESPN's /scoreboard endpoint's historical-range
# behavior still unconfirmed (see module docstring / CLAUDE.md). Sherdog is
# the dedicated MMA stats database most public scraping projects target;
# Wikipedia is the fallback precisely because it CANNOT be bot-walled (it
# has an official API built for exactly this kind of read access).
SHERDOG_BASE = "https://www.sherdog.com"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
_SHERDOG_PROFILE_RE = re.compile(r"/fighter/[^\"'>\s]+-\d+")

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


def _parse_espn_mma_events(data: dict, cutoff) -> list[dict]:
    """Extract completed-bout entries from one /scoreboard response payload.
    Shared by fetch_espn_scoreboard_range's chunked calls."""
    out = []
    if not data:
        return out
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


def fetch_espn_scoreboard_range(
    days_back: int = 400, before_date: Optional[str] = None, chunk_days: int = 90,
) -> list[dict]:
    """
    Pull completed UFC bouts from ESPN's /scoreboard across a date range —
    ports the exact same architecture fix that rescued rugby's broken
    per-team endpoint: /athletes (fighter bio/profile lookups) is confirmed
    404 on both the "ufc" slug and the numeric league id 3321 (see
    diagnose()), but /scoreboard is CONFIRMED working (200, real event data)
    with the "ufc" slug. So fighter data here is derived from aggregating
    real fight results league-wide, not from a per-fighter profile call.

    FIXED 2026-07-15 (live-tested via /ufc-diag): a single one-shot
    dates=START-END call spanning the full days_back window came back with
    ZERO events, even though the exact same endpoint with NO dates param at
    all returned a real event. Rather than guess why a wide range breaks,
    this now chunks the window into `chunk_days`-sized pieces (default 90
    — the exact size fetchers/tennis.py:_espn_recent_matches already
    proved works for ESPN's individual-athlete scoreboards) and aggregates
    across them, mirroring tennis's proven windowing instead of rugby's
    unproven-for-this-endpoint single wide range.

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
    Empty list on failure (or if ESPN genuinely has no historical MMA data
    behind this endpoint — see diagnose()'s per-chunk event counts).
    """
    cutoff = None
    if before_date:
        try:
            cutoff = datetime.strptime(before_date[:10], "%Y-%m-%d").date()
        except ValueError:
            cutoff = None

    end = datetime.now(timezone.utc).date()
    overall_start = end - timedelta(days=days_back)
    out: list[dict] = []
    seen = set()
    window_end = end
    while window_end > overall_start:
        window_start = max(overall_start, window_end - timedelta(days=chunk_days))
        data = _espn_get("/scoreboard", {
            "dates": f"{window_start.strftime('%Y%m%d')}-{window_end.strftime('%Y%m%d')}",
        })
        for fight in _parse_espn_mma_events(data, cutoff):
            key = (fight["date"], fight["fighter_a_id"], fight["fighter_b_id"])
            if key not in seen:
                seen.add(key)
                out.append(fight)
        window_end = window_start
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


# ── Sherdog: PRIMARY — record, bio, fight history with method-of-victory ────

def search_sherdog_fighter(name: str) -> Optional[dict]:
    """
    Search sherdog.com for a fighter profile link. Finds candidates by URL
    PATTERN (/fighter/{Name-With-Dashes}-{id}) rather than a guessed CSS
    class, same convention this file already uses for FightMatrix/Tapology
    (path patterns survive markup/redesign changes better than class
    names). Returns {name, url} for the best fuzzy match, or None.
    """
    html = _get(f"{SHERDOG_BASE}/search", {"q": name})
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()
    for a in soup.find_all("a", href=_SHERDOG_PROFILE_RE):
        href = a.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        text = a.get_text(strip=True)
        if text:
            candidates.append({"name": text, "url": href if href.startswith("http") else f"{SHERDOG_BASE}{href}"})
    if not candidates:
        return None
    for c in candidates:
        if c["name"].lower() == name.lower().strip():
            return c
    for c in candidates:
        if _name_matches(name, c["name"]):
            return c
    return candidates[0]


def fetch_sherdog_profile(url: str) -> dict:
    """
    Scrape a Sherdog fighter profile for record + bio. Uses best-effort TEXT
    PATTERN matching (numbers/keyword regexes over the page's plain text)
    rather than assuming exact CSS classes, since Sherdog's markup hasn't
    been live-verified from this sandbox — same resilience approach already
    used for fetch_tapology_profile. Individual fields are None when their
    pattern isn't found rather than guessed. Returns {} on failure. Keys:
    name, wins, losses, draws, height_in, reach_in, age.
    """
    html = _get(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    name_el = soup.find(attrs={"itemprop": "name"}) or soup.find("h1") or soup.find("title")
    name = name_el.get_text(strip=True) if name_el else ""

    wins = losses = draws = None
    m = re.search(r"\b(\d{1,3})-(\d{1,3})-(\d{1,3})\b", text)
    if m:
        wins, losses, draws = int(m.group(1)), int(m.group(2)), int(m.group(3))

    height_in = None
    m = re.search(r"HEIGHT[:\s]*(\d)['’]\s*(\d{1,2})", text, re.IGNORECASE)
    if m:
        height_in = int(m.group(1)) * 12 + int(m.group(2))

    reach_in = None
    m = re.search(r'REACH[:\s]*(\d{2,3})\s*"?', text, re.IGNORECASE)
    if m:
        reach_in = float(m.group(1))

    age = None
    m = re.search(r"AGE[:\s]*(\d{2})", text, re.IGNORECASE)
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


def fetch_sherdog_fight_history(url: str, limit: int = 15) -> list[dict]:
    """
    Extract a fighter's fight history from their Sherdog profile page.
    Tries a table whose header row mentions "method" or "opponent" first
    (flexible header matching rather than an exact class name, since the
    real markup hasn't been live-verified); falls back to the same
    "Win/Loss ... Method" text-pattern scan fetch_tapology_fight_history
    already uses if no such table is found. Returns [{result, opponent,
    method}]. Empty list on failure.
    """
    html = _get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    fights = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = rows[0].get_text(" ", strip=True).lower()
        if "method" not in header_text and "opponent" not in header_text:
            continue
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            cell_texts = [c.get_text(" ", strip=True) for c in cells]
            result_text = cell_texts[0].lower()
            if "win" in result_text:
                result = "W"
            elif "los" in result_text:
                result = "L"
            else:
                continue
            opponent = cell_texts[1] if len(cell_texts) > 1 else ""
            method = next(
                (c for c in cell_texts[2:] if re.search(r"decision|submission|\bko\b|\btko\b|dq", c, re.IGNORECASE)),
                "",
            )
            fights.append({"result": result, "opponent": opponent, "method": method})
        if fights:
            break
    if not fights:
        text = soup.get_text(" ", strip=True)
        for m in re.finditer(
            r"\b(Win|Loss)\b[^.]{0,60}?\b(Decision|Submission|KO|TKO|DQ)\b",
            text, re.IGNORECASE,
        ):
            result = "W" if m.group(1).lower() == "win" else "L"
            fights.append({"result": result, "method": m.group(2)})
    return fights[:limit]


# ── Wikipedia: fallback — guaranteed reachable, structured record table ─────

def _wikipedia_search(name: str) -> Optional[str]:
    """Find the best-matching Wikipedia page title for a fighter name via
    the official search API. Returns None on failure or no results."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(WIKIPEDIA_API, params={
                "action": "query", "list": "search",
                "srsearch": f"{name} mixed martial artist",
                "format": "json", "srlimit": 5,
            })
            resp.raise_for_status()
            results = resp.json().get("query", {}).get("search", [])
            return results[0].get("title") if results else None
    except Exception:
        return None


def fetch_wikipedia_mma_record(name: str) -> list[dict]:
    """
    Fetch a fighter's MMA fight history from their Wikipedia page. Wikipedia
    is not anti-bot-blocked (it has an official, scraping-friendly Action
    API) — used here specifically as the source that CAN'T hit the same
    Cloudflare/JS-challenge walls that killed ufcstats.com/Tapology and the
    JS-rendering issue that killed FightMatrix's ranking table. Most UFC
    fighter pages carry a "Mixed martial arts record" wikitable via a
    common template, but this parses it via flexible header-name matching
    (not an assumed fixed column order) since the exact table hasn't been
    live-verified from this sandbox. Returns [{result: 'W'|'L'|'D'|'NC',
    opponent, method, event, date}]. Empty list on failure or if no record
    table is found (e.g. the fighter has no Wikipedia page).
    """
    title = _wikipedia_search(name)
    if not title:
        return []
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(WIKIPEDIA_API, params={
                "action": "parse", "page": title, "prop": "text", "format": "json",
            })
            resp.raise_for_status()
            html = resp.json().get("parse", {}).get("text", {}).get("*", "")
    except Exception:
        return []
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    heading = None
    for tag in soup.find_all(["h2", "h3"]):
        if "mixed martial arts record" in tag.get_text(" ", strip=True).lower():
            heading = tag
            break

    table = None
    if heading is not None:
        node = heading.find_next(["table", "h2", "h3"])
        if node is not None and node.name == "table":
            table = node
    if table is None:
        table = soup.find("table", class_="wikitable")
    if table is None:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []
    header_cells = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]

    def _col(name_frag: str) -> Optional[int]:
        for i, h in enumerate(header_cells):
            if name_frag in h:
                return i
        return None

    res_i, opp_i = _col("res"), _col("opponent")
    method_i, event_i, date_i = _col("method"), _col("event"), _col("date")

    fights = []
    for row in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) < 3:
            continue

        def _get(i: Optional[int]) -> str:
            return cells[i] if i is not None and i < len(cells) else ""

        res_text = _get(res_i).lower()
        if res_text.startswith("win"):
            result = "W"
        elif res_text.startswith("loss") or res_text.startswith("lose"):
            result = "L"
        elif res_text.startswith("draw"):
            result = "D"
        elif res_text.startswith("nc") or "no contest" in res_text:
            result = "NC"
        else:
            continue
        fights.append({
            "result": result, "opponent": _get(opp_i),
            "method": _get(method_i), "event": _get(event_i), "date": _get(date_i),
        })
    return fights


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
    Main entry point. REWRITTEN 2026-07-15 (third rewrite same day) — per
    direct user request, Wikipedia is dropped as a fallback ("real-time
    stats, not non-updated shit from Wikipedia"). Sherdog is now the ONLY
    fight-history source, no fallback:

      Sherdog — search_sherdog_fighter → fetch_sherdog_profile (bio:
      wins/losses/draws/height/reach/age) + fetch_sherdog_fight_history
      (per-fight result/opponent/method, feeds _method_rates).

    Every returned profile carries `sherdog_resolved: bool` — True only if
    Sherdog both found the fighter AND returned at least one parseable
    fight (after the leakage-exclusion below). run_ufc_analysis
    (analyze_ufc.py) checks this flag directly and halts with an explicit
    "Unable to fetch data" response when either fighter's is False, rather
    than quietly proceeding on FightMatrix ranking alone — no silent
    fallback, by design.

    FightMatrix ranking is still layered in as a supplementary signal for
    models/ufc_model.py:stats_win_prob, but only ever on top of real
    Sherdog data — never a substitute for it. fetch_wikipedia_mma_record
    stays in the file (parked, not deleted) but is not called here. Tapology
    and ESPN also stay disabled/unused (see module docstring).

    `before_date` is accepted for call-site compatibility (analyze_ufc.py
    passes match_date) but Sherdog doesn't support a date-filtered query —
    the leakage guard here instead excludes any fight against
    `name_b`/`name_a` directly from each other's history (see below), which
    is actually more precise than a date cutoff for this specific case.
    """
    result: dict = {"a": {}, "b": {}}
    opponent_of = {"a": name_b, "b": name_a}
    for key, name in (("a", name_a), ("b", name_b)):
        profile: dict = {}

        sherdog_match = search_sherdog_fighter(name)
        fights: list[dict] = []
        if sherdog_match:
            bio = fetch_sherdog_profile(sherdog_match["url"])
            if bio:
                profile.update(bio)
            fights = fetch_sherdog_fight_history(sherdog_match["url"])

        # Data-leakage guard (same class as rugby's before_date exclusion):
        # if these two fighters already fought and that bout is sitting in
        # the career history returned above (e.g. a same-day query made
        # after the result is in), its own outcome/method shouldn't feed
        # the "prediction" of itself. Match by opponent name rather than by
        # date since Sherdog isn't queried with a date filter here.
        opponent = opponent_of[key]
        fights = [f for f in fights if not _name_matches(opponent, f.get("opponent") or "")]

        profile["sherdog_resolved"] = bool(sherdog_match) and bool(fights)

        if fights:
            if profile.get("wins") is None:
                profile["wins"] = sum(1 for f in fights if f["result"] == "W")
                profile["losses"] = sum(1 for f in fights if f["result"] == "L")
                profile["draws"] = sum(1 for f in fights if f["result"] == "D")
            profile.update(_method_rates(fights))
            profile["fight_history_count"] = len(fights)
            profile.setdefault("name", name)

        fm_match = lookup_fightmatrix_fighter(name)
        if fm_match:
            profile["fm_rank"] = fm_match.get("rank")
            profile["fm_rating"] = fm_match.get("rating")
            profile.setdefault("name", fm_match["name"])

        # Always store the profile (even if Sherdog found nothing) so
        # sherdog_resolved is visible to the caller — an empty {} would
        # hide WHY there's no data, same "diagnose don't guess" spirit as
        # the rest of this file.
        result[key] = profile
    return result


def diagnose(sample_fighter: str = "Jon Jones") -> dict:
    """
    One-shot diagnostic. REORDERED 2026-07-15 (second time same day) to
    test Sherdog FIRST — the sole fight-history source as of the same day's
    third rewrite (Wikipedia was tried as a fallback, then dropped per
    direct user request: "real-time stats, not non-updated shit from
    Wikipedia"). Same "test the URL before trusting it" approach used
    throughout this file: raw status/content checks before any parsed-data
    assertions. The Wikipedia probe below is KEPT for reference only (it's
    parked, not called from enrich_ufc_fighters anymore); ESPN/FightMatrix/
    Tapology probes are likewise kept for reference — see module docstring.
    """
    out: dict = {
        "sherdog_base": SHERDOG_BASE, "wikipedia_api": WIKIPEDIA_API,
        "espn_base": ESPN_BASE,
        "fightmatrix_base": FIGHTMATRIX_BASE, "tapology_base": TAPOLOGY_BASE,
    }

    # 0. Sherdog search + profile + fight history (NEW PRIMARY)
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(f"{SHERDOG_BASE}/search", params={"q": sample_fighter})
            out["sherdog_search_status"] = resp.status_code
            out["sherdog_search_url"] = str(resp.url)
            out["sherdog_content_length"] = len(resp.text)
            out["sherdog_has_profile_links"] = bool(_SHERDOG_PROFILE_RE.search(resp.text))
            if resp.status_code != 200 or not out["sherdog_has_profile_links"]:
                out["sherdog_raw_snippet"] = resp.text[:1500]
    except Exception as exc:
        out["sherdog_search_exception"] = str(exc)

    try:
        match = search_sherdog_fighter(sample_fighter)
        out["sherdog_sample_matched"] = match
        if match:
            out["sherdog_parsed_profile"] = fetch_sherdog_profile(match["url"])
            out["sherdog_parsed_history_sample"] = fetch_sherdog_fight_history(match["url"], limit=5)
    except Exception as exc:
        out["sherdog_parse_exception"] = str(exc)

    # 0a. Wikipedia record table (NEW FALLBACK — can't be bot-walled)
    try:
        title = _wikipedia_search(sample_fighter)
        out["wikipedia_matched_title"] = title
        if title:
            fights = fetch_wikipedia_mma_record(sample_fighter)
            out["wikipedia_parsed_fight_count"] = len(fights)
            out["wikipedia_parsed_sample"] = fights[:5]
    except Exception as exc:
        out["wikipedia_exception"] = str(exc)

    # 1. ESPN /athletes probe (PARKED — no longer called from
    # enrich_ufc_fighters, kept here only in case a future ESPN fix makes
    # it worth revisiting)
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
                        entry = {
                            "path": str(path), "status": resp.status_code,
                            "looks_ok": resp.status_code == 200,
                            "event_count": None, "body_snippet": None,
                        }
                        if resp.status_code == 200:
                            body = resp.json()
                            events = body.get("events", [])
                            entry["event_count"] = len(events)
                            # FIXED 2026-07-15: previously only counted events
                            # here, so we never actually SAW what this "1
                            # event" the no-dates-param call finds really is
                            # (upcoming vs final, real MMA bout vs something
                            # else) — capture it directly instead of guessing.
                            entry["events_sample"] = [
                                {
                                    "name": ev.get("name"),
                                    "date": ev.get("date"),
                                    "status": (ev.get("competitions") or [{}])[0]
                                                .get("status", {}).get("type", {}).get("name"),
                                    "competitor_names": [
                                        (c.get("athlete") or c.get("team") or {}).get("displayName")
                                        for c in (ev.get("competitions") or [{}])[0].get("competitors", [])
                                    ],
                                }
                                for ev in events[:5]
                            ]
                        else:
                            entry["body_snippet"] = resp.text[:300]
                        out[f"espn_scoreboard_probe_{label}"] = entry
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

    # 0c. FIXED 2026-07-15 (live-tested): a single one-shot dates=START-END
    # call spanning ~400 days came back with ZERO events even though the
    # SAME endpoint with no dates param at all found a real event —
    # fetch_espn_scoreboard_range switched to chunked ~90-day windows to
    # work around this (see its docstring). Report per-chunk raw counts +
    # a raw body snippet for the most recent chunk here so if chunking
    # still comes back empty, the actual response body (not just a count)
    # is visible without yet another round trip.
    chunk_probe = []
    today = datetime.now(timezone.utc).date()
    for weeks_back in (0, 13, 26, 39, 52):
        w_end = today - timedelta(days=weeks_back * 7)
        w_start = w_end - timedelta(days=90)
        try:
            with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                url = f"{ESPN_BASE}/scoreboard"
                params = {"dates": f"{w_start.strftime('%Y%m%d')}-{w_end.strftime('%Y%m%d')}"}
                resp = client.get(url, params=params)
                entry = {
                    "window": f"{w_start.isoformat()}..{w_end.isoformat()}",
                    "status": resp.status_code,
                }
                if resp.status_code == 200:
                    body = resp.json()
                    entry["event_count"] = len(body.get("events", []))
                    entry["top_level_keys"] = list(body.keys())
                    if entry["event_count"] == 0:
                        entry["raw_body_snippet"] = resp.text[:400]
                else:
                    entry["body_snippet"] = resp.text[:300]
                chunk_probe.append(entry)
        except Exception as exc:
            chunk_probe.append({"window": f"{w_start.isoformat()}..{w_end.isoformat()}", "exception": str(exc)})
    out["espn_scoreboard_chunk_probe"] = chunk_probe

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
