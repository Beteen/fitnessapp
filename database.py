import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path.home() / ".fitnessapp" / "data.db"


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activities (
                activity_id     TEXT PRIMARY KEY,
                start_time      TEXT NOT NULL,
                activity_type   TEXT,
                distance_km     REAL,
                duration_seconds INTEGER,
                avg_pace_sec_per_km REAL,
                avg_hr          INTEGER,
                max_hr          INTEGER,
                calories        INTEGER
            );

            CREATE TABLE IF NOT EXISTS daily_hr (
                date        TEXT PRIMARY KEY,
                resting_hr  INTEGER
            );

            CREATE TABLE IF NOT EXISTS sleep (
                date            TEXT PRIMARY KEY,
                sleep_score     INTEGER,
                duration_seconds INTEGER
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at   TEXT NOT NULL,
                days_synced INTEGER
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
