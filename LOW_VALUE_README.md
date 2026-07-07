# Low Value Trades — Contrarian Sub-$20 Engine

Built per Kimi's round-6 follow-up review. A completely independent trading
engine from High Value (the existing intraday/swing model) — shares
infrastructure (logging, calibration, position-sizing helpers) but zero
signal logic, zero shared weights, zero shared thresholds.

**Purpose:** daily personal-use scanning of beaten-down sub-$20 stocks for a
contrarian "buy the fear" thesis — oversold price action + insider buying +
positive catalysts, sized small ($25/trade) so a bad pick never does real
damage. High Value stays frozen, documented, and untouched for its own
(separate) purpose.

---

## Directory Structure

```
models/trading/
  high_value/          # signals.py, intraday.py, ngram.py — moved, unmodified logic
  low_value/            # scanner.py, news_overlay.py, thesis_tracker.py — new
  shared/                # kelly.py, signal_calibration.py — moved, extended (engine param)
  pairs.py, screener.py  # unchanged location; imports updated to high_value/shared paths

fetchers/
  high_value_runner.py   # renamed from paper_runner.py — frozen High Value runner
  low_value_runner.py    # new — daily Low Value runner
  low_value_dashboard.py # new — separate dashboard renderer
  trading_logger.py       # extended: engine column, log_low_value_trade, universe snapshot logging
  finnhub.py, sec_edgar.py, openinsider.py, finra.py, yahoo_quote.py  # new data-source fetchers

templates/
  trading_hub.html        # new — "Stock Market" landing page, splits into the two engines
  trading_high_value.html # renamed from trading.html
  trading_low_value.html  # new
```

**Deviation from the original spec's file layout:** `trading_logger.py`
stays in `fetchers/` rather than moving to `models/trading/shared/`. Every
other I/O-and-DB-writing module in this codebase (`fred.py`, `alpaca.py`,
`market_data.py`, ...) lives in `fetchers/`, and `trading_logger.py` writes
directly to SQLite — moving it would mix I/O concerns into `models/` for no
functional benefit. The `engine` column + Low Value logging functions were
added there instead, satisfying the *behavioral* requirement (shared logger,
engine-tagged rows) without an architecturally awkward relocation.

---

## Pipeline

1. **Universe scanner** (`models/trading/low_value/scanner.py`,
   `build_low_value_universe`) — runs once daily (cached in-memory per day):
   - `fetchers.alpaca.get_all_active_assets()` — all active, tradable,
     non-OTC US equities.
   - Cheap first-pass filter: batch snapshots, keep `last_close < $20`.
   - Per-candidate: `avg_daily_volume_20d > 100k` (Alpaca daily bars),
     `market_cap > $50M` (Finnhub primary, Yahoo Finance backup),
     no earnings-blackout match (`fetchers.high_value_runner.EARNINGS_BLACKOUT`
     — read-only import from the frozen High Value runner), no 8-K Item 1.03
     bankruptcy filing in the last 90 days (`fetchers.sec_edgar`).
   - Output capped to 200 symbols; logged to `low_value_universe_snapshot`
     via `trading_logger.log_universe_snapshot`.

2. **News overlay** (`models/trading/low_value/news_overlay.py`) —
   `scan_universe_news()` batches Finnhub company-news (7-day window,
   rate-limited to 60 calls/min via `fetchers.finnhub.fetch_news_batch`),
   flags headline categories (`EARNINGS_MISS`, `ANALYST_DOWNGRADE`,
   `REGULATORY_RISK`, `OPERATIONAL_CRISIS`, `POSITIVE_CATALYST`) via
   case-insensitive keyword match, and scores VADER sentiment on article
   summaries. `INSIDER_BUYING` is a separate SEC/OpenInsider fetch, not a
   headline flag, matching the spec.

3. **Signal engine** (`models/trading/low_value/thesis_tracker.py`,
   `compute_thesis_score`) — 8 signals from DAILY bars only, weighted
   `price_vs_20d_low 20% / rsi_14 15% / volume_spike 10% / insider_buying_30d
   20% / short_interest_pct 10% / sector_relative_strength 10% /
   cash_burn_months 10% / news_sentiment 5%`. Composite -100..+100, label
   STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL, entry threshold `|score| >= 40`.
   **Missing signals are excluded from the weighted average and their weight
   redistributed proportionally — never faked as a default value**, same
   "no faking" discipline as every calibration gate in this codebase.
   No regime-conditioning from High Value: contrarian polarity is the
   opposite of a trend-following engine (near a 20-day low is bullish here,
   not bearish), so the two engines' scoring philosophies could not share
   logic even if they shared a file.

4. **Runner** (`fetchers/low_value_runner.py`) — daily background thread,
   8:00-8:14 AM ET scan window (after the news scan). Logs any
   `|composite| >= 40` symbol via `trading_logger.log_low_value_trade`
   (`engine='low_value'`, fixed $25 sizing via
   `models.trading.shared.kelly.low_value_position_size`, max 3 concurrent
   positions). Same tick, checks open positions for exit: thesis resolved
   (a `NEGATIVE_THESES` entry followed by a fresh `POSITIVE_CATALYST` flag),
   `+50%` target, `-50%` stop, or 5 trading days elapsed. No intraday
   checks, no 3:50 PM force-close — those are High Value concepts that don't
   apply to a multi-day hold.

5. **Dashboard** (`fetchers/low_value_dashboard.py`, `GET
   /trade/low-value/dashboard`) — today's universe count, open positions,
   closed P&L, win rate by thesis type. Completely separate page from
   `/trade/dashboard` (High Value). `GET /trading` now lands on a hub page
   (`templates/trading_hub.html`) with two cards — High Value
   (`/trading/high-value`) and Low Value (`/trading/low-value`).

---

## Shared Infrastructure Changes

- **`db/schema.sql` / `db/database.py`**: `intraday_trades` gained `engine`
  (`'high_value'` default — every pre-existing row is unaffected),
  `lv_thesis_type`, `lv_news_flags`, `lv_news_sentiment`, `lv_headline_count`.
  New `low_value_universe_snapshot` table. Migration is idempotent
  (`_migrate_intraday_trades` in `db/database.py`) and additive-only.
- **`fetchers/trading_logger.py`**: `log_low_value_trade()` (parallel to
  `log_hypothetical_trade`, engine-tagged), `log_universe_snapshot()` /
  `get_universe_snapshots()`. `log_trade_exit()` is unchanged and reused
  as-is — it operates on a `trade_id`, agnostic to which engine logged it.
- **`models/trading/shared/kelly.py`**: `low_value_position_size()` — fixed
  $25/trade, no ATR/Kelly math. `atr_position_size`, `kelly_from_signals`,
  `risk_parity_position_size` untouched.
- **`models/trading/shared/signal_calibration.py`**: `_load_closed_trades` /
  `_load_closed_trades_full` gained an `engine: str = "high_value"`
  parameter (default preserves every existing caller's behavior bit-for-bit).
  Low Value's calibration doesn't reuse the per-signal (`_SIGNAL_COLS`)
  machinery — its 8 signals don't map onto High Value's vwap/or/rsi/etc.
  columns — so it gets its own axis: `thesis_type_calibration_report()`
  (per `lv_thesis_type` win rate) and `low_value_calibration_readiness()`
  (20 trades/thesis-type preliminary, 50 dynamic-weight, 100 empirical-sizing
  — looser tiers than High Value's 30/50/100 overall-pool thresholds, per spec).

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /trading` | Stock Market hub — links to both engines |
| `GET /trading/high-value` | High Value UI (renamed from `/trading`) |
| `GET /trading/low-value` | Low Value UI |
| `GET /trade/low-value/universe` | Today's scanned universe (JSON) |
| `POST /trade/low-value/scan-now` | Manual scan trigger |
| `GET /trade/low-value/runner/status` | Runner state |
| `POST /trade/low-value/runner/start` / `/stop` | Runner control |
| `GET /trade/low-value/calibration` | Per-thesis-type win rate + readiness |
| `GET /trade/low-value/dashboard` | Plain-English monitor |

---

## Data Sources

| Source | Used For | Key Required |
|---|---|---|
| Alpaca Data API | Daily bars, asset list, snapshots | Existing `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` |
| Finnhub (`fetchers/finnhub.py`) | News, market cap, basic fundamentals | `FINNHUB_API_KEY` (free, 60 calls/min) |
| SEC EDGAR (`fetchers/sec_edgar.py`) | Bankruptcy 8-K (Item 1.03) filings | None — needs `SEC_EDGAR_USER_AGENT` (fair-access policy, not auth) |
| OpenInsider (`fetchers/openinsider.py`) | Insider Form 4 purchases | None |
| Nasdaq public API (`fetchers/finra.py`) | Short interest (FINRA settlement data, ~2wk lag) | None |
| Yahoo Finance (`fetchers/yahoo_quote.py`) | Market cap backup | None |

All fetchers fail safe (return `None`/`[]`/`False`) on a missing key or
request error — a data-source hiccup degrades a signal to "unavailable,"
never crashes the scan or fabricates a value.

---

## Validation Gates (same discipline as High Value)

- 0 trades: every calibration function returns `None`/empty, no faking.
- 20 trades per thesis type: preliminary win-rate read
  (`LOW_VALUE_THESIS_PRELIMINARY_MIN_TRADES`).
- 50 trades: dynamic weight recalibration becomes eligible
  (`LOW_VALUE_DYNAMIC_WEIGHT_MIN_TRADES`) — not yet wired to
  `SIGNAL_WEIGHTS`, same "don't tune pre-data" principle used everywhere
  else in this codebase; `low_value_calibration_readiness()` just reports
  eligibility today.
- 100 trades: empirical position sizing becomes usable
  (`LOW_VALUE_EMPIRICAL_SIZING_MIN_TRADES`) — sizing stays fixed-$25 until
  then, same story.
- Never falls back to synthetic heuristics: every missing signal is
  excluded from the weighted composite, never estimated.

---

## What Was NOT Touched

`models/trading/high_value/signals.py`, `intraday.py`, `ngram.py`;
`fetchers/high_value_runner.py`'s scan/exit/regime logic; `WEIGHTS` in any
High Value file; any pre-existing `intraday_trades` row (all default to
`engine='high_value'`); High Value's dashboard, calibration thresholds, or
kill-switch logic.
