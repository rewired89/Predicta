"""
Shared Anthropic client factory.

Auth priority:
  1. ANTHROPIC_API_KEY env var  — local .env or CI secret
  2. CLAUDE_SESSION_INGRESS_TOKEN_FILE — Claude Code remote container session token
     (uses bearer auth_token, not api_key)

Raises RuntimeError if neither is available.
"""
from __future__ import annotations
import os
from pathlib import Path


def get_client():
    """Return an authenticated anthropic.Anthropic instance."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return anthropic.Anthropic(api_key=api_key)

    token_file = os.environ.get("CLAUDE_SESSION_INGRESS_TOKEN_FILE", "")
    if token_file and Path(token_file).exists():
        token = Path(token_file).read_text().strip()
        if token:
            return anthropic.Anthropic(auth_token=token)

    raise RuntimeError(
        "No Anthropic credentials found. Set ANTHROPIC_API_KEY in .env "
        "or run inside a Claude Code remote session."
    )
