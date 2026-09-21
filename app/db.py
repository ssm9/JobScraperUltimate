"""SQLite storage for jobs, settings and scrape-run history."""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("JSU_DATA_DIR", "/data")
DB_PATH = os.environ.get("JSU_DB_PATH", os.path.join(DATA_DIR, "jobscraper.db"))

_init_lock = threading.Lock()
_initialised = False

DEFAULT_SETTINGS = {
    # --- search ---
    "search_term": "software engineer",
    "location": "",
    "country_to_search": "USA",
    "scrape_from": ["linkedin", "indeed", "skillsire"],
    "results_fetch_count": 100,
    "hours": 24,
    "job_type": "",
    "is_remote": False,
    "fetch_description": False,
    # --- filtering ---
    "roles_of_interest": ["Backend", "Frontend", "Developer"],
    "exclude_keywords": [],
    # --- schedule ---
    "schedule_enabled": False,
    "sleep_time": 1800,
    # --- email ---
    "email_send": False,
    "from_email": "",
    "email_password": "",
    "to_email": "",
    "email_smtp": "",
    "email_port": 587,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_url       TEXT NOT NULL UNIQUE,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    site          TEXT,
    job_type      TEXT,
    is_remote     INTEGER DEFAULT 0,
    salary        TEXT,
    description   TEXT,
    date_posted   TEXT,
    first_seen    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',
    notes         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_site ON jobs(site);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    status       TEXT NOT NULL DEFAULT 'running',
    scraped      INTEGER DEFAULT 0,
    matched      INTEGER DEFAULT 0,
    new_jobs     INTEGER DEFAULT 0,
    message      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        with get_conn() as conn:
            conn.executescript(SCHEMA)
        _initialised = True


def get_settings() -> dict:
    """Stored settings merged over defaults, so new keys appear automatically."""
    settings = dict(DEFAULT_SETTINGS)
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        if row["key"] in settings:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                pass
    return settings


def save_settings(updates: dict) -> dict:
    known = {k: v for k, v in updates.items() if k in DEFAULT_SETTINGS}
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(k, json.dumps(v)) for k, v in known.items()],
        )
    return get_settings()
