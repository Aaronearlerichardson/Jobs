"""Legacy dedupe API — thin adapter over the unified store.

Earlier revisions kept a standalone `seen_jobs` SQLite table per track
(seen_jobs_remote.db / jobs_local_clinical.db). Both tracks now share one
store (jobcrawler/store.py) whose `jobs` table carries the dedupe state,
so this module just forwards. Kept so the classic orchestrator path and
older scripts keep working unchanged.

NOTE: pre-merge seen_jobs_*.db files are not auto-imported; the first run
after upgrading will re-surface previously seen postings once.
"""

import config

from . import store


def init_db(path=None):
    """Open the unified store. Honours a --db override on config."""
    return store.connect(path or getattr(config, "STORE_DB_PATH", None))


def is_new(conn, job_id):
    return store.is_new(conn, job_id)


def mark_seen(conn, job, track=None):
    store.mark_seen(conn, job, track=track)
