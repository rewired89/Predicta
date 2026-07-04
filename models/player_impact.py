"""
Player Impact Score — how much a pitcher moves the needle on game outcomes.

Runs the split Poisson model twice:
  1. With the real pitcher's FIP (from feature store)
  2. With a league-average replacement (FIP = 4.00)
The delta in win probability = the pitcher's impact in percentage points.
"""
from __future__ import annotations
from typing import Optional

from fetchers.feature_store import load_store, lookup_pitcher_features, _norm
from models.baseball_market import (
    expected_runs_split, build_run_matrix, moneyline_market,
    LEAGUE_AVG_FIP, LEAGUE_BULLPEN_FIP,
)

REPLACEMENT_FIP = 4.00
DEFAULT_WRC_PLUS = 100.0

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
        barrel_pct = data.get("barrel_pct_against") or data.get("barrel_pct") or 0.075
        hard_hit = data.get("hard_hit_pct", 0.35)
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
        "barrel_pct": data.get("barrel_pct_against") or data.get("barrel_pct"),
        "avg_fb_velo": data.get("avg_fb_velo") or data.get("avg_velo"),
        "csw_pct": data.get("csw_pct"),
        "whiff_pct": data.get("whiff_pct"),
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
