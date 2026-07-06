"""
FRED (Federal Reserve Economic Data) fetcher — free API, requires FRED_API_KEY
env var (register at fred.stlouisfed.org). Fails safe (returns None/empty)
when the key is absent or the request fails, matching the OPENWEATHER_API_KEY /
PANDASCORE_API_KEY convention used elsewhere in this codebase.

Used for read-only macro regime tags (Kimi review, round 6) — lagging
indicators logged for post-hoc analysis, never used to gate trades directly
except the explicit Dalio 3-force overlay in paper_runner.py, which only
elevates the logging threshold (same class of gate as the existing SPY-gap
regime check) rather than blocking collection outright.
"""
from __future__ import annotations
import os
from typing import Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


def _fred_get(series_id: str, limit: int = 1) -> list[dict]:
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key or not _HAS_REQUESTS:
        return []
    try:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        r = requests.get(FRED_BASE_URL, params=params, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        return [o for o in obs if o.get("value") not in (None, ".")]
    except Exception:
        return []


def get_latest_value(series_id: str) -> Optional[float]:
    """Most recent published value for a FRED series, or None if unavailable."""
    obs = _fred_get(series_id, limit=1)
    if not obs:
        return None
    try:
        return float(obs[0]["value"])
    except Exception:
        return None


def get_series_stats(series_id: str, n_obs: int = 252) -> dict:
    """
    Mean/std over the last n_obs observations, for "N std dev above mean"
    checks (Dalio 3-force overlay). Returns {"mean", "std", "n", "latest"} —
    all None/0 if the series can't be fetched (no key, or request failure).
    """
    obs = _fred_get(series_id, limit=n_obs)
    vals: list[float] = []
    for o in obs:
        try:
            vals.append(float(o["value"]))
        except Exception:
            continue
    if len(vals) < 10:
        return {"mean": None, "std": None, "n": len(vals), "latest": vals[0] if vals else None}
    mean = sum(vals) / len(vals)
    var  = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"mean": round(mean, 4), "std": round(var ** 0.5, 4), "n": len(vals), "latest": round(vals[0], 4)}
