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
    "exclude_companies": [],
    "collapse_duplicates": True,
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
    notes         TEXT DEFAULT '',
    fingerprint   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_site ON jobs(site);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

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


def preflight() -> None:
    """Fail with an actionable message if the data volume isn't writable.

    Raw sqlite3 just says "unable to open database file", which doesn't say
    which path, which uid, or what to do about it.
    """
    directory = os.path.dirname(DB_PATH) or "."
    uid, gid = os.getuid(), os.getgid()

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create data directory {directory!r} (running as uid:gid {uid}:{gid}): {exc}. "
            f"Create the dataset on the host and give it to {uid}:{gid}, "
            f"e.g. `chown -R {uid}:{gid} /mnt/<pool>/apps/jobscraper`."
        ) from exc

    if not os.access(directory, os.W_OK | os.X_OK):
        info = os.stat(directory)
        raise RuntimeError(
            f"Data directory {directory!r} is not writable. The container runs as "
            f"uid:gid {uid}:{gid}, but the directory is owned by {info.st_uid}:{info.st_gid} "
            f"with mode {oct(info.st_mode & 0o777)}. Fix it on the TrueNAS host with "
            f"`chown -R {uid}:{gid} /mnt/<pool>/apps/jobscraper`, or set `user: \"{info.st_uid}:{info.st_gid}\"` "
            f"in the app's compose YAML to match the directory."
        )


def init_db() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        preflight()
        with get_conn() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)
        _initialised = True


def _migrate(conn) -> None:
    """Add columns introduced after the first release, and backfill them."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}

    if "fingerprint" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN fingerprint TEXT")

    # Created here rather than in SCHEMA: on an upgraded database the column
    # only exists once the ALTER above has run.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint)")

    # Backfill any rows still missing a fingerprint (fresh column, or rows
    # written by an older version).
    from .scraper import fingerprint  # local import: scraper imports from db

    rows = conn.execute(
        "SELECT id, company, title FROM jobs WHERE fingerprint IS NULL"
    ).fetchall()
    if rows:
        conn.executemany(
            "UPDATE jobs SET fingerprint = ? WHERE id = ?",
            [(fingerprint(r["company"], r["title"]), r["id"]) for r in rows],
        )


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
