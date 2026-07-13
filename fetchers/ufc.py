"""
UFC data scraper — ufcstats.com.

Unlike ESPN (used by fetchers/baseball.py, fetchers/rugby.py, etc.), UFC Stats
has no JSON API at all, official or hidden — every scraper (including the
well-known public ones this repo's approach mirrors) reads the site's plain
HTML tables directly with requests+BeautifulSoup. There's no Cloudflare wall
here (unlike FanGraphs/BoxRec) — same "clean HTML, no rate limiting" reasoning
that made Kimi recommend UFC as more tractable than Boxing.

NOTE: this repo's remote build/test containers cannot reach external sites at
all (confirmed for both espn.com and ufcstats.com from this sandbox — same
"remote container egress policy" documented in fetchers/baseball.py). The CSS
class names and page layout below are based on ufcstats.com's long-stable,
widely-scraped structure (unchanged for years across public scraper projects)
but have NOT been live-verified from this session. Confirm against a real
response once deployed (Railway) or run locally, same as the rugby ESPN slug.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

BASE = "http://ufcstats.com"
TIMEOUT = 20.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


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
    """Parse a stat cell like '4.52', '58%', '--' into a float, or default."""
    if val is None:
        return default
    s = str(val).strip().rstrip("%")
    if not s or s == "--":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def search_fighters_by_letter(last_initial: str) -> list[dict]:
    """
    Return every fighter whose last name starts with `last_initial` from
    /statistics/fighters?char={letter}&page=all.
    Each entry: {name, url, wins, losses, draws}. Empty list on failure.
    """
    html = _get(f"{BASE}/statistics/fighters", {"char": last_initial.lower(), "page": "all"})
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("tr.b-statistics__table-row"):
        cells = row.select("td.b-statistics__table-col")
        if len(cells) < 10:
            continue
        first_a = cells[0].select_one("a")
        last_a = cells[1].select_one("a")
        if not first_a or not last_a:
            continue
        first = first_a.get_text(strip=True)
        last = last_a.get_text(strip=True)
        url = first_a.get("href", "")
        out.append({
            "name": f"{first} {last}".strip(),
            "url": url,
            "wins":   _f(cells[7].get_text(strip=True), 0),
            "losses": _f(cells[8].get_text(strip=True), 0),
            "draws":  _f(cells[9].get_text(strip=True), 0),
        })
    return out


def lookup_fighter(name: str) -> Optional[dict]:
    """
    Resolve a free-text fighter name to a ufcstats.com fighter entry.
    Tries the last word of the query as the last-name initial (ufcstats.com's
    listing is indexed by last name), then substring/fuzzy-matches the full
    "First Last" against that letter's page. Returns None if nothing matches.
    """
    name = name.strip()
    if not name:
        return None
    last_word = name.split()[-1]
    candidates = search_fighters_by_letter(last_word[0])
    if not candidates:
        return None

    name_lo = name.lower()
    for c in candidates:
        if c["name"].lower() == name_lo:
            return c
    for c in candidates:
        if name_lo in c["name"].lower() or c["name"].lower() in name_lo:
            return c

    import difflib
    names = [c["name"].lower() for c in candidates]
    close = difflib.get_close_matches(name_lo, names, n=1, cutoff=0.6)
    if close:
        return next((c for c in candidates if c["name"].lower() == close[0]), None)
    return None


_STAT_LABELS = {
    "Height:": "height", "Weight:": "weight_class_raw", "Reach:": "reach",
    "STANCE:": "stance", "DOB:": "dob",
    "SLpM:": "slpm", "Str. Acc.:": "str_acc",
    "SApM:": "sapm", "Str. Def:": "str_def",
    "TD Avg.:": "td_avg", "TD Acc.:": "td_acc",
    "TD Def.:": "td_def", "Sub. Avg.:": "sub_avg",
}


def fetch_fighter_profile(fighter_url: str) -> dict:
    """
    Scrape a fighter's ufcstats.com detail page for career stats + bio.
    Returns {} on failure. Keys: name, record (wins/losses/draws),
    wins_by_ko, wins_by_sub, wins_by_dec, height_in, reach_in, stance, dob,
    slpm, str_acc, sapm, str_def, td_avg, td_acc, td_def, sub_avg.
    """
    html = _get(fighter_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")

    name_el = soup.select_one("span.b-content__title-highlight")
    name = name_el.get_text(strip=True) if name_el else ""

    record_el = soup.select_one("span.b-content__title-record")
    wins = losses = draws = 0
    if record_el:
        m = re.search(r"(\d+)-(\d+)-(\d+)", record_el.get_text(strip=True))
        if m:
            wins, losses, draws = int(m.group(1)), int(m.group(2)), int(m.group(3))

    raw: dict = {}
    for li in soup.select("li.b-list__box-list-item"):
        label_el = li.select_one("i.b-list__box-item-title")
        if not label_el:
            continue
        label = label_el.get_text(strip=True)
        key = _STAT_LABELS.get(label)
        if not key:
            continue
        full_text = li.get_text(strip=True)
        value = full_text[len(label):].strip()
        raw[key] = value

    def _inches(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        m = re.match(r"(\d+)'\s*(\d+)", text)
        if m:
            return int(m.group(1)) * 12 + int(m.group(2))
        m2 = re.match(r'(\d+)"', text)
        if m2:
            return float(m2.group(1))
        return None

    age = None
    if raw.get("dob"):
        try:
            dob = datetime.strptime(raw["dob"], "%b %d, %Y")
            age = (datetime.now() - dob).days / 365.25
        except Exception:
            age = None

    return {
        "name": name,
        "wins": wins, "losses": losses, "draws": draws,
        "height_in": _inches(raw.get("height")),
        "reach_in":  _f(raw.get("reach", "").rstrip('"') if raw.get("reach") else None),
        "stance":    raw.get("stance"),
        "age":       age,
        "slpm":    _f(raw.get("slpm")),
        "str_acc": _f(raw.get("str_acc")),
        "sapm":    _f(raw.get("sapm")),
        "str_def": _f(raw.get("str_def")),
        "td_avg":  _f(raw.get("td_avg")),
        "td_acc":  _f(raw.get("td_acc")),
        "td_def":  _f(raw.get("td_def")),
        "sub_avg": _f(raw.get("sub_avg")),
    }


def fetch_fight_history(fighter_url: str, limit: int = 15) -> list[dict]:
    """
    Scrape a fighter's recent fight history table (most recent first):
      [{result: 'W'|'L'|'D'|'NC', opponent, method, round, event, date}]
    `method` is the raw ufcstats.com string (e.g. "KO/TKO", "Submission",
    "Decision - Unanimous"), used by models/ufc_model.py to derive each
    fighter's KO-rate / sub-rate and KO-susceptibility / sub-susceptibility.
    Empty list on failure.
    """
    html = _get(fighter_url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    fights = []
    for row in soup.select("tbody.b-fight-details__table-body > tr"):
        cells = row.select("td.b-fight-details__table-col")
        if len(cells) < 10:
            continue
        result_flair = cells[0].select_one("a.b-flag, i.b-flag")
        result_text = (result_flair.get_text(strip=True) if result_flair
                       else cells[0].get_text(strip=True))
        result = "W" if "win" in result_text.lower() else (
            "L" if "loss" in result_text.lower() else
            "NC" if "nc" in result_text.lower() else "D"
        )
        opp_a = cells[1].select_one("a")
        opponent = opp_a.get_text(strip=True) if opp_a else ""
        method = cells[7].get_text(" ", strip=True) if len(cells) > 7 else ""
        rnd = cells[8].get_text(strip=True) if len(cells) > 8 else ""
        event_a = cells[6].select_one("a") if len(cells) > 6 else None
        event = event_a.get_text(strip=True) if event_a else ""
        fights.append({
            "result": result, "opponent": opponent, "method": method,
            "round": rnd, "event": event,
        })
    return fights[:limit]


def _method_rates(fights: list[dict]) -> dict:
    """
    From a fight history list, compute:
      wins_by_ko/sub/dec (share of WINS by method) and
      losses_by_ko/sub (share of LOSSES by method — durability/susceptibility proxy).
    All None when the relevant outcome count is 0 (not 0.0 — avoids a fighter
    with zero losses looking identical to one who's simply never been finished).
    """
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
    Main entry point — resolve both fighter names and return
    {"a": {...profile, ...method_rates}, "b": {...}}.
    A fighter's dict is {} (not a replacement-level default) when they can't
    be resolved, matching this repo's "don't hallucinate from nothing"
    convention (see analyze_soccer.py, fetchers/rugby.py).
    """
    result: dict = {"a": {}, "b": {}}
    for key, name in (("a", name_a), ("b", name_b)):
        match = lookup_fighter(name)
        if not match:
            continue
        profile = fetch_fighter_profile(f"{BASE}{match['url']}" if match["url"].startswith("/") else match["url"])
        if not profile:
            continue
        history = fetch_fight_history(match["url"])
        rates = _method_rates(history)
        result[key] = {**profile, **rates, "fight_history": history}
    return result


def diagnose(sample_fighter: str = "Jones") -> dict:
    """
    One-shot diagnostic reporting the RAW HTTP status + response snippet for
    ufcstats.com, instead of the silent {} enrich_ufc_fighters returns on any
    failure. Added 2026-07-12 (same pattern as fetchers/rugby.py:diagnose,
    fetchers/nrfi_odds.py:diagnose) after a user report that no fighter stats
    ever come back. Since this repo's dev sandbox can't reach ufcstats.com at
    all (confirmed both http and https, same proxy block as espn.com), the
    scraper's CSS-class assumptions were never live-verified — this checks
    each stage in order so a failure shows up as a specific status code /
    missing selector instead of a generic empty result.
    """
    out: dict = {"base": BASE}

    # 1. Raw fetch of the alphabetical listing page
    letter = sample_fighter[0].lower() if sample_fighter else "a"
    url = f"{BASE}/statistics/fighters"
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(url, params={"char": letter, "page": "all"})
            out["listing_status"] = resp.status_code
            out["listing_url"] = str(resp.url)
            out["listing_content_length"] = len(resp.text)
            out["listing_has_expected_class"] = "b-statistics__table-row" in resp.text
            if resp.status_code != 200:
                out["listing_error_body"] = resp.text[:500]
    except Exception as exc:
        out["listing_exception"] = str(exc)

    parsed = search_fighters_by_letter(letter)
    out["parsed_fighter_count"] = len(parsed)
    out["parsed_fighter_sample"] = parsed[:5]

    # 2. If lookup_fighter resolves the sample name, probe its detail page raw
    match = lookup_fighter(sample_fighter) if parsed else None
    out["sample_fighter_matched"] = match
    if match:
        fighter_url = f"{BASE}{match['url']}" if match["url"].startswith("/") else match["url"]
        try:
            with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
                resp = client.get(fighter_url)
                out["profile_status"] = resp.status_code
                out["profile_content_length"] = len(resp.text)
                out["profile_has_expected_classes"] = {
                    "b-content__title-highlight": "b-content__title-highlight" in resp.text,
                    "b-list__box-list-item": "b-list__box-list-item" in resp.text,
                    "b-fight-details__table-body": "b-fight-details__table-body" in resp.text,
                }
                if resp.status_code != 200:
                    out["profile_error_body"] = resp.text[:500]
        except Exception as exc:
            out["profile_exception"] = str(exc)

        try:
            out["parsed_profile"] = fetch_fighter_profile(fighter_url)
        except Exception as exc:
            out["parsed_profile_exception"] = str(exc)

        try:
            history = fetch_fight_history(fighter_url, limit=3)
            out["parsed_fight_history_sample"] = history
        except Exception as exc:
            out["parsed_fight_history_exception"] = str(exc)

    return out
