"""Mirror everything a run prints into a per-session log file.

The crawler talks entirely through print(): sources tried, misses, scores,
digests, tracebacks. That output used to vanish with the console window.
`start()` tees sys.stdout and sys.stderr into
``<DATA_DIR>/logs/session-<stamp>-<mode>.log`` so a later reader — you, or
Claude reviewing whether a change actually improved a run — can pull up the
exact output of any past session.

Notes:
    run_scraper.main() calls start() right after argument parsing, so the
    whole session is captured including the final traceback of a crash
    (Python prints it to stderr before atexit handlers run). The offline
    test suite never calls start(); tests/test_session_log.py exercises it
    against a tmp directory instead of your real data dir.
"""

import atexit
import sys
import time
from datetime import datetime

import config

# How many session logs to keep. A daily crawl plus ad-hoc maintenance runs
# stays under this for roughly a month of history.
KEEP = 60

# Flags that tune or scope a run rather than naming what kind of run it is.
# A session whose only flags are these is the daily crawl.
_MODIFIERS = {
    "track", "preview", "no-fit", "send", "no-websearch", "confirm-cost",
    "samples", "top", "workers", "no-verify", "stale-days", "limit",
    "described-only", "why", "db",
}

# Finishers for session logs opened by start(); finish() drains it.
_active = []


def _log_dir():
    """Where session logs live.

    Notes:
        A function rather than a constant so the offline test suite can
        point it at a tmp directory (tests/conftest.py does, autouse) —
        DATA_DIR itself is resolved once at config import.
    """
    return config.DATA_DIR / "logs"


def log_basename(argv, now):
    """The log file name for a session: timestamp plus the command run.

    The first flag that is not a scope/tuning modifier names the session:

    >>> t = datetime(2026, 8, 28, 9, 30, 0)
    >>> log_basename(["--sync-status", "--track", "local"], t)
    'session-20260828-093000-sync-status.log'
    >>> log_basename(["--mark", "applied", "1234"], t)
    'session-20260828-093000-mark.log'

    A run with only modifiers (or no flags at all) is the daily crawl:

    >>> log_basename(["--track", "local", "--preview"], t)
    'session-20260828-093000-crawl.log'
    >>> log_basename([], t)
    'session-20260828-093000-crawl.log'
    """
    return f"session-{now:%Y%m%d-%H%M%S}-{_clean(_mode(argv))}.log"


def _mode(argv):
    """The first flag that isn't a scope/tuning modifier, else 'crawl'.

    >>> _mode(["--rescore", "--limit", "20"])
    'rescore'
    >>> _mode(["--track", "remote"])
    'crawl'
    """
    for tok in argv:
        if tok.startswith("--") and tok[2:] not in _MODIFIERS:
            return tok[2:]
    return "crawl"


def _clean(mode):
    """`mode` reduced to filename-safe characters.

    >>> _clean("sync-status")
    'sync-status'
    >>> _clean("weird/../mode")
    'weirdmode'
    >>> _clean("")
    'crawl'
    """
    return "".join(c for c in mode if c.isalnum() or c == "-") or "crawl"


class _Tee:
    """Writes to the original stream and the log file; reads everything
    else (isatty, encoding, ...) from the original stream.

    Notes:
        The sink write is wrapped so a closed log file (interpreter
        teardown, double finish) can never break console output.
    """

    def __init__(self, stream, sink):
        self._stream = stream
        self._sink = sink

    def write(self, text):
        n = self._stream.write(text)
        try:
            self._sink.write(text)
        except ValueError:
            pass
        return n

    def flush(self):
        self._stream.flush()
        try:
            self._sink.flush()
        except ValueError:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _prune(log_dir, keep=KEEP):
    """Delete the oldest session logs so that after the new file is
    created at most `keep` remain.

    Notes:
        Timestamped names sort chronologically, so lexicographic order is
        age order.
    """
    logs = sorted(log_dir.glob("session-*.log"))
    for old in logs[:-(keep - 1)] if len(logs) >= keep else []:
        try:
            old.unlink()
        except OSError:
            pass


def open_log(mode, invocation, now=None):
    """Create a fresh session log with the standard header; returns
    (path, open file). The caller owns writing to and closing the file —
    start() below does both for CLI runs, webapp/ops.py streams its
    browser-tee'd output into one per UI operation.

    Notes:
        Also prunes the log directory down to KEEP files, so every entry
        point that opens a log enforces retention. `now` exists for tests.
    """
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    _prune(log_dir)

    now = now or datetime.now()
    path = log_dir / f"session-{now:%Y%m%d-%H%M%S}-{_clean(mode)}.log"
    fh = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
    fh.write(f"# job crawler session\n"
             f"# started : {now:%Y-%m-%d %H:%M:%S}\n"
             f"# run     : {invocation}\n\n")
    return path, fh


def footer(fh, t0):
    """Stamp the closing timestamp + elapsed seconds and close `fh`.
    Safe on an already-closed file.
    """
    try:
        fh.write(f"\n# ended   : {datetime.now():%Y-%m-%d %H:%M:%S}"
                 f"  ({time.monotonic() - t0:.0f}s)\n")
        fh.close()
    except ValueError:
        pass


def start(argv, now=None):
    """Begin mirroring stdout/stderr into a new session log; returns its
    path. finish() (registered atexit) restores the streams and stamps a
    footer with the elapsed time.

    Notes:
        argv is the CLI argument list *without* the program name, exactly
        what run_scraper.main() received. `now` exists for tests.
    """
    path, fh = open_log(_mode(argv), "run_scraper.py " + " ".join(argv), now)

    out, err = sys.stdout, sys.stderr
    sys.stdout = _Tee(out, fh)
    sys.stderr = _Tee(err, fh)
    t0 = time.monotonic()

    def _finish():
        sys.stdout, sys.stderr = out, err
        footer(fh, t0)

    _active.append(_finish)
    atexit.register(finish)
    return path


def finish():
    """Close every session log opened by start(), restoring the original
    streams. Safe to call more than once.
    """
    while _active:
        _active.pop()()
