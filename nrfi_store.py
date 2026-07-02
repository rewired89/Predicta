"""
nrfi_store.py

Read + aggregate helpers over the committed daily NRFI prediction files
(data/nrfi_predictions/YYYY-MM-DD.json), which are the persistent, auditable
record produced by scripts/daily_nrfi.py and committed to git by the
nrfi_daily GitHub Actions workflow.

These files — not the ephemeral CI database — are the source of truth for the
public /v1 API, because they live in git history (timestamped, append-only)
and survive redeploys.
"""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_REPO    = Path(__file__).resolve().parent
PRED_DIR = _REPO / "data" / "nrfi_predictions"

_BET_VERDICTS = ("BET", "LEAN")


def list_dates() -> list[str]:
    """All dates with a committed prediction file, oldest → newest."""
    if not PRED_DIR.exists():
        return []
    dates = []
    for f in PRED_DIR.glob("*.json"):
        stem = f.stem
        try:
            datetime.strptime(stem, "%Y-%m-%d")
            dates.append(stem)
        except ValueError:
            continue
    return sorted(dates)


def latest_date() -> Optional[str]:
    d = list_dates()
    return d[-1] if d else None


def load_date(game_date: str) -> list[dict]:
    """Load one date's prediction records; [] if the file is missing."""
    f = PRED_DIR / f"{game_date}.json"
    if not f.exists():
        return []
    try:
        with open(f) as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return []


def _recent_dates(days: int) -> list[str]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [d for d in list_dates() if d >= cutoff]


def _plays(records: list[dict]) -> list[dict]:
    return [r for r in records
            if r.get("verdict") in _BET_VERDICTS and not r.get("error")]


def aggregate_performance(days: int = 3650) -> dict:
    """
    Win/loss + ROI + CLV across resolved BET/LEAN plays in the recent window.
    ROI assumes -110 juice (win = +0.909u, loss = -1.0u).
    """
    import math

    all_plays: list[dict] = []
    for d in _recent_dates(days):
        all_plays += _plays(load_date(d))

    resolved = [p for p in all_plays if p.get("won") in (0, 1)]
    wins   = sum(1 for p in resolved if p.get("won") == 1)
    losses = len(resolved) - wins
    pnl    = sum(p.get("pnl_units") or 0 for p in resolved)
    wr     = (wins / len(resolved)) if resolved else None
    roi    = (pnl / len(resolved) * 100) if resolved else None

    p_value = None
    ci_95 = None
    if resolved and len(resolved) > 1 and wr is not None:
        se    = math.sqrt(wr * (1 - wr) / len(resolved))
        ci_95 = [round(max(0.0, wr - 1.96 * se) * 100, 1),
                 round(min(1.0, wr + 1.96 * se) * 100, 1)]
        z     = (wr - 0.524) / math.sqrt(0.524 * 0.476 / len(resolved))
        try:
            import scipy.stats as _st
            p_value = round(float(1 - _st.norm.cdf(z)), 4)
        except Exception:
            p_value = None

    clv = aggregate_clv(days)

    if p_value is not None and p_value < 0.05 and (roi or 0) > 3:
        verdict = "EDGE PROVEN"
    elif clv.get("avg_clv_pp") is not None and clv["avg_clv_pp"] > 0 and clv.get("n", 0) >= 20:
        verdict = "BEATING THE CLOSE"
    elif wr is not None and wr > 0.524:
        verdict = "EDGE EXISTS"
    elif len(resolved) < 50:
        verdict = "TOO EARLY"
    else:
        verdict = "NO EDGE DETECTED"

    return {
        "verdict":      verdict,
        "n_plays":      len(all_plays),
        "n_resolved":   len(resolved),
        "wins":         wins,
        "losses":       losses,
        "win_rate_pct": round(wr * 100, 1) if wr is not None else None,
        "roi_pct":      round(roi, 2) if roi is not None else None,
        "pnl_units":    round(pnl, 3),
        "ci_95":        ci_95,
        "p_value":      p_value,
        "clv":          clv,
        "window_days":  days,
    }


def aggregate_clv(days: int = 3650) -> dict:
    """
    Closing Line Value summary across plays that have both entry + closing lines.
    CLV accumulates every game (independent of outcome), so it's the fastest
    signal of edge and the number sharp buyers ask for.
    """
    clv_plays: list[dict] = []
    for d in _recent_dates(days):
        for p in _plays(load_date(d)):
            if p.get("clv_pp") is not None:
                clv_plays.append(p)

    if not clv_plays:
        return {"n": 0, "avg_clv_pp": None, "beat_close_pct": None,
                "message": "No closing-line data yet. Set ODDS_API_KEY and run the "
                           "capture-odds step so CLV can accumulate."}

    beat = sum(1 for p in clv_plays if p.get("beat_close") == 1)
    avg  = sum(p["clv_pp"] for p in clv_plays) / len(clv_plays)
    return {
        "n":              len(clv_plays),
        "avg_clv_pp":     round(avg, 3),
        "beat_close":     beat,
        "beat_close_pct": round(beat / len(clv_plays) * 100, 1),
    }
