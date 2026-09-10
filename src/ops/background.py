"""Background operation runner (one op at a time, console tee'd to the
browser via /api/run/status polling), and the web UI's view of the shared
operation table in src/ops/registry.py."""

import functools
import io
import sys
import threading
from datetime import datetime

from src import config
from src import session_log
from src.ops import registry
from src.claude import api as claude_api

TASK = {"name": None, "thread": None, "log": [], "log_offset": 0,
        "started": None, "ended": None, "error": None, "active": False}
_LOG_LOCK = threading.Lock()
# Guards the claim on TASK. Separate from _LOG_LOCK, which the Tee takes on
# every write — holding one while waiting on the other would deadlock.
_TASK_LOCK = threading.Lock()


class _Tee(io.TextIOBase):
    """stdout tee: real console keeps printing; the browser polls the copy,
    and an optional `sink` SessionLog (see src/session_log.py) gets a
    third copy as it streams — mirrored there as timestamped, levelled
    records — so UI-triggered runs are reviewable after the fact just like
    CLI ones. Swapped in globally while an operation runs so the crawl's
    many worker-thread print()s are captured too.

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

    def __init__(self, orig, sink=None):
        self.orig = orig
        self.sink = sink
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
                self.sink.write(s)
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
_BASELINE_KW = (list(config.CORE_KEYWORDS), list(config.DOMAIN_KEYWORDS),
                list(config.SKILL_KEYWORDS), list(config.INCLUDE_KEYWORDS),
                bool(getattr(config, "ACCEPT_REMOTE", False)))


def _restore_keywords():
    """Reset config's shared keyword lists (in place, so modules holding
    references see it) to their import-time state."""
    core, dom, skill, inc, accept = _BASELINE_KW
    config.CORE_KEYWORDS[:] = core
    config.DOMAIN_KEYWORDS[:] = dom
    config.SKILL_KEYWORDS[:] = skill
    config.INCLUDE_KEYWORDS[:] = inc
    config.ACCEPT_REMOTE = accept


def _run_op(name, fn):
    """Start `fn` on a worker thread. Returns False if an operation is
    already running.

    The claim is taken under a lock rather than left to the caller's
    `_running()` check. Two /api/run requests arriving together could both
    pass that check before either set TASK["thread"], and the second op then
    ran concurrently with the first — doing the whole crawl twice and, worse,
    nesting the stdout tee: each layer appends to TASK["log"], so every line
    landed in the browser log once per layer.
    """
    def worker():
        orig = sys.stdout
        try:
            slog = session_log.open_log(f"webui-{name}",
                                        f"web UI op {name!r}")
        except OSError:
            slog = None              # a full/read-only disk can't block the op
        tee = _Tee(orig, sink=slog)
        sys.stdout = tee
        try:
            # Re-arm the unrecoverable-API-error breaker: it is process-
            # lifetime and this server process outlives many operations
            # (see src.claude.reset_breaker).
            claude_api.reset_breaker()
            _restore_keywords()
            fn()
        except Exception as e:
            TASK["error"] = f"{type(e).__name__}: {e}"
            print(f"  [!] operation failed: {TASK['error']}")
        finally:
            if slog is not None:
                slog.close()
            # Only unwind our own layer. Blindly assigning `orig` back would
            # restore a stale stream if anything else swapped stdout while we
            # ran, permanently leaving a tee installed that copies every later
            # print into the op log.
            if sys.stdout is tee:
                sys.stdout = orig
            TASK["ended"] = datetime.now().isoformat()
            with _TASK_LOCK:
                TASK["active"] = False

    with _TASK_LOCK:
        if _running():
            return False
        TASK["active"] = True          # claim before the thread exists —
        TASK["thread"] = None          # an unstarted thread isn't yet alive
        TASK.update(name=name, error=None, ended=None,
                    started=datetime.now().isoformat())
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
    return True


def _running():
    if TASK["active"]:
        return True
    t = TASK["thread"]
    return bool(t and t.is_alive())


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
