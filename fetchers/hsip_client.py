"""
HSIP decision-attestation connector.

Records real, already-executed Predicta transactions (a Buy/Sell order
actually placed) to a self-hosted HSIP instance as a tamper-proof,
independently verifiable attestation. HSIP never receives the real trade
content (symbol, price, qty, reasoning) — only a SHA-256 hash of it plus
non-sensitive metadata (decision_type, model_version, strategy_id). See
https://github.com/rewired89/HSIP-1PHASE, "Decision Attestations".

This attests ACTIONS, not predictions: call attest_transaction() only after
an order is actually placed (a real transaction), never at signal/prediction
time — a hypothetical/unexecuted signal is not a transaction.

Config (both required — silently disabled if either is missing, same
fail-safe convention as fetchers/fred.py / fetchers/finnhub.py):
  HSIP_API_KEY   Predicta's own scoped ai_agent bearer key (never the
                 HSIP admin/root key — see HSIP's CLAUDE.md, "Trading Bot
                 Integration", Step 1)
  HSIP_API_URL   e.g. http://127.0.0.1:7474 (desktop mode) or a deployed
                 HSIP instance's public URL

Any other tool wanting the same attestation just needs to set these two env
vars and call attest_transaction() the same way — no HSIP-specific code
beyond this one small file.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

HSIP_API_KEY = os.environ.get("HSIP_API_KEY", "")
HSIP_API_URL = os.environ.get("HSIP_API_URL", "").rstrip("/")
HSIP_ENABLED = bool(HSIP_API_KEY and HSIP_API_URL)
_TIMEOUT_SECS = 5

_identity_cache: dict = {}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {HSIP_API_KEY}",
        "Content-Type": "application/json",
    }


def _get_identity() -> Optional[dict]:
    """Predicta's own HSIP identity (Ed25519 verify key) — resolved once per process, auto-created on first call if missing."""
    if "identity" in _identity_cache:
        return _identity_cache["identity"]
    try:
        r = requests.post(f"{HSIP_API_URL}/v1/identity", headers=_headers(), timeout=_TIMEOUT_SECS)
        r.raise_for_status()
        identity = r.json()
        _identity_cache["identity"] = identity
        return identity
    except Exception as exc:
        log.warning(f"[HSIP] Could not resolve Predicta's HSIP identity: {exc}")
        return None


def hash_payload(payload: dict) -> str:
    """Hex SHA-256 of a canonical (sorted-key) JSON encoding of payload — the only fingerprint of the real transaction that ever reaches HSIP."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attest_transaction(
    decision_type: str,
    strategy_id: str,
    payload: dict,
    model_version: str = "predicta-v1",
) -> Optional[dict]:
    """
    Attest one real, already-executed Predicta transaction to HSIP.

    decision_type: the actual action taken, e.g. "buy" / "sell" — the
                   executed order side, never the model's prediction or
                   reasoning.
    strategy_id:   which engine/strategy produced it, e.g.
                   "high_value_intraday" / "low_value_swing".
    payload:       the real transaction details (symbol, side, qty, price,
                   alpaca_order_id, predicta_trade_id, ...) — hashed
                   locally via hash_payload(); the raw dict itself is never
                   sent to HSIP.

    Returns the HSIP receipt dict, or None if attestation is disabled or
    failed. No-op (returns None immediately, no network call) if
    HSIP_API_KEY/HSIP_API_URL aren't both set. Never raises — any failure
    (HSIP down, misconfigured, network hiccup) is logged and swallowed, so
    HSIP can never block or break a real trade.
    """
    if not HSIP_ENABLED:
        return None
    try:
        identity = _get_identity()
        if not identity:
            return None
        accountable_key = identity.get("verify_key")
        if not accountable_key:
            log.warning("[HSIP] Identity response missing verify_key — skipping attestation")
            return None

        body = {
            "accountable_key": accountable_key,
            "model_version": model_version,
            "strategy_id": strategy_id,
            "decision_type": decision_type,
            "payload_hash": hash_payload(payload),
        }
        r = requests.post(
            f"{HSIP_API_URL}/v1/decisions", headers=_headers(), json=body, timeout=_TIMEOUT_SECS
        )
        r.raise_for_status()
        receipt = r.json()
        log.info(f"[HSIP] Attested {decision_type} ({strategy_id}) — decision_id={receipt.get('decision_id')}")
        return receipt
    except Exception as exc:
        log.warning(f"[HSIP] Attestation failed (the transaction itself is unaffected): {exc}")
        return None
