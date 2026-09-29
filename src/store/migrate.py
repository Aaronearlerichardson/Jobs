"""Store migrations: the numbered .sql files in migrations/, applied in order
and recorded in the file's PRAGMA user_version.

A schema change is a new file (0002_<what>.sql), never an edit of an applied
one. connect() calls migrate() on every open: an up-to-date store costs one
PRAGMA, and a store that needs work is migrated in a single transaction that
one process wins (BEGIN IMMEDIATE) while the others wait, then find nothing
left to do. The web UI opens several connections at once, so that matters.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from src import tags

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _statements(script: str) -> Iterator[str]:
    """The statements of a .sql script, one at a time: executescript would
    COMMIT the migration's transaction first."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration the store has not seen; no-op when current."""
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if conn.execute("PRAGMA user_version").fetchone()[0] >= len(files):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the lock: another process may have just migrated.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='companies'").fetchone():
            _adopt_unversioned(conn, files[0].read_text(encoding="utf-8"))
        for n, path in enumerate(files, 1):
            if n > version:
                for statement in _statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {n}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _adopt_unversioned(conn: sqlite3.Connection, baseline: str) -> None:
    """Bring a store from before versioning up to the baseline's SHAPE, so
    the baseline's own statements (IF NOT EXISTS tables, then indexes on
    columns that must exist by then) apply cleanly.

    Frozen: once every store has been opened by this version, so has passed
    through here, this function can be deleted.

    * Columns the baseline has and the store lacks are added, by name and
      type only. ADD COLUMN with a DEFAULT writes it into every existing
      row, which would make the rename copy below (guarded on IS NULL) a
      no-op and drop the counts inherited from nc_job_count.
    * Columns whose CONTENT lives on under a new, field-neutral name (the old
      ones were named for one user's search: "neural" anchors, "nc" for the
      local region) are copied across, then the old column is dropped. Two
      more, never populated, are dropped outright: hq_location, and the
      job-level mission/tech_bar_score that became company-level.
    * Retired company scope-tag tokens ("nc_local", "neural") are rewritten
      to their current names (src/tags.py), so SQL tag filters, which match
      the literal token, stay honest.
    """
    with contextlib.closing(sqlite3.connect(":memory:")) as shape:
        shape.executescript(baseline)
        for (table,) in shape.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for _, col, decl, *_ in shape.execute(f"PRAGMA table_info({table})"):
                if have and col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    renames = {("jobs", "anchor_signal"): "neural_signal",
               ("companies", "local_job_count"): "nc_job_count"}
    drops = {"jobs": ("mission", "tech_bar_score", "neural_signal"),
             "companies": ("hq_location", "nc_job_count")}
    for table, cols in drops.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for (t, new), old in renames.items():
            if t == table and old in have:
                conn.execute(f"UPDATE {table} SET {new}={old} "
                             f"WHERE {new} IS NULL AND {old} IS NOT NULL")
        for col in cols:
            if col in have:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    like = " OR ".join(["(',' || tags || ',') LIKE ?"] * len(tags.ALIASES))
    for cid, raw in conn.execute(
            f"SELECT id, tags FROM companies WHERE tags IS NOT NULL AND ({like})",
            tuple(f"%,{legacy},%" for legacy in tags.ALIASES)).fetchall():
        conn.execute("UPDATE companies SET tags=? WHERE id=?",
                     (tags.join(tags.parse(raw)), cid))
