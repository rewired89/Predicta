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

---

## 12. Post-Deploy Bug Fix + Query Box (2026-07-07)

Two issues surfaced once this actually shipped to Railway:

**Bug: "Open Dashboard" hung indefinitely.** `render_low_value_dashboard()`
called `get_daily_universe()` directly to show the universe count. That
function triggers a full live scan (`build_low_value_universe()` — Alpaca
`get_all_active_assets()`, likely thousands of symbols, then per-candidate
Finnhub/SEC/Alpaca calls) on any cache miss, and the in-memory universe
cache is empty after every Railway restart/redeploy. So the dashboard — a
page that should just be a fast read-only monitor — was silently kicking
off a slow, multi-thousand-API-call scan on every cold load. **Fixed**: the
dashboard now reads `universe_size` from the most recently *logged*
`low_value_universe_snapshot` row (a plain DB read) instead of calling
`get_daily_universe()`. A dashboard view should never trigger a live scan —
only the runner's 8 AM tick or an explicit scan-now/query call should. Same
root-cause class as the schema-ordering crash from earlier today: a
read path that accidentally did expensive/fragile work it had no business
doing.

**Added: natural-language query box.** The High Value page has always had
a free-text box ("give me a brief", ticker lookups); Low Value's page only
had static JSON links until now, which wasn't a great daily-use experience
for the engine specifically built for daily personal use. Added an "Ask"
box to `templates/trading_low_value.html` + `POST /trade/low-value/query`:
- **"scan the market" / "any signals" / "check the market"** → live
  `run_low_value_scan()` (can genuinely take a minute or two on a cold
  cache, since it's a full-market scan, not an 8-symbol watchlist — the UI
  says so explicitly while it's working).
- **Everything else** ("brief", "this week", "yesterday", or anything
  unrecognized) → `get_low_value_brief(days)` — a new DB-only read (closed
  trades in the window, win rate, total P&L, open position count, most
  recent universe size). `"yesterday"` sets `days=1`, otherwise `days=7`.
  This path is intentionally never allowed to trigger a scan, so "what
  happened" is always fast regardless of cache state.

Verified with a stubbed test that explicitly asserts `render_low_value_dashboard()`
raises if it ever calls `get_daily_universe()` again (regression guard for
the exact bug above), plus a real-schema test of `get_low_value_brief()`
against inserted universe-snapshot and closed-trade rows.

---

## 13. Round-2 Review Response (2026-07-07)

Kimi's round-2 read-through confirmed the architecture, endorsed the
missing-signal reweighting design, and gave five prioritized follow-ups
(P1/P1/P2/P2/P3). All five are built:

1. **P1 — `lv_missing_signals` logging.** `intraday_trades` gained
   `lv_missing_signals` (JSON list from `compute_thesis_score()`'s
   `missing_signals`) and a new `models.trading.shared.signal_calibration
   .missing_signal_impact_report()` — splits closed trades per signal into
   "missing at entry" vs "present at entry" cohorts and compares win
   rate/avg P&L. Exposed on `GET /trade/low-value/calibration`. Answers
   Kimi's exact question ("did missing `cash_burn_months` trades perform
   worse?") once enough trades exist per cohort (same `min_trades` gate as
   everything else — no faking below threshold).
2. **P1 — Scan-cost confirmation.** The query box now shows a
   `window.confirm()` ("~200 stocks, 2-10 minutes") before firing any
   scan-trigger query, via a client-side trigger list mirroring `app.py`'s
   `_LV_SCAN_TRIGGERS`. Applies uniformly whether typed or clicked from an
   example chip — no path bypasses the warning.
3. **P2 — 7-day Finnhub cache.** New `finnhub_cache` table (added via
   `_migrate_new_tables`, not `schema.sql` — deliberately, see the note
   below) caches `get_company_profile`/`get_basic_financials` for 7 days,
   same pattern as the existing `fbref_cache`. News stays uncached (fetched
   fresh daily, per Kimi's framing of it as "the fast-moving piece"). Cuts
   the ~10-minute cold-scan estimate roughly in half after the first week,
   once most of the universe's cap/fundamentals are cached.
4. **P2 — US market holiday calendar.** `fetchers/low_value_runner.py`
   gained `US_MARKET_HOLIDAYS` (real NYSE holiday dates, 2026-2027 — same
   convention as `MACRO_EVENT_DATES`), and `_trading_days_elapsed()` now
   skips them in addition to weekends. Verified with a synthetic case
   spanning the Jul 3, 2026 Independence Day observance: holiday-aware
   count is one trading day fewer than the old weekday-only approximation.
5. **P3 — `short_interest_as_of_date` logging.** `fetchers/finra.py` now
   exposes `get_short_interest()` returning `{pct, as_of_date}` (the
   settlement date from Nasdaq's response); `get_short_interest_pct()`
   stays as a backward-compatible wrapper. `_score_short_interest()` logs
   `short_interest_as_of` in its detail dict, threaded through to a new
   `lv_short_interest_asof` column. Not a fix — the 2-week lag is expected
   and, per Kimi, "a feature, not a bug" for this thesis — just makes the
   staleness visible instead of implicit.

**On P3's "build `LOW_VALUE_README.md`"**: already existed (created
alongside the initial engine build) — Kimi's summary table assumed it
didn't. No action needed.

**One note on the caching table's placement**: `finnhub_cache` was added
via `db/database.py`'s `_migrate_new_tables()` (Python, `CREATE TABLE IF
NOT EXISTS`), not `schema.sql`. This is deliberate, not an oversight — the
production crash earlier the same day (documented in the main session, not
duplicated here) was caused by an index on a new column living directly in
`schema.sql`, which runs via `executescript()` on every boot *before* the
Python migration that actually adds the column to an existing production
DB. Keeping new tables/columns in the Python migration path (which already
has the idempotent "does this exist yet" checks) avoids that whole class
of bug going forward — `fbref_cache` already followed this convention;
`finnhub_cache` now does too.

Also fixed the same day, unrelated to Kimi's list: Alpaca deprecated
`pattern_day_trader`/`daytrade_count` (FINRA replaced the PDT rule with an
intraday margin framework on 2026-06-04) — `get_account()` and the
Positions & Orders account bar no longer reference them.

---

## 14. Production Bug: Unbounded Scan + Blocking Request Architecture (2026-07-07, same day)

After deploying Section 13's fixes, the user tried the query box's "scan
the market today" and it sat with no result for 20+ minutes, eventually
showing a blank `Error:` with no message. Two compounding bugs, both real:

**Bug A — the scan had no candidate limit.** `get_daily_universe()` called
`build_low_value_universe(today_str)` with no `max_candidates`. The
scanner's `max_candidates` parameter existed and was documented as "a
safety valve against a very large Alpaca universe burning through
Finnhub's/SEC's rate limits" — but it was never actually passed at the one
call site that matters. Section 9's own math ("200 symbols × 1 Finnhub
call = 3.3 min") assumed the candidate pool *was* ~200 — it isn't. The
candidate pool is every US equity that survives the cheap `< $20` price
filter, which is realistically several thousand tickers, not a couple
hundred. Every one of those got the full sequential treatment (Alpaca
daily bars, Finnhub/Yahoo market cap, SEC EDGAR bankruptcy check) before
the 50-200 *output* cap even applied. **Fixed**: `get_daily_universe()` now
passes a new `UNIVERSE_SCAN_MAX_CANDIDATES = 500` constant, bounding the
input side, not just the output.

**Bug B — a multi-minute operation was blocking the HTTP request.** Even
correctly bounded, 500 candidates × up to 3 sequential network calls each
is a real multi-minute operation. Running that synchronously inside a
FastAPI request handler means the response is entirely at the mercy of
Railway's proxy timeout (or the browser's) — which is exactly what
produced the blank `Error:` after a long silent wait: the connection got
killed server-side with no useful error payload to relay. **Fixed**: all
three manual-trigger paths (`POST /trade/low-value/scan-now`, the query
box's scan mode, and `GET /trade/low-value/universe?force_refresh=true`)
now use fire-and-forget triggers (`trigger_scan_async()` /
`trigger_universe_refresh_async()`) — a background thread does the real
work, the HTTP response returns in milliseconds with a `"started"` status,
and a `threading.Lock`-guarded flag (`_scan_in_progress` /
`_universe_build_in_progress`) rejects a duplicate concurrent trigger
instead of racing. `get_runner_status()` now exposes
`scan_in_progress`/`last_scan_started_at`/`last_scan_completed_at`/
`last_scan_trade_ids`/`last_scan_error`, and the dashboard shows an amber
"Scan In Progress" or red "Last Scan Failed" banner built from that same
status — checking on a background scan no longer requires reading raw
JSON. The scheduled 8 AM runner tick is unaffected — it already ran
`run_low_value_scan()` inside its own background thread, so it was never
part of this bug; only the manual/query-triggered paths needed the fix.

**Verification**: a stubbed test with an artificially slow `get_all_active_assets()`
confirms `trigger_scan_async()` returns in under 100ms regardless of how
long the underlying scan takes, that `scan_in_progress` is visible while
it runs, that a second trigger during that window correctly returns
`already_running` instead of starting a duplicate scan, and that the
status fields update correctly once the background scan completes.

**Open question for you**: `UNIVERSE_SCAN_MAX_CANDIDATES = 500` is a guess
at the right tradeoff — high enough to still reach something close to the
50-200 target universe size after the volume/cap/bankruptcy filters, low
enough to keep a real scan's wall-clock time reasonable. We don't have
real data yet on what fraction of sub-$20 candidates actually pass every
filter, so this number may need tuning once a few real scans complete and
we can see the actual candidate-to-output survival rate.

---

## 15. Volatility Floor (2026-07-07, Kimi's proposal — built as specified)

Built exactly to spec, with one implementation optimization:

- **`VOLATILITY_FLOOR_ENABLED`**, **`VOLATILITY_FLOOR_MIN_DAY_MOVE_PCT`
  (0.02)**, **`VOLATILITY_FLOOR_MIN_5D_RANGE_PCT`(0.05)** — module
  constants in `scanner.py`, per your naming preference.
- **`_has_meaningful_volatility(daily_bars)`** — True (passes) if at least
  one of the last 5 daily bars had a `|close-open|/open >= 2%` move, OR the
  5-day high-low range is `>= 5%` of the 5-day-ago open. A candidate is
  only excluded when BOTH conditions miss, matching your OR-logic exactly.
  Fails open (True, don't exclude) when fewer than 5 bars of history exist
  — same "insufficient data isn't evidence of X" convention as every other
  signal in this engine.
- **Ordering**: runs after the free earnings-blackout check (zero cost,
  no reason not to check it first) and before the volume/market-cap/SEC
  checks — same effect as your "step 1.5" placement: it cuts the candidate
  pool before any paid API call.
- **One deliberate deviation from the literal spec**: rather than a
  separate `get_daily_bars` call for the volatility check (as your example
  code implied), `build_low_value_universe` fetches the 20-day bars once
  per candidate and reuses that same list for both `_has_meaningful_volatility`
  (last 5 bars) and the existing volume check (`_avg_daily_volume_20d`,
  refactored to take bars instead of a symbol) — one Alpaca call instead of
  two. Same outcome, fewer API calls, which matters given the same-day
  scan-cost problem in Section 14.
- **`stagnant_filtered_count`** — logged in `low_value_universe_snapshot.filter_stats_json`
  (new column, migrated onto the existing table via
  `db/database.py:_migrate_low_value_universe_snapshot`) and shown on the
  dashboard, per your ask.

Verified against your own four scenarios: a flat stock (±0.5% daily, tight
range) is excluded; a falling-knife day (-17% single day) passes via the
single-day-move condition; a gradual bleed with no single dramatic day but
a wide 5-day range passes via the range condition; a candidate with fewer
than 5 bars of history fails open rather than being excluded. End-to-end
integration test confirms `build_low_value_universe` returns `(universe,
stats)`, `get_daily_universe` unpacks it correctly and its own external
contract (`list[str]`) is unchanged, and the logged snapshot row's
`filter_stats_json` matches the actual excluded count.

---

## 16. Production Crash: `get_daily_bars` Returns `None`, Not `[]` (2026-07-08)

First real "scan the market" attempt after Section 15 shipped crashed
immediately — the dashboard showed "Last Scan Failed: object of type
'NoneType' has no len()", with 0 symbols scanned and 0 excluded.

**Root cause**: `fetchers/alpaca.py`'s `get_daily_bars()` (and `get_bars()`,
same bug) did `return data.get("bars", [])`. That default only applies
when the `"bars"` key is *missing* from Alpaca's response — but Alpaca
sometimes returns `{"bars": null, ...}` explicitly, for a symbol with no
data in the requested date range. `.get("bars", [])` on a dict that
literally contains `"bars": None` returns `None`, not `[]`. Section 15's
new volatility-floor code called `_has_meaningful_volatility(daily_bars)`,
which did `len(daily_bars) < 5` with no None-guard — `len(None)` is
exactly `"object of type 'NoneType' has no len()"`.

**Why High Value never hit this**: its watchlist is a fixed set of liquid
large-caps (`RUNNER_SYMBOLS`) that always have bar data. Low Value's
scanner evaluates a much wider pool of obscure sub-$20 stocks — thin
listings, recent IPOs, illiquid names — where Alpaca returning no data for
the requested window is common, not an edge case. This is also why it
only surfaced now: `get_daily_bars`/`get_bars` have been in the codebase
since before this session, but every prior caller either happened to only
query liquid symbols (High Value) or already guarded the return value
before using it (e.g. `_avg_daily_volume_20d`'s `if not daily_bars: return
0.0` — `not None` is `True`, so that path was always safe). Section 15's
new code was the first caller to use a fetched-once `daily_bars` value
directly without that guard.

**Fixed at three layers** (defense in depth, not redundancy for its own
sake — each catches a different future mistake):
1. **Root cause**: `get_daily_bars`/`get_bars` now do `data.get("bars") or
   []` — never returns `None` regardless of whether Alpaca omits the key
   or sends it as `null`. This protects every current and future caller,
   not just the volatility floor.
2. **Scanner-level**: `build_low_value_universe` now does `if not
   daily_bars: continue` right after fetching — a candidate with no bar
   data can't be evaluated for volatility or volume anyway, so it's
   skipped cleanly instead of relying solely on the fix above.
3. **Function-level**: `_has_meaningful_volatility`'s guard changed from
   `len(daily_bars) < 5` to `not daily_bars or len(daily_bars) < 5`.

Verified two ways: a direct test confirms `get_daily_bars`/`get_bars`
return `[]` (not `None`) when fed Alpaca's exact `{"bars": null}` response
shape, and a full scanner regression test stubs `get_daily_bars` to return
`None` (the pre-fix real-world behavior) and confirms
`build_low_value_universe` now completes cleanly instead of crashing.

---

## 17. Scan Ran 5+ Minutes With Zero Result, No Visibility (2026-07-11)

After Section 16's crash fix deployed, a live "scan the market" attempt
still produced nothing — no crash this time, just silence. Runner status
showed `scan_in_progress: false, last_scan_started_at: null` even though
the query box had just reported `{"status": "started", ...}` a few minutes
earlier.

**Diagnosis process, since we have no direct server access**: rather than
guess, walked the user through hitting `POST /trade/low-value/query`
directly (bypassing the browser frontend entirely) via curl/PowerShell.
That confirmed the trigger itself works correctly — first call returned
`{"status": "started"}`, an immediate second call correctly returned
`{"status": "already_running"}` (proving both `trigger_scan_async` and its
duplicate-scan lock work as designed). So the bug isn't in the
trigger/lock mechanism — it's that the background scan itself either never
finishes or the state gets reset before it does, and we have zero logging
to tell which.

**Root problem found on inspection, not from a log (we don't have log
access)**: `build_low_value_universe` and `_cheap_price_filter` had no
wall-clock time limit anywhere, and *no progress logging at all* — a
completely silent multi-minute (or multi-hour, in the worst case) loop.
Combined with `get_all_active_assets()` potentially returning several
thousand candidates and `_cheap_price_filter` making up to ~50 sequential
batched Alpaca snapshot calls before the per-symbol loop even starts, this
pipeline has no circuit breaker if *any* dependency in the chain (Alpaca's
snapshot endpoint on a large batch, or — most likely suspect — Finnhub,
SEC EDGAR's ticker-map download, or Nasdaq's short-interest endpoint being
slow/throttled from Railway's datacenter IP, the exact same failure class
already documented in this codebase for FanGraphs/Baseball Savant) is
slower than expected. It just keeps going, invisibly.

**Fixed**: two independent wall-clock budgets — `SCAN_TIME_BUDGET_SEC`
(240s) bounds the whole `build_low_value_universe` call,
`PRICE_FILTER_TIME_BUDGET_SEC` (60s) bounds just the price-filter pass.
Either one tripping returns whatever was found so far
(`time_budget_exceeded: True` in the stats dict) instead of continuing
indefinitely. Added progress logging (`[LOW_VALUE_SCANNER]` prefix) every
10 price-filter batches and every 25 candidates evaluated, plus start/end
markers — so the *next* time this happens, Railway's runtime logs will
show exactly where time is going, which we had zero visibility into this
round. `elapsed_sec`/`candidates_evaluated`/`time_budget_exceeded` are now
also logged to `low_value_universe_snapshot` and shown on the dashboard,
so this is visible without needing log access at all.

**Honest state of this fix**: it guarantees the scan always terminates and
gives visibility into timing, but does NOT identify or fix whichever
dependency is actually slow — that diagnosis needs either Railway log
access (which we don't have in this session) or a completed scan's timing
breakdown to point at the specific stage. If the next attempt still
returns 0 symbols but now shows `time_budget_exceeded: True` with a low
`candidates_evaluated` count, that pins the slowness to the *price filter*
stage (Alpaca). If `candidates_evaluated` is high but the universe is
still empty, the filters themselves (volume/cap/volatility/bankruptcy) are
the more likely culprit, not a slow dependency — a different investigation
entirely.
