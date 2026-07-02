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

    try:
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


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)  # ensure volume dir exists on Railway
    schema = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    _migrate_sport_check(conn)
    _migrate_intraday_trades(conn)
    _migrate_new_tables(conn)
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
