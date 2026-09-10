"""
The store's physical layer: the SQLite schema, the additive migrations that
bring an older file up to date, and the connection/transaction helpers
(connect, checkpoint, batch). No roster or job semantics live here; the
functions that read and write rows are in core.store, which re-exports
everything below so callers keep saying ``store.connect``.
"""

import sqlite3

import config

import tags


# --------------------------------------------------------------------------- #
#  Schema                                                                      #
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id             INTEGER PRIMARY KEY,
    name           TEXT UNIQUE NOT NULL,
    ats            TEXT,              -- greenhouse|lever|ashby|workday|...
    slug           TEXT,              -- board slug (non-workday)
    wd_tenant      TEXT,              -- workday triple
    wd_pod         INTEGER,
    wd_site        TEXT,
    careers_url    TEXT,
    local_job_count INTEGER DEFAULT 0, -- openings inside your [locality]
    total_job_count INTEGER DEFAULT 0,
    mission_tier   TEXT,              -- a tier name from profile [mission]
    mission_score  REAL,              -- 0..1 (alignment with what you care about)
    mission_reason TEXT,
    tags           TEXT,              -- comma scope tokens; see tags.py
    source         TEXT,              -- how it was discovered
    active         INTEGER DEFAULT 1, -- crawl this company?
    last_probed    TEXT,
    notes          TEXT,
    created_at     TEXT,              -- first time this row was inserted
    miss_reason    TEXT,              -- why it is not crawlable (see MISS_REASONS)
    miss_at        TEXT               -- when that failure was last recorded
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY,
    job_id         TEXT UNIQUE NOT NULL,  -- source-stable id
    company_id     INTEGER REFERENCES companies(id),
    company_name   TEXT,
    title          TEXT,
    url            TEXT,
    location       TEXT,
    track          TEXT,                  -- comma-separated SET of track names
    geo_mode       TEXT,                  -- onsite|remote
    remote_eligible INTEGER,              -- 1 when the remote filter passed
    remote_signal  TEXT,                  -- phrase/hint that marked it remote
    anchor_signal  TEXT,                  -- the CORE keyword that anchored it
    description    TEXT,
    desc_checked_at TEXT,                 -- last FAILED description backfill
                                          -- attempt (ops.backfill_* skip
                                          -- recently-checked rows)
    resume_fit_score REAL,
    fit_reason     TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    status         TEXT DEFAULT 'open'   -- open|closed (see sync_job_statuses)
);

-- Company names a person rejected from the review queue. Keyed by the
-- normalized name (see _name_key), so one rejected spelling blocks the rest.
CREATE TABLE IF NOT EXISTS name_blocklist (
    key      TEXT PRIMARY KEY,
    name     TEXT,              -- the spelling that was rejected
    reason   TEXT,
    added_at TEXT
);
"""

# Created after _ensure_columns: on a pre-merge DB the jobs table exists
# without `track`, so these must not run before the column migrations.
_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS ix_jobs_track   ON jobs(track);
"""

# Columns added after a table's first release: additive, idempotent
# migrations so existing DBs (e.g. an old local_tech.db) upgrade in place.
_MIGRATIONS = {
    "companies": {
        "tags": "TEXT",
        # No DEFAULT, unlike _SCHEMA's fresh-DB declaration: ADD COLUMN with a
        # default writes that default into every existing row, which would
        # make the _RENAMED_COLUMNS copy below (guarded on IS NULL) a no-op
        # and silently drop the counts inherited from nc_job_count.
        "local_job_count": "INTEGER",   # was nc_job_count
        # When the row was first inserted. Deliberately NOT backfilled on an
        # existing DB: we do not know when a pre-migration row arrived, and a
        # backfill would invent a roster-growth spike at migration time. NULL
        # reads as "predates the column" (see roster_growth).
        "created_at":  "TEXT",
        # Why a candidate is not crawlable, and when we last found that out.
        # See MISS_REASONS; rows carrying these are always active=0.
        "miss_reason": "TEXT",
        "miss_at":     "TEXT",
        # Crawl scheduling (record_crawl_outcome / crawlable_companies).
        # 181 of 300 active companies had never produced a single job yet
        # were fetched on every crawl, and a handful of huge off-mission
        # boards (a state health agency: 663 local rows, best fit 0.15)
        # burned most of the run. `crawl_state` NULL reads as 'active', so
        # existing rows need no backfill.
        "crawl_state":      "TEXT",      # active|dormant|off (NULL=active)
        "empty_streak":     "INTEGER",   # consecutive empty DAYS
        "last_crawled_at":  "TEXT",
        "last_nonempty_at": "TEXT",
        "next_crawl_at":    "TEXT",      # dormant rows wake at/after this
        # Last SUCCESSFUL whole-board pull by the background harvester
        # (scrapers/harvest.py). Independent of the crawl stamps above: the
        # harvester ignores crawl_state and never writes it.
        "last_harvested_at": "TEXT",
    },
    "jobs": {
        # Stamped by the background harvester when it stored or refreshed
        # the row. A harvested row arrives with NO track and NO score; the
        # crawl adopts it (gates, scores, stamps its track) the next time
        # that company's board comes round -- see crawl_seen.
        "harvested_at":    "TEXT",
        # Harvest triage (scrapers/triage.py). NULL = not yet judged (or
        # judged but still waiting on a body); 'ok' = surfaced into the
        # track set below; anything else names the cheapest gate that
        # dropped the row (mission|title|anchor|geo|exclude|division|fit).
        # triage_detail is the per-track record ("local-tech=geo;...").
        "triage_status":   "TEXT",
        "triage_detail":   "TEXT",
        "triaged_at":      "TEXT",
        "track":           "TEXT",
        "remote_eligible": "INTEGER",
        "remote_signal":   "TEXT",
        "anchor_signal":   "TEXT",   # was neural_signal
        # Per-axis fit sub-scores (core/fit.py). resume_fit_score stays
        # the combined scalar; these expose the breakdown for querying/sorting.
        "fit_domain":      "REAL",
        "fit_function":    "REAL",
        "fit_stack":       "REAL",
        "fit_seniority":   "REAL",
        "fit_gates":       "TEXT",   # comma-joined tripped gate names, or NULL
        # Model id that wrote the current score (NULL before this column
        # existed). verify_top re-verifies a finalist unless fit_model is
        # the CURRENT verify model, so a model change re-reads the top N once.
        "fit_model":       "TEXT",
        # When status flipped to 'closed' (NULL while open). Set by
        # sync_job_statuses / set_job_status, cleared on reopen.
        "closed_at":       "TEXT",
        # Real posting date from the board (YYYY-MM-DD; first-known wins).
        # first_seen is when WE noticed it; posted_at is when it went up.
        "posted_at":       "TEXT",
        # The user's decision on this job (see DISPOSITIONS): drives ranking
        # exclusion, the digest pipeline section, and few-shot calibration.
        "disposition":      "TEXT",
        "disposition_note": "TEXT",
        "disposition_at":   "TEXT",
        # Last FAILED description-backfill attempt. The backfill ops skip
        # rows checked in the last few days: a posting that has dropped off
        # its board never matches, and without this stamp every rerun
        # re-fetched the same boards to fail on the same rows ("0 of 18
        # backfilled" three runs in a row, 2026-08-28 session logs).
        "desc_checked_at":  "TEXT",
        # Application-pipeline tracking (see update_pipeline_fields,
        # conversion_report, followups_due). applied_at is stamped the FIRST
        # time a row is marked applied and never again: a later
        # interviewing/rejected must not move the date the application went
        # out, or every elapsed-time question loses its clock. The other four
        # are user-entered; `referral` is 0/1, `outcome_reason` one of
        # OUTCOME_REASONS.
        "applied_at":       "TEXT",
        "followup_at":      "TEXT",   # YYYY-MM-DD, the next nudge
        "contact":          "TEXT",   # recruiter / hiring manager / referrer
        "referral":         "INTEGER",
        "outcome_reason":   "TEXT",
    },
}


# Columns whose CONTENT lives on under a new, field-neutral name: the old
# ones were named for one user's search ("neural" anchors, "nc" for the local
# region). new -> old; _ensure_columns copies old values across before the
# old column is dropped, so no history is lost on an existing DB.
_RENAMED_COLUMNS = {
    "jobs":      {"anchor_signal": "neural_signal"},
    "companies": {"local_job_count": "nc_job_count"},
}

# Columns retired for good. Dropped idempotently on connect so existing DBs
# (which keep old columns under CREATE TABLE IF NOT EXISTS) shed them too.
# mission/tech_bar_score became company-level after unification and
# hq_location was never populated — all three were 100% NULL. The last two
# are the _RENAMED_COLUMNS sources, dropped only after their copy runs.
_DROPPED_COLUMNS = {
    "jobs": ("mission", "tech_bar_score", "neural_signal"),
    "companies": ("hq_location", "nc_job_count"),
}


def _ensure_columns(conn):
    # Concurrency-tolerant: the web UI opens several connections to the same
    # DB at once (one per API request), and on a DB this process hasn't
    # migrated yet they all read PRAGMA table_info before any ALTER lands —
    # every loser then raises "duplicate column name" (or "no such column"
    # for drops). Both mean "another connection already did it": skip.
    for table, cols in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
    # Carry renamed columns' values across BEFORE the old ones are dropped.
    # Only fills rows the new column hasn't got a value for, so re-running is
    # a no-op and a re-crawl's fresh value is never overwritten by stale data.
    for table, renames in _RENAMED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for new, old in renames.items():
            if new in existing and old in existing:
                conn.execute(f"UPDATE {table} SET {new}={old} "
                             f"WHERE {new} IS NULL AND {old} IS NOT NULL")
    for table, cols in _DROPPED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                except sqlite3.OperationalError as e:
                    if "no such column" not in str(e).lower():
                        raise
    conn.commit()


def _migrate_tags(conn):
    """Rewrite retired company scope-tag tokens in place (tags.py).

    The tags started out named after one user's search ('nc_local', 'neural')
    and now say what they DO ('local', 'sweep'). Reads tolerate the old names
    via tags.canonical(), but rewriting the stored tokens keeps SQL tag
    filters — which match the literal token — honest. Idempotent, and done in
    Python rather than SQL string surgery so a row that somehow holds both a
    legacy name and its replacement collapses to one token instead of two.
    Costs one scan of a table with hundreds of rows, not millions.
    """
    if not tags.ALIASES:
        return
    where = " OR ".join(["(',' || tags || ',') LIKE ?"] * len(tags.ALIASES))
    rows = conn.execute(
        f"SELECT id, tags FROM companies WHERE tags IS NOT NULL AND ({where})",
        tuple(f"%,{legacy},%" for legacy in tags.ALIASES)).fetchall()
    for row in rows:
        conn.execute("UPDATE companies SET tags=? WHERE id=?",
                     (tags.join(tags.parse(row["tags"])), row["id"]))
    conn.commit()


# How long a writer waits on another process's write lock before giving up.
# The web UI, the scheduled crawl and the background harvester all write to
# one file, so a locked DB is normal, not an error.
BUSY_TIMEOUT_S = 30.0


def connect(path=None):
    """Open the store: schema applied, migrations run, WAL journaling on.

    WAL matters because several PROCESSES share this file (the web UI, the
    Task-Scheduler crawl, the background harvester): under the default
    rollback journal a reader blocks a writer and two writers collide the
    moment they overlap, and nothing here waited, so the loser died with
    "database is locked". In WAL mode readers never block the writer and
    the busy timeout queues writers instead of failing them. Cost: two
    sidecar files (jobs.db-wal, jobs.db-shm) beside the DB while any
    connection is open -- copy all three when backing up by hand, or run
    checkpoint() first.
    """
    conn = sqlite3.connect(path or config.STORE_DB_PATH,
                           timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass                    # read-only media: the pragmas are optional
    conn.executescript(_SCHEMA)
    conn.commit()
    _ensure_columns(conn)
    _migrate_tags(conn)
    conn.executescript(_INDEXES)
    conn.commit()
    return conn


def checkpoint(conn):
    """Fold the WAL back into the main file (before a file-copy backup)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


# Connections currently inside a batch() block: their per-row writers skip
# the commit and the block commits once at the end. Keyed by id() because
# sqlite3.Connection accepts no attributes.
_BATCHING = set()


def _commit(conn):
    """Commit unless the caller is batching (see batch)."""
    if id(conn) not in _BATCHING:
        conn.commit()


class batch:
    """Group many upsert_job / sync_job_statuses calls into ONE transaction.

    >>> from core.store import upsert_job, job_exists
    >>> conn = connect(":memory:")
    >>> with batch(conn):
    ...     for i in range(3):
    ...         _ = upsert_job(conn, {"job_id": f"b{i}", "title": "T"})
    >>> conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    3

    An exception inside the block rolls the whole group back:

    >>> try:
    ...     with batch(conn):
    ...         _ = upsert_job(conn, {"job_id": "b9", "title": "T"})
    ...         raise RuntimeError("boom")
    ... except RuntimeError:
    ...     pass
    >>> job_exists(conn, "b9")
    False

    Notes:
        The harvester stores one whole board per block: 1,000 rows as one
        write-lock acquisition instead of 1,000, which is what keeps it from
        starving the web UI's own writes while it runs.
    """

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        _BATCHING.add(id(self.conn))
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        _BATCHING.discard(id(self.conn))
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        return False
