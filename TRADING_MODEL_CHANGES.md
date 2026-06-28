# Trading Model Updates — Session Summary
Date: 2026-06-28
Commit: adeca17

---

## 1. fetchers/market_data.py — Full Rewrite

**Problem:** Used `yfinance`, which Yahoo Finance now blocks with 403 errors — completely dead.

**Fix:** Replaced entirely with Alpaca Data API v2 (already in the codebase).
Now calls `get_daily_bars()` + `get_snapshot()` instead.

- Deleted `_detect_asset_type()` function (no longer needed)
- `fetch_ticker()` returns the same shape as before — no other files needed changing
- Fundamentals requiring a paid feed (PE ratio, sector, beta) return `None`
- `search_ticker()` now validates via Alpaca snapshot instead of `yf.Search`

---

## 2. models/trading/signals.py — Added MACD

**Problem:** Daily swing signal engine had no MACD — missing the most widely-used
trend-momentum indicator.

### New function: `_macd(closes)`
- MACD(12,26,9) with correct EMA warm-up
- EMA12 is seeded and run through bars 12–25 BEFORE the MACD line starts at bar 26
  (most implementations get this wrong and produce inaccurate early values)

### Updated: `_momentum()`
- Now includes a `macd` dict in its output alongside rsi/roc

### Updated: `_composite_score()` — MACD at 20% weight in all regimes

| Regime          | Old weights                          | New weights (with MACD)                        |
|-----------------|--------------------------------------|------------------------------------------------|
| High-vol >30%   | trend 20%, RSI 35%, ROC 25%, BB 20% | trend 15%, RSI 30%, ROC 20%, BB 15%, MACD 20% |
| Normal          | trend 40%, RSI 30%, ROC 20%, BB 10% | trend 30%, RSI 25%, ROC 15%, BB 10%, MACD 20% |
| Low-vol <15%    | trend 50%, RSI 20%, ROC 20%, BB 10% | trend 40%, RSI 15%, ROC 15%, BB 10%, MACD 20% |

**MACD scoring:**
- Bullish crossover  → +100 raw
- Bearish crossover  → -100 raw
- Sustained bullish  → +50 raw
- Sustained bearish  → -50 raw

---

## 3. models/trading/intraday.py — Three Improvements

### Change 1: New `_sig_macd(daily_bars)`
- Daily MACD computed to give the intraday engine a trend-momentum anchor
- Crossover scores ±20, sustained direction scores ±10
- Requires ≥35 daily bars

### Change 2: Regime-conditioned `_sig_vwap()`
Added `trend_label` parameter to flip the overextension logic by market regime.

| Condition                        | Before | After  |
|----------------------------------|--------|--------|
| Price >1.5% above VWAP, uptrend  | -20    | +15    |
| Price >1.5% above VWAP, neutral  | -20    | -20    |
| Price >1.5% below VWAP, downtrend| +20    | -15    |
| Price >1.5% below VWAP, neutral  | +20    | +20    |

Rationale: in a strong uptrend, being extended above VWAP is momentum
confirmation — not a mean-reversion risk.

### Change 3: Updated signal weights — 9 signals, sum = 1.00

| Signal    | Old weight | New weight |
|-----------|------------|------------|
| vwap      | 0.20       | 0.20       |
| or        | 0.15       | 0.15       |
| rsi       | 0.15       | 0.15       |
| relvol    | 0.10       | 0.10       |
| gap       | 0.10       | 0.08       |
| trend     | 0.15       | 0.10       |
| bollinger | 0.10       | 0.10       |
| volsurge  | 0.05       | 0.05       |
| macd      | —          | 0.07       |

---

## 4. models/trading/ngram.py — Lowered Thresholds

**Problem:** N-gram overlay almost never fired — thresholds too conservative
for a pattern space of only 27 patterns (3-bar, U/D/E).

| Setting          | Old  | New  |
|------------------|------|------|
| `_THRESHOLD_PCT` | 0.55 | 0.52 |
| `min_samples`    | 50   | 30   |

The overlay now activates on more patterns while still requiring a
statistically meaningful edge.

---

## 5. CODEMAP.md — Updated in Same Commit

All affected entries updated per the CodeMap Protocol:
- Removed: `_detect_asset_type`
- Updated: `fetch_ticker`, `search_ticker`, `_momentum`, `_composite_score`,
  `WEIGHTS`, `_sig_vwap`, `_composite`, `compute_intraday_signals`, `ngram_signal`
- Added: `_macd` (signals.py), `_sig_macd` (intraday.py)
