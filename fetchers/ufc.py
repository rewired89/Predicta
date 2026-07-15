"""
UFC data scraper — fightmatrix.com (rankings) + tapology.com (records, bio,
fight history). Switched 2026-07-14 from ufcstats.com after live-testing on
Railway showed ufcstats.com serves a JavaScript proof-of-work anti-bot
challenge ("Checking your browser…") to any plain HTTP client — a wall this
scraper cannot and will not try to solve (that's circumventing a site's
explicit anti-bot security measure, not just reading a public page). See
CLAUDE.md's UFC section for the full story.

Neither fightmatrix.com nor tapology.com has been live-verified from this
session either — this repo's dev sandbox can't reach ANY external site
(confirmed for espn.com, ufcstats.com, and now these two as well), and
unlike ufcstats.com's exact CSS classes (which were at least confidently
known before turning out to be blocked), the precise markup of these two
sites was never memorized with high confidence to begin with. To reduce
how much can go wrong on the first live test, fighter-profile links are
found by URL PATTERN (a distinctive path segment like
"/fightcenter/fighters/12345-name") rather than by guessing CSS class
names — path patterns tend to survive markup/redesign changes better than
class names do. diagnose() is built in from the start (not bolted on after
a failure, like it was for ufcstats.com) so the first real test surfaces
the actual page content immediately.

Data available here is deliberately less rich than ufcstats.com would have
been: FightMatrix gives an Elo-style ranking (used the same way
analyze_esports.py turns a world ranking into a rating — see
models/ufc_model.py), Tapology gives W-L-D record, height/reach/age, and
fight history with method-of-victory. Neither exposes ufcstats.com-level
per-minute striking/grappling stats (SLpM, TD accuracy, etc.), so
models/ufc_model.py's stats-based win-probability signal now runs on
ranking + reach + age instead.
"""
from __future__ import annotations
import re
from datetime import datetime
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
    a fighter has zero fights of that outcome type."""
    wins = [f for f in fights if f["result"] == "W"]
    losses = [f for f in fights if f["result"] == "L"]

    def _share(items: list[dict], keyword: str) -> Optional[float]:
        if not items:
            return None
        n = sum(1 for f in items if keyword in f["method"].lower())
        return n / len(items)

    return {
        "win_ko_rate":  _share(wins, "ko"),
        "win_sub_rate": _share(wins, "submission"),
        "loss_ko_rate":  _share(losses, "ko"),
        "loss_sub_rate": _share(losses, "submission"),
        "n_wins": len(wins), "n_losses": len(losses),
    }


def enrich_ufc_fighters(name_a: str, name_b: str) -> dict:
    """
    Main entry point. PRIMARY source is ESPN (added 2026-07-15, added after
    FightMatrix's ranking table turned out to be JS-rendered and Tapology
    turned out Cloudflare-blocked) — resolves each fighter against ESPN's
    /athletes listing for bio (age, reach, W-L-D). FightMatrix ranking is
    still layered in as a supplementary signal when it resolves (its lookup
    doesn't depend on the broken ranking-table scrape — the still-untested
    question is whether ESPN's own sport/league slug guess is even right;
    see diagnose()). A fighter's dict is {} only when ESPN also fails to
    resolve them. Tapology stays disabled (confirmed Cloudflare-blocked).
    """
    result: dict = {"a": {}, "b": {}}
    espn_athletes = fetch_espn_athletes()
    for key, name in (("a", name_a), ("b", name_b)):
        profile: dict = {}

        espn_match = lookup_espn_athlete(name, espn_athletes)
        if espn_match and espn_match.get("id"):
            bio = fetch_espn_athlete_bio(espn_match["id"])
            if bio:
                profile.update(bio)

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
        out["espn_league_discovery"] = discover_espn_leagues("mma")
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
