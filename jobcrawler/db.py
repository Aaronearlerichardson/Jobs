"""SQLite dedupe store for seen jobs."""

import sqlite3
from datetime import datetime

import config


def init_db():
    # Read DB_PATH off the live config module so a --db override (set on
    # config before crawl) is honoured without re-importing this module.
    conn = sqlite3.connect(config.DB_PATH)
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
