# Predicta — CodeMap
> Auto-generated index of every function, class, and key variable.
> **Protocol**: Read this before touching any code. Update the relevant entry in the same commit as any code change.

---

## ai_client.py

---
name: get_client
type: function
file: ai_client.py
purpose: Shared Anthropic client factory. Tries ANTHROPIC_API_KEY first (local .env / CI), then falls back to CLAUDE_SESSION_INGRESS_TOKEN_FILE bearer token (Claude Code remote sessions). Raises RuntimeError if neither is available.
inputs: none
outputs: anthropic.Anthropic
calls: anthropic.Anthropic
called_by: _client (ai_agent.py, ai_agent_baseball.py, ai_agent_tennis.py, ai_agent_table_tennis.py, ai_agent_trading.py)
mutates: none
---

---

## db/database.py

---
name: DB_PATH
type: variable
file: db/database.py
purpose: Path to the SQLite database file. Reads PREDICTA_DB_PATH env var first — set this on Railway to a volume path (e.g. /data/predicta.db) so data survives redeploys. Falls back to predicta.db at project root for local dev.
inputs: none
outputs: pathlib.Path
calls: none
called_by: init_db, get_db
mutates: none
---

---
name: SCHEMA_PATH
type: variable
file: db/database.py
purpose: Absolute path to the SQL schema file used to initialize the database.
inputs: none
outputs: pathlib.Path
calls: none
called_by: init_db
mutates: none
---

---
name: init_db
type: function
file: db/database.py
purpose: Initialize the SQLite database by executing schema.sql, running all idempotent migrations, and creating any missing indexes. Creates the parent directory first (supports Railway volume mounts). Called on every app startup.
inputs: db_path: Path = DB_PATH
outputs: none
calls: SCHEMA_PATH, _migrate_sport_check, _repair_matches_fk, _migrate_intraday_trades, _migrate_new_tables
called_by: startup (app.py)
mutates: predicta.db (creates/migrates tables and indexes)
---

---
name: get_db
type: function
file: db/database.py
purpose: Context manager that yields an open SQLite connection with Row factory and foreign keys on; commits on exit or rolls back on exception.
inputs: db_path: Path = DB_PATH
outputs: yields sqlite3.Connection
calls: sqlite3.connect
called_by: EloModel, Glicko2Model, predict_match, record_outcome, _market_consensus, log_signal, get_signals_for_match, fetch_signals_for_match, log_manual_odds, fetch_odds_snapshot, compute_metrics_from_db, generate_html_report, create_match, list_matches, get_match, add_signal, get_signals, list_predictions, get_odds, add_outcome, list_orders (app.py), _gather_training_data (ml_layer.py), log_trade_entry/log_trade_exit/get_trade_stats (trading_logger.py)
mutates: predicta.db
---

---

## db/schema.sql

---
name: intraday_trades
type: table
file: db/schema.sql
purpose: Two-phase trade record supporting real, paper, and hypothetical (signal-only) trades. entry inserted by log_trade_entry/log_hypothetical_trade; exit columns filled by log_trade_exit. is_hypothetical=1 marks signal-only records. v3 adds: target1_price, theoretical_entry, liquidity_label, intraday_vol, relative_volume, model_version, is_hypothetical, notes. v4 adds: composite_raw, vwap_score, or_score, rsi_score, relvol_score, gap_score, trend_score, bollinger_score, volsurge_score, ngram_signal, ngram_confidence for calibration feedback loop. v5 (Kimi review) adds: spy_gap_pct, xlk_change_pct, market_regime — market regime tags for post-hoc analysis of which conditions produced which outcomes, logged only via paper_runner.py's _fetch_market_regime, never fed back into live scoring. adjusted_pnl subtracts half-spread cost on both legs for conservative live estimate.
inputs: none (DDL)
outputs: none (DDL)
calls: none
called_by: log_trade_entry, log_hypothetical_trade, log_trade_exit, get_trade_stats (trading_logger.py), _load_closed_trades_full (signal_calibration.py)
mutates: none (DDL)
---

---
name: suspended_pairs
type: table
file: db/schema.sql
purpose: Pairs suspended from the weekly find_all_pairs scan. Each row carries sym1, sym2, reason, suspended_at (auto now), and optional reinstate_after (ISO datetime UTC). get_suspended_pairs() filters to rows where reinstate_after IS NULL or in the future. auto_cull_pairs() writes here with reinstate_after = now+90d.
inputs: none (DDL)
outputs: none (DDL)
calls: none
called_by: get_suspended_pairs, suspend_pair, reinstate_pair, auto_cull_pairs (pairs.py)
mutates: none (DDL)
---

---

## db/database.py (migrations)

---
name: _migrate_new_tables
type: function
file: db/database.py
purpose: Idempotent migration for tables added after initial deployment. Currently ensures suspended_pairs table exists (CREATE TABLE IF NOT EXISTS) with its index. Called by init_db after _migrate_intraday_trades.
inputs: conn: sqlite3.Connection
outputs: none
calls: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS
called_by: init_db
mutates: predicta.db schema
---

---
name: _migrate_intraday_trades
type: function
file: db/database.py
purpose: Idempotent migration for intraday_trades — runs on every startup, adds any missing columns from master list covering v2 (lifecycle + sizing), v3 (hypothetical mode + signal context), and v4 (per-signal scores: composite_raw, vwap/or/rsi/relvol/gap/trend/bollinger/volsurge scores, ngram_signal, ngram_confidence). For empty tables, drops and recreates clean. Never destroys rows. idx_trades_hypo created here (not in schema.sql) since is_hypothetical may not exist in old tables.
inputs: conn: sqlite3.Connection
outputs: none
calls: PRAGMA table_info, ALTER TABLE, DROP TABLE, CREATE INDEX
called_by: init_db
mutates: predicta.db schema
---

---

## fetchers/trading_logger.py

---
name: log_trade_entry
type: function
file: fetchers/trading_logger.py
purpose: Inserts an open trade record at entry time. exit_time and exit_price remain NULL until log_trade_exit is called. Captures: symbol, side, entry_price, qty, position_value, entry_score, time_of_day_label, spread_pct_at_entry, stop_price, target_price, risk_dollars, alpaca_order_id. Returns trade_id (int).
inputs: symbol, side, entry_price, qty, position_value, entry_score, time_of_day_label, spread_pct, planned_hold_bars, stop_price, target_price, risk_dollars, alpaca_order_id=None, entry_time=None
outputs: int (trade_id)
calls: db.database.get_db
called_by: smart_trade (app.py)
mutates: intraday_trades table (INSERT)
---

---
name: log_trade_exit
type: function
file: fetchers/trading_logger.py
purpose: Updates an open trade record with exit data and computes: pnl_dollars (exit-entry × qty), pnl_pct, pnl_r (P&L in R multiples vs risk_dollars), adjusted_pnl (pnl_dollars minus half-spread cost × position_value × 2 legs — conservative live estimate since paper fills ignore spread).
inputs: trade_id, exit_price, exit_reason, actual_hold_bars=None, slippage_exit=0.0, exit_time=None
outputs: dict {trade_id, exit_price, exit_reason, pnl_dollars, pnl_pct, pnl_r, adjusted_pnl, slippage_cost}
calls: db.database.get_db
called_by: sync_trade_exits (app.py), check_and_close_positions (paper_runner.py)
mutates: intraday_trades table (UPDATE)
---

---
name: get_trade_stats
type: function
file: fetchers/trading_logger.py
purpose: Aggregated paper trading performance for last N days (closed trades only). Returns: win rate, total P&L, adjusted P&L, avg P&L/trade, avg R, breakdown by time-of-day label and exit reason, recent 10 trades. is_hypothetical flag included in recent_trades for comparison.
inputs: days: int = 7
outputs: dict {period_days, total_trades, win_rate, total_pnl, adjusted_pnl, avg_pnl_per_trade, avg_r, by_time_of_day, by_exit_reason, recent_trades}
calls: db.database.get_db
called_by: trade_performance (app.py)
mutates: none
---

---
name: log_hypothetical_trade
type: function
file: fetchers/trading_logger.py
purpose: Logs what WOULD have happened without placing an order — signal-only dry-run mode for Week 1-2 validation. Sets is_hypothetical=1, theoretical_entry=entry. Also stores per-signal scores (v4: composite_raw, vwap_score, or_score, rsi_score, relvol_score, gap_score, trend_score, bollinger_score, volsurge_score, ngram_signal, ngram_confidence) via _extract_signal_scores(). v5 (Kimi review): optional regime_tags dict (spy_gap_pct, xlk_change_pct, regime) stored for post-hoc analysis of which market conditions produced which outcomes — logged only, never fed back into live scoring. Outcomes resolved later via log_trade_exit.
inputs: symbol, side, score_value, signals: dict (full compute_intraday_signals result), levels: dict, hold_bars=6, model_version="v3", regime_tags: Optional[dict] = None
outputs: int (trade_id)
calls: db.database.get_db, _extract_signal_scores
called_by: signal_only (app.py), run_open_scan (paper_runner.py)
mutates: intraday_trades table (INSERT with is_hypothetical=1, all v4 signal score cols, v5 regime tag cols)
---

---

## fetchers/paper_runner.py

---
name: RUNNER_SYMBOLS
type: variable
file: fetchers/paper_runner.py
purpose: Default watchlist for the automated paper runner — single-tier large-cap liquid names (AAPL, MSFT, NVDA, AMD, AMZN, META, GOOGL, TSLA) per Kimi's recommendation to avoid mixing volatility regimes in the first 30-40 calibration trades.
inputs: none
outputs: list[str]
calls: none
called_by: run_open_scan, _runner_loop, get_runner_status
mutates: none
---

---
name: RUNNER_MIN_SCORE
type: variable
file: fetchers/paper_runner.py
purpose: Minimum abs(score) threshold for logging a hypothetical trade during automated scans. Set to 20 (wide net) so weak signals are captured for calibration; the trading endpoint action threshold is 40. Overridden to REGIME_EXTREME_MIN_SCORE (60) for the current scan when _fetch_market_regime() reports EXTREME.
inputs: none
outputs: int
calls: none
called_by: run_open_scan, _runner_loop, get_runner_status
mutates: none
---

---
name: MAX_CONCURRENT_POSITIONS
type: variable
file: fetchers/paper_runner.py
purpose: Portfolio risk cap (Kimi review) — max simultaneous open hypothetical positions across the whole 8-symbol universe. Since all 8 symbols are a single correlated tech cluster (not diversified sectors), caps total exposure at 3x1% risk instead of computing pairwise correlation. run_open_scan stops logging new entries once (already-open + logged-this-scan) reaches this cap.
inputs: none
outputs: int (3)
calls: none
called_by: run_open_scan
mutates: none
---

---
name: REGIME_GAP_THRESHOLD_PCT
type: variable
file: fetchers/paper_runner.py
purpose: SPY overnight gap % threshold (2.0) above which _fetch_market_regime() classifies the day as EXTREME. Used as a volatility-regime proxy since VIX isn't available on the Alpaca free tier (Kimi review, structural gap E).
inputs: none
outputs: float (2.0)
calls: none
called_by: _fetch_market_regime
mutates: none
---

---
name: REGIME_EXTREME_MIN_SCORE
type: variable
file: fetchers/paper_runner.py
purpose: Elevated min_score (60) applied by run_open_scan on EXTREME-regime days, replacing RUNNER_MIN_SCORE (20) so the paper-trading dataset isn't contaminated with signals fired during untradeable volatility.
inputs: none
outputs: int (60)
calls: none
called_by: run_open_scan
mutates: none
---

---
name: EARNINGS_BLACKOUT
type: variable
file: fetchers/paper_runner.py
purpose: Static earnings-blackout calendar (Kimi review, structural gap D — low priority). Empty dict by default; format is {"SYMBOL": ["YYYY-MM-DD", ...]}. No live earnings-calendar API is wired in — dates must be added manually as they're confirmed. Mechanism only, not a populated calendar.
inputs: none
outputs: dict[str, list[str]]
calls: none
called_by: _is_earnings_blackout
mutates: none
---

---
name: _fetch_market_regime
type: function
file: fetchers/paper_runner.py
purpose: Coarse "should we even be trading today" gate (Kimi review, structural gap E). Fetches SPY + XLK snapshots in one batch call; computes SPY's overnight gap % (open vs prev_close) as a volatility-regime proxy and XLK's daily change % as a sector-rotation tag for the all-tech watchlist. Classifies EXTREME if abs(spy_gap_pct) >= REGIME_GAP_THRESHOLD_PCT. Fails safe to NORMAL/0.0 on any API error.
inputs: none
outputs: dict {regime: "NORMAL"|"EXTREME", spy_gap_pct: float, xlk_change_pct: float}
calls: get_snapshots
called_by: run_open_scan
mutates: none
---

---
name: _is_earnings_blackout
type: function
file: fetchers/paper_runner.py
purpose: Returns True if symbol has a manually-confirmed earnings date matching date_str in EARNINGS_BLACKOUT.
inputs: symbol: str, date_str: str
outputs: bool
calls: EARNINGS_BLACKOUT
called_by: run_open_scan
mutates: none
---

---
name: _HOLD_BARS
type: variable
file: fetchers/paper_runner.py
purpose: Hold duration in 5-min bars per time-of-day label. MORNING_TREND/AFTERNOON_TREND=12 (60 min), CLOSE_REVERSAL/OPEN_NOISE/LUNCH_CHOP=6 (30 min).
inputs: none
outputs: dict[str, int]
calls: none
called_by: run_open_scan
mutates: none
---

---
name: _DEFAULT_HOLD_BARS
type: variable
file: fetchers/paper_runner.py
purpose: Fallback hold duration (6 bars = 30 min) when time_label is not in _HOLD_BARS.
inputs: none
outputs: int
calls: none
called_by: run_open_scan, _check_exit
mutates: none
---

---
name: _et_now
type: function
file: fetchers/paper_runner.py
purpose: Returns current datetime in US/Eastern timezone. Uses zoneinfo (DST-aware) when available; falls back to UTC-4/UTC-5 offset approximation based on month.
inputs: none
outputs: datetime (ET-aware)
calls: none
called_by: _in_scan_window, _near_close, _is_market_open, _et_minutes, run_open_scan, check_and_close_positions, get_runner_status, _runner_loop
mutates: none
---

---
name: _et_minutes
type: function
file: fetchers/paper_runner.py
purpose: Current ET time expressed as minutes since midnight (for threshold comparisons like 9:30=570, 16:00=960).
inputs: none
outputs: int
calls: _et_now
called_by: _is_market_open, _in_scan_window, _near_close
mutates: none
---

---
name: _is_market_open
type: function
file: fetchers/paper_runner.py
purpose: Returns True if current ET time is within NYSE regular session (Mon-Fri 09:30-16:00).
inputs: none
outputs: bool
calls: _et_now, _et_minutes
called_by: _runner_loop, get_runner_status
mutates: none
---

---
name: _in_scan_window
type: function
file: fetchers/paper_runner.py
purpose: Returns True between 09:35-09:59 ET on weekdays — the morning scan window (5 min after open to let price discovery settle).
inputs: none
outputs: bool
calls: _et_now, _et_minutes
called_by: _runner_loop
mutates: none
---

---
name: _near_close
type: function
file: fetchers/paper_runner.py
purpose: Returns True at or after 15:50 ET — triggers end-of-day force-close of all open positions.
inputs: none
outputs: bool
calls: _et_minutes
called_by: _runner_loop
mutates: none
---

---
name: _avg_daily_vol
type: function
file: fetchers/paper_runner.py
purpose: Compute 20-day average daily volume from daily bars. Returns 1,000,000 if bars are empty (safe default for relative volume calculation).
inputs: daily_bars: list[dict]
outputs: float
calls: none
called_by: run_open_scan
mutates: none
---

---
name: run_open_scan
type: function
file: fetchers/paper_runner.py
purpose: Run intraday signal computation on all configured symbols. For each symbol where abs(score) >= effective min_score, calls log_hypothetical_trade() to persist the signal as a hypothetical trade. Uses batch snapshots + per-symbol throttling to stay under Alpaca free-tier rate limits. Three pre-trade gates applied (Kimi review): (1) market regime gate — calls _fetch_market_regime() once per scan; on EXTREME days raises effective min_score to REGIME_EXTREME_MIN_SCORE (60); (2) earnings blackout — skips symbols matching EARNINGS_BLACKOUT for today via _is_earnings_blackout; (3) portfolio cap — stops logging once (already-open + logged-this-scan) reaches MAX_CONCURRENT_POSITIONS (3). Passes regime_tags (spy_gap_pct, xlk_change_pct, regime) into log_hypothetical_trade for every entry. Returns list of trade_ids created.
inputs: symbols: Optional[list[str]] = None, min_score: int = RUNNER_MIN_SCORE
outputs: list[int] (trade_ids)
calls: _fetch_market_regime, _is_earnings_blackout, get_snapshots, get_bars, get_daily_bars, _avg_daily_vol, compute_intraday_signals, log_hypothetical_trade, _load_open_positions, _et_now, _HOLD_BARS
called_by: _runner_loop, paper_runner_scan_now (app.py)
mutates: intraday_trades table (INSERT via log_hypothetical_trade), _run_log
---

---
name: _load_open_positions
type: function
file: fetchers/paper_runner.py
purpose: Query DB for all open hypothetical trades (is_hypothetical=1, exit_time IS NULL). Returns list of dicts with id, symbol, side, entry_price, entry_time, stop_price, target1_price, target_price, planned_hold_bars.
inputs: none
outputs: list[dict]
calls: db.database.get_db
called_by: check_and_close_positions, get_runner_status
mutates: none
---

---
name: _check_exit
type: function
file: fetchers/paper_runner.py
purpose: Evaluate exit conditions for a single open position against recent 5-min bar data. Checks actual bar highs/lows so stop/target touches within a 30-min check interval are not missed. Priority: STOP_LOSS > TARGET_2_HIT > TARGET_1_HIT > TIME_STOP. Returns (reason, exit_price, elapsed_bars) or None if no exit.
inputs: trade: dict, recent_bars: list[dict], current_price: float, now_utc: datetime
outputs: Optional[tuple[str, float, int]]
calls: none
called_by: check_and_close_positions
mutates: none
---

---
name: check_and_close_positions
type: function
file: fetchers/paper_runner.py
purpose: Check all open hypothetical positions and close any that hit stop, target, or time limit. Gets current snapshots + last 6 five-min bars per symbol; evaluates via _check_exit; calls log_trade_exit for each position that exits. force_close=True exits all at current price regardless of levels (used at 15:50 ET). Returns {"checked": N, "closed": M}.
inputs: force_close: bool = False
outputs: dict {checked, closed}
calls: _load_open_positions, get_snapshots, get_bars, _check_exit, log_trade_exit, _et_now
called_by: _runner_loop, paper_runner_scan_now (app.py indirectly)
mutates: intraday_trades table (UPDATE via log_trade_exit), _run_log
---

---
name: _runner_loop
type: function
file: fetchers/paper_runner.py
purpose: Background thread body. Runs every 60s; on weekdays during market hours: triggers morning scan at 9:35 ET (once per day), position checks every 30 min, and EOD force-close at 15:50 ET. Exits cleanly when _runner_active is set to False.
inputs: symbols: list[str], min_score: int
outputs: none
calls: _et_now, _is_market_open, _in_scan_window, _near_close, run_open_scan, check_and_close_positions
called_by: start_runner (thread target)
mutates: _runner_active (reads), today_scanned (local), _run_log (via calls)
---

---
name: start_runner
type: function
file: fetchers/paper_runner.py
purpose: Start the background paper runner thread (daemon). Returns True if started fresh, False if already running. Called from app.py startup event.
inputs: symbols: Optional[list[str]] = None, min_score: int = RUNNER_MIN_SCORE
outputs: bool
calls: _runner_loop (thread target)
called_by: startup (app.py), paper_runner_start (app.py)
mutates: _runner_thread, _runner_active
---

---
name: stop_runner
type: function
file: fetchers/paper_runner.py
purpose: Signal the runner loop to stop on its next 60s tick by setting _runner_active=False.
inputs: none
outputs: none
calls: none
called_by: paper_runner_stop (app.py)
mutates: _runner_active
---

---
name: get_runner_status
type: function
file: fetchers/paper_runner.py
purpose: Return current state of the paper runner for the status endpoint. Includes: active flag, configured symbols/min_score, market_open status, ET time, count of open positions, their symbols, and last 20 _run_log events (newest first).
inputs: none
outputs: dict
calls: _runner_active, _runner_thread, _is_market_open, _et_now, _load_open_positions
called_by: paper_runner_status (app.py)
mutates: none
---

---

## app.py (trading endpoints)

---
name: smart_trade
type: function
file: app.py
purpose: POST /trade/smart-order — signal-driven bracket order. Fetches snapshot + bars, computes intraday signals, applies liquidity veto (pass=False), time-of-day veto (MARKET_CLOSED, LUNCH_CHOP < 60), signal gate (|score| < min_score), places Alpaca market bracket order (entry=None), logs entry via log_trade_entry. Returns predicta_trade_id + alpaca_order_id.
inputs: SmartOrderRequest {symbol, account_value=10000, hold_bars=6, min_score=30}
outputs: dict {status, predicta_trade_id, alpaca_order_id, symbol, side, qty, entry, stop, target, ...}
calls: get_snapshot, get_bars, get_daily_bars, compute_intraday_signals, place_bracket_order, log_trade_entry
called_by: POST /trade/smart-order
mutates: intraday_trades (INSERT via log_trade_entry)
---

---
name: sync_trade_exits
type: function
file: app.py
purpose: POST /trade/sync-exits — polls Alpaca closed orders and logs exits for any open intraday_trades matched by alpaca_order_id. Determines exit reason from bracket leg type (TARGET2, STOP, MANUAL). Returns synced/skipped/error counts.
inputs: none
outputs: dict {synced, skipped, checked, errors}
calls: get_orders, log_trade_exit, get_db
called_by: POST /trade/sync-exits (cron or frontend poll)
mutates: intraday_trades (UPDATE via log_trade_exit)
---

---
name: trade_performance
type: function
file: app.py
purpose: GET /trade/performance?days=7 — returns aggregated paper trading stats via get_trade_stats. Includes win rate, total P&L, adjusted P&L, and breakdown by time-of-day session and exit reason.
inputs: days: int = 7 (query param)
outputs: dict from get_trade_stats
calls: get_trade_stats
called_by: GET /trade/performance
mutates: none
---

---
name: signal_only
type: function
file: app.py
purpose: POST /trade/signal-only — logs signal + hypothetical trade WITHOUT placing Alpaca order. Uses same SmartOrderRequest as smart_trade. Evaluates all vetoes and logs WOULD_REJECT signals too (status=SIGNAL_ONLY_WOULD_REJECT) — critical for detecting over-rejection bias. Returns would_reject reason so caller knows what gate would have fired.
inputs: SmartOrderRequest {symbol, account_value=10000, hold_bars=6, min_score=30}
outputs: dict {status, predicta_trade_id, would_reject, entry, stop, target1, target2, score, liquidity, note}
calls: get_snapshot, get_bars, get_daily_bars, compute_intraday_signals, log_hypothetical_trade
called_by: POST /trade/signal-only
mutates: intraday_trades (INSERT with is_hypothetical=1 via log_hypothetical_trade)
---

---
name: resolve_hypothetical
type: function
file: app.py
purpose: POST /trade/resolve-hypothetical/{trade_id} — manually record outcome of a signal-only trade. Accepts exit_price, exit_reason (TARGET_HIT/STOP_HIT/TIME_EXPIRED/MANUAL), actual_hold_bars. Calls log_trade_exit which computes the same pnl_r and adjusted_pnl as real trades, enabling apples-to-apples comparison of hypothetical vs paper performance.
inputs: trade_id (path), HypotheticalOutcome {exit_price, exit_reason, actual_hold_bars=None}
outputs: dict {status: RESOLVED, trade_id, pnl_dollars, pnl_r, adjusted_pnl, ...}
calls: log_trade_exit
called_by: POST /trade/resolve-hypothetical/{trade_id}
mutates: intraday_trades (UPDATE via log_trade_exit)
---

---
name: _determine_exit_reason
type: function
file: app.py
purpose: Maps Alpaca bracket order leg status to internal exit reason codes: TARGET2 (limit leg filled), STOP (stop leg filled), MANUAL_CANCEL (order cancelled), MANUAL (fallback).
inputs: alpaca_order: dict
outputs: str ("TARGET2" | "STOP" | "MANUAL_CANCEL" | "MANUAL")
calls: none
called_by: sync_trade_exits
mutates: none
---

---

## models/elo.py

---
name: IMPORTANCE_K
type: variable
file: models/elo.py
purpose: Maps match importance labels to Elo K-factor values controlling how much ratings shift per result.
inputs: none
outputs: dict[str, int]
calls: none
called_by: EloModel.update
mutates: none
---

---
name: DEFAULT_K
type: variable
file: models/elo.py
purpose: Fallback K-factor (30) for importance labels not in IMPORTANCE_K.
inputs: none
outputs: int
calls: none
called_by: EloModel.update
mutates: none
---

---
name: DEFAULT_RATING
type: variable
file: models/elo.py
purpose: Starting Elo rating (1500) assigned to any team not yet in the database.
inputs: none
outputs: float
calls: none
called_by: EloModel.get_rating, EloModel.explain
mutates: none
---

---
name: _goal_diff_multiplier
type: function
file: models/elo.py
purpose: Returns a multiplier (1.0–2.25+) based on goal difference to scale the K-factor; larger wins move ratings more.
inputs: goal_diff: int
outputs: float
calls: abs
called_by: EloModel.update
mutates: none
---

---
name: _expected
type: function
file: models/elo.py
purpose: Calculates expected score for team A given both Elo ratings using the standard Elo formula.
inputs: rating_a: float, rating_b: float
outputs: float (0–1)
calls: none
called_by: EloModel.win_probability, EloModel.update, EloModel.explain
mutates: none
---

---
name: EloModel
type: class
file: models/elo.py
purpose: World Football Elo system — stores ratings in SQLite and provides win probability + update methods.
inputs: none (stateless; reads/writes DB)
outputs: none
calls: get_db
called_by: predict_match, record_outcome, run_analysis
mutates: elo_ratings table
---

---
name: EloModel.get_rating
type: function
file: models/elo.py
purpose: Retrieves a team's current Elo rating from DB; returns DEFAULT_RATING if not found.
inputs: team: str
outputs: float
calls: get_db
called_by: EloModel.win_probability, EloModel.update, EloModel.explain, predict_match
mutates: none
---

---
name: EloModel.set_rating
type: function
file: models/elo.py
purpose: Upserts a team's Elo rating into the elo_ratings table.
inputs: team: str, rating: float
outputs: none
calls: get_db
called_by: EloModel.update, run_analysis
mutates: elo_ratings table
---

---
name: EloModel.win_probability
type: function
file: models/elo.py
purpose: Returns (prob_a_wins, prob_b_wins) for two teams based on their Elo ratings; ignores draw.
inputs: team_a: str, team_b: str
outputs: tuple[float, float]
calls: get_rating, _expected
called_by: predict_match
mutates: none
---

---
name: EloModel.update
type: function
file: models/elo.py
purpose: Updates both teams' Elo ratings after a match result using goal-difference-scaled K-factor.
inputs: team_a: str, team_b: str, score_a: int, score_b: int, importance: str = "default"
outputs: tuple[float, float] (new ratings)
calls: get_rating, _goal_diff_multiplier, _expected, set_rating
called_by: record_outcome
mutates: elo_ratings table
---

---
name: EloModel.explain
type: function
file: models/elo.py
purpose: Returns a human-readable string describing the Elo rating gap and win probability.
inputs: team_a: str, team_b: str
outputs: str
calls: get_rating, win_probability
called_by: none (utility)
mutates: none
---

---

## models/glicko.py

---
name: Glicko2Model
type: class
file: models/glicko.py
purpose: Glicko-2 rating system for tennis and table tennis, supporting per-surface ratings stored in SQLite.
inputs: none
outputs: none
calls: get_db, glicko2 package
called_by: predict_match, record_outcome
mutates: glicko2_ratings table
---

---
name: Glicko2Model.get_rating
type: function
file: models/glicko.py
purpose: Retrieves Glicko-2 rating, RD, and volatility for a participant/sport/surface; returns defaults if not found.
inputs: participant: str, sport: str, surface: str = "all"
outputs: dict {rating, rd, volatility}
calls: get_db
called_by: Glicko2Model.win_probability, Glicko2Model.update, Glicko2Model.explain
mutates: none
---

---
name: Glicko2Model.set_rating
type: function
file: models/glicko.py
purpose: Upserts a Glicko-2 rating record for a participant/sport/surface combination.
inputs: participant: str, sport: str, surface: str, rating: float, rd: float, volatility: float
outputs: none
calls: get_db
called_by: Glicko2Model.update
mutates: glicko2_ratings table
---

---
name: Glicko2Model.win_probability
type: function
file: models/glicko.py
purpose: Approximates win probability from Glicko-2 rating difference using the logistic expected score formula with RD uncertainty.
inputs: participant_a: str, participant_b: str, sport: str, surface: str = "all"
outputs: tuple[float, float]
calls: get_rating, math.log, math.sqrt
called_by: predict_match
mutates: none
---

---
name: Glicko2Model.update
type: function
file: models/glicko.py
purpose: Updates winner and loser Glicko-2 ratings after a match outcome.
inputs: winner: str, loser: str, sport: str, surface: str = "all"
outputs: none
calls: get_rating, glicko2.Player.update_player, set_rating
called_by: record_outcome
mutates: glicko2_ratings table
---

---

## models/dixon_coles.py

---
name: TAU
type: variable
file: models/dixon_coles.py
purpose: Dixon-Coles rho parameter (0.1) controlling the strength of the low-score correlation correction.
inputs: none
outputs: float
calls: none
called_by: _dc_adjustment, predict
mutates: none
---

---
name: HOME_ADVANTAGE
type: variable
file: models/dixon_coles.py
purpose: Multiplicative home advantage factor (1.15) applied to home team's attack mu.
inputs: none
outputs: float
calls: none
called_by: predict
mutates: none
---

---
name: _dc_adjustment
type: function
file: models/dixon_coles.py
purpose: Applies the Dixon-Coles correction factor to adjust probability of low-score outcomes (0-0, 1-0, 0-1, 1-1).
inputs: goals_home: int, goals_away: int, mu_h: float, mu_a: float, tau: float
outputs: float (adjustment multiplier)
calls: none
called_by: predict
mutates: none
---

---
name: predict
type: function
file: models/dixon_coles.py
purpose: Legacy single-strength Dixon-Coles predictor. Computes home/draw/away win probabilities and expected goals from attack/defense values. Used by engine.py for backward compatibility with the original /analyze endpoint. New soccer pipeline uses predict_xg().
inputs: attack_home: float, defense_home: float, attack_away: float, defense_away: float, league_avg_goals: float = 1.35, neutral: bool = False
outputs: dict {prob_home, prob_draw, prob_away, mu_home, mu_away}
calls: poisson.pmf, _dc_adjustment, np.zeros, np.tril, np.triu, np.trace
called_by: predict_match
mutates: none
---

---
name: strengths_from_signals
type: function
file: models/dixon_coles.py
purpose: Legacy strengths builder. Converts xg_for_avg5 / xg_against_avg5 from the signals dict into attack/defense multipliers relative to a hardcoded 1.35 league average. Used by engine.py soccer path. New pipeline uses strengths_from_xg() which supports npxG, venue splits, recent/season blend, and shrinkage.
inputs: signals: dict
outputs: dict {attack: float, defense: float}
calls: none
called_by: predict_match
mutates: none
---

---
name: explain
type: function
file: models/dixon_coles.py
purpose: Returns a human-readable string summarizing the Dixon-Coles model output (expected goals and win probabilities).
inputs: result: dict, team_home: str, team_away: str
outputs: str
calls: none
called_by: none (utility)
mutates: none
---

---
name: DEFAULT_RECENT_WEIGHT
type: variable
file: models/dixon_coles.py
purpose: Weight (0.6) given to last-5 recent xG vs season-long xG when blending in strengths_from_xg. Fixed weight tracks form changes without over-fitting; replaces Mark Dixon's exponential decay for in-season use.
inputs: none
outputs: float
calls: none
called_by: strengths_from_xg
mutates: none
---

---
name: SHRINKAGE_K
type: variable
file: models/dixon_coles.py
purpose: Shrinkage strength constant (10.0). When matches_played is small, raw xG is pulled toward league average with weight matches / (matches + K). At 10 games ≈ 50% raw, at 38 games ≈ 79% raw.
inputs: none
outputs: float
calls: none
called_by: _shrink
mutates: none
---

---
name: _blend
type: function
file: models/dixon_coles.py
purpose: Weighted average of season and recent xG values; falls back to whichever exists if one is None. Used by strengths_from_xg to combine season totals with recent form.
inputs: season_val: Optional[float], recent_val: Optional[float], recent_weight: float = DEFAULT_RECENT_WEIGHT
outputs: Optional[float]
calls: none
called_by: strengths_from_xg
mutates: none
---

---
name: _shrink
type: function
file: models/dixon_coles.py
purpose: Bayesian-style shrinkage toward league mean using w = matches / (matches + SHRINKAGE_K). Pulls noisy small-sample xG toward the league prior. Accepts float matches (Round 3 #1) so callers using time-decayed inputs can pass Kish effective_n as the right variance denominator.
inputs: value: float, league_mean: float, matches: float
outputs: float
calls: SHRINKAGE_K
called_by: strengths_from_xg
mutates: none
---

---
name: strengths_from_xg
type: function
file: models/dixon_coles.py
purpose: Build attack/defense multipliers from npxG-style inputs. Layers: (1) season vs recent xG blend (60/40, recent component is now time-decayed via Kimi #2), (2) venue-specific vs overall blend scaled by venue sample size with a 6-match ramp (Kimi #g; was 8), (3) Bayesian shrinkage to league mean, (4) continuous damping by goal_overperform (Kimi #8): damp = 1 + clamp((1 - overperform) × 0.15, -0.05, +0.05) — smooth gradient replacing the binary 0.85/1.15 step. Returns attack/defense clamped to [0.3, 2.5] plus a components dict.
inputs: season_xg_for, season_xg_against, recent_xg_for, recent_xg_against, venue_xg_for, venue_xg_against (all Optional[float]); league_avg_goals: float = 1.40; matches_played: int = 19; venue_matches: int = 9; goal_overperform: float = 1.0; is_home: bool = True; recent_weight: float = 0.6; venue_weight: float = 0.45
outputs: dict {attack: float, defense: float, components: dict}
calls: _blend, _shrink
called_by: _build_strengths (analyze_soccer.py)
mutates: none
---

---
name: predict_xg
type: function
file: models/dixon_coles.py
purpose: npxG-aware Dixon-Coles predictor that decomposes μ into open-play + set-piece components, applies a TIERED opposing-keeper PSxG-GA adjustment (Kimi #f: open-play μ ±12% clamped [0.88, 1.15]; set-piece μ ±6% clamped [0.94, 1.08], reflecting that GKs influence shots from build-up more than close-range set-piece finishes), and computes the score matrix with low-score correction. Returns prob_home/draw/away plus mu_home/mu_away and their open/set decomposition (open/set values returned are POST-keeper adjustment).
inputs: home_attack: float, home_defense: float, away_attack: float, away_defense: float, league_avg_goals: float = 1.40, home_advantage: float = HOME_ADVANTAGE, neutral: bool = False, keeper_adj_home: float = 0.0, keeper_adj_away: float = 0.0, set_piece_share_home: float = 0.22, set_piece_share_away: float = 0.22, set_piece_aerial_mult_home: float = 1.0, set_piece_aerial_mult_away: float = 1.0, max_goals: int = MAX_GOALS, tau: float = TAU
outputs: dict {prob_home, prob_draw, prob_away, mu_home, mu_away, mu_home_open, mu_home_set, mu_away_open, mu_away_set, score_matrix}
calls: poisson.pmf, _dc_adjustment, np.zeros, np.tril, np.triu, np.trace
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---

## models/soccer_leagues.py

---
name: DEFAULT_LEAGUE_AVG_GOALS
type: variable
file: models/soccer_leagues.py
purpose: Sport-wide fallback for goals-per-team-per-game (1.40) when the league is unknown or Understat lookup fails.
inputs: none
outputs: float
calls: none
called_by: league_avg_goals
mutates: none
---

---
name: DEFAULT_HOME_ADVANTAGE
type: variable
file: models/soccer_leagues.py
purpose: Sport-wide fallback multiplicative home advantage (1.15) when league is unknown.
inputs: none
outputs: float
calls: none
called_by: home_advantage
mutates: none
---

---
name: DEFAULT_OPEN_PLAY_SHARE
type: variable
file: models/soccer_leagues.py
purpose: Sport-wide fallback open-play xG share (0.78) when league is unknown. Set-piece share = 1 - open_play_share.
inputs: none
outputs: float
calls: none
called_by: open_play_share
mutates: none
---

---
name: _LEAGUES
type: variable
file: models/soccer_leagues.py
purpose: Per-league constants table covering EPL (1.43, ha 1.13), La_liga (1.31, 1.18), Bundesliga (1.55, 1.10), Serie_A (1.38, 1.16), Ligue_1 (1.28, 1.17), RFPL (1.30, 1.20). Each entry has goals_per_team_per_game, home_advantage, open_play_xg_share. 5-season averages from public FBref totals; calibrate against live Understat league_avg_goals when available.
inputs: none
outputs: dict[str, dict[str, float]]
calls: none
called_by: league_avg_goals, home_advantage, open_play_share, known_leagues
mutates: none
---

---
name: league_avg_goals
type: function
file: models/soccer_leagues.py
purpose: Lookup goals-per-team-per-game for a league slug; falls back to DEFAULT_LEAGUE_AVG_GOALS. Kept as a secondary fallback only — the preferred anchor is league_avg_xg (Kimi #6).
inputs: league: Optional[str]
outputs: float
calls: _LEAGUES
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---
name: league_avg_xg
type: function
file: models/soccer_leagues.py
purpose: Lookup xG-per-team-per-game for a league slug; falls back to DEFAULT_LEAGUE_AVG_XG (1.47). Preferred anchor over league_avg_goals because the model produces xG (Kimi #6). EPL 1.51, La Liga 1.38, Bundesliga 1.62, Serie A 1.45, Ligue 1 1.36, RFPL 1.37 — each ~5% higher than the goals counterpart to reflect finishing variance.
inputs: league: Optional[str]
outputs: float
calls: _LEAGUES
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---
name: home_advantage
type: function
file: models/soccer_leagues.py
purpose: League-specific home advantage multiplier. EPL 1.13, La Liga 1.18, Bundesliga 1.10 — replaces the hardcoded 1.15 in the legacy Dixon-Coles path.
inputs: league: Optional[str]
outputs: float
calls: _LEAGUES
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---
name: open_play_share
type: function
file: models/soccer_leagues.py
purpose: Fraction of league xG from open play (rest is set pieces + penalties). Fallback when team-level Understat situation split is unavailable.
inputs: league: Optional[str]
outputs: float
calls: _LEAGUES
called_by: _set_piece_share (analyze_soccer.py)
mutates: none
---

---
name: known_leagues
type: function
file: models/soccer_leagues.py
purpose: Return list of league slugs with hardcoded constants. Used for UI dropdowns / validation.
inputs: none
outputs: list[str]
calls: _LEAGUES
called_by: none (utility)
mutates: none
---

---

## models/markets.py

---
name: build_score_matrix
type: function
file: models/markets.py
purpose: Builds the full Dixon-Coles score probability matrix (home goals × away goals) and returns it along with expected goals.
inputs: attack_home, defense_home, attack_away, defense_away: float, league_avg_goals: float = 1.35, neutral: bool = False, max_goals: int = 7, tau: float = 0.1
outputs: tuple (np.ndarray matrix, mu_home: float, mu_away: float)
calls: poisson.pmf, np.zeros
called_by: compute_all_markets
mutates: none
---

---
name: match_result_2up
type: function
file: models/markets.py
purpose: Computes probability that home wins by 2+, away wins by 2+, or neither, from the score matrix.
inputs: matrix: np.ndarray
outputs: dict {home_win_2up, away_win_2up, not_2up}
calls: none
called_by: compute_all_markets
mutates: none
---

---
name: correct_score
type: function
file: models/markets.py
purpose: Returns the top N most likely exact scorelines ranked by probability from the score matrix.
inputs: matrix: np.ndarray, top_n: int = 8
outputs: list[dict {score_home, score_away, label, prob}]
calls: none
called_by: compute_all_markets
mutates: none
---

---
name: spread
type: function
file: models/markets.py
purpose: Computes Asian handicap/spread probabilities for the home team covering each handicap line.
inputs: matrix: np.ndarray, lines: list[float] = None
outputs: list[dict {line, label, p_home_covers, p_push, p_away_covers}]
calls: none
called_by: compute_all_markets
mutates: none
---

---
name: winner_push_if_tied
type: function
file: models/markets.py
purpose: Returns 2-way market probabilities where a draw results in a push (void/refund).
inputs: matrix: np.ndarray
outputs: dict {p_home_win, p_away_win, p_draw, p_home_no_draw, p_away_no_draw}
calls: np.tril, np.triu, np.trace
called_by: compute_all_markets
mutates: none
---

---
name: next_shot_on_target
type: function
file: models/markets.py
purpose: Estimates which team gets the next shot on target based on xG pace ratio (mu values).
inputs: mu_home: float, mu_away: float
outputs: dict {p_home_next_sot, p_away_next_sot, note}
calls: none
called_by: compute_all_markets
mutates: none
---

---
name: method_of_goal
type: function
file: models/markets.py
purpose: Returns foot/header/penalty probability distribution for a specific goal number, optionally adjusted for team aerial index.
inputs: goal_number: int = 2, home_aerial_index: float = 1.0, away_aerial_index: float = 1.0, attacker_is_home: Optional[bool] = None
outputs: dict {goal_number, foot, header, penalty, note}
calls: none
called_by: compute_all_markets
mutates: none
---

---
name: corners_market
type: function
file: models/markets.py
purpose: Builds corner kick markets (totals O/U, first corner team probability, corners handicap) using a Poisson model from average corner stats.
inputs: home_corners_for, home_corners_against, away_corners_for, away_corners_against: float, lines: list[float] = None
outputs: dict {lambda_home, lambda_away, lambda_total, totals, first_corner, handicap, note}
calls: poisson.cdf, poisson.pmf
called_by: compute_all_markets
mutates: none
---

---
name: compute_all_markets
type: function
file: models/markets.py
purpose: Orchestrates all market calculators and returns a single dict with every sportsbook market derived from the score matrix.
inputs: attack_home, defense_home, attack_away, defense_away: float, league_avg_goals: float = 1.35, neutral: bool = False, home_aerial_index, away_aerial_index: float = 1.0, home_corners_for, home_corners_against, away_corners_for, away_corners_against: float
outputs: dict {mu_home, mu_away, match_result_2up, correct_score, spread, winner_push_if_tied, next_shot_on_target, method_of_goal_2, corners}
calls: build_score_matrix, match_result_2up, correct_score, spread, winner_push_if_tied, next_shot_on_target, method_of_goal, corners_market
called_by: predict_match
mutates: none
---

---

## models/kelly.py

---
name: KELLY_FRACTION
type: variable
file: models/kelly.py
purpose: Default Kelly fraction (0.25 = quarter-Kelly) applied to limit position size conservatively.
inputs: none
outputs: float
calls: none
called_by: kelly_stake
mutates: none
---

---
name: kelly_stake
type: function
file: models/kelly.py
purpose: Computes recommended paper stake using quarter-Kelly formula from model probability and decimal odds.
inputs: your_prob: float, decimal_odds: float, bankroll: float, fraction: float = KELLY_FRACTION
outputs: dict {edge, full_kelly_fraction, applied_kelly_fraction, recommended_stake, bankroll, paper_mode, note}
calls: none
called_by: predict_match, run_baseball_analysis
mutates: none
---

---
name: american_to_decimal
type: function
file: models/kelly.py
purpose: Convert American odds to decimal. -130 → 1.769, +110 → 2.100
inputs: american: float
outputs: float
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: vig_removed_prob
type: function
file: models/kelly.py
purpose: Strip bookmaker vig from two decimal odds; returns true implied probabilities summing to 1.
inputs: decimal_a: float, decimal_b: float
outputs: tuple[float, float]
calls: none
called_by: market_edge_summary
mutates: none
---

---
name: vig_removed_prob_three_way
type: function
file: models/kelly.py
purpose: Proportional (multiplicative) devig for 3-way markets (soccer 1X2). Returns (fair_a, fair_draw, fair_b) summing to 1.0. Round 5 patch A — soccer market_edge_summary was applying 2-way devig on 3-way inputs, producing negative vig and garbage breakevens. This function fixes it.
inputs: decimal_a: float, decimal_draw: float, decimal_b: float
outputs: tuple[float, float, float]
calls: none
called_by: market_edge_summary (when decimal_draw is not None)
mutates: none
---

---
name: market_edge_summary
type: function
file: models/kelly.py
purpose: Compare model probabilities to market odds — computes edge_a/b, vig, breakeven, verdict (VALUE/SLIGHT EDGE/FAIR/AVOID). Handles both 2-way markets (baseball / TT / DNB) and 3-way markets (soccer 1X2 — pass decimal_draw to activate). In 3-way mode, edge is measured against the vig-free fair prob because the draw absorbs implied probability. In 2-way mode edge is measured against raw 1/decimal (unchanged legacy behavior). Adds market_type: "2way"|"3way" and, when draw odds supplied, market_implied_draw / breakeven_draw / edge_draw / verdict_draw to the output.
inputs: model_prob_a: float, model_prob_b: float, decimal_a: float, decimal_b: float, decimal_draw: float | None = None, model_prob_draw: float | None = None
outputs: dict {has_real_odds, market_type, market_implied_a/b [/draw], breakeven_a/b [/draw], edge_a/b [/draw], vig, verdict_a/b [/draw]}
calls: vig_removed_prob, vig_removed_prob_three_way
called_by: run_baseball_analysis, run_soccer_analysis
mutates: none
---

---
name: explain_kelly
type: function
file: models/kelly.py
purpose: Returns a human-readable string describing the Kelly stake recommendation or why no stake is suggested.
inputs: result: dict (output of kelly_stake)
outputs: str
calls: none
called_by: predict_match
mutates: none
---

---

## models/calibration.py

---
name: brier_score
type: function
file: models/calibration.py
purpose: Computes mean squared error between predicted probabilities and binary outcomes (lower is better).
inputs: predictions: list[float], outcomes: list[int]
outputs: float
calls: np.mean
called_by: compute_metrics_from_db
mutates: none
---

---
name: log_loss_score
type: function
file: models/calibration.py
purpose: Computes log-loss (cross-entropy) between predicted probabilities and binary outcomes (lower is better).
inputs: predictions: list[float], outcomes: list[int], eps: float = 1e-7
outputs: float
calls: math.log
called_by: compute_metrics_from_db
mutates: none
---

---
name: expected_calibration_error
type: function
file: models/calibration.py
purpose: Expected Calibration Error — weighted average absolute difference between mean predicted confidence and actual accuracy across n_bins probability buckets. Target ECE < 0.05 = well-calibrated, < 0.10 = acceptable. math: ECE = (1/N) × Σ_bins |mean_conf_bin - mean_acc_bin| × |bin|
inputs: predictions: list[float], outcomes: list[int], n_bins: int = 10
outputs: float (0–1, lower is better)
calls: none
called_by: compute_metrics_from_db
mutates: none
---

---
name: reliability_curve
type: function
file: models/calibration.py
purpose: Buckets predictions into decile bins and returns mean predicted vs actual win rate per bin for calibration analysis.
inputs: predictions: list[float], outcomes: list[int], n_bins: int = 10
outputs: list[dict {bin_lower, bin_upper, mean_predicted, mean_actual, count}]
calls: none
called_by: compute_metrics_from_db
mutates: none
---

---
name: compute_metrics_from_db
type: function
file: models/calibration.py
purpose: Full accuracy + calibration report: prediction accuracy %, bet accuracy %, ROI, Brier score, log-loss, ECE, reliability curve, ECE-based Kelly multiplier, benchmark comparison. Broken down by sport, confidence level, and prediction method.
inputs: method: Optional[str], sport: Optional[str]
outputs: dict {overall, by_sport, by_confidence, by_method, brier_score, log_loss, ece, ece_benchmark, reliability_curve, recommended_kelly_adjustment {multiplier, note}, benchmarks}
calls: get_db, _accuracy_block, brier_score, log_loss_score, reliability_curve
called_by: accuracy (app.py), calibration (app.py), generate_html_report
mutates: none
---

---
name: _accuracy_block
type: function
file: models/calibration.py
purpose: Compute accuracy, bet accuracy, and ROI for a slice of prediction/outcome rows.
inputs: rows: list[dict]
outputs: dict {n, correct, accuracy, bets_placed, bets_correct, bet_accuracy, roi}
calls: none
called_by: compute_metrics_from_db
mutates: none
---

---

## models/devig.py

---
name: american_to_decimal
type: function
file: models/devig.py
purpose: Converts American moneyline odds to decimal format.
inputs: american: float
outputs: float
calls: none
called_by: _market_consensus (engine.py), user/CLI usage
mutates: none
---

---
name: decimal_to_implied
type: function
file: models/devig.py
purpose: Converts decimal odds to implied probability (1 / decimal odds).
inputs: decimal: float
outputs: float
calls: none
called_by: devig_market
mutates: none
---

---
name: devig_market
type: function
file: models/devig.py
purpose: Removes bookmaker vig from a set of decimal odds and returns normalized fair probabilities.
inputs: prices: dict {a, b, draw?} of decimal odds
outputs: dict {a, b, draw?} of fair probabilities (sum to 1)
calls: decimal_to_implied
called_by: add_odds (app.py), _market_consensus (engine.py), run_analysis
mutates: none
---

---
name: clv
type: function
file: models/devig.py
purpose: Computes Closing Line Value — how much your model probability exceeds the vig-free closing line (positive = edge).
inputs: your_implied_prob: float, closing_prices: dict, outcome_key: str = "a"
outputs: float
calls: devig_market
called_by: none (utility)
mutates: none
---

---

## models/ml_layer.py

---
name: MIN_SAMPLES
type: variable
file: models/ml_layer.py
purpose: Minimum number of resolved predictions (100) before the ML model will train; below this the Elo/Glicko/Poisson baseline is more reliable than a fitted model (raised from 50 — too few samples risk overfitting with 5-10 features).
inputs: none
outputs: int
calls: none
called_by: train
mutates: none
---

---
name: BASEBALL_FEATURES / TENNIS_FEATURES / SOCCER_FEATURES / TABLE_TENNIS_FEATURES
type: variable
file: models/ml_layer.py
purpose: Sport-specific ML feature lists; signal names must match what log_signal() records in each sport's pipeline. Missing signals default to 0.0 at train/predict time.
  BASEBALL: wrc_plus, starter_fip, bullpen_fip, park_factor, platoon_adj, starter_avg_ip, home_boost, elo_rating, wind_factor, temp_factor, is_dome
  TENNIS:   sqi, rqi, surface_win_rate, form_score, rest_days, glicko2_rating, surface_amp
  SOCCER:   elo_diff, glicko2_diff, form_diff, h2h_decayed, rest_diff, key_player_out_flag, neutral_site_flag, fatigue_flag, home_adv
  TABLE_TENNIS: elo_diff, glicko2_diff, form_diff, h2h_decayed, fatigue_flag
calls: none
called_by: _feature_list
mutates: none
---

---
name: SPORT_FEATURES
type: variable
file: models/ml_layer.py
purpose: Dict mapping sport name → feature list (e.g. "baseball" → BASEBALL_FEATURES). Used by _feature_list() to select the right schema at train/predict time.
calls: none
called_by: _feature_list
mutates: none
---

---
name: FEATURE_SCHEMA_VERSION
type: variable
file: models/ml_layer.py
purpose: Version string ("2025-06-v2") for detecting schema migrations. Included in train() return dict so model artifacts are traceable to feature schema.
calls: none
called_by: train
mutates: none
---

---
name: _feature_list
type: function
file: models/ml_layer.py
purpose: Returns feature list for a given sport, or union of all sport features (sparse, missing=0.0) when sport is None.
inputs: sport: Optional[str]
outputs: list[str]
calls: SPORT_FEATURES
called_by: _gather_training_data, train, predict
mutates: none
---

---
name: _gather_training_data
type: function
file: models/ml_layer.py
purpose: Queries DB for predictions + outcomes + signals and assembles feature matrix X and label vector y. When sport is specified, filters to that sport and uses its feature schema; otherwise uses union schema for mixed-sport training.
inputs: sport: Optional[str] = None
outputs: tuple (X: list[list], y: list[int])
calls: get_db, _feature_list
called_by: train
mutates: none
---

---
name: train
type: function
file: models/ml_layer.py
purpose: Trains a calibrated logistic regression or GBM model on historical prediction data and saves it to disk with joblib. Sport parameter selects feature schema and filters training data.
inputs: model_path: str = MODEL_PATH_DEFAULT, use_gbm: bool = False, sport: Optional[str] = None
outputs: dict {status, samples, model_path, sport, features, schema_version} or {error}
calls: _gather_training_data, _feature_list, LogisticRegression, GradientBoostingClassifier, CalibratedClassifierCV, joblib.dump
called_by: none (invoked manually / via CLI)
mutates: ml_model.json (creates/overwrites)
---

---
name: predict
type: function
file: models/ml_layer.py
purpose: Loads a saved ML model and returns predicted win probability for a feature dict. Sport parameter must match the sport used during training to select the right feature schema.
inputs: features: dict, model_path: str = MODEL_PATH_DEFAULT, sport: Optional[str] = None
outputs: Optional[float]
calls: joblib.load, np.array, _feature_list
called_by: none (invoked manually / via CLI)
mutates: none
---

---

## models/trading/signals.py

---
name: compute_signals
type: function
file: models/trading/signals.py
purpose: Entry point for daily trading signal computation — runs all indicators on OHLCV history and returns a unified signals dict.
inputs: history: list[dict] (OHLCV, oldest first)
outputs: dict {trend, momentum, volatility, volume, expected_move, support_resistance, score}
calls: _trend, _momentum, _volatility, _volume, _expected_move, _support_resistance, _composite_score
called_by: run_trade_analysis
mutates: none
---

---
name: _sma
type: function
file: models/trading/signals.py
purpose: Computes simple moving average of the last n values in an array.
inputs: arr: np.ndarray, n: int
outputs: float
calls: none
called_by: _trend, _volatility
mutates: none
---

---
name: _trend
type: function
file: models/trading/signals.py
purpose: Determines bull/bear/neutral trend direction and strength from MA20/MA50/MA200 alignment with weighted votes.
inputs: closes: np.ndarray
outputs: dict {direction, strength, votes, mas}
calls: _sma
called_by: compute_signals
mutates: none
---

---
name: _rsi
type: function
file: models/trading/signals.py
purpose: Computes RSI(period) from a closing price array using simple average gain/loss.
inputs: closes: np.ndarray, period: int = 14
outputs: float (0–100)
calls: np.diff, np.where
called_by: _momentum
mutates: none
---

---
name: _macd
type: function
file: models/trading/signals.py
purpose: MACD(12,26,9) with correct EMA warm-up: EMA12 runs from bar 12 through bar 25 before MACD line starts at bar 26. Returns histogram direction and crossover label for composite scoring.
inputs: closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
outputs: dict {macd, signal_line, histogram, direction, crossover}
calls: np.ndarray.mean
called_by: _momentum
mutates: none
---

---
name: _momentum
type: function
file: models/trading/signals.py
purpose: Returns RSI(14), RSI signal label, 5-day/20-day rate of change, and MACD(12,26,9) dict.
inputs: closes: np.ndarray
outputs: dict {rsi, rsi_signal, roc_5d, roc_20d, macd}
calls: _rsi, _macd
called_by: compute_signals
mutates: none
---

---
name: _atr
type: function
file: models/trading/signals.py
purpose: Computes Average True Range over the last period bars.
inputs: closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, period: int = 14
outputs: float
calls: np.mean
called_by: _volatility
mutates: none
---

---
name: _volatility
type: function
file: models/trading/signals.py
purpose: Computes ATR, ATR%, annualized historical volatility (20-day), and Bollinger Bands with %B position.
inputs: closes: np.ndarray, highs: np.ndarray, lows: np.ndarray
outputs: dict {atr, atr_pct, hv_annual, bollinger}
calls: _atr, _sma, np.log, np.diff, math.sqrt
called_by: compute_signals
mutates: none
---

---
name: _volume
type: function
file: models/trading/signals.py
purpose: Computes current volume relative to 20-day average and assigns a signal label.
inputs: volumes: np.ndarray
outputs: dict {current, avg_20d, relative, signal}
calls: none
called_by: compute_signals
mutates: none
---

---
name: _expected_move
type: function
file: models/trading/signals.py
purpose: Calculates ±1σ and ±2σ expected price range for the next trading session based on 20-day historical volatility. prob_up uses z-score of recent mean log return divided by daily vol, clamped to [20%, 80%] to avoid overconfidence.
inputs: closes: np.ndarray, days_ahead: int = 1
outputs: dict {days, pct_1sigma, upper/lower_1sigma, pct_2sigma, upper/lower_2sigma, prob_up}
calls: np.log, np.diff, math.sqrt
called_by: compute_signals
mutates: none
---

---
name: _support_resistance
type: function
file: models/trading/signals.py
purpose: Identifies support and resistance levels from 60-day swing highs and lows plus the 25th/75th percentile mid levels.
inputs: closes: np.ndarray, highs: np.ndarray, lows: np.ndarray
outputs: dict {resistance, mid_resistance, support, mid_support, pct_to_resistance, pct_to_support}
calls: np.percentile
called_by: compute_signals
mutates: none
---

---
name: _composite_score
type: function
file: models/trading/signals.py
purpose: Regime-adaptive composite score -100 to +100 across 5 signals (all weights sum to 1.0). High-vol (HV>30%): trend 15%, RSI 30%, ROC 20%, BB 15%, MACD 20%. Low-vol (<15%): trend 40%, RSI 15%, ROC 15%, BB 10%, MACD 20%. Normal: trend 30%, RSI 25%, ROC 15%, BB 10%, MACD 20%. MACD crossover fires ±100 raw; sustained direction ±50 raw.
inputs: signals: dict (output of compute_signals)
outputs: dict {value, label, color, reasons}
calls: none
called_by: compute_signals
mutates: none
---

---

## models/trading/kelly.py

---
name: trading_kelly
type: function
file: models/trading/kelly.py
purpose: Computes quarter-Kelly position size for a trade using win rate and average win/loss percentages. Use only when historical win/loss stats are available (50+ closed trades).
inputs: win_rate: float, avg_win_pct: float, avg_loss_pct: float, bankroll: float = 10000.0, fraction: float = 0.25
outputs: dict {full_kelly, kelly_fraction, position_size, bankroll, edge_pct, win_rate, avg_win_pct, avg_loss_pct, win_loss_ratio, note, paper_mode}
calls: none
called_by: none (manual / future use once 50+ trades exist)
mutates: none
---

---
name: atr_position_size
type: function
file: models/trading/kelly.py
purpose: Volatility-targeting position size: risks a fixed % of capital per trade, sized so stop (stop_mult × ATR) = risk_amount. Regime-agnostic alternative to Kelly when win rate is unknown.
inputs: price: float, atr: float, risk_per_trade: float = 0.01, account_value: float = 10000.0, stop_mult: float = 1.5
outputs: dict {shares, position_size, stop_distance, risk_amount, risk_pct_of_account, paper_mode}
calls: none
called_by: none (utility; logic embedded in kelly_from_signals)
mutates: none
---

---
name: kelly_from_signals
type: function
file: models/trading/kelly.py
purpose: ATR-based position sizing with empirical win rate overlay. Risks 1% per trade (1.5× ATR stop). Score gate: no position when |score| < 20. Kimi review (round 2): empirical win rate + veto are both gated behind calibration_globally_active() — no-op until 100 total closed trades exist, since the base ensemble's edge is unvalidated below that floor. Once active, veto uses veto_decision() — an 80% confidence-interval test on the score bucket's win rate (min 30 trades in-bucket, vetoes only if CI upper bound < 48%) — replacing the old naive "win rate < 45%" point-estimate check (Kimi review, round 1), which was noise-prone at small sample sizes. Reports win_rate and win_rate_source ("empirical", "unavailable", or "gated_pending_100_trades") for transparency.
inputs: score: float, atr_pct: float, bankroll: float = 10000.0
outputs: dict {full_kelly, kelly_fraction, position_size, bankroll, edge_pct, win_rate, win_rate_source, avg_win_pct, avg_loss_pct, win_loss_ratio, note, paper_mode}
calls: calibrated_win_rate, veto_decision, calibration_globally_active (signal_calibration.py)
called_by: run_trade_analysis, intraday_analysis (app.py)
mutates: none
---

---

## models/trading/intraday.py

---
name: WEIGHTS
type: variable
file: models/trading/intraday.py
purpose: 8-signal weight dictionary for the intraday ensemble (sums to 1.00): vwap 0.20, or 0.15, rsi 0.15, relvol 0.10, gap 0.10, trend 0.15, bollinger 0.10, volsurge 0.05.
inputs: none
outputs: dict[str, float]
calls: none
called_by: _composite
mutates: none
---

---
name: SCORE_LABELS
type: variable
file: models/trading/intraday.py
purpose: Ordered threshold-to-label pairs for classifying composite intraday score into Strong Buy / Buy / Neutral / Sell / Strong Sell.
inputs: none
outputs: list[tuple[int, str]]
calls: none
called_by: _composite
mutates: none
---

---
name: _ema
type: function
file: models/trading/intraday.py
purpose: Computes Exponential Moving Average for a list of values and pads the output to match input length.
inputs: values: list[float], period: int
outputs: list[float]
calls: none
called_by: none (utility available)
mutates: none
---

---
name: _rsi
type: function
file: models/trading/intraday.py
purpose: Computes RSI(period) from a list of closing prices using simple average gain/loss method.
inputs: closes: list[float], period: int = 9
outputs: float (0–100)
calls: none
called_by: _sig_rsi
mutates: none
---

---
name: _vwap
type: function
file: models/trading/intraday.py
purpose: Computes cumulative VWAP from the first bar of the session using typical price × volume.
inputs: bars: list[dict]
outputs: list[float]
calls: none
called_by: _sig_vwap
mutates: none
---

---
name: _atr
type: function
file: models/trading/intraday.py
purpose: Computes Average True Range over the last period bars from intraday OHLCV data.
inputs: bars: list[dict], period: int = 14
outputs: float
calls: none
called_by: _trade_levels
mutates: none
---

---
name: _bollinger
type: function
file: models/trading/intraday.py
purpose: Computes Bollinger Bands (20-period, 2σ) and %B position for intraday closes.
inputs: closes: list[float], period: int = 20
outputs: dict {upper, mid, lower, pct_b}
calls: math.sqrt
called_by: _sig_bollinger
mutates: none
---

---
name: _sig_vwap
type: function
file: models/trading/intraday.py
purpose: Regime-conditioned VWAP deviation signal. In a strong uptrend, deviation >1.5% above VWAP scores +15 (momentum) instead of -20 (mean-reversion). In a downtrend, deviation >1.5% below VWAP scores -15 instead of +20. trend_label sourced from _sig_trend_bias, pre-computed in compute_intraday_signals. Kimi review addition: trend_confidence gates the flip — only applies when "strong" (MA20/MA50 spread >=2%, from _sig_trend_bias's regime_confidence); when "weak" (compressed/ambiguous MA spread), falls back to the default fade logic instead of trusting an unreliable regime label.
inputs: bars: list[dict], snapshot: dict, trend_label: str = "neutral", trend_confidence: str = "strong"
outputs: dict {vwap, deviation_pct, label, score}
calls: _vwap
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_gap
type: function
file: models/trading/intraday.py
purpose: Regime-conditioned pre-market gap signal. Large gaps (>2%) are always faded. Small gaps (0.5–2%): gap-down in uptrend scores +10 (buy the dip); gap-up in downtrend scores -10 (fade the bounce). Neutral regime follows momentum direction. Kimi review addition: trend_confidence gates the trend-following branch — only applies when "strong" (MA20/MA50 spread >=2%); "weak" confidence falls back to regime-neutral default.
inputs: snapshot: dict, trend_label: str = "neutral", trend_confidence: str = "strong"
outputs: dict {gap_pct, direction, fill_prob, score}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_opening_range
type: function
file: models/trading/intraday.py
purpose: Determines if price has broken above or below the first-15-minute opening range and scores the breakout strength.
inputs: bars: list[dict]
outputs: dict {or_high, or_low, or_range, label, score}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_rsi
type: function
file: models/trading/intraday.py
purpose: Computes RSI-9 signal from intraday bars and labels it overbought/bearish/neutral/bullish/oversold.
inputs: bars: list[dict]
outputs: dict {rsi9, label, score}
calls: _rsi
called_by: compute_intraday_signals
mutates: none
---

---
name: _intraday_vol_curve
type: function
file: models/trading/intraday.py
purpose: Returns the expected fraction of daily volume that has traded by the bar's timestamp, modelling the U-shaped intraday volume seasonality (heavy at open/close, thin at lunch). Used by _sig_relative_volume to avoid comparing raw cumulative volume to a daily average without time adjustment.
inputs: bar_timestamp: str (ISO 8601)
outputs: float (0–1; defaults to 1.0 on parse error)
calls: datetime.fromisoformat, ZoneInfo
called_by: _sig_relative_volume
mutates: none
---

---
name: _sig_relative_volume
type: function
file: models/trading/intraday.py
purpose: Compares today's session cumulative volume to the time-adjusted expected volume (via _intraday_vol_curve) rather than raw daily average, correcting for intraday volume seasonality. Outputs rel_vol = today_volume / expected_volume_by_now and expected_pct_of_day for transparency.
inputs: bars: list[dict], daily_avg_volume: float
outputs: dict {today_volume, avg_volume, rel_vol, expected_pct_of_day, label, score}
calls: _intraday_vol_curve
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_trend_bias
type: function
file: models/trading/intraday.py
purpose: Assesses daily trend regime by checking whether price is above MA20 and MA50, scoring bullish or bearish bias. Kimi review addition: also computes ma_spread_pct (abs(ma20-ma50)/mid*100) and regime_confidence ("strong" if spread >=2%, else "weak") — downstream _sig_vwap/_sig_gap use this to decide whether to trust the label enough to flip their regime-conditioning logic, since a compressed spread means the regime could flip on the next session.
inputs: daily_bars: list[dict]
outputs: dict {ma20, ma50, ma_spread_pct, regime_confidence, above_ma20, above_ma50, label, score}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_bollinger
type: function
file: models/trading/intraday.py
purpose: Computes Bollinger %B for intraday closes and scores position (near lower band = bullish reversion candidate).
inputs: bars: list[dict]
outputs: dict {upper, mid, lower, pct_b, label, score}
calls: _bollinger
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_volume_surge
type: function
file: models/trading/intraday.py
purpose: Detects whether the last 3 bars show a ≥2× volume spike vs the session average, confirming signal momentum.
inputs: bars: list[dict]
outputs: dict {surge, recent_avg_vol, session_avg_vol, ratio, label, score}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_liquidity
type: function
file: models/trading/intraday.py
purpose: Spread-based liquidity filter. Hard reject (pass=False) at >0.3% spread because day trading edge is 10–30 bps. UNTRADEABLE (>0.5%): score zeroed. WIDE_SPREAD (>0.3%): pass=False. ELEVATED_SPREAD (>0.1%): pass=True but 50% score haircut + 10% position reduction. LIQUID: no penalty. Includes estimated_slippage_pct = spread/2 for market order cost modelling.
inputs: snapshot: dict (requires bid, ask, price keys)
outputs: dict {score, label, bid, ask, spread_pct, estimated_slippage_pct, pass}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _time_of_day_modifier
type: function
file: models/trading/intraday.py
purpose: Returns session-quality dict based on Eastern Time. MORNING_TREND (10:00–11:30) = 1.0; AFTERNOON_TREND (14:00–15:30) = 1.0; OPEN_NOISE (9:30–10:00) = 0.7; LUNCH_CHOP (11:30–14:00) = 0.4 with hard zero if score < 60; CLOSE_REVERSAL (15:30–16:00) = 0.6; MARKET_CLOSED = 0.0. Returns UNKNOWN with modifier=1.0 on parse failure.
inputs: bar_timestamp: str (ISO 8601)
outputs: dict {modifier, label, note}
calls: datetime.fromisoformat, ZoneInfo
called_by: compute_intraday_signals
mutates: none
---

---
name: _intraday_expected_move
type: function
file: models/trading/intraday.py
purpose: Computes expected price move for a specific hold period using per-bar volatility. More accurate than daily HV / sqrt(252) for intraday stop placement because daily HV includes overnight gaps. At 5-min bars: hold_bars=6 = 30-min scalp, hold_bars=12 = 1-hour hold.
inputs: closes: list[float], hold_bars: int = 6
outputs: dict {hold_bars, hold_minutes, pct_1sigma, dollars_1sigma, bar_vol_pct, suggested_stop_pct}
calls: math.log, math.sqrt
called_by: compute_intraday_signals
mutates: none
---

---
name: compute_exit_action
type: function
file: models/trading/intraday.py
purpose: Active position management for day trading — call after each new bar while a position is open. Four exit triggers: TIME_STOP (no progress after 10 bars/50 min), TRAIL_1.5R (trail stop 1.5R behind price at 2R profit, locks in 0.5R minimum), BREAKEVEN_LOCK (move stop to entry+1 tick after 1R profit), SIGNAL_REVERSAL (composite score flips sign AND |score| > 40 vs entry direction). Returns action dict for app.py to execute.
inputs: entry: float, stop: float, target1: float, current_price: float, bars_held: int, current_signals: dict, entry_score: float
outputs: dict {action: "EXIT"|"MODIFY_STOP"|"HOLD", reason: str, ...}
calls: none
called_by: none (utility; called by position management layer in app.py)
mutates: none
---

---
name: _trade_levels
type: function
file: models/trading/intraday.py
purpose: Entry/stop/target levels + position sizing. Stop uses intraday_em dollars_1sigma (calibrated to hold period) when available, falls back to 1.5×ATR. Targets scale with trend (strong: 2.0× and 3.5×; else 1.5× and 2.5×). Position sizing: risk 1% of account, capped at 25%. ELEVATED_SPREAD reduces size 10% via liquidity_adjustment.
inputs: bars: list[dict], snapshot: dict, side: str, trend_label: str = "neutral", hold_bars: int = 6, intraday_em: dict = None, liquidity: dict = None, account_value: float = 10000.0, risk_pct: float = 0.01
outputs: dict {side, entry, stop, target1, target2, atr, stop_basis, risk_per_share, rr_ratio, shares, position_value, risk_dollars, slippage_estimate, liquidity_adjustment}
calls: _atr
called_by: compute_intraday_signals
mutates: none
---

---
name: _composite
type: function
file: models/trading/intraday.py
purpose: Combines all 8 intraday signal scores using WEIGHTS into a normalized -100 to +100 ensemble score with label and top reasons.
inputs: signals: dict (individual signal dicts)
outputs: dict {value, label, reasons}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: compute_intraday_signals
type: function
file: models/trading/intraday.py
purpose: Main entry point — pre-computes trend_sig to regime-condition both _sig_vwap and _sig_gap (passing both trend_label and trend_confidence — Kimi review addition — so the conditioning flip is suppressed when the daily MA20/MA50 spread is compressed/ambiguous), runs all 8 signals + ensemble scoring + liquidity filter (hard reject / pass=False if spread >0.3%) + time-of-day modifier (0.4× + hard zero during LUNCH_CHOP if score < 40; 0.7× OPEN_NOISE; 0.0 MARKET_CLOSED) + intraday-EM-based trade levels with position sizing + exit_template for active management.
inputs: intraday_bars: list[dict], daily_bars: list[dict], snapshot: dict, daily_avg_volume: float = 0, hold_bars: int = 6
outputs: dict {signals, score, levels, liquidity, intraday_expected_move, exit_template}
calls: _sig_liquidity, _sig_trend_bias, _sig_vwap, _sig_opening_range, _sig_rsi, _sig_relative_volume, _sig_gap, _sig_bollinger, _sig_volume_surge, _composite, _time_of_day_modifier, _trade_levels, _intraday_expected_move
called_by: intraday_analysis (app.py), _analyze_one (screener.py)
mutates: none
---

---
name: log_intraday_trade
type: function
file: models/trading/intraday.py
purpose: Persists a completed intraday trade to the intraday_trades table for post-trade analysis and slippage tracking. Computes pnl_dollars and pnl_pct from prices when not supplied. Designed to be called after a position closes with actual fill prices. Returns {id, symbol, pnl_dollars} or {error}.
inputs: symbol, entry_time, exit_time, side, entry_price, exit_price, planned_hold_bars, actual_hold_bars, entry_score, exit_reason, slippage_entry=0.0, slippage_exit=0.0, pnl_dollars=None, pnl_pct=None
outputs: dict {id, symbol, pnl_dollars} or {error: str}
calls: db.database.get_db
called_by: none (utility; called by trade execution layer)
mutates: intraday_trades table (INSERT)
---

---

## models/trading/screener.py

---
name: DEFAULT_WATCHLIST
type: variable
file: models/trading/screener.py
purpose: Default list of 16 large-cap liquid symbols scanned when no custom watchlist is provided.
inputs: none
outputs: list[str]
calls: none
called_by: run_screener
mutates: none
---

---
name: _avg_daily_volume
type: function
file: models/trading/screener.py
purpose: Computes average daily volume from the last 20 daily bars for use as the relative volume baseline.
inputs: daily_bars: list[dict]
outputs: float
calls: none
called_by: _analyze_one
mutates: none
---

---
name: _API_LOCK / _LAST_API_CALL / _MIN_INTERVAL / _throttle
type: variable / function
file: models/trading/screener.py
purpose: Module-level rate-limit guard for Alpaca API (200 req/min free tier). _throttle() blocks until ≥0.40 s has elapsed since the last call, keeping throughput ~150 req/min across all threads so parallel screener scans don't trigger 429 errors.
inputs: none
outputs: none
calls: time.monotonic, time.sleep
called_by: _analyze_one
mutates: _LAST_API_CALL
---

---
name: _analyze_one
type: function
file: models/trading/screener.py
purpose: Fetches intraday and daily bars for one symbol and computes all signals; returns a ranked result dict or None on failure. Each API call is throttled via _throttle() to respect Alpaca rate limits.
inputs: symbol: str, snapshot: dict
outputs: Optional[dict] {symbol, price, change_pct, volume, score, label, reasons, side, entry, stop, target1, target2, rr_ratio, atr, signals}
calls: _throttle, get_bars, get_daily_bars, _avg_daily_volume, compute_intraday_signals
called_by: run_screener (via ThreadPoolExecutor)
mutates: none
---

---
name: run_screener
type: function
file: models/trading/screener.py
purpose: Scans a watchlist or today's top movers/most active stocks in parallel, computes intraday signals for each, and returns a ranked list sorted by composite score.
inputs: symbols: Optional[list[str]] = None, use_movers: bool = False, max_workers: int = 8
outputs: dict {count, symbols_scanned, results}
calls: get_top_movers, get_most_active, get_snapshots, _analyze_one (ThreadPoolExecutor)
called_by: scan_market (app.py)
mutates: none
---

---

## fetchers/thesportsdb.py

---
name: _tsdb_get
type: function
file: fetchers/thesportsdb.py
purpose: Makes an authenticated GET request to TheSportsDB API using the configured API key.
inputs: path: str, params: dict = {}
outputs: dict (JSON response)
calls: httpx.Client.get
called_by: search_team, last5_from_tsdb, fetch_h2h, search_players
mutates: none
---

---
name: _espn_get
type: function
file: fetchers/thesportsdb.py
purpose: Makes a GET request to ESPN's public API with a browser User-Agent header to avoid blocks.
inputs: url: str, params: dict = {}
outputs: dict (JSON response)
calls: httpx.Client.get
called_by: _espn_last5_by_scoreboard
mutates: none
---

---
name: search_team
type: function
file: fetchers/thesportsdb.py
purpose: Searches TheSportsDB for a soccer team by name and returns the first matching team dict.
inputs: name: str
outputs: Optional[dict]
calls: _tsdb_get
called_by: fetch_match_context
mutates: none
---

---
name: last5_from_tsdb
type: function
file: fetchers/thesportsdb.py
purpose: Tries multiple TheSportsDB season endpoints and formats to retrieve the last 5 completed results for a team ID.
inputs: team_id: str
outputs: list[dict]
calls: _tsdb_get
called_by: fetch_match_context
mutates: none
---

---
name: fetch_h2h
type: function
file: fetchers/thesportsdb.py
purpose: Retrieves the last 5 head-to-head results between two team IDs from TheSportsDB.
inputs: team_id_a: str, team_id_b: str
outputs: list[dict]
calls: _tsdb_get
called_by: fetch_match_context
mutates: none
---

---
name: search_players
type: function
file: fetchers/thesportsdb.py
purpose: Searches TheSportsDB for players associated with a team name.
inputs: team_name: str
outputs: list[dict]
calls: _tsdb_get
called_by: fetch_match_context
mutates: none
---

---
name: _parse_espn_events
type: function
file: fetchers/thesportsdb.py
purpose: Filters and normalizes ESPN scoreboard response into a list of completed match dicts for a specific team name.
inputs: data: dict, name_lower: str
outputs: list[dict]
calls: none
called_by: _espn_last5_by_scoreboard
mutates: none
---

---
name: _espn_last5_by_scoreboard
type: function
file: fetchers/thesportsdb.py
purpose: Searches ESPN scoreboards across 14 major soccer leagues over the past 12 months to find the last 5 results for a team without needing a team ID.
inputs: name: str
outputs: list[dict]
calls: _espn_get, _parse_espn_events
called_by: fetch_match_context
mutates: none
---

---
name: fetch_match_context
type: function
file: fetchers/thesportsdb.py
purpose: Main data fetcher — retrieves team profiles, last 5 results, squad, and H2H for both teams from TheSportsDB and ESPN, with full source transparency.
inputs: team_a: str, team_b: str
outputs: dict {team_a, team_b, h2h, sources}
calls: search_team, last5_from_tsdb, _espn_last5_by_scoreboard, search_players, fetch_h2h, _normalize_last5
called_by: run_analysis
mutates: none
---

---
name: _normalize_last5
type: function
file: fetchers/thesportsdb.py
purpose: Converts raw TheSportsDB or ESPN event dicts into a consistent frontend format with date, home, away, scores, and winner fields.
inputs: raw: list[dict]
outputs: list[dict {date, home, away, score_home, score_away, winner}]
calls: none
called_by: fetch_match_context, last5_from_tsdb, fetch_h2h
mutates: none
---

---

## fetchers/odds.py

---
name: SPORT_MAP
type: variable
file: fetchers/odds.py
purpose: Maps Predicta sport names to default domestic-league Odds API sport keys; international soccer uses INTL_SOCCER_KEYS first.
inputs: none
outputs: dict[str, Optional[str]]
calls: none
called_by: fetch_odds_snapshot
mutates: none
---

---
name: INTL_SOCCER_KEYS
type: variable
file: fetchers/odds.py
purpose: Ordered list of Odds API sport keys for international soccer (World Cup, Euros, Nations League, etc.) probed before the domestic fallback.
inputs: none
outputs: list[str]
calls: none
called_by: fetch_odds_snapshot
mutates: none
---

---
name: _fetch_from_key
type: function
file: fetchers/odds.py
purpose: Fetches raw event list from a single Odds API sport key; returns empty list on HTTP error.
inputs: api_key: str, sport_key: str, market: str
outputs: list
calls: httpx.Client.get
called_by: fetch_odds_snapshot
mutates: none
---

---
name: fetch_odds_snapshot
type: function
file: fetchers/odds.py
purpose: Fetches current odds from The Odds API and persists to odds_snapshots; for soccer auto-probes international competition keys before falling back to EPL.
inputs: match_id: int, sport: str, league_key: Optional[str] = None, market: str = "h2h"
outputs: list[dict]
calls: _get_api_key, _fetch_from_key, get_db
called_by: run_analysis
mutates: odds_snapshots table
---

---
name: log_manual_odds
type: function
file: fetchers/odds.py
purpose: Persists a manually entered odds snapshot to the database and returns its row ID.
inputs: match_id: int, book: str, market: str, price_a: float, price_b: float, price_draw: Optional[float] = None
outputs: int (row ID)
calls: get_db
called_by: add_odds (app.py)
mutates: odds_snapshots table
---

---

## fetchers/signals.py

---
name: _save_signal
type: function
file: fetchers/signals.py
purpose: Inserts a single signal row into the signals table within an existing DB connection.
inputs: conn, match_id: int, signal_name: str, participant: Optional[str], signal_value: Optional[float], signal_text: Optional[str], source: str
outputs: none
calls: conn.execute
called_by: log_signal
mutates: signals table
---

---
name: log_signal
type: function
file: fetchers/signals.py
purpose: Persists a single signal value for a match participant to the database.
inputs: match_id: int, signal_name: str, participant: Optional[str], signal_value: Optional[float], signal_text: Optional[str], source: str = "manual"
outputs: none
calls: get_db, _save_signal
called_by: add_signal (app.py), run_analysis
mutates: signals table
---

---
name: get_signals_for_match
type: function
file: fetchers/signals.py
purpose: Retrieves all signals for a match and returns them as a nested dict keyed by participant then signal name.
inputs: match_id: int
outputs: dict {participant: {signal_name: value}}
calls: get_db
called_by: fetch_signals_for_match, get_signals (app.py)
mutates: none
---

---
name: compute_form_weighted
type: function
file: fetchers/signals.py
purpose: Computes an exponentially decayed form score from recent match results (1=win, 0.5=draw, 0=loss) with configurable decay.
inputs: results: list[float], n: int = 10, decay: float = 0.9
outputs: float (0–1)
calls: none
called_by: none (utility)
mutates: none
---

---
name: compute_h2h_decayed
type: function
file: fetchers/signals.py
purpose: Computes a time-decayed head-to-head win probability for participant A from a list of historical results with dates.
inputs: h2h_results: list[tuple[float, str]], decay: float = 0.85
outputs: float (0–1)
calls: none
called_by: none (utility)
mutates: none
---

---
name: fetch_signals_for_match
type: function
file: fetchers/signals.py
purpose: Retrieves pre-stored signals for a match structured for model input (alias for get_signals_for_match).
inputs: match_id: int
outputs: dict
calls: get_signals_for_match
called_by: predict_match (engine.py)
mutates: none
---

---

## fetchers/market_data.py

---
name: fetch_ticker
type: function
file: fetchers/market_data.py
purpose: Fetches OHLCV history, snapshot price, computed fundamentals (52w high/low, avg_volume), and moving averages for a ticker symbol via Alpaca Data API v2. Fundamentals requiring a paid data feed (PE, sector, beta) are returned as None.
inputs: symbol: str, period: str = "3mo"
outputs: dict {symbol, history, price, fundamentals, moving_averages, sources} or {error}
calls: get_daily_bars, get_snapshot
called_by: run_trade_analysis
mutates: none
---

---
name: search_ticker
type: function
file: fetchers/market_data.py
purpose: Best-effort ticker lookup via Alpaca snapshot — only exact/known symbols resolve (Alpaca has no search API). Returns a single-element list when symbol is valid, empty list otherwise.
inputs: query: str
outputs: list[dict {symbol, name, exchange, type}]
calls: get_snapshot
called_by: run_trade_analysis
mutates: none
---

---

## fetchers/alpaca.py

---
name: DATA_BASE_URL
type: variable
file: fetchers/alpaca.py
purpose: Base URL for Alpaca market data API (IEX feed, real-time data).
inputs: none
outputs: str
calls: none
called_by: get_bars, get_daily_bars, get_snapshot, get_snapshots, get_top_movers, get_most_active
mutates: none
---

---
name: PAPER_BASE_URL
type: variable
file: fetchers/alpaca.py
purpose: Base URL for Alpaca paper trading API (order placement, positions, account).
inputs: none
outputs: str
calls: none
called_by: place_order, place_bracket_order, get_orders, cancel_order, get_positions, get_account
mutates: none
---

---
name: _headers
type: function
file: fetchers/alpaca.py
purpose: Builds Alpaca authentication headers from ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables; raises RuntimeError if missing.
inputs: none
outputs: dict
calls: os.environ.get
called_by: _get, _post, _delete
mutates: none
---

---
name: _get
type: function
file: fetchers/alpaca.py
purpose: Makes an authenticated GET request to any Alpaca endpoint and returns parsed JSON or an error dict.
inputs: url: str, params: dict = None
outputs: dict
calls: _headers, requests.get
called_by: get_bars, get_daily_bars, get_snapshot, get_snapshots, get_top_movers, get_most_active, get_orders, get_positions, get_account
mutates: none
---

---
name: _post
type: function
file: fetchers/alpaca.py
purpose: Makes an authenticated POST request to any Alpaca endpoint and returns parsed JSON or an error dict with detail.
inputs: url: str, body: dict
outputs: dict
calls: _headers, requests.post
called_by: place_order, place_bracket_order
mutates: Alpaca paper account (creates orders)
---

---
name: _delete
type: function
file: fetchers/alpaca.py
purpose: Makes an authenticated DELETE request to cancel an Alpaca order; returns status ok or error dict.
inputs: url: str
outputs: dict
calls: _headers, requests.delete
called_by: cancel_order
mutates: Alpaca paper account (cancels order)
---

---
name: get_bars
type: function
file: fetchers/alpaca.py
purpose: Fetches OHLCV bars for a symbol at a given timeframe (default 5-min, 78 bars = one full trading day).
inputs: symbol: str, timeframe: str = "5Min", limit: int = 78
outputs: list[dict {t, o, h, l, c, v, vw}]
calls: _get
called_by: _analyze_one (screener.py), intraday_analysis (app.py)
mutates: none
---

---
name: get_daily_bars
type: function
file: fetchers/alpaca.py
purpose: Fetches daily OHLCV bars for context and trend analysis over the past N days.
inputs: symbol: str, days: int = 60
outputs: list[dict]
calls: _get
called_by: _analyze_one (screener.py), intraday_analysis (app.py)
mutates: none
---

---
name: get_snapshot
type: function
file: fetchers/alpaca.py
purpose: Fetches latest quote, trade, daily bar, and previous close for a single symbol to build a price snapshot.
inputs: symbol: str
outputs: dict {symbol, price, prev_close, change_pct, open, high, low, volume, vwap_day, bid, ask}
calls: _get
called_by: intraday_analysis (app.py)
mutates: none
---

---
name: get_snapshots
type: function
file: fetchers/alpaca.py
purpose: Batch-fetches snapshots for multiple symbols in one API call for efficient screener operation.
inputs: symbols: list[str]
outputs: dict[str, dict] (keyed by symbol)
calls: _get
called_by: run_screener
mutates: none
---

---
name: get_top_movers
type: function
file: fetchers/alpaca.py
purpose: Retrieves today's top gaining and losing stocks from Alpaca's screener endpoint.
inputs: limit: int = 20
outputs: dict {gainers: list, losers: list}
calls: _get
called_by: run_screener
mutates: none
---

---
name: get_most_active
type: function
file: fetchers/alpaca.py
purpose: Retrieves today's most actively traded stocks by volume from Alpaca's screener endpoint.
inputs: limit: int = 20
outputs: list[dict]
calls: _get
called_by: run_screener
mutates: none
---

---
name: _assert_paper_mode
type: function
file: fetchers/alpaca.py
purpose: Safety guard — raises RuntimeError if PAPER_BASE_URL does not contain "paper". Called at the top of every order-placement function to prevent accidental live trading if the constant is changed.
inputs: none
outputs: none (raises RuntimeError if guard fails)
calls: none
called_by: place_order, place_bracket_order
mutates: none
---

---
name: place_order
type: function
file: fetchers/alpaca.py
purpose: Places a paper trading order (market, limit, stop, or stop-limit) on Alpaca. Calls _assert_paper_mode() first to guarantee paper-only execution.
inputs: symbol: str, qty: float, side: str, order_type: str, limit_price: Optional[float], stop_price: Optional[float], time_in_force: str = "day", client_order_id: Optional[str]
outputs: dict (Alpaca order response or error)
calls: _assert_paper_mode, _post
called_by: place_trade (app.py)
mutates: Alpaca paper account (creates order)
---

---
name: place_bracket_order
type: function
file: fetchers/alpaca.py
purpose: Places a bracket order (entry + take-profit limit + stop-loss stop) as a single atomic Alpaca order. Calls _assert_paper_mode() first to guarantee paper-only execution.
inputs: symbol: str, qty: float, side: str, entry_price: Optional[float], take_profit: float, stop_loss: float
outputs: dict (Alpaca order response or error)
calls: _assert_paper_mode, _post
called_by: place_trade (app.py)
mutates: Alpaca paper account (creates bracket order)
---

---
name: get_orders
type: function
file: fetchers/alpaca.py
purpose: Lists paper trading orders filtered by status (open, closed, or all).
inputs: status: str = "open"
outputs: list[dict]
calls: _get
called_by: list_orders (app.py)
mutates: none
---

---
name: cancel_order
type: function
file: fetchers/alpaca.py
purpose: Cancels an open paper trading order by order ID.
inputs: order_id: str
outputs: dict {status: "ok"} or error
calls: _delete
called_by: cancel_trade (app.py)
mutates: Alpaca paper account (cancels order)
---

---
name: get_positions
type: function
file: fetchers/alpaca.py
purpose: Retrieves all open paper trading positions with symbol, quantity, entry, current price, and unrealized P&L.
inputs: none
outputs: list[dict {symbol, qty, side, avg_entry, current_price, unrealized_pl, unrealized_plpc, market_value}]
calls: _get
called_by: get_positions (app.py)
mutates: none
---

---
name: get_account
type: function
file: fetchers/alpaca.py
purpose: Retrieves paper trading account summary including equity, cash, buying power, and day trade count.
inputs: none
outputs: dict {equity, cash, buying_power, portfolio_value, daytrade_count, pattern_day_trader}
calls: _get
called_by: get_account (app.py)
mutates: none
---

---

## engine.py

---
name: elo_model
type: variable
file: engine.py
purpose: Module-level shared EloModel instance used across all prediction calls.
inputs: none
outputs: EloModel
calls: EloModel()
called_by: predict_match, record_outcome
mutates: elo_ratings table (via EloModel methods)
---

---
name: glicko_model
type: variable
file: engine.py
purpose: Module-level shared Glicko2Model instance used across all prediction calls.
inputs: none
outputs: Glicko2Model
calls: Glicko2Model()
called_by: predict_match, record_outcome
mutates: glicko2_ratings table (via Glicko2Model methods)
---

---
name: _market_consensus
type: function
file: engine.py
purpose: Averages vig-free probabilities across all stored odds snapshots for a match to build a market consensus.
inputs: match_id: int
outputs: Optional[dict {fair_probs, n_books}]
calls: get_db, devig_market
called_by: predict_match
mutates: none
---

---
name: _build_explanation
type: function
file: engine.py
purpose: Constructs a human-readable explanation string from model inputs including form, injury notes, Elo gap, and market consensus.
inputs: sport, participant_a, participant_b, prob_a, signals_a, signals_b, market, rating_detail: various
outputs: str
calls: none
called_by: predict_match
mutates: none
---

---
name: predict_match
type: function
file: engine.py
purpose: Full prediction pipeline for a stored match — fetches signals, runs Elo/Glicko/Dixon-Coles, blends probabilities, computes markets, persists prediction, returns result dict.
inputs: match_id: int, method: str = "model_v1", bankroll: float = 1000.0, surface: str = "all"
outputs: dict {prediction_id, match_id, sport, prob_a, prob_b, prob_draw, explanation, kelly, kelly_note, market, markets}
calls: get_db, fetch_signals_for_match, elo_model.win_probability, strengths_from_signals, dc_predict, compute_all_markets, glicko_model.win_probability, _market_consensus, _build_explanation, kelly_stake, explain_kelly
called_by: predict (app.py), run_analysis
mutates: predictions table
---

---
name: record_outcome
type: function
file: engine.py
purpose: Records a match result, updates match status to "final", and optionally updates Elo/Glicko ratings.
inputs: match_id: int, result: str, score_a: Optional[int], score_b: Optional[int], update_ratings: bool = True, importance: str = "default", surface: str = "all"
outputs: dict {status, result, new_rating_a?, new_rating_b?}
calls: get_db, elo_model.update, glicko_model.update
called_by: add_outcome (app.py)
mutates: outcomes table, matches table, elo_ratings or glicko2_ratings table
---

---

## analyze.py

---
name: _format_markets
type: function
file: analyze.py
purpose: Transforms raw market probability dicts from the engine into frontend-ready format with percentage labels, best-option flags, and team name substitution.
inputs: raw: dict, team_a: str, team_b: str
outputs: dict {match_result_2up, correct_score, spread, winner_push_if_tied, next_shot_on_target, method_of_goal_2, corners}
calls: none
called_by: run_analysis
mutates: none
---

---
name: _safe_float
type: function
file: analyze.py
purpose: Safely converts a value to float, returning a default if conversion fails or value is None.
inputs: val: any, default: any = None
outputs: Optional[float]
calls: float
called_by: run_analysis
mutates: none
---

---
name: run_analysis
type: function
file: analyze.py
purpose: Complete end-to-end sports analysis pipeline: parse query → fetch web data → AI interprets signals → create match + log signals → run prediction engine → AI generates narrative → return full result dict.
inputs: user_query: str
outputs: dict {match_id, team_a, team_b, prob_a, prob_draw, prob_b, narrative, markets, top_scorers_a/b, h2h, raw_sources, steps, ...}
calls: init_db, parse_query, fetch_match_context, interpret_signals, EloModel.set_rating, get_db, log_signal, fetch_odds_snapshot, predict_match, generate_narrative, _format_markets, devig_market
called_by: analyze (app.py)
mutates: matches, signals, odds_snapshots, predictions tables
---

---

## analyze_trading.py

---
name: _parse_ticker
type: function
file: analyze_trading.py
purpose: Extracts a ticker symbol from a natural language query using regex heuristics or falls back to AI parsing via parse_trade_query.
inputs: query: str
outputs: str (ticker symbol, uppercase)
calls: re.match, parse_trade_query
called_by: run_trade_analysis
mutates: none
---

---
name: run_trade_analysis
type: function
file: analyze_trading.py
purpose: Full trading analysis pipeline: parse ticker → fetch market data → compute signals → Kelly sizing → AI narrative → return result dict.
inputs: query: str, bankroll: float = 10000.0
outputs: dict {symbol, price, fundamentals, moving_averages, trend, momentum, volatility, volume, expected_move, support_resistance, score, kelly, narrative, raw_sources, history, steps}
calls: _parse_ticker, fetch_ticker, search_ticker, compute_signals, kelly_from_signals, generate_trade_narrative
called_by: analyze_trade (app.py)
mutates: none
---

---

## ai_agent.py

---
name: MODEL
type: variable
file: ai_agent.py
purpose: Claude model identifier used for all sports AI calls (claude-haiku-4-5-20251001).
inputs: none
outputs: str
calls: none
called_by: parse_query, interpret_signals, generate_narrative
mutates: none
---

---
name: _client
type: function
file: ai_agent.py
purpose: Creates and returns an authenticated Anthropic client from ANTHROPIC_API_KEY; raises RuntimeError if key missing.
inputs: none
outputs: anthropic.Anthropic
calls: os.environ.get, anthropic.Anthropic
called_by: parse_query, interpret_signals, generate_narrative
mutates: none
---

---
name: parse_query
type: function
file: ai_agent.py
purpose: Sends a user's natural language match query to Claude and returns structured JSON with team names, sport, date, and notes.
inputs: user_text: str
outputs: dict {team_a, team_b, date, sport, notes}
calls: _client, client.messages.create, json.loads, re.sub
called_by: run_analysis
mutates: none
---

---
name: interpret_signals
type: function
file: ai_agent.py
purpose: Sends fetched match context to Claude and returns quantified model signals (xG, form, Elo, corners, top scorers, injury flags) as a structured dict.
inputs: team_a: str, team_b: str, fetched_data: dict
outputs: dict {team_a signals, team_b signals, neutral_site, likely_scorer_a/b, top_scorers_a/b, confidence, signal_notes}
calls: _client, client.messages.create, json.loads, re.sub
called_by: run_analysis
mutates: none
---

---
name: generate_narrative
type: function
file: ai_agent.py
purpose: Sends model probabilities and signal context to Claude and returns a 2–3 sentence plain-language prediction narrative.
inputs: team_a, team_b, prob_a, prob_b, prob_draw, explanation, signals, fetched_context: various
outputs: str
calls: _client, client.messages.create
called_by: run_analysis
mutates: none
---

---

## ai_agent_trading.py

---
name: MODEL
type: variable
file: ai_agent_trading.py
purpose: Claude model identifier used for trading AI calls (claude-haiku-4-5-20251001).
inputs: none
outputs: str
calls: none
called_by: parse_trade_query, generate_trade_narrative
mutates: none
---

---
name: _client
type: function
file: ai_agent_trading.py
purpose: Creates and returns an authenticated Anthropic client from ANTHROPIC_API_KEY; raises RuntimeError if key missing.
inputs: none
outputs: anthropic.Anthropic
calls: os.environ.get, anthropic.Anthropic
called_by: parse_trade_query, generate_trade_narrative
mutates: none
---

---
name: parse_trade_query
type: function
file: ai_agent_trading.py
purpose: Sends a user's natural language trading query to Claude and returns the extracted ticker symbol in uppercase.
inputs: query: str
outputs: str (ticker symbol)
calls: _client, client.messages.create
called_by: _parse_ticker (analyze_trading.py)
mutates: none
---

---
name: generate_trade_narrative
type: function
file: ai_agent_trading.py
purpose: Sends signal data and Kelly sizing to Claude and returns a 3-sentence trading analysis under 90 words ending with the paper-mode disclaimer.
inputs: symbol: str, market_data: dict, signals: dict, kelly: dict
outputs: str
calls: _client, client.messages.create
called_by: run_trade_analysis
mutates: none
---

---

## report.py

---
name: generate_html_report
type: function
file: report.py
purpose: Renders the full accuracy dashboard: KPI tiles (prediction %, bet %, ROI, Brier), accuracy by sport, by confidence level, benchmark comparison table, and full prediction history with correct/wrong verdict badges. Also shows pending-results call-to-action.
inputs: none
outputs: str (HTML)
calls: get_db, compute_metrics_from_db
called_by: html_report (app.py)
mutates: none
---

---

## app.py (FastAPI routes and Pydantic models)

---
name: app
type: variable
file: app.py
purpose: The FastAPI application instance that registers all routes and middleware.
inputs: none
outputs: FastAPI
calls: FastAPI()
called_by: uvicorn (entrypoint)
mutates: none
---

---
name: startup
type: hook
file: app.py
purpose: FastAPI startup event handler. Initializes SQLite DB, launches background auto-resolve pass, and starts the automated paper trading runner (paper_runner.start_runner).
inputs: none
outputs: none
calls: init_db, run_auto_resolve (tasks/auto_resolve.py), start_runner (paper_runner.py)
called_by: FastAPI on_event("startup")
mutates: predicta.db, _runner_thread/_runner_active (paper_runner.py globals)
---

---
name: create_match
type: function
file: app.py
purpose: POST /matches — validates sport and inserts a new match record, returning its ID.
inputs: body: MatchCreate
outputs: dict {match_id}
calls: get_db
called_by: HTTP POST /matches
mutates: matches table
---

---
name: list_matches
type: function
file: app.py
purpose: GET /matches — returns all matches optionally filtered by sport and/or status.
inputs: sport: Optional[str], status: Optional[str]
outputs: list[dict]
calls: get_db
called_by: HTTP GET /matches
mutates: none
---

---
name: get_match
type: function
file: app.py
purpose: GET /matches/{id} — returns a single match by ID or 404.
inputs: match_id: int
outputs: dict
calls: get_db
called_by: HTTP GET /matches/{match_id}
mutates: none
---

---
name: add_signal
type: function
file: app.py
purpose: POST /matches/{id}/signals — logs a signal value for a match participant.
inputs: match_id: int, body: SignalCreate
outputs: dict {status: "ok"}
calls: log_signal
called_by: HTTP POST /matches/{match_id}/signals
mutates: signals table
---

---
name: predict
type: function
file: app.py
purpose: POST /matches/{id}/predict — runs the prediction engine for a match and returns full prediction result.
inputs: match_id: int, body: PredictRequest
outputs: dict (prediction result)
calls: predict_match
called_by: HTTP POST /matches/{match_id}/predict
mutates: predictions table
---

---
name: add_odds
type: function
file: app.py
purpose: POST /matches/{id}/odds — logs manual odds and returns fair de-vigged probabilities.
inputs: match_id: int, body: OddsCreate
outputs: dict {snapshot_id, fair_probs}
calls: log_manual_odds, devig_market
called_by: HTTP POST /matches/{match_id}/odds
mutates: odds_snapshots table
---

---
name: add_outcome
type: function
file: app.py
purpose: POST /matches/{id}/outcome — records a match result and optionally updates ratings.
inputs: match_id: int, body: OutcomeCreate
outputs: dict (record_outcome result)
calls: record_outcome
called_by: HTTP POST /matches/{match_id}/outcome
mutates: outcomes table, matches table, ratings tables
---

---
name: add_outcomes_batch
type: function
file: app.py
purpose: POST /outcomes/batch — record multiple match results at once. Body: [{match_id, result, score_a?, score_b?}].
inputs: list[BatchOutcome]
outputs: list[dict] with per-match status
calls: record_outcome
called_by: HTTP POST /outcomes/batch
mutates: outcomes table
---

---
name: pending_outcomes
type: function
file: app.py
purpose: GET /pending-outcomes — list matches that have a prediction but no recorded outcome. Shows recommendation and confidence for quick result entry.
inputs: none
outputs: list[dict]
calls: get_db
called_by: HTTP GET /pending-outcomes
mutates: none
---

---
name: accuracy
type: function
file: app.py
purpose: GET /accuracy — full accuracy report: prediction %, bet %, ROI, Brier, log-loss, calibration curve, benchmark comparison. Filter by sport= and method= query params.
inputs: sport: Optional[str], method: Optional[str]
outputs: dict (compute_metrics_from_db result)
calls: compute_metrics_from_db
called_by: HTTP GET /accuracy
mutates: none
---

---
name: resolve_pending
type: function
file: app.py
purpose: POST /resolve-pending — trigger auto-resolve of all unresolved match predictions. Fetches actual results from Setka Cup / TT Cup and records outcomes automatically. dry_run=true previews without writing.
inputs: dry_run: bool = False
outputs: dict {attempted, resolved, failed, skipped, details}
calls: run_auto_resolve
called_by: HTTP POST /resolve-pending
mutates: outcomes table (unless dry_run)
---

---
name: tt_performance
type: function
file: app.py
purpose: GET /tt-performance — Kimi's proof-of-edge endpoint for table tennis validation. Returns verdict: EDGE PROVEN (ROI>5%), EDGE EXISTS (ROI>0%), CALIBRATED-CHECK-ODDS (Brier<0.22), NO EDGE, or NOT ENOUGH DATA (<10 resolved). Plain-English guidance for each state. Call weekly after paper trading round-trip.
inputs: none
outputs: dict {verdict, n_resolved, roi?, brier_score?, message, next_step}
calls: compute_metrics_from_db(sport="table_tennis")
called_by: HTTP GET /tt-performance
mutates: none
---

---
name: signal_accuracy
type: function
file: app.py
purpose: GET /signal-accuracy — show per-signal accuracy lift (high vs low) to identify which model inputs are most predictive. Used to guide blend weight tuning.
inputs: none
outputs: dict {signal_name: {n, accuracy_high, accuracy_low, lift}}
calls: _signal_accuracy_summary
called_by: HTTP GET /signal-accuracy
mutates: none
---

---
name: calibration
type: function
file: app.py
purpose: GET /calibration — returns Brier score, log-loss, and reliability curve for all or a specific method's predictions.
inputs: method: Optional[str]
outputs: dict (calibration metrics)
calls: compute_metrics_from_db
called_by: HTTP GET /calibration
mutates: none
---

---
name: html_report
type: function
file: app.py
purpose: GET /report — generates and returns the full HTML prediction tracker report.
inputs: none
outputs: HTMLResponse
calls: generate_html_report
called_by: HTTP GET /report
mutates: none
---

---
name: analyze
type: function
file: app.py
purpose: POST /analyze — runs the full sports analysis pipeline from a natural language query.
inputs: body: AnalyzeRequest
outputs: dict (full analysis result)
calls: run_analysis
called_by: HTTP POST /analyze
mutates: matches, signals, predictions tables
---

---
name: analyze_trade
type: function
file: app.py
purpose: POST /analyze-trade — runs the full trading analysis pipeline for a ticker query. Checks body.query against _BRIEF_TRIGGERS first (case-insensitive substring match); if matched, runs run_screener(symbols=RUNNER_SYMBOLS) instead of the single-ticker pipeline and returns {"mode": "brief", ...scan result...} so the frontend can render the watchlist scan (with entry/stop/target1/rr_ratio per symbol) instead of the single-ticker card. Non-brief queries return {"mode": "single", ...run_trade_analysis result...}.
inputs: body: TradeRequest
outputs: dict (trading analysis result, or scan result with mode="brief")
calls: run_trade_analysis, run_screener (screener.py), RUNNER_SYMBOLS (paper_runner.py)
called_by: HTTP POST /analyze-trade
mutates: none
---

---
name: _BRIEF_TRIGGERS
type: variable
file: app.py
purpose: Multi-word phrases (e.g. "brief", "scan the market", "any signals") that route a query typed into the Analyze-tab text box to a watchlist scan instead of single-ticker analysis. Deliberately excludes bare "scan"/"watchlist" to avoid mis-firing on an actual ticker symbol.
inputs: none
outputs: tuple[str, ...]
calls: none
called_by: analyze_trade
mutates: none
---

---
name: scan_market
type: function
file: app.py
purpose: POST /scan — runs the market screener across a watchlist or today's top movers and returns ranked signal results.
inputs: body: ScanRequest
outputs: dict {count, symbols_scanned, results}
calls: run_screener
called_by: HTTP POST /scan
mutates: none
---

---
name: intraday_analysis
type: function
file: app.py
purpose: POST /intraday — fetches live Alpaca bars and computes full intraday signal analysis plus Kelly sizing for a single symbol.
inputs: body: IntradayRequest
outputs: dict {symbol, snapshot, intraday_bars, score, signals, levels, kelly}
calls: get_snapshot, get_bars, get_daily_bars, compute_intraday_signals, kelly_from_signals
called_by: HTTP POST /intraday
mutates: none
---

---
name: place_trade
type: function
file: app.py
purpose: POST /trade/order — places a paper trading order or bracket order on Alpaca.
inputs: body: OrderRequest
outputs: dict (Alpaca order response)
calls: place_order, place_bracket_order
called_by: HTTP POST /trade/order
mutates: Alpaca paper account
---

---
name: list_orders
type: function
file: app.py
purpose: GET /trade/orders — lists Alpaca paper trading orders by status.
inputs: status: str = "open"
outputs: list[dict]
calls: get_orders
called_by: HTTP GET /trade/orders
mutates: none
---

---
name: cancel_trade
type: function
file: app.py
purpose: DELETE /trade/orders/{id} — cancels an open Alpaca paper trading order.
inputs: order_id: str
outputs: dict
calls: cancel_order
called_by: HTTP DELETE /trade/orders/{order_id}
mutates: Alpaca paper account
---

---
name: get_positions
type: function
file: app.py
purpose: GET /trade/positions — returns all open Alpaca paper trading positions with P&L.
inputs: none
outputs: list[dict]
calls: get_positions (fetchers/alpaca.py)
called_by: HTTP GET /trade/positions
mutates: none
---

---
name: get_account
type: function
file: app.py
purpose: GET /trade/account — returns Alpaca paper account summary (equity, cash, buying power).
inputs: none
outputs: dict
calls: get_account (fetchers/alpaca.py)
called_by: HTTP GET /trade/account
mutates: none
---

---
name: analyze_baseball
type: function
file: app.py
purpose: POST /analyze-baseball — runs the full baseball analysis pipeline from a natural language query. Accepts optional odds_a/odds_b (decimal) so Kelly sizing uses real market prices instead of the hardcoded -110 default.
inputs: body: BaseballRequest {query, bankroll=1000, odds_a=1.909, odds_b=1.909}
outputs: dict (full baseball analysis result, now includes kelly_a and kelly_b)
calls: run_baseball_analysis
called_by: HTTP POST /analyze-baseball
mutates: matches, signals, predictions tables
---

---
name: home
type: function
file: app.py
purpose: GET / — serves the landing page with Sports and Stock Market category cards (home.html).
inputs: none
outputs: HTMLResponse
calls: none
called_by: HTTP GET /
mutates: none
---

---
name: sports
type: function
file: app.py
purpose: GET /sports — serves the sports sub-landing page with Soccer and Baseball cards (sports.html).
inputs: none
outputs: HTMLResponse
calls: none
called_by: HTTP GET /sports
mutates: none
---

---
name: soccer
type: function
file: app.py
purpose: GET /soccer — serves the soccer analysis page (index.html).
inputs: none
outputs: HTMLResponse
calls: none
called_by: HTTP GET /soccer
mutates: none
---

---
name: baseball_ui
type: function
file: app.py
purpose: GET /baseball — serves the baseball analysis page (baseball.html).
inputs: none
outputs: HTMLResponse
calls: none
called_by: HTTP GET /baseball
mutates: none
---

## templates/home.html

---
name: home.html
type: template
file: templates/home.html
purpose: Landing page with two category cards — Sports and Stock Market — using dark/neon-green portfolio-inspired design.
inputs: none
outputs: HTML
calls: none
called_by: home (app.py)
mutates: none
---

## templates/sports.html

---
name: sports.html
type: template
file: templates/sports.html
purpose: Sports sub-landing with Soccer, Baseball, Tennis, Ping Pong, and E-Sports cards; shows model pills for each sport.
inputs: none
outputs: HTML
calls: none
called_by: sports (app.py)
mutates: none
---

## templates/baseball.html

---
name: baseball.html
type: template
file: templates/baseball.html
purpose: Baseball analysis UI — natural language query → /analyze-baseball → renders probabilities, markets (moneyline, run line, totals, NRFI, first 5), and starter stats.
inputs: none
outputs: HTML
calls: /analyze-baseball API
called_by: baseball_ui (app.py)
mutates: none
---

---

## fetchers/data_cache.py

---
name: load_cached
type: function
file: fetchers/data_cache.py
purpose: Read a pre-fetched JSON file from data/live/. Returns the 'data' payload if the file exists and is younger than max_age_hours, else None. Used by baseball/tennis fetchers as fallback when ESPN is blocked by egress policy.
inputs: name: str (filename in data/live/), max_age_hours: int = 30
outputs: dict | list | None
calls: json.loads, datetime.fromisoformat
called_by: _cache_fallback (fetchers/baseball.py), _espn_get (fetchers/tennis.py)
mutates: none
---

---
name: cache_age_hours
type: function
file: fetchers/data_cache.py
purpose: Return the age in hours of a cached data/live/ file, or None if it doesn't exist.
inputs: name: str
outputs: float | None
calls: json.loads, datetime.fromisoformat
called_by: diagnostics / debug
mutates: none
---

## fetchers/live_data.py

---
name: fetch_mlb
type: function
file: fetchers/live_data.py
purpose: Fetch today's MLB scoreboard, standings, team list, team stats, and probable pitcher stats+profiles from ESPN. Saves 5 JSON files to data/live/.
inputs: date_str: str (YYYY-MM-DD)
outputs: none (writes files)
calls: _get, _save, ESPN_MLB endpoints
called_by: main (fetchers/live_data.py), GitHub Actions workflow
mutates: data/live/mlb_*.json
---

---
name: fetch_tennis
type: function
file: fetchers/live_data.py
purpose: Fetch today's ATP and WTA scoreboards from ESPN. Falls back to undated scoreboard if today's is empty.
inputs: date_str: str
outputs: none
calls: _get, _save
called_by: main (fetchers/live_data.py)
mutates: data/live/tennis_atp.json, data/live/tennis_wta.json
---

---
name: fetch_odds
type: function
file: fetchers/live_data.py
purpose: Fetch MLB and tennis odds from The Odds API. Skips silently if ODDS_API_KEY is not set.
inputs: none (reads ODDS_API_KEY from env)
outputs: none
calls: _get, _save
called_by: main (fetchers/live_data.py)
mutates: data/live/odds_mlb.json, data/live/odds_tennis_atp.json, data/live/odds_tennis_wta.json
---

## fetchers/baseball.py

---
name: ESPN_BASE
type: variable
file: fetchers/baseball.py
purpose: Base URL for the public ESPN MLB API (no key required; same host used by soccer fetcher).
inputs: none
outputs: str
calls: none
called_by: _espn_get, fetch_baseball_context (source labels)
mutates: none
---

---
name: LEAGUE_AVG_RUNS
type: variable
file: fetchers/baseball.py
purpose: 2024 MLB league-average runs per team per game (4.5), used as Poisson model baseline.
inputs: none
outputs: float
calls: none
called_by: expected_runs (baseball_market.py)
mutates: none
---

---
name: LEAGUE_AVG_FIP
type: variable
file: fetchers/baseball.py
purpose: 2024 MLB league-average FIP (4.00), used as pitcher quality baseline and default for TBD starters.
inputs: none
outputs: float
calls: none
called_by: expected_runs (baseball_market.py), _build_starter, _extract_probable, _get_pitcher_stats
mutates: none
---

---
name: FIP_CONSTANT
type: variable
file: fetchers/baseball.py
purpose: Constant (3.20) added to raw FIP numerator to align FIP scale with ERA scale.
inputs: none
outputs: float
calls: none
called_by: compute_fip
mutates: none
---

---
name: PARK_FACTORS
type: variable
file: fetchers/baseball.py
purpose: Dict mapping ESPN MLB team abbreviations to multi-year park run factors (1.0 = neutral). Coors Field (COL) = 1.38. Corrected 2026-07-04 from compressed range to FanGraphs-calibrated values.
inputs: none
outputs: dict[str, float]
calls: none
called_by: fetch_baseball_context
mutates: none
---

---
name: _espn_get
type: function
file: fetchers/baseball.py
purpose: GET request to ESPN MLB API with browser User-Agent; on any failure falls back to _cache_fallback() to read pre-fetched data from data/live/.
inputs: path: str, params: dict | None
outputs: dict
calls: httpx.Client.get, _cache_fallback
called_by: _all_teams, _get_all_records, _get_scoreboard, _get_pitcher_stats, _get_team_hitting
mutates: none
---

---
name: _cache_fallback
type: function
file: fetchers/baseball.py
purpose: Maps an ESPN API path to the corresponding pre-fetched JSON file in data/live/ and returns its parsed content. Called by _espn_get when the live request fails.
inputs: path: str
outputs: dict
calls: load_cached (fetchers/data_cache.py)
called_by: _espn_get
mutates: none
---

---
name: _f
type: function
file: fetchers/baseball.py
purpose: Safe float conversion for ESPN fields that may be strings (e.g. ".265") or None.
inputs: val: any, default: float = 0.0
outputs: float
calls: float
called_by: _stat, _get_all_records, compute_fip, _extract_probable, _get_pitcher_stats, _get_team_hitting
mutates: none
---

---
name: _stat
type: function
file: fetchers/baseball.py
purpose: Extracts a value from ESPN's name/value stats array by trying multiple name aliases.
inputs: stats_list: list, *names: str, default: float = 0.0
outputs: float
calls: _f
called_by: _get_all_records, compute_fip, _get_pitcher_stats, _get_team_hitting
mutates: none
---

---
name: _ip_from_espn
type: function
file: fetchers/baseball.py
purpose: Converts ESPN's IP format (95.1 = 95 innings + 1 out) to decimal innings (95.333). Handles both fractional and decimal formats.
inputs: ip_val: any
outputs: float
calls: none
called_by: compute_fip, _get_pitcher_stats
mutates: none
---

---
name: compute_fip
type: function
file: fetchers/baseball.py
purpose: Computes FIP from ESPN pitching stats list: ((13×HR + 3×BB - 2×K) / IP) + FIP_constant.
inputs: stats: list (ESPN name/value array)
outputs: Optional[float]
calls: _stat, _ip_from_espn
called_by: _get_pitcher_stats
mutates: none
---

---
name: _all_teams
type: function
file: fetchers/baseball.py
purpose: Fetches all active MLB teams from ESPN /teams endpoint and flattens the nested sports/leagues/teams structure.
inputs: none
outputs: list[dict]
calls: _espn_get
called_by: fetch_baseball_context
mutates: none
---

---
name: _match_team
type: function
file: fetchers/baseball.py
purpose: Fuzzy-matches a user-supplied team name to a single ESPN MLB team object using multiple name fields and difflib (cutoff 0.45).
inputs: name: str, teams: list[dict]
outputs: Optional[dict]
calls: difflib.get_close_matches
called_by: fetch_baseball_context
mutates: none
---

---
name: _match_teams
type: function
file: fetchers/baseball.py
purpose: Added 2026-07-05. Like _match_team but returns ALL plausible candidates ranked by closeness, not just the single best guess — needed because short city hints are genuinely ambiguous ("LA" matches both LAD and LAA equally well, "NY" matches NYY and NYM, "CHI" matches CHC and CWS). lookup_batter tries each candidate's roster in turn instead of committing to one fuzzy guess that might silently be the wrong team. Fixed the "Andy Pages" + "LA" bug where the player exists on LAD but the lookup could land on LAA and report a false "not found."
inputs: name: str, teams: list[dict], limit: int = 3
outputs: list[dict] (deduped by team id)
calls: difflib.get_close_matches
called_by: lookup_batter
mutates: none
---

---
name: _get_all_records
type: function
file: fetchers/baseball.py
purpose: Fetches all team W-L records from ESPN /standings; walks nested children to collect {team_id: {wins, losses, win_pct, run_differential}}.
inputs: none
outputs: dict[str, dict]
calls: _espn_get, _stat
called_by: fetch_baseball_context
mutates: none
---

---
name: _get_scoreboard
type: function
file: fetchers/baseball.py
purpose: Gets all MLB events for a given date (YYYY-MM-DD) from ESPN /scoreboard.
inputs: date_str: str
outputs: list[dict] (ESPN event objects)
calls: _espn_get
called_by: fetch_baseball_context
mutates: none
---

---
name: _find_game
type: function
file: fetchers/baseball.py
purpose: Scans ESPN scoreboard events for the competition between two team IDs; returns (event, competition) tuple or (None, None).
inputs: events: list[dict], team_a_id: str, team_b_id: str
outputs: tuple[Optional[dict], Optional[dict]]
calls: none
called_by: fetch_baseball_context
mutates: none
---

---
name: _extract_probable
type: function
file: fetchers/baseball.py
purpose: Extracts probable pitcher {id, name, era, stats_from_scoreboard} for home or away side from an ESPN competition object.
inputs: comp: dict, side: str ("home" or "away")
outputs: Optional[dict]
calls: _f
called_by: fetch_baseball_context
mutates: none
---

---
name: _get_pitcher_stats
type: function
file: fetchers/baseball.py
purpose: Fetches detailed season pitching stats (ERA, FIP, WHIP, K/9, BB/9, IP, GS) for an athlete from ESPN /athletes/{id}/statistics.
inputs: athlete_id: str
outputs: dict {era, fip, whip, k9, bb9, innings_pitched, games_started}
calls: _espn_get, _stat, _ip_from_espn, compute_fip, _f
called_by: _build_starter
mutates: none
---

---
name: _get_team_hitting
type: function
file: fetchers/baseball.py
purpose: Fetches team batting stats from ESPN /teams/{id}/statistics. wRC+ computed as (2×OBP+SLG)/1.045×100 when OBP/SLG available (correlates 0.97 with true wRC+), else falls back to OPS/0.730×100. math: wRC+ ≈ (2×OBP + SLG) / 1.045 × 100 where 1.045 = 2×0.315+0.415 (2025-26 MLB avg)
inputs: team_id: str
outputs: dict {ops, avg, obp, slg, k_pct, bb_pct, runs_per_game, wrc_plus}
calls: _espn_get, _stat, _f
called_by: fetch_baseball_context
mutates: none
---

---
name: _get_team_pitching
type: function
file: fetchers/baseball.py
purpose: Fetches team-level pitching stats (ERA, WHIP, K/9) from ESPN /teams/{id}/statistics pitching section; used to derive bullpen FIP.
inputs: team_id: str
outputs: dict {era, whip, k9}
calls: _espn_get, _stat, _f
called_by: fetch_baseball_context
mutates: none
---

---
name: _get_pitcher_handedness
type: function
file: fetchers/baseball.py
purpose: Fetches pitcher throwing hand ("R" or "L") from ESPN /athletes/{id} profile; returns "R" on any failure.
inputs: athlete_id: str
outputs: str ("R" or "L")
calls: _espn_get
called_by: _build_starter
mutates: none
---

---
name: _build_starter
type: function
file: fetchers/baseball.py
purpose: Builds a complete starter profile dict from a probable-pitcher stub; fetches detailed stats via _get_pitcher_stats and throwing hand via _get_pitcher_handedness; returns league-average defaults if pitcher is TBD or unknown. Added 2026-07-05 (Kimi's injury-gate review): also checks fetch_team_injuries(team_id) for the probable starter's athlete id — if he appears on the team's injury report at all (any status), sets injury_status/injury_detail so analyze_baseball.py can downgrade data_confidence to low instead of guessing at a replacement's stats.
inputs: probable: Optional[dict], team_id: str = ""
outputs: dict {name, fip, era, whip, k9, bb9, innings_pitched, games_started, recent_games, throws, injury_status, injury_detail}
calls: _get_pitcher_stats, _get_pitcher_handedness, fetch_team_injuries
called_by: fetch_baseball_context
mutates: none
---

---
name: fetch_team_injuries
type: function
file: fetchers/baseball.py
purpose: Added 2026-07-05 per Kimi's review. Fetches a team's current injury report from ESPN (/teams/{id}/injuries). Fails open (returns {} on any error) since this is a data-quality gate, not a core input — an ESPN injuries-endpoint hiccup must mean "no injury info available," never "block/downgrade the whole slate" (explicitly designed to avoid repeating the 2026-07-04 silent-failure pattern in a new subsystem).
inputs: team_id: str
outputs: dict[str, dict] — {athlete_id: {status, detail}}
calls: _espn_get
called_by: _build_starter
mutates: none
---

---
name: fetch_baseball_context
type: function
file: fetchers/baseball.py
purpose: Main entry point — resolves team names, fetches records, scoreboard game, probable starters (with FIP and handedness), team hitting stats, team pitching stats, and park factor from ESPN. Falls back gracefully if game not found.
inputs: team_a: str, team_b: str, game_date: Optional[str]
outputs: dict {team_a, team_b, game, sources} or {error, sources}; team_a/b include hitting and team_pitching sub-dicts
calls: _all_teams, _match_team, _get_all_records, _get_scoreboard, _find_game, _extract_probable, _build_starter, _get_team_hitting, _get_team_pitching
called_by: run_baseball_analysis
mutates: none
---

---
name: lookup_batter
type: function
file: fetchers/baseball.py
purpose: Live ESPN fallback for individual hitters not in the Player Impact KNOWN_HITTERS table. Resolves team_abbr to ALL plausible ESPN teams (via _match_teams, not just one best guess — fixed 2026-07-05) and tries each one's roster in turn until the player is found, pulling season batting stats from whichever team actually has them. Requires team_abbr since ESPN has no cross-league player-name search endpoint; returns {} if team_abbr is missing or the player isn't found on any matched team.
inputs: name (str), team_abbr (optional str)
outputs: dict (display_name, team, bats, pos, war=None, avg, ops, hr, sb, wrc_plus) or {}
calls: _match_teams, _all_teams, _find_roster_athlete, _get_batter_stats
called_by: models/player_impact.py _get_hitter_data
mutates: none
---

---
name: _find_roster_athlete
type: function
file: fetchers/baseball.py
purpose: Fuzzy-matches a player name against a team's ESPN roster (/teams/{id}/roster) and returns the athlete's id, position, and batting handedness.
inputs: team_id (str), name (str)
outputs: dict {id, name, pos, bats} or None
calls: _espn_get
called_by: lookup_batter
mutates: none
---

---
name: _get_batter_stats
type: function
file: fetchers/baseball.py
purpose: Fetches season batting stats (AVG/OBP/SLG/OPS/HR/SB) for one ESPN athlete id and derives wRC+ using the same 2×OBP+SLG formula as _get_team_hitting.
inputs: athlete_id (str)
outputs: dict {avg, ops, hr, sb, wrc_plus} or {}
calls: _espn_get
called_by: lookup_batter
mutates: none
---

---

## models/player_impact.py

NOTE: This module currently handles PITCHERS ONLY (Phase 1). Phase 2 will add hitter/position player impact scoring — see CLAUDE.md "Player Impact Score" section for the full plan.

---
name: KNOWN_STARTERS
type: dict
file: models/player_impact.py
purpose: Hardcoded stats for 80+ MLB starting pitchers (ERA, FIP, avg_ip, throws, team). Primary data source for pitcher impact lookups. Must be updated manually when stats change significantly.
inputs: none
outputs: dict[str, dict] keyed by pitcher display name
calls: none
called_by: _get_pitcher_data, search_pitchers
mutates: none
---

---
name: TRADE_NEWS
type: dict
file: models/player_impact.py
purpose: Trade/signing/injury news for 16 notable pitchers. Displayed in the UI when a matching pitcher is looked up.
inputs: none
outputs: dict[str, str] keyed by pitcher display name
calls: none
called_by: compute_impact
mutates: none
---

---
name: compute_impact
type: function
file: models/player_impact.py
purpose: Computes a pitcher's impact on win probability in percentage points vs a league-average replacement (FIP=4.00). Runs split Poisson model twice and returns delta, tier (ACE/FRONT-LINE/SOLID/AVERAGE/BELOW AVG/LIABILITY), stats, and optional trade note. Phase 2 will extend this to hitters using wRC+ delta instead of FIP delta.
inputs: pitcher_name (str), team_abbr (optional str), opponent_wrc_plus, park_factor, is_home
outputs: dict with impact_pp, tier, tier_desc, fip, era, avg_ip, win_prob_with, win_prob_replacement, runs, trade_note
calls: _get_pitcher_data, _win_prob_for_fip, expected_runs_split
called_by: app.py /player-impact endpoint
mutates: none
note: Fixed 2026-07-05 — barrel_pct/hard_hit_pct from the feature store are raw percents (e.g. 10.4 meaning 10.4%), not decimals. The backup FIP-estimate formula (used when a pitcher has no fip/era from KNOWN_STARTERS or the store) and the returned barrel_pct/whiff_pct display fields now convert to decimal before use — previously fed raw percents into decimal-scale formulas/frontend math, blowing FIP to the 6.0 clamp for almost any feature-store-only pitcher (e.g. Emmet Sheehan) and showing e.g. "1040.0%"/"8700.0%" for barrel%/whiff% in the UI.
---

---
name: search_pitchers
type: function
file: models/player_impact.py
purpose: Searches for pitchers by name prefix across KNOWN_STARTERS and the feature store. Returns list of {key, display_name, team, fip}.
inputs: query (str), limit (int)
outputs: list[dict]
calls: load_store, _norm
called_by: app.py /player-search endpoint (type=pitcher), search_players
mutates: none
---

---
name: KNOWN_HITTERS
type: variable
file: models/player_impact.py
purpose: Hardcoded dict of ~100 MLB position players with wRC+, OPS, AVG, HR, SB, WAR, bats, pos, team. Primary data source for hitter impact scoring.
inputs: none
outputs: dict[str, dict]
calls: none
called_by: _get_hitter_data, search_hitters
mutates: none
---

---
name: TEAM_WRC_PLUS
type: variable
file: models/player_impact.py
purpose: Team-level wRC+ averages (2024-25 blend) for all 30 MLB teams. Used to calculate how much a single hitter moves the team's overall wRC+.
inputs: none
outputs: dict[str, float]
calls: none
called_by: _team_wrc_with_hitter, _team_wrc_without_hitter
mutates: none
---

---
name: compute_hitter_impact
type: function
file: models/player_impact.py
purpose: Computes a hitter's impact on win probability in percentage points. Runs Poisson model with team wRC+ including the hitter vs replacing with league-average bat (wRC+ 100). Returns delta, tier (MVP/ALL-STAR/STARTER/AVERAGE/BENCH/REPLACEMENT), stats, and trade note. war is None for hitters resolved via the live ESPN fallback (ESPN doesn't expose WAR).
inputs: hitter_name (str), team_abbr (optional str), park_factor, is_home
outputs: dict with impact_pp, tier, tier_desc, wrc_plus, ops, avg, hr, sb, war, team_wrc_with, team_wrc_without, win_prob_with, win_prob_replacement, trade_note
calls: _get_hitter_data, _team_wrc_with_hitter, _team_wrc_without_hitter, _win_prob_for_wrc
called_by: app.py /hitter-impact endpoint
mutates: none
---

---
name: _get_hitter_data
type: function
file: models/player_impact.py
purpose: Looks up a hitter's stats — checks KNOWN_HITTERS first, then falls back to fetchers.baseball.lookup_batter for a live ESPN roster lookup so any active MLB hitter (not just the ~100 hardcoded stars) can resolve. team_abbr is required for the live fallback since ESPN has no cross-league player-name search.
inputs: name (str), team_abbr (optional str)
outputs: dict (display_name, wrc_plus, ops, avg, hr, sb, war, bats, pos, team) or {} if not found
calls: lookup_batter
called_by: compute_hitter_impact
mutates: none
---

---
name: search_hitters
type: function
file: models/player_impact.py
purpose: Searches for hitters by name prefix across KNOWN_HITTERS. Returns list of {key, display_name, team, wrc_plus, pos, type}.
inputs: query (str), limit (int)
outputs: list[dict]
calls: _norm
called_by: app.py /player-search endpoint (type=hitter), search_players
mutates: none
---

---
name: search_players
type: function
file: models/player_impact.py
purpose: Unified search across pitchers and hitters. Merges results from search_pitchers and search_hitters, tagging each with type=pitcher or type=hitter.
inputs: query (str), limit (int)
outputs: list[dict]
calls: search_pitchers, search_hitters
called_by: app.py /player-search endpoint (no type filter)
mutates: none
---

## models/baseball_market.py

---
name: LEAGUE_AVG_RUNS
type: variable
file: models/baseball_market.py
purpose: 2024 MLB baseline runs per team per game (4.5) used in split expected_runs formula.
inputs: none
outputs: float
calls: none
called_by: expected_runs_split
mutates: none
---

---
name: LEAGUE_AVG_FIP
type: variable
file: models/baseball_market.py
purpose: 2024 MLB baseline starter FIP (4.00) — denominator in pitcher quality factor.
inputs: none
outputs: float
calls: none
called_by: expected_runs_split
mutates: none
---

---
name: LEAGUE_BULLPEN_FIP
type: variable
file: models/baseball_market.py
purpose: 2024 MLB baseline bullpen FIP (4.40) — default when bullpen ERA cannot be derived from team stats.
inputs: none
outputs: float
calls: none
called_by: expected_runs, expected_runs_split
mutates: none
---

---
name: STARTER_FRAC / BULLPEN_FRAC
type: variable
file: models/baseball_market.py
purpose: Fractions of a 9-inning game pitched by starters (5/9 ≈ 0.556) and bullpen (4/9 ≈ 0.444). Used to split mu into F5 and L4 windows.
inputs: none
outputs: float
calls: none
called_by: expected_runs_split
mutates: none
---

---
name: PLATOON_VS_LHP / PLATOON_VS_RHP
type: variable
file: models/baseball_market.py
purpose: wRC+ multipliers for handedness matchup — RHB-heavy lineup gets +5% vs LHP starter (1.05); neutral vs RHP (1.00).
inputs: none
outputs: float
calls: none
called_by: platoon_wrc_adjust
mutates: none
---

---
name: platoon_wrc_adjust
type: function
file: models/baseball_market.py
purpose: Scales a team's wRC+ based on the opposing starter's throwing hand. +5% for typical RHB-heavy lineup vs LHP.
inputs: wrc_plus: float, pitcher_throws: str ("R" or "L")
outputs: float (adjusted wRC+)
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: expected_runs_split
type: function
file: models/baseball_market.py
purpose: Returns (mu_f5, mu_l4): expected runs for innings 1-5 (starter FIP) and 6-9 (bullpen FIP). When opp_starter_avg_ip is provided, starter_frac = clamp(avg_ip, 3, 7)/9 (dynamic); otherwise falls back to fixed 5/9. weather_factor (temp+wind combined, 0.85–1.15) and off_rest_mult (0.99–1.01) applied to base. math: base = LEAGUE_AVG × (wRC+/100) × park × home × weather_factor × off_rest_mult; mu_f5 = base × starter_frac × (starter_FIP/LEAGUE_FIP); mu_l4 = base × bullpen_frac × (bullpen_FIP/LEAGUE_FIP).
inputs: wrc_plus: float, opp_starter_fip: float, opp_bullpen_fip: float, park_factor: float = 1.0, is_home: bool = False, opp_starter_avg_ip: Optional[float] = None, weather_factor: float = 1.0, off_rest_mult: float = 1.0
outputs: tuple[float, float] — (mu_f5 clamped 0.5-8.0, mu_l4 clamped 0.4-7.0)
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: expected_runs
type: function
file: models/baseball_market.py
purpose: Total expected runs: delegates to expected_runs_split and sums F5+L4. Accepts optional opp_bullpen_fip; defaults to LEAGUE_BULLPEN_FIP.
inputs: wrc_plus: float, opp_starter_fip: float, park_factor: float = 1.0, is_home: bool = False, opp_bullpen_fip: Optional[float] = None
outputs: float (clamped 1.5–10.0)
calls: expected_runs_split
called_by: none (kept for backward compatibility)
mutates: none
---

---
name: build_run_matrix
type: function
file: models/baseball_market.py
purpose: Builds joint probability matrix P[home_runs, away_runs] using independent Poisson distributions.
inputs: mu_home: float, mu_away: float
outputs: np.ndarray shape (MAX_RUNS+1, MAX_RUNS+1)
calls: poisson.pmf, np.outer, np.arange
called_by: moneyline_market, run_line_market, total_market, compute_baseball_markets
mutates: none
---

---
name: moneyline_market
type: function
file: models/baseball_market.py
purpose: Returns P(home wins) and P(away wins) from the run matrix; tied games resolved in extras at 52/48.
inputs: matrix: np.ndarray
outputs: dict {p_home_win, p_away_win}
calls: np.sum, np.tril, np.triu, np.trace
called_by: compute_baseball_markets, first_five_market
mutates: none
---

---
name: run_line_market
type: function
file: models/baseball_market.py
purpose: Computes run line (spread) probabilities — P(home covers -line), P(away covers +line), P(push).
inputs: matrix: np.ndarray, line: float = 1.5
outputs: list[dict {line, label, p_home_covers, p_away_covers, p_push}]
calls: none
called_by: compute_baseball_markets
mutates: none
---

---
name: total_market
type: function
file: models/baseball_market.py
purpose: Computes Over/Under probabilities for multiple run total lines from the score matrix.
inputs: matrix: np.ndarray, lines: list[float] | None
outputs: list[dict {line, label, p_over, p_under, p_push}]
calls: none
called_by: compute_baseball_markets
mutates: none
---

---
name: first_five_market
type: function
file: models/baseball_market.py
purpose: First 5 innings market — takes pre-computed F5 expected run values (mu_home_f5, mu_away_f5) and returns home/away win probabilities for innings 1-5 only.
inputs: mu_home_f5: float, mu_away_f5: float
outputs: dict {mu_home_f5, mu_away_f5, p_home_win, p_away_win, note}
calls: poisson.pmf, np.outer, np.arange, moneyline_market
called_by: compute_baseball_markets
mutates: none
---

---
name: last_four_market
type: function
file: models/baseball_market.py
purpose: Innings 6-9 market — takes pre-computed L4 expected run values (mu_home_l4, mu_away_l4) and returns home/away win probabilities for the bullpen window.
inputs: mu_home_l4: float, mu_away_l4: float
outputs: dict {mu_home_l4, mu_away_l4, p_home_win, p_away_win, note}
calls: poisson.pmf, np.outer, np.arange, moneyline_market
called_by: compute_baseball_markets
mutates: none
---

---
name: nrfi_market
type: function
file: models/baseball_market.py
purpose: No Run First Inning market — each team's 1st-inning Poisson rate ≈ mu/9; computes P(neither scores in inning 1).
inputs: mu_home: float, mu_away: float
outputs: dict {p_nrfi, p_yrfi, note}
calls: poisson.pmf
called_by: compute_baseball_markets
mutates: none
---

---
name: team_total_market
type: function
file: models/baseball_market.py
purpose: Over/Under market for a single team's run total at lines 3.5, 4.5, 5.5.
inputs: mu: float, lines: list[float] | None
outputs: list[dict {line, label, p_over, p_under, p_push}]
calls: poisson.cdf
called_by: compute_baseball_markets
mutates: none
---

---
name: compute_baseball_markets
type: function
file: models/baseball_market.py
purpose: Orchestrates all baseball market calculators from split F5/L4 expected run values and returns unified market dict.
inputs: mu_home: float, mu_away: float, mu_home_f5: float, mu_away_f5: float, mu_home_l4: float, mu_away_l4: float, run_line: float = 1.5, total_lines: list[float] | None
outputs: dict {mu_home, mu_away, moneyline, run_line, totals, first_five, last_four, nrfi, team_total_home, team_total_away}
calls: build_run_matrix, moneyline_market, run_line_market, total_market, first_five_market, last_four_market, nrfi_market, team_total_market
called_by: run_baseball_analysis
mutates: none
---

---

## ai_agent_baseball.py

---
name: MODEL
type: variable
file: ai_agent_baseball.py
purpose: Claude model identifier used for baseball AI calls (claude-haiku-4-5-20251001).
inputs: none
outputs: str
calls: none
called_by: parse_baseball_query, generate_baseball_narrative
mutates: none
---

---
name: _client
type: function
file: ai_agent_baseball.py
purpose: Creates and returns an authenticated Anthropic client from ANTHROPIC_API_KEY; raises RuntimeError if key missing.
inputs: none
outputs: anthropic.Anthropic
calls: os.environ.get, anthropic.Anthropic
called_by: parse_baseball_query, generate_baseball_narrative
mutates: none
---

---
name: parse_baseball_query
type: function
file: ai_agent_baseball.py
purpose: Sends user's baseball query to Claude and returns structured JSON with home team, away team, date, notes, and optional American odds. Extracts odds_a_american and odds_b_american when present in query (e.g. "NYY -130 vs BOS +110 tonight").
inputs: user_text: str
outputs: dict {team_a, team_b, date, notes, odds_a_american?: float, odds_b_american?: float}
calls: _client, client.messages.create, json.loads, re.sub
called_by: run_baseball_analysis
mutates: none
---

---
name: interpret_baseball_signals
type: function
file: ai_agent_baseball.py
purpose: Fallback when ESPN API is unreachable — Claude estimates wRC+, starter FIP/ERA, park factor, and win% from training knowledge with confidence=low.
inputs: team_a: str, team_b: str, notes: str = ""
outputs: dict {team_a signals, team_b signals, park_factor, home_team, confidence, notes}
calls: _client, client.messages.create, json.loads, re.sub
called_by: run_baseball_analysis (fallback path)
mutates: none
---

---
name: generate_baseball_narrative
type: function
file: ai_agent_baseball.py
purpose: Sends pitcher FIP matchup, wRC+, park factor, and probabilities to Claude; returns a ≤90-word analytical narrative.
inputs: team_home: str, team_away: str, prob_home: float, prob_away: float, explanation: str, context: dict
outputs: str
calls: _client, client.messages.create
called_by: run_baseball_analysis
mutates: none
---

---

## analyze_baseball.py

---
name: _elo_from_winpct
type: function
file: analyze_baseball.py
purpose: Converts current-season win% to an equivalent Elo rating so team quality is seeded from real records rather than default 1500.
inputs: win_pct: float
outputs: float
calls: math.log10
called_by: run_baseball_analysis
mutates: none
---

---
name: _derive_bullpen_fip
type: function
file: analyze_baseball.py
purpose: Derives team bullpen ERA proxy from team ERA and starter stats. Uses starter_ERA (not FIP) for ERA decomposition to keep both sides in consistent units: bullpen_ERA = (team_ERA×9 - starter_ERA×avg_ip) / (9 - avg_ip). Falls back to starter_FIP when starter_ERA unavailable. Fallback when avg_ip unknown: (team_ERA×9 - starter_rate×5) / 4. Clamped [3.0, 7.5]; returns LEAGUE_BULLPEN_FIP when team_era missing.
inputs: team_era: float, starter_fip: float, starter_avg_ip: Optional[float] = None, starter_era: Optional[float] = None
outputs: float
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: _baseball_data_confidence
type: function
file: analyze_baseball.py
purpose: Computes data confidence (low/medium/high) for a baseball prediction from how much input is real live data vs defaults — mirrors the other sports' pipelines. low = ESPN unreachable (AI-estimated) OR both probable starters unknown/TBD (common for next-day games before lineups post); medium = one starter TBD, missing team wRC+, or thin sample (<10 GP); high = both starters named with FIP + real wRC+ + ≥10 GP. Fixes the bug where baseball never returned data_confidence so the UI always showed its hardcoded "medium" default (baseball.html:737). run_baseball_analysis applies an ADDITIONAL override after calling this (2026-07-05, Kimi's review): if either starter has injury_status set (appears on the team's injury report despite being the probable starter), confidence is force-downgraded to "low" regardless of what this function returns — a data-quality gate layered on top, not a change to this function itself.
inputs: ai_fallback: bool, starter_a, starter_b, hitting_a, hitting_b, record_a, record_b: dict
outputs: str ("low"|"medium"|"high")
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: _format_baseball_markets
type: function
file: analyze_baseball.py
purpose: Transforms raw baseball market probability dicts into frontend-ready format with percentages, labels, and best-option flags. run_line uses lines:[{label,p_home_covers,p_away_covers,p_push}]; totals uses lines:[{line,p_over,p_under}]; team_totals.home/.away are {team,mu,lines:[...]}.
inputs: markets: dict, team_home: str, team_away: str
outputs: dict {moneyline, run_line{label,lines}, totals{label,lines}, first_five, last_four, nrfi, team_totals{home:{team,mu,lines},away:{...}}}
notes: run_baseball_analysis also returns ai_signals list built from all collected data (wRC+, FIP/SIERA, CSW%, velo, barrel%, bullpen FIP, fatigue, weather)
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: _build_plain_summary
type: function
file: analyze_baseball.py
purpose: Plain-English bet recommendation in the user's preferred phrasing — "Team A is gonna win vs Team B (XX% accuracy). Bet on: runs OVER/UNDER 8.5, 1st 5 innings X, NRFI/YRFI." Derives runs over/under by comparing full-game μ_total to the 8.5 baseline (≥+0.7 → OVER, ≤−0.7 → UNDER); F5 side by μ_home_f5 vs μ_away_f5 (≥0.4 gap); NRFI/YRFI by Poisson on per-inning μ (≥58% NRFI / ≤42% NRFI → YRFI). Headline: "gonna win" (≥58%), "slight favourite" (52-58%), or "coin-flip" (<52%).
inputs: team_home: str, team_away: str, prob_home: float, prob_away: float, mu_home: float, mu_away: float, mu_home_f5: float, mu_away_f5: float
outputs: str
calls: math.exp
called_by: run_baseball_analysis
mutates: none
---

---
name: run_baseball_analysis
type: function
file: analyze_baseball.py
purpose: Full baseball_v2 pipeline: parse → fetch ESPN → step 2.5 enrich starters with FG SIERA/xFIP + Savant barrel% (non-destructive fallback) → derive bullpen FIP (dynamic) → platoon wRC+ → split Poisson F5/L4 → Elo blend (60/40, was 70/30) → 72% max confidence cap → markets → weather fetch (step 6.5, signal-only) → persist (signals incl. siera, xfip, barrel_pct_against, xwoba_against) → Kelly sizing (both sides, caller-supplied decimal odds) → AI narrative → plain_summary in user-preferred phrasing → result dict.
inputs: user_query: str, bankroll: float = 1000.0, odds_a: float = 1.909, odds_b: float = 1.909
outputs: dict {match_id, team_a, team_b, team_home, team_away, plain_summary, prob_a, prob_b, mu_home, mu_away, mu_home_f5, mu_away_f5, mu_home_l4, mu_away_l4, starters (with siera, xfip, fip_source, barrel_pct_against, xwoba_against, bullpen_fip), team_stats (with wrc_source, woba, iso), kelly_a, kelly_b, markets, narrative, raw_sources, game_pk (added 2026-07-05 — the MLB game id from context["game"], lets resolve_pending_bets grade this query's picks later), steps, …}
calls: parse_baseball_query, fetch_baseball_context, enrich_starter, enrich_team_hitting, _avg_ip, _derive_bullpen_fip, _baseball_data_confidence, platoon_wrc_adjust, expected_runs_split, compute_baseball_markets, EloModel, team_to_stadium_code, fetch_game_weather, weather_to_signals, kelly_stake, log_signal, get_db, generate_baseball_narrative, _format_baseball_markets, _build_plain_summary, _log_prediction
note: result dict now includes data_confidence (low/medium/high, from _baseball_data_confidence); also logged as a text signal for audit/calibration by-confidence. _log_prediction fires unconditionally at the end (manual queries included), persisting NRFI + moneyline/F5/O-U picks + game_pk to nrfi_bets so every query — not just the scheduled daily slate — can be graded later.
called_by: analyze_baseball (app.py)
mutates: matches, signals (incl. weather + savant signals), predictions tables, nrfi_bets (via _log_prediction)
---

---
name: _avg_ip
type: function (inner, defined inside run_baseball_analysis)
file: analyze_baseball.py
purpose: Returns a starter's average innings per start: innings_pitched / games_started. Returns None when games_started < 3 (insufficient sample). Used for dynamic starter_frac in both _derive_bullpen_fip and expected_runs_split.
inputs: starter: dict (keys: innings_pitched, games_started)
outputs: Optional[float]
calls: none
called_by: run_baseball_analysis (step 3)
mutates: none
---

---

## fetchers/nrfi_lineup.py

---
name: get_nrfi_lineup_wrc
type: function
file: fetchers/nrfi_lineup.py
purpose: Fetches today's confirmed top-3 batting lineup from MLB Stats API and returns approximate wRC+ for home and away teams. Uses MLB Schedule API to find game_pk by team abbreviation match, then boxscore API to extract batting order slots 100/200/300 (positions 1-3). Calls people/{id}/stats for each player to get season OBP+SLG (OPS), converts OPS to wRC+ via wrc≈(ops/0.710)*100. Returns (None, None) on any failure so predict_nrfi() gracefully falls back to league-average default (100). Called by analyze_baseball.py before predict_nrfi() each game query.
inputs: home_team: str (ESPN abbr), away_team: str, game_date: str|None (YYYY-MM-DD)
outputs: tuple[float|None, float|None] — (home_top3_wrc_approx, away_top3_wrc_approx)
calls: MLB Stats API /schedule, /game/{pk}/boxscore, /people/{id}/stats
called_by: run_baseball_analysis (analyze_baseball.py)
mutates: none
---

---

## fetchers/weather.py

---
name: STADIUM_COORDS
type: variable
file: fetchers/weather.py
purpose: dict mapping 3-letter MLB team code → (lat, lon) for all 30 MLB stadiums. Used to query OpenWeatherMap forecast API.
---

---
name: DOME_PARKS
type: variable
file: fetchers/weather.py
purpose: frozenset of stadium codes with dome or retractable roof (HOU, MIA, MIL, SEA, TB, TEX, TOR). wind_factor = 0.0 and is_dome = 1.0 for these parks.
---

---
name: TEAM_TO_STADIUM
type: variable
file: fetchers/weather.py
purpose: dict mapping ESPN team name variants (short code / nickname / full name) → 3-letter stadium code. Used by team_to_stadium_code() to resolve whatever ESPN returns.
---

---
name: team_to_stadium_code
type: function
file: fetchers/weather.py
purpose: Resolves a team name string (any ESPN format) to a stadium code. Tries exact match, then case-insensitive substring match. Returns None if unknown.
inputs: team_name: str
outputs: Optional[str]
calls: TEAM_TO_STADIUM
called_by: run_baseball_analysis
mutates: none
---

---
name: _wind_direction_factor
type: function
file: fetchers/weather.py
purpose: Returns alignment of wind with outfield direction. +1.0 = blowing straight out to CF (HR boost), -1.0 = blowing straight in (HR suppressor), 0.0 = dome/crosswind. Simplified: assumes CF at ~45° NE for most parks.
inputs: wind_deg: float, stadium_code: str
outputs: float (-1.0 to +1.0)
calls: math.cos, math.sin
called_by: fetch_game_weather
mutates: none
---

---
name: fetch_game_weather
type: function
file: fetchers/weather.py
purpose: Fetches 5-day 3-hour forecast from OpenWeatherMap for a stadium. Finds closest forecast entry to game time (within 3h). Returns None when API key missing, httpx not installed, stadium unknown, or fetch fails.
inputs: stadium_code: str, game_date: str, game_time: str = "19:05"
outputs: Optional[dict {temp_f, wind_mph, wind_deg, wind_factor, temp_factor, is_dome, forecast_time, source}]
calls: httpx.Client, _wind_direction_factor
called_by: run_baseball_analysis (step 6.5)
mutates: none
env_vars: OPENWEATHER_API_KEY
---

---
name: weather_to_signals
type: function
file: fetchers/weather.py
purpose: Converts weather dict to flat {signal_name: float} for DB logging. Falls back to neutral values (temp_f=72, wind_mph=0, weather_confidence=0.0) when input is None.
inputs: weather: Optional[dict]
outputs: dict[str, float] — keys: temp_f, wind_mph, wind_factor, temp_factor, is_dome, weather_confidence
calls: none
called_by: run_baseball_analysis (step 6.5)
mutates: none
---

---

## fetchers/tennis.py

---
name: ESPN_ATP_BASE
type: variable
file: fetchers/tennis.py
purpose: Base URL for the public ESPN ATP tennis API (no key required).
inputs: none
outputs: str
calls: none
called_by: _espn_recent_matches, _search_espn_athlete
mutates: none
---

---
name: ESPN_WTA_BASE
type: variable
file: fetchers/tennis.py
purpose: Base URL for the public ESPN WTA tennis API (no key required).
inputs: none
outputs: str
calls: none
called_by: _espn_recent_matches, _search_espn_athlete
mutates: none
---

---
name: TSDB_BASE
type: variable
file: fetchers/tennis.py
purpose: Base URL for TheSportsDB API (free key=1).
inputs: none
outputs: str
calls: none
called_by: _tsdb_get
mutates: none
---

---
name: ATP_AVG_* / WTA_AVG_*
type: variable
file: fetchers/tennis.py
purpose: Tour-average serve and return stats (first serve %, first serve won %, second serve won %, break points converted, aces per match) used to normalize quality indices to 100.
inputs: none
outputs: float
calls: none
called_by: serve_quality_index, return_quality_index
mutates: none
---

---
name: SURFACE_KEYWORDS
type: variable
file: fetchers/tennis.py
purpose: Dict mapping surface names (clay/grass/hard) to lists of tournament name keywords for surface inference.
inputs: none
outputs: dict[str, list[str]]
calls: none
called_by: infer_surface
mutates: none
---

---
name: infer_surface
type: function
file: fetchers/tennis.py
purpose: Infers court surface (clay/grass/hard) from a tournament name string by checking SURFACE_KEYWORDS. Returns 'hard' as default.
inputs: text: str
outputs: str
calls: none
called_by: fetch_tennis_context
mutates: none
---

---
name: _elo_from_ranking
type: function
file: fetchers/tennis.py
purpose: Converts ATP/WTA ranking to an approximate Elo seed: Rank 1 ≈ 2400, Rank 50 ≈ 2000, Rank 500+ → 1300 floor.
inputs: ranking: int
outputs: float
calls: math.log10
called_by: fetch_tennis_context
mutates: none
---

---
name: serve_quality_index
type: function
file: fetchers/tennis.py
purpose: Computes a composite serve quality score normalized to 100 = tour average, from first serve %, first serve won %, and second serve won %. Analogous to wRC+ in baseball.
inputs: first_serve_pct: float, first_won_pct: float, second_won_pct: float, tour: str
outputs: float (100 = tour average)
calls: none
called_by: fetch_tennis_context
mutates: none
---

---
name: return_quality_index
type: function
file: fetchers/tennis.py
purpose: Computes return quality normalized to 100 = tour average from break points converted percentage.
inputs: bp_converted_pct: float, tour: str
outputs: float (100 = tour average)
calls: none
called_by: fetch_tennis_context
mutates: none
---

---
name: _espn_get
type: function
file: fetchers/tennis.py
purpose: GET request to any ESPN URL with browser User-Agent; on failure falls back to load_cached("tennis_atp.json") or ("tennis_wta.json") depending on which base URL was called.
inputs: url: str, params: dict
outputs: dict
calls: httpx.Client.get, load_cached
called_by: _espn_recent_matches, _search_espn_athlete, _espn_athlete_stats
mutates: none
---

---
name: _tsdb_get
type: function
file: fetchers/tennis.py
purpose: GET request to TheSportsDB API; returns parsed JSON or {} on failure.
inputs: path: str, params: dict
outputs: dict
calls: httpx.Client.get
called_by: _tsdb_search_player, _tsdb_player_last5, fetch_tennis_context
mutates: none
---

---
name: _f
type: function
file: fetchers/tennis.py
purpose: Safe float conversion for ESPN/TSDB fields; returns default on None or parse error.
inputs: val: any, default: float = 0.0
outputs: float
calls: float
called_by: _extract_serve_stats, _tsdb_player_last5
mutates: none
---

---
name: _parse_espn_tennis_events
type: function
file: fetchers/tennis.py
purpose: Filters and normalizes ESPN scoreboard response into completed match dicts for a specific player name.
inputs: data: dict, name_lower: str
outputs: list[dict]
calls: none
called_by: _espn_recent_matches
mutates: none
---

---
name: _espn_recent_matches
type: function
file: fetchers/tennis.py
purpose: Fetches the last 5 completed matches for a player from ESPN ATP/WTA scoreboard over the past N days.
inputs: name: str, tour: str, days: int = 60
outputs: list[dict]
calls: _espn_get, _parse_espn_tennis_events
called_by: fetch_tennis_context
mutates: none
---

---
name: _search_espn_athlete
type: function
file: fetchers/tennis.py
purpose: Fuzzy-searches ESPN athletes list for a player by name and returns the matching athlete dict or None.
inputs: name: str, tour: str
outputs: Optional[dict]
calls: _espn_get
called_by: fetch_tennis_context
mutates: none
---

---
name: _espn_athlete_stats
type: function
file: fetchers/tennis.py
purpose: Fetches serve and return statistics for an ESPN athlete ID from the /statistics endpoint.
inputs: athlete_id: str, tour: str
outputs: dict {first_serve_pct, first_won_pct, second_won_pct, bp_converted_pct, aces}
calls: _espn_get, _extract_serve_stats
called_by: fetch_tennis_context
mutates: none
---

---
name: _extract_serve_stats
type: function
file: fetchers/tennis.py
purpose: Normalizes raw ESPN statistics name/value array into a consistent serve/return stats dict.
inputs: raw: list
outputs: dict {first_serve_pct, first_won_pct, second_won_pct, bp_converted_pct, aces}
calls: _f
called_by: _espn_athlete_stats
mutates: none
---

---
name: _tsdb_search_player
type: function
file: fetchers/tennis.py
purpose: Searches TheSportsDB for a tennis player by name and returns the first matching player dict.
inputs: name: str
outputs: Optional[dict]
calls: _tsdb_get
called_by: fetch_tennis_context
mutates: none
---

---
name: _tsdb_player_last5
type: function
file: fetchers/tennis.py
purpose: Retrieves the last 5 completed match results for a TSDB player ID via eventsplayer.php.
inputs: player_id: str
outputs: list[dict]
calls: _tsdb_get, _normalize_tennis_results
mutates: none
---

---
name: _normalize_tennis_results
type: function
file: fetchers/tennis.py
purpose: Converts raw TSDB or ESPN event dicts into a consistent format with date, player_a, player_b, winner, and surface fields.
inputs: raw: list[dict]
outputs: list[dict]
calls: infer_surface
called_by: _tsdb_player_last5
mutates: none
---

---
name: fetch_tennis_context
type: function
file: fetchers/tennis.py
purpose: Main tennis data entry point — fetches serve/return stats, surface win rate, recent form, H2H, and last-5 results for two players from ESPN and TheSportsDB. Returns full context dict.
inputs: player_a: str, player_b: str, surface: str, tour: str
outputs: dict {player_a, player_b, surface, tour, h2h, last5_a, last5_b, sources}
calls: _tsdb_search_player, _search_espn_athlete, _espn_athlete_stats, _espn_recent_matches, _tsdb_player_last5, serve_quality_index, return_quality_index
called_by: run_tennis_analysis
mutates: none
---

---

## ai_agent_tennis.py

---
name: MODEL
type: variable
file: ai_agent_tennis.py
purpose: Claude model identifier used for tennis AI calls (claude-haiku-4-5-20251001).
inputs: none
outputs: str
calls: none
called_by: parse_tennis_query, interpret_tennis_signals, generate_tennis_narrative
mutates: none
---

---
name: _client
type: function
file: ai_agent_tennis.py
purpose: Creates and returns an authenticated Anthropic client from ANTHROPIC_API_KEY; raises RuntimeError if key missing.
inputs: none
outputs: anthropic.Anthropic
calls: os.environ.get, anthropic.Anthropic
called_by: parse_tennis_query, interpret_tennis_signals, generate_tennis_narrative
mutates: none
---

---
name: parse_tennis_query
type: function
file: ai_agent_tennis.py
purpose: Sends user's tennis query to Claude and returns structured JSON with player names, surface, tour, date, and notes.
inputs: user_text: str
outputs: dict {player_a, player_b, surface, tour, date, notes}
calls: _client, client.messages.create, json.loads, re.sub
called_by: run_tennis_analysis
mutates: none
---

---
name: interpret_tennis_signals
type: function
file: ai_agent_tennis.py
purpose: Fallback when ESPN/TSDB is unreachable — Claude estimates ranking, surface win rate, recent form, serve quality, and return quality from training knowledge.
inputs: player_a: str, player_b: str, surface: str, tour: str, notes: str
outputs: dict {player_a signals, player_b signals, h2h_advantage, confidence, notes}
calls: _client, client.messages.create, json.loads, re.sub
called_by: run_tennis_analysis (fallback path)
mutates: none
---

---
name: generate_tennis_narrative
type: function
file: ai_agent_tennis.py
purpose: Sends serve/return quality, surface win rates, H2H, and probabilities to Claude; returns a ≤90-word analytical narrative.
inputs: player_a: str, player_b: str, prob_a: float, prob_b: float, explanation: str, context: dict
outputs: str
calls: _client, client.messages.create
called_by: run_tennis_analysis
mutates: none
---

---

## analyze_tennis.py

---
name: _elo_from_ranking
type: function
file: analyze_tennis.py
purpose: Converts ATP/WTA ranking to a Glicko-2 seed rating: Rank 1 ≈ 2400, Rank 50 ≈ 2000, Rank 500+ → 1300 floor.
inputs: ranking: int
outputs: float
calls: math.log10
called_by: run_tennis_analysis
mutates: none
---

---
name: _logistic
type: function
file: analyze_tennis.py
purpose: Logistic sigmoid on x/scale — maps any real number to (0,1).
inputs: x: float, scale: float = 40.0
outputs: float
calls: math.exp
called_by: _compute_point_probs
mutates: none
---

---
name: _rest_days
type: function
file: analyze_tennis.py
purpose: Returns days since the player's last completed match by comparing last5[0]["date"] to game_date. Returns 99 (= no penalty applied) when last5 is empty or dates are unparseable.
inputs: last5: list[dict], game_date: str (ISO "YYYY-MM-DD")
outputs: int (0 = same day, 1 = next day, 2+ = rested, 99 = unknown)
calls: datetime.fromisoformat
called_by: run_tennis_analysis
mutates: none
---

---
name: SQI_PENALTY_SAME_DAY / SQI_PENALTY_NEXT_DAY
type: constant
file: analyze_tennis.py
purpose: Tunable rest-day SQI penalty multipliers. SAME_DAY=0.96 (−4% for 0 days rest), NEXT_DAY=0.99 (−1% for 1 day rest). Conservative starting values — Kimi/Gemini recommended these over the initial 0.92/0.97 until backtesting data confirms larger penalties.
---

---
name: _sqi_rest_factor
type: function
file: analyze_tennis.py
purpose: Returns SQI multiplier for rest-day fatigue using named constants: 0 days = SQI_PENALTY_SAME_DAY (−4%), 1 day = SQI_PENALTY_NEXT_DAY (−1%), 2+ days = 1.0 (no penalty). Captures measurable serve quality drop when players compete on consecutive or same days. Penalty logged as rest_days signal for future calibration.
inputs: rest_days: int
outputs: float (SQI_PENALTY_SAME_DAY | SQI_PENALTY_NEXT_DAY | 1.0)
calls: none
called_by: run_tennis_analysis
mutates: none
---

---
name: _compute_point_probs
type: function
file: analyze_tennis.py
purpose: Compute P_serve (A wins point on A's serve) and P_return (A wins point on B's serve). SQI, RQI, surface win rate, and form all feed in as modifiers to the logistic — not blended as external percentages. SQI values passed in have already been adjusted for rest-day fatigue by _sqi_rest_factor.
inputs: sqi_a, rqi_a, sqi_b, rqi_b: float (SQI/RQI centred on 100, rest-adjusted); swr_a, swr_b, form_a, form_b: float; surface: str
outputs: tuple[float, float] — (p_serve, p_return)
calls: _logistic
called_by: run_tennis_analysis
mutates: none
---

---
name: _markov_game_prob
type: function
file: analyze_tennis.py
purpose: P(A wins one tennis game) given who is serving. DP over (score_a, score_b); deuce closed form p²/(p²+q²).
inputs: p_serve: float, p_return: float, a_serving: bool
outputs: float
calls: lru_cache DP, _logistic implicitly
called_by: _markov_set_prob
mutates: none
---

---
name: _markov_set_prob
type: function
file: analyze_tennis.py
purpose: P(A wins one set). First to 6, win by 2; tiebreak at 6-6 approximated as average of p_serve/p_return.
inputs: p_serve: float, p_return: float, a_serves_first: bool
outputs: float
calls: _markov_game_prob, lru_cache DP
called_by: markov_tennis_match
mutates: none
---

---
name: markov_tennis_match
type: function
file: analyze_tennis.py
purpose: Nested Markov simulation points→games→sets→match. Averages over both first-server possibilities. Returns prob_a, prob_b, p_serve, p_return, best_of.
inputs: p_serve: float, p_return: float, best_of: int = 3
outputs: dict {prob_a, prob_b, p_serve, p_return, best_of}
calls: _markov_set_prob, lru_cache DP
called_by: run_tennis_analysis
mutates: none
---

---
name: _build_plain_summary_tennis
type: function
file: analyze_tennis.py
purpose: Plain-English tennis bet recommendation in user-preferred phrasing — "<favourite> is gonna win vs <opponent> (XX% accuracy on <surface>). Bet on: straight sets, first set <player>." Gated: straight-sets only when win_prob >=0.70 (with set label adapting to best_of 3 vs 5); first-set bet when 0.55-0.70. Headlines: "gonna win" (>=65%), "slight favourite" (52-65%), "coin-flip" (<52%).
inputs: player_a: str, player_b: str, prob_a: float, prob_b: float, best_of: int, surface: str
outputs: str
calls: none
called_by: run_tennis_analysis
mutates: none
---

---
name: run_tennis_analysis
type: function
file: analyze_tennis.py
purpose: Full tennis pipeline: parse query → fetch ESPN/TSDB → _compute_point_probs → markov_tennis_match (nested Markov) → Glicko-2 validation (logged only) → confidence shrinkage → persist to DB → Kelly sizing → AI narrative → plain_summary in user-preferred phrasing → Market Efficiency Model recommendation.
inputs: user_query: str, bankroll: float = 1000.0
outputs: dict {match_id, player_a, player_b, recommendation, recommendation_reason, sport, tour, surface, date, plain_summary, prob_a, prob_b, data_confidence, markov_sim, player_stats, h2h, last5_a, last5_b, narrative, raw_sources, steps, …}
calls: parse_tennis_query, fetch_tennis_context, _compute_point_probs, markov_tennis_match, Glicko2Model, kelly_stake, log_signal, get_db, generate_tennis_narrative, _build_plain_summary_tennis
called_by: analyze_tennis (app.py)
mutates: matches, signals, predictions tables
---

---

## app.py additions (tennis)

---
name: TennisRequest
type: class
file: app.py
purpose: Pydantic request model for POST /analyze-tennis with query string and bankroll.
inputs: query: str, bankroll: float = 1000.0
outputs: none
calls: none
called_by: analyze_tennis
mutates: none
---

---
name: analyze_tennis
type: function
file: app.py
purpose: POST /analyze-tennis — runs the full tennis analysis pipeline from a natural language query.
inputs: body: TennisRequest
outputs: dict (full tennis analysis result)
calls: run_tennis_analysis
called_by: HTTP POST /analyze-tennis
mutates: matches, signals, predictions tables
---

## fetchers/table_tennis.py

---
name: fetch_table_tennis_context
type: function
file: fetchers/table_tennis.py
purpose: Fetch table tennis match context from TheSportsDB. Returns player stats, last10 results, H2H summary.
inputs: player_a: str, player_b: str, tour: str = "ittf"
outputs: dict {player_a, player_b, h2h, sources}
calls: _tsdb_search_player, _tsdb_player_last10, _normalize_tt_results, _tsdb_get
called_by: run_table_tennis_analysis
mutates: none
---

---
name: attack_quality_index
type: function
file: fetchers/table_tennis.py
purpose: Composite AQI — 100=tour average. Built from attack win rate and 3rd ball win rate.
inputs: attack_win_rate: float|None, third_ball_win_rate: float|None
outputs: float
calls: none
called_by: fetch_table_tennis_context
mutates: none
---

---
name: return_quality_index
type: function
file: fetchers/table_tennis.py
purpose: RQI — 100=tour average. Built from return point win rate.
inputs: return_win_rate: float|None
outputs: float
calls: none
called_by: fetch_table_tennis_context
mutates: none
---

## ai_agent_table_tennis.py

---
name: parse_table_tennis_query
type: function
file: ai_agent_table_tennis.py
purpose: Claude Haiku — extract player_a, player_b, tour, date, notes from natural language query.
inputs: user_text: str
outputs: dict
calls: anthropic.messages.create
called_by: run_table_tennis_analysis
mutates: none
---

---
name: interpret_table_tennis_signals
type: function
file: ai_agent_table_tennis.py
purpose: AI fallback — Claude estimates AQI, RQI, ranking, form, style when TSDB returns no data.
inputs: player_a, player_b, tour, notes
outputs: dict {player_a, player_b, h2h_advantage, style_edge, notes}
calls: anthropic.messages.create
called_by: run_table_tennis_analysis (fallback path)
mutates: none
---

---
name: generate_table_tennis_narrative
type: function
file: ai_agent_table_tennis.py
purpose: Claude Haiku — generate 2-3 sentence prediction narrative for a table tennis match.
inputs: player_a, player_b, prob_a, prob_b, explanation, context
outputs: str
calls: anthropic.messages.create
called_by: run_table_tennis_analysis
mutates: none
---

## analyze_table_tennis.py

---
name: _build_plain_summary_tt
type: function
file: analyze_table_tennis.py
purpose: Plain-English table-tennis bet recommendation in user-preferred phrasing — "<favourite> is gonna win vs <opponent> (XX% accuracy). Bet on: 4-0 sweep, -1.5 sets handicap." Gated: 4-0 sweep only when win_prob >=0.75; handicap when 0.60-0.75. Headlines: "gonna win" (>=60%), "slight favourite" (52-60%), "coin-flip" (<52%).
inputs: player_a: str, player_b: str, prob_a: float, prob_b: float
outputs: str
calls: none
called_by: run_table_tennis_analysis
mutates: none
---

---
name: run_table_tennis_analysis
type: function
file: analyze_table_tennis.py
purpose: Full TT pipeline: parse → ITTF/WTT/Setka/TSDB fetch → AQI/RQI → Markov Chain sim → handedness → first-time premium → form → fatigue → line_movement → Glicko-2 → Bayesian prior shrinkage → market efficiency model → persist → Kelly → narrative → plain_summary in user-preferred phrasing.
inputs: user_query: str, bankroll: float, open_odds_a/b: float?, curr_odds_a/b: float?, matches_today_a/b: int
outputs: dict with match_id, player_a/b, recommendation, recommendation_reason, plain_summary, prob_a/b, data_confidence, player_stats, h2h, markov_sim, narrative, market_comparison, kelly_edge, steps
notes: Kelly now uses real odds (curr > open > 1.909 default); recommendation uses 5pp value gate when real odds provided (PASS if no edge); market_comparison same shape as baseball
calls: parse_table_tennis_query, fetch_table_tennis_context, interpret_table_tennis_signals, markov_match_prob, Glicko2Model, kelly_stake, american_to_decimal, market_edge_summary, generate_table_tennis_narrative, log_signal, get_db, _build_plain_summary_tt
called_by: analyze_table_tennis endpoint (app.py)
mutates: matches, signals, predictions tables
---

---
name: _attack_return_win_prob
type: function
file: analyze_table_tennis.py
purpose: Returns (prob_a, p_serve, p_return) — separate logistic win probs for serve points and return points, with style-adjusted AQI inputs.
inputs: aqi_a, rqi_a, aqi_b, rqi_b: float, style_a/b: str
outputs: tuple[float, float, float]
calls: _logistic, _style_aqi_modifier
called_by: run_table_tennis_analysis
mutates: none
---

---
name: _markov_game_prob
type: function
file: analyze_table_tennis.py
purpose: Markov Chain DP over all (points_a, points_b, serve_turn) game states to compute P(A wins one game to 11). Handles deuce via closed-form formula.
inputs: p_serve: float, p_return: float
outputs: float — P(A wins the game)
calls: functools.lru_cache
called_by: markov_match_prob
mutates: none
---

---
name: markov_match_prob
type: function
file: analyze_table_tennis.py
purpose: Simulate a best-of-N TT match using per-game Markov probability. Returns prob_a/b, per-game win %, score distribution, expected total games.
inputs: p_serve: float, p_return: float, best_of: int = 7
outputs: dict {prob_a, prob_b, game_prob_a, dist, expected_games}
calls: _markov_game_prob
called_by: run_table_tennis_analysis
mutates: none
---

---
name: _first_time_premium
type: function
file: analyze_table_tennis.py
purpose: +3pp nudge toward the player with unconventional style (penhold/chopper/long pips) when H2H=0 — no film means opponent can't adapt.
inputs: h2h_wins_a: int, h2h_wins_b: int, style_a: str, style_b: str
outputs: float nudge in [-0.03, 0.03]
calls: none
called_by: run_table_tennis_analysis
mutates: none
---

---
name: _style_aqi_modifier
type: function
file: analyze_table_tennis.py
purpose: Returns a multiplier (0.90–1.0) that suppresses attacker's effective AQI when facing a defender/chopper — style interacts at model input, not as post-hoc nudge.
inputs: style_attacker: str, style_defender: str
outputs: float multiplier
calls: none
called_by: _attack_return_win_prob
mutates: none
---

---
name: _fatigue_decay
type: function
file: analyze_table_tennis.py
purpose: Exponential performance decay from intraday match load. f(n)=exp(-0.12*max(0,n-2)). n=3→0.887, n=4→0.787, n=5→0.698.
inputs: matches_played: int
outputs: float (0,1]
calls: math.exp
called_by: _fatigue_adjustment
mutates: none
---

---
name: _fatigue_adjustment
type: function
file: analyze_table_tennis.py
purpose: Converts per-player exponential decay to a centred probability nudge. Returns prob nudge for prob_a (positive = A is fresher). Replaces old linear 3pp/match rule.
inputs: matches_today_a: int, matches_today_b: int
outputs: float in [-0.5, 0.5]
calls: _fatigue_decay
called_by: run_table_tennis_analysis
mutates: none
---

---
name: _line_movement_edge
type: function
file: analyze_table_tennis.py
purpose: Sharp money signal from line movement. Threshold 5pp for club circuits (Setka/TT Cup/ukr_dl/czk_dl), 10pp for ITTF/WTT. Returns nudge to prob_a capped at ±8pp.
inputs: open_a/b: float?, curr_a/b: float?, circuit: str = "ittf"
outputs: float nudge
calls: none
called_by: run_table_tennis_analysis
mutates: none
---

---
name: analyze_table_tennis
type: function
file: app.py
purpose: POST /analyze-table-tennis — runs full table tennis pipeline from natural language query.
inputs: body: TableTennisRequest
outputs: dict (full analysis result)
calls: run_table_tennis_analysis
called_by: HTTP POST /analyze-table-tennis
mutates: matches, signals, predictions tables
---

---

## fetchers/setka.py

---
name: setka_search_player
type: function
file: fetchers/setka.py
purpose: Search tabletennis.setkacup.com/en/participants for a club-circuit player by name. Returns {name, player_id, profile_url, source} or None.
inputs: name: str
outputs: Optional[dict]
calls: _get, BeautifulSoup
called_by: lookup_club_tt_player
mutates: none
---

---
name: setka_player_profile
type: function
file: fetchers/setka.py
purpose: Scrape Setka Cup player profile for recent form (win rate), recent_n, ranking.
inputs: player_id: str
outputs: dict {recent_form?, recent_n?, ranking?, recent_matches?}
calls: _get, BeautifulSoup
called_by: lookup_club_tt_player
mutates: none
---

---
name: ttcup_search_player
type: function
file: fetchers/setka.py
purpose: Search tt-cup.com for a player. Returns {name, player_id, profile_url, source} or None.
inputs: name: str
outputs: Optional[dict]
calls: _get, BeautifulSoup
called_by: lookup_club_tt_player
mutates: none
---

---
name: ttcup_player_profile
type: function
file: fetchers/setka.py
purpose: Scrape TT Cup player profile for win rate and recent results.
inputs: player_id: str
outputs: dict {recent_form?, recent_n?}
calls: _get
called_by: lookup_club_tt_player
mutates: none
---

---
name: lookup_club_tt_player
type: function
file: fetchers/setka.py
purpose: Unified lookup for Eastern European club TT players — tries Setka Cup then TT Cup. Used as Step 2 in the TT fetcher pipeline before TSDB fallback.
inputs: name: str
outputs: dict {name, recent_form?, recent_n?, ranking?, source, profile_url}
calls: setka_search_player, setka_player_profile, ttcup_search_player, ttcup_player_profile
called_by: fetch_table_tennis_context (fetchers/table_tennis.py)
mutates: none
---

---
name: setka_matches_today
type: function
file: fetchers/setka.py
purpose: Count how many matches a player has completed today on Setka Cup. Used for intraday fatigue calculation.
inputs: player_id: str, match_date: str? (ISO "YYYY-MM-DD")
outputs: int (0–8)
calls: _get
called_by: get_matches_today
mutates: none
---

---
name: ttcup_matches_today
type: function
file: fetchers/setka.py
purpose: Count how many matches a TT Cup player has completed today.
inputs: player_id: str, match_date: str?
outputs: int (0–8)
calls: _get
called_by: get_matches_today
mutates: none
---

---
name: get_matches_today
type: function
file: fetchers/setka.py
purpose: Look up a club TT player's intraday match count. Tries Setka Cup then TT Cup. Called automatically by run_table_tennis_analysis when matches_today=0 and circuit is club.
inputs: name: str, match_date: str?
outputs: int
calls: setka_search_player, setka_matches_today, ttcup_search_player, ttcup_matches_today
called_by: run_table_tennis_analysis
mutates: none
---

---
name: lookup_player_profile
type: function
file: fetchers/setka.py
purpose: Return style/grip/hand for a circuit player from data/tt_player_profiles.json. Exact match first, then token-overlap fuzzy (≥2 tokens). Called when context returns default "all-round" style.
inputs: name: str
outputs: dict {style, grip, hand} or {}
calls: _load_profiles (lazy-loaded JSON cache)
called_by: run_table_tennis_analysis (step 2b)
mutates: none
---

---
name: tt_player_profiles.json
type: variable
file: data/tt_player_profiles.json
purpose: Static style/grip/hand profile dictionary for ~100 Setka Cup and TT Cup circuit regulars. Keyed by full player name. Activates style and handedness signals for players invisible to ITTF/WTT/TSDB.
inputs: none
outputs: JSON dict
calls: none
called_by: lookup_player_profile
mutates: none (static file — update when new regulars join the circuit)
---

---

## fetchers/results_collector.py

---
name: espn_fetch_baseball_result
type: function
file: fetchers/results_collector.py
purpose: Look up a completed MLB game result from the ESPN scoreboard. Resolves team names via _match_team, finds the game in the scoreboard events for match_date, checks completion status, and extracts final run scores for each team.
inputs: team_a, team_b: str, match_date: str (ISO "YYYY-MM-DD")
outputs: Optional[dict {result, score_a, score_b, source}]
calls: fetchers.baseball._get_scoreboard, _all_teams, _match_team
called_by: fetch_match_result
mutates: none
---

---
name: fetch_match_result
type: function
file: fetchers/results_collector.py
purpose: Unified entry point — routes to ESPN MLB scoreboard for baseball; tries Setka Cup then TT Cup for table tennis; returns None for tennis/soccer (not yet implemented). Returns {result, score_a, score_b, source} or None.
inputs: player_a, player_b: str, match_date: str (ISO), sport: str, tour: str
outputs: Optional[dict]
calls: espn_fetch_baseball_result, setka_fetch_result, ttcup_fetch_result
called_by: run_auto_resolve
mutates: none
---

---
name: setka_fetch_result
type: function
file: fetchers/results_collector.py
purpose: Fetch a Setka Cup match result via H2H page then player profile fallback. Parses score, determines winner, handles name-position ambiguity.
inputs: player_a, player_b: str, match_date: str, tolerance_days: int = 1
outputs: Optional[dict {result, score_a, score_b, source, raw}]
calls: setka_search_player, setka_h2h, setka_player_profile
called_by: fetch_match_result
mutates: none
---

---
name: ttcup_fetch_result
type: function
file: fetchers/results_collector.py
purpose: Fetch a TT Cup match result via H2H page then player profile fallback.
inputs: player_a, player_b: str, match_date: str, tolerance_days: int = 1
outputs: Optional[dict]
calls: ttcup_search_player, _get
called_by: fetch_match_result
mutates: none
---

## tasks/auto_resolve.py

---
name: run_auto_resolve
type: function
file: tasks/auto_resolve.py
purpose: Scan DB for unresolved predictions past scheduled_at. Fetch actual results and record outcomes automatically. Supports: baseball (ESPN scoreboard), table_tennis (Setka/TT Cup). Tennis and soccer are skipped (manual outcome recording required). dry_run=True fetches but doesn't write.
inputs: dry_run: bool = False
outputs: dict {attempted, resolved, failed, skipped, details}
calls: _pending_matches, fetch_match_result, record_outcome
called_by: resolve_pending (app.py), startup thread, CLI
mutates: outcomes table
---

---
name: _signal_accuracy_summary
type: function
file: tasks/auto_resolve.py
purpose: After outcomes accumulate, compute per-signal accuracy lift. Shows which signals (AQI, RQI, form, fatigue...) actually correlate with correct predictions. Guides blend weight tuning.
inputs: none
outputs: dict {signal_name: {n, accuracy_high, accuracy_low, lift}} sorted by |lift|
calls: get_db
called_by: signal_accuracy (app.py)
mutates: none
---

## fetchers/ittf.py

---
name: wtt_search_player
type: function
file: fetchers/ittf.py
purpose: Search worldtabletennis.com/playerslist for a player by name; returns {name, ittf_id, ranking, nationality} or None.
inputs: name: str
outputs: Optional[dict]
calls: _get, BeautifulSoup
called_by: lookup_tt_player
mutates: none
---

---
name: wtt_player_profile
type: function
file: fetchers/ittf.py
purpose: Scrape worldtabletennis.com/playerProfile/{id} for ranking, nationality, and season win rate.
inputs: ittf_id: str
outputs: dict {ranking?, nationality?, season_win_rate?, season_matches?}
calls: _get, BeautifulSoup
called_by: lookup_tt_player
mutates: none
---

---
name: ittf_search_player
type: function
file: fetchers/ittf.py
purpose: Search results.ittf.link for a player via POST form; returns {name, ittf_seq, ittf_id, profile_url} or None.
inputs: name: str
outputs: Optional[dict]
calls: httpx.Client.post, _get, BeautifulSoup
called_by: lookup_tt_player
mutates: none
---

---
name: ittf_player_profile
type: function
file: fetchers/ittf.py
purpose: Scrape ITTF player profile page for ranking and recent match history (up to 20 matches).
inputs: ittf_seq: str, ittf_id: str
outputs: dict {ranking?, recent_matches, recent_form, recent_n}
calls: _get, BeautifulSoup
called_by: lookup_tt_player
mutates: none
---

---
name: ittf_h2h
type: function
file: fetchers/ittf.py
purpose: Fetch H2H record from results.ittf.link/head-to-head between two player IDs.
inputs: ittf_id_a: str, ittf_id_b: str
outputs: dict {wins_a, wins_b, matches: list}
calls: _get, BeautifulSoup
called_by: fetch_table_tennis_context
mutates: none
---

---
name: lookup_tt_player
type: function
file: fetchers/ittf.py
purpose: Unified player lookup — tries WTT first for ranking, then ITTF results for match history; merges best available data.
inputs: name: str
outputs: dict {name, ittf_id, wtt_id, ranking, recent_form, nationality, source, ...}
calls: wtt_search_player, wtt_player_profile, ittf_search_player, ittf_player_profile
called_by: fetch_table_tennis_context
mutates: none
---



---

## Math & Models Reference

> Complete mathematical formulas for every model and signal in Predicta.
> This section exists so an external AI (e.g. Gemini) can ingest the full
> quantitative framework and contribute predictions or model improvements.

---

### 1. Elo Rating System  (`models/elo.py`)

**Expected score (win probability):**
```
E(A) = 1 / (1 + 10^((R_B - R_A) / 400))
E(B) = 1 - E(A)
```
Where `R_A`, `R_B` are current Elo ratings.  Default = 1500 for unknown teams.

**Rating update after a match:**
```
R'_A = R_A + K × M × (S_A - E(A))
R'_B = R_B + K × M × (S_B - E(B))
```
- `S_A` = 1 if A wins, 0 if B wins, 0.5 draw  
- `K` = importance K-factor:  
  - Grand Slam / playoff: 60  
  - International / major: 50  
  - Default: 30  
- `M` = goal-difference multiplier `_goal_diff_multiplier(|score_A - score_B|)`:  
  - |diff|=0 → 1.0  
  - |diff|=1 → 1.0  
  - |diff|=2 → 1.5  
  - |diff|=3 → 1.75  
  - |diff|≥4 → 1.75 + (|diff| - 3) × 0.5 (capped at ~2.25+)

**Seeding Elo from current-season win%** (baseball pipeline):
```
R_seeded = 1500 - 400 × log10((1 - win_pct) / win_pct)
```
A team with .600 win% gets ≈ 1572 Elo; .400 gets ≈ 1428.

---

### 2. Glicko-2 Rating System  (`models/glicko.py`)

**Win probability (logistic approximation):**
```
E(A,B) = 1 / (1 + exp(-g(RD_comb) × (r_A - r_B) / 400))
```
Where:
```
g(RD) = 1 / sqrt(1 + 3 × RD² / π²)
RD_comb = sqrt(RD_A² + RD_B²)
```
- `r_A`, `r_B` = Glicko-2 ratings on the Elo scale (centred ~1500)  
- `RD` = Rating Deviation — uncertainty (starts ~200, shrinks toward ~50 with games played)  
- Volatility σ governs how much RD grows between rating periods (default 0.06)

**Rating update:**  Full Glicko-2 step-6 algorithm via the `glicko2` package:  
1. Convert to internal scale `µ = (r - 1500)/173.7178`  
2. Compute estimated variance `v` and improvement `∆` from match outcomes  
3. Update volatility `σ'` via iterative Illinois algorithm  
4. Update RD: `φ' = sqrt((φ*² + σ'²) × (1/v))`  
5. Update rating: `µ' = µ + φ'² × ∆`  
6. Convert back to Elo scale  

Used for: tennis (per surface: clay / grass / hard) and table tennis.

---

### 3. Poisson Run Model  (`models/baseball_market.py`, `fetchers/baseball.py`)

#### 3a. Constants
```
LEAGUE_AVG_RUNS     = 4.50   (2024 MLB runs/team/game)
LEAGUE_AVG_FIP      = 4.00   (2024 MLB starter FIP)
LEAGUE_BULLPEN_FIP  = 4.40   (MLB bullpens slightly worse)
LEAGUE_AVG_WRC_PLUS = 100.0  (by definition — 100 = average)
FIP_CONSTANT        = 3.20   (calibration constant, aligns FIP to ERA scale)
STARTER_FRAC        = 5/9    ≈ 0.5556  (starter covers ~5 of 9 innings)
BULLPEN_FRAC        = 4/9    ≈ 0.4444  (bullpen covers remaining ~4 innings)
HOME_BOOST          = 1.03   (home team runs +3% for home field advantage)
```

#### 3b. Fielding Independent Pitching (FIP)
```
FIP = ((13 × HR + 3 × BB - 2 × K) / IP) + 3.20
```
- Controls for defense: only counts outcomes the pitcher directly controls  
- HR heavily penalised (13×) because each HR guarantees ≥1 run  
- Walks add baserunners (3×); Strikeouts remove them (-2×)  
- HBP excluded (minor — ESPN doesn't track separately)  
- `IP` = innings pitched converted from ESPN notation:  
  `IP_decimal = floor(IP) + (tenths_digit / 3)` when tenths ∈ {1,2}

**ERA calculation** (from ESPN component stats when pre-computed value is absent):
```
ERA = (earnedRuns × 9) / fullInningsPlayed
```

**WHIP calculation:**
```
WHIP = (H + BB) / IP
```
Lower WHIP = fewer baserunners per inning; good starter ≈ 1.10–1.25.

#### 3c. Bullpen FIP Derivation
```
team_ERA ≈ (starter_FIP × 5 + bullpen_FIP × 4) / 9
=> bullpen_FIP = (team_ERA × 9 - starter_FIP × 5) / 4
```
Clamped to `[3.0, 7.5]`.  If `team_ERA ≤ 0`, fallback to `LEAGUE_BULLPEN_FIP = 4.40`.

#### 3d. wRC+ from OBP/SLG (improved approximation)
```
wRC+ ≈ round(((2 × OBP + SLG) / 1.045) × 100)    when OBP and SLG are both available
wRC+ ≈ round((OPS / 0.730) × 100)                  fallback when only OPS is available
```
- 1.045 = 2×0.315 + 0.415 = 2025-26 MLB average (2×OBP + SLG)
- 2×OBP+SLG weights OBP more heavily — a point of OBP is ~1.8× more valuable than a point of SLG in run creation
- Correlates ~0.97 with true wRC+ vs ~0.93 for raw OPS
- wRC+ = 100 → league average offense; 120 → 20% above average; 80 → 20% below average
- Used because ESPN provides OPS, OBP, and SLG but not wRC+ directly

#### 3e. Platoon Adjustment
```
wRC+_adj = wRC+ × 1.05    (if opposing starter throws Left-handed)
wRC+_adj = wRC+ × 1.00    (if opposing starter throws Right-handed)
```
Rationale: ~65% of MLB lineups are Right-handed batters, who have a statistical
advantage against LHP starters (≈+5% wRC+).

#### 3f. Expected Runs (Split F5/L4)
```
off  = wRC+_adj / 100.0
home = 1.03 if home else 1.0
base = LEAGUE_AVG_RUNS × off × park_factor × home

# Dynamic starter fraction (Kimi P0 fix — uses actual depth prediction per starter):
if avg_ip_per_start is known (GS >= 3):
    starter_frac = clamp(avg_ip, 3.0, 7.0) / 9.0
    bullpen_frac = 1.0 - starter_frac
else:
    starter_frac = 5/9 = 0.556   (default)
    bullpen_frac = 4/9 = 0.444

mu_f5    = base × starter_frac × (starter_FIP / LEAGUE_AVG_FIP)
mu_l4    = base × bullpen_frac × (bullpen_FIP / LEAGUE_AVG_FIP)
mu_total = mu_f5 + mu_l4
```
Clamped: `mu_f5 ∈ [0.5, 6.0]`, `mu_l4 ∈ [0.4, 5.0]`, `mu_total ∈ [1.5, 10.0]`

avg_ip_per_start = `starter["innings_pitched"] / starter["games_started"]` (ESPN season stats).
Minimum GS=3 before using dynamic fraction; otherwise defaults to 5/9.

**Interpretation:** A team with wRC+=110 facing a FIP=3.50 starter at a neutral park:
```
off = 1.10
base = 4.50 × 1.10 × 1.00 × 1.00 = 4.95
mu_f5 = 4.95 × 0.5556 × (3.50/4.00) = 2.41
mu_l4 = 4.95 × 0.4444 × (4.40/4.00) = 2.42
mu_total = 4.83 runs
```

#### 3g. Park Factors (`PARK_FACTORS` dict)
3-year run-factor multipliers relative to neutral (1.00):
- Coors Field (COL): 1.19 — highest run environment in MLB  
- Oracle Park (SF): 0.92 — lowest run environment  
- Applied to `base` run calculation before FIP adjustment

#### 3h. Score Matrix (Poisson joint distribution)
```
P(home=i, away=j) = Poisson(i; mu_home) × Poisson(j; mu_away)
```
Matrix is `(MAX_RUNS+1) × (MAX_RUNS+1)` where `MAX_RUNS=20`.  
Independent Poisson — no Dixon-Coles correction for baseball.

#### 3i. Moneyline Market
```
p_home_reg = sum of matrix cells where home_runs > away_runs
p_away_reg = sum of matrix cells where away_runs > home_runs
p_extras   = sum of diagonal (tie at end of 9)

p_home = p_home_reg + p_extras × 0.52   (home wins extras 52% — slight home edge)
p_away = p_away_reg + p_extras × 0.48

Normalise: p_home + p_away = 1.0
```

#### 3j. Run Line Market (±1.5)
For each cell `(i,j)` in the score matrix:
```
if (i - j) >  1.5  → home covers
if (i - j) < -1.5  → away covers
if |i - j| == 1.5  → push (impossible with integers; handled for ±2.5 lines)
```

#### 3k. Totals (O/U)
```
P(over L)  = sum of cells where (home_runs + away_runs) > L
P(under L) = sum of cells where (home_runs + away_runs) < L
P(push L)  = sum of cells where (home_runs + away_runs) = L
```
Multiple lines computed: 7.5, 8.0, 8.5, 9.0, 9.5.

#### 3l. NRFI / YRFI (No/Yes Run First Inning)
```
mu_first_inning = (mu_home + mu_away) / 9  (rough per-inning split)
P(NRFI) = P(home 1st-inn = 0) × P(away 1st-inn = 0)
         = Poisson(0; mu_home/9) × Poisson(0; mu_away/9)
         = e^(-mu_home/9) × e^(-mu_away/9)
P(YRFI) = 1 - P(NRFI)
```

#### 3m. Elo Blend (final probability)
```
prob_a_final = 0.70 × prob_a_poisson + 0.30 × prob_a_elo
prob_b_final = 0.70 × prob_b_poisson + 0.30 × prob_b_elo
Normalise so prob_a_final + prob_b_final = 1.0
```
Elo is seeded from current-season win% (see §1 above), providing a momentum/form signal.

---

### 4. Dixon-Coles Soccer Model  (`models/dixon_coles.py`, `models/markets.py`)

**Expected goals:**
```
mu_home = attack_home × defense_away × league_avg × HOME_ADVANTAGE (1.15)
mu_away = attack_away × defense_home × league_avg
```
Where `attack` and `defense` are strength multipliers relative to league (1.0 = average).

**Dixon-Coles correction** (adjusts low-score probabilities):
```
ρ(i,j) adjustment factor for score (i,j):
  (0,0): 1 + mu_home × mu_away × TAU
  (1,0): 1 - mu_away × TAU
  (0,1): 1 - mu_home × TAU
  (1,1): 1 + TAU
  else:  1.0
TAU = 0.10
```

**Score probability:**
```
P(home=i, away=j) = Poisson(i; mu_home) × Poisson(j; mu_away) × ρ(i,j)
```

**Match result probabilities:**
```
P(home win) = Σ P(i,j) for i > j
P(draw)     = Σ P(i,j) for i = j  (trace)
P(away win) = Σ P(i,j) for j > i
```

**Corners market (Poisson):**
```
lambda_home = corners_for_home × corners_against_away / league_avg
lambda_away = corners_for_away × corners_against_home / league_avg
lambda_total = lambda_home + lambda_away
P(total > L) = 1 - Poisson.CDF(L; lambda_total)
```

---

### 5. Kelly Criterion  (`models/kelly.py`, `models/trading/kelly.py`)

**Full Kelly fraction:**
```
f* = (p × b - (1 - p)) / b
   = (p × b - q) / b
```
Where:
- `p` = model's estimated win probability  
- `q = 1 - p` = loss probability  
- `b = decimal_odds - 1` = net profit per unit staked  

**Edge:**
```
edge = p × b - (1 - p) = p - (1 / decimal_odds)
```
If `edge ≤ 0`: no bet recommended.

**Quarter-Kelly (applied fraction):**
```
f_applied = f* × KELLY_FRACTION   (KELLY_FRACTION = 0.25)
recommended_stake = f_applied × bankroll
```
Quarter-Kelly reduces variance significantly at the cost of ~6% long-run growth
versus full Kelly, making it appropriate for a prediction system still in calibration.

**Trading Kelly** (`models/trading/kelly.py`):
```
f* = (win_rate × avg_win_pct - (1 - win_rate) × avg_loss_pct) / avg_win_pct
```
Derived from signal score: `win_rate = (score + 100) / 200`, `avg_win_pct = score/200 × 0.05`.

---

### 6. Tennis Markov Chain  (`analyze_tennis.py`)

#### 6a. Point Probabilities

**Inputs:**
- `SQI_A` = Serve Quality Index, centred at 100 = tour average  
  `SQI = (first_serve_pct / AVG_FIRST_SERVE_PCT + first_won_pct / AVG_FIRST_WON_PCT + second_won_pct / AVG_SECOND_WON_PCT) / 3 × 100`  
- `RQI_A` = Return Quality Index, centred at 100  
  `RQI = (bp_converted / AVG_BP_CONVERTED × 0.6 + return_points_won_pct × 0.4) × 100`  
- `SWR_A` = surface win rate (e.g. 0.68 on grass)  
- `form_A` = recent form score (weighted rolling window, normalised 0–1)

**Surface serve amplifier:**
```
surf_serve_amp = 1.15 (grass) | 1.00 (hard) | 0.88 (clay)
```
Grass amplifies serve dominance; clay neutralises it.

**Effective SQI with adjustments:**
```
swr_ratio  = SWR_A / (SWR_A + SWR_B)
form_ratio = form_A / (form_A + form_B)
swr_adj    = (swr_ratio  - 0.5) × 20   (±10 max)
form_adj   = (form_ratio - 0.5) × 10   (±5 max)

SQI_A_eff = SQI_A × surf_serve_amp + swr_adj + form_adj
SQI_B_eff = SQI_B × surf_serve_amp - swr_adj - form_adj  (symmetric)
```

**Point probability via logistic:**
```
P_serve  = 1 / (1 + exp(-(SQI_A_eff - RQI_B) / 40))   (A serving, B returning)
P_return = 1 / (1 + exp(-(RQI_A - SQI_B_eff) / 40))   (B serving, A returning)
```
Scale=40 is calibrated so a 40-point SQI advantage ≈ +25pp win probability.

#### 6b. Game Markov Chain
```
State: (points_A, points_B) — standard tennis scoring 0/15/30/40/deuce
P(A wins game | A serving) = DP(0,0) with recursion:
  DP(pA, pB):
    if pA≥4 and pA-pB≥2: return 1.0
    if pB≥4 and pB-pA≥2: return 0.0
    if pA≥3 and pB≥3 (deuce):
      return p² / (p² + (1-p)²)   [closed form]
    return p × DP(pA+1,pB) + (1-p) × DP(pA,pB+1)
```
Where `p = P_serve` when A is serving, `p = P_return` when B is serving.

**Deuce closed form:**
```
P(A wins from deuce) = p² / (p² + (1-p)²)
```
This is the geometric series solution: A must win 2 consecutive points, with deuce re-entered on splits.

#### 6c. Set Markov Chain
```
State: (games_A, games_B, who_serves)
  if gA==6 and gB==6: tiebreak ≈ (P_serve + P_return)/2
  if gA≥6 and gA-gB≥2: A wins set
  if gB≥6 and gB-gA≥2: B wins set
  p_game = P(A wins game given current server)
  DP(gA,gB,server) = p_game × DP(gA+1,gB,flip) + (1-p_game) × DP(gA,gB+1,flip)
```
Serve alternates every game (`flip` = not current_server).

#### 6d. Match Markov Chain
```
State: (sets_A, sets_B, who_serves_set_first)
  sets_needed = ceil(best_of / 2) = 2 (best-of-3) or 3 (best-of-5)
  DP_match(sA,sB,server):
    if sA == sets_needed: return 1.0
    if sB == sets_needed: return 0.0
    p_set = P(A wins set with server serving first)
    return p_set × DP_match(sA+1,sB,flip) + (1-p_set) × DP_match(sA,sB+1,flip)
```

**Final match probability:**
```
prob_A = 0.5 × DP_match(0,0,A_serves) + 0.5 × DP_match(0,0,B_serves)
```
Averaged over both serve-first scenarios to remove first-serve artifact.

#### 6e. Tour Average Baselines

**ATP:**
```
ATP_AVG_FIRST_SERVE_PCT = 0.62
ATP_AVG_FIRST_WON_PCT   = 0.73
ATP_AVG_SECOND_WON_PCT  = 0.54
ATP_AVG_BP_CONVERTED    = 0.40
ATP_AVG_ACES_PER_MATCH  = 7.0
```

**WTA:**
```
WTA_AVG_FIRST_SERVE_PCT = 0.60
WTA_AVG_FIRST_WON_PCT   = 0.68
WTA_AVG_SECOND_WON_PCT  = 0.51
WTA_AVG_BP_CONVERTED    = 0.42
```

---

### 7. Calibration Metrics  (`models/calibration.py`)

**Brier Score** (lower = better, 0 = perfect):
```
BS = (1/N) × Σ (p_i - o_i)²
```
Where `p_i` = predicted probability of win, `o_i` ∈ {0,1}.  
Random model = 0.25; perfect = 0.00; typical good sports model = 0.18–0.22.

**Log-Loss** (lower = better):
```
LL = -(1/N) × Σ [o_i × log(p_i) + (1-o_i) × log(1-p_i)]
```
Heavily penalises confident wrong predictions. Clipped at epsilon=1e-7 to avoid log(0).

**ROI:**
```
ROI = (total_profit / total_staked) × 100%
```
Positive ROI means the model generates profit above the break-even point (vig-adjusted).

**Reliability curve:**  Predictions bucketed into 10 decile bins.  
For each bin `[b_lower, b_upper]`:
```
mean_predicted = avg(p_i) for all predictions in bin
mean_actual    = avg(o_i) for same predictions
```
Perfect calibration: mean_predicted ≈ mean_actual across all bins.

---

### 8. Devig  (`models/devig.py`)

**Implied probability from decimal odds:**
```
implied_prob = 1 / decimal_odds
```

**Remove vig (additive method):**
```
overround = Σ implied_prob_i   (sum > 1.0 = bookmaker's juice)
fair_prob_i = implied_prob_i / overround
```
Additive devig is the simplest method; multiplicative and power devig exist but overround
rarely exceeds 5% for 2-way markets, making the difference minimal.

**Closing Line Value:**
```
CLV = fair_prob_at_close - model_implied_prob
```
Positive CLV = model priced A higher probability than the market closed at → value bet.

**American to decimal:**
```
if american > 0:  decimal = american/100 + 1
if american < 0:  decimal = 100/|american| + 1
```

---

### 9. Trading Signals  (`models/trading/signals.py`, `models/trading/intraday.py`)

**SMA:**
```
SMA(n) = (1/n) × Σ close[i] for last n bars
```

**RSI:**
```
avg_gain = mean(positive_daily_changes, period=14)
avg_loss = mean(|negative_daily_changes|, period=14)
RS  = avg_gain / avg_loss
RSI = 100 - 100 / (1 + RS)
```
RSI > 70 = overbought; RSI < 30 = oversold.

**ATR (Average True Range):**
```
TR   = max(high - low, |high - prev_close|, |low - prev_close|)
ATR  = mean(TR, period=14)
ATR% = ATR / close × 100
```

**Historical Volatility (annualised):**
```
log_returns = log(close[i] / close[i-1]) for last 20 days
HV = stdev(log_returns) × sqrt(252)
```

**Bollinger Bands:**
```
mid    = SMA(20)
std    = stdev(close, 20)
upper  = mid + 2 × std
lower  = mid - 2 × std
%B     = (close - lower) / (upper - lower)
```
%B > 1 = price above upper band; %B < 0 = below lower band.

**Expected Move (±1σ next session):**
```
pct_1sigma = HV / sqrt(252)
upper_1σ   = close × (1 + pct_1sigma)
lower_1σ   = close × (1 - pct_1sigma)
prob_up    = 0.5 + (SMA(5) - SMA(20)) / (SMA(20) × 0.02)  [clamped 0.2–0.8]
```

**Composite Signal Score (–100 to +100):**
```
score = 0.40 × trend_score
      + 0.30 × rsi_score
      + 0.20 × roc_20d_score
      + 0.10 × bollinger_score
```
Where each sub-score is normalised to [–100, +100]:
- `trend_score`: +100 if strong bull, –100 if strong bear  
- `rsi_score`: linear from –100 (RSI=0) to +100 (RSI=100) centred at 50  
- `roc_20d_score`: clamped ±100 based on 20-day rate of change  
- `bollinger_score`: 200×(%B – 0.5) so %B=1.0 → +100, %B=0.0 → –100  

---

### 10. Table Tennis Model  (`analyze_table_tennis.py`)

**Service advantage index (AQI):**
```
AQI = (wins_on_serve / serve_attempts) × 100    (centred at ~60 = average)
```

**Return quality index (RQI):**
```
RQI = (points_won_returning / return_attempts) × 100
```

**Elo blend for table tennis:**
Same formula as §1 (Elo), but using Glicko-2 ratings per surface (§2).
Form factor applied as a logistic modifier:
```
form_modifier = logistic(form_score_delta, scale=20)   (±10pp max)
```

**Fatigue penalty** (intraday club matches):
```
fatigue_factor = 1 - 0.06 × matches_today   (–6% win rate per prior match)
```
Max of 3 applied: 4+ matches → –18% to –24%.

**Style matchup:**
```
aggressive_vs_chopper:     +8pp to aggressive player's raw win prob
looper_vs_allround:        +4pp to looper
penholder_vs_shakehand:    no adjustment (style-neutral)
```

---

### 11. Data Flow Summary

```
User query
    │
    ├─► parse_query (Claude AI)  →  team names, date
    │
    ├─► fetch_*_context (ESPN / data/live/ cache)
    │       → team stats, starters, records, park factor
    │
    ├─► expected_runs_split / Markov point probs
    │       → μ_home, μ_away (baseball) OR P_serve, P_return (tennis)
    │
    ├─► build_run_matrix / markov_tennis_match
    │       → joint probability distribution
    │
    ├─► market calculations
    │       → moneyline, run line, totals, F5/L4, NRFI, team totals
    │
    ├─► Elo blend (30% weight)
    │       → final prob_a, prob_b
    │
    ├─► kelly_stake
    │       → recommended_stake (quarter-Kelly × bankroll)
    │
    ├─► log_signal / DB persistence
    │
    └─► generate_narrative (Claude AI)  →  human-readable analysis
```

---

### 12. Key Calibration Parameters for Gemini Review

| Parameter | Value | File | Purpose |
|-----------|-------|------|---------|
| LEAGUE_AVG_RUNS | 4.50 | baseball_market.py | Baseline runs/game |
| LEAGUE_AVG_FIP | 4.00 | baseball_market.py | Baseline starter quality |
| LEAGUE_BULLPEN_FIP | 4.40 | baseball_market.py | Baseline bullpen quality |
| STARTER_FRAC | 5/9 ≈ 0.556 | baseball_market.py | Innings weight for starter |
| BULLPEN_FRAC | 4/9 ≈ 0.444 | baseball_market.py | Innings weight for bullpen |
| HOME_BOOST | 1.03 | baseball_market.py | Home field advantage |
| PLATOON_VS_LHP | 1.05 | baseball_market.py | RHB lineup bonus vs LHP |
| FIP_CONSTANT | 3.20 | fetchers/baseball.py | Calibration offset |
| wRC+_OPS_baseline | 0.730 | fetchers/baseball.py | 2025-26 MLB avg OPS |
| ELO_BLEND | 0.70 Poisson / 0.30 Elo | analyze_baseball.py | Model blend weight |
| KELLY_FRACTION | 0.25 | models/kelly.py | Quarter-Kelly sizing |
| DEFAULT_RATING | 1500 | models/elo.py | Initial Elo |
| DEFAULT_K | 30 | models/elo.py | Default K-factor |
| surf_serve_amp (grass) | 1.15 | analyze_tennis.py | Grass serve multiplier |
| surf_serve_amp (clay) | 0.88 | analyze_tennis.py | Clay serve suppressor |
| logistic_scale | 40.0 | analyze_tennis.py | SQI→probability scale — NEEDS VALIDATION: validate_tennis_scale.py output shows scale=40 produces ~80pp hold-rate gap vs ATP reference of ~20-25pp; likely too aggressive. Run tests/validate_tennis_scale.py with real ATP match data to find optimal value (likely 80-120). |
| extras_home_pct | 0.52 | baseball_market.py | MLB extras home win rate |
| DIXON_COLES_TAU | 0.10 | models/dixon_coles.py | Low-score correction |
| HOME_ADVANTAGE | 1.15 | models/dixon_coles.py | Soccer home boost (xG) |
| MIN_SAMPLES | 100 | models/ml_layer.py | ML training threshold |

---

*End of Math & Models Reference. Feed this section to Gemini with a specific matchup to get a parallel probability estimate or parameter critique.*

---

## tests/

---
name: validate_tennis_scale.py
type: script (standalone, no project imports)
file: tests/validate_tennis_scale.py
purpose: Sanity-check the logistic_scale parameter in analyze_tennis.py against ATP hold-rate benchmarks. Computes model-implied service game hold% for elite vs qualifier matchups at multiple scales. Focus metric: GAP column should match ATP reference of ~20-25pp (relative-calibration note: absolute hold% values will be lower than ATP because p_serve is centred at 0.5 for equal players). Populate TEST_MATCHES with real match data from Tennis Abstract / UTS to run grid-search validation.
inputs: none (standalone)
outputs: console table — p_serve, hold%, gap across scales + optional best-scale from TEST_MATCHES
calls: none (stdlib only)
called_by: developer manually (python tests/validate_tennis_scale.py)
mutates: none
open_issue: scale=40 produces ~80pp gap (too aggressive); likely needs raising to 80-120 once real data confirms
---

---
name: test_smoke.py
type: pytest test suite
file: tests/test_smoke.py
purpose: Smoke tests for core pipeline functions.
calls: various pipeline modules
called_by: pytest
mutates: none
---

---
name: test_pairs.py
type: pytest test suite
file: tests/test_pairs.py
purpose: 9 unit tests for models/trading/pairs.py. Uses synthetic price series (seeded random) to validate: _ols_beta exact recovery, _half_life sinusoidal vs RW, find_cointegrated_pairs detects cointegrated pair (p<0.05) and rejects independent RWs, pairs_signal generates correct LONG/SHORT/NONE actions, compute_pairs_levels returns sensible position sizes with correct risk_dollars.
calls: models.trading.pairs
called_by: pytest / python tests/test_pairs.py
mutates: none
---

---

## fetchers/pairs_data.py

---
name: fetch_pair_history
type: function
file: fetchers/pairs_data.py
purpose: Fetches daily closing prices for a list of symbols over `days` trading days and returns a date-aligned dict. Intersection of available dates across all symbols ensures all series are same length (required for cointegration tests). Date strings are ISO format "YYYY-MM-DD".
inputs: symbols: list[str], days: int = 120
outputs: dict[date_str, {symbol: close_price}]
calls: Alpaca /v2/stocks/{symbol}/bars (IEX feed, 1Day timeframe)
called_by: pairs_scan, pairs_signal_endpoint (app.py)
mutates: none
---

---
name: prices_to_series
type: function
file: fetchers/pairs_data.py
purpose: Converts fetch_pair_history output to {symbol: [close, ...]} parallel aligned lists sorted by date ascending. Both lists guaranteed same length.
inputs: history: dict[str, dict[str, float]]
outputs: dict[str, list[float]]
calls: none
called_by: pairs_scan, pairs_signal_endpoint (app.py)
mutates: none
---

---

## models/trading/pairs.py

---
name: _ols_beta
type: function
file: models/trading/pairs.py
purpose: Computes OLS slope β = Σ(xi-x̄)(yi-ȳ) / Σ(xi-x̄)² for hedge ratio estimation in cointegration.
inputs: x: list[float], y: list[float]
outputs: float
calls: none
called_by: find_cointegrated_pairs
mutates: none
---

---
name: _adf_pvalue
type: function
file: models/trading/pairs.py
purpose: ADF (Augmented Dickey-Fuller) p-value for spread residuals. Uses statsmodels.tsa.stattools.adfuller when available; falls back to first-order autocorrelation proxy (heuristic for ranking only). Small p-value (<0.05) = stationary spread = pair is cointegrated.
inputs: residuals: list[float]
outputs: float (p-value 0–1)
calls: statsmodels.tsa.stattools.adfuller (optional)
called_by: find_cointegrated_pairs
mutates: none
---

---
name: _half_life
type: function
file: models/trading/pairs.py
purpose: OU (Ornstein-Uhlenbeck) half-life estimate: how many days for the spread to mean-revert halfway. Estimated via OLS regression of Δspread on lagged spread. Returns None if no mean reversion (β >= 0) or half-life > 365 days. Lower half-life = faster reversion = better for trading.
inputs: spread: list[float]
outputs: Optional[int]
calls: _ols_beta, math.log
called_by: find_cointegrated_pairs
mutates: none
---

---
name: find_cointegrated_pairs
type: function
file: models/trading/pairs.py
purpose: Tests all symbol pairs for cointegration (Gatev et al. 2006). For each pair: estimates OLS hedge ratio β (s1 = β×s2 + ε), computes spread residuals, runs ADF test. Returns pairs with p-value < threshold sorted ascending by p-value. Also computes half_life_days via OU estimation.
inputs: series: dict[str, list[float]], pvalue_threshold: float = 0.05
outputs: list[dict {sym1, sym2, beta, pvalue, half_life_days}]
calls: _ols_beta, _adf_pvalue, _half_life
called_by: pairs_scan (app.py), tests/test_pairs.py
mutates: none
---

---
name: pairs_signal
type: function
file: models/trading/pairs.py
purpose: Generates a spread trade signal using z-score of spread = s1 - β×s2 over rolling lookback window. LONG_SPREAD (zscore < -entry_z): buy sym1, short sym2. SHORT_SPREAD (zscore > +entry_z): short sym1, buy sym2. EXIT_ZONE (|z| < exit_z): close position. NONE: no action. Confidence = min(|z|/(entry_z+1), 1.0).
inputs: sym1, sym2, beta, series, lookback=60, entry_z=2.0, exit_z=0.5
outputs: dict {action, zscore, sym1, sym2, long_sym, short_sym, confidence, spread_mean, spread_std, current_spread, note}
calls: none
called_by: pairs_scan, pairs_signal_endpoint (app.py), tests/test_pairs.py
mutates: none
---

---
name: compute_pairs_levels
type: function
file: models/trading/pairs.py
purpose: Position sizing for a pairs trade. Dollar-neutral: long_qty × long_price ≈ short_qty × short_price. Risk defined as spread widening 1σ beyond current z. Stop z = current_z + 1σ; target z = exit_z. Units = risk_dollars / spread_std. Returns long/short quantities, values, and expected_r = (current_z - target_z) / (stop_z - current_z).
inputs: long_sym, short_sym, series, beta, zscore, spread_std, account_value=10000, risk_pct=0.01, exit_z=0.5
outputs: dict {long_sym, short_sym, long_price, short_price, long_qty, short_qty, long_value, short_value, beta, stop_z, target_z, risk_dollars, expected_r}
calls: none
called_by: pairs_signal_endpoint (app.py), tests/test_pairs.py
mutates: none
---

---

## app.py (pairs endpoints)

---
name: pairs_scan
type: function
file: app.py
purpose: POST /trade/pairs-scan — scans a watchlist for cointegrated pairs. Fetches 120 days of daily bars via fetch_pair_history, runs find_cointegrated_pairs (ADF p<threshold), and for each detected pair computes current spread z-score via pairs_signal. Returns pairs sorted by p-value with signal action and note. Default watchlist: XOM/CVX, PEP/KO, JPM/BAC, AAPL/MSFT.
inputs: PairsScanRequest {symbols=DEFAULT_WATCHLIST, days=120, pvalue_threshold=0.05, account_value=10000, risk_pct=0.01}
outputs: dict {symbols_scanned, days_history, pairs_found, pairs: [{sym1, sym2, beta, pvalue, half_life_days, current_zscore, signal_action, long_sym, short_sym, signal_note}]}
calls: fetch_pair_history, prices_to_series, find_cointegrated_pairs, pairs_signal
called_by: POST /trade/pairs-scan
mutates: none
---

---
name: pairs_signal_endpoint
type: function
file: app.py
purpose: POST /trade/pairs-signal — get current spread signal and position sizing for a specific pair. Fetches history, computes z-score, and if LONG_SPREAD or SHORT_SPREAD also calls compute_pairs_levels for position sizes. Use after pairs-scan to get actionable entry details.
inputs: PairsSignalRequest {sym1, sym2, beta, days=120, lookback=60, entry_z=2.0, exit_z=0.5, account_value=10000, risk_pct=0.01}
outputs: dict {pair, beta, signal: {action, zscore, ...}, levels: {long_qty, short_qty, risk_dollars, expected_r, ...} or None}
calls: fetch_pair_history, prices_to_series, pairs_signal, compute_pairs_levels
called_by: POST /trade/pairs-signal
mutates: none
---

---
name: pairs_candidates
type: function
file: app.py
purpose: POST /trade/pairs-candidates — scan the 8 CANDIDATE_PAIRS for cointegration using full Engle-Granger test (statsmodels coint). Filters by OU half-life (default 1–30 days). Returns cointegrated pairs with current z-score and generate_pair_signal output. Run weekly.
inputs: PairsCandidatesRequest {days=90, min_half_life=1.0, max_half_life=30.0}
outputs: dict {candidate_pairs_tested, cointegrated_found, filters, pairs: [{sym1, sym2, pvalue, beta, half_life_days, current_zscore, signal: {action, confidence, long, short, stop_z, ...}}]}
calls: find_all_pairs, generate_pair_signal, CANDIDATE_PAIRS
called_by: POST /trade/pairs-candidates
mutates: none
---

---
name: ngram_build
type: function
file: app.py
purpose: POST /trade/ngram-build — build or refresh n-gram pattern frequency tables for a list of symbols. Fetches up to 10,000 5-min bars from Alpaca, builds 3-bar pattern table, saves to ngram_models DB table. Takes ~2s per symbol.
inputs: NgramBuildRequest {symbols: list[str], months=6}
outputs: dict {symbols_requested, results: {symbol: "built" | "insufficient_data" | "error: ..."}}
calls: build_ngram_from_alpaca
called_by: POST /trade/ngram-build
mutates: ngram_models table (INSERT OR REPLACE)
---

---
name: calibration_status
type: function
file: app.py
purpose: GET /trade/calibration — one-call readiness summary: trade count, kelly_ready flag, calibration_quality, empirical_score_threshold, overall_win_rate, avg_pnl_r.
inputs: none
outputs: calibration_summary() dict
calls: calibration_summary (signal_calibration.py)
called_by: GET /trade/calibration
mutates: none
---

---
name: calibration_scores
type: function
file: app.py
purpose: GET /trade/calibration/scores — win rate and avg P&L per composite score bucket. Optional ?min_trades=N query param.
inputs: min_trades: int = 5 (query param)
outputs: score_accuracy_report() dict
calls: score_accuracy_report (signal_calibration.py)
called_by: GET /trade/calibration/scores
mutates: none
---

---
name: calibration_time
type: function
file: app.py
purpose: GET /trade/calibration/time — win rate per time-of-day session. Optional ?min_trades=N query param.
inputs: min_trades: int = 5 (query param)
outputs: time_accuracy_report() dict
calls: time_accuracy_report (signal_calibration.py)
called_by: GET /trade/calibration/time
mutates: none
---

---
name: ngram_validate
type: function
file: app.py
purpose: GET /trade/ngram-validate/{symbol} — binomial significance test on all patterns in the symbol's n-gram table. Optional ?significance=0.05 query param.
inputs: symbol: str (path), significance: float = 0.05 (query)
outputs: validate_ngram_patterns() dict {n_validated, n_weak, validated, weak}
calls: validate_ngram_patterns (ngram.py)
called_by: GET /trade/ngram-validate/{symbol}
mutates: none
---

---
name: pairs_exit
type: function
file: app.py
purpose: POST /trade/pairs-exit/{signal_id} — close an open pair_signals row with exit z-score, reason, and optional P&L %. Mirrors /trade/resolve-hypothetical for pairs. exit_reason: TARGET_HIT | STOP_HIT | TIME_EXIT | MANUAL.
inputs: signal_id: int (path), PairsExitRequest {exit_zscore, exit_reason, pnl_pct?}
outputs: dict {signal_id, closed, exit_reason}
calls: log_pair_exit (pairs.py)
called_by: POST /trade/pairs-exit/{signal_id}
mutates: pair_signals table (UPDATE via log_pair_exit)
---

---
name: calibration_readiness
type: function
file: app.py
purpose: GET /trade/calibration/status — per-feature readiness: which ML/sizing features are unlocked. Returns calibration_readiness_status() plus compute_dynamic_weights(), compute_ngram_blend_weight(), and per_signal_accuracy_report() when thresholds are met (or None when not).
inputs: none
outputs: dict {total_closed_trades, features, n_ready, pct_ready, overall_status, dynamic_weights, ngram_blend, per_signal_report}
calls: calibration_readiness_status, compute_dynamic_weights, compute_ngram_blend_weight, per_signal_accuracy_report (signal_calibration.py)
called_by: GET /trade/calibration/status
mutates: none
---

---
name: pairs_suspend
type: function
file: app.py
purpose: POST /trade/pairs-suspend/{sym1}/{sym2} — suspend a pair from the weekly find_all_pairs scan. Optional reinstate_after ISO datetime UTC and reason in request body.
inputs: sym1, sym2 (path), PairsSuspendRequest {reason="", reinstate_after=None}
outputs: dict {pair, suspended, reinstate_after}
calls: suspend_pair (pairs.py)
called_by: POST /trade/pairs-suspend/{sym1}/{sym2}
mutates: suspended_pairs table (INSERT OR UPDATE)
---

---
name: pairs_reinstate
type: function
file: app.py
purpose: DELETE /trade/pairs-suspend/{sym1}/{sym2} — remove a pair from the suspended list so it's eligible for find_all_pairs again.
inputs: sym1, sym2 (path)
outputs: dict {pair, reinstated}
calls: reinstate_pair (pairs.py)
called_by: DELETE /trade/pairs-suspend/{sym1}/{sym2}
mutates: suspended_pairs table (DELETE)
---

---
name: list_suspended_pairs
type: function
file: app.py
purpose: GET /trade/pairs-suspend — list all currently suspended pairs (reinstate_after in future or NULL).
inputs: none
outputs: dict {suspended: list[dict]}
calls: get_suspended_pairs (pairs.py)
called_by: GET /trade/pairs-suspend
mutates: none
---

---
name: pairs_auto_cull
type: function
file: app.py
purpose: POST /trade/pairs-auto-cull — auto-suspend underperforming pairs based on realized P&L. Suspends for 90 days any pair with win_rate < win_rate_floor OR avg_pnl_pct < 0 (with >= min_trades closed). Optional ?min_trades=10 and ?win_rate_floor=0.40 query params.
inputs: min_trades: int = 10 (query), win_rate_floor: float = 0.40 (query)
outputs: dict {culled: list[dict], n_culled}
calls: auto_cull_pairs (pairs.py)
called_by: POST /trade/pairs-auto-cull
mutates: suspended_pairs table (via auto_cull_pairs → suspend_pair)
---

---
name: paper_runner_status
type: function
file: app.py
purpose: GET /trade/paper-runner/status — returns current state of the automated paper trading runner: active flag, config, open position count, open symbols, market_open status, ET time, last 20 log events.
inputs: none
outputs: dict (from get_runner_status)
calls: get_runner_status (paper_runner.py)
called_by: GET /trade/paper-runner/status
mutates: none
---

---
name: paper_runner_scan_now
type: function
file: app.py
purpose: POST /trade/paper-runner/scan-now — manually trigger a signal scan outside the scheduled window. Useful for afternoon session or ad-hoc testing.
inputs: min_score: int = 20 (query)
outputs: dict {logged, trade_ids}
calls: run_open_scan (paper_runner.py)
called_by: POST /trade/paper-runner/scan-now
mutates: intraday_trades table (INSERT via run_open_scan)
---

---
name: paper_runner_start
type: function
file: app.py
purpose: POST /trade/paper-runner/start — start the background runner if not already running.
inputs: none
outputs: dict {started: bool}
calls: start_runner (paper_runner.py)
called_by: POST /trade/paper-runner/start
mutates: _runner_thread, _runner_active (paper_runner.py globals)
---

---
name: paper_runner_stop
type: function
file: app.py
purpose: POST /trade/paper-runner/stop — signal background runner to stop on its next tick.
inputs: none
outputs: dict {ok: true}
calls: stop_runner (paper_runner.py)
called_by: POST /trade/paper-runner/stop
mutates: _runner_active (paper_runner.py global)
---

---
name: trade_dashboard
type: function
file: app.py
purpose: GET /trade/dashboard — self-contained HTML page that translates paper trading data into plain English. Shows: current phase (Watching/Collecting/Calibrating/Sizing), win rate with interpretation text, avg R with interpretation text, total adjusted P&L, best time-of-day breakdown, how trades are closing (exit reasons), open positions, recent 8 trades, and a readiness verdict ("Ready for real money" / "Not yet — why"). Auto-refreshes every 5 minutes. Mobile-friendly.
inputs: none
outputs: HTMLResponse
calls: db.database.get_db, get_runner_status (paper_runner.py), _et_now (paper_runner.py)
called_by: GET /trade/dashboard
mutates: none
---

---
name: ping
type: function
file: app.py
purpose: GET /ping — lightweight liveness check for Railway health monitoring. Returns immediately with no DB call.
inputs: none
outputs: dict {ok: true}
calls: none
called_by: Railway healthcheck, GET /ping
mutates: none
---

---
name: paper_data_export
type: function
file: app.py
purpose: GET /trade/paper-data — export all hypothetical paper trade records for analysis. Returns closed trades by default (?include_open=true adds open positions). Includes summary stats (win_rate, avg_pnl, avg_r) + full trade rows with v4 signal scores for per-signal calibration analysis.
inputs: days: int = 60 (query), include_open: bool = False (query)
outputs: dict {period_days, total, closed, open, win_rate, avg_pnl, avg_r, trades: list[dict]}
calls: db.database.get_db
called_by: GET /trade/paper-data
mutates: none
---

---

## db/schema.sql (new tables — Round 7)

---
name: pair_signals
type: table
file: db/schema.sql
purpose: Persists pair trade signals for tracking outcomes. Populated by log_pair_signal. exit_z/exit_time/pnl_pct/exit_reason filled when the trade closes. Indexed by (sym1, sym2) and created_at for range queries.
inputs: none (DDL)
outputs: none (DDL)
calls: none
called_by: log_pair_signal (models/trading/pairs.py)
mutates: none (DDL)
---

---
name: ngram_models
type: table
file: db/schema.sql
purpose: Stores per-symbol n-gram frequency tables as JSON. UNIQUE(symbol, pattern_len) — one table per symbol/pattern length. updated_at drives staleness check in load_pattern_table (stale after 7 days → rebuild). bar_count records training data size.
inputs: none (DDL)
outputs: none (DDL)
calls: none
called_by: save_pattern_table, load_pattern_table (models/trading/ngram.py)
mutates: none (DDL)
---

---

## models/trading/pairs.py (Round 7 additions)

---
name: CANDIDATE_PAIRS
type: variable
file: models/trading/pairs.py
purpose: 8 pre-defined sector-pairs with strong cointegration priors: XOM/CVX (oil), PEP/KO (beverages), JPM/BAC (banks), AAPL/MSFT (big tech), WMT/TGT (retail), DAL/UAL (airlines), INTC/AMD (semis), JNJ/PFE (pharma). Used by find_all_pairs and pairs-candidates endpoint.
inputs: none
outputs: list[tuple[str, str]]
calls: none
called_by: find_all_pairs, pairs_candidates (app.py)
mutates: none
---

---
name: _spread_stats
type: function
file: models/trading/pairs.py
purpose: Compute spread mean, std, current value, and z-score for s1 - β×s2. Shared helper between test_cointegration and find_cointegrated_pairs to avoid duplication.
inputs: s1: list[float], s2: list[float], beta: float
outputs: dict {spread, spread_mean, spread_std, current_spread, zscore}
calls: math.sqrt
called_by: test_cointegration, find_cointegrated_pairs
mutates: none
---

---
name: test_cointegration
type: function
file: models/trading/pairs.py
purpose: Full Engle-Granger cointegration analysis for a specific pair. Uses statsmodels.tsa.stattools.coint (proper 2-step EG procedure, more accurate than ADF alone). Falls back to ADF on OLS residuals if coint import fails. Returns rich dict including coint_score, pvalue, beta, spread stats, OU half-life, and current z-score. Primary input for find_all_pairs and generate_pair_signal.
inputs: sym1: str, sym2: str, series: dict[str, list[float]]
outputs: dict {sym1, sym2, cointegrated, pvalue, coint_score, beta, spread_mean, spread_std, half_life_days, current_zscore, n_obs}
calls: _ols_beta, _spread_stats, _half_life, statsmodels.tsa.stattools.coint
called_by: find_all_pairs
mutates: none
---

---
name: get_suspended_pairs
type: function
file: models/trading/pairs.py
purpose: Returns all currently-active suspended pairs from suspended_pairs table. Filters out rows where reinstate_after is in the past.
inputs: none
outputs: list[dict {sym1, sym2, reason, suspended_at, reinstate_after}]
calls: db.database.get_db
called_by: find_all_pairs, list_suspended_pairs endpoint (app.py)
mutates: none
---

---
name: suspend_pair
type: function
file: models/trading/pairs.py
purpose: Insert or update a suspended_pairs row. ON CONFLICT updates reason/suspended_at/reinstate_after. reinstate_after=None means indefinite suspension.
inputs: sym1, sym2, reason, reinstate_after=None
outputs: bool (True on success)
calls: db.database.get_db
called_by: auto_cull_pairs, pairs_suspend endpoint (app.py)
mutates: suspended_pairs table (INSERT OR UPDATE)
---

---
name: reinstate_pair
type: function
file: models/trading/pairs.py
purpose: Delete a pair from suspended_pairs. Returns True if a row was actually deleted.
inputs: sym1: str, sym2: str
outputs: bool
calls: db.database.get_db
called_by: pairs_reinstate endpoint (app.py)
mutates: suspended_pairs table (DELETE)
---

---
name: auto_cull_pairs
type: function
file: models/trading/pairs.py
purpose: Scan pairs_calibration_summary and suspend any pair with n>=min_trades where win_rate < win_rate_floor OR avg_pnl_pct < 0. Suspension is for 90 days (reinstate_after = now+90d). Returns list of dicts describing each pair culled.
inputs: min_trades=10, win_rate_floor=0.40
outputs: list[dict {pair, reason, win_rate, avg_pnl_pct, n_trades, reinstate_after}]
calls: pairs_calibration_summary (signal_calibration.py), suspend_pair
called_by: pairs_auto_cull endpoint (app.py)
mutates: suspended_pairs table (via suspend_pair)
---

---
name: find_all_pairs
type: function
file: models/trading/pairs.py
purpose: End-to-end weekly scan of CANDIDATE_PAIRS. Skips pairs in suspended_pairs table (checks get_suspended_pairs before testing). Fetches daily bars for each pair via fetch_pair_history, runs test_cointegration (full EG test), filters by cointegration p<0.05 AND OU half-life in [min_half_life, max_half_life] days. Returns results sorted by p-value. Typical output: 2-4 cointegrated pairs from 8 candidates.
inputs: days=90, min_half_life=1.0, max_half_life=30.0
outputs: list[dict] (test_cointegration output for qualifying pairs, sorted by pvalue)
calls: fetch_pair_history, prices_to_series, test_cointegration
called_by: pairs_candidates (app.py), scripts/weekly_build.py
mutates: none
---

---
name: generate_pair_signal
type: function
file: models/trading/pairs.py
purpose: Trade signal from a test_cointegration result dict. Fixed thresholds: entry |z|>2.0, target |z|<0.5, stop |z|>3.5. Confidence = min(|z|/3.5, 1.0). Includes expected_hold_days from OU half-life. Returns LONG_SPREAD (buy sym1 short sym2), SHORT_SPREAD (short sym1 buy sym2), or NONE.
inputs: pair_result: dict (from test_cointegration / find_all_pairs)
outputs: dict {action, zscore, long, short, beta, shares_ratio, entry_z, target_z, stop_z, confidence, expected_hold_days, note}
calls: none
called_by: pairs_candidates (app.py), scripts/weekly_build.py, tests
mutates: none
---

---
name: log_pair_signal
type: function
file: models/trading/pairs.py
purpose: Persist a pair signal to pair_signals table for outcome tracking. sym1/sym2 are canonical (from test_cointegration). Returns row ID.
inputs: signal: dict, sym1: str, sym2: str
outputs: int (row ID)
calls: db.database.get_db
called_by: pairs_candidates (app.py) — optional, caller decides to log or not
mutates: pair_signals table (INSERT)
---

---
name: log_pair_exit
type: function
file: models/trading/pairs.py
purpose: Close an open pair_signals row with exit z-score, reason, and P&L %. Mirrors log_trade_exit for intraday_trades. Only updates rows where exit_time IS NULL (idempotent). Returns True on success.
inputs: signal_id: int, exit_zscore: float, exit_reason: str, pnl_pct: Optional[float]
outputs: bool
calls: db.database.get_db
called_by: pairs_exit endpoint (app.py)
mutates: pair_signals table (UPDATE exit_z, exit_time, pnl_pct, exit_reason)
---

---

## models/trading/signal_calibration.py

---
name: signal_calibration
type: module
file: models/trading/signal_calibration.py
purpose: Signal accuracy feedback loop. Queries closed intraday_trades to compute per-bucket win rates and avg P&L-in-R per composite score bucket and time-of-day session. calibrated_win_rate() returns empirical win rate with no heuristic fallback (returns None when data insufficient). calibration_summary() checks Kelly readiness and empirical score threshold.
inputs: none (queries DB internally)
outputs: see individual functions below
calls: db.database.get_db
called_by: calibration endpoints in app.py
mutates: none (read-only)
---

---
name: score_accuracy_report
type: function
file: models/trading/signal_calibration.py
purpose: Win rate and avg P&L-in-R per composite score bucket (Strong Sell / Sell / Neutral / Buy / Strong Buy). Returns None for buckets with fewer than min_trades closed trades.
inputs: min_trades: int = 5
outputs: dict {total_closed, overall_win_rate, avg_pnl_r, buckets: [{label, min_score, max_score, n, win_rate, avg_pnl_r}], note}
calls: _load_closed_trades
called_by: calibration_scores endpoint, calibration_summary
mutates: none
---

---
name: time_accuracy_report
type: function
file: models/trading/signal_calibration.py
purpose: Win rate and avg P&L per time-of-day session. Used to empirically tune time_of_day_modifier thresholds.
inputs: min_trades: int = 5
outputs: dict {total_closed, by_session: [{session, n, win_rate, avg_pnl_r}]}
calls: _load_closed_trades
called_by: calibration_time endpoint
mutates: none
---

---
name: calibrated_win_rate
type: function
file: models/trading/signal_calibration.py
purpose: Returns empirical win rate for a composite score. Returns None when data is insufficient — NEVER falls back to the (score+100)/200 heuristic.
inputs: score: float
outputs: Optional[float]
calls: score_accuracy_report
called_by: kelly_from_signals (only when calibration_globally_active() is True — 100+ total closed trades)
mutates: none
---

---
name: MIN_TRADES_FOR_ANY_CALIBRATION
type: variable
file: models/trading/signal_calibration.py
purpose: Hard floor (100) before any calibration output — empirical win rate, veto, dynamic weights — is allowed to influence a live decision (Kimi review, round 2). Below this, acting on 30-50 trade calibration data risks tuning noise rather than validated edge.
inputs: none
outputs: int (100)
calls: none
called_by: calibration_globally_active
mutates: none
---

---
name: calibration_globally_active
type: function
file: models/trading/signal_calibration.py
purpose: True once total closed trades >= MIN_TRADES_FOR_ANY_CALIBRATION (100). kelly_from_signals checks this before using calibrated_win_rate() or veto_decision() for a real decision — below the floor, falls back to static ATR sizing with no empirical overlay.
inputs: none
outputs: bool
calls: _load_closed_trades
called_by: kelly_from_signals
mutates: none
---

---
name: _wilson_ci
type: function
file: models/trading/signal_calibration.py
purpose: Wilson score interval for a binomial proportion — better small-sample behavior than a normal approximation. Used by veto_decision to compute an 80%-confidence range for the true win rate given wins/n.
inputs: wins: int, n: int, confidence: float = 0.80
outputs: tuple[float, float] (ci_lower, ci_upper)
calls: math.sqrt
called_by: veto_decision
mutates: none
---

---
name: veto_decision
type: function
file: models/trading/signal_calibration.py
purpose: Statistically-gated trade veto (Kimi review, round 1). Replaces a naive point-estimate check ("win rate < 45%") with an 80% confidence-interval test via _wilson_ci, and refuses to veto below min_trades in the score's bucket — a 45% observed win rate at n=15 could easily be a true 55% rate with bad variance. Only vetoes when the bucket's 80% CI upper bound sits entirely below ci_floor (default 0.48), which in practice requires ~25-30+ trades in the bucket.
inputs: score: float, min_trades: int = 30, ci_floor: float = 0.48
outputs: dict {veto, reason, n, win_rate, ci_lower, ci_upper, min_trades}
calls: _load_closed_trades, _wilson_ci, _is_winner, _BUCKETS
called_by: kelly_from_signals (only when calibration_globally_active() is True)
mutates: none
---

---
name: calibration_summary
type: function
file: models/trading/signal_calibration.py
purpose: One-call readiness check. Returns kelly_ready (n≥50), calibration_quality, empirical_score_threshold (lowest bucket with >50% WR), overall_win_rate, avg_pnl_r.
inputs: none
outputs: dict {total_closed_trades, kelly_ready, calibration_quality, empirical_score_threshold, recommended_min_score, overall_win_rate, avg_pnl_r, note}
calls: _load_closed_trades, score_accuracy_report
called_by: calibration_status endpoint
mutates: none
---

---
name: pairs_calibration_summary
type: function
file: models/trading/signal_calibration.py
purpose: Per-pair realized edge from closed pair_signals rows. Returns win_rate, avg_pnl_pct, avg_hold_h, and UNDERPERFORMING flag (win_rate < 0.50 or avg_pnl < 0) for pairs with min_trades or more closed trades. Sorted by avg_pnl_pct descending.
inputs: min_trades: int = 3
outputs: dict {total_closed, pairs: [{pair, n, win_rate, avg_pnl_pct, avg_hold_h, flag, note}]}
calls: db.database.get_db
called_by: calibration_pairs endpoint (app.py), auto_cull_pairs (pairs.py)
mutates: none
---

---
name: _load_closed_trades_full
type: function
file: models/trading/signal_calibration.py
purpose: All closed trades with v4 per-signal score columns (composite_raw, vwap/or/rsi/relvol/gap/trend/bollinger/volsurge scores, ngram_signal, ngram_confidence). Returns empty list on DB error or if no closed trades exist. Used by per_signal_accuracy_report, compute_dynamic_weights, compute_ngram_blend_weight, calibration_readiness_status.
inputs: none
outputs: list[dict]
calls: db.database.get_db
called_by: per_signal_accuracy_report, compute_dynamic_weights, compute_ngram_blend_weight, calibration_readiness_status
mutates: none
---

---
name: _per_signal_stats
type: function
file: models/trading/signal_calibration.py
purpose: Win rate and avg P&L (in R) for one signal column. Only counts trades where |signal_score| >= active_threshold (signal was active/contributing). Returns {n, win_rate, avg_r} with None values when n=0.
inputs: trades: list[dict], col: str, active_threshold: float = 10.0
outputs: dict {n, win_rate, avg_r}
calls: _is_winner
called_by: per_signal_accuracy_report, compute_dynamic_weights, calibration_readiness_status
mutates: none
---

---
name: per_signal_accuracy_report
type: function
file: models/trading/signal_calibration.py
purpose: Win rate and avg P&L per individual signal component. Only counts trades where |signal_score| >= active_threshold. Trades logged pre-v4 schema have NULL signal scores and are excluded silently. Results sorted by win_rate descending.
inputs: min_trades: int = 10, active_threshold: float = 10.0
outputs: dict {total_closed, by_signal: [{signal, column, n, win_rate, avg_r, note}], active_threshold, note}
calls: _load_closed_trades_full, _per_signal_stats
called_by: calibration_readiness endpoint (app.py)
mutates: none
---

---
name: compute_dynamic_weights
type: function
file: models/trading/signal_calibration.py
purpose: Empirically-driven signal weights from closed trades. Formula: raw = max(0, (win_rate-0.5)*avg_r) per signal; normalize by total raw. Falls back to _STATIC_WEIGHTS when sum of raws=0 (status="fallback_static"). Returns None when total closed trades < min_trades. Used to replace WEIGHTS in intraday.py once enough data exists.
inputs: min_trades: int = 30
outputs: Optional[dict {weights, raw_weights, negative_utility, n_trades, status}]
calls: _load_closed_trades_full, _per_signal_stats
called_by: calibration_readiness endpoint (app.py)
mutates: none
---

---
name: compute_ngram_blend_weight
type: function
file: models/trading/signal_calibration.py
purpose: Calibrate n-gram overlay multiplier. Agree cohort = n-gram direction matches composite_raw direction; disagree cohort = opposes. agree_multiplier = agree_avg_r / baseline_avg_r, clamped to [0.5, 2.0]. Returns None when either cohort < min_samples. When agree_multiplier > 1.0, n-gram adds value; < 1.0 means the agree-direction trades underperform baseline.
inputs: min_samples: int = 20
outputs: Optional[dict {agree_multiplier, disagree_multiplier, n_agree, n_disagree, agree_avg_r, disagree_avg_r, baseline_avg_r, status}]
calls: _load_closed_trades_full, _avg_r (internal)
called_by: calibration_readiness endpoint (app.py)
mutates: none
---

---
name: calibration_readiness_status
type: function
file: models/trading/signal_calibration.py
purpose: Per-feature readiness check with threshold targets. Features: kelly_sizing (50 trades), dynamic_weights (30 trades), per_signal_accuracy (10 active per signal), ngram_blend_weight (20 agree + 20 disagree), time_session_accuracy (20 per session). Returns features list, n_ready/n_total, pct_ready, overall_status (fully_calibrated / partially_calibrated / insufficient).
inputs: none
outputs: dict {total_closed_trades, features, n_ready, n_total_features, pct_ready, overall_status}
calls: _load_closed_trades_full, _per_signal_stats
called_by: calibration_readiness endpoint (app.py)
mutates: none
---

---

## models/trading/ngram.py

---
name: PATTERN_LENGTH
type: variable
file: models/trading/ngram.py
purpose: N-gram context window length. Extended 3→4 bars (Kimi review): 3 bars (15 min) showed weak directional autocorrelation (~0.05-0.12 in large-cap 5-min bars) — too close to the 50% baseline given only a slightly-elevated 52% threshold. 4-bar patterns → 3^4 = 81 possible sequences (U/D/E per bar), ~20 min of context (closer to the model's 30-60 min intended hold), still ~115 expected occurrences per pattern over 6 months — comfortably above the 40-sample floor.
inputs: none
outputs: int (4)
calls: none
called_by: encode_sequence, build_pattern_table, ngram_signal, save/load_pattern_table
mutates: none
---

---
name: encode_bar
type: function
file: models/trading/ngram.py
purpose: Classify a single bar direction: U (up, ratio > 1.0001), D (down, ratio < 0.9999), E (equal, within 1bp). The 1bp threshold prevents noise from flat bars polluting the pattern table.
inputs: current_close: float, previous_close: float
outputs: str ("U" | "D" | "E")
calls: none
called_by: encode_sequence, build_pattern_table
mutates: none
---

---
name: encode_sequence
type: function
file: models/trading/ngram.py
purpose: Encode a list of closes into a direction string. len(closes)≥2 required; returns string of length (len-1). Example: [100, 101, 100.5] → "UD".
inputs: closes: list[float]
outputs: str
calls: encode_bar
called_by: build_pattern_table, ngram_signal
mutates: none
---

---
name: build_pattern_table
type: function
file: models/trading/ngram.py
purpose: Build frequency table from historical closes. For each overlapping (pattern_len+1) window: encode pattern from first pattern_len bars, record next bar direction. Returns {pattern_str: {U: n, D: n, E: n}} covering all observed patterns.
inputs: closes: list[float], pattern_len: int = PATTERN_LENGTH
outputs: dict[str, dict[str, int]]
calls: encode_sequence, encode_bar
called_by: build_ngram_from_alpaca
mutates: none
---

---
name: save_pattern_table
type: function
file: models/trading/ngram.py
purpose: Upsert pattern frequency table into ngram_models (INSERT OR REPLACE, unique on symbol+pattern_len). Serialises table as JSON.
inputs: symbol: str, table: dict, bar_count: int
outputs: none
calls: db.database.get_db
called_by: build_ngram_from_alpaca
mutates: ngram_models table
---

---
name: load_pattern_table
type: function
file: models/trading/ngram.py
purpose: Load pattern table from DB. Returns None if not found, table is stale (>max_age_days), or DB table doesn't exist yet. Caller should trigger rebuild on None.
inputs: symbol: str, max_age_days: int = 7
outputs: Optional[dict]
calls: db.database.get_db
called_by: ngram_signal
mutates: none
---

---
name: ngram_signal
type: function
file: models/trading/ngram.py
purpose: Generate n-gram pattern signal for current bar context. Looks up encode_sequence(recent_closes[-(PATTERN_LENGTH+1):]) in stored frequency table — now 5 bars (4-bar pattern + 1) since PATTERN_LENGTH extended 3→4 (Kimi review). Returns UP (p_up>0.52, conf=(p-0.5)×200), DOWN (p_down>0.52), or NONE. Confidence is on 0–100 scale. Requires min_samples=40 historical occurrences (raised from 30 to maintain statistical power for the larger 81-pattern space).
inputs: symbol: str, recent_closes: list[float], min_samples: int = 40
outputs: dict {signal, confidence, historical_win_rate?, pattern?, n_historical?, expected_edge?, reason?}
calls: encode_sequence, load_pattern_table
called_by: compute_intraday_signals (intraday.py)
mutates: none
---

---
name: validate_ngram_patterns
type: function
file: models/trading/ngram.py
purpose: Binomial significance test per pattern. z = (p̂ - 0.5) / sqrt(0.25/n), one-tailed p-value via erfc. Patterns with p < significance (default 0.05) have demonstrated directional edge; others are noise. Call after building tables to audit which patterns the signal engine should trust.
inputs: symbol: str, significance: float = 0.05
outputs: dict {symbol, total_patterns, n_validated, n_weak, validated: [{pattern, direction, win_rate, n, z_score, p_value}], weak: [...]}
calls: load_pattern_table
called_by: ngram_validate endpoint (app.py)
mutates: none
---

---
name: ngram_to_composite_score
type: function
file: models/trading/ngram.py
purpose: Convert ngram signal to -100…+100 scale for ensemble blending. UP → +confidence, DOWN → -confidence, NONE → 0.
inputs: signal: dict
outputs: float
calls: none
called_by: compute_intraday_signals (intraday.py)
mutates: none
---

---
name: build_ngram_from_alpaca
type: function
file: models/trading/ngram.py
purpose: Fetch historical 5-min bars from Alpaca (up to 10000 bars, ~6 months), build 3-bar pattern table, save to DB. Returns True on success. Takes ~2s per symbol. Call weekly per symbol via scripts/weekly_build.py.
inputs: symbol: str, months: int = 6
outputs: bool
calls: fetchers.alpaca.get_bars, build_pattern_table, save_pattern_table
called_by: ngram_build (app.py), scripts/weekly_build.py
mutates: ngram_models table (via save_pattern_table)
---

---

## models/trading/intraday.py (Round 7 update)

---
name: compute_intraday_signals (updated)
type: function
file: models/trading/intraday.py
purpose: [UPDATED] Added symbol: str = None parameter and ngram blend. When symbol provided, loads ngram pattern table and blends result: agreement (same direction) boosts score ≤20% (×(1+conf/500)); disagreement reduces score 30% (×0.7). Only fires when |score_val|>0 (skips MARKET_CLOSED, LUNCH_SUPPRESSED, liquidity-killed scores). ngram added to returned signals dict as signals["ngram"]. Backward compatible — existing callers without symbol param are unaffected.
inputs: ...(existing)..., symbol: str = None
outputs: dict {signals (now includes signals["ngram"]), score, levels, liquidity, intraday_expected_move, exit_template}
calls: ...(existing)..., ngram_signal, ngram_to_composite_score (conditional)
called_by: intraday_analysis (app.py — now passes symbol=body.symbol), _analyze_one (screener.py — no change, symbol=None)
mutates: none
---

---

## scripts/weekly_build.py

---
name: weekly_build.py
type: script
file: scripts/weekly_build.py
purpose: Weekly maintenance script. Run every Sunday before market open. Rebuilds n-gram frequency tables for 18-symbol watchlist (NGRAM_WATCHLIST) via build_ngram_from_alpaca. Scans CANDIDATE_PAIRS for cointegration via find_all_pairs and prints current z-scores + signals. Logs results to stdout. Cron: 0 8 * * 0 python scripts/weekly_build.py.
inputs: none (standalone script)
outputs: console report
calls: init_db, build_ngram_from_alpaca, find_all_pairs, generate_pair_signal
called_by: cron / manual
mutates: ngram_models table
---

---

## tests/ (Round 7 additions)

---
name: test_ngram.py
type: pytest test suite
file: tests/test_ngram.py
purpose: 13 unit tests for models/trading/ngram.py. Covers: encode_bar U/D/E classification, encode_sequence multi-bar, build_pattern_table counts and edge cases, ngram_signal with no table / too few bars / mock UP table / mock DOWN / score conversion, and full DB round-trip via save_pattern_table + load_pattern_table.
calls: models.trading.ngram, db.database.init_db
called_by: pytest / python tests/test_ngram.py
mutates: ngram_models table (test symbol "_TEST_NGRAM_SYMBOL_")
---

---

## fetchers/savant.py

---
name: _load_fg_pitchers
type: function
file: fetchers/savant.py
purpose: Load FanGraphs pitching_stats DataFrame for a season. Tries `season` first, then `season-1` as fallback — handles mid-season gaps where current-year data is not yet available (e.g. 2026 in June). Results cached in _PITCHER_CACHE keyed by the requested season. Returns None when pybaseball/pandas not installed.
inputs: season: int
outputs: Optional[pd.DataFrame]
calls: pyb.pitching_stats
called_by: fetch_pitcher_fg
mutates: _PITCHER_CACHE
---

---
name: _load_fg_batters
type: function
file: fetchers/savant.py
purpose: Load FanGraphs batting_stats DataFrame for a season. Same season-1 fallback as _load_fg_pitchers — if current year empty or errors, silently retries with season-1. Results cached in _BATTER_CACHE. Returns None when pybaseball/pandas not installed.
inputs: season: int
outputs: Optional[pd.DataFrame]
calls: pyb.batting_stats
called_by: fetch_team_batting_fg
mutates: _BATTER_CACHE
---

---
name: fetch_pitcher_fg
type: function
file: fetchers/savant.py
purpose: Look up a starting pitcher's advanced FanGraphs stats for a season. Returns dict with siera, xfip, fip, era, k_pct, bb_pct, swstr_pct, gb_pct, hr_per_9, whip, ip, war, source. Empty dict on lookup failure. Uses pybaseball.pitching_stats() with module-level season cache.
inputs: name: str, season: int | None
outputs: dict
calls: _load_fg_pitchers, _match_name, _safe_float
called_by: enrich_starter
mutates: _PITCHER_CACHE
---

---
name: fetch_team_batting_fg
type: function
file: fetchers/savant.py
purpose: Aggregate FanGraphs batting stats for a full team roster. Returns PA-weighted team averages: wrc_plus (real), woba, iso, babip, k_pct, bb_pct, obp, slg. Replaces ESPN wRC+ approximation when available. Uses _ESPN_TO_FG_ABBR mapping for team name normalization.
inputs: team_abbr: str, season: int | None
outputs: dict
calls: _load_fg_batters
called_by: enrich_team_hitting
mutates: _BATTER_CACHE
---

---
name: fetch_team_statcast
type: function
file: fetchers/savant.py
purpose: Fetch team-level Statcast aggregates from Baseball Savant CSV export. player_type="pitcher" → barrel% against, hard-hit% against, xwOBA against. player_type="batter" → team barrel%, exit velo, xwOBA. Runs without pybaseball via direct httpx CSV fetch.
inputs: team_abbr: str, season: int | None, player_type: str = "pitcher"
outputs: dict
calls: httpx, pd.read_csv
called_by: (available for future use)
mutates: none
---

---
name: fetch_pitcher_statcast
type: function
file: fetchers/savant.py
purpose: Fetch individual pitcher Statcast metrics from Baseball Savant leaderboard. Returns barrel_pct_against, hard_hit_pct_against, exit_velo_against, xwoba_against. Uses pybaseball.statcast_pitcher_exitvelo_barrels() with Savant "Last, First" name matching.
inputs: player_name: str, season: int | None
outputs: dict
calls: pyb.statcast_pitcher_exitvelo_barrels, _match_name_savant, _safe_float
called_by: enrich_starter
mutates: none
---

---
name: enrich_starter
type: function
file: fetchers/savant.py
purpose: Non-destructively enrich an ESPN starter dict with FanGraphs SIERA/xFIP and Savant barrel%/xwOBA. NOW CHECKS THE PRECOMPUTED FEATURE STORE FIRST (fetchers.feature_store.lookup_pitcher_features): if the committed store has the pitcher, uses those fields, sets feature_source="store", and returns WITHOUT live-fetching (FanGraphs/Savant are IP-blocked from datacenter/CI). Falls through to live fetch only when the store misses (e.g. local dev on residential IP). Priority for fip field: SIERA > xFIP > FG FIP > original ESPN FIP; fip_source records which. Returns new merged dict; original unchanged.
inputs: starter: dict, team_abbr: str = "", season: int | None
outputs: dict (adds feature_source="store" when store hit)
calls: feature_store.lookup_pitcher_features, fetch_pitcher_fg, fetch_pitcher_statcast, fetch_pitcher_arsenal
called_by: run_baseball_analysis (analyze_baseball.py step 2.5)
mutates: none
---

---
name: feature_store
type: module
file: fetchers/feature_store.py
purpose: Reader for the committed pitcher feature store (data/nrfi_feature_store.json) — the fix for FanGraphs/Savant being IP-blocked from servers (Actions/Railway). load_store() (cached), store_meta() (season/built_at/n_pitchers), store_freshness() (age_days/is_stale>7d/is_expired>14d/label — used by write_report to flag data-integrity drift), lookup_pitcher_features(name) → precomputed {siera,xfip,fip,csw_pct,o_swing_pct,k_pct,bb_pct,gb_pct,hr_fb_pct,barrel_pct_against,hard_hit_pct_against,avg_fb_velo,whiff_pct,...} via normalized-name match (exact, then last-name+first-initial fallback). Returns {} when store absent so enrich_starter falls through to live fetch. Store is built locally by scripts/build_feature_store.py and committed.
inputs: name: str
outputs: dict
calls: json, datetime
called_by: enrich_starter (fetchers/savant.py), write_report (scripts/daily_nrfi.py)
mutates: none
---

---
name: build_feature_store
type: script
file: scripts/build_feature_store.py
purpose: Builds data/nrfi_feature_store.json LOCALLY (residential IP) so CI never live-fetches. BACKBONE = Baseball Savant exit-velo/barrels leaderboard (pyb.statcast_pitcher_exitvelo_barrels — one bulk call, MLB-official, rarely blocks) → pitcher universe + barrel%/hard-hit%/exit-velo/xwOBA. FanGraphs (SIERA/xFIP/CSW%/O-Swing%/K%/BB%/GB%/HR-FB) merged BEST-EFFORT and auto-skipped when its endpoint 403s (FanGraphs retired leaders-legacy.aspx → 403 for all IPs). --with-arsenal adds per-pitcher fastball velo/whiff (slow). Name key normalized from Savant 'Last, First'. NaN dropped. Even Savant-only (no FanGraphs) un-flattens the model: ESPN FIP + real Statcast per pitcher. Refresh every few days and commit.
inputs: --season <int>, --with-arsenal (flag)
outputs: data/nrfi_feature_store.json
calls: pybaseball.statcast_pitcher_exitvelo_barrels, fetchers.savant (_load_fg_pitchers, fetch_pitcher_arsenal, _current_season)
called_by: manual: python scripts/build_feature_store.py
mutates: data/nrfi_feature_store.json
---

---
name: enrich_team_hitting
type: function
file: fetchers/savant.py
purpose: Non-destructively enrich an ESPN team hitting dict with real FanGraphs wRC+, wOBA, ISO. Replaces the ESPN wRC+ approximation (2×OBP+SLG)/1.045×100 when FG data is available. wrc_plus_source key records "fangraphs" vs "espn".
inputs: hitting: dict, team_abbr: str, season: int | None
outputs: dict
calls: fetch_team_batting_fg
called_by: run_baseball_analysis (analyze_baseball.py step 2.5)
mutates: none
---

---
name: _ESPN_TO_FG_ABBR
type: variable
file: fetchers/savant.py
purpose: ESPN team abbreviation → FanGraphs abbreviation mapping for teams that differ (WSH→WSN, CWS→CHW, KC→KCR, SD→SDP, SF→SFG, TB→TBR).
inputs: none
outputs: dict[str, str]
calls: none
called_by: fetch_team_batting_fg
mutates: none
---

---
name: _PITCHER_CACHE / _BATTER_CACHE
type: variable
file: fetchers/savant.py
purpose: Module-level season caches keyed by year. Prevents re-fetching FanGraphs data for the same season within a single process run.
inputs: none
outputs: dict[int, pd.DataFrame]
calls: none
called_by: _load_fg_pitchers, _load_fg_batters
mutates: populated on first fetch per season
---

---

## fetchers/understat.py

---
name: fetch_league_xg
type: function
file: fetchers/understat.py
purpose: Fetch all teams' xG table for a European soccer league and season from Understat. Extracts JSON from page HTML via regex (no API key). Returns list of dicts with: team, xg, xga, npxg, npxga, xg_per_game, xga_per_game, goals, goals_against, matches, pts, position. Empty list on failure. Results cached in _LEAGUE_CACHE keyed by (league, season).
inputs: league: str, season: int
outputs: list[dict]
calls: _get, _extract_json_var
called_by: fetch_team_xg, _LEAGUE_CACHE
mutates: _LEAGUE_CACHE
---

---
name: fetch_team_xg
type: function
file: fetchers/understat.py
purpose: Fetch a single team's xG season stats from Understat league table. Uses _ESPN_TO_UNDERSTAT mapping + fuzzy matching for team name normalization. Returns dict with xg, xga, npxg, npxga, xg_per_game, xga_per_game, goals, goals_against, matches, position, source. Empty dict on failure.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: fetch_league_xg, _fuzzy_match
called_by: enrich_soccer_teams
mutates: none
---

---
name: fetch_team_recent_xg
type: function
file: fetchers/understat.py
purpose: Rolling xG/xGA averages over the last N matches (default 5) as a form indicator. Fetches team page HTML and extracts per-game datesData JSON. Returns xg_per_game, xga_per_game, goals_per_game, ga_per_game, matches_used. Empty dict on failure.
inputs: team_name: str, league: str, season: int, last_n: int = 5
outputs: dict
calls: _get, _extract_json_var
called_by: enrich_soccer_teams
mutates: none
---

---
name: enrich_soccer_teams
type: function
file: fetchers/understat.py
purpose: Enrich both home and away soccer teams with Understat xG, npxG, venue splits, situation splits, and NATIVE PPDA (Round 5 — new JSON endpoint includes ppda, ppda_allowed, deep, deep_allowed, xpts per match). Returns {"home", "away", "league_avg_goals", "league_avg_xg"}.
inputs: home_name: str, away_name: str, league: str, season: int
outputs: dict {"home": dict, "away": dict, "league_avg_goals": Optional[float], "league_avg_xg": Optional[float]}
calls: fetch_team_xg, fetch_team_decayed_xg, fetch_team_recent_xg, fetch_team_shot_quality, fetch_team_venue_splits, fetch_team_situation_split, fetch_team_ppda, fetch_league_avg_goals, fetch_league_avg_xg
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---
name: _LEAGUE_JSON_CACHE
type: variable
file: fetchers/understat.py
purpose: Module-level cache keyed by (league, season) → raw API response dict {teams, players, dates}. Set by _fetch_league_json on first hit, read by every team-level function. One call per (league, season) per process; downstream functions consume cached JSON instead of re-fetching individual team HTML pages.
inputs: none
outputs: dict[tuple, dict]
calls: none
called_by: _fetch_league_json
mutates: filled by _fetch_league_json
---

---
name: _fetch_league_json
type: function
file: fetchers/understat.py
purpose: Fetch the modern Understat AJAX endpoint /getLeagueData/<league>/<season>. Round 5 — Understat moved from embedded `var teamsData = JSON.parse(...)` in league HTML to a clean JSON AJAX endpoint around Nov 2025. Returns the raw response dict {teams: {id → {title, history[]}}, players, dates}. Requires X-Requested-With: XMLHttpRequest and Referer headers. Single ~500KB call provides every team's per-match xG/xGA/npxG/npxGA/PPDA/deep/xpts.
inputs: league: str, season: int
outputs: dict (empty on any failure)
calls: httpx.Client, _LEAGUE_JSON_CACHE
called_by: fetch_league_xg, _find_team_in_league
mutates: _LEAGUE_JSON_CACHE
---

---
name: _find_team_in_league
type: function
file: fetchers/understat.py
purpose: Find a single team's record {id, title, history[]} in the cached league JSON. Matches via _ESPN_TO_UNDERSTAT canonical map + fuzzy fallback against the live title list. Returns the team object or None.
inputs: team_name: str, league: str, season: int
outputs: Optional[dict]
calls: _fetch_league_json, _ESPN_TO_UNDERSTAT, _fuzzy_match
called_by: fetch_team_recent_xg, fetch_team_decayed_xg, fetch_team_venue_splits, fetch_team_ppda
mutates: none
---

---
name: fetch_team_ppda
type: function
file: fetchers/understat.py
purpose: Native Understat PPDA (passes per defensive action) — new in Round 5 from the JSON endpoint. Replaces fetchers.fbref.fetch_team_pressing's approximate proxy with real per-match values. Returns ppda (own pressing pressure, lower=more), ppda_allowed (opponent pressure on this team), deep (passes/crosses into the box per game), deep_allowed (same conceded), xpts_per_game.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _find_team_in_league
called_by: enrich_soccer_teams
mutates: none
---

---
name: _decayed_xg_from_history
type: function
file: fetchers/understat.py
purpose: Inner helper for fetch_team_decayed_xg. Given a list of completed match dicts and a half-life, computes time-decayed averages for xG/xGA/goals/ga with Kish effective sample size  n_eff = (Σw)² / Σ(w²)  (Round 3 #1 — sum_w over-counts when weights are dispersed; Kish equals n at equal weights and shrinks with dispersion, which is the correct quantity for Bayesian shrinkage and variance bounds).
inputs: completed: list[dict], half_life_days: float
outputs: dict {xg_per_game, xga_per_game, goals_per_game, ga_per_game, matches_used, effective_n, sum_weights, half_life_days}
calls: datetime parsing
called_by: fetch_team_decayed_xg
mutates: none
---

---
name: fetch_team_decayed_xg
type: function
file: fetchers/understat.py
purpose: Time-decayed xG/xGA over the full available season history (Kimi #2). Each match weight = 0.5 ** (days_ago / half_life_days). 90d default: 3-month-old match = 0.5x, 6 months = 0.25x. Smooth recency curve replacing the noisy fixed last-5 window. adaptive=True (Round 3 #4): if Kish effective_n < 12 at the requested half-life, retries at 150d then 240d to lengthen the window for sparse data (early season, promoted teams). Returns xg_per_game, xga_per_game (weighted), goals_per_game, ga_per_game, matches_used, effective_n (Kish), sum_weights, half_life_days_used. Empty dict on failure.
inputs: team_name: str, league: str, season: int, half_life_days: float = 90.0, adaptive: bool = True
outputs: dict
calls: _get, _extract_json_var, _ESPN_TO_UNDERSTAT, _decayed_xg_from_history
called_by: enrich_soccer_teams
mutates: none
---

---
name: fetch_league_avg_xg
type: function
file: fetchers/understat.py
purpose: League-wide avg xG per team per game from Understat (Kimi #6). Preferred over fetch_league_avg_goals as a model anchor because the model produces expected goals, not finished goals — and xG is ~5% higher than goals due to finishing variance. Returns None on failure so callers can fall back to models.soccer_leagues.league_avg_xg constants.
inputs: league: str, season: int
outputs: Optional[float]
calls: fetch_league_xg
called_by: enrich_soccer_teams, run_soccer_analysis
mutates: none
---

---
name: fetch_team_venue_splits
type: function
file: fetchers/understat.py
purpose: Compute home/away xG splits from a team's per-game Understat datesData. Filters on h_a field. Returns home_xg_per_game, home_xga_per_game, home_npxg_per_game, home_npxga_per_game and the away_* counterparts, plus home_matches / away_matches counts. Critical for proper Dixon-Coles — teams differ substantially in venue-specific scoring beyond a single home advantage multiplier.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _get, _extract_json_var
called_by: enrich_soccer_teams
mutates: none
---

---
name: fetch_team_situation_split
type: function
file: fetchers/understat.py
purpose: Split a team's offensive xG by Understat shot situation field (OpenPlay, FromCorner, SetPiece, DirectFreekick, Penalty). Open-play xG is materially more predictive of future scoring than season total because set pieces and penalties are noisy. Returns open_play_xg_share, set_piece_xg_share (combined corners+set+freekick), corner_xg_share, direct_fk_xg_share, penalty_xg_share, total_shots, open_play_xg_per_shot.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _get, _extract_json_var, _norm
called_by: enrich_soccer_teams
mutates: none
---

---
name: fetch_league_avg_goals
type: function
file: fetchers/understat.py
purpose: Compute league-wide avg goals per team per game from the Understat league table. Critical because hardcoded 1.35 over-fits EPL/La Liga and badly mis-scales Bundesliga (~1.55) and Ligue 1 (~1.25). Returns None on failure so callers fall back to models.soccer_leagues constants.
inputs: league: str, season: int
outputs: Optional[float]
calls: fetch_league_xg
called_by: enrich_soccer_teams, run_soccer_analysis
mutates: none
---

---
name: _ESPN_TO_UNDERSTAT
type: variable
file: fetchers/understat.py
purpose: Common/ESPN team name → Understat canonical name mapping. Covers EPL, La Liga, Bundesliga, Serie A, Ligue 1 variants.
inputs: none
outputs: dict[str, str]
calls: none
called_by: fetch_team_xg, fetch_team_recent_xg
mutates: none
---


---
name: fetch_pitcher_arsenal
type: function
file: fetchers/savant.py
purpose: Fetch pitch-mix and velocity profile from Baseball Savant pitch arsenal leaderboard. Returns fastball_pct, breaking_pct, offspeed_pct, avg_fb_velo, top_pitch_type, per-pitch usage% and whiff%. Implements Kimi-recommended pitch-level features. Uses pybaseball.statcast_pitcher_pitch_arsenal() with Savant "Last, First" name matching.
inputs: player_name: str, season: int | None
outputs: dict
calls: pyb.statcast_pitcher_pitch_arsenal, _match_name_savant, _safe_float
called_by: enrich_starter
mutates: none
---

---
name: fetch_team_shot_quality (understat.py)
type: function
file: fetchers/understat.py
purpose: Fetch shot-level quality metrics from Understat team page shotData JSON. Computes xg_per_shot (shot quality), sot_pct (shots on target %), goal_overperform (goals/xG ratio — regression signal: >1.15 likely to regress), xga_per_shot (quality conceded). Implements Kimi-recommended event-level features for soccer.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _get, _extract_json_var
called_by: enrich_soccer_teams
mutates: none
---


---

## fetchers/schedule.py

---
name: fetch_team_fatigue
type: function
file: fetchers/schedule.py
purpose: Compute bullpen fatigue and rest-day context from ESPN schedule. Counts games played in last 3 days by calling _get_scoreboard for each of the 3 prior dates and checking if the team appears. Returns games_last_3, rest_days, bullpen_fatigue_mult (0.97–1.08 applied to bullpen_fip), off_rest_mult (0.99–1.01 applied to wrc_plus in expected_runs_split). Falls back to neutral multipliers (1.0) on any ESPN failure.
inputs: team_id: str, game_date: str (ISO)
outputs: dict {games_last_3, rest_days, bullpen_fatigue_mult, off_rest_mult, source}
calls: _get_scoreboard (fetchers/baseball.py)
called_by: run_baseball_analysis (analyze_baseball.py step 2.6)
mutates: none
---


---
name: pitcher_process_adjustment
type: function
file: models/baseball_market.py
purpose: Converts pitch-level process metrics to a run-prevention multiplier for mu_f5. CSW% (1.7% per pp above avg), avg_fb_velo (1% per mph above avg), o_swing_pct (1.2% per pp above avg), barrel_pct_against (2.5% per pp above avg, inverted — high barrel% = MORE runs). Each signal capped ±8%; combined ±15%. Returns 1.0 when all None. Applied to mu_f5 only. Kimi-validated: barrel% has ~0.85 R² with future ERA, capturing contact quality FIP/SIERA miss.
inputs: csw_pct: Optional[float], avg_fb_velo: Optional[float], o_swing_pct: Optional[float], barrel_pct_against: Optional[float]
outputs: float (0.85–1.15)
calls: none
called_by: expected_runs_split
mutates: none
note: Fixed 2026-07-05 (major bug) — barrel_pct_against always arrives as a raw percent from the Savant feature store (e.g. 10.4), never a decimal, but LEAGUE_AVG_BARREL_PCT (0.075) is a decimal fraction. A raw percent always blew past the ±0.08 cap, so EVERY pitcher — elite or poor contact suppression alike — silently clamped to the exact same +8% run-inflation adjustment, destroying the signal's differentiation entirely (verified: barrel% 4.0 through 12.0 all produced the identical 1.08 multiplier before the fix). Now converts internally (÷100 when >1.5) so real barrel-rate variation actually differentiates pitchers again. csw_pct/o_swing_pct were NOT affected — they're already decimal-scale via the feature store's FanGraphs CSV import (_to_decimal in scripts/build_feature_store.py).
---


---

## fetchers/esports.py

---
name: PANDASCORE_API_KEY
type: variable
file: fetchers/esports.py
purpose: PandaScore API key from env var PANDASCORE_API_KEY; empty string disables PandaScore calls and falls back to Claude AI estimates.
inputs: none
outputs: str
calls: none
called_by: _pandascore_get
mutates: none
---

---
name: _normalise_game
type: function
file: fetchers/esports.py
purpose: Normalise free-text game name (e.g. "csgo", "League") to a PandaScore slug (cs2, lol, dota2, valorant).
inputs: raw: str
outputs: str
calls: none
called_by: fetch_esports_context, _search_team, _team_recent_matches, _h2h_from_pandascore
mutates: none
---

---
name: _pandascore_get
type: function
file: fetchers/esports.py
purpose: Authenticated GET to PandaScore API with retry on 429; returns empty list when PANDASCORE_API_KEY is absent.
inputs: path: str, params: Optional[dict], retries: int = 2
outputs: list | dict
calls: requests.get
called_by: _search_team, _team_recent_matches, _h2h_from_pandascore
mutates: none
---

---
name: _search_team
type: function
file: fetchers/esports.py
purpose: Search PandaScore for a team by name in a given game; prefers exact name match, falls back to first result.
inputs: name: str, game: str
outputs: dict (PandaScore team object) or {}
calls: _pandascore_get
called_by: fetch_esports_context
mutates: none
---

---
name: _team_recent_matches
type: function
file: fetchers/esports.py
purpose: Fetch last n completed matches for a team ID from PandaScore.
inputs: team_id: int, game: str, n: int = 10
outputs: list
calls: _pandascore_get
called_by: fetch_esports_context
mutates: none
---

---
name: _compute_form
type: function
file: fetchers/esports.py
purpose: Compute win rate (0.0–1.0) for a team from a list of PandaScore match objects.
inputs: team_id: int, matches: list
outputs: float
calls: none
called_by: fetch_esports_context
mutates: none
---

---
name: _h2h_from_pandascore
type: function
file: fetchers/esports.py
purpose: Retrieve head-to-head wins and last encounters for two PandaScore team IDs.
inputs: team_a_id: int, team_b_id: int, game: str, n: int = 10
outputs: dict {wins_a, wins_b, last_encounters}
calls: _pandascore_get
called_by: fetch_esports_context
mutates: none
---

---
name: _fallback_team_info
type: function
file: fetchers/esports.py
purpose: Claude AI fallback — estimates world ranking, recent form, and region when PandaScore is unavailable.
inputs: name: str, game: str
outputs: dict {ranking, form, region}
calls: anthropic.Anthropic (claude-haiku-4-5-20251001)
called_by: fetch_esports_context
mutates: none
---

---
name: fetch_esports_context
type: function
file: fetchers/esports.py
purpose: Main e-sports data entry — returns team data, H2H, and sources for two teams. Tries PandaScore first; falls back to Claude AI estimates for any missing team.
inputs: team_a: str, team_b: str, game: str
outputs: dict {team_a, team_b, game, team_a_data, team_b_data, h2h, sources}
calls: _search_team, _team_recent_matches, _compute_form, _h2h_from_pandascore, _fallback_team_info
called_by: run_esports_analysis (analyze_esports.py)
mutates: none
---

---

## ai_agent_esports.py

---
name: parse_esports_query
type: function
file: ai_agent_esports.py
purpose: Extract team_a, team_b, game, format, date, notes from free-text. Claude claude-haiku-4-5-20251001 first; heuristic regex fallback.
inputs: user_text: str
outputs: dict {team_a, team_b, game, format, date, notes}
calls: anthropic.Anthropic (claude-haiku-4-5-20251001)
called_by: run_esports_analysis (analyze_esports.py)
mutates: none
---

---
name: generate_esports_narrative
type: function
file: ai_agent_esports.py
purpose: Generate a 2-3 sentence analytical match preview using Claude. Falls back to template string on API failure.
inputs: team_a, team_b, game, prob_a, prob_b, team_a_data, team_b_data, recommendation, data_confidence, notes
outputs: str
calls: anthropic.Anthropic (claude-haiku-4-5-20251001)
called_by: run_esports_analysis (analyze_esports.py)
mutates: none
---

---

## analyze_esports.py

---
name: _elo_from_ranking
type: function
file: analyze_esports.py
purpose: Convert world ranking to Elo-like rating. Rank 1 ≈ 2200, floors at 1300. Formula: max(1300, 2200 − 400×log10(rank)).
inputs: ranking: int
outputs: float
calls: math.log10
called_by: run_esports_analysis
mutates: none
---

---
name: _elo_prob
type: function
file: analyze_esports.py
purpose: Standard Elo win probability for team A given two Elo ratings.
inputs: elo_a: float, elo_b: float
outputs: float (0–1)
calls: none
called_by: run_esports_analysis
mutates: none
---

---
name: _h2h_adjustment
type: function
file: analyze_esports.py
purpose: Nudge probability by H2H record; max ±3pp, requires ≥3 encounters.
inputs: prob: float, wins_a: int, wins_b: int
outputs: float
calls: none
called_by: run_esports_analysis
mutates: none
---

---
name: _build_plain_summary_esports
type: function
file: analyze_esports.py
purpose: Plain-English e-sports bet recommendation in user-preferred phrasing — "<favourite> is gonna win vs <opponent> (XX% accuracy on <game>). Bet on: 2-0 (or 3-0) map sweep, -1.5 maps handicap, first map." Gated: sweep + -1.5 handicap when win_prob >=0.70 (map count adapts to Bo3 vs Bo5); first map when 0.58-0.70. Headlines: "gonna win" (>=60%), "slight favourite" (52-60%), "coin-flip" (<52%).
inputs: team_a: str, team_b: str, prob_a: float, prob_b: float, game: str, fmt: str
outputs: str
calls: none
called_by: run_esports_analysis
mutates: none
---

---
name: run_esports_analysis
type: function
file: analyze_esports.py
purpose: Full e-sports pipeline: parse query → fetch PandaScore/Claude context → Elo from ranking → 60% Elo + 40% form blend → H2H adjustment → market comparison → Kelly → persist DB → AI narrative → plain_summary in user-preferred phrasing.
inputs: user_query: str, bankroll: float = 1000.0, odds_a_american: Optional[float], odds_b_american: Optional[float]
outputs: dict {match_id, team_a, team_b, game, format, date, recommendation, recommendation_reason, plain_summary, prob_a, prob_b, team_stats, h2h, market_comparison, kelly, kelly_note, kelly_edge, narrative, data_confidence, raw_sources, steps}
calls: parse_esports_query, fetch_esports_context, american_to_decimal, market_edge_summary, kelly_stake, get_db, log_signal, generate_esports_narrative, _build_plain_summary_esports
called_by: analyze_esports (app.py POST /analyze-esports)
mutates: matches, predictions, signals tables
---

---

## templates/tennis.html

---
name: tennis.html
type: template
file: templates/tennis.html
purpose: Tennis analysis UI — natural language query → POST /analyze-tennis → renders player stats (SQI, RQI, surface win rate, form, ranking), probability bars, recommendation badge, H2H, last 5 results, narrative, and pipeline trace.
inputs: none
outputs: HTML
calls: /analyze-tennis API
called_by: tennis_ui (app.py GET /tennis)
mutates: none
---

---

## templates/ping_pong.html

---
name: ping_pong.html
type: template
file: templates/ping_pong.html
purpose: Ping Pong (table tennis) analysis UI — natural language query + optional American odds inputs → POST /analyze-table-tennis → renders player stats (AQI, RQI, form, style, ranking), probability bars, market comparison card (VALUE/PASS/SLIGHT EDGE/AVOID), recommendation badge, H2H, recent form, narrative.
inputs: none
outputs: HTML
calls: /analyze-table-tennis API
called_by: ping_pong_ui (app.py GET /ping-pong)
mutates: none
---

---

## templates/esports.html

---
name: esports.html
type: template
file: templates/esports.html
purpose: E-Sports analysis UI — game selector tabs (CS2/LoL/Dota2/Valorant), natural language query + optional American odds → POST /analyze-esports → renders team stats (ranking, Elo, form, region), probability bars, market comparison, recommendation, H2H, narrative, pipeline trace.
inputs: none
outputs: HTML
calls: /analyze-esports API
called_by: esports_ui (app.py GET /esports)
mutates: none
---

---

## app.py (new sport UI routes)

---
name: tennis_ui
type: function
file: app.py
purpose: GET /tennis — serves tennis.html analysis UI.
inputs: none
outputs: HTMLResponse
calls: none
called_by: GET /tennis
mutates: none
---

---
name: ping_pong_ui
type: function
file: app.py
purpose: GET /ping-pong — serves ping_pong.html analysis UI.
inputs: none
outputs: HTMLResponse
calls: none
called_by: GET /ping-pong
mutates: none
---

---
name: esports_ui
type: function
file: app.py
purpose: GET /esports — serves esports.html analysis UI.
inputs: none
outputs: HTMLResponse
calls: none
called_by: GET /esports
mutates: none
---

---
name: analyze_esports
type: function
file: app.py
purpose: POST /analyze-esports — runs full e-sports pipeline from natural language query + optional American odds.
inputs: body: EsportsRequest {query, bankroll, odds_a_american, odds_b_american}
outputs: dict (full analysis result from run_esports_analysis)
calls: run_esports_analysis
called_by: HTTP POST /analyze-esports
mutates: matches, predictions, signals tables
---

---

## app.py (prediction audit)

---
name: _categorize_prediction
type: function
file: app.py
purpose: Assign a failure (or success) category to a resolved prediction. Returns one of 7 labels: CORRECT, CORRECT_HIGH_CONF, HIGH_CONFIDENCE_MISS, CLOSE_GAME, COIN_FLIP, WEATHER_IMPACT, LOW_DATA_QUALITY, NORMAL_VARIANCE. Called per-row inside prediction_audit at query time — nothing is stored; categories are computed fresh from logged signals + probability.
inputs: was_correct: bool, prob_a: float, score_a: int|None, score_b: int|None, sport: str, signals: dict
outputs: str (category label)
calls: none
called_by: prediction_audit
mutates: none
---

---
name: prediction_audit
type: function
file: app.py
purpose: GET /prediction-audit?sport=&limit=100 — returns resolved predictions with full logged signals and failure category per row. Summary block includes total, correct count, accuracy_pct, and category_counts dict. Used by audit UI and for copy-for-AI export.
inputs: sport: Optional[str] = None, limit: int = 100 (query params)
outputs: dict {total, correct, accuracy_pct, category_counts, predictions: list[dict]}
calls: get_db, _categorize_prediction
called_by: GET /prediction-audit, audit.html (fetch)
mutates: none
---

---
name: audit_ui
type: function
file: app.py
purpose: GET /audit — serves audit.html, the prediction failure analysis UI.
inputs: none
outputs: HTMLResponse
calls: none
called_by: GET /audit
mutates: none
---

---

## templates/audit.html

---
name: audit.html
type: template
file: templates/audit.html
purpose: Prediction audit and failure analysis UI. Fetches GET /prediction-audit on load. Displays summary cards (total, correct, wrong, accuracy, high-conf misses, close games). Category legend chips filter the table. Per-row: date, sport badge, match, predicted team+prob+bar, actual result (color-coded), score, failure_category badge, key signals preview with expand button. Copy panel exports AI-ready structured text via 4 buttons: Copy All, Copy Failures Only, Copy Baseball, Copy Summary Only. Output includes Kimi's 7 model-design questions for AI reviewer context.
inputs: none (static HTML; fetches /prediction-audit via JS)
outputs: HTMLResponse
calls: GET /prediction-audit
called_by: GET /audit (audit_ui in app.py)
mutates: none
---

## scripts/build_nrfi_dataset.py

---
name: build_nrfi_dataset
type: script
file: scripts/build_nrfi_dataset.py
purpose: One-time scraper that builds data/nrfi_dataset.csv — the training set for the NRFI XGBoost model. Pulls 2022–2024 MLB regular-season games from MLB Stats API (linescore for NRFI outcome, boxscore for starter names + top-3 lineup batters via battingOrder field), looks up per-season pitcher stats from FanGraphs (via pybaseball: SIERA, xFIP, CSW%, O-Swing%, K%, BB%, GB%, HR/FB%) and batter wRC+ for lineup spots 1-3 (home_top3_wrc, away_top3_wrc). Writes per-season checkpoints so the run can be resumed if interrupted. Run locally (external HTTP blocked in container).
inputs: --seasons (default 2022 2023 2024), --output (default data/nrfi_dataset.csv), --delay (default 1.0s)
outputs: data/nrfi_dataset.csv (~7300 rows), data/nrfi_checkpoint_{year}.csv per season
calls: MLB Stats API, pybaseball.pitching_stats, pybaseball.batting_stats
called_by: manual (one-time): python scripts/build_nrfi_dataset.py
mutates: data/nrfi_dataset.csv
key_functions: get_starters (extracts top-3 batters from battingOrder), _load_fg_batters_season, lookup_top3_wrc, _load_fg_season, lookup_pitcher_fg, build_season, get_season_game_pks, get_linescore
---

---

## scripts/enrich_nrfi_fi_rates.py

---
name: enrich_nrfi_savant
type: script
file: scripts/enrich_nrfi_savant.py
purpose: Post-processing enrichment for data/nrfi_dataset.csv using Baseball Savant Statcast data (accessible; no 403 block unlike FanGraphs). For each pitcher and season, looks up: xwoba_against (expected wOBA allowed), barrel_pct (barrel rate against), hard_hit_pct, whiff_pct, avg_velo. Uses statcast_pitcher_exitvelo_barrels (called WITHOUT minPA kwarg — invalid in current pybaseball), statcast_pitcher_percentile_ranks, statcast_pitcher_pitch_arsenal from pybaseball. Pitch arsenal step: searches for velocity column among (avg_speed, mph, release_speed, velocity, mean_speed); prints actual column names for debugging; creates new lookup entry for pitchers not seen in earlier steps so avg_velo is always populated when pitch_arsenal succeeds. Name matching: exact normalized → last-name exact → fuzzy (cutoff 0.82). Defaults to MLB averages when no match found. Run AFTER enrich_nrfi_fi_rates.py.
inputs: data/nrfi_dataset.csv
outputs: data/nrfi_dataset.csv (adds home/away_xwoba_against, barrel_pct, hard_hit_pct, whiff_pct, avg_velo columns)
calls: pybaseball.statcast_pitcher_exitvelo_barrels, statcast_pitcher_percentile_ranks, statcast_pitcher_pitch_arsenal
called_by: manual: python scripts/enrich_nrfi_savant.py
mutates: data/nrfi_dataset.csv
---

---
name: enrich_nrfi_fip_bref
type: script
file: scripts/enrich_nrfi_fip_bref.py
purpose: Backfills home_fip/away_fip (and home_k_pct/away_k_pct/home_bb_pct/away_bb_pct) in data/nrfi_dataset.csv using pybaseball.pitching_stats_bref() without requiring a full dataset rebuild. Only overwrites NaN cells — does not clobber existing FanGraphs values. Computes FIP from BRef components (HR/BB/HBP/SO/IP) when BRef doesn't return a pre-computed FIP column; FIP_CONSTANT=3.15. Name matching: exact normalized → last-name exact → fuzzy (cutoff 0.82). Run AFTER enrich_nrfi_savant.py and BEFORE nrfi_model.py --train.
inputs: data/nrfi_dataset.csv
outputs: data/nrfi_dataset.csv (fills NaN in home_fip, away_fip, home_k_pct, away_k_pct, home_bb_pct, away_bb_pct)
calls: pybaseball.pitching_stats_bref
called_by: manual: python scripts/enrich_nrfi_fip_bref.py
mutates: data/nrfi_dataset.csv
---

---
name: enrich_nrfi_fi_rates
type: script
file: scripts/enrich_nrfi_fi_rates.py
purpose: Post-processing enrichment for data/nrfi_dataset.csv. Computes SHORT-WINDOW, TIME-DECAYED venue-split per-starter first-inning run rate (no API calls, ~5 sec) — combats baseball data drift (Kimi feedback 2026-07). _decayed_rate() keeps only the last WINDOW=6 starts, weights them by exponential decay (HALF_LIFE_DAYS=21, a start halves in weight every 3 weeks), and shrinks toward the league mean by PRIOR_STARTS=2 pseudo-starts to stabilize thin samples. home_starter_fi_rate = recent HOME-start rate; away_starter_fi_rate = recent AWAY-start rate. Fallback cascade: venue-specific (≥MIN_STARTS_VENUE=3) → overall (≥MIN_STARTS=3) → dynamic league average. No look-ahead (histories updated after read). Must be run after build_nrfi_dataset.py and before nrfi_model.py --train (retrain required for changes to take effect).
inputs: data/nrfi_dataset.csv
outputs: data/nrfi_dataset.csv (adds home_starter_fi_rate, away_starter_fi_rate columns)
calls: pandas, math (_decayed_rate)
called_by: manual: python scripts/enrich_nrfi_fi_rates.py
mutates: data/nrfi_dataset.csv
---

---

## scripts/daily_nrfi.py

---
name: daily_nrfi
type: script
file: scripts/daily_nrfi.py
purpose: Automated daily NRFI pipeline. Morning mode (default): fetches today's MLB schedule, runs NRFI analysis for each game via run_baseball_analysis(), saves raw predictions to data/nrfi_predictions/YYYY-MM-DD.json and a markdown report to data/nrfi_reports/YYYY-MM-DD.md. Night/resolve mode (--resolve): loads yesterday's prediction JSON, fetches final linescores from MLB Stats API, computes NRFI/YRFI outcome for each game, and appends a Results section to the markdown report. Triggered by GitHub Actions nrfi_daily.yml (predict 9 AM ET, resolve 1 AM ET).
inputs: --resolve (flag), --date YYYY-MM-DD (optional override)
outputs: data/nrfi_predictions/YYYY-MM-DD.json, data/nrfi_reports/YYYY-MM-DD.md
calls: run_baseball_analysis (analyze_baseball.py), MLB Stats API (/schedule, /game/{pk}/linescore), ESPN cache fallback (data/live/mlb_scoreboard.json)
called_by: .github/workflows/nrfi_daily.yml (cron), manual: python scripts/daily_nrfi.py
mutates: data/nrfi_predictions/, data/nrfi_reports/
---

name: fetch_schedule
type: function
file: scripts/daily_nrfi.py
purpose: Returns list of game dicts {game_pk, home_abbr, away_abbr, home_name, away_name, game_time} for a given date. Primary: MLB Stats API /schedule. Fallback: ESPN scoreboard cache at data/live/mlb_scoreboard.json.
inputs: game_date: str (YYYY-MM-DD)
outputs: list[dict]
calls: _fetch_schedule_mlb, _fetch_schedule_espn_cache
called_by: main (daily_nrfi.py)
mutates: none
---

name: run_predictions
type: function
file: scripts/daily_nrfi.py
purpose: Loops over schedule games, calls run_baseball_analysis() for each, extracts NRFI market probabilities, verdict, Kelly staking, starters, and all-market picks (ml_pick/ml_prob/ml_verdict for moneyline, f5_pick/f5_prob/f5_verdict for F5) from bet_recs. Also captures lean_side (NRFI or YRFI based on p_nrfi vs 0.50) and feature-source diagnostics. Returns list of prediction records. Sleeps 0.5s between games to avoid rate limiting. Checks `result.get("error")` immediately after the call (added 2026-07-05) and raises so it lands in the except block as a flagged error record — previously a failed run_baseball_analysis call (e.g. Anthropic query-parse API error) fell through silently into building a record with every field None but no "error" key, indistinguishable in the report from a genuine model SKIP. Root-caused after a live run on 2026-07-04 produced 11 blank games with no error indication.
inputs: games: list[dict], game_date: str
outputs: list[dict]
calls: run_baseball_analysis (analyze_baseball.py)
called_by: main (daily_nrfi.py)
mutates: none
---

name: resolve_predictions
type: function
file: scripts/daily_nrfi.py
purpose: Loads prediction JSON for a given date, fetches MLB Stats API linescores, resolves ALL four markets: NRFI (1st-inning outcome), moneyline (final winner via ml_correct), F5 (leader after 5 innings via f5_correct), Over/Under (total runs vs ou_line via ou_correct). Also computes lean_side/lean_correct for every game and W/L/P&L for BET/LEAN plays. fetch_linescore now returns full game data: home_1st/away_1st, home_runs/away_runs (finals), home_f5/away_f5 (through 5).
inputs: game_date: str
outputs: list[dict] (updated predictions)
calls: fetch_linescore (MLB Stats API /game/{pk}/linescore)
called_by: main (daily_nrfi.py)
mutates: none (caller saves to disk)
---

---
name: resolve_pending_bets
type: function
file: scripts/daily_nrfi.py
purpose: Added 2026-07-05. Grades nrfi_bets DB rows that have a game_pk but no outcome yet — closes the loop for ad-hoc manual website queries, which _log_prediction persists to the DB on every call but which the JSON-file pipeline (resolve_predictions) never sees since that only covers the scheduled slate it fetched itself. Reuses fetch_linescore so both paths grade identically (NRFI outcome/won/pnl_units, plus ml_correct/f5_correct/ou_correct by comparing the stored pick strings to home_team/away_team and the real linescore). Skips rows whose game_date is too recent (game may be in progress) or older than max_age_days (avoids repeatedly hitting the API for stale unresolved rows).
inputs: max_age_days: int (default 14)
outputs: {checked: int, graded: int}
calls: fetch_linescore, get_db
called_by: tasks/nrfi_auto.run_resolve (nightly, automatic)
mutates: nrfi_bets (UPDATE)
---

name: write_report
type: function
file: scripts/daily_nrfi.py
purpose: Writes markdown report to data/nrfi_reports/YYYY-MM-DD.md. Sections: Live Validation Tracker, Feature Store Freshness (age + stale/expired warnings via store_freshness()), All Games — Model Picks table (validation-status header separating NRFI ✅ validated from moneyline/F5 unvalidated; ⭐ edge flags; "Best play" column), Plays table (BET/LEAN only — columns: #, Matchup, Starter H/A, p_NRFI, Verdict, CLV, Model; Half-Kelly column removed 2026-07-04 per Kimi's review — public report shows verdicts only, not bet-sizing advice), Skipped Games table, Results summary (resolve mode only), Errors list.
inputs: predictions: list[dict], game_date: str, is_resolve: bool
outputs: Path (written file)
calls: nrfi_store.validation_tracker, fetchers.feature_store.store_freshness
called_by: main (daily_nrfi.py)
mutates: data/nrfi_reports/YYYY-MM-DD.md
---

## .github/workflows/nrfi_daily.yml

---
name: nrfi_daily
type: workflow
file: .github/workflows/nrfi_daily.yml (REMOVED 2026-07-02)
purpose: REMOVED — superseded by tasks/nrfi_auto.py (always-on Railway scheduler). GitHub Actions `schedule` cron proved unreliable (delayed/skipped runs) and its `git push` raced with Railway's Contents-API pushes to main (non-fast-forward rejections). Deleted so there is exactly ONE writer to main. Run predictions on demand via GET or POST /nrfi-auto/run?job=predict instead of the old workflow_dispatch.
---

name: capture_odds
type: function
file: scripts/daily_nrfi.py
purpose: Fetch current NRFI/YRFI odds from The Odds API and attach to each prediction record for Closing Line Value (CLV). phase="entry" fills entry_* (line when pick was made), records entry_hours_to_fp (hours before first pitch via _hours_before_first_pitch) + entry_stale flag (True if < MIN_ENTRY_LEAD_HOURS=3h before first pitch, so stale entries can be excluded from headline CLV), and seeds closing_*; phase="closing" updates closing_*. Graceful no-op (returns 0) when ODDS_API_KEY unset or no market match. Mutates records in place.
inputs: predictions: list[dict], phase: str ("entry"|"closing")
outputs: int (games updated)
calls: fetchers.nrfi_odds.fetch_nrfi_odds
called_by: main (daily_nrfi.py) predict + capture-odds modes
mutates: prediction record dicts
---

name: _compute_clv
type: function
file: scripts/daily_nrfi.py
purpose: Computes clv_pp + beat_close on a resolved prediction record (in place) from entry vs closing NRFI lines via models.devig.nrfi_clv. Mirrors odds/CLV onto the matching nrfi_bets row best-effort (_mirror_clv_to_db). Called for each game during resolve.
inputs: pred: dict
outputs: none (mutates pred)
calls: models.devig.nrfi_clv, _mirror_clv_to_db
called_by: resolve_predictions
mutates: prediction record dict, nrfi_bets (best-effort UPDATE)
---

## fetchers/nrfi_odds.py

---
name: fetch_nrfi_odds
type: function
file: fetchers/nrfi_odds.py
purpose: Fetch first-inning NRFI/YRFI odds from The Odds API (market totals_1st_1_innings, line 0.5; Under=NRFI, Over=YRFI) for a list of games. Matches events by fuzzy team-name comparison, prefers Pinnacle then other sharp books, else median across books. Returns {"{away_abbr}@{home_abbr}": {nrfi_dec, yrfi_dec, book, captured_at}}. Empty dict when ODDS_API_KEY unset or API unreachable (graceful).
inputs: games: list[dict] (home_abbr, away_abbr, home_name, away_name)
outputs: dict[str, dict]
calls: The Odds API /sports/baseball_mlb/events + /events/{id}/odds (httpx)
called_by: scripts/daily_nrfi.capture_odds
mutates: none
---

## models/devig.py (nrfi_clv)

---
name: nrfi_clv
type: function
file: models/devig.py
purpose: Closing Line Value for a 2-way NRFI/YRFI bet — the vig-free implied probability of the bet side at the CLOSING line minus the same side at the ENTRY line. Positive = market moved toward our pick after we bet it = beat the close. Returns {clv_pp (percentage points), entry_fair, close_fair, beat_close}. This is the fastest-accumulating proof of edge (independent of game outcome).
inputs: bet_side: str, entry_nrfi_dec, entry_yrfi_dec, close_nrfi_dec, close_yrfi_dec: float
outputs: dict
calls: devig_market
called_by: scripts/daily_nrfi._compute_clv, nrfi_store.aggregate_clv (indirect via stored values)
mutates: none
---

## api_auth.py

---
name: require_api_key
type: function
file: api_auth.py
purpose: FastAPI dependency for the public /v1 endpoints. Validates the X-API-Key header against keys configured in PREDICTA_API_KEYS ("key:label,key:label"). Returns the client label on success; raises 401 when keys are configured and the header is missing/invalid. Open "dev mode" (returns "dev", no key required) when PREDICTA_API_KEYS is unset — keeps local dev + existing UI working.
inputs: x_api_key: Optional[str] (Header)
outputs: str (client label)
calls: _load_keys
called_by: app.py /v1/* endpoints (Depends)
mutates: none
---

name: auth_enabled
type: function
file: api_auth.py
purpose: True when PREDICTA_API_KEYS is configured (production lock-down active). Used by /v1/status to report auth state.
inputs: none
outputs: bool
calls: _load_keys
called_by: app.v1_status
mutates: none
---

## nrfi_store.py

---
name: nrfi_store
type: module
file: nrfi_store.py
purpose: Read + aggregate helpers over the committed daily NRFI prediction files (data/nrfi_predictions/YYYY-MM-DD.json) — the persistent, auditable source of truth for the public /v1 API (git-versioned, survives redeploys, unlike the ephemeral CI database). Functions: list_dates(), latest_date(), load_date(date), aggregate_performance(days) → W/L+ROI+CI+p-value+CLV summary with verdict (EDGE PROVEN / BEATING THE CLOSE / EDGE EXISTS / TOO EARLY / NO EDGE) plus a state-aware honesty `disclaimer` (no profit claim before edge proven), aggregate_clv(days) → n, avg_clv_pp, beat_close_pct, avg_entry_lead_hrs, n_stale_excluded (headline CLV excludes entry_stale plays so it measures leading the market, not moving with it), clv_vs_winrate(days) → buckets resolved plays by CLV + Pearson corr + t-test verdict (detects whether positive CLV predicts winners or is just line-chasing), validation_tracker(days) → one-glance proof snapshot rendered atop every daily report, aggregate_market_performance(days) → moneyline/F5/O-U win-rate rollup (added 2026-07-04 — resolve_predictions already grades ml_correct/f5_correct/ou_correct into each day's JSON, but nothing aggregated them across days before this; closes the "losses aren't being collected" gap for the three non-NRFI markets). Tracking operates over TWO universes: _plays() = actual BET/LEAN plays (staking ROI/P&L); _tracked() = every game with a model probability (any verdict incl. SKIP), scored on the model's lean_side/lean_correct so a predictive-accuracy + CLV record accumulates even while verdicts are SKIP (the 55% bet gate rarely fires when features default to league-average). aggregate_performance adds model_lean{n_games,n_resolved,lean_accuracy_pct}; aggregate_clv + clv_vs_winrate use _tracked.
inputs: game_date/days args
outputs: list[str] / list[dict] / dict summaries
calls: json, scipy.stats (optional)
called_by: app.py /v1/nrfi/* endpoints
mutates: none
---

## app.py (/v1 public API)

---
name: v1_nrfi_endpoints
type: endpoints
file: app.py
purpose: API-key-gated read-only endpoints for syndicate/media clients. GET /v1/status (health + coverage + client label), GET /v1/nrfi/predictions?date= (all records for a date, default latest), GET /v1/nrfi/plays?date= (BET/LEAN only; kelly_half/stake_100/edge_pct stripped from the response as of 2026-07-04 per Kimi's review — public API surfaces verdicts, not bet-sizing advice), GET /v1/nrfi/clv?days=30 (CLV summary), GET /v1/nrfi/performance?days= (W/L+ROI+CLV+verdict+disclaimer), GET /v1/nrfi/clv-quality?days= (CLV-vs-winrate diagnostic — significance-tested detection of line-chasing vs predictive edge). All read committed JSON via nrfi_store and depend on require_api_key.
inputs: query params (date, days), X-API-Key header
outputs: dict JSON
calls: nrfi_store.*, api_auth.require_api_key
called_by: FastAPI (HTTP), external API clients
mutates: none
---

## db/database.py (_migrate_nrfi_clv)

---
name: _migrate_nrfi_clv
type: function
file: db/database.py
purpose: Idempotent migration adding CLV columns to nrfi_bets: entry_nrfi_dec, entry_yrfi_dec, entry_book, entry_odds_at, closing_nrfi_dec, closing_yrfi_dec, closing_book, closing_odds_at, clv_pp, beat_close. Adds only missing columns (safe on every startup). Called by init_db after _migrate_new_tables.
inputs: conn: sqlite3.Connection
outputs: none
calls: PRAGMA table_info, ALTER TABLE
called_by: init_db
mutates: predicta.db (nrfi_bets schema)
---

## models/nrfi_model.py

---
name: nrfi_model
type: module
file: models/nrfi_model.py
purpose: XGBoost-based NRFI probability model. Replaces the rough Poisson mu/9 approximation with a calibrated classifier trained on historical first-inning MLB outcomes. Features: rolling per-starter first-inning run rate (home/away_starter_fi_rate — from enrich_nrfi_fi_rates.py), Savant Statcast metrics (xwoba_against, barrel_pct, hard_hit_pct, whiff_pct, avg_velo — from enrich_nrfi_savant.py), BRef pitcher stats (K%/BB%/FIP), FanGraphs stats (SIERA/xFIP when available), top-3 lineup wRC+ (home/away_top3_wrc), park_factor, is_dome. Platt scaling is skipped when val AUC < 0.52 (would amplify noise rather than correct). Trained on 2022-2023, tested on 2024 hold-out.
inputs: dataset: data/nrfi_dataset.csv (for training)
outputs: models/nrfi_xgb.json, models/nrfi_calibrator.pkl
calls: xgboost.XGBClassifier, sklearn.linear_model.LogisticRegression (Platt scaling)
called_by: predict_nrfi (imported by analyze_baseball.py)
mutates: models/nrfi_xgb.json, models/nrfi_calibrator.pkl (training only)
---

---
name: predict_nrfi
type: function
file: models/nrfi_model.py
purpose: Returns calibrated NRFI probability (float 0–1) using the trained XGBoost model. Returns None if model not trained yet (caller falls back to Poisson). Builds 32-feature vector in exact FEATURES list order; maps starter dict keys to training column names (barrel_pct_against→home_barrel_pct, hard_hit_pct_against→home_hard_hit_pct, avg_fb_velo→home_avg_velo). Optional home_fi_rate/away_fi_rate for rolling fi_rate (defaults to 0.29). Optional home_top3_wrc/away_top3_wrc for live lineup data (defaults to 100). Includes assertion to catch future FEATURES/fv length mismatches.
inputs: home_starter: dict, away_starter: dict, home_team: str, park_factor: Optional[float], home_top3_wrc: Optional[float], away_top3_wrc: Optional[float], home_fi_rate: Optional[float], away_fi_rate: Optional[float]
outputs: Optional[float] — calibrated probability of NRFI
calls: xgboost.XGBClassifier.predict_proba, LogisticRegression.predict_proba
called_by: run_baseball_analysis (analyze_baseball.py)
mutates: none
---

---
name: train (nrfi_model)
type: function
file: models/nrfi_model.py
purpose: Trains XGBoost on data/nrfi_dataset.csv. Dynamic train/val/test split: train=all seasons except two most recent, val=second-most-recent, test=most-recent. With 2022-2026: train=2022-2024, val=2025, test=2026. xwoba_against removed from FEATURES and diag_cols (statcast_pitcher_exitvelo_barrels does not return xwOBA). BET_THRESH=0.55 (lowered from 0.57 → 55% gives ~100-150 tagged games/season for faster edge validation; 57% gave only 20 games). Calibration curve header uses actual test_season variable. Platt scaling only applied when val AUC > 0.52. Saves models/nrfi_xgb.json and models/nrfi_calibrator.pkl. Run via: python models/nrfi_model.py --train

---

---
name: validate (nrfi_model)
type: function
file: models/nrfi_model.py
purpose: Walk-forward (expanding-window) validation across all seasons. For each season s except the first, trains on all prior seasons and predicts season s — no future data leaks. With 2022-2026 produces 4 folds: train 2022→predict 2023, train 2022-2023→predict 2024, etc. Combines all out-of-sample predictions (~8000+ games) and prints threshold analysis at 52-58% with win rate, edge over breakeven (-110 = 52.4%), 95% CI, and p-value (one-tailed z-test). Answers "is the edge real across all years, not just 2026?" Run via: python models/nrfi_model.py --validate
inputs: dataset_path: Path (default data/nrfi_dataset.csv)
outputs: none (side effect: saves model files)
calls: xgboost.XGBClassifier, LogisticRegression, sklearn metrics, calibration_curve
called_by: __main__ (CLI: python models/nrfi_model.py --train)
mutates: models/nrfi_xgb.json, models/nrfi_calibrator.pkl
---

---

## analyze_baseball.py (bet recommendations)

---
name: _bet_recommendations
type: function
file: analyze_baseball.py
purpose: Generate explicit BET / LEAN / SKIP verdicts for NRFI, F5, full-game moneyline, and Game Total (O/U). Full Game: BET requires >= 65% (was 62%) + data_confidence != "low" + no market disagreement > 20pp; F5: BET requires >= 62% (was 60%) + starter gap >= 1.0 + data_confidence != "low"; O/U: picks best line (6.5–10.5), BET >= 62%, LEAN >= 57%; NRFI: prob >= 55% + elite starter signal → BET. Market sanity check: when sportsbook odds provided, if model disagrees with market by > 20pp, downgrades ML to LEAN at best. Accepts bankroll param and attaches kelly dict to BET/LEAN recommendations.
inputs: formatted_markets: dict, home_starter: dict, away_starter: dict, team_home: str, team_away: str, bankroll: float, data_confidence: str, market_implied_home: float|None, market_implied_away: float|None
outputs: list[dict] — one entry per market (NRFI, F5, Full Game)
calls: _nrfi_kelly
called_by: run_baseball_analysis
mutates: none
---

---
name: _nrfi_kelly
type: function
file: analyze_baseball.py
purpose: Computes Kelly staking for an NRFI bet at given American odds. Returns full_kelly_pct, half_kelly_pct (recommended), recommended_stake (half-Kelly × bankroll), breakeven_pct, edge_pct. Standard odds default = -110 (juice).
inputs: p_nrfi_pct: float (0-100), bankroll: float, american_odds: float
outputs: dict with full_kelly_pct, half_kelly_pct, recommended_stake, breakeven_pct, edge_pct
calls: american_to_decimal (models/kelly.py)
called_by: _bet_recommendations
mutates: none
---

---
name: _log_prediction
type: function
file: analyze_baseball.py
purpose: Persists every prediction to nrfi_bets after run_baseball_analysis completes — renamed from _log_nrfi_prediction on 2026-07-05 when it was extended to also capture moneyline/F5/O-U (ml_pick/ml_prob/ml_verdict, f5_pick/f5_prob/f5_verdict, ou_pick/ou_prob/ou_verdict/ou_line) and game_pk, not just NRFI. Fires on EVERY call to run_baseball_analysis — both the automated daily pipeline AND ad-hoc manual website queries — so a one-off "Yankees vs Red Sox tonight" query gets logged and can be graded later by resolve_pending_bets(), not just the scheduled slate. Still only logs when the NRFI market is available; silently skips on any error so it never breaks the main prediction flow.
inputs: result: dict, game_date: str, home_team: str, away_team: str, home_starter: dict, away_starter: dict, bankroll: float
outputs: none
calls: get_db
called_by: run_baseball_analysis
mutates: nrfi_bets (INSERT)
---

---
name: nrfi_performance
type: route
file: app.py
purpose: GET /nrfi-performance — live performance dashboard for NRFI model. Reads nrfi_bets table, computes win rate + ROI + 95% CI + p-value at 55% and 57% thresholds, returns verdict (EDGE PROVEN / EDGE EXISTS / TOO EARLY / NO EDGE DETECTED). Requires scipy for p-value. Returns full bet history as all_bets list — each row now also includes ml_pick/ml_correct, f5_pick/f5_correct, ou_pick/ou_correct (added 2026-07-05) so manual-query moneyline/F5/O-U picks are visible here too, not just NRFI.
inputs: none (reads DB)
outputs: JSON — verdict, note, n_total, n_resolved, n_pending, threshold_55, threshold_57, all_bets
calls: get_db, scipy.stats.norm
called_by: GET /nrfi-performance
mutates: none
---

---
name: market_performance
type: route
file: app.py
purpose: GET /market-performance?days= — moneyline/F5/O-U win-rate rollup across all committed daily prediction files. Added 2026-07-04 so these three markets' resolved outcomes (already graded nightly by resolve_predictions but never aggregated before) don't have to be tallied by hand from individual JSON files. Diagnostic only — these markets aren't walk-forward validated like NRFI.
inputs: days (int, query param, default 3650)
outputs: JSON — n_games_tracked, moneyline{n_graded,n_correct,accuracy_pct,n_bets,bet_correct,bet_win_pct}, first_five{...}, over_under{...}, window_days, note
calls: nrfi_store.aggregate_market_performance
called_by: GET /market-performance
mutates: none
---

---
name: nrfi_resolve
type: route
file: app.py
purpose: POST /nrfi-resolve — marks a logged NRFI bet as resolved after the game is played. Accepts {id, home_1st_runs, away_1st_runs} or {game_date, home_team, away_team, home_1st_runs, away_1st_runs}. Team lookup uses exact match first then LIKE fallback so abbreviations (DET) resolve against full names (Detroit Tigers) in DB. Auto-computes outcome (1=NRFI/0=YRFI), won (1/0), pnl_units (+0.909 win / -1.0 loss at -110).
inputs: body: dict
outputs: {id, outcome, won, pnl_units}
calls: get_db
called_by: POST /nrfi-resolve
mutates: nrfi_bets (UPDATE)
---

---
name: api_nrfi
type: route
file: app.py
purpose: GET /api/nrfi?home=HOU&away=DET&date=2026-06-29&bankroll=500 — clean JSON endpoint for programmatic NRFI probability consumption by syndicates or automated bettors. Returns home/away starter names, p_nrfi, p_yrfi, verdict, confidence, kelly dict, and reasons. Thin wrapper around run_baseball_analysis.
inputs: home: str, away: str, date: str|None, bankroll: float
outputs: JSON with probability, verdict, kelly staking, reasons
calls: run_baseball_analysis
called_by: GET /api/nrfi
mutates: nrfi_bets (via run_baseball_analysis logging)
---

---
name: nrfi_bets
type: table
file: db/schema.sql
purpose: Tracks every prediction (NRFI + moneyline/F5/O-U) for live performance measurement — one row per game queried, from BOTH the automated daily pipeline and ad-hoc manual website queries. Columns: game_date, home/away team + starter, p_nrfi, verdict, confidence, kelly_full_pct, kelly_half_pct, recommended_stake, bankroll, market_odds, outcome (1=NRFI/0=YRFI/NULL=pending), home/away_1st_runs, won, pnl_units, logged_at, resolved_at. CLV columns (added by _migrate_nrfi_clv): entry_nrfi_dec/entry_yrfi_dec/entry_book/entry_odds_at, closing_nrfi_dec/closing_yrfi_dec/closing_book/closing_odds_at, clv_pp, beat_close. All-markets columns (added by _migrate_nrfi_all_markets, 2026-07-05): game_pk (lets resolve_pending_bets fetch the real linescore later), ml_pick/ml_prob/ml_verdict/ml_correct, f5_pick/f5_prob/f5_verdict/f5_correct, ou_pick/ou_prob/ou_verdict/ou_line/ou_correct, home_runs/away_runs. Before this migration, manual website queries only had their NRFI pick persisted — moneyline/F5/O-U picks were computed, shown once, and lost. Outcome filled via POST /nrfi-resolve (manual) or scripts/daily_nrfi.resolve_pending_bets (automatic, nightly via tasks/nrfi_auto.run_resolve); CLV mirrored by scripts/daily_nrfi._mirror_clv_to_db.
inputs: populated by _log_prediction (analyze_baseball.py)
outputs: read by GET /nrfi-performance
---

## nav link updates (Audit in all sport pages)

Added "Audit" link to the navbar of: home.html, sports.html, baseball.html, tennis.html, ping_pong.html, esports.html. All point to GET /audit. ping_pong.html also has diagnosis shortcut buttons (📊 TT Prediction Audit → /audit?sport=table_tennis, 🔬 Model Performance → /tt-performance).

---

## db/database.py (migrations)

---
name: _migrate_sport_check
type: function
file: db/database.py
purpose: Widens the matches.sport CHECK constraint to include all 5 active sports (soccer, table_tennis, tennis, baseball, esports). schema.sql now ships the full constraint, so on a fresh DB this returns immediately (no rename/rebuild). The rename→recreate→copy→drop path only runs to upgrade a pre-existing old-schema DB, and sets PRAGMA legacy_alter_table=ON around the RENAME so SQLite does NOT rewrite child-table foreign keys (predictions/signals/outcomes/odds_snapshots) to point at _matches_bak — that rewrite was the cause of the "no such table: _matches_bak" crash. Concurrency-safe: try/except so two overlapping init_db callers can never crash on a half-migrated _matches_bak. Idempotent.
inputs: conn: sqlite3.Connection
outputs: none
calls: sqlite3.Connection.execute, conn.commit, conn.rollback
called_by: init_db
mutates: predicta.db schema (matches table DDL) — only when upgrading an old-schema DB
---

---
name: _repair_matches_fk
type: function
file: db/database.py
purpose: Heals a DB corrupted by the pre-fix sport-check migration. That migration renamed matches→_matches_bak without legacy_alter_table, so SQLite rewrote every child table's foreign key (predictions/signals/outcomes/odds_snapshots) to reference _matches_bak; after _matches_bak was dropped, any INSERT into those children failed with "no such table: main._matches_bak". This drops/renames any stray _matches_bak table, then uses PRAGMA writable_schema to rewrite the stored DDL text of all remaining objects, changing dangling _matches_bak references back to matches. Idempotent — no-op once schema is clean.
inputs: conn: sqlite3.Connection
outputs: none
calls: sqlite3.Connection.execute, conn.commit
called_by: init_db
mutates: predicta.db schema (child-table FK DDL via sqlite_master) — only when corruption is detected
---


---

## fetchers/fbref.py

---
name: _HAS_CLOUDSCRAPER
type: variable
file: fetchers/fbref.py
purpose: True when the optional `cloudscraper` package is installed. FBref sits behind Cloudflare's JS challenge; cloudscraper mimics a browser well enough to solve it on ~70% of requests. When absent, _get falls back to plain httpx (usually returns HTTP 403 "Just a moment..." and the pipeline degrades to Understat-only enrichment).
inputs: none
outputs: bool
calls: none
called_by: _make_scraper, _get
mutates: none
---

---
name: _SCRAPER
type: variable
file: fetchers/fbref.py
purpose: Cached module-level cloudscraper session, created lazily by _make_scraper. Reusing one session lets the Cloudflare challenge cookie persist across requests within a process.
inputs: none
outputs: Optional[cloudscraper.CloudScraper]
calls: none
called_by: _make_scraper
mutates: assigned by _make_scraper
---

---
name: _make_scraper
type: function
file: fetchers/fbref.py
purpose: Lazily create a cloudscraper session with Chrome/Windows fingerprint. Returns None if cloudscraper isn't installed or session creation fails. Called by _get before every request.
inputs: none
outputs: Optional[cloudscraper.CloudScraper]
calls: cloudscraper.create_scraper
called_by: _get
mutates: _SCRAPER
---

---
name: FBREF_BASE
type: variable
file: fetchers/fbref.py
purpose: Base URL for FBref scraping (https://fbref.com). Sports Reference site publishing free squad-level tables with PSxG, possession, defense, and aerial duel data — process metrics Understat doesn't expose.
inputs: none
outputs: str
calls: none
called_by: _fetch_table
mutates: none
---

---
name: _LEAGUE_TO_FBREF
type: variable
file: fetchers/fbref.py
purpose: Map Understat league slug → (FBref competition id, slug-name). Covers EPL (9), La_liga (12), Bundesliga (20), Serie_A (11), Ligue_1 (13). RFPL is excluded — FBref does not publish detailed Russian Premier League advanced tables.
inputs: none
outputs: dict[str, tuple[int, str]]
calls: none
called_by: _fetch_table, enrich_soccer_advanced
mutates: none
---

---
name: _TEAM_ALIASES
type: variable
file: fetchers/fbref.py
purpose: Common-name → FBref canonical name mapping for fuzzy team resolution. Differs from Understat's mapping (FBref uses "Manchester Utd" not "Manchester United", "Nott'ham Forest" with apostrophe, etc.).
inputs: none
outputs: dict[str, str]
calls: none
called_by: _resolve_team
mutates: none
---

---
name: _FBREF_CACHE
type: variable
file: fetchers/fbref.py
purpose: Module-level cache keyed by (league, season, table_kind) → pandas DataFrame. Avoids re-hitting FBref for the same table within a single analysis session. Cleared on process restart.
inputs: none
outputs: dict[tuple, pd.DataFrame]
calls: none
called_by: _fetch_table
mutates: filled by _fetch_table
---

---
name: _strip_comments
type: function
file: fetchers/fbref.py
purpose: Strip HTML comment markers from FBref pages. FBref wraps several tables (advanced keepers, defense, possession) inside <!-- ... --> to slow simple scrapers; pandas.read_html needs them visible.
inputs: html: str
outputs: str
calls: re.sub
called_by: _fetch_table
mutates: none
---

---
name: _get (fbref.py)
type: function
file: fetchers/fbref.py
purpose: Fetch FBref HTML. Prefers cloudscraper session (bypasses Cloudflare's JS challenge on ~70% of requests) then falls back to plain httpx. Detects Cloudflare "Just a moment..." interstitial in first 400 chars and treats it as failure. Returns None on any failure — every fetch_team_* function already handles empty results silently.
inputs: url: str
outputs: Optional[str]
calls: _make_scraper, httpx.Client
called_by: _fetch_table
mutates: none
---

---
name: _fetch_table
type: function
file: fetchers/fbref.py
purpose: Generic FBref squad-table fetcher. Builds URL from (league, season, kind) where kind is one of 'standard' / 'keepers_adv' / 'defense' / 'misc' / 'possession'. Strips comments, calls pandas.read_html, flattens multi-index columns, caches the result. Returns None on any failure.
inputs: league: str, season: int, kind: str
outputs: Optional[pd.DataFrame]
calls: _LEAGUE_TO_FBREF, _get, _strip_comments, pd.read_html, _FBREF_CACHE
called_by: fetch_team_gk, fetch_team_pressing, fetch_team_possession, fetch_team_aerials
mutates: _FBREF_CACHE
---

---
name: _resolve_team
type: function
file: fetchers/fbref.py
purpose: Resolve a user-supplied team name to the canonical FBref Squad value, via _TEAM_ALIASES then fuzzy matching against the table's squad list.
inputs: team_name: str, table: pd.DataFrame
outputs: Optional[str]
calls: _TEAM_ALIASES, _fuzzy_match
called_by: fetch_team_gk, fetch_team_pressing, fetch_team_possession, fetch_team_aerials
mutates: none
---

---
name: fetch_team_gk
type: function
file: fetchers/fbref.py
purpose: Goalkeeper quality from FBref keepersadv squad table. Returns psxg, psxg_minus_ga (sign-aware: positive = saves above expected = better keeper), psxg_ga_per_90, ga, saves_pct. PSxG-GA per 90 is the best publicly available GK metric — Pinnacle uses an internal version.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _fetch_table, _resolve_team, _safe_float
called_by: enrich_soccer_advanced
mutates: none
---

---
name: fetch_team_pressing
type: function
file: fetchers/fbref.py
purpose: Pressing intensity proxy from FBref defense table. PPDA is the standard pressing metric but FBref doesn't publish it directly — we approximate as 370 / (tkl+int per 90 × 0.55), where 370 ≈ league opponent passes/game and 0.55 ≈ share of defensive actions occurring in opponent half. Lower = more pressing (top pressers ~7-9, low blocks ~15-18). Also returns tkl_int_per_90 and challenge_pct.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _fetch_table, _resolve_team, _safe_float
called_by: enrich_soccer_advanced
mutates: none
---

---
name: fetch_team_possession
type: function
file: fetchers/fbref.py
purpose: Field tilt + possession metrics from FBref possession table. Returns att_3rd_touch_pct (top teams 32-38%, bottom 22-27%), def_3rd_touch_pct, possession_pct, progressive_passes per 90. Field tilt approximation is touches_att_3rd / total_touches.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _fetch_table, _resolve_team, _safe_float
called_by: enrich_soccer_advanced
mutates: none
---

---
name: fetch_team_aerials
type: function
file: fetchers/fbref.py
purpose: Aerial duel win % + disciplinary stats from FBref misc table. Feeds the set-piece model — aerial dominance amplifies set-piece μ via _aerial_index in analyze_soccer.py. Returns aerials_won_pct (league avg ~50%), fouls_per_90, yellow_per_90.
inputs: team_name: str, league: str, season: int
outputs: dict
calls: _fetch_table, _resolve_team, _safe_float
called_by: enrich_soccer_advanced
mutates: none
---

---
name: enrich_soccer_advanced
type: function
file: fetchers/fbref.py
purpose: Combined FBref enrichment — calls fetch_team_gk, _pressing, _possession, _aerials for both teams and merges into {"home": dict, "away": dict}. Designed to layer on top of enrich_soccer_teams() (Understat) in analyze_soccer.py: Understat provides xG and shot-level data, FBref provides process metrics (PSxG, PPDA, field tilt, aerial). Returns empty dicts when league is unsupported or all fetches fail.
inputs: home_name: str, away_name: str, league: str, season: int
outputs: dict {"home": dict, "away": dict}
calls: fetch_team_gk, fetch_team_pressing, fetch_team_possession, fetch_team_aerials, _LEAGUE_TO_FBREF
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---

## ai_agent_soccer.py

---
name: MODEL
type: variable
file: ai_agent_soccer.py
purpose: Claude model identifier used for soccer query parsing and narrative generation (claude-haiku-4-5-20251001). Haiku is sufficient for structured extraction and short narratives; saves tokens vs Sonnet for high-throughput scenarios.
inputs: none
outputs: str
calls: none
called_by: parse_soccer_query, interpret_soccer_signals_fallback, generate_soccer_narrative
mutates: none
---

---
name: _client
type: function
file: ai_agent_soccer.py
purpose: Anthropic client factory wrapping ai_client.get_client. Shared across parse / fallback / narrative calls.
inputs: none
outputs: anthropic.Anthropic
calls: get_client
called_by: parse_soccer_query, interpret_soccer_signals_fallback, generate_soccer_narrative
mutates: none
---

---
name: PARSE_SYSTEM
type: variable
file: ai_agent_soccer.py
purpose: System prompt for parse_soccer_query. Extracts home_team, away_team, league slug (EPL/La_liga/Bundesliga/Serie_A/Ligue_1/RFPL), season year, date, neutral flag, decimal+American odds for home/draw/away, and notes. Strict JSON-only output, no markdown.
inputs: none
outputs: str
calls: none
called_by: parse_soccer_query
mutates: none
---

---
name: parse_soccer_query
type: function
file: ai_agent_soccer.py
purpose: Parse a free-form soccer query into structured JSON via Claude Haiku. Returns home_team, away_team, league, season, date, neutral, odds_home/draw/away (decimal + American), notes. Used as step 1 of run_soccer_analysis.
inputs: user_text: str
outputs: dict
calls: _client, PARSE_SYSTEM, anthropic.messages.create, json.loads
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---
name: FALLBACK_SYSTEM
type: variable
file: ai_agent_soccer.py
purpose: System prompt for interpret_soccer_signals_fallback. Used only when Understat enrichment fails entirely. Asks Claude to estimate season + recent xG, goal_overperform, and Elo ratings from training knowledge with confidence='low'.
inputs: none
outputs: str
calls: none
called_by: interpret_soccer_signals_fallback
mutates: none
---

---
name: interpret_soccer_signals_fallback
type: function
file: ai_agent_soccer.py
purpose: Last-resort signal estimation when Understat and FBref are both unreachable. Returns {"home": {season_xg_for, season_xg_against, recent_xg_for, recent_xg_against, goal_overperform, elo_rating}, "away": {same}, "league_avg_goals", "confidence": "low"}.
inputs: home_name: str, away_name: str, notes: str = ""
outputs: dict
calls: _client, FALLBACK_SYSTEM, anthropic.messages.create, json.loads
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---
name: NARRATIVE_SYSTEM
type: variable
file: ai_agent_soccer.py
purpose: System prompt for generate_soccer_narrative. Asks Claude to produce a 2-3 sentence prediction summary naming the favourite, the decisive driver (xG edge / GK / form / set-piece), the verdict if supplied, and a confidence caveat. Under 90 words, no emojis.
inputs: none
outputs: str
calls: none
called_by: generate_soccer_narrative
mutates: none
---

---
name: generate_soccer_narrative
type: function
file: ai_agent_soccer.py
purpose: Generate the final narrative string shown to the user via Claude Haiku. Takes model probabilities, the engine explanation, the bet verdict, and confidence. Returns plain text under 90 words.
inputs: home_team: str, away_team: str, prob_home: float, prob_draw: float, prob_away: float, explanation: str, verdict: str = "", confidence: str = "medium"
outputs: str
calls: _client, NARRATIVE_SYSTEM, anthropic.messages.create
called_by: run_soccer_analysis (analyze_soccer.py)
mutates: none
---

---

## analyze_soccer.py

---
name: _safe_float
type: function
file: analyze_soccer.py
purpose: Cast a value to float, returning default on None / TypeError / ValueError. Used when logging signals so noisy Understat/FBref values don't break the SQL insert.
inputs: val, default=None
outputs: Optional[float]
calls: none
called_by: run_soccer_analysis
mutates: none
---

---
name: _current_season
type: function
file: analyze_soccer.py
purpose: Determine the active Understat season number (start year). June-July rolls forward: July 1 onward returns current year, before returns previous year.
inputs: none
outputs: int
calls: datetime.now
called_by: run_soccer_analysis
mutates: none
---

---
name: _aerial_index
type: function
file: analyze_soccer.py
purpose: Convert FBref aerials_won_pct to a directional aerial-strength index. 50% maps to 1.0. Returned in [0.90, 1.10] — feed into _set_piece_aerial_mult which halves the swing further. Kimi #5: previously returned [0.85, 1.20] and the consumer used `2.0 - idx_opp`, compounding into ±22% set-piece μ at extremes; new pair caps compounding at ±5%.
inputs: aerials_won_pct: Optional[float]
outputs: float
calls: none
called_by: _set_piece_aerial_mult, run_soccer_analysis
mutates: none
---

---
name: _set_piece_aerial_mult
type: function
file: analyze_soccer.py
purpose: Convert the opponent's aerial index into a multiplier on our set-piece μ (Kimi #5). Formula: mult = 1 + (1 - opp_aerial_idx) × 0.5, clamped [0.90, 1.15]. Weak opponent in the air (idx < 1.0) amplifies our set-piece μ; strong opponent suppresses it. Max swing ±5% — set-piece goals matter but are noisy, so we don't let this single signal dominate. Replaces the older `2.0 - opp_aerial_idx` formulation.
inputs: opp_aerial_idx: float
outputs: float
calls: none
called_by: run_soccer_analysis
mutates: none
---

---
name: _set_piece_share
type: function
file: analyze_soccer.py
purpose: Determine the fraction of expected goals coming from set pieces (corners + indirect freekicks + set play; penalties excluded). Uses team-level Understat situation split if available, otherwise league default from models.soccer_leagues. Clamped to [0.05, 0.45].
inputs: team_sit: dict, league: Optional[str]
outputs: float
calls: leagues.open_play_share
called_by: run_soccer_analysis
mutates: none
---

---
name: PROMOTED_TEAM_ATTACK_PRIOR
type: variable
file: analyze_soccer.py
purpose: Default attack multiplier (0.90) used when a team has no live xG data at all (Round 4 #3). Teams with zero signal in top-flight context are typically promoted sides and below average — using 1.0/1.0 (league average) was systematically overrating unknown opponents.
inputs: none
outputs: float
calls: none
called_by: _build_strengths
mutates: none
---

---
name: PROMOTED_TEAM_DEFENSE_PRIOR
type: variable
file: analyze_soccer.py
purpose: Default defense multiplier (1.10) paired with PROMOTED_TEAM_ATTACK_PRIOR. Higher number = weaker defense in DC convention.
inputs: none
outputs: float
calls: none
called_by: _build_strengths
mutates: none
---

---
name: _build_strengths
type: function
file: analyze_soccer.py
purpose: Convert an Understat enriched team dict into Dixon-Coles attack/defense multipliers via strengths_from_xg. Picks npxG (penalty-stripped) when available, falls back to xG. Selects venue-specific xG when is_home indicates, applies recent/season blend + shrinkage. Round 4 #3 — if no xG signal exists in any column (data_completeness="minimal"), bypasses strengths_from_xg and returns PROMOTED_TEAM_ATTACK_PRIOR / PROMOTED_TEAM_DEFENSE_PRIOR (0.90/1.10) directly, replacing the previous 1.0/1.0 league-average default.
inputs: team_data: dict, opp_data: dict, league_avg_goals: float, is_home: bool
outputs: dict {attack: float, defense: float, components: dict}
calls: dc.strengths_from_xg, PROMOTED_TEAM_ATTACK_PRIOR, PROMOTED_TEAM_DEFENSE_PRIOR
called_by: run_soccer_analysis
mutates: none
---

---
name: _build_explanation
type: function
file: analyze_soccer.py
purpose: Build the multi-line plain-English explanation string for the prediction. Surfaces split Poisson μ_open + μ_set decomposition, attack/defense strengths, GK PSxG-GA per 90 when available, PPDA pressing proxy, and Elo win-prob blend.
inputs: home_team, away_team, dc: dict, str_h: dict, str_a: dict, league_avg: float, keeper_h, keeper_a, ppda_h, ppda_a, elo_h: float, elo_a: float
outputs: str
calls: none
called_by: run_soccer_analysis
mutates: none
---

---
name: _bet_recommendations
type: function
file: analyze_soccer.py
purpose: Translate model probabilities (+ optional market edge) into one structured bet recommendation. Round 4 #2 — partial-data threshold lift: when partial_data=True, edges-with-odds thresholds become BET ≥ +5pp / LEAN ≥ +1.5pp (from 3.0/0.5); no-odds DNB thresholds become BET ≥ 65% / LEAN ≥ 58% (from 62/55). Higher bars when either team has incomplete data because parameter uncertainty raises edge-estimate variance. Returns one dict containing market label, verdict (BET/LEAN/PASS), bet string, threshold info, partial_data flag, confidence, reasons, skip_reason.
inputs: prob_home: float, prob_draw: float, prob_away: float, home_team: str, away_team: str, edge_summary: Optional[dict], partial_data: bool = False
outputs: list[dict]
calls: none
called_by: run_soccer_analysis
mutates: none
---

---
name: _format_markets
type: function
file: analyze_soccer.py
purpose: Reshape compute_all_markets output for the frontend — adds team-name labels and rounds probabilities. Covers match_result_2up, correct_score (top 6), spread (Asian handicap), winner_push_if_tied (Draw No Bet).
inputs: raw: dict, home: str, away: str
outputs: dict
calls: none
called_by: run_soccer_analysis
mutates: none
---

---
name: _build_plain_summary
type: function
file: analyze_soccer.py
purpose: Plain-English bet recommendation in the user's preferred phrasing — "Team A is gonna win vs Team B (XX% accuracy). Bet on: goals (BTTS), 1st half X to score, 2nd half Y to score." Derives BTTS prob via Poisson independence on full-match μ_home/μ_away; derives 1H/2H scoring probs by splitting μ 45/55 across halves. Markets are gated (BTTS ≥55%, 1H scoring ≥55%, 2H scoring ≥60%) so only model-favoured legs appear. Headline switches between "gonna win" (≥60%), "slight favourite" (45-60%), and "too close to call" (<45%, draw-led).
inputs: home_team: str, away_team: str, prob_home: float, prob_draw: float, prob_away: float, mu_home: float, mu_away: float
outputs: str
calls: math.exp
called_by: run_soccer_analysis
mutates: none
---

---
name: run_soccer_analysis
type: function
file: analyze_soccer.py
purpose: Full soccer pipeline entry point. (1) Parse query via Claude. (2) Enrich both teams via Understat (xG/npxG + time-decayed recent + venue + situation), season-1 fallback. (3) Enrich via FBref (PSxG, PPDA, possession, aerials). (4) If BOTH teams have no Understat data → return {"status": "insufficient_data", "recommendation": "PASS"} immediately rather than hallucinate signals from Claude training data (Kimi #1 — critical for betting safety). (5) Anchor on league xG (Kimi #6) — falls back to live goals avg → static xG constant. (6) Build strengths via strengths_from_xg with time-decayed recent, venue blend, shrinkage, continuous overperform damping. (7) Run predict_xg with open-play+set-piece decomposition, tiered GK adjustment, and tightened aerial multiplier. (8) Blend with Elo 65/35 (DC keeps full draw probability). (9) Markets + bet recs labeled as "Win (Draw No Bet)" with explicit conditional-on-decisive note. (10) Build plain_summary in user-preferred phrasing. (11) Persist + narrate. Returns full dict or insufficient_data response.
inputs: user_query: str, bankroll: float = 1000.0
outputs: dict (success: full prediction dict including `plain_summary`; failure mode: {"status": "insufficient_data", "recommendation": "PASS", ...})
calls: init_db, parse_soccer_query, enrich_soccer_teams, enrich_soccer_advanced, _build_strengths, _set_piece_share, _aerial_index, _set_piece_aerial_mult, dc.predict_xg, EloModel.win_probability, compute_all_markets, market_edge_summary, _bet_recommendations, kelly_stake, log_signal, get_db, _build_explanation, _build_plain_summary, generate_soccer_narrative, _format_markets, leagues.league_avg_xg, leagues.league_avg_goals, leagues.home_advantage, american_to_decimal
called_by: analyze_soccer_endpoint (app.py)
mutates: predicta.db (matches, signals, predictions)
---

---

## app.py (soccer endpoint)

---
name: SoccerRequest
type: class
file: app.py
purpose: Pydantic body model for POST /analyze-soccer. Holds the free-form query string and an optional bankroll for Kelly staking. Odds and league info are parsed out of the query text by ai_agent_soccer.parse_soccer_query rather than passed as fields.
inputs: query: str, bankroll: float = 1000.0
outputs: SoccerRequest instance
calls: none
called_by: analyze_soccer_endpoint
mutates: none
---

---
name: analyze_soccer_endpoint
type: function
file: app.py
purpose: POST /analyze-soccer FastAPI route. Validates the query, runs run_soccer_analysis, and returns the full result dict. The legacy POST /analyze stays in place and routes to the older analyze.py pipeline for backward compatibility with the existing UI.
inputs: body: SoccerRequest
outputs: dict
calls: run_soccer_analysis
called_by: FastAPI (HTTP request)
mutates: predicta.db indirectly via run_soccer_analysis
---

---

## fetchers/soccer_schedule.py

---
name: _LEAGUE_TO_ESPN
type: variable
file: fetchers/soccer_schedule.py
purpose: Map Understat league slug → ESPN scoreboard slug. Covers the 5 Understat-supported leagues: EPL=eng.1, La_liga=esp.1, Bundesliga=ger.1, Serie_A=ita.1, Ligue_1=fra.1.
inputs: none
outputs: dict[str, str]
calls: none
called_by: upcoming_fixtures, finished_result
mutates: none
---

---
name: upcoming_fixtures
type: function
file: fetchers/soccer_schedule.py
purpose: Fetch scheduled matches across the 5 big soccer leagues within the next `days_ahead` days from ESPN's public scoreboard API. Each entry: {league, home, away, kickoff_utc, espn_id}. Used by tasks/soccer_auto.scan_fixtures to feed the auto-collection loop.
inputs: days_ahead: int = 3
outputs: list[dict]
calls: _get (ESPN scoreboard), _LEAGUE_TO_ESPN
called_by: scan_fixtures (tasks/soccer_auto.py)
mutates: none
---

---
name: finished_result
type: function
file: fetchers/soccer_schedule.py
purpose: Look up a completed match's final score by team names + earliest date. Scans ESPN scoreboards ±3 days around the given date across all 5 leagues, matches teams with fuzzy substring + last-word logic. Returns {result: 'a'|'b'|'draw', score_a, score_b, kickoff_utc, matched_home, matched_away} or None if the match hasn't finished yet.
inputs: home: str, away: str, on_or_after: str (YYYY-MM-DD)
outputs: Optional[dict]
calls: _get, _last_word_match, _LEAGUE_TO_ESPN
called_by: resolve_finished (tasks/soccer_auto.py)
mutates: none
---

---

## tasks/nrfi_auto.py

---
name: nrfi_auto
type: module
file: tasks/nrfi_auto.py
purpose: Always-on NRFI daily pipeline for Railway — replaces the unreliable GitHub Actions schedule cron (which delayed/skipped runs). An in-process daemon thread (start_nrfi_auto, called from app.py startup, disable via NRFI_AUTO_DISABLED=1) checks every 5 min and fires each job once per UTC day after its hour: run_predict (13:00 UTC / 9 AM ET — fetch schedule, run_predictions, capture entry odds, write JSON+report), run_capture (23:00 UTC / 7 PM ET — closing odds), run_resolve (05:00 UTC / 1 AM ET — resolve prior day + CLV; also calls scripts/daily_nrfi.resolve_pending_bets as of 2026-07-05, which grades ad-hoc manual-query rows in nrfi_bets that the JSON-file pipeline never sees). Reuses scripts/daily_nrfi functions. Persists results by pushing JSON+report to GitHub via the Contents API (_push_file/_push_day; needs GITHUB_TOKEN + GITHUB_REPO env vars — same as soccer_auto). Also overwrites a fixed-path data/nrfi_latest.md every run (header + the day's report) so the newest scan is always one known file — read that to check the most recent results without hunting for the date. status() reports running state + last-run/error/push + whether push is configured. Exposed via app.py GET /nrfi-auto/status and GET|POST /nrfi-auto/run?job=predict|capture|resolve (manual on-demand trigger; GET added for browser-friendly access).
inputs: env GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH, NRFI_AUTO_DISABLED
outputs: commits to data/nrfi_predictions/ + data/nrfi_reports/ on main; nrfi_bets DB rows (via run_baseball_analysis)
calls: scripts.daily_nrfi (fetch_schedule, run_predictions, capture_odds, resolve_predictions, write_report), GitHub Contents API (httpx)
called_by: app.startup (start_nrfi_auto); app.py /nrfi-auto/* endpoints
mutates: data/nrfi_predictions/, data/nrfi_reports/ (via GitHub API), nrfi_bets
---

---
name: nrfi_anthropic_diag
type: route
file: app.py
purpose: GET /nrfi-auto/anthropic-diag — added 2026-07-05 after an entire day's predict run silently produced 11 blank records (see run_predictions error-check fix). Actually calls parse_baseball_query with a trivial query and reports success/failure with the real exception instead of guessing whether the cause was a missing/expired ANTHROPIC_API_KEY, a rate limit, or something else. Mirrors the existing /nrfi-auto/odds-diag pattern.
inputs: none (reads ANTHROPIC_API_KEY env, calls Anthropic API live)
outputs: JSON — key_present, status (ok/failed), parsed (on success) or exception + note (on failure, with a best-guess reason: missing key / rate-limit / invalid key / unrecognized)
calls: ai_agent_baseball.parse_baseball_query
called_by: GET /nrfi-auto/anthropic-diag
mutates: none
---

## tasks/soccer_auto.py

---
name: REPORTS_DIR
type: variable
file: tasks/soccer_auto.py
purpose: Path to reports/ folder at the repo root. Weekly reports are written here as soccer_YYYY_WW.md. Committed to git (with .gitkeep) so Railway deploys include the directory.
inputs: none
outputs: pathlib.Path
calls: none
called_by: weekly_report, _push_to_github
mutates: created at import time
---

---
name: _STATE
type: variable
file: tasks/soccer_auto.py
purpose: Module-level dict holding last-run timestamps and schedule config (scan_interval_h=4, resolve_interval_h=2, report on Monday at 08:00 UTC). Surfaced via /soccer-auto/status endpoint.
inputs: none
outputs: dict
calls: none
called_by: _should_run_interval, _should_run_weekly, status
mutates: updated by every job run
---

---
name: scan_fixtures
type: function
file: tasks/soccer_auto.py
purpose: Job 1 — fetch upcoming fixtures via fetchers.soccer_schedule.upcoming_fixtures, call run_soccer_analysis for each new one (skipping already-predicted matches via _already_predicted). Returns {fixtures_seen, predicted, skipped, failed, details}.
inputs: days_ahead: int = 3
outputs: dict
calls: upcoming_fixtures, run_soccer_analysis (analyze_soccer.py), _already_predicted
called_by: _scheduler_loop, /soccer-auto/scan endpoint
mutates: predictions and signals tables (via run_soccer_analysis); _STATE
---

---
name: _already_predicted
type: function
file: tasks/soccer_auto.py
purpose: Idempotency check — True if a soccer match row exists for these two teams within ±1 day of the given kickoff. Handles both team orderings. Called before every scan_fixtures prediction to avoid duplicates.
inputs: home: str, away: str, kickoff_utc: str
outputs: bool
calls: get_db
called_by: scan_fixtures
mutates: none
---

---
name: resolve_finished
type: function
file: tasks/soccer_auto.py
purpose: Job 2 — find soccer predictions whose scheduled_at is in the past and have no outcome. Look them up on ESPN via fetchers.soccer_schedule.finished_result and record via engine.record_outcome (which also updates Elo ratings). Returns {candidates, resolved, still_pending, errors, details}.
inputs: none
outputs: dict
calls: get_db, finished_result, record_outcome (engine.py)
called_by: _scheduler_loop, /soccer-auto/resolve endpoint
mutates: outcomes table + matches.status; _STATE
---

---
name: weekly_report
type: function
file: tasks/soccer_auto.py
purpose: Job 3 — compute soccer model metrics (Brier score, favorite hit rate, per-league breakdown), render as markdown, write to reports/soccer_YYYY_WW.md, and push to GitHub via the Contents API when GITHUB_TOKEN + GITHUB_REPO env vars are set. Returns {filename, local_path, metrics, pushed_to_github, push_error}.
inputs: none
outputs: dict
calls: _compute_metrics, _render_report, _push_to_github
called_by: _scheduler_loop, /soccer-auto/report endpoint
mutates: reports/ directory, optionally the GitHub repo, _STATE
---

---
name: _compute_metrics
type: function
file: tasks/soccer_auto.py
purpose: Query all resolved soccer predictions and compute average Brier score (across 3-way probabilities), favorite hit rate (higher-prob side wins), and per-league breakdown. Returns {resolved, avg_brier, favorite_hit_rate, by_league, generated_utc}.
inputs: none
outputs: dict
calls: get_db
called_by: weekly_report
mutates: none
---

---
name: _render_report
type: function
file: tasks/soccer_auto.py
purpose: Convert the metrics dict into a markdown report with global metrics table, per-league breakdown table, and next-steps section. Interpretation thresholds: Brier <0.20 + hit rate >55% = beating market; Brier >0.25 = worse than random.
inputs: metrics: dict
outputs: str (markdown)
calls: none
called_by: weekly_report
mutates: none
---

---
name: _push_to_github
type: function
file: tasks/soccer_auto.py
purpose: Create or update a file in the GitHub repo via the Contents API. Requires GITHUB_TOKEN (PAT with contents:write) and GITHUB_REPO ("owner/repo") env vars. Optional GITHUB_BRANCH (default "main"). Fetches existing file's sha for updates; returns True on 200/201.
inputs: path_in_repo: str, content: str
outputs: bool
calls: httpx.Client
called_by: weekly_report
mutates: pushes to the configured GitHub repo
---

---
name: _scheduler_loop
type: function
file: tasks/soccer_auto.py
purpose: Background thread body. Every 5 minutes checks _should_run_interval / _should_run_weekly for each of the 3 jobs and fires them. Exceptions are printed but don't stop the loop.
inputs: none
outputs: none
calls: _should_run_interval, _should_run_weekly, scan_fixtures, resolve_finished, weekly_report
called_by: start_soccer_auto (via threading.Thread)
mutates: _STATE (indirectly through job calls)
---

---
name: start_soccer_auto
type: function
file: tasks/soccer_auto.py
purpose: Start the background scheduler thread. Idempotent — safe to call multiple times, only starts if not already running. Called from app.py startup unless SOCCER_AUTO_DISABLED env var is set.
inputs: none
outputs: none
calls: threading.Thread
called_by: startup (app.py), /soccer-auto/status endpoint
mutates: _THREAD, _STATE
---

---
name: stop_soccer_auto
type: function
file: tasks/soccer_auto.py
purpose: Signal the scheduler thread to stop. Sets _STOP event; thread exits after its current 5-min wait completes.
inputs: none
outputs: none
calls: _STOP.set
called_by: (utility)
mutates: _STOP
---

---
name: status (tasks/soccer_auto.py)
type: function
file: tasks/soccer_auto.py
purpose: Return current scheduler state — {running, last_scan_utc, last_resolve_utc, last_report_utc, intervals, started_at}. Surfaced via GET /soccer-auto/status.
inputs: none
outputs: dict
calls: _STATE
called_by: /soccer-auto/status endpoint
mutates: none
---

---

## app.py (soccer auto-collection admin)

---
name: soccer_auto_status
type: function
file: app.py
purpose: GET /soccer-auto/status — surface the background scheduler state.
inputs: none
outputs: dict
calls: tasks.soccer_auto.status
called_by: FastAPI (HTTP GET)
mutates: none
---

---
name: soccer_auto_scan
type: function
file: app.py
purpose: POST /soccer-auto/scan — manually trigger fixture scan + predict for the next N days. Query param days_ahead defaults to 3.
inputs: days_ahead: int = 3
outputs: dict
calls: tasks.soccer_auto.scan_fixtures
called_by: FastAPI (HTTP POST)
mutates: predictions, signals tables
---

---
name: soccer_auto_resolve
type: function
file: app.py
purpose: POST /soccer-auto/resolve — manually trigger resolution of past predictions. Fetches ESPN results and calls record_outcome.
inputs: none
outputs: dict
calls: tasks.soccer_auto.resolve_finished
called_by: FastAPI (HTTP POST)
mutates: outcomes, matches.status
---

---
name: soccer_auto_report
type: function
file: app.py
purpose: POST /soccer-auto/report — manually generate weekly report. Writes local file + pushes to GitHub if credentials are set.
inputs: none
outputs: dict
calls: tasks.soccer_auto.weekly_report
called_by: FastAPI (HTTP POST)
mutates: reports/, optionally GitHub repo
---

---
name: soccer_auto_report_latest
type: function
file: app.py
purpose: GET /soccer-auto/report/latest — return the most recent markdown report as JSON. Used to check what got generated without SSH-ing into the container.
inputs: none
outputs: dict {filename, content}
calls: REPORTS_DIR.glob, Path.read_text
called_by: FastAPI (HTTP GET)
mutates: none
---
