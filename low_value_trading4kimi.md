# Low Value Trades — Technical Dump for Kimi Review

Date: 2026-07-07
Purpose: full technical dump of the new Low Value contrarian sub-$20 engine,
built per your round-6 follow-up feedback. Separate document from
`trading_model_4kimi.md` (which covers the existing High Value engine and
the six rounds of review that shaped it) because this is a distinct system
with its own thesis, its own signals, and its own calibration — the two
share infrastructure, not logic, and this doc should be reviewable on its
own without re-reading six rounds of High Value history.

---

## 1. What This Is

A second, fully independent paper-trading engine: a contrarian scanner for
sub-$20 stocks. "Buy the fear" thesis — beaten-down price action, oversold
momentum, capitulation volume, insider buying, and positive catalysts are
all scored as BULLISH, the opposite polarity from a trend-following engine.
Built for daily personal use with small, fixed-size positions ($25/trade) —
explicitly not the same product as High Value (which stays frozen,
documented, and aimed at eventual monetization).

**Zero shared signal logic with High Value.** Shared only: the
`intraday_trades` DB table (scoped by a new `engine` column), the trade
logger, and position-sizing/calibration *infrastructure* (not thresholds or
formulas).

**Status: built, not yet run.** Same caveat as every other section of this
project's review history — no closed trades exist yet. Every number below
describes the mechanism, not a track record.

---

## 2. Directory Restructure

```
models/trading/
  high_value/    signals.py, intraday.py, ngram.py       — MOVED, logic unchanged
  low_value/     scanner.py, news_overlay.py, thesis_tracker.py   — NEW
  shared/        kelly.py, signal_calibration.py           — MOVED, extended (engine param)
  pairs.py, screener.py                                    — unchanged location, imports updated

fetchers/
  high_value_runner.py    — renamed from paper_runner.py, logic unchanged
  low_value_runner.py     — NEW, daily background runner
  low_value_dashboard.py  — NEW, standalone HTML monitor
  trading_logger.py       — extended: engine column, log_low_value_trade, universe snapshot logging
  finnhub.py, sec_edgar.py, openinsider.py, finra.py, yahoo_quote.py  — NEW data fetchers
```

Every High Value file that moved kept its logic byte-for-byte identical —
only import paths changed (verified via standalone syntax checks + a
functional test suite before committing). `WEIGHTS`, thresholds, and
scoring formulas in `signals.py`/`intraday.py`/`ngram.py` are untouched.

---

## 3. Universe Scanner (`models/trading/low_value/scanner.py`)

Runs once daily (cached in-memory per day, refreshed at the runner's 8 AM
ET tick — see Section 6):

1. `fetchers.alpaca.get_all_active_assets()` — all active, tradable,
   non-OTC US equities from Alpaca's `/v2/assets`.
2. Cheap first pass: batch snapshots (Alpaca allows large symbol batches
   in one call), keep only `last_close < $20`.
3. Per surviving candidate, in cost order (cheapest check first):
   - Skip if in `EARNINGS_BLACKOUT` for today (read-only import from the
     frozen High Value runner — same list, no duplication).
   - `avg_daily_volume_20d > 100,000` (Alpaca daily bars).
   - `market_cap > $50M` — Finnhub primary (`stock/profile2`), Yahoo
     Finance backup when Finnhub misses.
   - Skip if an 8-K with Item 1.03 (Bankruptcy/Receivership) was filed in
     the last 90 days (SEC EDGAR submissions API, via a cached
     ticker→CIK map from `company_tickers.json`).
4. Cap at 200 symbols, sort, log to a new `low_value_universe_snapshot`
   table (date, symbol list, count) for after-the-fact composition
   auditing.

**Open question for you:** the bankruptcy/market-cap/volume checks run
per-candidate after the cheap price filter, which could still be a few
hundred symbols on a volatile day. I bounded this with `max_candidates` as
a safety valve but haven't load-tested against Finnhub's 60-calls/min
ceiling at the high end of a real market's candidate count. Worth flagging
if you think this needs a harder cap or a smarter early-exit order.

---

## 4. News Overlay (`models/trading/low_value/news_overlay.py`)

- `fetchers/finnhub.py`'s `fetch_news_batch()` pulls 7-day company news per
  symbol, sleeping 60s every 60 calls to respect Finnhub's free tier —
  exactly the rate-limit pattern you specified.
- Headline category flags (case-insensitive keyword match, a headline can
  carry multiple):
  - `EARNINGS_MISS`: "miss", "misses", "below estimate", "guidance cut"
  - `ANALYST_DOWNGRADE`: "downgrade", "cut to sell", "price target lowered"
  - `REGULATORY_RISK`: "sec investigation", "fda rejects", "lawsuit", "fraud"
  - `OPERATIONAL_CRISIS`: "layoff", "layoffs", "restructuring", "bankruptcy filing"
  - `POSITIVE_CATALYST`: "contract win", "partnership", "fda approval", "breakthrough"
- `INSIDER_BUYING` is NOT a headline flag — it's a separate SEC/OpenInsider
  fetch (`fetchers/openinsider.py`, scrapes the per-ticker screener page for
  code-P Purchase transactions in the last 30 days), matching your spec.
- Sentiment: VADER (`vaderSentiment` package, added to `requirements.txt`)
  compound score (-1..+1) on article summaries, averaged across a symbol's
  articles. Fails safe to 0.0 (neutral) if the package isn't installed —
  never blocks the scan.

---

## 5. Signal Engine (`models/trading/low_value/thesis_tracker.py`)

8 signals, **daily bars only** (no 5-minute data — no regime-conditioning
from High Value; these names don't have reliable intraday trends to
condition on):

| Signal | Weight | Formula | Polarity |
|---|---|---|---|
| `price_vs_20d_low` | 20% | `(close - 20d_low) / 20d_low` → `max(0, 1 - ratio/0.30) * 100` | Higher = closer to the 20d low = more bullish. Floors at 0, never negative. |
| `rsi_14` | 15% | Standard 14-period RSI → `(50 - rsi) * 2`, clamped ±100 | Oversold (low RSI) = positive; overbought = negative |
| `volume_spike` | 10% | `today_vol / 20d_avg_vol` → `(spike - 1) * 40`, clamped [0, 100] | Magnitude-only capitulation intensity, never negative on its own |
| `insider_buying_30d` | 20% | Boolean (OpenInsider) → 100 or 0 | No penalty for absence |
| `short_interest_pct` | 10% | Nasdaq/FINRA short-interest % → `pct * 4`, clamped [0, 100] | Squeeze-potential framing, not a risk flag |
| `sector_relative_strength` | 10% | `symbol_5d_return - sector_etf_5d_return` → `relative * 10`, clamped ±100 | Sector ETF resolved from Finnhub's `finnhubIndustry`, falls back to SPY if unmapped |
| `cash_burn_months` | 10% | `cash / avg_monthly_burn` → `(months - 6) * 10`, clamped ±100 | None if Finnhub doesn't expose burn-rate fields (common for micro-caps) — **never estimated** |
| `news_sentiment` | 5% | VADER compound * 100 | None if zero headlines scanned |

Weights sum to exactly 1.00 (asserted at import time).

**Missing-signal handling (the part I want your read on):** when a signal
returns `None` (e.g. Finnhub has no fundamentals for a micro-cap, or short
interest is unavailable), it's excluded entirely from the composite and its
weight is redistributed proportionally across whatever signals ARE
available — `composite = sum(weight_i * score_i for available) /
sum(available weights)`. This keeps the output meaningfully scaled to
-100..+100 regardless of data gaps, rather than silently shrinking toward
zero as more signals go missing. The alternative (treating missing = 0
contribution without renormalizing) would make thinly-covered micro-caps
systematically score closer to NEUTRAL regardless of their real signal
strength, which felt like the wrong failure mode for a scanner whose whole
job is finding thinly-covered names. Flagging this as a design choice, not
an obvious right answer — open to your take.

Composite -100..+100. Labels: `STRONG_BUY` (≥70), `BUY` (≥40), `NEUTRAL`,
`SELL` (≤-40), `STRONG_SELL` (≤-70). Entry threshold `|score| >= 40` —
higher than High Value's 20, per your spec, since these are lower-liquidity,
lower-conviction names that need more conviction to trade.

`dominant_thesis_type()` picks a single tag for per-thesis calibration:
a news category flag first (most specific/event-driven), then
`INSIDER_BUYING`, then a generic `TECHNICAL_OVERSOLD` bucket when only
price/RSI/volume signals fired with no catalyst.

---

## 6. Runner (`fetchers/low_value_runner.py`)

Independent background thread from `high_value_runner.py` — separate
globals, separate thread name, imports only two read-only time helpers
(`_et_now`/`_et_minutes`) from the frozen High Value runner.

- **8:00-8:14 AM ET daily window** (pre-market, after the news scan can
  reasonably complete). Once per trading day.
- **Entry**: scans the daily universe, computes the thesis score, logs any
  `|composite| >= 40` symbol via `trading_logger.log_low_value_trade()` —
  side is `long` if composite > 0, `short` if < 0. Fixed $25 position via
  `low_value_position_size()`. Stops at max 3 concurrent positions.
- **Exit** (checked same tick, once daily — no intraday checks, no
  3:50 PM force-close, since these are multi-day holds by design):
  1. **Thesis resolved** — entry thesis was a "negative" category
     (`EARNINGS_MISS`/`ANALYST_DOWNGRADE`/`REGULATORY_RISK`/
     `OPERATIONAL_CRISIS`) and a fresh `POSITIVE_CATALYST` flag now appears
     in a 2-day news rescan.
  2. **+50% target** / **-50% stop** from entry (inverse for short side).
  3. **5 trading days elapsed** (weekday-count approximation — no market
     holiday calendar yet, flagged as a known simplification).
- Priority order: target/stop checked before thesis-resolution before time,
  so a big move exits on price even if the news hasn't caught up yet.

---

## 7. Shared Infrastructure — What Changed, What Didn't

- **`intraday_trades` table**: gained `engine` (`'high_value'` default —
  every pre-existing row is unaffected and unreclassified),
  `lv_thesis_type`, `lv_news_flags` (JSON), `lv_news_sentiment`,
  `lv_headline_count`. Migration is idempotent and additive-only
  (`db/database.py:_migrate_intraday_trades`), verified against both a
  fresh DB and a simulated pre-existing DB before committing.
- **New `low_value_universe_snapshot` table** for daily universe auditing.
- **`signal_calibration.py`**: `_load_closed_trades`/`_load_closed_trades_full`
  gained an `engine: str = "high_value"` parameter. Default value means
  every pre-existing caller (dozens of them across `app.py` and elsewhere)
  behaves identically to before — verified with a test that inserts both
  `high_value` and `low_value` rows and confirms each engine's functions
  only ever see its own rows. Low Value doesn't reuse the per-signal
  (`vwap_score`/`or_score`/etc.) machinery — those columns are High Value's
  signal set and don't apply — so it gets its own axis:
  `thesis_type_calibration_report()` (win rate per `lv_thesis_type`) and
  `low_value_calibration_readiness()` (20 trades/thesis-type preliminary,
  50 dynamic-weight-eligible, 100 empirical-sizing-eligible — per your
  spec's looser tiering vs. High Value's 30/50/100 overall-pool gates).
  **Dynamic weights and empirical sizing are NOT wired in yet** — same
  "don't tune what's unmeasured" discipline used throughout this project;
  `low_value_calibration_readiness()` only reports eligibility today.
- **`kelly.py`**: `low_value_position_size()` — fixed $25, no ATR/Kelly
  math at all. `atr_position_size`, `kelly_from_signals`,
  `risk_parity_position_size` (High Value's sizing) are untouched.
- **`trading_logger.py`**: `log_low_value_trade()` parallel to
  `log_hypothetical_trade()`; `log_trade_exit()` is reused completely
  unchanged (operates on a `trade_id`, agnostic to engine).

---

## 8. Endpoints + UI

| Endpoint | Purpose |
|---|---|
| `GET /trading` | Now a hub page — splits into High Value / Low Value |
| `GET /trading/high-value` | High Value UI (moved, content unchanged) |
| `GET /trading/low-value` | New Low Value landing page |
| `GET /trade/low-value/universe` | Today's scanned universe (JSON) |
| `POST /trade/low-value/scan-now` | Manual scan trigger |
| `GET /trade/low-value/runner/status` | Runner state |
| `POST /trade/low-value/runner/start` / `/stop` | Runner control |
| `GET /trade/low-value/calibration` | Per-thesis-type win rate + readiness |
| `GET /trade/low-value/dashboard` | Standalone monitor — universe count, open positions, closed P&L, win rate by thesis type. Never mixes with High Value's `/trade/dashboard`. |

---

## 9. Data Sources

| Source | Used For | Key? | Fails Safe To |
|---|---|---|---|
| Alpaca Data API | Daily bars, asset list, snapshots | Existing keys | `[]` / `{}` |
| Finnhub (`fetchers/finnhub.py`) | News, market cap, fundamentals | `FINNHUB_API_KEY` (free, 60/min) | `None` / `[]` |
| SEC EDGAR (`fetchers/sec_edgar.py`) | Bankruptcy 8-K filings | None (needs a `SEC_EDGAR_USER_AGENT` per SEC's fair-access policy, not auth) | `False` |
| OpenInsider (`fetchers/openinsider.py`) | Insider Form 4 purchases | None | `False` / `[]` |
| Nasdaq public API (`fetchers/finra.py`) | Short interest (~2wk lag, FINRA settlement data) | None | `None` |
| Yahoo Finance (`fetchers/yahoo_quote.py`) | Market cap backup | None | `None` |

Every fetcher here follows the exact same fail-safe convention as
`fred.py`/`market_data.py` elsewhere in this codebase — a missing key or a
request error degrades one signal to "unavailable," never crashes the scan
or fabricates a value.

---

## 10. One Documented Deviation From Your Spec

Your directory diagram showed `trading_logger.py` moving into
`models/trading/shared/`. I kept it in `fetchers/` instead — every other
DB-writing, I/O-heavy module in this codebase (`fred.py`, `alpaca.py`,
`market_data.py`) lives there, and moving just this one file would mix I/O
concerns into `models/` for no functional gain. The *behavioral* ask
(shared logger, engine-tagged rows, both engines writing through one
lifecycle logger) is fully met — only the physical file location differs
from your diagram. Flagging this explicitly in case you had a reason for
that specific placement I'm not seeing.

---

## 11. Testing Done (Sandbox — No Live Network Access)

Since this sandbox can't reach Alpaca/Finnhub/SEC live, everything was
verified with:
- Standalone syntax checks (`ast.parse`) on every new/modified file.
- A real SQLite schema+migration test (fresh DB and simulated
  pre-existing DB) confirming the new columns/table appear correctly and
  idempotently.
- Unit tests on `thesis_tracker.py`'s pure math (RSI, price-vs-low,
  volume spike, sector relative strength, label thresholds,
  missing-signal reweighting, thesis-type priority) against synthetic bar
  data.
- An end-to-end test (stubbed fetchers, real schema, real
  `trading_logger`/`kelly`/`signal_calibration` code) confirming: universe
  scan filters correctly, a beaten-down/oversold/volume-spiking symbol logs
  a $25 `engine='low_value'` hypothetical trade, a +50% price move
  triggers a `TARGET` exit, and the 3-position cap is respected.
- A dedicated engine-isolation test confirming `high_value` and
  `low_value` rows never leak into each other's calibration queries, and
  that every pre-existing calibration function's default behavior
  (no `engine` argument passed) is bit-for-bit unchanged.

No real market data has touched this yet — same standing caveat as every
other section of this project.
