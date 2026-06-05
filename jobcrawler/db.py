"""SQLite dedupe store for seen jobs."""

import sqlite3
from datetime import datetime

from config import DB_PATH


def _ensure_columns(conn):
    """Additive, idempotent migrations for columns added after v1.

    Keeps existing databases usable when new optional fields are
    introduced (e.g. remote_eligible for the remote-neural track) without
    a destructive rebuild.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "remote_eligible" not in existing:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN remote_eligible INTEGER")
    conn.commit()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id          TEXT PRIMARY KEY,
            company         TEXT,
            title           TEXT,
            url             TEXT,
            location        TEXT,
            first_seen      TEXT,
            remote_eligible INTEGER
        )
    """)
    conn.commit()
    _ensure_columns(conn)
    return conn


def is_new(conn, job_id):
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
    ).fetchone() is None


def mark_seen(conn, job):
    """Insert a job record. Columns are named so callers that don't set
    optional fields (e.g. remote_eligible) still work unchanged."""
    remote = job.get("remote_eligible")
    if remote is not None:
        remote = int(bool(remote))
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs "
        "(job_id, company, title, url, location, first_seen, remote_eligible) "
        "VALUES (?,?,?,?,?,?,?)",
        (job["id"], job["company"], job["title"], job["url"],
         job["location"], datetime.now().isoformat(), remote),
    )
    conn.commit()
