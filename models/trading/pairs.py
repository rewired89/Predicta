"""
Pairs trading: cointegration detection and spread signal generation.

Based on Gatev et al. (2006) — the most-cited statistical arbitrage paper.
Core idea: find two stocks whose price ratio is stationary (cointegrated),
then trade the spread when it deviates beyond ±2σ from its mean.

Market-neutral: profits from relative mispricing, not directional market moves.

Two-level API:
  Low-level  (API endpoints / custom lists): find_cointegrated_pairs, pairs_signal, compute_pairs_levels
  High-level (weekly scan):                  find_all_pairs, generate_pair_signal, log_pair_signal
"""
from __future__ import annotations
import math
from typing import Optional

# ── Known candidate pairs (industry intuition, Kimi Round 7) ─────────────────
# Each tuple is (sym1, sym2) — ordered by industry sector.
# These are strong priors; validate cointegration with data before trading.
CANDIDATE_PAIRS = [
    ("XOM",  "CVX"),   # Oil majors — very tight cointegration historically
    ("PEP",  "KO"),    # Beverages — same consumer demand cycle
    ("JPM",  "BAC"),   # Money-center banks — same rate/credit exposure
    ("AAPL", "MSFT"),  # Big tech — weaker, test before trading
    ("WMT",  "TGT"),   # Discount retail — same consumer price sensitivity
    ("DAL",  "UAL"),   # Airlines — same fuel/demand cycle (high vol)
    ("INTC", "AMD"),   # Semiconductors — divergence play (may not cointegrate)
    ("JNJ",  "PFE"),   # Pharma — same pipeline + regulatory risk
]

# Signal thresholds
_ENTRY_Z = 2.0   # Open when |spread z-score| > 2σ
_EXIT_Z  = 0.5   # Close when |z| < 0.5σ (near mean)
_STOP_Z  = 3.5   # Stop at 3.5σ — spread breaking down, not mean-reverting


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _ols_beta(x: list[float], y: list[float]) -> float:
    """OLS slope: β = Σ(xi - x̄)(yi - ȳ) / Σ(xi - x̄)²"""
    n = len(x)
    x_mean = sum(x) / n
    y_mean = sum(y) / n
    num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
    den = sum((x[i] - x_mean) ** 2 for i in range(n))
    return num / den if den else 1.0


def _half_life(spread: list[float]) -> Optional[int]:
    """
    Ornstein-Uhlenbeck half-life: how many days until the spread mean-reverts
    halfway back. Lower = faster reversion = better for trading.
    Estimated via OLS regression of Δspread on lagged spread.
    Returns None if no mean reversion (β ≥ 0) or half-life > 365 days.
    """
    if len(spread) < 5:
        return None
    delta = [spread[i] - spread[i - 1] for i in range(1, len(spread))]
    lagged = spread[:-1]
    beta = _ols_beta(lagged, delta)
    if beta >= 0:
        return None
    hl = -math.log(2) / beta
    return int(round(hl)) if 0 < hl < 365 else None


def _spread_stats(s1: list[float], s2: list[float], beta: float) -> dict:
    """Compute spread mean, std, current value, and z-score."""
    spread = [s1[k] - beta * s2[k] for k in range(len(s1))]
    n = len(spread)
    mean = sum(spread) / n
    variance = sum((v - mean) ** 2 for v in spread) / n
    std = math.sqrt(variance) if variance > 0 else 1.0
    zscore = (spread[-1] - mean) / std
    return {
        "spread":         spread,
        "spread_mean":    mean,
        "spread_std":     std,
        "current_spread": spread[-1],
        "zscore":         zscore,
    }


# ── Cointegration test (Engle-Granger via statsmodels) ───────────────────────

def test_cointegration(sym1: str, sym2: str, series: dict[str, list[float]]) -> dict:
    """
    Full cointegration analysis for a pair using the Engle-Granger test.

    Uses statsmodels.tsa.stattools.coint for the p-value (more accurate than
    ADF alone because it runs the full two-step EG procedure). Falls back to
    ADF on the OLS residuals if statsmodels is unavailable.

    Returns a rich analytics dict suitable for find_all_pairs filtering
    and generate_pair_signal input.
    """
    s1 = series.get(sym1, [])
    s2 = series.get(sym2, [])
    if len(s1) < 20 or len(s2) < 20:
        return {"error": f"Insufficient data for {sym1}/{sym2}"}

    beta = _ols_beta(s2, s1)
    ss = _spread_stats(s1, s2, beta)
    hl = _half_life(ss["spread"])

    # Engle-Granger cointegration p-value
    try:
        from statsmodels.tsa.stattools import coint as _coint
        import numpy as np
        coint_score, pvalue, _ = _coint(np.array(s1), np.array(s2))
        coint_score = float(coint_score)
        pvalue = float(pvalue)
    except Exception:
        # Fallback: ADF on the spread
        try:
            from statsmodels.tsa.stattools import adfuller
            res = adfuller(ss["spread"], maxlag=1, regression="c", autolag=None)
            coint_score = float(res[0])
            pvalue = float(res[1])
        except Exception:
            coint_score = 0.0
            pvalue = 1.0

    return {
        "sym1":            sym1,
        "sym2":            sym2,
        "cointegrated":    pvalue < 0.05,
        "pvalue":          round(pvalue, 4),
        "coint_score":     round(coint_score, 4),
        "beta":            round(beta, 6),
        "spread_mean":     round(ss["spread_mean"], 4),
        "spread_std":      round(ss["spread_std"], 4),
        "half_life_days":  hl,
        "current_zscore":  round(ss["zscore"], 3),
        "n_obs":           len(s1),
    }


def get_suspended_pairs() -> list[dict]:
    """All currently suspended pairs (reinstate_after in future or NULL)."""
    from datetime import datetime as _dt
    try:
        from db.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT sym1, sym2, reason, suspended_at, reinstate_after FROM suspended_pairs"
            ).fetchall()
        now = _dt.utcnow().isoformat()
        return [
            dict(r) for r in rows
            if r["reinstate_after"] is None or r["reinstate_after"] > now
        ]
    except Exception:
        return []


def suspend_pair(
    sym1: str,
    sym2: str,
    reason: str = "",
    reinstate_after: Optional[str] = None,
) -> bool:
    """
    Suspend a pair from find_all_pairs scans.
    reinstate_after: ISO datetime string (UTC); None = indefinite.
    Returns True on success.
    """
    try:
        from db.database import get_db
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO suspended_pairs (sym1, sym2, reason, reinstate_after)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sym1, sym2) DO UPDATE
                    SET reason = excluded.reason,
                        suspended_at = datetime('now'),
                        reinstate_after = excluded.reinstate_after
                """,
                (sym1, sym2, reason, reinstate_after),
            )
        return True
    except Exception:
        return False


def reinstate_pair(sym1: str, sym2: str) -> bool:
    """Remove a pair from the suspended list. Returns True if a row was deleted."""
    try:
        from db.database import get_db
        with get_db() as conn:
            rows_deleted = conn.execute(
                "DELETE FROM suspended_pairs WHERE sym1 = ? AND sym2 = ?",
                (sym1, sym2),
            ).rowcount
        return rows_deleted > 0
    except Exception:
        return False


def auto_cull_pairs(min_trades: int = 10, win_rate_floor: float = 0.40) -> list[dict]:
    """
    Suspend pairs that show consistent underperformance with sufficient data.

    Criteria (both must be true):
      - n_closed >= min_trades
      - win_rate < win_rate_floor
    OR:
      - avg_pnl_pct < 0 with n_closed >= min_trades

    Pairs are suspended for 90 days (reinstate_after = now + 90d).
    Returns list of dicts describing each action taken.
    """
    from datetime import datetime as _dt, timedelta
    from models.trading.shared.signal_calibration import pairs_calibration_summary

    report  = pairs_calibration_summary(min_trades=min_trades)
    culled  = []

    for pair in report.get("pairs", []):
        n        = pair.get("n", 0)
        wr       = pair.get("win_rate")
        avg_pnl  = pair.get("avg_pnl_pct")
        pair_key = pair["pair"]  # "SYM1/SYM2"

        if n < min_trades:
            continue
        if (wr is not None and wr < win_rate_floor) or (avg_pnl is not None and avg_pnl < 0):
            sym1, sym2 = pair_key.split("/")
            reinstate = (_dt.utcnow() + timedelta(days=90)).isoformat()
            ok = suspend_pair(
                sym1, sym2,
                reason=f"auto_cull: wr={wr}, avg_pnl={avg_pnl} over {n} trades",
                reinstate_after=reinstate,
            )
            if ok:
                culled.append({
                    "pair":            pair_key,
                    "reason":          "underperforming",
                    "win_rate":        wr,
                    "avg_pnl_pct":     avg_pnl,
                    "n_trades":        n,
                    "reinstate_after": reinstate,
                })

    return culled


def find_all_pairs(
    days: int = 90,
    min_half_life: float = 1.0,
    max_half_life: float = 30.0,
) -> list[dict]:
    """
    Scan all CANDIDATE_PAIRS for cointegration, filtered by half-life.

    Skips pairs that are in the suspended_pairs table. Suspension is
    time-limited (reinstate_after) or indefinite.

    min_half_life / max_half_life: discard pairs that mean-revert too fast
    (noise) or too slowly (capital tied up for months).

    Recommended cadence: run weekly (Sunday before market open) and cache
    results. Pairs status changes slowly — daily re-scanning is wasteful.

    Returns: list of test_cointegration dicts sorted by p-value ascending.
    """
    from fetchers.pairs_data import fetch_pair_history, prices_to_series

    suspended = {(s["sym1"], s["sym2"]) for s in get_suspended_pairs()}

    results = []
    for sym1, sym2 in CANDIDATE_PAIRS:
        if (sym1, sym2) in suspended or (sym2, sym1) in suspended:
            continue

        history = fetch_pair_history([sym1, sym2], days=days)
        if not history or len(history) < int(days * 0.7):
            continue
        series = prices_to_series(history)
        if sym1 not in series or sym2 not in series:
            continue

        result = test_cointegration(sym1, sym2, series)
        if "error" in result:
            continue

        if (result["cointegrated"]
                and result["half_life_days"] is not None
                and min_half_life <= result["half_life_days"] <= max_half_life):
            results.append(result)

    return sorted(results, key=lambda x: x["pvalue"])


# ── Signal generation (high-level) ───────────────────────────────────────────

def generate_pair_signal(pair_result: dict) -> dict:
    """
    Generate a trade signal from a test_cointegration result dict.

    Uses fixed z-score thresholds:
      Entry:  |z| > 2.0σ
      Target: |z| < 0.5σ (near mean)
      Stop:   |z| > 3.5σ (spread widening further — not mean-reverting)

    Confidence scales with z: conf = min(|z| / stop_z, 1.0).
    expected_hold_days comes from the OU half-life — useful for sizing
    position duration and deciding whether hold_bars needs to be extended.
    """
    z    = pair_result.get("current_zscore", 0)
    sym1 = pair_result["sym1"]
    sym2 = pair_result["sym2"]
    beta = pair_result["beta"]
    hl   = pair_result.get("half_life_days")

    if z < -_ENTRY_Z:
        return {
            "action":              "LONG_SPREAD",
            "zscore":              z,
            "long":                sym1,
            "short":               sym2,
            "beta":                beta,
            "shares_ratio":        beta,
            "entry_z":             z,
            "target_z":            _EXIT_Z,
            "stop_z":              -_STOP_Z,
            "confidence":          round(min(abs(z) / _STOP_Z, 1.0), 3),
            "expected_hold_days":  hl,
            "note":                f"Spread {z:.2f}σ below mean — buy {sym1}, short {sym2}",
        }
    elif z > _ENTRY_Z:
        return {
            "action":              "SHORT_SPREAD",
            "zscore":              z,
            "short":               sym1,
            "long":                sym2,
            "beta":                beta,
            "shares_ratio":        beta,
            "entry_z":             z,
            "target_z":            -_EXIT_Z,
            "stop_z":              _STOP_Z,
            "confidence":          round(min(abs(z) / _STOP_Z, 1.0), 3),
            "expected_hold_days":  hl,
            "note":                f"Spread {z:.2f}σ above mean — short {sym1}, buy {sym2}",
        }
    else:
        return {
            "action": "NONE",
            "zscore": z,
            "note":   f"Within entry band (±{_ENTRY_Z}σ, current {z:.2f}σ)",
        }


def log_pair_exit(
    signal_id: int,
    exit_zscore: float,
    exit_reason: str,
    pnl_pct: Optional[float] = None,
) -> bool:
    """
    Close an open pair_signals row with exit data.
    Mirrors log_trade_exit() for intraday_trades.
    Returns True on success, False if row not found or DB error.
    """
    from datetime import datetime as _dt
    from db.database import get_db

    try:
        with get_db() as conn:
            rows_updated = conn.execute(
                """
                UPDATE pair_signals
                SET exit_z = ?, exit_time = ?, pnl_pct = ?, exit_reason = ?
                WHERE id = ? AND exit_time IS NULL
                """,
                (exit_zscore, _dt.now().isoformat(), pnl_pct, exit_reason, signal_id),
            ).rowcount
        return rows_updated > 0
    except Exception:
        return False


def log_pair_signal(signal: dict, sym1: str, sym2: str) -> int:
    """
    Persist a pair signal to the pair_signals table for tracking.
    sym1/sym2 are the canonical pair symbols (from test_cointegration).
    Returns the row ID.
    """
    from db.database import get_db

    action = signal.get("action", "NONE")
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO pair_signals (
                sym1, sym2, action, zscore, beta,
                entry_z, target_z, stop_z, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sym1, sym2,
                action,
                signal.get("zscore"),
                signal.get("beta"),
                signal.get("entry_z"),
                signal.get("target_z"),
                signal.get("stop_z"),
                signal.get("confidence"),
            ),
        )
        return cur.lastrowid


# ── Low-level API (custom symbol lists, API endpoints) ────────────────────────

def _adf_pvalue(residuals: list[float]) -> float:
    """
    ADF p-value for spread residuals. Uses statsmodels; falls back to an
    autocorrelation proxy if unavailable (for ranking purposes only).
    """
    try:
        from statsmodels.tsa.stattools import adfuller
        result = adfuller(residuals, maxlag=1, regression="c", autolag=None)
        return float(result[1])
    except ImportError:
        pass
    n = len(residuals)
    if n < 4:
        return 1.0
    mean = sum(residuals) / n
    diffs = [r - mean for r in residuals]
    numer = sum(diffs[i] * diffs[i - 1] for i in range(1, n))
    denom = sum(d ** 2 for d in diffs)
    autocorr = numer / denom if denom else 1.0
    return max(0.0, min(1.0, autocorr ** 2))


def find_cointegrated_pairs(
    series: dict[str, list[float]],
    pvalue_threshold: float = 0.05,
) -> list[dict]:
    """
    Test all pairs from a custom series dict for cointegration.
    Used by the /trade/pairs-scan API endpoint for arbitrary watchlists.

    Args:
        series: {symbol: [close_price, ...]} — aligned lists, same length.
        pvalue_threshold: pairs with ADF p-value below this are returned.

    Returns list of dicts sorted by p-value ascending.
    """
    symbols = list(series.keys())
    results = []

    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            s1_name, s2_name = symbols[i], symbols[j]
            s1, s2 = series[s1_name], series[s2_name]
            if len(s1) < 20 or len(s2) < 20:
                continue

            beta = _ols_beta(s2, s1)
            ss   = _spread_stats(s1, s2, beta)
            pvalue = _adf_pvalue(ss["spread"])

            if pvalue <= pvalue_threshold:
                results.append({
                    "sym1":           s1_name,
                    "sym2":           s2_name,
                    "beta":           round(beta, 6),
                    "pvalue":         round(pvalue, 4),
                    "half_life_days": _half_life(ss["spread"]),
                })

    return sorted(results, key=lambda x: x["pvalue"])


def pairs_signal(
    sym1: str,
    sym2: str,
    beta: float,
    series: dict[str, list[float]],
    lookback: int = 60,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> dict:
    """
    Z-score signal for a custom-supplied pair. Used by /trade/pairs-scan
    and /trade/pairs-signal API endpoints.

    Long spread  (zscore < -entry_z): buy sym1, short sym2
    Short spread (zscore >  entry_z): short sym1, buy sym2
    Exit zone    (|zscore| < exit_z): close existing position
    """
    s1 = series.get(sym1, [])
    s2 = series.get(sym2, [])
    if not s1 or not s2:
        return {"action": "ERROR", "error": f"Missing data for {sym1} or {sym2}"}

    window = min(lookback, len(s1), len(s2))
    ss = _spread_stats(s1[-window:], s2[-window:], beta)

    zscore   = ss["zscore"]
    abs_z    = abs(zscore)
    conf     = round(min(abs_z / (entry_z + 1.0), 1.0), 3)

    base = {
        "sym1":           sym1,
        "sym2":           sym2,
        "beta":           round(beta, 6),
        "zscore":         round(zscore, 3),
        "current_spread": round(ss["current_spread"], 4),
        "spread_mean":    round(ss["spread_mean"], 4),
        "spread_std":     round(ss["spread_std"], 4),
    }

    if zscore < -entry_z:
        return {**base, "action": "LONG_SPREAD", "long_sym": sym1, "short_sym": sym2,
                "confidence": conf,
                "note": f"Spread {zscore:.2f}σ below mean — buy {sym1}, short {sym2}"}
    elif zscore > entry_z:
        return {**base, "action": "SHORT_SPREAD", "long_sym": sym2, "short_sym": sym1,
                "confidence": conf,
                "note": f"Spread {zscore:.2f}σ above mean — short {sym1}, buy {sym2}"}
    elif abs_z < exit_z:
        return {**base, "action": "EXIT_ZONE",
                "note": f"Spread {zscore:.2f}σ — within exit zone, close any open position"}
    else:
        return {**base, "action": "NONE",
                "note": f"Spread {zscore:.2f}σ — inside entry threshold, no action"}


def compute_pairs_levels(
    long_sym: str,
    short_sym: str,
    series: dict[str, list[float]],
    beta: float,
    zscore: float,
    spread_std: float,
    account_value: float = 10_000.0,
    risk_pct: float = 0.01,
    exit_z: float = 0.5,
) -> dict:
    """
    Dollar-neutral position sizing for a pairs trade.
    Risk = spread widening 1σ beyond current z (mean-reversion fails).
    """
    long_price  = series[long_sym][-1]
    short_price = series[short_sym][-1]

    if not long_price or not short_price:
        return {"error": "Missing current price for position sizing"}

    risk_dollars = account_value * risk_pct
    stop_z       = abs(zscore) + 1.0
    target_z     = exit_z

    spread_risk_per_unit = spread_std * 1.0
    if spread_risk_per_unit <= 0:
        return {"error": "Spread std is zero, cannot size position"}

    units     = max(1.0, round(risk_dollars / spread_risk_per_unit, 1))
    long_qty  = round(units)
    short_qty = round(units * beta)

    current_abs_z = abs(zscore)
    reward     = max(current_abs_z - target_z, 0)
    risk_z     = max(stop_z - current_abs_z, 0.01)
    expected_r = round(reward / risk_z, 2)

    return {
        "long_sym":     long_sym,
        "short_sym":    short_sym,
        "long_price":   round(long_price, 4),
        "short_price":  round(short_price, 4),
        "long_qty":     long_qty,
        "short_qty":    short_qty,
        "long_value":   round(long_qty * long_price, 2),
        "short_value":  round(short_qty * short_price, 2),
        "beta":         round(beta, 4),
        "stop_z":       round(stop_z, 2),
        "target_z":     round(target_z, 2),
        "risk_dollars": round(risk_dollars, 2),
        "expected_r":   expected_r,
    }
