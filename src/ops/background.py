"""Background operation runner (one op at a time, the rest waiting in a FIFO
run queue, console tee'd to the browser via /api/run/status polling), and the
web UI's view of the shared operation table in src/ops/registry.py."""

import functools
import io
import json
import secrets
import sys
import threading
from datetime import datetime

from src import config
from src import session_log
from src.claude import api as claude_api
from src.ops import registry

TASK = {"name": None, "thread": None, "log": [], "log_offset": 0,
        "started": None, "ended": None, "error": None, "active": False}
_LOG_LOCK = threading.Lock()
# Guards the claim on TASK. Separate from _LOG_LOCK, which the Tee takes on
# every write — holding one while waiting on the other would deadlock.
_TASK_LOCK = threading.Lock()


class _Tee(io.TextIOBase):
    """stdout/stderr tee: the real stream keeps printing; the browser polls
    the copy, and an optional `sink` SessionLog (see src/session_log.py)
    gets a third copy as it streams — mirrored there as timestamped,
    levelled records — so UI-triggered runs are reviewable after the fact
    just like CLI ones. Swapped in globally while an operation runs so the
    crawl's many worker-thread print()s are captured too.

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
    routes.py can translate an absolute cursor back into a list index
    (`since - log_offset`) that stays correct across trims."""

    def __init__(self, orig, sink=None, err=False):
        self.orig = orig
        self.sink = sink
        self._err = err
        # Partial lines keyed by writing thread: print() issues separate
        # text/newline writes, and one shared buffer let a fetch worker's
        # line fuse into the middle of a progress line in the browser log
        # (and the session log — its SessionLog sink assembles per-thread
        # the same way).
        self._bufs = {}

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        if self.sink is not None:
            try:
                self.sink.feed(s, err=self._err)
            except Exception:
                pass
        key = threading.get_ident()
        with _LOG_LOCK:
            buf = self._bufs.get(key, "") + s
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                TASK["log"].append(line)
                if len(TASK["log"]) > 5000:
                    del TASK["log"][:1000]
                    TASK["log_offset"] += 1000
            if buf:
                self._bufs[key] = buf
            else:
                self._bufs.pop(key, None)
        return len(s)

    def flush(self):
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


def _restore_keywords():
    """Reset config's shared keyword lists (in place, so modules holding
    references see it) to their import-time state."""
    config.restore_keywords(_BASELINE_KW)


def _claim_locked(name):
    """Take the single runner slot for `name`. _TASK_LOCK must be held.

    The claim is taken under a lock rather than left to the caller's
    `_running()` check. Two /api/run requests arriving together could both
    pass that check before either set TASK["thread"], and the second op then
    ran concurrently with the first — doing the whole crawl twice and, worse,
    nesting the stdout tee: each layer appends to TASK["log"], so every line
    landed in the browser log once per layer.
    """
    TASK["active"] = True              # claim before the thread exists —
    TASK["thread"] = None              # an unstarted thread isn't yet alive
    TASK.update(name=name, error=None, ended=None,
                started=datetime.now().isoformat())


def _launch(name, fn):
    """Start the worker thread for a slot that is ALREADY claimed.

    Never call this while holding _TASK_LOCK: it takes _LOG_LOCK to clear
    the log, and the thread it starts takes _TASK_LOCK itself as it
    finishes.
    """
    def worker():
        orig_out, orig_err = sys.stdout, sys.stderr
        try:
            slog = session_log.open_log(f"webui-{name}",
                                        f"web UI op {name!r}")
        except OSError:
            slog = None              # a full/read-only disk can't block the op
        tee_out = _Tee(orig_out, sink=slog)
        tee_err = _Tee(orig_err, sink=slog, err=True)
        sys.stdout, sys.stderr = tee_out, tee_err
        claude_baseline = claude_api.cache_stats()
        try:
            # Re-arm the unrecoverable-API-error breaker: it is process-
            # lifetime and this server process outlives many operations
            # (see src.claude.reset_breaker).
            claude_api.reset_breaker()
            _restore_keywords()
            fn()
        except Exception as e:
            TASK["error"] = f"{type(e).__name__}: {e}"
            # stderr: the session log records it at ERROR, not WARNING;
            # the browser shows it like any print.
            print(f"  [!] operation failed: {TASK['error']}", file=sys.stderr)
        finally:
            # This op's own Claude spend, while its log is still open: the
            # server process never reaches the atexit footer.
            claude_api.report_cache_stats(claude_baseline)
            if slog is not None:
                slog.close()
            # Only unwind our own layer. Blindly assigning `orig` back would
            # restore a stale stream if anything else swapped stdout while we
            # ran, permanently leaving a tee installed that copies every later
            # print into the op log.
            if sys.stdout is tee_out:
                sys.stdout = orig_out
            if sys.stderr is tee_err:
                sys.stderr = orig_err
            TASK["ended"] = datetime.now().isoformat()
            # Chain the run queue from HERE, in the finishing thread, and
            # only now: stdout is back, so the next op's tee wraps the real
            # console instead of ours, and this op's session log is closed,
            # so the next run gets a file of its own. _hand_off returns with
            # the slot re-claimed but the lock released — starting a thread
            # inside that lock region would have the new worker contend with
            # the very block still mutating TASK for the op it belongs to.
            # A thread the OS refuses to start must not strand everything
            # behind it, so the loop moves on to the entry after it.
            nxt = _hand_off()
            while nxt is not None:
                try:
                    _launch(nxt["name"], nxt["fn"])
                    break
                except Exception:
                    nxt = _hand_off()

    with _LOG_LOCK:
        TASK["log"].clear()
        TASK["log_offset"] = 0
    t = threading.Thread(target=worker, daemon=True)
    TASK["thread"] = t
    try:
        t.start()
    except Exception:
        with _TASK_LOCK:
            TASK["active"] = False
        raise


def _run_op(name, fn):
    """Start `fn` on a worker thread, jumping straight past the run queue.
    Returns False if an operation is already running."""
    with _TASK_LOCK:
        if _running():
            return False
        _claim_locked(name)
    _launch(name, fn)
    return True


def _running():
    if TASK["active"]:
        return True
    t = TASK["thread"]
    return bool(t and t.is_alive())


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
# recognisable), and are handed the slot by the finishing worker itself.
# Nothing here survives a restart: the queue is in memory, and a config save
# relaunches the process (src/web/server.py schedule_restart), which is why
# routes.py refuses to save while entries are waiting.
QUEUE = []


def _params_key(params):
    """Canonical text for one run's params, so two requests that mean the
    same thing compare equal.

    Keys are sorted and None values dropped: the browser posts a whole form,
    so the same button pressed twice can differ only in key order or in a
    field left blank, and those two must not take two places in the queue.

    >>> a, b = {"pages": 2, "track": "x"}, {"track": "x", "pages": 2}
    >>> _params_key(a) == _params_key(b)
    True
    >>> _params_key({"track": "x", "limit": None})
    '{"track": "x"}'
    """
    return json.dumps({k: v for k, v in params.items() if v is not None},
                      sort_keys=True, default=str)


def _entry_json(entry, position, duplicate=None):
    """One queue entry as the browser sees it — everything but the callable."""
    d = {"id": entry["id"], "name": entry["name"], "position": position,
         "enqueued_at": entry["enqueued_at"], "params": entry["params"]}
    if duplicate is not None:
        d["duplicate"] = duplicate
    return d


def _hand_off():
    """Release the runner slot and re-claim it for the next queued entry in
    one lock hold, so a request landing in between cannot jump the queue.

    Returns the entry — which the caller must then `_launch`, outside the
    lock — or None when nothing is waiting.
    """
    with _TASK_LOCK:
        TASK["active"] = False
        if not QUEUE:
            return None
        entry = QUEUE.pop(0)
        _claim_locked(entry["name"])
        return entry


def submit(name, params, fn):
    """Run `fn` now if the slot is free, else put it in the run queue.

    Returns None when the op started, otherwise the queue entry it added —
    or the identical one already waiting, marked `duplicate`, since asking
    twice for the same run with the same params is a double click, not a
    second job. Queueing the op that is CURRENTLY running is allowed on
    purpose: a crawl re-run after a config change is a real request.
    """
    key = _params_key(params)
    with _TASK_LOCK:
        # `or QUEUE` keeps the order honest: while anything is waiting, a new
        # request goes to the back even if the runner is momentarily free.
        #
        # The busy test is the claim flag, NOT _running(): _running() also
        # counts the finishing worker's thread, which stays alive for a few
        # microseconds after its `finally` called _hand_off() for the last
        # time. An entry queued in THAT window is never drained -- the worker
        # has already looked at the queue for the last time and is on its way
        # out -- and because every later submit() then sees a non-empty QUEUE
        # it queues behind the stranded entry too, so the run queue wedges
        # permanently (and a config save, refused while the queue is
        # non-empty, wedges with it). TASK["active"] is the authoritative
        # claim: set before the thread exists, cleared only by _hand_off or a
        # failed thread start. Claiming the slot in that window is safe --
        # the outgoing worker restored stdout and closed its session log
        # before handing off.
        if TASK["active"] or QUEUE:
            for i, e in enumerate(QUEUE):
                if e["name"] == name and e["key"] == key:
                    return _entry_json(e, i + 1, duplicate=True)
            entry = {"id": secrets.token_hex(4), "name": name,
                     "params": dict(params), "key": key,
                     "enqueued_at": datetime.now().isoformat(), "fn": fn}
            QUEUE.append(entry)
            return _entry_json(entry, len(QUEUE), duplicate=False)
        _claim_locked(name)
    _launch(name, fn)
    return None


def queue_snapshot():
    """The waiting entries, oldest first, without their callables."""
    with _TASK_LOCK:
        return [_entry_json(e, i + 1) for i, e in enumerate(QUEUE)]


def queue_remove(entry_id):
    """Drop one WAITING entry. False if it is unknown — which includes the
    entry that has just been handed the runner slot: a started op is
    cancelled by nothing here."""
    with _TASK_LOCK:
        for i, e in enumerate(QUEUE):
            if e["id"] == entry_id:
                del QUEUE[i]
                return True
    return False


def queue_clear():
    """Drop every waiting entry; returns how many there were. Whatever is
    already running keeps running."""
    with _TASK_LOCK:
        n = len(QUEUE)
        QUEUE.clear()
        return n


def _int(p, key, default=None):
    v = str(p.get(key, "") or "").strip()
    return int(v) if v else default


# The operations, from the ONE registry shared with the CLIs. Each entry
# keeps the {label, engine, fn} shape routes.py and the tests read: `engine`
# is the crawl engine the op needs ("local" = the location-scoped crawler,
# "sweep" = the location-agnostic one — src/crawl/runner.py; None = any
# track), matched against the active track's profile-configured engine and
# never against a user-chosen track id. `fn(params)` takes the JSON the
# browser POSTed (api_run injects params["track"]) and runs the op through
# ops_registry.invoke, which coerces the params and imports the target.
OPS = {
    name: {"label": e["label"], "engine": e["engine"],
           "fn": functools.partial(registry.invoke, name)}
    for name, e in registry.ui_ops().items()
}
