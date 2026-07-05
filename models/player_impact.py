"""
Player Impact Score — how much ANY player moves the needle on game outcomes.

Pitchers: Runs the split Poisson model with the real pitcher's FIP vs a
replacement-level FIP (4.00). Delta = pitcher's impact in percentage points.

Hitters: Runs the Poisson model with the team's wRC+ including the hitter vs
replacing the hitter with a league-average bat (wRC+ 100). Delta = hitter's
impact in percentage points.
"""
from __future__ import annotations
from typing import Optional

from fetchers.feature_store import load_store, lookup_pitcher_features, _norm
from fetchers.baseball import lookup_batter
from models.baseball_market import (
    expected_runs_split, build_run_matrix, moneyline_market,
    LEAGUE_AVG_FIP, LEAGUE_BULLPEN_FIP,
)

REPLACEMENT_FIP = 4.00
DEFAULT_WRC_PLUS = 100.0
REPLACEMENT_WRC_PLUS = 100.0
LINEUP_SLOTS = 9

TRADE_NEWS: dict[str, str] = {
    "Paul Skenes":      "Franchise cornerstone — Pirates locked him up through 2030",
    "Gerrit Cole":      "Signed 9yr/$324M extension with NYY (Dec 2023)",
    "Max Fried":        "Signed with NYY as free agent (Dec 2024)",
    "Blake Snell":       "Signed 5yr/$182M with LAD (Mar 2024)",
    "Corbin Burnes":    "Traded to BAL (Feb 2024), signed extension",
    "Yoshinobu Yamamoto": "Signed 12yr/$325M with LAD (Dec 2023)",
    "Spencer Strider":  "TJ surgery rehab — expected back mid-2026",
    "Shane Bieber":     "TJ surgery rehab — missed most of 2025",
    "Justin Verlander": "Final year of contract — retirement candidate after 2025",
    "Shohei Ohtani":    "Signed 10yr/$700M with LAD — not pitching until mid-2025 TJ rehab",
    "Tyler Glasnow":    "Signed 5yr/$136.5M with LAD (Nov 2023)",
    "Zack Wheeler":     "Phillies ace — contract through 2025 with option",
    "Tarik Skubal":     "Tigers extended — AL Cy Young favorite 2024-25",
    "Chris Sale":       "Resurgence with ATL — NL Cy Young 2024",
    "Logan Webb":       "Giants ace — team control through 2026",
    "Framber Valdez":   "Astros workhorse — signed extension through 2027",
}

KNOWN_STARTERS: dict[str, dict] = {
    "Paul Skenes":      {"era": 1.96, "fip": 2.07, "avg_ip": 6.5, "throws": "R", "team": "PIT"},
    "Gerrit Cole":      {"era": 3.41, "fip": 3.12, "avg_ip": 6.3, "throws": "R", "team": "NYY"},
    "Tarik Skubal":     {"era": 2.39, "fip": 2.44, "avg_ip": 6.8, "throws": "L", "team": "DET"},
    "Chris Sale":       {"era": 2.38, "fip": 2.86, "avg_ip": 6.2, "throws": "L", "team": "ATL"},
    "Zack Wheeler":     {"era": 2.57, "fip": 2.98, "avg_ip": 6.7, "throws": "R", "team": "PHI"},
    "Logan Webb":       {"era": 3.03, "fip": 3.54, "avg_ip": 6.5, "throws": "R", "team": "SF"},
    "Framber Valdez":   {"era": 3.14, "fip": 3.55, "avg_ip": 6.6, "throws": "L", "team": "HOU"},
    "Corbin Burnes":    {"era": 2.92, "fip": 3.28, "avg_ip": 6.4, "throws": "R", "team": "BAL"},
    "Max Fried":        {"era": 3.25, "fip": 3.53, "avg_ip": 6.1, "throws": "L", "team": "NYY"},
    "Blake Snell":      {"era": 3.12, "fip": 3.19, "avg_ip": 5.5, "throws": "L", "team": "LAD"},
    "Tyler Glasnow":    {"era": 3.32, "fip": 3.02, "avg_ip": 5.8, "throws": "R", "team": "LAD"},
    "Yoshinobu Yamamoto": {"era": 3.00, "fip": 3.31, "avg_ip": 5.7, "throws": "R", "team": "LAD"},
    "Sonny Gray":       {"era": 2.79, "fip": 3.10, "avg_ip": 6.1, "throws": "R", "team": "STL"},
    "Seth Lugo":        {"era": 3.00, "fip": 3.45, "avg_ip": 6.3, "throws": "R", "team": "KC"},
    "Reynaldo Lopez":   {"era": 1.83, "fip": 2.95, "avg_ip": 5.8, "throws": "R", "team": "ATL"},
    "Ranger Suarez":    {"era": 2.87, "fip": 3.50, "avg_ip": 6.0, "throws": "L", "team": "PHI"},
    "Dylan Cease":      {"era": 3.47, "fip": 3.62, "avg_ip": 5.9, "throws": "R", "team": "SD"},
    "George Kirby":     {"era": 3.35, "fip": 3.28, "avg_ip": 6.3, "throws": "R", "team": "SEA"},
    "Luis Castillo":    {"era": 3.64, "fip": 3.71, "avg_ip": 6.0, "throws": "R", "team": "SEA"},
    "Bryce Miller":     {"era": 3.45, "fip": 3.65, "avg_ip": 5.9, "throws": "R", "team": "SEA"},
    "Aaron Nola":       {"era": 3.57, "fip": 3.49, "avg_ip": 6.2, "throws": "R", "team": "PHI"},
    "Pablo Lopez":      {"era": 3.32, "fip": 3.55, "avg_ip": 5.8, "throws": "R", "team": "MIN"},
    "Joe Ryan":         {"era": 3.55, "fip": 3.45, "avg_ip": 5.7, "throws": "R", "team": "MIN"},
    "Tanner Houck":     {"era": 3.20, "fip": 3.60, "avg_ip": 5.9, "throws": "R", "team": "BOS"},
    "Brayan Bello":     {"era": 4.24, "fip": 3.99, "avg_ip": 5.9, "throws": "R", "team": "BOS"},
    "Hunter Greene":    {"era": 3.41, "fip": 3.30, "avg_ip": 5.6, "throws": "R", "team": "CIN"},
    "Nick Lodolo":      {"era": 3.77, "fip": 3.82, "avg_ip": 5.4, "throws": "L", "team": "CIN"},
    "Spencer Strider":  {"era": 3.86, "fip": 3.50, "avg_ip": 5.0, "throws": "R", "team": "ATL"},
    "Kevin Gausman":    {"era": 3.74, "fip": 3.60, "avg_ip": 5.9, "throws": "R", "team": "TOR"},
    "Jose Berrios":     {"era": 3.65, "fip": 3.72, "avg_ip": 6.0, "throws": "R", "team": "TOR"},
    "Shane Baz":        {"era": 3.50, "fip": 3.40, "avg_ip": 5.3, "throws": "R", "team": "TB"},
    "Taj Bradley":      {"era": 3.77, "fip": 3.65, "avg_ip": 5.5, "throws": "R", "team": "TB"},
    "Shane McClanahan": {"era": 3.40, "fip": 3.55, "avg_ip": 5.6, "throws": "L", "team": "TB"},
    "Grayson Rodriguez": {"era": 3.90, "fip": 3.75, "avg_ip": 5.7, "throws": "R", "team": "BAL"},
    "Carlos Rodon":     {"era": 3.96, "fip": 3.62, "avg_ip": 5.5, "throws": "L", "team": "NYY"},
    "Nestor Cortes":    {"era": 4.05, "fip": 4.12, "avg_ip": 5.4, "throws": "L", "team": "NYY"},
    "Marcus Stroman":   {"era": 4.31, "fip": 4.18, "avg_ip": 5.6, "throws": "R", "team": "NYM"},
    "Luis Severino":    {"era": 3.95, "fip": 4.05, "avg_ip": 5.5, "throws": "R", "team": "NYM"},
    "Sean Manaea":      {"era": 3.47, "fip": 3.70, "avg_ip": 5.8, "throws": "L", "team": "NYM"},
    "Justin Verlander": {"era": 4.50, "fip": 4.22, "avg_ip": 5.5, "throws": "R", "team": "HOU"},
    "Ronel Blanco":     {"era": 2.80, "fip": 3.35, "avg_ip": 5.9, "throws": "R", "team": "HOU"},
    "Lance McCullers Jr.": {"era": 4.20, "fip": 4.00, "avg_ip": 5.3, "throws": "R", "team": "HOU"},
    "Jack Flaherty":    {"era": 3.75, "fip": 3.80, "avg_ip": 5.8, "throws": "R", "team": "LAD"},
    "Bobby Miller":     {"era": 4.15, "fip": 4.05, "avg_ip": 5.2, "throws": "R", "team": "LAD"},
    "Michael King":     {"era": 3.42, "fip": 3.55, "avg_ip": 5.9, "throws": "R", "team": "SD"},
    "Yu Darvish":       {"era": 3.80, "fip": 3.75, "avg_ip": 5.6, "throws": "R", "team": "SD"},
    "Joe Musgrove":     {"era": 3.88, "fip": 3.90, "avg_ip": 5.5, "throws": "R", "team": "SD"},
    "Miles Mikolas":    {"era": 4.45, "fip": 4.40, "avg_ip": 5.8, "throws": "R", "team": "STL"},
    "Patrick Corbin":   {"era": 5.20, "fip": 5.10, "avg_ip": 5.2, "throws": "L", "team": "WSH"},
    "MacKenzie Gore":   {"era": 4.50, "fip": 4.25, "avg_ip": 5.3, "throws": "L", "team": "WSH"},
    "Shota Imanaga":    {"era": 2.91, "fip": 3.10, "avg_ip": 5.9, "throws": "L", "team": "CHC"},
    "Justin Steele":    {"era": 3.06, "fip": 3.35, "avg_ip": 6.0, "throws": "L", "team": "CHC"},
    "Freddy Peralta":   {"era": 3.43, "fip": 3.55, "avg_ip": 5.6, "throws": "R", "team": "MIL"},
    "Colin Rea":        {"era": 3.55, "fip": 3.80, "avg_ip": 5.5, "throws": "R", "team": "MIL"},
    "Mitch Keller":     {"era": 3.88, "fip": 3.90, "avg_ip": 6.0, "throws": "R", "team": "PIT"},
    "Jared Jones":      {"era": 3.65, "fip": 3.55, "avg_ip": 5.3, "throws": "R", "team": "PIT"},
    "Shane Bieber":     {"era": 3.20, "fip": 3.15, "avg_ip": 5.0, "throws": "R", "team": "CLE"},
    "Tanner Bibee":     {"era": 3.44, "fip": 3.50, "avg_ip": 5.8, "throws": "R", "team": "CLE"},
    "Ben Lively":       {"era": 3.81, "fip": 3.92, "avg_ip": 5.5, "throws": "R", "team": "CLE"},
    "Sandy Alcantara":  {"era": 4.14, "fip": 4.00, "avg_ip": 5.0, "throws": "R", "team": "MIA"},
    "Jesus Luzardo":    {"era": 3.60, "fip": 3.55, "avg_ip": 5.4, "throws": "L", "team": "MIA"},
    "Zac Gallen":       {"era": 3.45, "fip": 3.50, "avg_ip": 6.1, "throws": "R", "team": "ARI"},
    "Merrill Kelly":    {"era": 3.60, "fip": 3.70, "avg_ip": 5.8, "throws": "R", "team": "ARI"},
    "Brandon Pfaadt":   {"era": 4.10, "fip": 3.95, "avg_ip": 5.5, "throws": "R", "team": "ARI"},
    "Reid Detmers":     {"era": 4.50, "fip": 4.30, "avg_ip": 5.3, "throws": "L", "team": "LAA"},
    "Tyler Anderson":   {"era": 4.18, "fip": 4.10, "avg_ip": 5.5, "throws": "L", "team": "LAA"},
    "Andrew Heaney":    {"era": 4.32, "fip": 4.15, "avg_ip": 5.3, "throws": "L", "team": "TEX"},
    "Nathan Eovaldi":   {"era": 3.60, "fip": 3.72, "avg_ip": 6.0, "throws": "R", "team": "TEX"},
    "Jon Gray":         {"era": 4.15, "fip": 4.05, "avg_ip": 5.4, "throws": "R", "team": "TEX"},
    "Dane Dunning":     {"era": 4.30, "fip": 4.20, "avg_ip": 5.5, "throws": "R", "team": "TEX"},
    "Michael Wacha":    {"era": 3.22, "fip": 3.55, "avg_ip": 5.7, "throws": "R", "team": "KC"},
    "Cole Ragans":      {"era": 3.14, "fip": 3.25, "avg_ip": 5.8, "throws": "L", "team": "KC"},
    "Brady Singer":     {"era": 3.71, "fip": 3.80, "avg_ip": 5.6, "throws": "R", "team": "KC"},
    "Garrett Crochet":  {"era": 3.58, "fip": 3.30, "avg_ip": 5.5, "throws": "L", "team": "CWS"},
    "Chris Bassitt":    {"era": 3.56, "fip": 3.75, "avg_ip": 6.0, "throws": "R", "team": "TOR"},
    "Cristopher Sanchez": {"era": 3.44, "fip": 3.60, "avg_ip": 5.8, "throws": "L", "team": "PHI"},
    "Clarke Schmidt":   {"era": 3.60, "fip": 3.70, "avg_ip": 5.3, "throws": "R", "team": "NYY"},
    "Bowden Francis":   {"era": 3.90, "fip": 3.85, "avg_ip": 5.2, "throws": "R", "team": "TOR"},
    "Max Scherzer":     {"era": 4.10, "fip": 3.90, "avg_ip": 5.3, "throws": "R", "team": "TEX"},
    "Jordan Montgomery": {"era": 4.40, "fip": 4.25, "avg_ip": 5.4, "throws": "L", "team": "ARI"},
    "Erick Fedde":      {"era": 3.11, "fip": 3.55, "avg_ip": 6.0, "throws": "R", "team": "STL"},
}


def _get_pitcher_data(name: str) -> dict:
    """Look up pitcher data from KNOWN_STARTERS first, then feature store."""
    if not name:
        return {}
    for known_name, data in KNOWN_STARTERS.items():
        if _norm(known_name) == _norm(name):
            return {"display_name": known_name, **data}
    store_data = lookup_pitcher_features(name)
    if store_data:
        return store_data
    return {}


def _win_prob_for_fip(fip: float, avg_ip: float = 5.5,
                      opponent_wrc_plus: float = 100.0,
                      park_factor: float = 1.0,
                      is_home: bool = True) -> float:
    """Run split Poisson and return win probability for the pitcher's team."""
    bullpen_fip = LEAGUE_BULLPEN_FIP

    mu_against_f5, mu_against_l4 = expected_runs_split(
        opponent_wrc_plus, fip, bullpen_fip, park_factor, not is_home, avg_ip
    )
    mu_for_f5, mu_for_l4 = expected_runs_split(
        DEFAULT_WRC_PLUS, REPLACEMENT_FIP, bullpen_fip, park_factor, is_home, 5.5
    )

    mu_for = mu_for_f5 + mu_for_l4
    mu_against = mu_against_f5 + mu_against_l4

    matrix = build_run_matrix(mu_for, mu_against) if is_home else build_run_matrix(mu_against, mu_for)
    ml = moneyline_market(matrix)
    return ml["p_home_win"] if is_home else ml["p_away_win"]


def compute_impact(
    pitcher_name: str,
    team_abbr: Optional[str] = None,
    opponent_wrc_plus: float = 100.0,
    park_factor: float = 1.0,
    is_home: bool = True,
) -> dict:
    """
    Compute how many percentage points a pitcher moves win probability
    vs a league-average replacement (FIP = 4.00).

    Returns a dict with impact_pp, tier, stats, and optional trade_note.
    """
    data = _get_pitcher_data(pitcher_name)
    if not data:
        return {"error": f"Pitcher '{pitcher_name}' not found in database. Try a different spelling."}

    display_name = data.get("display_name", pitcher_name)
    fip = data.get("fip")
    era = data.get("era")
    avg_ip = data.get("avg_ip", 5.5)
    throws = data.get("throws", "R")
    team = team_abbr or data.get("team", "")

    if not fip:
        # barrel_pct/hard_hit_pct from the feature store are raw percents (e.g.
        # 10.4 meaning 10.4%), not decimal fractions — this formula expects
        # decimals (see the 0.075/0.35 defaults below), so convert first.
        # Fixed 2026-07-05: previously fed raw percents straight in, blowing
        # the formula past its 6.0 clamp for almost every feature-store-only
        # pitcher regardless of actual quality (e.g. Emmet Sheehan showed
        # FIP 6.0 / -16.3pp LIABILITY from this alone).
        barrel_raw = data.get("barrel_pct_against") or data.get("barrel_pct")
        barrel_pct = (barrel_raw / 100.0) if barrel_raw else 0.075
        hard_hit_raw = data.get("hard_hit_pct")
        hard_hit = (hard_hit_raw / 100.0) if hard_hit_raw else 0.35
        avg_velo = data.get("avg_fb_velo") or data.get("avg_velo") or 93.5
        fip = 2.5 + barrel_pct * 20 + (1 - avg_velo / 100) * 5 + hard_hit * 3
        fip = max(2.5, min(fip, 6.0))

    if not era:
        era = fip + 0.3

    win_prob_real = _win_prob_for_fip(fip, avg_ip, opponent_wrc_plus, park_factor, is_home)
    win_prob_repl = _win_prob_for_fip(REPLACEMENT_FIP, 5.5, opponent_wrc_plus, park_factor, is_home)

    impact_pp = round((win_prob_real - win_prob_repl) * 100, 1)

    if impact_pp >= 8:
        tier, tier_desc = "ACE", "Elite game-changer — team's win probability jumps significantly with this pitcher"
    elif impact_pp >= 4:
        tier, tier_desc = "FRONT-LINE", "Top-of-rotation arm — clearly moves the needle"
    elif impact_pp >= 1:
        tier, tier_desc = "SOLID", "Above-average starter — positive but modest impact"
    elif impact_pp >= -1:
        tier, tier_desc = "AVERAGE", "Replacement-level — minimal impact on game outcome"
    elif impact_pp >= -4:
        tier, tier_desc = "BELOW AVG", "Below replacement — team is slightly worse with this starter"
    else:
        tier, tier_desc = "LIABILITY", "Significant drag on team — opponents gain a clear edge"

    trade_note = ""
    for known_name, note in TRADE_NEWS.items():
        if _norm(known_name) == _norm(pitcher_name):
            trade_note = note
            break

    runs_real_f5, runs_real_l4 = expected_runs_split(
        opponent_wrc_plus, fip, LEAGUE_BULLPEN_FIP, park_factor, not is_home, avg_ip
    )
    runs_repl_f5, runs_repl_l4 = expected_runs_split(
        opponent_wrc_plus, REPLACEMENT_FIP, LEAGUE_BULLPEN_FIP, park_factor, not is_home, 5.5
    )

    return {
        "pitcher": display_name,
        "team": team,
        "throws": throws,
        "impact_pp": impact_pp,
        "tier": tier,
        "tier_desc": tier_desc,
        "fip": round(fip, 2),
        "era": round(era, 2) if era else None,
        "avg_ip": round(avg_ip, 1),
        "win_prob_with": round(win_prob_real * 100, 1),
        "win_prob_replacement": round(win_prob_repl * 100, 1),
        "runs_allowed_f5": round(runs_real_f5, 2),
        "runs_allowed_repl_f5": round(runs_repl_f5, 2),
        "runs_allowed_l4": round(runs_real_l4, 2),
        # barrel_pct/whiff_pct are raw percents in the feature store (e.g. 10.4
        # for 10.4%); the frontend multiplies by 100 to render "%", so convert
        # to decimal here. csw_pct is already decimal-scale (FanGraphs CSV
        # import normalizes it), so it's left as-is.
        "barrel_pct": (lambda v: v / 100.0 if v else None)(data.get("barrel_pct_against") or data.get("barrel_pct")),
        "avg_fb_velo": data.get("avg_fb_velo") or data.get("avg_velo"),
        "csw_pct": data.get("csw_pct"),
        "whiff_pct": (lambda v: v / 100.0 if v else None)(data.get("whiff_pct")),
        "trade_note": trade_note,
    }


def search_pitchers(query: str, limit: int = 10) -> list[dict]:
    """Search for pitchers by name prefix. Returns list of matches."""
    results = []
    q = _norm(query)
    if not q or len(q) < 2:
        return results

    for name, data in KNOWN_STARTERS.items():
        if q in _norm(name):
            results.append({
                "key": _norm(name),
                "display_name": name,
                "team": data.get("team", ""),
                "fip": data.get("fip"),
            })

    pitchers = load_store().get("pitchers", {})
    seen = {r["key"] for r in results}
    for key, data in pitchers.items():
        if key in seen:
            continue
        display = data.get("display_name", key)
        if q in _norm(display):
            results.append({
                "key": key,
                "display_name": display,
                "team": data.get("team", ""),
                "fip": data.get("fip"),
            })
            seen.add(key)

    return results[:limit]


# ── Team average wRC+ (2024-25 blend) ──────────────────────────────────────
TEAM_WRC_PLUS: dict[str, float] = {
    "LAD": 112, "ATL": 108, "NYY": 107, "PHI": 106, "HOU": 105,
    "BAL": 104, "SEA": 103, "SD": 103, "MIN": 102, "TEX": 102,
    "BOS": 101, "SF": 101, "CLE": 100, "NYM": 100, "MIL": 100,
    "TB": 99, "CHC": 99, "ARI": 99, "CIN": 98, "KC": 98,
    "TOR": 97, "STL": 97, "DET": 96, "PIT": 95, "LAA": 94,
    "WSH": 93, "COL": 92, "MIA": 90, "CWS": 88, "OAK": 89,
}

HITTER_TRADE_NEWS: dict[str, str] = {
    "Shohei Ohtani":     "Signed 10yr/$700M with LAD (Dec 2023) — DH full-time 2024-25",
    "Juan Soto":         "Signed 15yr/$765M with NYM (Dec 2024) — biggest deal in history",
    "Aaron Judge":       "Signed 9yr/$360M with NYY (Dec 2022) — AL MVP 2022",
    "Mookie Betts":      "Signed 12yr/$365M extension with LAD (Mar 2024)",
    "Ronald Acuna Jr.":  "Signed 10yr/$100M with ATL — ACL tear July 2024, rehab 2025",
    "Freddie Freeman":   "Signed 6yr/$162M with LAD (Mar 2022) — NL MVP 2020",
    "Trea Turner":       "Signed 11yr/$300M with PHI (Dec 2022)",
    "Corey Seager":      "Signed 10yr/$325M with TEX (Nov 2021) — 2023 WS MVP",
    "Julio Rodriguez":   "Signed 12yr/$210M extension with SEA (Aug 2023)",
    "Gunnar Henderson":  "Under team control through 2029 — AL ROY 2023",
    "Bobby Witt Jr.":    "Signed 11yr/$288M extension with KC (Feb 2024)",
    "Elly De La Cruz":   "Under team control — elite speed + power combo",
    "Corbin Carroll":    "Under team control — 2023 NL ROY",
    "Fernando Tatis Jr.": "Signed 14yr/$340M with SD (Feb 2021)",
    "Pete Alonso":       "Free agent signing with NYM — power bat",
    "Bryce Harper":      "Signed 13yr/$330M with PHI (Feb 2019) — 2 NL MVPs",
    "Mike Trout":        "Signed 12yr/$426M with LAA — injuries limiting 2024-25",
    "Yordan Alvarez":    "Signed 6yr/$115M extension with HOU (Jun 2023)",
    "Marcus Semien":     "Signed 7yr/$175M with TEX (Nov 2021)",
    "Matt Olson":        "Signed 8yr/$168M with ATL (Mar 2022)",
}

KNOWN_HITTERS: dict[str, dict] = {
    "Shohei Ohtani":     {"wrc_plus": 190, "ops": 1.036, "avg": .304, "hr": 54, "sb": 59, "war": 9.2, "bats": "L", "pos": "DH", "team": "LAD"},
    "Aaron Judge":       {"wrc_plus": 180, "ops": .989,  "avg": .290, "hr": 58, "sb": 3,  "war": 8.5, "bats": "R", "pos": "RF", "team": "NYY"},
    "Juan Soto":         {"wrc_plus": 175, "ops": .989,  "avg": .288, "hr": 41, "sb": 4,  "war": 7.8, "bats": "L", "pos": "RF", "team": "NYM"},
    "Bobby Witt Jr.":    {"wrc_plus": 158, "ops": .903,  "avg": .332, "hr": 32, "sb": 31, "war": 8.0, "bats": "R", "pos": "SS", "team": "KC"},
    "Mookie Betts":      {"wrc_plus": 155, "ops": .893,  "avg": .283, "hr": 29, "sb": 14, "war": 7.0, "bats": "R", "pos": "SS", "team": "LAD"},
    "Freddie Freeman":   {"wrc_plus": 152, "ops": .897,  "avg": .282, "hr": 22, "sb": 3,  "war": 5.5, "bats": "L", "pos": "1B", "team": "LAD"},
    "Gunnar Henderson":  {"wrc_plus": 155, "ops": .912,  "avg": .279, "hr": 37, "sb": 11, "war": 7.5, "bats": "L", "pos": "SS", "team": "BAL"},
    "Corey Seager":      {"wrc_plus": 148, "ops": .879,  "avg": .275, "hr": 33, "sb": 3,  "war": 6.2, "bats": "L", "pos": "SS", "team": "TEX"},
    "Ronald Acuna Jr.":  {"wrc_plus": 145, "ops": .862,  "avg": .275, "hr": 28, "sb": 45, "war": 6.5, "bats": "R", "pos": "RF", "team": "ATL"},
    "Yordan Alvarez":    {"wrc_plus": 160, "ops": .926,  "avg": .288, "hr": 35, "sb": 1,  "war": 5.0, "bats": "L", "pos": "DH", "team": "HOU"},
    "Trea Turner":       {"wrc_plus": 130, "ops": .808,  "avg": .280, "hr": 21, "sb": 26, "war": 5.5, "bats": "R", "pos": "SS", "team": "PHI"},
    "Julio Rodriguez":   {"wrc_plus": 125, "ops": .790,  "avg": .270, "hr": 24, "sb": 25, "war": 4.5, "bats": "R", "pos": "CF", "team": "SEA"},
    "Fernando Tatis Jr.": {"wrc_plus": 140, "ops": .850, "avg": .272, "hr": 30, "sb": 22, "war": 5.0, "bats": "R", "pos": "RF", "team": "SD"},
    "Pete Alonso":       {"wrc_plus": 128, "ops": .822,  "avg": .240, "hr": 34, "sb": 2,  "war": 3.5, "bats": "R", "pos": "1B", "team": "NYM"},
    "Bryce Harper":      {"wrc_plus": 145, "ops": .868,  "avg": .277, "hr": 30, "sb": 5,  "war": 5.8, "bats": "L", "pos": "1B", "team": "PHI"},
    "Matt Olson":        {"wrc_plus": 132, "ops": .815,  "avg": .252, "hr": 33, "sb": 1,  "war": 4.0, "bats": "L", "pos": "1B", "team": "ATL"},
    "Rafael Devers":     {"wrc_plus": 140, "ops": .852,  "avg": .278, "hr": 28, "sb": 3,  "war": 5.0, "bats": "L", "pos": "3B", "team": "BOS"},
    "Elly De La Cruz":   {"wrc_plus": 115, "ops": .760,  "avg": .248, "hr": 22, "sb": 67, "war": 4.5, "bats": "S", "pos": "SS", "team": "CIN"},
    "Corbin Carroll":    {"wrc_plus": 108, "ops": .735,  "avg": .260, "hr": 18, "sb": 30, "war": 3.5, "bats": "L", "pos": "CF", "team": "ARI"},
    "Marcus Semien":     {"wrc_plus": 122, "ops": .780,  "avg": .265, "hr": 25, "sb": 14, "war": 5.0, "bats": "R", "pos": "2B", "team": "TEX"},
    "Mike Trout":        {"wrc_plus": 152, "ops": .885,  "avg": .260, "hr": 18, "sb": 2,  "war": 2.0, "bats": "R", "pos": "CF", "team": "LAA"},
    "Kyle Tucker":       {"wrc_plus": 148, "ops": .878,  "avg": .289, "hr": 26, "sb": 18, "war": 5.5, "bats": "L", "pos": "RF", "team": "CHC"},
    "Willy Adames":      {"wrc_plus": 126, "ops": .795,  "avg": .251, "hr": 32, "sb": 14, "war": 4.5, "bats": "R", "pos": "SS", "team": "SF"},
    "Marcell Ozuna":     {"wrc_plus": 142, "ops": .860,  "avg": .270, "hr": 39, "sb": 2,  "war": 4.0, "bats": "R", "pos": "DH", "team": "ATL"},
    "Vladimir Guerrero Jr.": {"wrc_plus": 138, "ops": .840, "avg": .282, "hr": 30, "sb": 3, "war": 4.5, "bats": "R", "pos": "1B", "team": "LAD"},
    "Jose Ramirez":      {"wrc_plus": 140, "ops": .855,  "avg": .270, "hr": 29, "sb": 20, "war": 5.5, "bats": "S", "pos": "3B", "team": "CLE"},
    "Anthony Volpe":     {"wrc_plus": 108, "ops": .730,  "avg": .258, "hr": 18, "sb": 22, "war": 3.5, "bats": "R", "pos": "SS", "team": "NYY"},
    "Bo Bichette":       {"wrc_plus": 112, "ops": .750,  "avg": .268, "hr": 18, "sb": 12, "war": 3.0, "bats": "R", "pos": "SS", "team": "TOR"},
    "Jazz Chisholm Jr.": {"wrc_plus": 118, "ops": .770,  "avg": .255, "hr": 22, "sb": 20, "war": 3.5, "bats": "L", "pos": "3B", "team": "NYY"},
    "Adley Rutschman":   {"wrc_plus": 120, "ops": .778,  "avg": .265, "hr": 20, "sb": 2,  "war": 4.0, "bats": "S", "pos": "C",  "team": "BAL"},
    "William Contreras": {"wrc_plus": 125, "ops": .800,  "avg": .278, "hr": 22, "sb": 3,  "war": 3.8, "bats": "R", "pos": "C",  "team": "MIL"},
    "Austin Riley":      {"wrc_plus": 130, "ops": .820,  "avg": .268, "hr": 30, "sb": 2,  "war": 4.5, "bats": "R", "pos": "3B", "team": "ATL"},
    "Christian Yelich":  {"wrc_plus": 128, "ops": .810,  "avg": .275, "hr": 18, "sb": 18, "war": 3.5, "bats": "L", "pos": "LF", "team": "MIL"},
    "Alex Bregman":      {"wrc_plus": 124, "ops": .785,  "avg": .262, "hr": 22, "sb": 5,  "war": 4.0, "bats": "R", "pos": "3B", "team": "BOS"},
    "Francisco Lindor":  {"wrc_plus": 132, "ops": .822,  "avg": .270, "hr": 28, "sb": 16, "war": 5.5, "bats": "S", "pos": "SS", "team": "NYM"},
    "Ozzie Albies":      {"wrc_plus": 110, "ops": .742,  "avg": .262, "hr": 20, "sb": 16, "war": 3.5, "bats": "S", "pos": "2B", "team": "ATL"},
    "Michael Harris II": {"wrc_plus": 115, "ops": .755,  "avg": .268, "hr": 19, "sb": 14, "war": 3.5, "bats": "L", "pos": "CF", "team": "ATL"},
    "CJ Abrams":         {"wrc_plus": 110, "ops": .738,  "avg": .262, "hr": 18, "sb": 25, "war": 3.5, "bats": "L", "pos": "SS", "team": "WSH"},
    "Alec Bohm":         {"wrc_plus": 118, "ops": .768,  "avg": .285, "hr": 18, "sb": 5,  "war": 3.5, "bats": "R", "pos": "3B", "team": "PHI"},
    "Cody Bellinger":    {"wrc_plus": 118, "ops": .770,  "avg": .265, "hr": 20, "sb": 10, "war": 3.0, "bats": "L", "pos": "CF", "team": "CHC"},
    "Seiya Suzuki":      {"wrc_plus": 130, "ops": .825,  "avg": .278, "hr": 24, "sb": 8,  "war": 4.0, "bats": "R", "pos": "RF", "team": "CHC"},
    "Lars Nootbaar":     {"wrc_plus": 112, "ops": .748,  "avg": .255, "hr": 16, "sb": 8,  "war": 2.5, "bats": "L", "pos": "RF", "team": "STL"},
    "Nolan Arenado":     {"wrc_plus": 110, "ops": .735,  "avg": .260, "hr": 18, "sb": 2,  "war": 2.5, "bats": "R", "pos": "3B", "team": "HOU"},
    "Tyler O'Neill":     {"wrc_plus": 122, "ops": .785,  "avg": .248, "hr": 26, "sb": 8,  "war": 3.0, "bats": "R", "pos": "LF", "team": "BOS"},
    "Cal Raleigh":       {"wrc_plus": 115, "ops": .758,  "avg": .232, "hr": 28, "sb": 2,  "war": 3.5, "bats": "S", "pos": "C",  "team": "SEA"},
    "Salvador Perez":    {"wrc_plus": 108, "ops": .730,  "avg": .262, "hr": 25, "sb": 1,  "war": 2.5, "bats": "R", "pos": "C",  "team": "KC"},
    "Anthony Santander": {"wrc_plus": 135, "ops": .835,  "avg": .265, "hr": 36, "sb": 5,  "war": 4.5, "bats": "S", "pos": "RF", "team": "TOR"},
    "Jackson Merrill":   {"wrc_plus": 115, "ops": .755,  "avg": .270, "hr": 18, "sb": 12, "war": 3.0, "bats": "L", "pos": "CF", "team": "SD"},
    "Masataka Yoshida":  {"wrc_plus": 112, "ops": .745,  "avg": .285, "hr": 12, "sb": 3,  "war": 2.0, "bats": "L", "pos": "DH", "team": "BOS"},
    "Teoscar Hernandez": {"wrc_plus": 125, "ops": .795,  "avg": .268, "hr": 28, "sb": 8,  "war": 3.5, "bats": "R", "pos": "LF", "team": "LAD"},
    "Spencer Torkelson": {"wrc_plus": 95,  "ops": .680,  "avg": .235, "hr": 15, "sb": 1,  "war": 0.5, "bats": "R", "pos": "1B", "team": "DET"},
    "Jackson Chourio":   {"wrc_plus": 108, "ops": .728,  "avg": .265, "hr": 18, "sb": 20, "war": 3.0, "bats": "R", "pos": "CF", "team": "MIL"},
    "Oneil Cruz":        {"wrc_plus": 118, "ops": .772,  "avg": .252, "hr": 25, "sb": 18, "war": 3.5, "bats": "S", "pos": "SS", "team": "PIT"},
    "Vinnie Pasquantino": {"wrc_plus": 120, "ops": .778, "avg": .275, "hr": 19, "sb": 1,  "war": 2.5, "bats": "L", "pos": "1B", "team": "KC"},
    "Paul Goldschmidt":  {"wrc_plus": 102, "ops": .710,  "avg": .248, "hr": 16, "sb": 2,  "war": 1.5, "bats": "R", "pos": "1B", "team": "NYY"},
    "Willson Contreras":  {"wrc_plus": 118, "ops": .768, "avg": .265, "hr": 20, "sb": 2,  "war": 2.5, "bats": "R", "pos": "C",  "team": "STL"},
    "Jake Cronenworth":  {"wrc_plus": 105, "ops": .718,  "avg": .258, "hr": 14, "sb": 5,  "war": 2.0, "bats": "L", "pos": "2B", "team": "SD"},
    "Isaac Paredes":     {"wrc_plus": 118, "ops": .770,  "avg": .260, "hr": 20, "sb": 2,  "war": 3.0, "bats": "R", "pos": "3B", "team": "CHC"},
    "Ketel Marte":       {"wrc_plus": 135, "ops": .840,  "avg": .282, "hr": 28, "sb": 8,  "war": 5.0, "bats": "S", "pos": "2B", "team": "ARI"},
    "Riley Greene":      {"wrc_plus": 120, "ops": .780,  "avg": .268, "hr": 22, "sb": 10, "war": 3.5, "bats": "L", "pos": "CF", "team": "DET"},
    "Jarren Duran":      {"wrc_plus": 125, "ops": .800,  "avg": .285, "hr": 18, "sb": 35, "war": 5.0, "bats": "L", "pos": "CF", "team": "BOS"},
    "Tyler Soderstrom":  {"wrc_plus": 105, "ops": .718,  "avg": .248, "hr": 18, "sb": 2,  "war": 1.5, "bats": "L", "pos": "DH", "team": "OAK"},
    "J.P. Crawford":     {"wrc_plus": 102, "ops": .705,  "avg": .255, "hr": 10, "sb": 8,  "war": 2.5, "bats": "L", "pos": "SS", "team": "SEA"},
    "Giancarlo Stanton": {"wrc_plus": 125, "ops": .798,  "avg": .238, "hr": 27, "sb": 1,  "war": 2.0, "bats": "R", "pos": "DH", "team": "NYY"},
    "Randy Arozarena":   {"wrc_plus": 108, "ops": .732,  "avg": .258, "hr": 18, "sb": 18, "war": 2.5, "bats": "R", "pos": "LF", "team": "SEA"},
    "Manny Machado":     {"wrc_plus": 128, "ops": .808,  "avg": .268, "hr": 25, "sb": 8,  "war": 4.5, "bats": "R", "pos": "3B", "team": "SD"},
    "Ha-Seong Kim":      {"wrc_plus": 112, "ops": .745,  "avg": .260, "hr": 15, "sb": 22, "war": 4.0, "bats": "R", "pos": "SS", "team": "SD"},
    "Weston Wilson":     {"wrc_plus": 105, "ops": .715,  "avg": .252, "hr": 16, "sb": 6,  "war": 1.5, "bats": "R", "pos": "3B", "team": "PHI"},
    "Lane Thomas":       {"wrc_plus": 100, "ops": .700,  "avg": .248, "hr": 14, "sb": 20, "war": 2.0, "bats": "R", "pos": "RF", "team": "CLE"},
    "Josh Naylor":       {"wrc_plus": 122, "ops": .785,  "avg": .268, "hr": 26, "sb": 3,  "war": 3.0, "bats": "L", "pos": "1B", "team": "ARI"},
    "Ceddanne Rafaela":  {"wrc_plus": 90,  "ops": .650,  "avg": .238, "hr": 12, "sb": 15, "war": 1.5, "bats": "R", "pos": "SS", "team": "BOS"},
    "Ryan Mountcastle":  {"wrc_plus": 108, "ops": .730,  "avg": .260, "hr": 20, "sb": 2,  "war": 1.5, "bats": "R", "pos": "1B", "team": "BAL"},
    "Ezequiel Tovar":    {"wrc_plus": 95,  "ops": .685,  "avg": .255, "hr": 18, "sb": 10, "war": 3.0, "bats": "R", "pos": "SS", "team": "COL"},
    "Dansby Swanson":    {"wrc_plus": 105, "ops": .720,  "avg": .252, "hr": 18, "sb": 12, "war": 3.0, "bats": "R", "pos": "SS", "team": "CHC"},
    "J.D. Martinez":     {"wrc_plus": 115, "ops": .758,  "avg": .262, "hr": 22, "sb": 1,  "war": 1.5, "bats": "R", "pos": "DH", "team": "NYM"},
    "Max Muncy":         {"wrc_plus": 120, "ops": .778,  "avg": .235, "hr": 24, "sb": 2,  "war": 3.0, "bats": "L", "pos": "3B", "team": "LAD"},
    "George Springer":   {"wrc_plus": 110, "ops": .738,  "avg": .258, "hr": 18, "sb": 8,  "war": 2.0, "bats": "R", "pos": "DH", "team": "TOR"},
    "Will Smith":        {"wrc_plus": 118, "ops": .768,  "avg": .262, "hr": 20, "sb": 3,  "war": 3.5, "bats": "R", "pos": "C",  "team": "LAD"},
    "Xander Bogaerts":   {"wrc_plus": 108, "ops": .730,  "avg": .260, "hr": 14, "sb": 5,  "war": 2.0, "bats": "R", "pos": "SS", "team": "SD"},
    "Carlos Correa":     {"wrc_plus": 118, "ops": .770,  "avg": .265, "hr": 20, "sb": 3,  "war": 3.5, "bats": "R", "pos": "SS", "team": "MIN"},
    "Nathaniel Lowe":    {"wrc_plus": 110, "ops": .740,  "avg": .268, "hr": 16, "sb": 2,  "war": 2.0, "bats": "L", "pos": "1B", "team": "ARI"},
    "Brandon Nimmo":     {"wrc_plus": 118, "ops": .768,  "avg": .270, "hr": 18, "sb": 6,  "war": 3.0, "bats": "L", "pos": "LF", "team": "NYM"},
    "Tommy Edman":       {"wrc_plus": 108, "ops": .730,  "avg": .262, "hr": 14, "sb": 22, "war": 3.5, "bats": "S", "pos": "2B", "team": "LAD"},
    "Nick Castellanos":  {"wrc_plus": 108, "ops": .730,  "avg": .268, "hr": 20, "sb": 3,  "war": 1.5, "bats": "R", "pos": "RF", "team": "PHI"},
    "Spencer Horwitz":   {"wrc_plus": 110, "ops": .740,  "avg": .270, "hr": 14, "sb": 3,  "war": 2.0, "bats": "L", "pos": "1B", "team": "TOR"},
    "Bryan Reynolds":    {"wrc_plus": 122, "ops": .785,  "avg": .270, "hr": 22, "sb": 8,  "war": 3.5, "bats": "S", "pos": "CF", "team": "PIT"},
    "Ryan McMahon":      {"wrc_plus": 110, "ops": .738,  "avg": .255, "hr": 20, "sb": 5,  "war": 3.0, "bats": "L", "pos": "3B", "team": "COL"},
    "Cedric Mullins":    {"wrc_plus": 100, "ops": .700,  "avg": .248, "hr": 15, "sb": 22, "war": 2.0, "bats": "S", "pos": "CF", "team": "BAL"},
    "Josh Lowe":         {"wrc_plus": 115, "ops": .755,  "avg": .262, "hr": 18, "sb": 25, "war": 3.5, "bats": "L", "pos": "LF", "team": "TB"},
    "Kerry Carpenter":   {"wrc_plus": 125, "ops": .800,  "avg": .275, "hr": 24, "sb": 2,  "war": 3.0, "bats": "L", "pos": "DH", "team": "DET"},
    "Colton Cowser":     {"wrc_plus": 115, "ops": .755,  "avg": .258, "hr": 22, "sb": 5,  "war": 3.0, "bats": "L", "pos": "LF", "team": "BAL"},
    "Michael Busch":     {"wrc_plus": 115, "ops": .755,  "avg": .255, "hr": 22, "sb": 5,  "war": 2.5, "bats": "L", "pos": "1B", "team": "CHC"},
    "Mark Vientos":      {"wrc_plus": 128, "ops": .808,  "avg": .272, "hr": 28, "sb": 2,  "war": 3.5, "bats": "R", "pos": "3B", "team": "NYM"},
    "Yandy Diaz":        {"wrc_plus": 118, "ops": .768,  "avg": .282, "hr": 14, "sb": 3,  "war": 3.0, "bats": "R", "pos": "1B", "team": "TB"},
    "Luis Robert Jr.":   {"wrc_plus": 105, "ops": .718,  "avg": .248, "hr": 18, "sb": 12, "war": 2.0, "bats": "R", "pos": "CF", "team": "CWS"},
    "Brice Turang":      {"wrc_plus": 98,  "ops": .685,  "avg": .262, "hr": 8,  "sb": 28, "war": 2.5, "bats": "L", "pos": "2B", "team": "MIL"},
    "Andrew McCutchen":  {"wrc_plus": 102, "ops": .710,  "avg": .248, "hr": 15, "sb": 5,  "war": 1.0, "bats": "R", "pos": "DH", "team": "PIT"},
}


def _get_hitter_data(name: str, team_abbr: Optional[str] = None) -> dict:
    """Look up hitter data from KNOWN_HITTERS first, then a live ESPN roster lookup."""
    if not name:
        return {}
    for known_name, data in KNOWN_HITTERS.items():
        if _norm(known_name) == _norm(name):
            return {"display_name": known_name, **data}
    live_data = lookup_batter(name, team_abbr)
    if live_data:
        return live_data
    return {}


def _team_wrc_with_hitter(team: str, hitter_wrc: float) -> float:
    """Team wRC+ with this hitter in the lineup (1/9 of plate appearances)."""
    base = TEAM_WRC_PLUS.get(team, 100.0)
    return base + (hitter_wrc - base) / LINEUP_SLOTS


def _team_wrc_without_hitter(team: str) -> float:
    """Team wRC+ replacing the hitter with a league-average bat."""
    base = TEAM_WRC_PLUS.get(team, 100.0)
    return base + (REPLACEMENT_WRC_PLUS - base) / LINEUP_SLOTS


def _win_prob_for_wrc(wrc_plus: float, opp_starter_fip: float = LEAGUE_AVG_FIP,
                      park_factor: float = 1.0, is_home: bool = True) -> float:
    """Win probability for a team with given wRC+ facing a league-average starter."""
    mu_for_f5, mu_for_l4 = expected_runs_split(
        wrc_plus, opp_starter_fip, LEAGUE_BULLPEN_FIP, park_factor, is_home
    )
    mu_against_f5, mu_against_l4 = expected_runs_split(
        DEFAULT_WRC_PLUS, LEAGUE_AVG_FIP, LEAGUE_BULLPEN_FIP, park_factor, not is_home
    )
    mu_for = mu_for_f5 + mu_for_l4
    mu_against = mu_against_f5 + mu_against_l4
    matrix = build_run_matrix(mu_for, mu_against) if is_home else build_run_matrix(mu_against, mu_for)
    ml = moneyline_market(matrix)
    return ml["p_home_win"] if is_home else ml["p_away_win"]


def compute_hitter_impact(
    hitter_name: str,
    team_abbr: Optional[str] = None,
    park_factor: float = 1.0,
    is_home: bool = True,
) -> dict:
    data = _get_hitter_data(hitter_name, team_abbr)
    if not data:
        hint = "" if team_abbr else " Add a team abbreviation (e.g. PIT) to search live rosters."
        return {"error": f"Hitter '{hitter_name}' not found in database.{hint}"}

    display_name = data.get("display_name", hitter_name)
    wrc = data.get("wrc_plus", 100)
    ops = data.get("ops", 0)
    avg = data.get("avg", 0)
    hr = data.get("hr", 0)
    sb = data.get("sb", 0)
    war = data.get("war")
    bats = data.get("bats", "R")
    pos = data.get("pos", "")
    team = team_abbr or data.get("team", "")

    wrc_with = _team_wrc_with_hitter(team, wrc)
    wrc_without = _team_wrc_without_hitter(team)

    win_prob_with = _win_prob_for_wrc(wrc_with, park_factor=park_factor, is_home=is_home)
    win_prob_without = _win_prob_for_wrc(wrc_without, park_factor=park_factor, is_home=is_home)

    impact_pp = round((win_prob_with - win_prob_without) * 100, 1)

    if impact_pp >= 6:
        tier, tier_desc = "MVP", "Elite franchise player — dramatically lifts the entire lineup"
    elif impact_pp >= 3:
        tier, tier_desc = "ALL-STAR", "Top-tier bat — clearly moves the needle for any team"
    elif impact_pp >= 1:
        tier, tier_desc = "STARTER", "Above-average hitter — solid everyday contributor"
    elif impact_pp >= -0.5:
        tier, tier_desc = "AVERAGE", "Replacement-level bat — minimal impact on win probability"
    elif impact_pp >= -2:
        tier, tier_desc = "BENCH", "Below-average hitter — team is slightly worse with this bat"
    else:
        tier, tier_desc = "REPLACEMENT", "Significant drag — team should upgrade this spot"

    trade_note = ""
    for known_name, note in HITTER_TRADE_NEWS.items():
        if _norm(known_name) == _norm(hitter_name):
            trade_note = note
            break

    return {
        "hitter": display_name,
        "team": team,
        "bats": bats,
        "pos": pos,
        "impact_pp": impact_pp,
        "tier": tier,
        "tier_desc": tier_desc,
        "wrc_plus": wrc,
        "ops": ops,
        "avg": avg,
        "hr": hr,
        "sb": sb,
        "war": war,
        "team_wrc_with": round(wrc_with, 1),
        "team_wrc_without": round(wrc_without, 1),
        "win_prob_with": round(win_prob_with * 100, 1),
        "win_prob_replacement": round(win_prob_without * 100, 1),
        "trade_note": trade_note,
    }


def search_hitters(query: str, limit: int = 10) -> list[dict]:
    results = []
    q = _norm(query)
    if not q or len(q) < 2:
        return results
    for name, data in KNOWN_HITTERS.items():
        if q in _norm(name):
            results.append({
                "key": _norm(name),
                "display_name": name,
                "team": data.get("team", ""),
                "wrc_plus": data.get("wrc_plus"),
                "pos": data.get("pos", ""),
                "type": "hitter",
            })
    return results[:limit]


def search_players(query: str, limit: int = 10) -> list[dict]:
    """Unified search across pitchers and hitters."""
    q = _norm(query)
    if not q or len(q) < 2:
        return []
    pitchers = search_pitchers(query, limit)
    for p in pitchers:
        p["type"] = "pitcher"
    hitters = search_hitters(query, limit)
    combined = pitchers + hitters
    return combined[:limit]
