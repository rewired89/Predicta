"""
Local data cache reader.
GitHub Actions fetches live sports data and commits it to data/live/.
When running in a remote container where sports APIs are blocked,
fetchers call load_cached() to read that committed data instead.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

LIVE_DIR = Path(__file__).parent.parent / "data" / "live"
MAX_AGE_HOURS = 30  # accept data up to 30h old (covers overnight + off-days)


def load_cached(name: str, max_age_hours: int = MAX_AGE_HOURS) -> Optional[dict | list]:
    """
    Load a cached JSON file from data/live/.
    Returns the 'data' payload if the file exists and is fresh, else None.
    """
    path = LIVE_DIR / name
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        raw_ts = payload.get("fetched_at", "")
        if raw_ts:
            fetched_at = datetime.fromisoformat(raw_ts)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - fetched_at
            if age > timedelta(hours=max_age_hours):
                return None
        return payload.get("data")
    except Exception:
        return None


def cache_age_hours(name: str) -> Optional[float]:
    """Return age of cached file in hours, or None if missing/unreadable."""
    path = LIVE_DIR / name
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        raw_ts = payload.get("fetched_at", "")
        if not raw_ts:
            return None
        fetched_at = datetime.fromisoformat(raw_ts)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
    except Exception:
        return None
