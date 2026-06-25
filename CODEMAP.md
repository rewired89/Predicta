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
purpose: Absolute path to the SQLite database file (predicta.db at project root).
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
purpose: Create all database tables by executing schema.sql; safe to call repeatedly (CREATE IF NOT EXISTS).
inputs: db_path: Path = DB_PATH
outputs: none
calls: sqlite3.connect, SCHEMA_PATH.read_text
called_by: startup (app.py), run_analysis (analyze.py)
mutates: predicta.db (creates tables)
---

---
name: get_db
type: function
file: db/database.py
purpose: Context manager that yields an open SQLite connection with Row factory and foreign keys on; commits on exit or rolls back on exception.
inputs: db_path: Path = DB_PATH
outputs: yields sqlite3.Connection
calls: sqlite3.connect
called_by: EloModel, Glicko2Model, predict_match, record_outcome, _market_consensus, log_signal, get_signals_for_match, fetch_signals_for_match, log_manual_odds, fetch_odds_snapshot, compute_metrics_from_db, generate_html_report, create_match, list_matches, get_match, add_signal, get_signals, list_predictions, get_odds, add_outcome, list_orders (app.py), _gather_training_data (ml_layer.py)
mutates: predicta.db
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
purpose: Computes home/draw/away win probabilities and expected goals using a Dixon-Coles Poisson model from attack/defense strength values.
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
purpose: Converts xG signal values from the signals dict into attack/defense multipliers relative to league average (1.0 = average).
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
called_by: predict_match
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
purpose: Full accuracy + calibration report: prediction accuracy %, bet accuracy %, ROI, Brier score, log-loss, reliability curve, benchmark comparison. Broken down by sport, confidence level, and prediction method.
inputs: method: Optional[str], sport: Optional[str]
outputs: dict {overall, by_sport, by_confidence, by_method, brier_score, log_loss, reliability_curve, benchmarks}
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
purpose: Minimum number of resolved predictions (50) before the ML model will train; below this the Elo/Glicko baseline is more reliable.
inputs: none
outputs: int
calls: none
called_by: train
mutates: none
---

---
name: FEATURE_SIGNALS
type: variable
file: models/ml_layer.py
purpose: List of signal names used as features for the ML calibration model.
inputs: none
outputs: list[str]
calls: none
called_by: _gather_training_data, predict
mutates: none
---

---
name: _gather_training_data
type: function
file: models/ml_layer.py
purpose: Queries DB for predictions + outcomes + signals and assembles feature matrix X and label vector y for training.
inputs: none
outputs: tuple (X: list[list], y: list[int])
calls: get_db
called_by: train
mutates: none
---

---
name: train
type: function
file: models/ml_layer.py
purpose: Trains a calibrated logistic regression or GBM model on historical prediction data and saves it to disk with joblib.
inputs: model_path: str = MODEL_PATH_DEFAULT, use_gbm: bool = False
outputs: dict {status, samples, model_path} or {error}
calls: _gather_training_data, LogisticRegression, GradientBoostingClassifier, CalibratedClassifierCV, joblib.dump
called_by: none (invoked manually / via CLI)
mutates: ml_model.json (creates/overwrites)
---

---
name: predict
type: function
file: models/ml_layer.py
purpose: Loads a saved ML model and returns predicted win probability for a feature dict; returns None on any failure.
inputs: features: dict, model_path: str = MODEL_PATH_DEFAULT
outputs: Optional[float]
calls: joblib.load, np.array
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
name: _momentum
type: function
file: models/trading/signals.py
purpose: Returns RSI(14), RSI signal label, and 5-day/20-day rate of change.
inputs: closes: np.ndarray
outputs: dict {rsi, rsi_signal, roc_5d, roc_20d}
calls: _rsi
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
purpose: Calculates ±1σ and ±2σ expected price range for the next trading session based on 20-day historical volatility.
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
purpose: Combines trend (40%), RSI momentum (30%), 20-day ROC (20%), and Bollinger %B (10%) into a composite score from -100 to +100.
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
purpose: Computes quarter-Kelly position size for a trade using win rate and average win/loss percentages.
inputs: win_rate: float, avg_win_pct: float, avg_loss_pct: float, bankroll: float = 10000.0, fraction: float = 0.25
outputs: dict {full_kelly, kelly_fraction, position_size, bankroll, edge_pct, win_rate, avg_win_pct, avg_loss_pct, win_loss_ratio, note, paper_mode}
calls: none
called_by: kelly_from_signals
mutates: none
---

---
name: kelly_from_signals
type: function
file: models/trading/kelly.py
purpose: Estimates Kelly position size directly from composite signal score (–100 to +100) and ATR volatility, without needing historical trade stats.
inputs: score: float, atr_pct: float, bankroll: float = 10000.0
outputs: dict (output of trading_kelly)
calls: trading_kelly
called_by: run_trade_analysis, intraday_analysis (app.py)
mutates: none
---

---

## models/trading/intraday.py

---
name: WEIGHTS
type: variable
file: models/trading/intraday.py
purpose: Signal weight dictionary mapping each intraday signal name to its ensemble contribution (sums to ~1.0).
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
purpose: Computes VWAP deviation signal — price above/below session VWAP and extent of overextension.
inputs: bars: list[dict], snapshot: dict
outputs: dict {vwap, deviation_pct, label, score}
calls: _vwap
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
name: _sig_relative_volume
type: function
file: models/trading/intraday.py
purpose: Compares today's session volume to historical daily average to score volume confirmation of signals.
inputs: bars: list[dict], daily_avg_volume: float
outputs: dict {today_volume, avg_volume, rel_vol, label, score}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_gap
type: function
file: models/trading/intraday.py
purpose: Measures pre-market gap from previous close and scores its direction and fill probability (gaps >2% fill ~65% of the time).
inputs: snapshot: dict
outputs: dict {gap_pct, direction, fill_prob, score}
calls: none
called_by: compute_intraday_signals
mutates: none
---

---
name: _sig_trend_bias
type: function
file: models/trading/intraday.py
purpose: Assesses daily trend regime by checking whether price is above MA20 and MA50, scoring bullish or bearish bias.
inputs: daily_bars: list[dict]
outputs: dict {ma20, ma50, above_ma20, above_ma50, label, score}
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
name: _trade_levels
type: function
file: models/trading/intraday.py
purpose: Calculates specific entry, stop-loss, and two take-profit levels based on 1.5× and 2.5× ATR from current price.
inputs: bars: list[dict], snapshot: dict, side: str ("long" or "short")
outputs: dict {side, entry, stop, target1, target2, atr, risk_per_share, rr_ratio}
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
purpose: Main entry point — runs all 8 intraday signals plus ensemble scoring and ATR-based trade levels from intraday bars, daily bars, and a live snapshot.
inputs: intraday_bars: list[dict], daily_bars: list[dict], snapshot: dict, daily_avg_volume: float = 0
outputs: dict {signals, score, levels}
calls: _sig_vwap, _sig_opening_range, _sig_rsi, _sig_relative_volume, _sig_gap, _sig_trend_bias, _sig_bollinger, _sig_volume_surge, _composite, _trade_levels
called_by: intraday_analysis (app.py), _analyze_one (screener.py)
mutates: none
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
name: _analyze_one
type: function
file: models/trading/screener.py
purpose: Fetches intraday and daily bars for one symbol and computes all signals; returns a ranked result dict or None on failure.
inputs: symbol: str, snapshot: dict
outputs: Optional[dict] {symbol, price, change_pct, volume, score, label, reasons, side, entry, stop, target1, target2, rr_ratio, atr, signals}
calls: get_bars, get_daily_bars, _avg_daily_volume, compute_intraday_signals
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
purpose: Fetches OHLCV history, current price snapshot, fundamentals, moving averages, and analyst recs for a ticker symbol via yfinance.
inputs: symbol: str, period: str = "3mo"
outputs: dict {symbol, history, price, fundamentals, moving_averages, analyst, sources} or {error}
calls: yf.Ticker, ticker.history, ticker.info, ticker.recommendations
called_by: run_trade_analysis
mutates: none
---

---
name: _detect_asset_type
type: function
file: fetchers/market_data.py
purpose: Classifies a ticker as stock, etf, fund, crypto, forex, or futures based on yfinance quoteType and symbol format.
inputs: info: dict, symbol: str
outputs: str
calls: none
called_by: fetch_ticker
mutates: none
---

---
name: search_ticker
type: function
file: fetchers/market_data.py
purpose: Searches yfinance for tickers matching a company name or partial symbol and returns top 5 matches.
inputs: query: str
outputs: list[dict {symbol, name, exchange, type}]
calls: yf.Search
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
name: place_order
type: function
file: fetchers/alpaca.py
purpose: Places a paper trading order (market, limit, stop, or stop-limit) on Alpaca.
inputs: symbol: str, qty: float, side: str, order_type: str, limit_price: Optional[float], stop_price: Optional[float], time_in_force: str = "day", client_order_id: Optional[str]
outputs: dict (Alpaca order response or error)
calls: _post
called_by: place_trade (app.py)
mutates: Alpaca paper account (creates order)
---

---
name: place_bracket_order
type: function
file: fetchers/alpaca.py
purpose: Places a bracket order (entry + take-profit limit + stop-loss stop) as a single atomic Alpaca order.
inputs: symbol: str, qty: float, side: str, entry_price: Optional[float], take_profit: float, stop_loss: float
outputs: dict (Alpaca order response or error)
calls: _post
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
purpose: FastAPI startup event handler that initializes the SQLite database on server start.
inputs: none
outputs: none
calls: init_db
called_by: FastAPI on_event("startup")
mutates: predicta.db
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
purpose: POST /analyze-trade — runs the full trading analysis pipeline for a ticker query.
inputs: body: TradeRequest
outputs: dict (trading analysis result)
calls: run_trade_analysis
called_by: HTTP POST /analyze-trade
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
purpose: POST /analyze-baseball — runs the full baseball analysis pipeline from a natural language query.
inputs: body: BaseballRequest
outputs: dict (full baseball analysis result)
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
purpose: Sports sub-landing with Soccer and Baseball cards; shows model pills (Elo, FIP, etc.) for each sport.
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

## db/database.py (additions)

---
name: _migrate_sport_check
type: function
file: db/database.py
purpose: One-time migration — recreates matches table with updated sport CHECK constraint that includes 'baseball'; cleans up any leftover _matches_bak from a previously interrupted run before attempting.
inputs: conn: sqlite3.Connection
outputs: none
calls: conn.execute, conn.commit
called_by: init_db
mutates: matches table (rename → recreate → copy → drop old)
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
purpose: Dict mapping ESPN MLB team abbreviations to 3-year park run factors (1.0 = neutral). Covers all 30 MLB venues.
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
purpose: Fuzzy-matches a user-supplied team name to an ESPN MLB team object using multiple name fields and difflib (cutoff 0.45).
inputs: name: str, teams: list[dict]
outputs: Optional[dict]
calls: difflib.get_close_matches
called_by: fetch_baseball_context
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
purpose: Fetches team batting stats from ESPN /teams/{id}/statistics; computes wRC+ approximation (OPS/0.730 × 100).
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
purpose: Builds a complete starter profile dict from a probable-pitcher stub; fetches detailed stats via _get_pitcher_stats and throwing hand via _get_pitcher_handedness; returns league-average defaults if pitcher is TBD or unknown.
inputs: probable: Optional[dict]
outputs: dict {name, fip, era, whip, k9, bb9, innings_pitched, games_started, recent_games, throws}
calls: _get_pitcher_stats, _get_pitcher_handedness
called_by: fetch_baseball_context
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
purpose: Returns (mu_f5, mu_l4): expected runs for innings 1-5 (starter FIP) and 6-9 (bullpen FIP) separately. Replaces single-FIP expected_runs as primary model function.
inputs: wrc_plus: float, opp_starter_fip: float, opp_bullpen_fip: float, park_factor: float = 1.0, is_home: bool = False
outputs: tuple[float, float] — (mu_f5 clamped 0.5-6.0, mu_l4 clamped 0.4-5.0)
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
purpose: Sends user's baseball query to Claude and returns structured JSON with home team, away team, date, and notes.
inputs: user_text: str
outputs: dict {team_a, team_b, date, notes}
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
purpose: Derives a team's bullpen FIP from team ERA and starter FIP using the innings formula (team_ERA×9 - starter_FIP×5) / 4; clamped to [3.0, 7.5]; returns LEAGUE_BULLPEN_FIP as fallback.
inputs: team_era: float, starter_fip: float
outputs: float
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: _format_baseball_markets
type: function
file: analyze_baseball.py
purpose: Transforms raw baseball market probability dicts into frontend-ready format with percentages, labels, and best-option flags. Includes first_five, last_four, nrfi, and team_totals sections.
inputs: markets: dict, team_home: str, team_away: str
outputs: dict {moneyline, run_line, totals, first_five, last_four, nrfi, team_totals}
calls: none
called_by: run_baseball_analysis
mutates: none
---

---
name: run_baseball_analysis
type: function
file: analyze_baseball.py
purpose: Full baseball_v2 pipeline: parse query → fetch ESPN MLB API → derive bullpen FIP → apply platoon wRC+ adjustment → split Poisson F5/L4 model → Elo blend → compute markets (incl. first_five, last_four) → persist to DB → Kelly sizing → AI narrative → return result dict.
inputs: user_query: str, bankroll: float = 1000.0
outputs: dict {match_id, team_a, team_b, team_home, team_away, prob_a, prob_b, mu_home, mu_away, mu_home_f5, mu_away_f5, mu_home_l4, mu_away_l4, starters (with throws, bullpen_fip), team_stats, markets, narrative, raw_sources, steps, …}
calls: parse_baseball_query, fetch_baseball_context, _derive_bullpen_fip, platoon_wrc_adjust, expected_runs_split, compute_baseball_markets, EloModel, kelly_stake, log_signal, get_db, generate_baseball_narrative, _format_baseball_markets
called_by: analyze_baseball (app.py)
mutates: matches, signals, predictions tables
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
name: _compute_point_probs
type: function
file: analyze_tennis.py
purpose: Compute P_serve (A wins point on A's serve) and P_return (A wins point on B's serve). SQI, RQI, surface win rate, and form all feed in as modifiers to the logistic — not blended as external percentages (Gemini fix).
inputs: sqi_a, rqi_a, sqi_b, rqi_b: float (SQI/RQI centred on 100); swr_a, swr_b, form_a, form_b: float; surface: str
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
name: run_tennis_analysis
type: function
file: analyze_tennis.py
purpose: Full tennis pipeline: parse query → fetch ESPN/TSDB → _compute_point_probs → markov_tennis_match (nested Markov) → Glicko-2 validation (logged only) → confidence shrinkage → persist to DB → Kelly sizing → AI narrative → Market Efficiency Model recommendation.
inputs: user_query: str, bankroll: float = 1000.0
outputs: dict {match_id, player_a, player_b, recommendation, recommendation_reason, sport, tour, surface, date, prob_a, prob_b, data_confidence, markov_sim, player_stats, h2h, last5_a, last5_b, narrative, raw_sources, steps, …}
calls: parse_tennis_query, fetch_tennis_context, _compute_point_probs, markov_tennis_match, Glicko2Model, kelly_stake, log_signal, get_db, generate_tennis_narrative
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
name: run_table_tennis_analysis
type: function
file: analyze_table_tennis.py
purpose: Full TT pipeline: parse → ITTF/WTT/Setka/TSDB fetch → AQI/RQI → Markov Chain sim → handedness → first-time premium → form → fatigue → line_movement → Glicko-2 → Bayesian prior shrinkage → market efficiency model → persist → Kelly → narrative.
inputs: user_query: str, bankroll: float, open_odds_a/b: float?, curr_odds_a/b: float?, matches_today_a/b: int
outputs: dict with match_id, player_a/b, recommendation, recommendation_reason, prob_a/b, data_confidence, player_stats, h2h, markov_sim, narrative, steps
calls: parse_table_tennis_query, fetch_table_tennis_context, interpret_table_tennis_signals, markov_match_prob, Glicko2Model, kelly_stake, generate_table_tennis_narrative, log_signal, get_db
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
name: fetch_match_result
type: function
file: fetchers/results_collector.py
purpose: Unified entry point — try Setka Cup then TT Cup to fetch the actual result of a completed match. Returns {result, score_a, score_b, source} or None.
inputs: player_a, player_b: str, match_date: str (ISO), sport: str, tour: str
outputs: Optional[dict]
calls: setka_fetch_result, ttcup_fetch_result
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
purpose: Scan DB for unresolved predictions past scheduled_at. Fetch actual results from Setka/TT Cup and record outcomes automatically. Returns summary with per-match status. dry_run=True fetches but doesn't write.
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

