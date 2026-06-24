"""
Bulletproof .env loader — call load_env() from any script.
Handles Windows CRLF line endings, BOM, inline comments, and quoted values.
Falls back to manual parsing if python-dotenv is unavailable.
"""
from __future__ import annotations
import os
from pathlib import Path

_ENV_PATH = Path(__file__).parent / ".env"


def load_env(path: Path = _ENV_PATH) -> None:
    """Load .env into os.environ, never raising, always preferring existing env vars."""
    if not path.exists():
        return

    # Try python-dotenv first (handles edge cases well)
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(path), override=False)
        return
    except Exception:
        pass

    # Manual fallback — handles CRLF, BOM, comments, quoted values
    try:
        raw = path.read_bytes()
        # Strip UTF-8 BOM if present
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        text = raw.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip().rstrip("\r")        # strip CR from CRLF
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip inline comments (# preceded by space)
            if " #" in val:
                val = val[:val.index(" #")].strip()
            # Strip surrounding quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


# Auto-load when imported
load_env()
