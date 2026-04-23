"""SQLite dedupe store for seen jobs."""

import sqlite3
from datetime import datetime

from config import DB_PATH


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id     TEXT PRIMARY KEY,
            company    TEXT,
            title      TEXT,
            url        TEXT,
            location   TEXT,
            first_seen TEXT
        )
    """)
    conn.commit()
    return conn


def is_new(conn, job_id):
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
    ).fetchone() is None


def mark_seen(conn, job):
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs VALUES (?,?,?,?,?,?)",
        (job["id"], job["company"], job["title"],
         job["url"], job["location"], datetime.now().isoformat()),
    )
    conn.commit()
