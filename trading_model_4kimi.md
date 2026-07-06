# Predicta Trading Model — Technical Overview for Kimi

Date: 2026-07-06 (updated after round-1 review — see Section 8)
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

**Update after round-1 review:** both flips are now gated by a `regime_confidence`
field ("strong"/"weak") computed alongside the trend label — see Section 8.1.

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
treats words — a 4-bar pattern (e.g. "UUDU") has a historical frequency of what
comes next, built from 6+ months of bar history. 81 possible 4-bar patterns
(3^4, since each bar is U/D/E). **Extended from 3→4 bars after round-1 review**
— see Section 8.4.

- Only fires when historical frequency exceeds 52% in one direction with at least
  40 historical occurrences (raised from 30 to maintain statistical power for the
  larger 81-pattern space)
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
  when available for transparency. **Veto logic rebuilt after round-1 review** —
  see Section 8.3: the naive "win rate < 45%" point-estimate check is replaced
  with a confidence-interval test, and both the empirical win rate and the veto
  are now gated behind 100 total closed trades globally (was previously usable
  once a single bucket had 10+ trades). Classical quarter-Kelly sizing
  (`trading_kelly()`) exists as a separate function but is not yet wired into
  the live pipeline — it's waiting on `calibrated_win_rate()` to have enough
  data to be trustworthy.

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
  \|score\| ≥ 20 as a hypothetical trade (no real order placed) — raised to 60
  on EXTREME market-regime days (see Section 8.5). Temporarily lowered to
  ≥10 while `DATA_COLLECTION_SPRINT_MODE = True` (see Section 8.8) — reversible,
  entry_score is always stored so post-hoc filtering back to ≥20 stays possible
- **Every 30 min:** checks all open hypothetical positions against actual 5-min
  bar highs/lows (not just the current price) so a stop or target touched
  between checks isn't missed
- **3:50 PM ET:** force-closes any remaining open positions at market price
- **Portfolio cap (added after round-1 review):** stops logging new entries once
  3 hypothetical positions are open simultaneously (see Section 8.2) — the
  8-symbol universe is one correlated tech cluster, so this replaces a
  pairwise-correlation check that isn't meaningful across a single cluster
- **Market regime gate (added after round-1 review):** SPY overnight gap used as
  a volatility-regime proxy; on EXTREME days (gap ≥2%), the logging threshold
  rises to 60 (see Section 8.5)
- **Earnings blackout mechanism (added after round-1 review):** a manually-
  populated per-symbol date list to skip known earnings days. Round 4: populated
  with AAPL's confirmed 2026-07-30 Q3 release (only company-confirmed dates are
  added — the other 7 tickers haven't announced theirs yet, and guessing would
  be worse than no filter)
- **Macro event tag (added round 4):** logs macro_event_today (FOMC decision
  days, sourced from federalreserve.gov's 2026 calendar) on every hypothetical
  trade — read-only, not used to filter or size trades yet
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

## 7. Data Sources (cont'd — regime proxies)

The market-regime gate (Section 8.5) adds two more read-only Alpaca calls per
scan day: SPY (overnight gap, volatility-regime proxy) and XLK (tech sector ETF,
day-change sector-rotation tag). Both come from the existing `get_snapshots()`
batch endpoint already used for the 8-symbol watchlist — one extra API call per
day, negligible against the free-tier rate limit.

---

## 8. Changes Implemented After Round-1 Review

Kimi's round-1 review (5 direct questions + 5 structural gaps) and round-2
follow-up ("bridge from mechanism to validated edge") produced a prioritized
list. Here's what was built, in the order of Kimi's own priority table:

### 8.1 Regime-conditioning lag (Kimi's Q1 — "sound but with execution risk")

`_sig_trend_bias()` now also computes `ma_spread_pct` (`abs(ma20-ma50)/mid*100`)
and a `regime_confidence` field: `"strong"` when the two MAs are ≥2% apart,
`"weak"` when compressed. `_sig_vwap()` and `_sig_gap()` both take a new
`trend_confidence` parameter — the conditioning flip (the ±15/-20 VWAP swap, the
trend-following gap branch) only applies when confidence is `"strong"`; a
compressed/ambiguous MA spread falls back to the original regime-neutral logic
instead of trusting a label that could flip on the next session.

**Not implemented:** Kimi's other suggestion — a faster pre-market/overnight
regime detector as a tie-breaker — was not built. The MA-spread confidence gate
addresses the core risk (acting on an unreliable label) more directly than a
second detector would, and adding a second regime signal before the first one
has any real-world validation risked compounding unvalidated assumptions.

### 8.2 Portfolio/correlation risk (Kimi's structural gap A — High priority)

`fetchers/paper_runner.py` now caps simultaneous open hypothetical positions at
`MAX_CONCURRENT_POSITIONS = 3` across the whole 8-symbol universe. Kimi's
original framing (pairwise correlation, sector exposure caps) doesn't map
cleanly onto this watchlist since all 8 symbols are one correlated tech
cluster, not diversified sectors — so instead of computing pairwise
correlations, `run_open_scan()` just refuses to log a new entry once
(already-open + logged-this-scan) reaches the cap. This directly prevents the
scenario Kimi flagged: 6+ correlated shorts firing simultaneously on a
sector-wide move, each sized to 25% of account.

### 8.3 The 45% win-rate veto (Kimi's Q3 — "too permissive, statistical trap")

`models/trading/signal_calibration.py` gained `veto_decision()`, which computes
an 80% Wilson-score confidence interval for the bucket's true win rate and
vetoes only when the CI's upper bound sits entirely below 48% — replacing the
old point-estimate check (`win_rate < 0.45`). This requires roughly 25-30+
trades in a bucket to ever fire, exactly matching Kimi's estimate. Verified with
synthetic data: n=40 at a 25% observed win rate vetoes; n=40 at a 40% observed
win rate does not (CI too wide to be confident the edge is gone); n=15 never
vetoes regardless of win rate (below the 30-trade floor).

Additionally — per Kimi's round-2 ask to "block all calibration, Kelly sizing,
and weight changes until closed_trades_count >= 100" — both the empirical win
rate lookup and the veto are now gated behind a new
`calibration_globally_active()` check (`MIN_TRADES_FOR_ANY_CALIBRATION = 100`).
Below 100 total closed trades, `kelly_from_signals()` reports
`win_rate_source: "gated_pending_100_trades"` and sizes purely on ATR, with no
empirical overlay at all. `compute_dynamic_weights()` (the 30-trade-gated
diagnostic weight calculator) was left as-is — it's read-only, exposed via a
status endpoint, and never automatically applied to the live `WEIGHTS` constant
in `intraday.py`, so "blocking weight changes" was already structurally true.

### 8.4 N-gram context window (Kimi's Q4 — "3 bars likely noise")

`PATTERN_LENGTH` extended from 3 to 4 bars (81 patterns instead of 27),
`min_samples` raised from 30 to 40 to hold statistical power constant across
the larger pattern space. 4 bars = 20 minutes of context, closer to the
model's 30-60 minute intended hold than the previous 15-minute window.

**Not implemented:** Kimi's two enhancement ideas — recency-decayed pattern
weighting, and magnitude-encoded states (Strong/Weak × Up/Down instead of
plain U/D/E) — were left as future work. Both are legitimate upgrades but add
real complexity/parameters to a component that has zero live validation yet;
building them now would be tuning an unmeasured system, which is the exact
failure mode Kimi's round-2 feedback warns against.

### 8.5 Market regime gate (Kimi's structural gap E — High priority)

New `_fetch_market_regime()` in `paper_runner.py`: fetches SPY + XLK snapshots
once per scan, computes SPY's overnight gap % as a volatility-regime proxy (no
VIX access on the Alpaca free tier) and XLK's day-change % as a simple
sector-rotation tag. When `abs(spy_gap_pct) >= 2.0`, the day is classified
EXTREME and `run_open_scan()`'s effective logging threshold rises from 20 to 60,
so the dataset isn't contaminated by signals fired during untradeable
volatility. Fails safe to NORMAL on any API error.

Per Kimi's round-2 ask ("add regime tags to the v4 schema... you'll need to
know which market conditions produced the 55% vs 45% results"), every
hypothetical trade now also stores `spy_gap_pct`, `xlk_change_pct`, and
`market_regime` (v5 schema columns) — logged for future post-hoc analysis,
never fed back into live scoring.

**Round-3 update:** Kimi pushed back on the "wait until live capital" call for
the *tagging* half of this — see Section 8.7. Position-size scaling itself is
still deferred (unchanged reasoning: no effect on paper-mode `pnl_r`).

### 8.6 Deferred, not built (with reasons)

- **Earnings/event filter (structural gap D, Low priority):** mechanism built
  (`EARNINGS_BLACKOUT` dict + `_is_earnings_blackout()` check in
  `paper_runner.py`), but the calendar itself is empty — no live earnings-date
  API is wired in, and fabricating specific dates without a real source would
  be worse than no filter at all. Add real dates manually as they're confirmed.
- **Spread velocity check (structural gap C, Low priority):** would require
  tracking bid/ask spread across repeated intra-session snapshots, which the
  runner doesn't currently sample (it only reads spread once per position
  check). Building real spread-history tracking is a bigger addition than its
  Low-priority ranking justified this round.
- **Bollinger/VWAP weight redundancy (Q2, Low priority):** Kimi's own
  meta-observation explicitly cautioned against tuning existing signal weights
  before real data exists — "the biggest risk at this stage is that Claude will
  suggest optimizing weights or thresholds that should remain fixed until you
  have 100+ closed trades per bucket." Given that instruction and the item's own
  Low-priority tag, `WEIGHTS` in `intraday.py` was left untouched. Once 100+
  trades exist, `per_signal_accuracy_report()` (already built, gated at 10
  active trades per signal) will show empirically whether Bollinger is actually
  redundant with VWAP — that's the point at which reallocating weight is a
  data-driven decision instead of a guess.

### 8.7 Round-3 review — three items built

Kimi's round-2 review of Sections 8.1/8.3/8.5 came back positive, with three
concrete additions:

1. **Market vol regime tag (LOW/NORMAL/HIGH).** New `_spy_realized_vol_pct()`
   computes SPY's 20-day annualized realized vol (log-return based);
   `_vol_regime_bucket()` sorts it into LOW (<12%), NORMAL, or HIGH (>25%).
   Logged as `spy_realized_vol_pct`/`market_vol_regime` (v5b schema columns)
   on every hypothetical trade — read-only, same convention as the other
   regime tags. Kimi's point: without this, the first 100 trades mix high-vol
   and low-vol regimes with no way to separate "bad signal" from "bad market"
   after the fact.
2. **Intraday SPY re-check.** The 9:35 AM scan only sees the overnight gap —
   a stock that opens flat and sells off 3%+ intraday would never trip the
   EXTREME gate. New `_check_intraday_regime_escalation()` runs at every
   30-min position check, and if SPY's cumulative change from prior close
   hits ±3%, sets a same-day override that `_fetch_market_regime()` honors for
   any later manual re-scan. Reset at day rollover via
   `_reset_regime_override_if_new_day()`.
3. **`WILSON_CONFIDENCE` named constant.** `_wilson_ci`'s confidence level was
   a hardcoded `0.80` default; now a module constant in
   `signal_calibration.py` so tightening it later (e.g. to 0.90/0.95 past 500
   trades) is a one-line change instead of re-auditing the veto function.

All three verified: vol bucketing against synthetic low/high-vol SPY series,
intraday escalation carrying an EXTREME classification into a later same-day
scan despite a flat overnight gap, and the veto's reason text reflecting the
named constant.

### 8.8 Round-4 review — data velocity + instrumentation

Kimi's round-4 feedback centered on one core point: at the pre-round-4 rate
(≤8 trades logged/day, 3-position cap, |score|≥20 floor), reaching the
100-trade validation floor could take 6-12 months. Four items came out of it:

1. **`DATA_COLLECTION_SPRINT_MODE` (P0).** Lowers the effective logging floor
   from 20 to `SPRINT_MIN_SCORE = 10` for `run_open_scan()`, explicitly flagged
   and reversible (flip the constant back to `False`). This does not change
   what's *measured* — `entry_score` is stored on every row regardless, so any
   later analysis can still filter back to ≥20. It only changes how many rows
   get collected while waiting.
2. **Macro event tag.** `MACRO_EVENT_DATES` — a static set of 2026 FOMC
   decision days, sourced from federalreserve.gov (not guessed) — logged via
   `macro_event_today` on every trade. Kimi's weather-condition suggestion
   (OpenWeatherMap, sunny/cloudy/rainy in NYC) was declined: there's no
   established mechanism linking local weather to AAPL/NVDA/TSLA intraday
   moves the way there is for agricultural commodities — adding it would be
   noise dressed as a feature, not a real hypothesis.
3. **Earnings blackout populated (partially).** Only AAPL's Q3 2026 date
   (July 30) is actually confirmed by the company as of this writing; the
   other 7 tickers haven't announced theirs. Rather than estimate/guess dates
   for them (which Kimi's own framing correctly flagged as worse than no
   filter in round 1), only the one verified date was added. Add the rest as
   each company confirms.
4. **Suppression-rate instrumentation.** `_record_suppression_stat()` tallies
   every scan (not just logged ones) into `weak_regime_inside_or_scans` /
   `total_scans`, surfaced via `GET /trade/paper-runner/status`. This is pure
   counting — no scoring change — and answers Kimi's round-2 question
   (Section 8.1's open item) about how often `regime_confidence == "weak"` and
   price-inside-opening-range co-occur. If it clears 60%, that's the trigger
   to let Opening Range fire independently on unusually wide ranges; not built
   yet since there's no data to justify it.

**Declined, with reasoning:**
- **HMM-based regime detection** replacing the MA20/MA50 trend signal — this
  would swap a simple, interpretable heuristic for an unvalidated ML model
  with a new dependency (`hmmlearn`) and training-data requirements, before
  the *simple* version has any real trade data behind it. Same principle Kimi
  used to argue against tuning weights pre-data, applied to architecture
  instead of thresholds.
- **Spread velocity check** — still deferred; would require repeated
  intra-scan quote sampling the runner doesn't currently do, and the payoff is
  unclear before real trade data exists to show whether execution quality is
  actually a problem.
- **Cloud backtesting infrastructure** — Kimi's own dependency graph gates
  this at 100+ trades; not building spend-incurring infra ahead of that.

### 8.9 Round-5 review — cross-firm methodology sweep (Bridgewater, Two Sigma,
Jane Street, HRT, D.E. Shaw, Citadel, Citadel Securities)

Kimi's round-5 feedback surveyed seven quant firms for stealable ideas, scored
across P0-P2. Six items were built; nine were declined with reasoning.

**Built:**
1. **Risk-parity position sizing** (`risk_parity_position_size()` in
   `kelly.py`) — inverse-volatility sizing as an alternative to fixed 1% risk,
   gated behind `RISK_PARITY_MODE = False`. Inert by default.
2. **Manual override log** — `manual_override_log` table + `log_manual_override()`,
   wired into `POST /trade/order` (the one endpoint that bypasses signal
   generation entirely). Jane Street's "never override the computer" rule
   applied structurally: overrides aren't forbidden, just never silent.
   `GET /trade/manual-overrides` surfaces the history.
3. **Signal bucket kill switch** — `check_signal_kill_switches()` in
   `signal_calibration.py`: after 50 active trades for an individual signal,
   if its Wilson CI upper bound sits below 48%, it's marked `BUCKET_KILLED`
   and persisted to a new `signal_kill_switches` table (survives restarts).
   Requires a manual `resurrect_signal()` call plus 20 new active trades to
   requalify. Diagnostic + persisted flag only — does not itself zero out
   `WEIGHTS`, since dynamic weights aren't wired to the live ensemble yet.
4. **Per-signal P&L on the dashboard** — `/trade/dashboard` now has a "Which
   Signals Are Actually Working" card, reusing the already-built
   `per_signal_accuracy_report()` rather than building a separate view.
5. **Effective-cost diagnostic** — `_effective_cost_diagnostic()` in
   `intraday.py` computes `effective_cost_pct` (spread + slippage + a
   square-root market-impact proxy) and flags `margin_too_thin` above 0.15%.
   **Kept diagnostic-only, not a hard reject** — the existing 4-tier spread
   system already gates trades, and folding this in as a second hard reject
   would silently swallow most of the ELEVATED_SPREAD tier the sprint-mode
   data collection depends on. Verified: retail-size positions against
   large-cap ADV show market impact isn't the binding cost (confirms Kimi's
   own framing — that's Citadel Securities' problem at billions of dollars,
   not ours at $10k).
6. **Inventory/exposure snapshot on the dashboard** — per-ticker open/flat
   status, side, and days-since-last-trade for all 8 watchlist symbols, plus
   long/short/open counts. Skipped the correlation heatmap Kimi also
   suggested — real added complexity for marginal value with 0 real trades.

**Declined, with reasoning:**
- **Bridgewater's "Four Boxes" macro tags** and **Dalio's 3-force regime
  gate** — both need real GDP/CPI *surprise-vs-consensus* and credit-spread
  data with no free source (FRED gives raw series, not consensus estimates).
  Approximating would produce misleading regime labels, not informative ones.
  The 3-force version is worse: Kimi's spec has it actually *gate the score
  threshold*, so bad proxies would corrupt the collection process this whole
  session has been trying to accelerate.
- **NLP + social sentiment logging** — both require new rate-limited external
  APIs living inside the critical 9:35 AM scan path, for signals with no
  established validation yet. Revisit after simpler signals are validated at
  100 trades.
- **ML feature store** — `intraday_trades` already *is* the feature store
  (all v4/v5/v5b/v5c tags land there); a separate store would just
  duplicate it.
- **Static analysis in CI** (mypy/pylint/bandit) — this repo has a documented
  CI landmine (Railway/GitHub Actions race, see CLAUDE.md's NRFI automation
  section); not touching that surface for tooling hygiene. Also, this sandbox
  can't even install the dependencies to test it.
- **15-min multi-timeframe overlay** and **HRT-style overnight hold** — both
  are core signal-behavior changes before any real trade data exists, same
  "don't tune what's unmeasured" logic used to decline HMM in round 4.
- **D.E. Shaw-style human review queue for extreme days** — Kimi's own table
  rates this Low impact, and it reintroduces a manual step into a pipeline
  this session already optimized for automated collection speed
  (`DATA_COLLECTION_SPRINT_MODE`).

---

## 9. What We're Asking Kimi to Review Now

1. **Section 8.1** — is confidence-gating via MA-spread compression (vs. a
   second faster regime detector) a sufficient fix for the lag risk, or does
   the compressed-regime fallback (treating it as regime-neutral) need its own
   tie-breaker?
2. **Section 8.3** — is an 80% Wilson CI with a 48% floor the right
   statistical strictness, or should the confidence level / floor be adjusted?
3. **Section 8.5** — is a single SPY-gap threshold (2%) a reasonable one-signal
   proxy for "abnormal market day" given no VIX access, or is it too coarse
   (e.g., should intraday realized vol of SPY also factor in)?
4. Anything in Section 8.6's deferred list that should be reprioritized higher
   despite the reasoning given?

Reminder: there is still no real performance data. All of the above are
architecture changes made in response to review, not results — the system
still needs its first 100 closed trades before any of this can be empirically
validated.
