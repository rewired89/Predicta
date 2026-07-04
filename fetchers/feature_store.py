"""
fetchers/feature_store.py

Precomputed pitcher feature store — the fix for FanGraphs/Savant being blocked
from datacenter IPs (GitHub Actions, Railway).

FanGraphs' leaders endpoint blocks server/datacenter IPs, so live enrichment
silently fails in production and every starter collapses to ESPN defaults —
which flattens the NRFI model to ~51% on every game. This module reads a
committed JSON snapshot (data/nrfi_feature_store.json) built locally, from a
residential IP, by scripts/build_feature_store.py. enrich_starter() consults
this store first, so the model gets real SIERA/CSW%/O-Swing%/barrel% etc. in
CI without ever touching FanGraphs at runtime.

Refresh cadence: re-run the build script locally every few days (pitcher stats
move slowly) and commit the updated JSON — the same "run locally, commit"
rhythm as retraining.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "nrfi_feature_store.json"
_CACHE: Optional[dict] = None


def _norm(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def load_store() -> dict:
    """Load + cache the committed feature store. Returns {} if absent/invalid."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not _STORE_PATH.exists():
        _CACHE = {}
        return _CACHE
    try:
        with open(_STORE_PATH) as f:
            _CACHE = json.load(f)
    except (ValueError, OSError):
        _CACHE = {}
    return _CACHE


def store_meta() -> dict:
    """Season + build timestamp + pitcher count, for diagnostics."""
    s = load_store()
    p = s.get("pitchers", {})
    return {"season": s.get("season"), "built_at": s.get("built_at"),
            "n_pitchers": len(p), "sources": s.get("sources", [])}


def store_freshness() -> dict:
    """
    How stale is the feature store? Returns age_days, is_stale (>7d),
    is_expired (>14d), and a human label for the daily report.
    """
    meta = store_meta()
    built = meta.get("built_at")
    if not built:
        return {"age_days": None, "is_stale": True, "is_expired": True,
                "label": "no feature store", "sources": []}
    try:
        ts = datetime.fromisoformat(built)
    except (ValueError, TypeError):
        return {"age_days": None, "is_stale": True, "is_expired": True,
                "label": "unparseable timestamp", "sources": meta.get("sources", [])}
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    return {
        "age_days": round(age, 1),
        "is_stale": age > 7,
        "is_expired": age > 14,
        "label": (f"{age:.0f}d ago" if age >= 1 else "today"),
        "sources": meta.get("sources", []),
        "n_pitchers": meta.get("n_pitchers", 0),
    }


def lookup_pitcher_features(name: str) -> dict:
    """
    Return the precomputed feature dict for a pitcher (by name), or {} if the
    store is missing or the pitcher isn't found. Exact normalized match first,
    then a last-name + first-initial fallback for minor spelling differences.
    """
    if not name or name.strip().lower() in ("tbd", "unknown", ""):
        return {}
    pitchers = load_store().get("pitchers", {})
    if not pitchers:
        return {}

    key = _norm(name)
    if key in pitchers:
        return pitchers[key]

    # Fallback: match on last name + first initial (handles accents/Jr. etc.)
    parts = name.strip().split()
    if len(parts) >= 2:
        target = _norm(parts[-1]) + key[:1]
        for k, v in pitchers.items():
            disp = v.get("display_name", "")
            dparts = disp.strip().split()
            if len(dparts) >= 2 and _norm(dparts[-1]) + _norm(dparts[0])[:1] == target:
                return v
    return {}
