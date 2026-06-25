CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    sport TEXT NOT NULL CHECK(sport IN ('soccer','table_tennis','tennis','baseball')),
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
    exit_time TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('long', 'short')),
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    planned_hold_bars INTEGER,
    actual_hold_bars INTEGER,
    entry_score REAL,
    exit_reason TEXT,
    slippage_entry REAL DEFAULT 0.0,
    slippage_exit REAL DEFAULT 0.0,
    pnl_dollars REAL,
    pnl_pct REAL,
    logged_at TEXT NOT NULL
);
