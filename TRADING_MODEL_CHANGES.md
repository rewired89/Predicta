# Trading Model Updates — Session Summary
Date: 2026-06-28
Commits: adeca17, b003073, fbc4ca5

---

## 1. fetchers/market_data.py — Full Rewrite (P0 Fix)

**Problem:** Used `yfinance`, which Yahoo Finance now blocks with 403 errors — completely dead.

**Fix:** Replaced entirely with Alpaca Data API v2 (already in the codebase).
Now calls `get_daily_bars()` + `get_snapshot()` instead.

- Deleted `_detect_asset_type()` function (no longer needed)
- Added `_PERIOD_TO_DAYS` dict to map period strings to calendar days
- `fetch_ticker()` returns the same shape as before — no other files needed changing
- Fundamentals requiring a paid feed (PE ratio, sector, beta) return `None`
- `search_ticker()` now validates via Alpaca snapshot instead of `yf.Search`

---

## 2. models/trading/signals.py — Added MACD (Daily Swing Engine)

**Problem:** Daily swing signal engine had no MACD — missing the most widely-used
trend-momentum indicator.

### New function: `_macd(closes)`
- MACD(12,26,9) with correct EMA warm-up
- EMA12 is seeded and run through bars 12–25 BEFORE the MACD line starts at bar 26
  (most implementations skip this warm-up and produce inaccurate early values)

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

## 3. models/trading/intraday.py — Final State (4 Changes)

### Change 1: Regime-conditioned `_sig_vwap()`
Added `trend_label` parameter to flip the overextension logic by market regime.

| Condition                         | Before | After |
|-----------------------------------|--------|-------|
| Price >1.5% above VWAP, uptrend   | -20    | +15   |
| Price >1.5% above VWAP, neutral   | -20    | -20   |
| Price >1.5% below VWAP, downtrend | +20    | -15   |
| Price >1.5% below VWAP, neutral   | +20    | +20   |

Rationale: in a strong uptrend, being extended above VWAP is momentum
confirmation — not a mean-reversion risk.

### Change 2: Regime-conditioned `_sig_gap()` (Bug Fix)

**Problem:** Small gap-down (0.5–2%) in an uptrend scored -10 — penalizing dip-buying.
Small gap-up (0.5–2%) in a downtrend scored +10 — rewarding dead-cat bounces.

**Fix:** Added `trend_label` parameter:

| Gap condition                     | Before | After |
|-----------------------------------|--------|-------|
| Gap-down small, uptrend           | -10    | +10   |
| Gap-down small, neutral/downtrend | -10    | -10   |
| Gap-up small, downtrend           | +10    | -10   |
| Gap-up small, neutral/uptrend     | +10    | +10   |
| Gap-down large (>2%)              | +10    | +10   |
| Gap-up large (>2%)                | -10    | -10   |

Large gaps (>2%) are always faded regardless of regime.

### Change 3: LUNCH_CHOP threshold lowered 60 → 40

Old threshold of 60 suppressed anything below Strong Buy/Sell.
Regular Buy/Sell signals (score 40–60) were being silently killed.
Lowered to 40 to preserve those signals while still blocking truly choppy readings (<40).

### Change 4: Signal weights — 8 signals, sum = 1.00 (MACD NOT added to intraday)

| Signal    | Weight |
|-----------|--------|
| vwap      | 0.20   |
| or        | 0.15   |
| rsi       | 0.15   |
| relvol    | 0.10   |
| gap       | 0.10   |
| trend     | 0.15   |
| bollinger | 0.10   |
| volsurge  | 0.05   |

**Why no MACD in intraday:** Daily MACD uses a 26-day EMA as its slow line — a 4-6 week
lag. Intraday holds are 30–90 minutes. Daily MACD is the wrong time-scale for intraday
signal generation and would add noise, not signal.

### Caller update: `compute_intraday_signals()`
Pre-computes `trend_sig` once and passes `trend_label` to both `_sig_vwap()` and
`_sig_gap()` so they share the same regime context:

```python
trend_sig   = _sig_trend_bias(daily_bars)
trend_label = trend_sig.get("label", "neutral")
sigs = {
    "vwap": _sig_vwap(intraday_bars, snapshot, trend_label),
    "gap":  _sig_gap(snapshot, trend_label),
    "trend": trend_sig,
    ...
}
```

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

## 5. app.py — LUNCH_CHOP Threshold

Updated in both `/trade/smart-order` and `/trade/signal-only` endpoints:
- `abs(score_val) < 60` → `abs(score_val) < 40`
- Rejection message updated to reference the new threshold

---

## 6. v4 Logging Schema — Verified (No Changes Needed)

`fetchers/trading_logger.py` was audited and confirmed correct:
- `_extract_signal_scores()` captures all 8 per-signal scores + ngram signal/confidence
- `log_trade_entry()` stores the full schema including composite_raw, vwap_score, or_score,
  rsi_score, relvol_score, gap_score, trend_score, bollinger_score, volsurge_score,
  ngram_signal, ngram_confidence
- Both `/trade/smart-order` and `/trade/signal-only` pass `signal_scores=signals` correctly
- Calibration loop in `signal_calibration.py` activates at 30 closed trades (weights),
  50 closed trades (Kelly sizing)

---

## 7. CODEMAP.md — Updated in Same Commits

All affected entries updated per the CodeMap Protocol:
- Removed: `_detect_asset_type`, `_sig_macd` (intraday — never shipped)
- Updated: `fetch_ticker`, `search_ticker`, `_momentum`, `_composite_score`,
  `WEIGHTS`, `_sig_vwap`, `_sig_gap`, `_composite`, `compute_intraday_signals`, `ngram_signal`
- Added: `_macd` (signals.py), `_PERIOD_TO_DAYS` (market_data.py)

---

## Roadmap — What Comes Next (Per Kimi AI Feedback)

**Phase 1 (Weeks 1-2): Collect Paper Trading Data**
- Use `/trade/signal-only` for hypothetical trades (no Alpaca order placed)
- Target: 2-3 signals/day × 20 trading days = 40-60 closed trades
- Log exits via `log_trade_exit()` when stop/target/time would have been hit

**Phase 2 (Week 3): First Calibration**
- At 30 closed trades: `GET /trade/calibration` — `compute_dynamic_weights()` activates
- At 50 closed trades: empirical Kelly sizing activates via `calibrated_win_rate()`

**Phase 3 (Week 4): Sized Paper Trading**
- Use `/trade/smart-order` with quarter-Kelly sizing on high-confidence signals
- Watch adjusted_pnl (spread-corrected) — that is the real performance measure

**Phase 4: Live Graduation**
- After 4-6 weeks showing positive adjusted_pnl with Sharpe > 1.0
- Flip Alpaca account from paper to live

**Watchlist Note (Kimi concern — not yet addressed):**
`DEFAULT_WATCHLIST` in `screener.py` mixes large-caps (AAPL/MSFT), index ETFs (SPY/QQQ),
and high-beta retail (COIN/MSTR/HOOD). These have incompatible volatility profiles.
Calibration weights trained on mixed tickers will be garbage. Consider splitting into
tiers and running calibration per-tier when enough data exists.
