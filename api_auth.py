"""
api_auth.py

Lightweight API-key authentication for Predicta's public /v1 endpoints.

Keys are configured via the PREDICTA_API_KEYS environment variable as a
comma-separated list. Each entry is either a bare key or "key:label":

    PREDICTA_API_KEYS="sk_live_abc123:acme_syndicate,sk_live_def456:betmedia"

Clients authenticate by sending the header `X-API-Key: sk_live_abc123`.

When PREDICTA_API_KEYS is unset or empty, the API runs in open "dev mode" —
no key required — so local development and the existing UI keep working. Set
the variable in production (Railway) to lock the /v1 endpoints down.
"""
from __future__ import annotations
import os
from typing import Optional

from fastapi import Header, HTTPException


def _load_keys() -> dict[str, str]:
    raw = os.environ.get("PREDICTA_API_KEYS", "").strip()
    keys: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, label = part.split(":", 1)
            keys[k.strip()] = label.strip() or "client"
        else:
            keys[part] = "client"
    return keys


def auth_enabled() -> bool:
    return bool(_load_keys())


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """
    FastAPI dependency. Returns the client label on success.
    Raises 401 when keys are configured and the header is missing/invalid.
    In dev mode (no keys configured) returns "dev" and allows the request.
    """
    keys = _load_keys()
    if not keys:
        return "dev"
    if x_api_key and x_api_key in keys:
        return keys[x_api_key]
    raise HTTPException(
        status_code=401,
        detail="Missing or invalid API key. Send header 'X-API-Key'.",
    )


def trade_action_gate_enabled() -> bool:
    return bool(os.environ.get("TRADE_ACTION_PASSCODE", "").strip())


def require_trade_passcode(x_trade_passcode: Optional[str] = Header(default=None)) -> None:
    """
    FastAPI dependency for the handful of endpoints that place or close a
    REAL (even if paper) order — POST /trade/execute/:id, /trade/close/:id,
    and the Low Value equivalents. Unlike require_api_key above, this exists
    because those routes have no auth on them at all otherwise: /trade/*
    isn't covered by PREDICTA_API_KEYS (see this module's docstring — that's
    scoped to /v1), so a real order-placing button on a public dashboard
    would otherwise be clickable by anyone who finds the URL, not just the
    person who owns the account.

    Same fail-open-in-dev-mode convention as require_api_key: if
    TRADE_ACTION_PASSCODE isn't set, this is a no-op — local development and
    a genuinely private deployment keep working with zero extra steps. Set
    it before putting a real Buy/Sell button anywhere reachable by anyone
    other than you.
    """
    expected = os.environ.get("TRADE_ACTION_PASSCODE", "").strip()
    if not expected:
        return
    if x_trade_passcode != expected:
        raise HTTPException(
            status_code=401,
            detail="Missing or incorrect trade passcode. Send header 'X-Trade-Passcode'.",
        )
