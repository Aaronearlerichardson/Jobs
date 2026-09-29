"""
The store's physical layer: the SQLite schema, the additive migrations that
bring an older file up to date, and the connection/transaction helpers
(connect, batch, Writer). No roster or job semantics live here; the
functions that read and write rows are in src.store, which re-exports
everything below so callers keep saying ``store.connect``.
"""

from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import threading
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any, Concatenate, Literal, cast

from src import config
from src import tags

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
    tags           TEXT,              -- comma scope tokens; see src/tags.py
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
-- upsert_job probes `url` for every NEW row (the re-key path that catches a
-- posting arriving under a changed id scheme). Unindexed, that probe was a
-- full scan of a table whose rows carry whole job descriptions: 0.23s each
-- on a 240 MB store, INSIDE the batch transaction. A 183-job board therefore
-- held the single write lock for ~40s, and every other writer -- the other
-- harvest threads, and the web UI's own edits -- timed out against
-- BUSY_TIMEOUT_S with "database is locked" (2026-09-10 harvest logs).
CREATE INDEX IF NOT EXISTS ix_jobs_url     ON jobs(url);
-- triage_pending selects the harvester's unjudged rows on this column.
CREATE INDEX IF NOT EXISTS ix_jobs_triage  ON jobs(triage_status);
"""

# Columns added after a table's first release: additive, idempotent
# migrations so existing DBs (e.g. an old local_tech.db) upgrade in place.
_MIGRATIONS: dict[str, dict[str, str]] = {
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
        # (src/crawl/harvest.py). Independent of the crawl stamps above: the
        # harvester ignores crawl_state and never writes it.
        "last_harvested_at": "TEXT",
    },
    "jobs": {
        # Stamped by the background harvester when it stored or refreshed
        # the row. A harvested row arrives with NO track and NO score; the
        # crawl adopts it (gates, scores, stamps its track) the next time
        # that company's board comes round -- see crawl_seen.
        "harvested_at":    "TEXT",
        # Harvest triage (src/crawl/triage.py). NULL = not yet judged (or
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
        # Per-axis fit sub-scores (src/claude/fit.py). resume_fit_score stays
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
        # Consecutive closure probes that could NOT verify the row either
        # way (bot-gated host, JS-only page, an ATS with no closure signal,
        # a probe that raised). The companies-side empty_streak, one level
        # down: src.ops.status.check_closed_jobs stops selecting a row
        # at CLOSED_PROBE_GIVE_UP, and any live sighting -- a probe that
        # confirms it open, a board that lists it again -- resets it to 0.
        # NULL reads as 0, so existing rows need no backfill.
        "probe_streak":     "INTEGER",
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
_RENAMED_COLUMNS: dict[str, dict[str, str]] = {
    "jobs":      {"anchor_signal": "neural_signal"},
    "companies": {"local_job_count": "nc_job_count"},
}

# Columns retired for good. Dropped idempotently on connect so existing DBs
# (which keep old columns under CREATE TABLE IF NOT EXISTS) shed them too.
# mission/tech_bar_score became company-level after unification and
# hq_location was never populated — all three were 100% NULL. The last two
# are the _RENAMED_COLUMNS sources, dropped only after their copy runs.
_DROPPED_COLUMNS: dict[str, tuple[str, ...]] = {
    "jobs": ("mission", "tech_bar_score", "neural_signal"),
    "companies": ("hq_location", "nc_job_count"),
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
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
    for table, dropped in _DROPPED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col in dropped:
            if col in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                except sqlite3.OperationalError as e:
                    if "no such column" not in str(e).lower():
                        raise
    conn.commit()


def _migrate_tags(conn: sqlite3.Connection) -> None:
    """Rewrite retired company scope-tag tokens in place (src/tags.py).

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


# Python functions the store's SQL calls by name (norm_title, name_key, ...).
# A module declares one with @sql_function beside the Python rule it wraps,
# so a rule lives in one place whether a row loop or a query applies it, and
# connect() installs every one on each new connection.
SQL_FUNCTIONS: dict[str, tuple[int, Callable[..., Any]]] = {}


def sql_function[F: Callable[..., Any]](name: str, narg: int) -> Callable[[F], F]:
    """Register `fn` as SQL function `name` (see SQL_FUNCTIONS); returns `fn`
    unchanged, so Python callers keep using it directly."""
    def register(fn: F) -> F:
        SQL_FUNCTIONS[name] = (narg, fn)
        return fn
    return register


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the store: schema applied, migrations run, WAL journaling on.

    WAL matters because several PROCESSES share this file (the web UI, the
    Task-Scheduler crawl, the background harvester): under the default
    rollback journal a reader blocks a writer and two writers collide the
    moment they overlap, and nothing here waited, so the loser died with
    "database is locked". In WAL mode readers never block the writer and
    the busy timeout queues writers instead of failing them. Cost: two
    sidecar files (jobs.db-wal, jobs.db-shm) beside the DB while any
    connection is open -- copy all three when backing up by hand (the
    -wal is not optional: the newest writes live there until SQLite
    folds them back in).

    Any thread may use the connection, one at a time: a Writer adopts one
    opened elsewhere (sqlite3 is serialized, threadsafety 3).
    """
    conn = sqlite3.connect(path or config.STORE_DB_PATH,
                           timeout=BUSY_TIMEOUT_S, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for name, (narg, fn) in SQL_FUNCTIONS.items():
        conn.create_function(name, narg, fn, deterministic=True)
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


# Connections currently inside a batch() block: their per-row writers skip
# the commit and the block commits once at the end. Keyed by id() because
# sqlite3.Connection accepts no attributes.
_BATCHING: set[int] = set()


def _commit(conn: sqlite3.Connection) -> None:
    """Commit unless the caller is batching (see batch)."""
    if id(conn) not in _BATCHING:
        conn.commit()


class sql:
    """A SET right-hand side that is an SQL EXPRESSION, not a value.

    ``apply_update({"applied_at": sql("COALESCE(applied_at, ?)", now)})``
    writes ``applied_at=COALESCE(applied_at, ?)`` and passes `now`. The
    partial updates that keep a first-known value (the apply date, a
    posting's first body, its first geo verdict) need this; everything
    else passes a plain value.
    """

    __slots__ = ("expr", "args")

    def __init__(self, expr: str, *args: Any) -> None:
        self.expr, self.args = expr, args


def apply_update(conn: sqlite3.Connection, table: str, id_col: str, id_val: Any,
                 fields: Mapping[str, Any]) -> int:
    """Write just the columns `fields` names on one row; return rows changed.

    Five writers -- the crawl-outcome stamp, the harvest stamp, the triage
    verdict, a disposition, the pipeline fields -- each build a SET clause
    from whichever columns this particular call decided to touch, and each
    had written the same three lines: join ``col=?``, splat the values plus
    the id, commit. Two of them built it out of parallel `sets`/`args`
    LISTS, which is the same thing with the column-to-value pairing left
    for the reader to check by eye.

    An empty `fields` writes nothing (and does not commit): "no columns to
    change" is a normal outcome for a caller whose arguments were all None.

    >>> conn = connect(":memory:")
    >>> _ = conn.execute("INSERT INTO companies (name, active) VALUES ('A', 1)")
    >>> cid = conn.execute("SELECT id FROM companies").fetchone()["id"]
    >>> apply_update(conn, "companies", "id", cid,
    ...              {"crawl_state": "dormant", "empty_streak": 3})
    1
    >>> dict(conn.execute("SELECT crawl_state, empty_streak "
    ...                   "FROM companies").fetchone())
    {'crawl_state': 'dormant', 'empty_streak': 3}
    >>> apply_update(conn, "companies", "id", cid, {})
    0

    An `sql` value puts an expression on the right-hand side, so a column
    that must keep its FIRST value can say so:

    >>> _ = apply_update(conn, "companies", "id", cid,
    ...                  {"notes": sql("COALESCE(notes, ?)", "first")})
    >>> _ = apply_update(conn, "companies", "id", cid,
    ...                  {"notes": sql("COALESCE(notes, ?)", "second")})
    >>> conn.execute("SELECT notes FROM companies").fetchone()["notes"]
    'first'

    Notes:
        Commits through `_commit`, so a caller inside a batch() block joins
        that transaction instead of ending it early.
    """
    sets: list[str] = []
    args: list[Any] = []
    for col, val in fields.items():
        if isinstance(val, sql):
            sets.append(f"{col}={val.expr}")
            args.extend(val.args)
        else:
            sets.append(f"{col}=?")
            args.append(val)
    if not sets:
        return 0
    cur = conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE {id_col}=?",
                       [*args, id_val])
    _commit(conn)
    return cur.rowcount


#: Held for the duration of every batch() block in this process.
#
# SQLite grants exactly one writer at a time, so eleven harvest threads
# racing for the file lock were never writing in parallel -- they were
# queueing, in a queue implemented as a busy-wait with a 30-second
# deadline. That is fine until the machine stalls: on 2026-09-11 the box
# was saturated (HTTP throughput in the same minute fell from 493 GETs a
# minute to 35), eighteen boards waited out the full BUSY_TIMEOUT_S, and
# each one threw away its whole fetched snapshot -- 12,674 postings
# re-fetched on the next pass.
#
# A lock turns the same queue into a FIFO with no deadline. Throughput is
# unchanged, because the writes were serial either way; what goes away is
# the deadline, and with it the only way an in-process writer can lose
# work it already paid for. BUSY_TIMEOUT_S now means what it should: the
# wait for a writer in ANOTHER process (the web UI, a backup).
_WRITE_LOCK = threading.Lock()


class batch:
    """Group many upsert_job / sync_job_statuses calls into ONE transaction.

    >>> from src.store import upsert_job, job_exists
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

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        _WRITE_LOCK.acquire()
        _BATCHING.add(id(self.conn))
        return self.conn

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> Literal[False]:
        _BATCHING.discard(id(self.conn))
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            _WRITE_LOCK.release()
        return False


class Writer:
    """`async with Writer(path) as db`: a store connection (`connect(path)`)
    on a thread of its own for the block; given an open connection
    instead, that one, left open.

    `await db.run(fn, ...)` is `fn(conn, ...)` on that thread, in a copy
    of the caller's context (its run, src/runstate.py), and `await
    db.batch(fn, ...)` the same inside one `batch` transaction. The calls
    run one at a time, in the order asked. A caller cancelled while its
    call waits never starts it; one cancelled while its batch runs has the
    batch rolled back.

    >>> import asyncio
    >>> from src.store import job_exists, upsert_job
    >>> async def demo():
    ...     async with Writer(":memory:") as db:
    ...         await db.batch(upsert_job, {"job_id": "w1", "title": "T"})
    ...         return await db.run(job_exists, "w1")
    >>> asyncio.run(demo())
    True

    Notes:
        The store for async code: each sqlite3 call blocks, so a coroutine
        never holds a connection. A pass's writes queue here rather than
        on the busy timeout; other processes keep their own connections.
    """

    #: The block's call queue and its drainer, made as the block opens.
    _queue: asyncio.Queue[tuple[asyncio.Future[Any], Callable[..., Any],
                                Callable[[], Any]] | None]
    _drainer: asyncio.Task[None]

    def __init__(self, path: str | Path | sqlite3.Connection | None = None) -> None:
        self.path = path
        self._thread = ThreadPoolExecutor(1, thread_name_prefix="store")
        self._conn = path if isinstance(path, sqlite3.Connection) else None

    async def __aenter__(self) -> Writer:
        loop = asyncio.get_running_loop()
        try:
            if self._conn is None:
                self._conn = await loop.run_in_executor(     # a path: not a Connection
                    self._thread, connect, cast("str | Path | None", self.path))
        except BaseException:
            self._thread.shutdown(wait=False)
            raise
        self._queue = asyncio.Queue()
        self._drainer = asyncio.create_task(self._drain())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._queue.put_nowait(None)
        try:
            await self._drainer
            if self._conn is not self.path:
                await asyncio.get_running_loop().run_in_executor(
                    self._thread, cast("sqlite3.Connection", self._conn).close)
        finally:
            self._thread.shutdown(wait=False)

    def run[**P, T](self, fn: Callable[Concatenate[sqlite3.Connection, P], T],
                    *args: P.args, **kw: P.kwargs) -> Coroutine[Any, Any, T]:
        """`fn(conn, *args, **kw)` on the store's thread (a coroutine)."""
        return self._ask(False, fn, args, kw)

    def batch[**P, T](self, fn: Callable[Concatenate[sqlite3.Connection, P], T],
                      *args: P.args, **kw: P.kwargs) -> Coroutine[Any, Any, T]:
        """`run`, inside one `batch` transaction."""
        return self._ask(True, fn, args, kw)

    async def _ask[T](self, whole: bool, fn: Callable[..., T], args: tuple[Any, ...],
                      kw: dict[str, Any]) -> T:
        asked: asyncio.Future[T] = asyncio.get_running_loop().create_future()

        def call() -> T:
            if not whole:
                return fn(self._conn, *args, **kw)
            with batch(cast(sqlite3.Connection, self._conn)):     # open in the block
                got = fn(self._conn, *args, **kw)
                # A read of the asker's state, no more: its cancel lands
                # before the commit (rolled back) or after it (kept).
                if asked.cancelled():
                    raise RuntimeError("its caller was cancelled: rolled back")
            return got
        self._queue.put_nowait((asked, contextvars.copy_context().run, call))
        return await asked

    async def _drain(self) -> None:
        """Run the queued calls on the store's thread, one at a time, until
        the None that ends the block."""
        loop = asyncio.get_running_loop()
        while (item := await self._queue.get()) is not None:
            asked, within, call = item
            if asked.cancelled():
                continue
            try:
                got = await loop.run_in_executor(self._thread, within, call)
            except Exception as e:
                if not asked.done():
                    asked.set_exception(e)
            else:
                if not asked.done():
                    asked.set_result(got)


def dedup_groups(conn: sqlite3.Connection, table: str, id_col: str,
                 groups: Mapping[Any, list[dict[str, Any]]],
                 rank: Callable[[dict[str, Any]], Any],
                 describe: Callable[[dict[str, Any], list[dict[str, Any]]], str],
                 merge: Callable[[dict[str, Any], list[dict[str, Any]]], object] | None = None
                 ) -> int:
    """Keep one row per group, delete the rest, say so. Returns rows deleted.

    The two dedup passes (companies that turned out to share a board, jobs
    that turned out to be one posting) disagree about everything that
    matters -- how rows group, which survivor wins, what has to move off a
    loser before it goes -- and agreed, line for line, about the part that
    does not: skip the singletons, sort, split keep/losers, delete by id,
    count, print one line. That skeleton lives here; the judgment stays
    with each caller.

    `groups` maps any key to a list of row dicts. `rank` is a sort key
    where the SURVIVOR sorts FIRST. `describe(keep, losers)` returns the
    line to print under the pass's own indent. `merge(keep, losers)`, when
    given, runs BEFORE the delete -- the hook for carrying a loser's rows
    or tags over to the survivor.

    >>> conn = connect(":memory:")
    >>> for n in ("Acme", "Acme Inc", "Solo"):
    ...     _ = conn.execute("INSERT INTO companies (name) VALUES (?)", (n,))
    >>> rows = [dict(r) for r in conn.execute("SELECT id, name FROM companies")]
    >>> groups = {"acme": rows[:2], "solo": rows[2:]}
    >>> dedup_groups(conn, "companies", "id", groups,
    ...              rank=lambda r: len(r["name"]),
    ...              describe=lambda k, l: f"{k['name']} <- {len(l)}")
        Acme <- 1
    1
    >>> [r["name"] for r in conn.execute("SELECT name FROM companies "
    ...                                  "ORDER BY name")]
    ['Acme', 'Solo']

    Notes:
        Commits through `_commit`, like every other writer here.
    """
    deleted = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=rank)
        keep, losers = members[0], members[1:]
        if merge is not None:
            merge(keep, losers)
        conn.executemany(f"DELETE FROM {table} WHERE {id_col}=?",
                         [(l[id_col],) for l in losers])
        deleted += len(losers)
        print(f"    {describe(keep, losers)}")
    _commit(conn)
    return deleted
