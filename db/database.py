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


def init_db(db_path: Path = DB_PATH) -> None:
    schema = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    _migrate_sport_check(conn)
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
