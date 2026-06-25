import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "predicta.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _migrate_sport_check(conn: sqlite3.Connection) -> None:
    """One-time migration: widen matches.sport CHECK to include 'baseball'."""
    # Clean up any leftover backup table from a previously interrupted migration
    bak_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_matches_bak'"
    ).fetchone()
    if bak_exists:
        conn.execute("DROP TABLE _matches_bak")
        conn.commit()

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='matches'"
    ).fetchone()
    if row and "'baseball'" not in row[0]:
        conn.execute("ALTER TABLE matches RENAME TO _matches_bak")
        conn.execute("""
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY,
                sport TEXT NOT NULL CHECK(sport IN ('soccer','table_tennis','tennis','baseball')),
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


def _migrate_intraday_trades(conn: sqlite3.Connection) -> None:
    """
    Upgrade intraday_trades to v2 schema:
    - Make exit_time / exit_price nullable (lifecycle logging: entry before exit)
    - Add columns: qty, position_value, time_of_day_label, stop_price, target_price,
      risk_dollars, spread_pct_at_entry, pnl_r, adjusted_pnl, alpaca_order_id
    Strategy: if the table has no rows (dev/paper, early stage), drop and recreate.
    Otherwise add missing nullable columns via ALTER TABLE.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='intraday_trades'"
    ).fetchone()
    if not row:
        return  # Table doesn't exist yet; schema.sql CREATE handles it

    existing_sql = row[0]
    if "alpaca_order_id" in existing_sql:
        return  # Already migrated

    # Check for existing rows
    count = conn.execute("SELECT COUNT(*) FROM intraday_trades").fetchone()[0]
    if count == 0:
        # Safe to drop and recreate with new schema
        conn.execute("DROP TABLE IF EXISTS intraday_trades")
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        return

    # Has data: add missing nullable columns without destroying rows
    existing_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(intraday_trades)").fetchall()
    }
    new_cols = [
        ("qty",                  "REAL"),
        ("position_value",       "REAL"),
        ("time_of_day_label",    "TEXT"),
        ("stop_price",           "REAL"),
        ("target_price",         "REAL"),
        ("risk_dollars",         "REAL"),
        ("spread_pct_at_entry",  "REAL DEFAULT 0.0"),
        ("pnl_r",                "REAL"),
        ("adjusted_pnl",         "REAL"),
        ("alpaca_order_id",      "TEXT"),
    ]
    for col, coltype in new_cols:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE intraday_trades ADD COLUMN {col} {coltype}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_alpaca ON intraday_trades(alpaca_order_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON intraday_trades(symbol, entry_time)"
    )
    conn.commit()


def init_db(db_path: Path = DB_PATH) -> None:
    schema = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    _migrate_sport_check(conn)
    _migrate_intraday_trades(conn)
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
