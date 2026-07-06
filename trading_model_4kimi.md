# Predicta Trading Model — Technical Overview for Kimi

Date: 2026-07-03
Purpose: Full technical dump of the current trading system for feedback review.

---

## 1. What This System Is

Predicta runs **two separate, independent signal engines** for two different trading
styles. They share no code and are never mixed:

| Engine | File | Style | Hold time |
|--------|------|-------|-----------|
| Daily swing engine | `models/trading/signals.py` | Multi-day swing trades | Days to weeks |
| Intraday engine | `models/trading/intraday.py` | Day trading | 30–60 minutes |

Everything below the "Current Status" section is broken into these two engines, plus
the shared infrastructure (position sizing, calibration feedback, automated data
collection) that both use.

---

## 2. Current Status — What's Live vs What's Measured

**Live and running:**
- Both signal engines are fully coded and deployed to Railway (production).
- An automated paper-trading runner (`fetchers/paper_runner.py`) scans 8 large-cap
  tickers every weekday morning, logs signals as hypothetical trades (no real
  orders), checks positions every 30 minutes, and force-closes at end of day.
- A plain-English dashboard (`/trade/dashboard`) translates the raw stats into
  human-readable verdicts.

**Not yet measured — this is the important part:**
- **Zero historical accuracy data exists yet.** The paper runner only started
  collecting data this week. We have not validated win rate, R-multiple, or any
  performance metric with real market data.
- All position sizing, calibration, and "empirical win rate" logic described below
  is *built and wired in*, but returns `None`/placeholder values until enough
  closed trades accumulate (30 for weight calibration, 50 for Kelly sizing,
  100 for the disconnected ML layer).
- **This document describes the mechanism, not a track record.** We are asking for
  feedback on the architecture and logic — not on results, because there are no
  results yet.

---

## 3. Daily Swing Engine (`models/trading/signals.py`)

Multi-day timescale. Computes 4 core technical signals + MACD, blended into a
single **regime-adaptive composite score** from -100 to +100.

### Signals computed

| Signal | Method |
|--------|--------|
| Trend | Price vs moving averages |
| RSI | Standard 14-period RSI |
| ROC | Rate of change (momentum) |
| Bollinger %B | Position within bands |
| MACD | MACD(12,26,9) — added this cycle, correct EMA warm-up (EMA12 runs bars 12–25 before the MACD line starts at bar 26, which many implementations get wrong) |

### Regime-adaptive weighting

Volatility regime (20-day historical vol, "HV") changes which signals get more
weight — the logic being that trend-following works in low-vol grinding markets
and mean-reversion (RSI/Bollinger) works better in high-vol choppy markets:

| Regime | Trend | RSI | ROC | Bollinger | MACD |
|--------|-------|-----|-----|-----------|------|
| High-vol (HV > 30%) | 15% | 30% | 20% | 15% | 20% |
| Normal | 30% | 25% | 15% | 10% | 20% |
| Low-vol (HV < 15%) | 40% | 15% | 15% | 10% | 20% |

MACD scoring: bullish crossover = +100 raw, bearish crossover = -100 raw,
sustained trend (no fresh crossover) = ±50 raw. Everything is weighted and
summed into the final -100..+100 composite.

### Expected move

±1σ price range for the next N sessions, computed from 20-day historical
volatility — used for setting realistic swing targets.

---

## 4. Intraday Engine (`models/trading/intraday.py`)

30–90 minute holds on 5-minute bars. This is the more complex of the two engines —
**8 independent signals**, each scored on its own scale, combined into one composite,
then adjusted by two post-processing layers (liquidity filter, time-of-day modifier)
and optionally blended with a 9th component (n-gram pattern matching).

### The 8 signals and their weights

| Signal | Weight | What it measures |
|--------|--------|-------------------|
| VWAP deviation | 20% | Price vs cumulative session VWAP — **regime-conditioned** (see below) |
| Opening range | 15% | Breakout above/below the first 15 minutes' high/low |
| RSI-9 | 15% | Short-period (9-bar) momentum oscillator |
| Relative volume | 10% | Volume vs historical average, adjusted for intraday time-of-day seasonality (volume is naturally U-shaped: heavy at open/close, thin at lunch) |
| Gap | 10% | Pre-market gap vs previous close — **regime-conditioned** (see below) |
| Trend bias | 15% | Daily MA20/MA50 context — is the stock in an uptrend or downtrend on the daily chart? |
| Bollinger %B | 10% | Position within 20-period bands |
| Volume surge | 5% | Last 3 bars vs session average — detects abnormal spikes |

Weights sum to 1.00. Each signal outputs a raw score (roughly ±10 to ±25 depending
on the signal), which gets weighted and summed, then normalized to a final
composite of -100 to +100.

### Regime-conditioning (the key design decision in this engine)

Two of the eight signals — VWAP and Gap — don't use fixed logic. They flip their
interpretation based on the daily trend bias signal, computed once per scan and
passed into both:

**VWAP deviation:**
- Price >1.5% above VWAP in a **strong uptrend** → +15 (momentum confirmation, not
  overextension)
- Price >1.5% above VWAP in **neutral/downtrend** → -20 (mean-reversion fade)
- Symmetric logic for below-VWAP in downtrends

**Gap:**
- Large gaps (>2%) are always faded regardless of regime (gaps statistically fill
  ~65% of the time)
- Small gaps (0.5–2%) follow the trend: a small gap-down in an uptrend scores +10
  (buy the dip), a small gap-up in a downtrend scores -10 (fade the dead-cat bounce)

Rationale: without regime-conditioning, the model was fighting strong trends —
treating "extended above VWAP" as a sell signal even during a genuine breakout,
and fading dip-buying opportunities in healthy uptrends.

### Post-composite adjustment layer 1: Liquidity filter

Computed from live bid/ask spread:

| Spread % | Effect |
|----------|--------|
| >0.5% | Hard reject — score forced to 0, labeled UNTRADEABLE |
| 0.3–0.5% | Hard reject — WIDE_SPREAD |
| 0.1–0.3% | Score cut 50% — ELEVATED_SPREAD |
| <0.1% | No penalty — LIQUID |

Rationale: intraday edge is typically 10–30 basis points; a 30bp spread consumes
the entire theoretical profit margin before the trade even starts.

### Post-composite adjustment layer 2: Time-of-day modifier

| Window (ET) | Modifier | Label |
|---|---|---|
| Before 9:30 / after 16:00 | 0.0× (no signal) | MARKET_CLOSED |
| 9:30–10:00 | 0.7× | OPEN_NOISE |
| 10:00–11:30 | 1.0× | MORNING_TREND |
| 11:30–14:00 | 0.4×, hard zero if \|score\|<40 | LUNCH_CHOP |
| 14:00–15:30 | 1.0× | AFTERNOON_TREND |
| 15:30–16:00 | 0.6× | CLOSE_REVERSAL |

The lunch threshold (40) was recently lowered from 60 — the old value was
suppressing every signal except the very strongest ones during midday, on the
reasoning that low volume means low signal reliability. 40 lets moderate-conviction
signals through while still killing weak noise.

### Optional 9th layer: N-gram pattern matching (`models/trading/ngram.py`)

Treats sequences of 5-min bar directions (Up/Down/Equal) like a language model
treats words — a 3-bar pattern (e.g. "UUD") has a historical frequency of what
comes next, built from 6+ months of bar history. 27 possible 3-bar patterns
(3^3, since each bar is U/D/E).

- Only fires when historical frequency exceeds 52% in one direction with at least
  30 historical occurrences (recently lowered from 55%/50 samples — the stricter
  thresholds meant it almost never fired)
- If n-gram agrees with the composite score's direction: score boosted up to +20%
  (scaled by n-gram confidence)
- If it disagrees: composite score reduced 30%
- Never blocks a trade on its own — only modulates confidence in the other 8 signals

### Final output per symbol

A `-100..+100` composite score, a human label (Strong Buy / Buy / Neutral / Sell /
Strong Sell / UNTRADEABLE / LUNCH_SUPPRESSED / etc.), entry/stop/target1/target2
price levels, and position size in shares/dollars.

### Trade levels and position sizing

Stop distance is set from the **intraday expected move** — 1-sigma volatility
calculated specifically for the intended hold period (e.g. 6 bars = 30 min), not
from daily ATR scaled down. The stated reasoning: daily historical volatility
divided by √(bars) systematically overestimates intraday moves because it
includes overnight gap risk that doesn't apply within a session. Falls back to
1.5× ATR(14) if insufficient bars for the expected-move calculation.

Position sizing: risk exactly 1% of account value per trade, capped at 25% of
account in any single position, with a further 10% size reduction when spread is
in the "elevated" tier.

Target multiples scale with trend strength: 2.0×/3.5× (target1/target2) in a
strong trend regime vs 1.5×/2.5× in a neutral regime.

### Active position management (`compute_exit_action`)

Runs on every position check (every 30 min in the automated runner). Three exit
triggers, checked in this order:
1. **TIME_STOP** — after 10 bars (50 min) with less than 0.3R of progress, exit
2. **TRAIL_1.5R** — once 2R profit is reached, trail the stop 1.5R behind current
   price (locks in a minimum 0.5R)
3. **BREAKEVEN_LOCK** — once >1R profit is reached, move stop to breakeven+1 tick
4. **SIGNAL_REVERSAL** — if the composite score flips sign vs entry direction and
   exceeds ±40 magnitude, exit regardless of P&L

---

## 5. Shared Infrastructure

### Position sizing (`models/trading/kelly.py`)

Two sizing methods, both always in "paper mode":

- `atr_position_size()` — pure volatility targeting: risk 1% of capital, sized by
  1.5×ATR stop distance
- `kelly_from_signals()` — the entry point actually used by the pipelines. Always
  ATR-based for now (does not scale by win rate); reports an empirical win rate
  when available for transparency, and **vetoes the trade entirely if the
  empirical win rate for that score bucket drops below 45%.** Classical
  quarter-Kelly sizing (`trading_kelly()`) exists as a separate function but is
  not yet wired into the live pipeline — it's waiting on `calibrated_win_rate()`
  to have enough data to be trustworthy.

### Calibration feedback loop (`models/trading/signal_calibration.py`)

Queries the closed-trades table and computes, per composite-score bucket
(Strong Sell / Sell / Neutral / Buy / Strong Buy):
- Win rate
- Average P&L in R-multiples

Thresholds for trusting this data:
- 0 trades → everything returns None/empty, explicitly not faked
- 5–20 trades per bucket → "preliminary" flag attached
- 30+ trades → dynamic weight recalibration activates
- 50+ trades → empirical Kelly sizing becomes usable
- 100+ trades → the currently-disconnected `models/ml_layer.py` activates
  (separate ML model, feature schemas ready per sport/asset, `predict()` never
  called yet)

Design principle stated in the code comments: **never fall back to a synthetic
heuristic** like `(score+100)/200` as a fake win rate — if there isn't enough
real data for a bucket, the function returns `None` and the caller must decide
what to do (currently: block Kelly sizing, fall back to fixed ATR sizing).

### Trade logging (`fetchers/trading_logger.py`)

Every signal — whether traded live or purely hypothetical — is logged with the
full breakdown of all individual signal scores (v4 schema: composite_raw,
vwap_score, or_score, rsi_score, relvol_score, gap_score, trend_score,
bollinger_score, volsurge_score, ngram_signal, ngram_confidence), not just the
final composite. This is specifically so that once enough trades close, we can
determine which of the 8 signals actually carries predictive weight and which
are dead weight — rather than only being able to evaluate the ensemble as a
black box.

`adjusted_pnl` subtracts an estimated half-spread cost on both entry and exit
legs, since Alpaca paper fills are optimistic by roughly spread/2 per leg
compared to what a real market order would experience.

### Automated data collection (`fetchers/paper_runner.py`)

Runs as a background thread inside the deployed app (Railway), started
automatically on server boot — no manual triggering required.

- **Universe:** 8 large-cap tickers only — AAPL, MSFT, NVDA, AMD, AMZN, META,
  GOOGL, TSLA. Deliberately excludes small-cap/high-beta names and index ETFs to
  avoid mixing incompatible volatility regimes into one calibration set.
- **9:35 AM ET weekdays:** scans all 8 symbols, logs every signal with
  \|score\| ≥ 20 as a hypothetical trade (no real order placed)
- **Every 30 min:** checks all open hypothetical positions against actual 5-min
  bar highs/lows (not just the current price) so a stop or target touched
  between checks isn't missed
- **3:50 PM ET:** force-closes any remaining open positions at market price
- Known gap: does not yet account for US market holidays (e.g. would still
  attempt a scan on July 4th observance) — harmless since Alpaca simply returns
  no fresh data, but not yet clean

---

## 6. Data Sources

| Source | Used for | Notes |
|--------|----------|-------|
| Alpaca Data API v2 | All price bars, snapshots, spreads | Free tier, IEX feed. Replaced yfinance entirely after Yahoo Finance began returning 403 on all requests |
| Alpaca Paper Trading API | Order execution | Paper only — hard-coded assertion that the base URL contains "paper," code will not run against live trading |

---

## 7. What We're Asking Kimi to Review

1. **Regime-conditioning logic for VWAP and gap** — is flipping interpretation
   based on daily trend label a sound approach, or does it risk overfitting to
   recent regime and lagging on regime changes?
2. **Weight allocation across the 8 intraday signals** — any of the 8 signals
   look structurally redundant (i.e., measuring the same underlying thing twice)?
3. **The 45% win-rate veto threshold in `kelly_from_signals`** — reasonable
   cutoff, or too permissive/conservative given it will trigger on small sample
   sizes early on?
4. **N-gram overlay** — is a 3-bar pattern with 52%/30-sample threshold likely to
   find real edge, or is 3 bars too short a context window for 5-min data?
5. **Anything structurally missing** from the ensemble that would be considered
   standard for a systematic intraday strategy at this scale?

Reminder: there is no real performance data yet. This is purely a request for
review of the *design*, not the results.
