# Predicta Trading Model — Technical Overview for AI Review

Date: 2026-07-06 (updated after round-1 review — see Section 8; updated 2026-07-18, see
Section 8.12; updated 2026-08-29 to cover all three engines — see Sections 10-12;
**updated 2026-08-29 again with a response to DeepSeek's review — see Section 13**)
Purpose: Full technical dump of the current trading system for feedback review. Originally
written for Kimi specifically; this doc is now meant to be shareable with any AI reviewer.

---

## 1. What This System Is

Predicta runs **three separate, independent paper-trading engines**. They share almost
no signal logic and are never mixed into a combined position or score:

| Engine | Files | Style | Hold time | Execution |
|--------|-------|-------|-----------|-----------|
| **High Value** | `models/trading/high_value/signals.py`, `intraday.py` | Day trading + multi-day swing on liquid large/low-priced caps | 30–60 min (intraday) to days/weeks (swing) | Human clicks Buy/Sell |
| **Low Value** | `models/trading/low_value/*` | Contrarian "buy the fear" on sub-$20 beaten-down stocks | 1–5 trading days | Human clicks Buy/Sell |
| **Automaton** | `models/trading/automaton/*`, `fetchers/automaton_runner.py` | Reuses Low Value's contrarian signal engine, held for a much longer horizon | Up to ~126 trading days (~6 months) | **Fully autonomous — no click, buys and sells itself** |

**Sections 3-9 below are the original, detailed technical dump of the High Value
engine** (its two sub-styles: the daily swing engine and the intraday engine), plus the
shared infrastructure both use. **Sections 10-12 (added 2026-08-29) cover Low Value and
Automaton** — Low Value's full technical detail already lives in a companion document
(`low_value_trading4kimi.md`), summarized here; Automaton is new and documented here in
full, since it's the newest and least-reviewed piece of the system.

All three engines currently trade **paper money only** (Alpaca's paper API). A human can
promote an individual High Value or Low Value candidate to a real paper order by clicking
Buy; Automaton places real paper orders on its own. **No engine is authorized to place a
real-money order autonomously** — that action is 100% human-gated on every engine, via a
passcode-protected endpoint, by explicit and deliberate design (see Section 12.5).

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

### 8.10 Round-6 review — Kimi overrode two round-5 declinations, plus a new
human review queue

Kimi pushed back on two of round 5's declines and clarified the actual spec
for each; both are now built. A third item (declined in round 5 as low-value)
was re-proposed with tighter scoping and also built.

**Built:**
1. **Bridgewater "Four Boxes" macro tags** — round 5 declined this citing no
   free source for GDP/CPI *surprise-vs-consensus* data. Kimi's correction:
   the actual ask was lagging, read-only *raw-series* tags, not real-time
   consensus-beating signals — FRED's free API covers that fine.
   `fetchers/fred.py` (new file) wraps `api.stlouisfed.org`, gated behind
   `FRED_API_KEY` (fails safe to None/empty, same convention as
   `OPENWEATHER_API_KEY`). `_fetch_macro_tags()` in `paper_runner.py` logs
   `yield_curve_slope` (10Y-2Y), `fed_rate`, `credit_spread_oas`, and
   `debt_to_gdp_pct` on every regime check. No trading logic changes.
2. **Dalio 3-force overlay** — round 5 declined this because Kimi's original
   spec had it *gate the score threshold* directly, risking corrupted
   collection from a rough proxy. Kimi's round-6 clarification: make it
   **additive** to the existing SPY-gap EXTREME classification, the same
   mechanism the intraday-escalation check (round 3) already uses — not a new
   independent gate. Built exactly that: `long_term_force_contraction` (True
   when HY-OAS credit spread is >2 std devs above its ~1yr mean, OR debt/GDP
   is >1 std dev above its ~5yr mean — Dalio's own thresholds) now ORs into
   the same `regime == "EXTREME"` classification `_fetch_market_regime()`
   already produces. Zero new gates, zero threshold changes — one more way to
   arrive at a classification that already existed.
3. **Human review queue for EXTREME days** (D.E. Shaw hybrid model) — round 5
   declined this as reintroducing a manual step into an automation-first
   pipeline. Kimi's round-6 framing made it explicitly *optional, not
   mandatory*: a new `review_queue` table holds `AWAITING_REVIEW` signals
   that fire during EXTREME-regime days; `check_review_queue_timeouts()` runs
   on every 60s runner tick (regardless of market hours) and auto-resolves
   any row older than 5 minutes to `SKIPPED` — the conservative default. A
   human can instead call `approve_review(id)`, which replays the originally
   stored signal/levels payload (not a re-fetch — avoids reacting to a price
   that's since moved) into `log_hypothetical_trade()`. `GET
   /trade/review-queue`, `POST /trade/review-queue/{id}/approve`, `POST
   /trade/review-queue/{id}/skip` expose this. NORMAL-regime days are
   completely unaffected — direct logging continues exactly as before.

**Not re-litigated:** all six round-5 built items continue unchanged; the
remaining round-5 declines (NLP/sentiment, ML feature store, CI static
analysis, multi-timeframe overlay, HRT-style overnight hold) were not
reopened by Kimi's round-6 feedback and stand as declined.

---

### 8.11 Round-6 follow-up — new engine: Low Value Trades (contrarian sub-$20 scanner)

Kimi's round-6 follow-up asked for a second, fully independent trading
engine — not a review of the existing one. Built exactly as specified, full
detail in `LOW_VALUE_README.md`:

- **Directory split**: `models/trading/high_value/` (moved, unmodified:
  `signals.py`, `intraday.py`, `ngram.py`), `models/trading/low_value/`
  (new: `scanner.py`, `news_overlay.py`, `thesis_tracker.py`),
  `models/trading/shared/` (moved + extended: `kelly.py`,
  `signal_calibration.py`). `fetchers/paper_runner.py` renamed to
  `fetchers/high_value_runner.py` per the spec's own suggestion.
- **Universe scanner**: Alpaca active assets → price/volume/market-cap
  filter → earnings-blackout + 90-day bankruptcy-8-K exclusion → 50-200
  symbols, logged to a new `low_value_universe_snapshot` table.
- **News overlay**: Finnhub 7-day headlines, keyword category flags, VADER
  sentiment — new `fetchers/finnhub.py` (fails safe without
  `FINNHUB_API_KEY`, same convention as `fred.py`).
- **Thesis tracker**: 8 signals (price-vs-20d-low, RSI-14, volume spike,
  insider buying, short interest, sector relative strength, cash-burn
  runway, news sentiment), weighted composite, entry threshold |40|. Missing
  signals are excluded and their weight redistributed — never faked.
- **Runner**: daily 8 AM ET scan + exit check (thesis-resolved / +50% /
  -50% / 5 trading days), fixed $25 sizing, max 3 positions — completely
  separate background thread from the High Value runner.
- **Dashboard + UI split**: `/trading` is now a hub page linking
  `/trading/high-value` and `/trading/low-value`; `GET
  /trade/low-value/dashboard` is a standalone monitor, matching the
  existing High Value dashboard's visual style but built as its own file
  per spec ("two tabs, two engines, no mixing").
- **Shared infra, not shared logic**: `intraday_trades` gained an `engine`
  column (defaults to `'high_value'` — every existing row is unaffected)
  plus `lv_thesis_type`/`lv_news_flags`/`lv_news_sentiment`/
  `lv_headline_count`. `signal_calibration.py`'s base loaders gained an
  `engine` parameter (defaulting to `high_value`, so every pre-existing
  caller is unchanged); Low Value gets its own calibration axis
  (`thesis_type_calibration_report`, 20/50/100 trade tiers) since its 8
  signals don't map onto High Value's per-signal columns.

**One documented deviation**: `trading_logger.py` stayed in `fetchers/`
rather than moving into `models/trading/shared/` as the spec's directory
diagram showed — every other DB-writing fetcher in this codebase
(`fred.py`, `alpaca.py`, `market_data.py`) lives there, and moving it would
mix I/O concerns into `models/` for no functional gain. The behavioral ask
(engine-tagged shared logger) is fully met; only the physical file location
differs from the diagram.

---

### 8.12 Round 7 — User-driven trading-model audit and fixes (2026-07-16 to 2026-07-18)

Not a Kimi round — the user directly asked four audit questions across every trading
engine (which variables are missing / overweighted / mislabeled as protective-vs-risky
/ how confident should the scores really be), then asked for a fix proposal. Answered
with a code-grounded audit, then built a 3-tier plan the user approved, then a series
of follow-on requests (day-trading clarification, plain-language cards, low-price
watchlist). Recording all of it here since it materially changed how scores are
computed and displayed.

**Tier 0 — visibility (2026-07-16).** High Value's dashboard already had a mature
"Phase + readiness" confidence banner and per-signal accuracy report (`per_signal_
accuracy_report`) — this section covers what changed for High Value specifically;
Low Value's equivalent additions are in `low_value_trading4kimi.md`. Nothing in this
tier touched High Value's scoring.

**Tier 1 — wire up existing calibration machinery, still gated (2026-07-16).**
`compute_dynamic_weights()`, `check_signal_kill_switches()`, and `compute_ngram_
blend_weight()` all existed, computed real recalibration from closed-trade data, and
were never called from live scoring — `WEIGHTS` stayed frozen at the original guesses
regardless of what real trades proved.
- `_effective_weights()` (`intraday.py`): returns `WEIGHTS` unless `calibration_
  globally_active()` (100+ closed trades) AND `compute_dynamic_weights()` finds real
  edge (`status == "dynamic"`); independently zeroes any `get_active_kill_switches()`
  signal regardless of the 100-trade gate (kill-switch has its own 50-trade-per-signal
  floor). Cached 5 minutes — this runs once per symbol per scan tick.
- `_composite()` now calls `_effective_weights()` for BOTH the raw score sum and its
  own normalization ceiling, using the same weights for both — a zeroed/reweighted
  signal's contribution shrinks symmetrically instead of just compressing the whole
  score toward zero.
- N-gram blend: `_calibrated_ngram_multipliers()` returns `compute_ngram_blend_
  weight()`'s real agree/disagree multipliers once 20+20 trades exist in each cohort;
  below that, the original hardcoded confidence-scaled boost / flat ×0.7 is unchanged.
- Verified with synthetic trade data for every path: static fallback at 0 trades,
  kill-switch zeroing independent of the weight-recalibration gate (a killed `rsi`
  signal zeroed out even while a synthetic 100-trade dataset gave it a real 0.5
  dynamic weight), dynamic weights actually shifting composite behavior once the gate
  opens, and n-gram multipliers coming back >1 for an outperforming agree cohort and
  <1 for an underperforming disagree cohort.

**Tier 2 — new capabilities (2026-07-17).**
1. **Cross-engine exposure** (`models/trading/shared/exposure.py`, new;
   `GET /trade/exposure`; dashboard cards on both engines). Each engine caps its own
   concurrent positions independently (High Value 3, Low Value 3, Pairs 0 — no cap
   exists), but nothing added them up. Deliberately READ-ONLY — reports position
   COUNTS (not dollars, since the three engines size positions too differently to
   compare in dollar terms yet) and does not enforce any combined limit, since there's
   no validated "safe combined cap" number and guessing one would repeat exactly the
   mistake this whole audit was correcting.
2. **Symmetric macro shadow-log** (`fetchers/high_value_runner.py:_fetch_macro_
   tags()`). `long_term_force_contraction` (built round 6, section 8.10) only ever
   penalized a bad regime; `long_term_force_expansion` mirrors ONLY the credit-spread
   half (HY-OAS >2 std devs BELOW its mean = calm/favorable — a legitimate symmetric
   reading), shadow-logged and never read by `_fetch_market_regime`'s actual gate.
   Debt/GDP deliberately NOT mirrored — Dalio's own long-term debt-cycle framing is
   asymmetric (slow multi-decade rise, sharp deleveraging fall), so a "low debt/GDP is
   bullish" signal would be a fabricated number, not a real one. Verified with
   synthetic FRED data for both the expansion and contraction cases; confirmed the
   regime gate itself is untouched.
3. Dilution warning + short-borrow gate — both primarily Low Value changes, detailed
   in `low_value_trading4kimi.md`. The shortability check (`fetchers/alpaca.py:
   get_asset_shortability()`, real `shortable`/`easy_to_borrow` fields from Alpaca's
   asset record) is shared infrastructure, callable by either engine.

**Plain-language trade card (2026-07-18).** User, unprompted by Kimi, pointed out
they're not a trader and "if the model starts talking to me in technical terms, I'm
screwed." High Value's open-position card went from `"BUY · score 62"` and a
timestamp — no why, no target, no stop shown despite `stop_price`/`target1_price`/
`target_price` already being computed and stored per trade — to a full plain card:
- `_hv_plain_why(t)`: reconstructs a concrete sentence from the top 2 strongest-
  scoring stored signals (`vwap_score`/`or_score`/`rsi_score`/`relvol_score`/
  `gap_score`/`trend_score`/`bollinger_score`/`volsurge_score`) via `_hv_signal_
  phrase()`, a sign/magnitude-based translator grounded in what each `_sig_*`
  function actually measures. **Not a byte-exact replay** of the original label text
  those functions generate at scan time — that text is never persisted to the DB,
  only the numeric score, so this is a re-translation. Flagging this as a design
  choice worth a second opinion (see open questions below).
- `_hv_plain_confidence(score)`: "very strong"/"strong"/"moderate" instead of a bare
  number.
- `_hv_plain_exit(t)`: stop, final target, partial target1 (previously not even
  selected by the dashboard's SQL query), and a fixed "closes by end of today's
  trading session — High Value never holds overnight" line, since every High Value
  position is same-day by construction.
- Raw per-signal scores moved into a collapsed `<details>` block, not removed.
- Verified end-to-end via a real FastAPI dashboard render for both a long position
  (correct "sell for a profit"/"sell to limit the loss" phrasing) and a short
  position (correctly mirrored "buy it back" phrasing in both directions).

**Low-price watchlist mode (2026-07-18).** User clarified their actual objection to
"day trading" wasn't the mechanic, it was the price tier — not interested in $100+
names like AAPL/AMZN, which is why they'd originally asked about Low Value at all.
Wants High Value's real day-trading engine to run a lower-priced universe instead,
**without losing the existing watchlist**.
- `ACTIVE_UNIVERSE_MODE`: `"large_cap"` (unchanged `RUNNER_SYMBOLS`, the original
  8-name list, still fully defined) vs `"low_price"` (currently active).
- `get_active_watchlist()`: in low_price mode, filters `LOW_PRICE_WATCHLIST_
  CANDIDATES` (F, INTC, T, PFE, CSCO, BAC, SOFI, PLTR, NIO, SNAP, UBER, RIVN, WBD,
  KO, NOK) against a LIVE Alpaca price snapshot at call time, keeping only symbols
  actually under `LOW_PRICE_CEILING` ($70) right now. The candidate list is
  explicitly NOT the enforcement mechanism — no sandbox access to verify current
  prices, so the real ceiling is checked live, not assumed. Fails toward an EMPTY
  list on a snapshot error, not toward `RUNNER_SYMBOLS` — silently reverting to
  $100+ stocks would violate the user's stated preference more than skipping a scan.
- All symbol-list call sites (`run_open_scan`, `start_runner`, `get_runner_status`,
  the screener-brief chat trigger, the dashboard's inventory snapshot) now resolve
  through this function instead of the raw constant. Dashboard header shows
  "Low-Price Mode (under $70)" so the active mode is never ambiguous.
- Verified with mocked prices: two candidates that had run past $70 (PLTR $185,
  UBER $85) were correctly excluded from both `get_runner_status()` and the rendered
  dashboard; the large-cap list remained fully intact and recoverable in the status
  response's `large_cap_symbols` field.

**Round 7 open questions for Kimi:**

1. `_hv_signal_phrase()` re-translates a stored numeric score back into plain English
   by sign/magnitude, since the original label text each `_sig_*` function generates
   at scan time (e.g. `"Momentum: above VWAP in uptrend"`) is never persisted to the
   DB. Should the label text itself be added as a new stored column going forward, so
   future dashboards don't need to reverse-engineer it — and is there a risk the
   current re-translation drifts from the real label semantics if `_sig_*`'s bucket
   boundaries change later without this translator being updated in lockstep?
2. Is the low-price candidate pool (F, INTC, T, PFE, CSCO, BAC, SOFI, PLTR, NIO, SNAP,
   UBER, RIVN, WBD, KO, NOK) a sound day-trading universe, or do any of these need
   the regime-conditioning logic (tuned around the original correlated tech-cluster
   assumption) revisited — PLTR and NIO in particular have very different volatility/
   news-gap profiles than the original 8-name list.
3. Should `ACTIVE_UNIVERSE_MODE` eventually support scanning BOTH lists at once
   (with `MAX_CONCURRENT_POSITIONS` scoped per-list) rather than an either/or toggle,
   once there's calibration data to check whether mixing price tiers actually hurts
   the way the original design comment warned mixing volatility regimes would?
4. The cross-engine exposure aggregator is count-based, not dollar-based, since Pairs
   has no position-sizing at all. Worth building a dollar-based version now, or does
   that need Pairs to get real sizing first?
5. Is failing toward an empty watchlist on a snapshot error (rather than falling back
   to `RUNNER_SYMBOLS`) the right default long-term, or should a PERSISTENT failure
   (e.g. 3+ consecutive days) eventually fall back rather than skip indefinitely?

---

## 9. What We're Asking Kimi to Review Now

(Superseded 2026-07-07 — Rounds 3–6 answered and built the round 1–2
questions that used to sit here. Current open questions, per Kimi's own
round-6 read-through:)

1. **Is the 5-minute review-queue timeout too short?** On EXTREME days a
   human has 5 minutes to approve a queued signal before it auto-skips. If
   nobody's at a screen, everything defaults to skip. Should the timeout
   scale with regime severity (e.g., longer window when only one of the two
   3-force conditions is contracting vs. both)?
2. **Should `DATA_COLLECTION_SPRINT_MODE` auto-expire?** It's currently a
   manual boolean with no built-in shutoff. Should it force itself off after
   N trades or N weeks so it can't be silently left on past the point it's
   useful?
3. **Is FRED's silent-failure mode correct?** When `FRED_API_KEY` is
   missing/fails, macro tags log as None with no alert. Is silent failure
   still right for an optional-enrichment signal, or should missing macro
   data surface somewhere (dashboard, log line) instead of just going quiet?
4. **What's the actual trigger for letting Opening Range fire independently
   on wide ranges?** Section 8.8 instruments the suppression rate but never
   set a numeric threshold. Is 60% the real trigger, and is "unusually wide"
   1.5x the 20-day average 15-min range, or something else?
5. **Should the human review queue extend to NORMAL days?** Right now only
   EXTREME-regime signals get queued. Is there a case for an opt-in "review
   everything above |60|" mode once real (non-hypothetical) capital is on
   the line, even outside EXTREME days?

Reminder: there is still no real performance data. All of the above are
architecture questions, not results — the system still needs its first 100
closed trades before any of this can be empirically validated.

---

## 10. Low Value Engine (Summary — full detail in `low_value_trading4kimi.md`)

Contrarian "buy the fear" engine, completely independent from High Value — shares only
infrastructure (position logger, calibration helpers), zero signal logic.

**Universe** (`models/trading/low_value/scanner.py`): all active, tradable, non-OTC US
equities, filtered to price < $20, 20-day avg daily volume > 100k shares, market cap >
$50M, no earnings in the near term, no bankruptcy (SEC 8-K Item 1.03) filing in the last
90 days. Capped at 200 symbols/day.

**Signal engine** (`models/trading/low_value/thesis_tracker.py`): 8 signals, weighted
composite -100..+100 (see table in Section 11.3 below — Automaton reuses this exact
formula). Contrarian polarity: near a 20-day low / oversold RSI / high short interest are
all scored **bullish** here (the opposite of a trend-following engine).

**Entry:** |composite| >= 40 -> BUY (long) or SELL (short, if shortable). **Exit** (checked
once daily): +50% target, -50% stop, thesis-resolved (entered on bad news, a fresh
positive headline appears), or 5 trading days elapsed — whichever comes first. **Sizing:**
fixed $25/trade, max 3 concurrent positions.

**Execution:** human-gated. The scan only ever logs a hypothetical candidate; a human
clicks Buy to place the real paper order.

**Learning:** `signal_calibration.py`'s `compute_low_value_dynamic_weights()` reweights
the 8 signals toward whatever's empirically been winning, but only after 50+ closed Low
Value trades (20 for a preliminary per-thesis-type read, 100 for empirical position
sizing). As of this writing, Low Value has real trade history accumulating but has not
yet crossed the 50-trade dynamic-weight threshold.

---

## 11. Automaton Engine (added 2026-08-28 — new, least-reviewed piece of the system)

### 11.1 What This Is and Why It Exists

Direct user request, modeled on the real "Automaton" AI agent project
(github.com/Conway-Research/automaton — a continuously-running agent that pays for its
own compute and dies if it can't, or spins up funded child agents if it can). Researched
that project first, then asked the user directly how far to take the analogy. Their
answers, which are the actual spec:

- **Autonomous execution: yes**, but scoped entirely to paper trading. It buys and sells
  with no human click.
- **A kill switch on drawdown: explicitly no.** ("It shouldn't be killed, it should
  learn.") There is no code path anywhere in this engine that disables or pauses trading
  based on performance.
- **Cloning / capital-scaling on success: explicitly no.** ("No clone.") One strategy, one
  fixed position size, no variant-spawning, no capital reallocation.
- **What they actually want:** a continuous learn-from-wins-and-losses loop that keeps
  finding more of whatever's been working.

So structurally, Automaton is **Low Value's exact signal formula, wearing a different
hold horizon and a different execution/learning wrapper** — deliberately reused rather
than reimplemented, since "underrated, overlooked stock with a positive catalyst" is the
same thesis Low Value already scores.

### 11.2 Universe (identical formula to Low Value, plus one addition)

Same filtered universe as Low Value (`get_daily_universe()`, literally shared/cached
between the two engines — one scan builds it, the other reuses the same day's result),
**plus**, added 2026-08-29: today's real Alpaca top gainers, top losers, and
most-active-by-volume symbols (`fetchers/alpaca.py: get_top_movers` /`get_most_active`,
free endpoints, same API key). This is purely additive — it can only add candidates to
the scan, never remove any from the filtered universe. Rationale: the filtered universe
answers "is this cheap/liquid/solvent," never "is this actually moving today, and in
which direction" — the movers cross-check answers that second question.

### 11.3 Signal Engine — identical 8-signal composite to Low Value

| Signal | Weight | Bullish when... |
|---|---|---|
| Price vs. 20-day low | 20% | Close to the recent low (contrarian: "sold off, may bounce") |
| Insider buying (30d) | 20% | Insiders bought and didn't sell (+100); mixed buy+sell (+40); sold only (-60); neither (0) |
| RSI(14) | 15% | Oversold (<30) |
| Volume spike | 10% | Today's volume >> 20-day average (magnitude only, direction comes from other signals) |
| Short interest % | 10% | Higher = more squeeze potential (scored as upside, not risk) |
| Sector relative strength | 10% | Outperforming its sector ETF over 5 days |
| Cash burn runway | 10% | Longer runway (also flags if extended by a recent dilutive share sale — display-only warning, not scored) |
| News sentiment | 5% | Positive VADER sentiment / positive-catalyst headline flag |

Composite: weighted average of whichever signals are computable for a given symbol —
**a missing signal is excluded and its weight redistributed proportionally, never
estimated or defaulted.** Composite range -100..+100; positive -> long candidate,
negative -> short candidate (short only if Alpaca confirms the symbol is actually
shortable).

**Real entry bar is |composite| >= 40** (same as Low Value). **Currently running in
"sprint mode"** — logs/executes anything >= 15 instead of 40 — because at the real 40
bar, a live test found only ~2 qualifying candidates out of 500 evaluated per day; at
that rate the 30-real-bar-trade threshold the learning loop (11.7) needs could take
months. The real composite score is always stored regardless of which bar let a trade
in, so this is fully reversible with no data loss. `AUTOMATON_DATA_
COLLECTION_SPRINT_MODE` (bool) / `AUTOMATON_SPRINT_MIN_SCORE` (15.0), independent
constants from Low Value's own (separate, also currently-on) sprint mode. **Trades
logged below the real 40 bar are excluded from weight calibration specifically (added
2026-08-29, see 11.7)** — they still count toward every other diagnostic/readiness read.

### 11.4 Position Sizing and Concurrency

Fixed **$25 per trade** (`AUTOMATON_FIXED_POSITION_DOLLARS`), independent constant from
Low Value's own $25 — not ATR/Kelly/volatility-scaled. Max **5 concurrent open
positions** (`AUTOMATON_MAX_CONCURRENT_POSITIONS`) — a starting guess, not fitted to
anything; larger than Low Value's 3 on the reasoning that a ~6-month hold ties up capital
per-slot for much longer, so more slots are needed to keep the scan doing anything most
days.

### 11.5 Exit Rules (checked once daily, not intraday)

Whichever fires first:
- **+50% target** (`AUTOMATON_TARGET_PCT`)
- **-50% stop** (`AUTOMATON_STOP_PCT`)
- **Thesis resolved** — entered on a negative-news thesis (earnings miss, downgrade,
  regulatory scare, operational crisis) and a fresh positive-catalyst headline appears
- **126 trading days elapsed** (`AUTOMATON_MAX_HOLD_DAYS`, ~6 months) — forced exit
  regardless of price

Same target/stop percentages as Low Value's 5-day hold, just stretched across a ~25x
longer time horizon — **not re-derived for the longer horizon, an open question below.**

### 11.6 Autonomous Execution — the actual mechanism

`fetchers/automaton_runner.py: run_automaton_scan()` scores each candidate, and for
anything crossing the entry bar, **immediately** (same function call, no queue, no
approval step) calls `_auto_execute_entry()`, which places a real Alpaca **paper** market
order — floors to whole shares for a short or a non-fractionable symbol (Alpaca doesn't
support fractional shorts), promotes the DB row from hypothetical to real in place, and
attests the transaction to HSIP (a tamper-proof off-chain hash log of every real order
this app places, across all three engines). `check_automaton_exits()` does the same on
the close side — once daily, it evaluates every open position's exit rule and, if
triggered, calls Alpaca to actually liquidate the position, no approval step.

**This never touches real money.** Every order-placing/closing call in `fetchers/
alpaca.py` runs through `_assert_paper_mode()`, which raises if the configured base URL
isn't Alpaca's paper endpoint — the same guard every other engine's human-triggered
orders already go through. There is no code path in this engine, or anywhere in this
app, that can place a live order without a human clicking a passcode-gated endpoint.

**Human override exists but is not the primary path:** `execute_automaton_trade` (retry
a single candidate whose autonomous order failed) and `close_automaton_trade` (close one
position early) are passcode-gated manual escape hatches. Neither pauses the strategy —
there is no "stop trading" switch anywhere in this engine, by the explicit design
decision in 11.1.

### 11.7 Learning Loop — how it's supposed to actually improve

`models/trading/automaton/learning.py: effective_weights()`. When 30+ of Automaton's own
trades AT THE REAL ENTRY BAR have closed (see the sprint-mode exclusion below —
**updated 2026-08-29**, was originally gated on 50 total closed trades including
sprint-mode ones), `compute_low_value_dynamic_weights(engine="automaton",
min_entry_score=ENTRY_THRESHOLD)` — a formula shared with Low Value's own dynamic-weight
machinery, generalized with an `engine` param so the two engines' calibration never mixes
— recomputes weights per signal as:

```
raw_weight(signal) = max(0, (win_rate(signal) - 0.5) * (avg_pnl_pct(signal) / 100))
weight(signal) = raw_weight(signal) / sum(all raw_weights)
```

i.e. a signal only gets weight if it's both winning more than half the time AND has
positive average P&L when active; weight is proportional to how strong that edge is.
Falls back to the static table if every signal's raw weight computes to zero (no real
edge found anywhere yet). Recomputed at most once per 5 minutes (cached), and **every
trade Automaton places is decided using whatever weight table is active at scan time** —
there is no retroactive re-scoring of past trades.

**Sprint-mode exclusion (added 2026-08-29, per DeepSeek's review — see Section 13):**
Automaton's sprint mode (11.3) logs/executes trades down to |score|=15, but weight
calibration only ever trains on trades that closed at |score| >= 40 (the real entry bar)
— sprint trades still count toward the 20/50/100 readiness tiers and the per-thesis-type/
per-signal diagnostic reads shown on the dashboard, they're just excluded from the
reweighting math itself. `learning_summary()` reports both `n_closed_total` (all closed
trades) and `n_closed_qualifying_real_bar` (the count actually used for calibration) so
the two numbers are never conflated.

**Reset lever (added 2026-08-29, per DeepSeek's review):** `AUTOMATON_LEARNING_ENABLED`
(bool, default True) — NOT a kill switch, has zero effect on scanning/entries/exits.
Since weights were never persisted (recomputed live from trade history every call),
setting this to False reverts every future scan to the untouched static starting weights
within one cache refresh, with no DB cleanup needed — the "how do I roll back a bad
learned state" gap DeepSeek flagged.

**As of this writing: 0 closed Automaton trades.** The learning loop has never fired.
Everything Automaton has done so far (or will do until 30 real-entry-bar trades close) is
on the static starting guess, identical to Low Value's own untuned starting point.

### 11.8 Scheduling

Daily scan window 8:15-8:29 ET (offset 15 min after Low Value's 8:00-8:14 window, to
avoid both engines hitting Finnhub's 60-calls/min budget simultaneously — they share the
same universe-build call). A durable per-date marker (`automaton_scan_log` table) lets
the runner catch up immediately if the process starts/restarts after today's window has
already passed, rather than silently waiting until tomorrow.

### 11.9 Current Status

**v1, uncalibrated, zero resolved trades.** Nothing above has been validated against
real outcomes. This is a rules-based heuristic composite score with human-guessed
weights, not a backtested or fitted model. The entire reason it runs in paper mode with
this much logging/transparency is that none of it is trusted yet.

---

## 12. Open Questions — Requesting AI Feedback (2026-08-29)

1. **Is reusing Low Value's contrarian signal set conceptually right for Automaton's
   stated thesis?** Low Value's 8 signals were built around "beaten-down stock, sellers
   overshot, reverts in 1-5 days." Automaton's stated goal is "underrated stock that
   could explode in price within 6 months" — closer to an asymmetric-upside/catalyst
   thesis than a pure mean-reversion one. Is scoring both engines with the identical
   formula defensible (shared "overlooked value" thesis), or does a 6-month explosive-
   growth thesis actually need different signals entirely (e.g. relative strength /
   momentum breakout components, which Low Value's contrarian polarity actively scores
   as bearish)?
2. **Are the ±50% target/stop levels right for a ~126-trading-day hold, unchanged from
   Low Value's 5-day hold?** Low Value's own docs note that in practice a wrong-thesis
   position usually exits via the time limit around -10%, long before the -50% stop ever
   triggers — that logic doesn't obviously transfer to a hold 25x longer, where far more
   underlying business/market drift can happen before a time-based exit ever fires. Should
   the stop be tighter, or trail, for a hold this long?
3. **Is a fixed $25/trade position size defensible for a 6-month hold?** Low Value's
   rationale for fixed sizing (cap the damage of one bad pick, thin/illiquid names) still
   applies, but capital sits committed 25x longer per trade here with only 5 concurrent
   slots — is there a real opportunity cost worth addressing (e.g. should closed-early
   capital get recycled faster, or is the 6-month lockup by design)?
4. **Is "no kill switch, ever" the right call even for pure paper trading?** The user was
   explicit and this was a deliberate design choice, not an oversight — but is there a
   version of "it should learn, not be killed" that still includes some form of automatic
   circuit breaker (e.g. pause NEW entries, not existing positions, past some extreme
   drawdown) without contradicting the user's actual intent, or does any such mechanism
   inherently become the kill switch they explicitly rejected?
5. **Sprint mode's 15-point entry bar (vs. the real 40) — is this introducing selection
   bias into the very data the learning loop will train on?** Trades logged at 15-39
   points are, by the model's own logic, weaker-conviction signals. If the learning loop
   (Section 11.7) reweights based on a pool dominated by sub-threshold trades, is there a
   risk it converges on weights that look good on weak signals but wouldn't hold at the
   real 40-point bar Automaton is meant to eventually run at?
6. **Movers cross-check (Section 11.2) has no separate scoring treatment** — a symbol
   pulled in because it's today's #1 gainer gets scored by the exact same contrarian
   formula as everything else, with no signal for "this is already moving, right now,"
   which seems like exactly the information a movers feed is meant to supply. Should
   there be a 9th signal (or a scoring modifier) that actually uses the movers data
   itself, rather than just widening which symbols get the existing formula?
7. **Should Automaton and Low Value share ANY learned state, or is full separation
   (current design) correct?** They run the identical signal formula on largely
   overlapping universes with different hold horizons. Right now their calibration data
   is 100% siloed (`engine="automaton"` vs `engine="low_value"`) — is there value in one
   informing the other's priors once one has real trade history and the other doesn't, or
   would that contaminate the very thing that makes each engine's calibration meaningful
   (its own actual hold-horizon-specific outcomes)?

Reminder, same as every other section in this document: there is no real performance
data behind Low Value or Automaton yet. These are architecture questions about mechanism
and risk, not requests to validate results — there are no results to validate.

---

## 13. DeepSeek Review (2026-08-29) — Response and Status

Shared this document with DeepSeek for independent feedback. Full review covered High
Value (Sections 3-9), Low Value/Automaton (Sections 10-12), and general suggestions.
Two claims were checked against the actual code before acting on anything (both were
off in ways worth recording for future reviewers):

- DeepSeek estimated Automaton could reach ~50-60 new positions/year. With the actual
  5-concurrent-position cap and up to a 126-trading-day hold, the real ceiling if every
  slot runs full-term is **~10/year** (5 x 252/126). This makes the "will the learning
  loop ever get enough data" concern worse than DeepSeek's own framing, not better.
- DeepSeek described "no rollback mechanism except manually editing the DB weights
  table." There is no persisted weights table — `effective_weights()` recomputes live
  from trade history every call (5-min cache). A rollback lever was trivial to add
  precisely because nothing was ever persisted (see 13.2 below).

### 13.1 Fixed now — sprint-mode trades excluded from weight calibration (P0)

DeepSeek's concern: reweighting on sub-threshold (sprint, |score| 15-39) trades risks
converging on weights tuned for weak signals that were never tested at the real 40-point
entry bar. **Agreed, implemented.** `signal_calibration.py`'s Low Value calibration
functions gained a `min_entry_score` param (default 0.0 — every existing caller,
including Low Value's own, is unaffected); Automaton's `effective_weights()` now passes
`min_entry_score=ENTRY_THRESHOLD` (40) specifically for the weight-calibration call.
Sprint-mode trades still count toward readiness tiers and the per-thesis-type/per-signal
diagnostic reads (the "velocity, not calibration" distinction DeepSeek proposed) — only
the actual reweighting math excludes them. Verified with a synthetic test: 35 winning
sprint-mode trades alone produced zero learning; adding 30 winning real-bar trades
correctly triggered it. See 11.7.

### 13.2 Fixed now — a real reset lever for learned weights (part of DeepSeek's #4)

`AUTOMATON_LEARNING_ENABLED` (bool, default True, `models/trading/automaton/learning.py`)
— explicitly NOT a kill switch (zero effect on scanning, entries, or exits, honoring the
user's "it shouldn't be killed" requirement) — controls only whether `effective_weights()`
may deviate from the static starting table. Flipping it to False reverts every future
scan to the untouched weights within one cache TTL. Verified with a synthetic test.

### 13.3 Deferred to the user's explicit decision — not implemented

These change what the engine screens for or its risk profile, not just a correctness
fix, so they need the user's sign-off rather than an autonomous edit:

- **P0 — add momentum/growth signals** (price vs. 50-day high, longer-window relative
  strength, earnings surprise if reliably available). DeepSeek's core critique: Low
  Value's 8 signals are mean-reversion-polarized (near-low, oversold RSI, high short
  interest all score bullish) and structurally cannot express "rising relative strength,
  new catalyst, improving fundamentals" — the actual shape of a 6-month explosive-growth
  thesis. The learning loop can only reweight the features it has; it can't discover it
  needs different ones. This is the single biggest open item.
- **P1 — target:stop ratio.** Current ±50%/±50% is Low Value's 5-day exit unchanged,
  stretched across a hold ~25x longer. DeepSeek's suggestion (e.g. 60% target / 20% stop,
  a 3:1 ratio) reflects a "win less often, win bigger" growth-thesis risk profile instead
  of the current "win about half the time" mean-reversion profile — which is really the
  same underlying question as 13.3's first item: the right target:stop ratio depends on
  which thesis the signal set is actually built to detect.
- **P1 — position size / concurrency cap.** DeepSeek's proposed fix (raise $25 and/or the
  5-position cap to speed up learning) is reasonable in isolation, but changes how much
  paper capital-equivalent risk sits in each trade — a call about risk posture, not a bug.
- **Movers cross-check as a scored signal, not just a universe filter** (DeepSeek's #6,
  matches this doc's own Section 12 Q6). Investigated but NOT implemented as suggested:
  DeepSeek's proposed polarity (gainer=bullish, loser=bearish) is a MOMENTUM framing that
  directly contradicts every other signal in the composite, which is CONTRARIAN (a top
  loser today, under this engine's existing "sold off = bullish" polarity, would actually
  read as MORE bullish, not less). Scoring it DeepSeek's way would make the composite
  internally inconsistent without first resolving 13.3's first item. This is coupled to
  the signal-set question, not a standalone fix.
- **Graduated sprint threshold** (P2 — start at 15, raise gradually as trade count grows)
  and **N-gram historical validation on the High Value engine** (P2) — smaller items, not
  actioned, open for a future round.

### 13.4 Where this leaves Section 12's open questions

Q1 (signal-thesis fit) and Q2 (exit-level scaling) — DeepSeek concurred these are real
problems, not resolved, tracked in 13.3. Q5 (sprint-mode selection bias) — DeepSeek
concurred, **fixed**, see 13.1. Q6 (movers as a real signal) — DeepSeek concurred it's a
missed opportunity, investigated, **coupled to Q1**, see 13.3. Q3, Q4, Q7 — DeepSeek's
answers (Q3: fixed sizing defensible, cap limits learning speed; Q4: honor "no kill
switch," but a reset lever for LEARNED STATE specifically is fine — addressed, see 13.2;
Q7: keep Automaton/Low Value calibration fully separate) all agree with this document's
original reasoning; no change made.
