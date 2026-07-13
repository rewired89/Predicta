"""
Signal fetcher/calculator.
Most signals require manual input or verified API access.
This module provides calculators and manual-entry helpers.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db


def _save_signal(
    conn,
    match_id: int,
    signal_name: str,
    participant: Optional[str] = None,
    signal_value: Optional[float] = None,
    signal_text: Optional[str] = None,
    source: str = "manual",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO signals
               (match_id, participant, signal_name, signal_value, signal_text, source, captured_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (match_id, participant, signal_name, signal_value, signal_text, source, now),
    )


def log_signal(
    match_id: int,
    signal_name: str,
    participant: Optional[str] = None,
    signal_value: Optional[float] = None,
    signal_text: Optional[str] = None,
    source: str = "manual",
) -> None:
    """Persist a single signal value."""
    with get_db() as conn:
        _save_signal(conn, match_id, signal_name, participant, signal_value, signal_text, source)


def get_signals_for_match(match_id: int) -> dict:
    """Return all signals for a match as a nested dict: {participant: {signal: value}}."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT participant, signal_name, signal_value, signal_text FROM signals WHERE match_id=?",
            (match_id,),
        ).fetchall()
    result: dict = {}
    for row in rows:
        p = row["participant"] or "_match"
        result.setdefault(p, {})
        result[p][row["signal_name"]] = row["signal_value"] if row["signal_value"] is not None else row["signal_text"]
    return result


def compute_form_weighted(results: list[float], n: int = 10, decay: float = 0.9) -> float:
    """
    Weighted form score from last N results (1=win, 0.5=draw, 0=loss).
    Most recent result has highest weight (decay^0), oldest has decay^(n-1).
    """
    recent = results[-n:]
    weights = [decay ** i for i in range(len(recent) - 1, -1, -1)]
    total_w = sum(weights)
    return sum(r * w for r, w in zip(recent, weights)) / total_w if total_w else 0.5


def fetch_signals_for_match(match_id: int) -> dict:
    """Retrieve pre-stored signals and return structured dict for model input."""
    return get_signals_for_match(match_id)
