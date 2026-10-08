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
import re
import sqlite3
import threading
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Concatenate, Literal, cast

from src import config
from src.match.names import name_key
from src.rows import JobRow
from .migrate import migrate

# How long a writer waits on another process's write lock before giving up.
# The web UI, the scheduled crawl and the background harvester all write to
# one file, so a locked DB is normal, not an error.
BUSY_TIMEOUT_S = 30.0


# Python functions the store's SQL calls by name (norm_title, name_key, ...).
# A module declares one with @sql_function beside the Python rule it wraps,
# so a rule lives in one place whether a row loop or a query applies it, and
# connect() installs every one on each new connection.
type SqlScalar = str | int | float | None
type SqlFn = Callable[..., SqlScalar | bytes]
SQL_FUNCTIONS: dict[str, tuple[int, SqlFn]] = {}


def sql_function[F: SqlFn](name: str, narg: int) -> Callable[[F], F]:
    """Register `fn` as SQL function `name` (see SQL_FUNCTIONS); returns `fn`
    unchanged, so Python callers keep using it directly."""
    def register(fn: F) -> F:
        SQL_FUNCTIONS[name] = (narg, fn)
        return fn
    return register


sql_function("name_key", 1)(name_key)


@sql_function("norm_url", 1)
def _norm_url(u: str | None) -> str:
    """Scheme/query/fragment/trailing-slash-insensitive URL key."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")


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

    A thread may use the connection, one at a time: a Writer adopts one
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
    migrate(conn)
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

    def __init__(self, expr: str, *args: object) -> None:
        self.expr, self.args = expr, args


def apply_update(conn: sqlite3.Connection, table: str, id_col: str, id_val: object,
                 fields: Mapping[str, object]) -> int:
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
    args: list[object] = []
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
    _queue: asyncio.Queue[tuple[asyncio.Future[object], Callable[..., object],
                                Callable[[], object]] | None]
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
                    *args: P.args, **kw: P.kwargs) -> Coroutine[None, None, T]:
        """`fn(conn, *args, **kw)` on the store's thread (a coroutine)."""
        return self._ask(False, fn, args, kw)

    def batch[**P, T](self, fn: Callable[Concatenate[sqlite3.Connection, P], T],
                      *args: P.args, **kw: P.kwargs) -> Coroutine[None, None, T]:
        """`run`, inside one `batch` transaction."""
        return self._ask(True, fn, args, kw)

    async def _ask[T](self, whole: bool, fn: Callable[..., T], args: tuple[object, ...],
                      kw: dict[str, object]) -> T:
        asked: asyncio.Future[object] = asyncio.get_running_loop().create_future()

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
        return cast(T, await asked)

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


def as_job(row: sqlite3.Row) -> JobRow:
    """A `SELECT *` jobs row as a JobRow: every column, which the total
    type promises."""
    return cast(JobRow, dict(row))


