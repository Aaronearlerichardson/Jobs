"""The web UI's operation runner: one op at a time, each a task, the rest
waiting in a FIFO run queue, the console tee'd to the browser
(/api/run/status polling); and the web UI's view of the shared operation
table in src/dispatch/registry.py.

The runner lives on one event loop (the web UI's, src/web/server.py) and
changes only there, between two awaits: a request thread asks through
that loop (`submit`, `stop`, `status`, `queue_remove`, `queue_clear` are
coroutines), a finishing op hands the slot on in the same step that reads
the queue, and a line another thread prints reaches the op's log through
the loop too. Two requests cannot both claim the slot, and a request
cannot land between an op's last look at the queue and its release of the
slot. Those were the thread runner's two bugs: a double-claimed slot, and
a queue that stopped draining.
"""

from __future__ import annotations

import asyncio
import functools
import io
import secrets
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import NotRequired, TextIO, TypedDict, override

from src import config, runstate
from src import session_log
from src.claude import api as claude_api
from src.dispatch import registry

class _Task(TypedDict):
    """The one running op's slot."""
    name: str | None
    task: asyncio.Task[None] | None
    log: list[str]
    log_offset: int
    started: str | None
    ended: str | None
    error: str | None
    stopped: bool
    active: bool


class _Entry(TypedDict):
    name: str
    fn: Callable[[], Awaitable[object]]


class _Queued(_Entry):
    """An op waiting for the slot."""
    id: str
    params: dict[str, object]
    key: str
    enqueued_at: str


class _Status(TypedDict):
    """`status`: the runner as the browser polls it."""
    running: bool
    name: str | None
    started: str | None
    ended: str | None
    error: str | None
    stopped: bool
    lines: list[str]
    total: int
    queue: list[_EntryJson]


class _EntryJson(TypedDict):
    """A queue entry as the browser sees it."""
    id: str
    name: str
    position: int
    enqueued_at: str
    params: dict[str, object]
    duplicate: NotRequired[bool]


TASK: _Task = {"name": None, "task": None, "log": [], "log_offset": 0,
               "started": None, "ended": None, "error": None, "stopped": False,
               "active": False}


def _log_lines(lines: list[str]) -> None:
    """Add `lines` to the op's log, its head trimmed past 5000 lines (see
    _Tee). On the loop."""
    log = TASK["log"]
    log.extend(lines)
    while len(log) > 5000:
        del log[:1000]
        TASK["log_offset"] += 1000


class _Tee(io.TextIOBase):
    """stdout/stderr tee: the real stream keeps printing; the browser polls
    the copy, and an optional `sink` SessionLog (see src/session_log.py)
    gets a third copy as it streams — mirrored there as timestamped,
    levelled records — so UI-triggered runs are reviewable after the fact
    just like CLI ones. Swapped in globally while an operation runs so the
    prints of its store thread and worker threads are captured too: their
    whole lines (session_log.whole_lines) reach the log through `loop`.

    `err=True` (the stderr tee) records its lines at ERROR, as
    session_log.start does for a CLI run.

    The browser tracks a cursor into the log (`since=<n>` on
    /api/run/status) so a poll only re-sends lines it hasn't seen yet. That
    cursor has to be an ABSOLUTE line count — total lines ever appended —
    not a raw index into TASK["log"], because the list below gets its head
    chopped off once it grows past 5000 lines. A raw-index cursor goes stale
    the instant a trim shifts every surviving line down by 1000: the same
    index now names an earlier line, and the client re-renders lines it
    already showed (the browser-side duplication bug this class exists to
    avoid). log_offset counts lines permanently dropped by trimming, so
    `status` can translate an absolute cursor back into a list index
    (`since - log_offset`) that stays correct across trims."""

    def __init__(self, orig: TextIO, loop: asyncio.AbstractEventLoop,
                 sink: session_log.SessionLog | None = None, err: bool = False) -> None:
        self.orig = orig
        self.loop = loop
        self.sink = sink
        self._err = err

    @override
    def write(self, s: str) -> int:
        try:
            self.orig.write(s)
        except Exception:
            pass
        if self.sink is not None:
            try:
                self.sink.feed(s, err=self._err)
            except Exception:
                pass
        lines = session_log.whole_lines(self, s)
        if lines:
            try:
                on_loop = asyncio.get_running_loop() is self.loop
            except RuntimeError:
                on_loop = False
            try:
                if on_loop:
                    _log_lines(lines)
                else:
                    self.loop.call_soon_threadsafe(_log_lines, lines)
            except RuntimeError:            # the loop has closed (exit)
                pass
        return len(s)

    @override
    def flush(self) -> None:
        try:
            self.orig.flush()
        except Exception:
            pass


# Pristine keyword lists, captured at import — BEFORE any op runs. Every
# track runs in THIS process: extend-mode tracks grow the shared lists,
# replace-mode tracks swap them — without a reset between ops, running one
# track's crawl would poison the next one's keyword filter. Restored in
# place (slice assignment) so modules that bound the list objects at import
# time (src/match/filters.py) see the reset.
_BASELINE_KW = config.keyword_snapshot()


def _start(entry: _Entry) -> None:
    """Claim the slot for `entry` and start its op's task. On the loop."""
    TASK.update({"name": entry["name"], "error": None, "ended": None, "stopped": False,
                 "started": datetime.now().isoformat(), "active": True,
                 "log": [], "log_offset": 0})
    task = TASK["task"] = asyncio.get_running_loop().create_task(
        _run(entry["name"], entry["fn"]))
    task.add_done_callback(_hand_off)


def _hand_off(task: asyncio.Task[None]) -> None:
    """The op task's done callback: the slot to the next queued entry, or
    freed. On the loop, so nothing can queue between the look and the
    release; a callback, so a task cancelled before its first step (its
    body never runs) hands off too. The ended task is let go: its context
    (and a stopped op's traceback) would outlive the op."""
    TASK["ended"] = datetime.now().isoformat()
    TASK["stopped"] |= task.cancelled()
    TASK["task"] = None
    entry = QUEUE.popleft() if QUEUE else None
    TASK["active"] = entry is not None
    if entry is not None:
        _start(entry)


async def _run(name: str, fn: Callable[[], Awaitable[object]]) -> None:
    """One op, a run of its own (src/runstate.py: fresh memos, a re-armed
    Claude breaker): `await fn()` with the console tee'd into its log (and
    a session log of its own). An exception is the op's error; a cancel
    (`stop`) marks it stopped."""
    async with runstate.Run():
        orig_out, orig_err = sys.stdout, sys.stderr
        slog: session_log.SessionLog | None
        try:
            slog = session_log.open_log(f"webui-{name}", f"web UI op {name!r}")
        except OSError:
            slog = None              # a full/read-only disk can't block the op
        loop = asyncio.get_running_loop()
        tee_out = _Tee(orig_out, loop, sink=slog)
        tee_err = _Tee(orig_err, loop, sink=slog, err=True)
        sys.stdout, sys.stderr = tee_out, tee_err
        try:
            config.restore_keywords(_BASELINE_KW)
            await fn()
        except Exception as e:
            TASK["error"] = f"{type(e).__name__}: {e}"
            # stderr: the session log records it at ERROR, not WARNING; the
            # browser shows it like any print.
            print(f"  [!] operation failed: {TASK['error']}", file=sys.stderr)
        finally:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                TASK["stopped"] = True
                print("  [!] operation stopped", file=sys.stderr)
            # This op's own Claude spend, while its log is still open.
            claude_api.report_cache_stats()
            if slog is not None:
                slog.close()
            # Only unwind our own layer. Blindly assigning `orig` back would
            # restore a stale stream if anything else swapped stdout while
            # we ran, permanently leaving a tee installed that copies every
            # later print into the op log.
            if sys.stdout is tee_out:
                sys.stdout = orig_out
            if sys.stderr is tee_err:
                sys.stderr = orig_err


# --------------------------------------------------------------------------- #
#  The run queue                                                               #
# --------------------------------------------------------------------------- #
#
# One operation still runs at a time — a crawl and a re-score writing the same
# SQLite store concurrently is exactly what the single slot exists to prevent.
# What used to be lost was the REQUEST: pressing a second button while a crawl
# ran returned 409 and the person had to sit and watch for the run to end
# before they could ask for the next one. The queue keeps the ask. Entries are
# FIFO, carry the params they were submitted with (so a duplicate press is
# recognisable), and are handed the slot by the finishing op itself.
# Nothing here survives a restart: the queue is in memory, and a config save
# relaunches the process (src/web/server.py schedule_restart), which is why
# routes.py refuses to save while entries are waiting.
QUEUE: deque[_Queued] = deque()


def _entry_json(entry: _Queued, position: int,
                duplicate: bool | None = None) -> _EntryJson:
    """One queue entry as the browser sees it — everything but the callable."""
    d: _EntryJson = {"id": entry["id"], "name": entry["name"], "position": position,
                         "enqueued_at": entry["enqueued_at"], "params": entry["params"]}
    if duplicate is not None:
        d["duplicate"] = duplicate
    return d


async def submit(name: str, args: registry.OpParams,
                 fn: Callable[[], Awaitable[object]]) -> _EntryJson | None:
    """Run `fn()` (a coroutine) now if the slot is free, else put it in
    the run queue.

    `args` is the op's validated params (a registry.OpParams). Returns None
    when the op started, otherwise the queue entry it added, or the
    identical one already waiting, marked `duplicate`: asking twice for a
    run whose params validate to the same values ("5" and 5, a blank field
    and its default) is a double click, not a second job. Queueing the op
    that is CURRENTLY running is allowed on purpose: a crawl re-run after a
    config change is a real request.
    """
    # `or QUEUE` keeps the order honest: while anything is waiting, a new
    # request goes to the back.
    if not (TASK["active"] or QUEUE):
        _start({"name": name, "fn": fn})
        return None
    key = args.model_dump_json()
    for i, e in enumerate(QUEUE):
        if e["name"] == name and e["key"] == key:
            return _entry_json(e, i + 1, duplicate=True)
    entry: _Queued = {"id": secrets.token_hex(4), "name": name,
             "params": args.model_dump(mode="json"), "key": key,
             "enqueued_at": datetime.now().isoformat(), "fn": fn}
    QUEUE.append(entry)
    return _entry_json(entry, len(QUEUE), duplicate=False)


async def stop() -> bool:
    """Cancel the running op (its open store batch rolls back), once; True
    when one was running. The queue is left as it is: the next entry
    starts once the op has unwound."""
    task = TASK["task"]
    if not TASK["active"] or task is None or task.done():
        return False
    if not task.cancelling():
        task.cancel()
    return True


async def status(since: int = 0) -> _Status:
    """The runner as the browser polls it: whether an op runs, its name,
    times, error and `stopped`, the log lines past the absolute cursor
    `since` (see _Tee) with the new `total`, and the waiting queue."""
    offset = TASK["log_offset"]
    return {"running": TASK["active"], "name": TASK["name"],
            "started": TASK["started"], "ended": TASK["ended"],
            "error": TASK["error"], "stopped": TASK["stopped"],
            "lines": TASK["log"][max(0, since - offset):],
            "total": offset + len(TASK["log"]),
            "queue": [_entry_json(e, i + 1) for i, e in enumerate(QUEUE)]}


async def queue_remove(entry_id: str) -> bool:
    """Drop one WAITING entry. False if it is unknown — which includes the
    entry that has just been handed the runner slot (`stop` ends that)."""
    for i, e in enumerate(QUEUE):
        if e["id"] == entry_id:
            del QUEUE[i]
            return True
    return False


async def queue_clear() -> int:
    """Drop every waiting entry; returns how many there were. Whatever is
    already running keeps running."""
    n = len(QUEUE)
    QUEUE.clear()
    return n


# The operations, from the ONE registry shared with the CLIs, in the
# {label, engine, params, fn} shape routes.py and the tests read: `engine`
# is the crawl engine the op needs ("local" = the location-scoped crawler,
# "sweep" = the location-agnostic one, src/crawl/runner.py; None = any
# track), matched against the active track's profile-configured engine and
# never against a user-chosen track id. `params` is the op's model, which
# api_run validates the POSTed JSON with; `fn(args)` is the op's coroutine
# through registry.invoke with those validated args.
class OpEntry(registry.OpSpec):
    """One web operation: see the comment above."""
    fn: Callable[[registry.OpParams], Awaitable[object]]


OPS: dict[str, OpEntry] = {
    name: {"label": e["label"], "engine": e["engine"], "params": e["params"],
           "fn": functools.partial(registry.invoke, name)}
    for name, e in registry.ui_ops().items()
}
