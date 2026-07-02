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
