"""
N-gram bar pattern matching for intraday signal generation.

Treats 5-min price sequences like language sequences — predict the next
"word" (bar direction) from the last N bars, using historical frequency tables.

Inspired by Peter Brown and Robert Mercer's IBM speech recognition work:
  price bar direction = word, sequence of bars = sentence,
  predict next direction from context (n-gram).

Requires 6+ months of 5-min bar history to build the frequency table.
Build once per week via build_ngram_from_alpaca; load from DB at signal time.

Signal is intentionally conservative: only fires at >52% historical edge
with at least min_samples occurrences — avoids false positives from sparse patterns.

Context window: 4 bars = 20 minutes (Kimi review — 3 bars/15 min showed weak
directional autocorrelation, ~0.05-0.12 in large-cap 5-min bars, too close to
the 50% baseline to be reliably distinguishable from noise at only slightly
elevated thresholds; 4 bars is closer to the model's 30-60 min intended hold
and still leaves ~115 expected occurrences per pattern over 6 months of data,
comfortably above the min_samples floor).
"""
from __future__ import annotations
import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Optional

PATTERN_LENGTH = 4   # 4-bar context → 3^4 = 81 possible patterns (U/D/E each bar)
_THRESHOLD_PCT = 0.52  # minimum directional frequency to call a signal
_UP_THRESH = 1.0001    # >0.01% move = "Up"
_DN_THRESH = 0.9999    # <-0.01% move = "Down"; otherwise "Equal"


# ── Pattern encoding ──────────────────────────────────────────────────────────

def encode_bar(current_close: float, previous_close: float) -> str:
    """Single bar direction: U (up), D (down), E (equal within 1bp)."""
    if previous_close <= 0:
        return "E"
    ratio = current_close / previous_close
    if ratio > _UP_THRESH:
        return "U"
    elif ratio < _DN_THRESH:
        return "D"
    return "E"


def encode_sequence(closes: list[float]) -> str:
    """
    Encode a list of closes into a pattern string.
    len(closes) must be ≥ 2; returns string of length (len-1).
    Example: [100, 101, 100.5] → "UD"
    """
    if len(closes) < 2:
        return ""
    return "".join(encode_bar(closes[i], closes[i - 1]) for i in range(1, len(closes)))


# ── Table building ────────────────────────────────────────────────────────────

def build_pattern_table(
    closes: list[float],
    pattern_len: int = PATTERN_LENGTH,
) -> dict[str, dict[str, int]]:
    """
    Build frequency table from historical closes.

    For each overlapping window of (pattern_len + 1) closes:
      - encode pattern = first pattern_len bars
      - record next bar direction

    Returns: {pattern_str: {"U": n, "D": n, "E": n}}
    """
    table: dict[str, dict[str, int]] = defaultdict(lambda: {"U": 0, "D": 0, "E": 0})

    if len(closes) < pattern_len + 2:
        return dict(table)

    for i in range(pattern_len, len(closes) - 1):
        # Pattern: directions over closes[i-pattern_len] … closes[i]
        pattern = encode_sequence(closes[i - pattern_len: i + 1])
        # Next bar direction: closes[i] → closes[i+1]
        next_dir = encode_bar(closes[i + 1], closes[i])
        table[pattern][next_dir] += 1

    return {k: dict(v) for k, v in table.items()}


# ── Persistence ───────────────────────────────────────────────────────────────

def save_pattern_table(symbol: str, table: dict, bar_count: int) -> None:
    """Upsert pattern table into ngram_models (unique on symbol, pattern_len)."""
    from db.database import get_db
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO ngram_models
                (symbol, pattern_len, bar_count, table_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (symbol, PATTERN_LENGTH, bar_count, json.dumps(table), datetime.now().isoformat()),
        )


def load_pattern_table(symbol: str, max_age_days: int = 7) -> Optional[dict]:
    """
    Load pattern table from DB if it exists and was updated within max_age_days.
    Returns None if not found or stale (caller should trigger a rebuild).
    """
    from db.database import get_db
    try:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT table_json, updated_at FROM ngram_models
                WHERE symbol = ? AND pattern_len = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (symbol, PATTERN_LENGTH),
            ).fetchone()
    except Exception:
        return None  # Table doesn't exist yet — caller should run init_db

    if not row:
        return None

    updated = datetime.fromisoformat(row["updated_at"])
    age_days = (datetime.now() - updated).total_seconds() / 86400
    if age_days > max_age_days:
        return None  # Stale — caller should rebuild

    return json.loads(row["table_json"])


# ── Signal generation ─────────────────────────────────────────────────────────

def ngram_signal(
    symbol: str,
    recent_closes: list[float],
    min_samples: int = 40,
) -> dict:
    """
    Generate an n-gram pattern signal for the current bar context.

    Looks up the last PATTERN_LENGTH bar pattern in the stored frequency table.
    Returns UP/DOWN/NONE with confidence (0–100 scale) and historical win rate.

    Args:
        symbol:        ticker for table lookup
        recent_closes: at least PATTERN_LENGTH+1 recent closes (5-min bars)
        min_samples:   ignore patterns seen fewer than this many times historically

    Returns signal dict — never raises; returns NONE on any failure.
    """
    if len(recent_closes) < PATTERN_LENGTH + 1:
        return {
            "signal":     "NONE",
            "confidence": 0,
            "reason":     f"Need {PATTERN_LENGTH + 1} bars, have {len(recent_closes)}",
        }

    current_pattern = encode_sequence(recent_closes[-(PATTERN_LENGTH + 1):])
    table = load_pattern_table(symbol)

    if table is None:
        return {
            "signal":     "NONE",
            "confidence": 0,
            "reason":     "No pattern table — run build_ngram_from_alpaca first",
            "pattern":    current_pattern,
        }

    if current_pattern not in table:
        return {
            "signal":     "NONE",
            "confidence": 0,
            "reason":     f"Pattern '{current_pattern}' not in historical data",
            "pattern":    current_pattern,
        }

    counts = table[current_pattern]
    total  = sum(counts.values())

    if total < min_samples:
        return {
            "signal":     "NONE",
            "confidence": 0,
            "reason":     f"Pattern seen {total} times, need {min_samples}",
            "pattern":    current_pattern,
            "n_historical": total,
        }

    p_up   = counts.get("U", 0) / total
    p_down = counts.get("D", 0) / total
    p_flat = counts.get("E", 0) / total

    if p_up > _THRESHOLD_PCT:
        return {
            "signal":              "UP",
            "confidence":          round((p_up - 0.5) * 200, 1),  # 0–100 scale
            "historical_win_rate": round(p_up, 3),
            "pattern":             current_pattern,
            "n_historical":        total,
            "expected_edge":       round(p_up - 0.5, 3),
        }
    elif p_down > _THRESHOLD_PCT:
        return {
            "signal":              "DOWN",
            "confidence":          round((p_down - 0.5) * 200, 1),
            "historical_win_rate": round(p_down, 3),
            "pattern":             current_pattern,
            "n_historical":        total,
            "expected_edge":       round(p_down - 0.5, 3),
        }
    else:
        return {
            "signal":              "NONE",
            "confidence":          0,
            "historical_win_rate": round(max(p_up, p_down), 3),
            "pattern":             current_pattern,
            "n_historical":        total,
            "reason":              f"No edge: U={p_up:.2f}, D={p_down:.2f}, E={p_flat:.2f}",
        }


def validate_ngram_patterns(symbol: str, significance: float = 0.05) -> dict:
    """
    Binomial significance test per pattern in the stored frequency table.

    For each pattern, test whether the best directional win rate is
    statistically distinguishable from 50% using the normal approximation:
        z = (p̂ - 0.5) / sqrt(0.25 / n)
    One-tailed p-value via erfc. Patterns below the significance level
    have a demonstrated edge; others are noise at current sample sizes.

    Returns: {validated: [...], weak: [...], n_validated, n_weak}
    """
    import math as _math

    table = load_pattern_table(symbol)
    if table is None:
        return {
            "symbol": symbol,
            "error":  "No pattern table — run build_ngram_from_alpaca first",
            "validated": [], "weak": [], "n_validated": 0, "n_weak": 0,
        }

    validated: list[dict] = []
    weak:      list[dict] = []

    for pattern, counts in table.items():
        total = sum(counts.values())
        if total < 30:
            weak.append({"pattern": pattern, "n": total, "reason": "too_few_samples"})
            continue

        p_up   = counts.get("U", 0) / total
        p_down = counts.get("D", 0) / total
        best_dir = "U" if p_up >= p_down else "D"
        best_p   = max(p_up, p_down)

        z     = (best_p - 0.5) / _math.sqrt(0.25 / total)
        p_val = 0.5 * _math.erfc(z / _math.sqrt(2))

        entry = {
            "pattern":   pattern,
            "direction": best_dir,
            "win_rate":  round(best_p, 3),
            "n":         total,
            "z_score":   round(z, 2),
            "p_value":   round(p_val, 4),
        }
        if p_val < significance:
            validated.append(entry)
        else:
            weak.append({**entry, "reason": "not_significant"})

    return {
        "symbol":         symbol,
        "total_patterns": len(table),
        "significance":   significance,
        "n_validated":    len(validated),
        "n_weak":         len(weak),
        "validated":      sorted(validated, key=lambda x: x["win_rate"], reverse=True),
        "weak":           weak,
    }


def ngram_to_composite_score(signal: dict) -> float:
    """Convert ngram signal to -100…+100 scale for ensemble blending."""
    s = signal.get("signal", "NONE")
    c = signal.get("confidence", 0)
    if s == "UP":
        return c
    elif s == "DOWN":
        return -c
    return 0.0


# ── Table builder (weekly maintenance) ───────────────────────────────────────

def build_ngram_from_alpaca(symbol: str, months: int = 6) -> bool:
    """
    Fetch historical 5-min bars from Alpaca and build the pattern table.

    Fetches up to 10000 bars (~6 months of 5-min data for a full trading day).
    Saves to DB via save_pattern_table. Returns True on success.

    Call this weekly per symbol (scripts/weekly_build.py handles the schedule).
    Building takes ~2 s per symbol due to API fetch.
    """
    from fetchers.alpaca import get_bars

    # ~21 trading days/month × 78 bars/day = 1638 bars/month
    bars_needed = min(months * 21 * 78, 10_000)
    bars = get_bars(symbol, timeframe="5Min", limit=bars_needed)

    if not bars or len(bars) < 1000:
        return False

    closes = [b["c"] for b in bars]
    table  = build_pattern_table(closes, PATTERN_LENGTH)
    save_pattern_table(symbol, table, len(bars))
    return True
