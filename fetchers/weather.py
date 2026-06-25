"""
Weather signal fetcher for baseball park conditions.
Stores temperature, wind, and derived factors as DB signals.
Does NOT apply to the run model yet — enable after 50+ predictions
validate the effect (see commented block in analyze_baseball.py).

Requires: pip install httpx
API key:  OPENWEATHER_API_KEY env var (free at openweathermap.org/api, 1000 calls/day)
"""
from __future__ import annotations
import math
import os
from datetime import datetime
from typing import Optional

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

OPENWEATHER_API_KEY: str = os.environ.get("OPENWEATHER_API_KEY", "")

# lat/lon for all 30 MLB stadiums
STADIUM_COORDS: dict[str, tuple[float, float]] = {
    "ARI": (33.4453, -112.0667),  # Chase Field
    "ATL": (33.8908, -84.4678),   # Truist Park
    "BAL": (39.2839, -76.6217),   # Camden Yards
    "BOS": (42.3467, -71.0972),   # Fenway Park
    "CHC": (41.9484, -87.6553),   # Wrigley Field
    "CWS": (41.8301, -87.6339),   # Guaranteed Rate Field
    "CIN": (39.0979, -84.5082),   # Great American Ball Park
    "CLE": (41.4962, -81.6852),   # Progressive Field
    "COL": (39.7561, -104.9942),  # Coors Field
    "DET": (42.3390, -83.0485),   # Comerica Park
    "HOU": (29.7573, -95.3555),   # Minute Maid Park (retractable)
    "KC":  (39.0517, -94.4806),   # Kauffman Stadium
    "LAA": (33.8003, -117.8827),  # Angel Stadium
    "LAD": (34.0739, -118.2400),  # Dodger Stadium
    "MIA": (25.7781, -80.2195),   # loanDepot park (retractable)
    "MIL": (43.0280, -87.9712),   # American Family Field (retractable)
    "MIN": (44.9817, -93.2775),   # Target Field
    "NYM": (40.7571, -73.8458),   # Citi Field
    "NYY": (40.8296, -73.9262),   # Yankee Stadium
    "OAK": (37.7516, -122.2005),  # Oakland Coliseum
    "PHI": (39.9058, -75.1665),   # Citizens Bank Park
    "PIT": (40.4469, -80.0057),   # PNC Park
    "SD":  (32.7073, -117.1566),  # Petco Park
    "SEA": (47.5914, -122.3325),  # T-Mobile Park (retractable)
    "SF":  (37.7786, -122.3893),  # Oracle Park
    "STL": (38.6226, -90.1928),   # Busch Stadium
    "TB":  (27.7683, -82.6534),   # Tropicana Field (fixed dome)
    "TEX": (32.7513, -97.0825),   # Globe Life Field (retractable)
    "TOR": (43.6414, -79.3894),   # Rogers Centre (retractable)
    "WSH": (38.8730, -77.0074),   # Nationals Park
}

# Parks with dome or retractable roof (wind_factor = 0.0 when closed)
DOME_PARKS: frozenset[str] = frozenset({"HOU", "MIA", "MIL", "SEA", "TB", "TEX", "TOR"})

# Mapping from ESPN team name formats → stadium code.
# Handles short codes, nicknames, and full names.
TEAM_TO_STADIUM: dict[str, str] = {
    # Short codes
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
    "CHC": "CHC", "CWS": "CWS", "CIN": "CIN", "CLE": "CLE",
    "COL": "COL", "DET": "DET", "HOU": "HOU", "KC": "KC",
    "LAA": "LAA", "LAD": "LAD", "MIA": "MIA", "MIL": "MIL",
    "MIN": "MIN", "NYM": "NYM", "NYY": "NYY", "OAK": "OAK",
    "PHI": "PHI", "PIT": "PIT", "SD": "SD", "SEA": "SEA",
    "SF": "SF", "STL": "STL", "TB": "TB", "TEX": "TEX",
    "TOR": "TOR", "WSH": "WSH",
    # Nicknames (ESPN short display names)
    "Diamondbacks": "ARI", "Braves": "ATL", "Orioles": "BAL",
    "Red Sox": "BOS", "Cubs": "CHC", "White Sox": "CWS",
    "Reds": "CIN", "Guardians": "CLE", "Rockies": "COL",
    "Tigers": "DET", "Astros": "HOU", "Royals": "KC",
    "Angels": "LAA", "Dodgers": "LAD", "Marlins": "MIA",
    "Brewers": "MIL", "Twins": "MIN", "Mets": "NYM",
    "Yankees": "NYY", "Athletics": "OAK", "Phillies": "PHI",
    "Pirates": "PIT", "Padres": "SD", "Mariners": "SEA",
    "Giants": "SF", "Cardinals": "STL", "Rays": "TB",
    "Rangers": "TEX", "Blue Jays": "TOR", "Nationals": "WSH",
    # Full names
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "Seattle Mariners": "SEA",
    "San Francisco Giants": "SF", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}


def team_to_stadium_code(team_name: str) -> Optional[str]:
    """Resolve a team name (any format) to a stadium code. Returns None if unknown."""
    if not team_name:
        return None
    # Exact match first
    code = TEAM_TO_STADIUM.get(team_name)
    if code:
        return code
    # Case-insensitive substring match
    lower = team_name.lower()
    for key, val in TEAM_TO_STADIUM.items():
        if key.lower() in lower or lower in key.lower():
            return val
    return None


def _wind_direction_factor(wind_deg: float, stadium_code: str) -> float:
    """
    Returns wind alignment with outfield: +1.0 = blowing straight out to CF
    (boosts HRs), -1.0 = blowing straight in from CF (suppresses HRs), 0.0 = dome.

    Most MLB outfields face roughly NE (center field at ~45°). This is a simplified
    approximation; precise per-park orientations can improve this later.
    """
    if stadium_code in DOME_PARKS:
        return 0.0
    # Convert: wind reported as direction it comes FROM; we want direction it blows TO
    blowing_toward = (wind_deg + 180) % 360
    # Alignment with ideal "out to CF" direction (~45° NE for most parks)
    out_rad = math.radians(45)
    blow_rad = math.radians(blowing_toward)
    alignment = (math.cos(out_rad) * math.cos(blow_rad)
                 + math.sin(out_rad) * math.sin(blow_rad))
    return round(alignment, 3)


def fetch_game_weather(
    stadium_code: str,
    game_date: str,
    game_time: str = "19:05",
) -> Optional[dict]:
    """
    Fetch 5-day hourly forecast from OpenWeatherMap for a stadium.

    Args:
        stadium_code: 3-letter MLB team code (e.g. "BOS", "COL")
        game_date:    ISO date "YYYY-MM-DD"
        game_time:    Local start time "HH:MM" (default 7:05 PM)

    Returns:
        dict with temp_f, wind_mph, wind_deg, wind_factor, temp_factor,
        is_dome, forecast_time, source — or None on any failure.
    """
    if not _HAS_HTTPX or not OPENWEATHER_API_KEY:
        return None

    coords = STADIUM_COORDS.get(stadium_code.upper())
    if not coords:
        return None

    lat, lon = coords
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "imperial",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    try:
        game_dt = datetime.strptime(f"{game_date} {game_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None

    # Find the forecast entry closest to game time (3-hour intervals)
    closest = None
    min_diff = float("inf")
    for entry in data.get("list", []):
        diff = abs((datetime.fromtimestamp(entry["dt"]) - game_dt).total_seconds())
        if diff < min_diff:
            min_diff = diff
            closest = entry

    if not closest or min_diff > 10_800:  # > 3 hours away → too uncertain
        return None

    main = closest.get("main", {})
    wind = closest.get("wind", {})
    temp_f   = main.get("temp")
    wind_mph = wind.get("speed")
    wind_deg = wind.get("deg", 0)

    wind_factor = _wind_direction_factor(wind_deg, stadium_code.upper()) if wind_mph else 0.0
    # ~3 ft extra fly-ball distance per 10°F above 72°F
    temp_factor = round(1.0 + ((temp_f - 72.0) * 0.003), 4) if temp_f is not None else 1.0

    return {
        "temp_f":        round(temp_f, 1) if temp_f is not None else None,
        "wind_mph":      round(wind_mph, 1) if wind_mph is not None else None,
        "wind_deg":      wind_deg,
        "wind_factor":   wind_factor,
        "temp_factor":   temp_factor,
        "is_dome":       stadium_code.upper() in DOME_PARKS,
        "forecast_time": closest.get("dt_txt", ""),
        "source":        "openweather",
    }


def weather_to_signals(weather: Optional[dict]) -> dict[str, float]:
    """
    Convert weather dict to flat {signal_name: float} for DB logging.
    Falls back to neutral values when weather is None (no API key / fetch failed).
    weather_confidence = 0.0 means no real data; 1.0 means forecast fetched.
    """
    if not weather:
        return {
            "temp_f":             72.0,
            "wind_mph":           0.0,
            "wind_factor":        0.0,
            "temp_factor":        1.0,
            "is_dome":            0.0,
            "weather_confidence": 0.0,
        }
    return {
        "temp_f":             float(weather.get("temp_f") or 72.0),
        "wind_mph":           float(weather.get("wind_mph") or 0.0),
        "wind_factor":        float(weather.get("wind_factor", 0.0)),
        "temp_factor":        float(weather.get("temp_factor", 1.0)),
        "is_dome":            1.0 if weather.get("is_dome") else 0.0,
        "weather_confidence": 1.0,
    }
