CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    sport TEXT NOT NULL CHECK(sport IN ('soccer','table_tennis','tennis','baseball','esports')),
    league TEXT,
    participant_a TEXT NOT NULL,
    participant_b TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT DEFAULT 'scheduled' CHECK(status IN ('scheduled','live','final')),
    venue TEXT,
    neutral_site INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    participant TEXT,
    signal_name TEXT NOT NULL,
    signal_value REAL,
    signal_text TEXT,
    source TEXT,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    method TEXT NOT NULL,
    prob_a REAL NOT NULL,
    prob_b REAL,
    prob_draw REAL,
    explanation TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    book TEXT NOT NULL,
    market TEXT NOT NULL,
    price_a REAL,
    price_b REAL,
    price_draw REAL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id) UNIQUE,
    result TEXT NOT NULL CHECK(result IN ('a','b','draw')),
    score_a INTEGER,
    score_b INTEGER,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS elo_ratings (
    id INTEGER PRIMARY KEY,
    team TEXT NOT NULL UNIQUE,
    rating REAL NOT NULL DEFAULT 1500.0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS glicko2_ratings (
    id INTEGER PRIMARY KEY,
    participant TEXT NOT NULL,
    sport TEXT NOT NULL,
    surface TEXT DEFAULT 'all',
    rating REAL NOT NULL DEFAULT 1500.0,
    rd REAL NOT NULL DEFAULT 350.0,
    volatility REAL NOT NULL DEFAULT 0.06,
    updated_at TEXT NOT NULL,
    UNIQUE(participant, sport, surface)
);

CREATE TABLE IF NOT EXISTS intraday_trades (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,                          -- NULL until position closed
    side TEXT NOT NULL CHECK(side IN ('long', 'short')),
    entry_price REAL NOT NULL,
    exit_price REAL,                         -- NULL until position closed
    qty REAL,
    position_value REAL,
    planned_hold_bars INTEGER,
    actual_hold_bars INTEGER,
    entry_score REAL,
    time_of_day_label TEXT,
    stop_price REAL,
    target1_price REAL,
    target_price REAL,                       -- target2 (wider R/R)
    theoretical_entry REAL,                  -- snapshot price at signal time
    risk_dollars REAL,
    exit_reason TEXT,
    slippage_entry REAL DEFAULT 0.0,
    slippage_exit REAL DEFAULT 0.0,
    spread_pct_at_entry REAL DEFAULT 0.0,
    liquidity_label TEXT,
    intraday_vol REAL,                       -- bar_vol_pct from intraday EM
    relative_volume REAL,                    -- rel_vol at signal time
    pnl_dollars REAL,
    pnl_pct REAL,
    pnl_r REAL,
    adjusted_pnl REAL,
    alpaca_order_id TEXT,
    model_version TEXT,
    is_hypothetical INTEGER DEFAULT 0,       -- 1 = signal-only, no real order
    notes TEXT,
    -- v4: per-signal scores for calibration feedback loop
    composite_raw REAL,                      -- pre-modifier composite score
    vwap_score REAL,
    or_score REAL,
    rsi_score REAL,
    relvol_score REAL,
    gap_score REAL,
    trend_score REAL,
    bollinger_score REAL,
    volsurge_score REAL,
    ngram_signal TEXT,                       -- UP / DOWN / NONE
    ngram_confidence REAL,
    -- v5: market regime tags (Kimi review) — logged so post-hoc analysis can
    -- separate results by market condition, not applied to live scoring
    spy_gap_pct REAL,                        -- SPY overnight gap pct at scan time
    xlk_change_pct REAL,                     -- XLK (tech sector ETF) day change pct — sector-rotation proxy
    market_regime TEXT,                      -- NORMAL / EXTREME (abs(spy_gap_pct) >= 2 pct, or intraday escalation)
    -- v5b: market vol regime tag (Kimi review, round 3) — logged only, not applied to scoring
    spy_realized_vol_pct REAL,                -- SPY 20-day annualized realized vol pct
    market_vol_regime TEXT,                  -- LOW / NORMAL / HIGH
    -- v5c: macro event tag (Kimi review, round 4) — logged only, not applied to scoring
    macro_event_today INTEGER,               -- 1 = known FOMC decision day, 0/NULL otherwise
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_alpaca ON intraday_trades(alpaca_order_id);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON intraday_trades(symbol, entry_time);

CREATE TABLE IF NOT EXISTS pair_signals (
    id INTEGER PRIMARY KEY,
    sym1 TEXT NOT NULL,
    sym2 TEXT NOT NULL,
    action TEXT CHECK(action IN ('LONG_SPREAD', 'SHORT_SPREAD', 'NONE')),
    zscore REAL,
    beta REAL,
    entry_z REAL,
    target_z REAL,
    stop_z REAL,
    confidence REAL,
    exit_z REAL,
    exit_time TEXT,
    pnl_pct REAL,
    exit_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pair_signals_sym ON pair_signals(sym1, sym2);
CREATE INDEX IF NOT EXISTS idx_pair_signals_created ON pair_signals(created_at);

CREATE TABLE IF NOT EXISTS ngram_models (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    pattern_len INTEGER NOT NULL,
    bar_count INTEGER,
    table_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(symbol, pattern_len)
);
CREATE INDEX IF NOT EXISTS idx_ngram_symbol ON ngram_models(symbol);

CREATE TABLE IF NOT EXISTS suspended_pairs (
    id INTEGER PRIMARY KEY,
    sym1 TEXT NOT NULL,
    sym2 TEXT NOT NULL,
    reason TEXT,
    suspended_at TEXT DEFAULT (datetime('now')),
    reinstate_after TEXT,
    UNIQUE(sym1, sym2)
);
CREATE INDEX IF NOT EXISTS idx_suspended_pairs ON suspended_pairs(sym1, sym2);

CREATE TABLE IF NOT EXISTS nrfi_bets (
    id               INTEGER PRIMARY KEY,
    game_date        TEXT    NOT NULL,
    home_team        TEXT    NOT NULL,
    away_team        TEXT    NOT NULL,
    home_starter     TEXT,
    away_starter     TEXT,
    p_nrfi           REAL    NOT NULL,   -- model probability 0-100
    verdict          TEXT    NOT NULL,   -- BET / LEAN / SKIP
    confidence       TEXT,               -- HIGH / MEDIUM / LOW
    kelly_full_pct   REAL,               -- full Kelly stake %
    kelly_half_pct   REAL,               -- half Kelly (recommended)
    recommended_stake REAL,              -- dollar amount at supplied bankroll
    bankroll         REAL,
    market_odds      TEXT,               -- American odds string if supplied ("−110")
    -- filled in after game resolves --
    outcome          INTEGER,            -- 1=NRFI, 0=YRFI, NULL=pending
    home_1st_runs    INTEGER,
    away_1st_runs    INTEGER,
    won              INTEGER,            -- 1=bet won, 0=lost, NULL=pending
    pnl_units        REAL,               -- +0.909 won / −1.0 lost (at −110)
    logged_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at      TEXT,
    -- Closing Line Value (CLV) tracking — see models/devig.nrfi_clv --
    entry_nrfi_dec   REAL,               -- NRFI decimal odds when pick was made
    entry_yrfi_dec   REAL,               -- YRFI decimal odds when pick was made
    entry_book       TEXT,               -- reference book for entry line
    entry_odds_at    TEXT,
    closing_nrfi_dec REAL,               -- NRFI decimal odds at/near first pitch
    closing_yrfi_dec REAL,               -- YRFI decimal odds at/near first pitch
    closing_book     TEXT,
    closing_odds_at  TEXT,
    clv_pp           REAL,               -- vig-free (close - entry) prob on bet side, pp
    beat_close       INTEGER             -- 1 = positive CLV (beat the close)
);
CREATE INDEX IF NOT EXISTS idx_nrfi_bets_date    ON nrfi_bets(game_date);
CREATE INDEX IF NOT EXISTS idx_nrfi_bets_verdict ON nrfi_bets(verdict, game_date);

-- Kimi review round 5 — Jane Street "never override the computer" enforcement:
-- every manually-placed order (bypassing the automated signal pipeline) is
-- logged here so overrides are visible, not silent.
CREATE TABLE IF NOT EXISTS manual_override_log (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    symbol TEXT,
    details TEXT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_manual_override_logged ON manual_override_log(logged_at);

-- Kimi review round 5 — Citadel "pod kill switch" applied to individual
-- signals: persists which signals have been auto-flagged for underperformance
-- so the state survives restarts. Requires manual resurrect_signal() call.
CREATE TABLE IF NOT EXISTS signal_kill_switches (
    signal TEXT PRIMARY KEY,
    killed_at TEXT NOT NULL,
    n_trades_at_kill INTEGER NOT NULL,
    win_rate_at_kill REAL,
    ci_upper_at_kill REAL,
    resurrected INTEGER DEFAULT 0,
    resurrected_at TEXT
);

-- Kimi review round 6 — D.E. Shaw hybrid model: an optional human safety
-- valve for EXTREME-regime days. Not mandatory — auto-skips after a timeout
-- so it never blocks automated collection.
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    score_value REAL NOT NULL,
    signals_json TEXT NOT NULL,
    levels_json TEXT NOT NULL,
    hold_bars INTEGER,
    regime_tags_json TEXT,
    status TEXT NOT NULL DEFAULT 'AWAITING_REVIEW',
    queued_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_by TEXT,
    trade_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status, queued_at);
