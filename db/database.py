import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

# PREDICTA_DB_PATH env var lets Railway (or any host) point the DB at a
# persistent volume (e.g. /data/predicta.db) so data survives redeploys.
DB_PATH = Path(os.environ.get("PREDICTA_DB_PATH", str(Path(__file__).parent.parent / "predicta.db")))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _migrate_sport_check(conn: sqlite3.Connection) -> None:
    """
    Widen matches.sport CHECK to include all active sports (baseball, esports).

    Concurrency-safe: schema.sql already ships the full constraint, so on any
    fresh DB this returns immediately without touching the table. The rename/
    rebuild path only runs to upgrade a pre-existing DB created by the old
    schema, and is wrapped so two overlapping callers (e.g. a web request and a
    background thread both calling init_db) can never crash on a half-migrated
    _matches_bak — the loser rolls back and the winner's result stands.
    """
    FULL_CONSTRAINT = "'soccer','table_tennis','tennis','baseball','esports'"

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='matches'"
    ).fetchone()

    # Already up to date (or table not created yet) — nothing to do.
    if not row or FULL_CONSTRAINT in row[0]:
        return

    # legacy_alter_table=ON stops SQLite from rewriting child-table foreign keys
    # (predictions/signals/outcomes/odds_snapshots REFERENCES matches) to point
    # at _matches_bak during the RENAME. Without this, dropping _matches_bak
    # leaves those children with a dangling FK → "no such table: _matches_bak"
    # on the next INSERT. _repair_matches_fk() cleans up any DB already corrupted
    # by the old migration.
    try:
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("DROP TABLE IF EXISTS _matches_bak")
        conn.execute("ALTER TABLE matches RENAME TO _matches_bak")
        conn.execute(f"""
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY,
                sport TEXT NOT NULL CHECK(sport IN ({FULL_CONSTRAINT})),
                league TEXT,
                participant_a TEXT NOT NULL,
                participant_b TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'scheduled' CHECK(status IN ('scheduled','live','final')),
                venue TEXT,
                neutral_site INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO matches SELECT * FROM _matches_bak")
        conn.execute("DROP TABLE _matches_bak")
        conn.commit()
    except Exception:
        # A concurrent init_db won the race, or a prior attempt was interrupted.
        # Roll back our partial work, then recover to a valid `matches` table
        # regardless of the intermediate state we find.
        conn.rollback()
        have_matches = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='matches'"
        ).fetchone()
        have_bak = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_matches_bak'"
        ).fetchone()
        try:
            if not have_matches and have_bak:
                # RENAME committed but rebuild didn't — restore the original.
                conn.execute("ALTER TABLE _matches_bak RENAME TO matches")
                conn.commit()
            elif have_matches and have_bak:
                # Winner already rebuilt matches — just drop the leftover backup.
                conn.execute("DROP TABLE _matches_bak")
                conn.commit()
        except Exception:
            conn.rollback()
    finally:
        try:
            conn.execute("PRAGMA legacy_alter_table=OFF")
        except Exception:
            pass


def _repair_matches_fk(conn: sqlite3.Connection) -> None:
    """
    Repair a DB corrupted by the old sport-check migration.

    The pre-fix migration did `ALTER TABLE matches RENAME TO _matches_bak`
    without legacy_alter_table, so SQLite rewrote the foreign keys in every
    child table (predictions, signals, outcomes, odds_snapshots) to reference
    _matches_bak. Once _matches_bak was dropped, any INSERT into those children
    failed with "no such table: main._matches_bak".

    This rewrites the stored DDL text, changing every dangling _matches_bak
    reference back to matches. Idempotent — a no-op once the schema is clean.
    """
    n_bad = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE sql LIKE '%_matches_bak%'"
    ).fetchone()[0]
    if not n_bad:
        return

    # Resolve any physical _matches_bak table first so its own CREATE DDL is not
    # rewritten into a second 'matches' definition.
    has_matches = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='matches'"
    ).fetchone()
    has_bak = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_matches_bak'"
    ).fetchone()
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        if has_bak and has_matches:
            conn.execute("DROP TABLE _matches_bak")
        elif has_bak and not has_matches:
            conn.execute("ALTER TABLE _matches_bak RENAME TO matches")
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")

    # Rewrite any remaining child-table FK references in the stored schema text.
    still_bad = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE sql LIKE '%_matches_bak%'"
    ).fetchone()[0]
    if still_bad:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = replace(sql, '_matches_bak', 'matches') "
            "WHERE sql LIKE '%_matches_bak%'"
        )
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()


def _migrate_intraday_trades(conn: sqlite3.Connection) -> None:
    """
    Idempotent migration for intraday_trades. Checks every expected column
    and adds any that are missing — safe to call on every startup regardless
    of schema version. For empty tables, drops and recreates for clean NOT NULL
    constraint semantics. Never destroys rows.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_trades'"
    ).fetchone()
    if not row:
        return  # Table doesn't exist yet; schema.sql CREATE IF NOT EXISTS handles it

    # All expected columns beyond the original v1 schema
    expected_cols = [
        # v2: lifecycle logging + position sizing
        ("qty",                 "REAL"),
        ("position_value",      "REAL"),
        ("time_of_day_label",   "TEXT"),
        ("stop_price",          "REAL"),
        ("target_price",        "REAL"),
        ("risk_dollars",        "REAL"),
        ("spread_pct_at_entry", "REAL DEFAULT 0.0"),
        ("pnl_r",               "REAL"),
        ("adjusted_pnl",        "REAL"),
        ("alpaca_order_id",     "TEXT"),
        # v3: signal-only / hypothetical mode
        ("target1_price",       "REAL"),
        ("theoretical_entry",   "REAL"),
        ("liquidity_label",     "TEXT"),
        ("intraday_vol",        "REAL"),
        ("relative_volume",     "REAL"),
        ("model_version",       "TEXT"),
        ("is_hypothetical",     "INTEGER DEFAULT 0"),
        ("notes",               "TEXT"),
        # v4: per-signal scores for calibration feedback loop
        ("composite_raw",       "REAL"),
        ("vwap_score",          "REAL"),
        ("or_score",            "REAL"),
        ("rsi_score",           "REAL"),
        ("relvol_score",        "REAL"),
        ("gap_score",           "REAL"),
        ("trend_score",         "REAL"),
        ("bollinger_score",     "REAL"),
        ("volsurge_score",      "REAL"),
        ("ngram_signal",        "TEXT"),
        ("ngram_confidence",    "REAL"),
        # v5: market regime tags (Kimi review) — logged only, not applied to scoring
        ("spy_gap_pct",         "REAL"),
        ("xlk_change_pct",      "REAL"),
        ("market_regime",       "TEXT"),
        # v5b: market vol regime tag (Kimi review, round 3) — logged only
        ("spy_realized_vol_pct", "REAL"),
        ("market_vol_regime",   "TEXT"),
    ]

    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(intraday_trades)").fetchall()
    }

    # If still at original v1 schema (exit_time was NOT NULL) and no rows, recreate cleanly
    count = conn.execute("SELECT COUNT(*) FROM intraday_trades").fetchone()[0]
    if count == 0 and "alpaca_order_id" not in existing_cols:
        conn.execute("DROP TABLE IF EXISTS intraday_trades")
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        return

    # Add any missing columns (idempotent)
    for col, coltype in expected_cols:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE intraday_trades ADD COLUMN {col} {coltype}")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_alpaca ON intraday_trades(alpaca_order_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON intraday_trades(symbol, entry_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_hypo ON intraday_trades(is_hypothetical, entry_time)"
    )
    conn.commit()


def _migrate_new_tables(conn: sqlite3.Connection) -> None:
    """Create any tables added after initial deployment (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suspended_pairs (
            id INTEGER PRIMARY KEY,
            sym1 TEXT NOT NULL,
            sym2 TEXT NOT NULL,
            reason TEXT,
            suspended_at TEXT DEFAULT (datetime('now')),
            reinstate_after TEXT,
            UNIQUE(sym1, sym2)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_suspended_pairs ON suspended_pairs(sym1, sym2)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nrfi_bets (
            id               INTEGER PRIMARY KEY,
            game_date        TEXT    NOT NULL,
            home_team        TEXT    NOT NULL,
            away_team        TEXT    NOT NULL,
            home_starter     TEXT,
            away_starter     TEXT,
            p_nrfi           REAL    NOT NULL,
            verdict          TEXT    NOT NULL,
            confidence       TEXT,
            kelly_full_pct   REAL,
            kelly_half_pct   REAL,
            recommended_stake REAL,
            bankroll         REAL,
            market_odds      TEXT,
            outcome          INTEGER,
            home_1st_runs    INTEGER,
            away_1st_runs    INTEGER,
            won              INTEGER,
            pnl_units        REAL,
            logged_at        TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at      TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nrfi_bets_date ON nrfi_bets(game_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nrfi_bets_verdict ON nrfi_bets(verdict, game_date)"
    )
    # Round 6 P1 (Kimi): FBref response cache with 7-day TTL. When Cloudflare
    # bypass fails, we serve stale data (flagged) instead of dropping to the
    # partial-data threshold. kind is one of 'gk'|'pressing'|'possession'|'aerials'.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fbref_cache (
            id            INTEGER PRIMARY KEY,
            league        TEXT NOT NULL,
            season        INTEGER NOT NULL,
            team          TEXT NOT NULL,
            kind          TEXT NOT NULL,
            data_json     TEXT NOT NULL,
            cached_at     TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(league, season, team, kind)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fbref_cache_lookup ON fbref_cache(league, season, team, kind)"
    )
    conn.commit()


def _migrate_nrfi_clv(conn: sqlite3.Connection) -> None:
    """
    Idempotent: add Closing Line Value (CLV) columns to nrfi_bets.
    entry_* = the NRFI/YRFI line available when we made the pick;
    closing_* = the last line captured before first pitch;
    clv_pp = vig-free closing prob − entry prob on the bet side (percentage
    points); beat_close = 1 when clv_pp > 0. See models/devig.nrfi_clv.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nrfi_bets'"
    ).fetchone()
    if not row:
        return  # created by _migrate_new_tables / schema.sql on first run

    clv_cols = [
        ("entry_nrfi_dec",      "REAL"),
        ("entry_yrfi_dec",      "REAL"),
        ("entry_book",          "TEXT"),
        ("entry_odds_at",       "TEXT"),
        ("closing_nrfi_dec",    "REAL"),
        ("closing_yrfi_dec",    "REAL"),
        ("closing_book",        "TEXT"),
        ("closing_odds_at",     "TEXT"),
        ("clv_pp",              "REAL"),
        ("beat_close",          "INTEGER"),
    ]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(nrfi_bets)").fetchall()}
    for col, coltype in clv_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE nrfi_bets ADD COLUMN {col} {coltype}")
    conn.commit()


def _migrate_nrfi_all_markets(conn: sqlite3.Connection) -> None:
    """
    Idempotent: add game_pk + moneyline/F5/O-U columns to nrfi_bets.

    Every call to run_baseball_analysis() (manual website query OR the
    automated daily pipeline) logs one row here via _log_prediction, but until
    this migration only the NRFI market was captured — a manual query's
    moneyline/F5/O-U pick was computed, shown once, and never persisted or
    graded. game_pk lets resolve_pending_bets() (scripts/daily_nrfi.py) fetch
    the real final linescore later and grade all four markets, the same way
    the automated pipeline's JSON files already do.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nrfi_bets'"
    ).fetchone()
    if not row:
        return

    cols = [
        ("game_pk",      "INTEGER"),
        ("ml_pick",      "TEXT"),
        ("ml_prob",      "REAL"),
        ("ml_verdict",   "TEXT"),
        ("ml_correct",   "INTEGER"),
        ("f5_pick",      "TEXT"),
        ("f5_prob",      "REAL"),
        ("f5_verdict",   "TEXT"),
        ("f5_correct",   "INTEGER"),
        ("ou_pick",      "TEXT"),
        ("ou_prob",      "REAL"),
        ("ou_verdict",   "TEXT"),
        ("ou_line",      "REAL"),
        ("ou_correct",   "INTEGER"),
        ("home_runs",    "INTEGER"),
        ("away_runs",    "INTEGER"),
    ]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(nrfi_bets)").fetchall()}
    for col, coltype in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE nrfi_bets ADD COLUMN {col} {coltype}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nrfi_bets_game_pk ON nrfi_bets(game_pk)"
    )
    conn.commit()


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)  # ensure volume dir exists on Railway
    schema = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    _migrate_sport_check(conn)
    _repair_matches_fk(conn)   # heal DBs corrupted by the old rename migration
    _migrate_intraday_trades(conn)
    _migrate_new_tables(conn)
    _migrate_nrfi_clv(conn)
    _migrate_nrfi_all_markets(conn)
    conn.close()


@contextmanager
def get_db(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
