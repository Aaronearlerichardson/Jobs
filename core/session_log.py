"""Per-session log files: timestamped, levelled records of everything a run
prints — plus DEBUG detail that is logged but never printed.

Every record in the file carries a timestamp, a level and a source name::

    2026-08-28 15:41:22 INFO     console | probing 171 candidate compan(ies)...
    2026-08-28 15:41:24 DEBUG    http    | GET https://boards-api.greenhouse.io/... -> 200 in 0.31s
    2026-08-28 15:41:29 WARNING  console | [!] greenhouse arine: HTTP 404

Two kinds of record end up here:

* **Console mirror** — sys.stdout/sys.stderr are tee'd, so everything the
  crawler prints reaches the console unchanged AND lands in the file as a
  levelled record (logger ``console``; ``[!]`` lines as WARNING, stderr as
  ERROR, everything else INFO).
* **File-only detail** — anything sent through the stdlib ``logging`` module
  (``logging.getLogger(__name__).debug(...)``) is written to the session
  file by the handler installed here, and NEVER printed: no console handler
  exists. This is the channel for per-request HTTP traces, cache hits, and
  other diagnostics too chatty for the terminal.

Notes:
    run_scraper.main() calls start() right after argument parsing;
    webapp/ops.py opens one SessionLog per UI operation and streams its
    browser tee into it. The offline test suite never calls start()
    (tests/conftest.py points _log_dir at a tmp directory, autouse);
    tests/test_session_log.py enforces the record format, the level
    routing, and the logged-but-not-printed contract. Noisy third-party
    loggers (urllib3 et al.) are capped at WARNING so a DEBUG root level
    doesn't flood the file with connection chatter.
"""

import atexit
import logging
import sys
import threading
import time
from datetime import datetime

import config

# How many session logs to keep. A daily crawl plus ad-hoc maintenance runs
# stays under this for roughly a month of history.
KEEP = 60

# One record per line: timestamp, level, source logger, message.
_FMT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers capped at WARNING so the file stays about THIS
# application's decisions: urllib3/requests/playwright emit per-connection
# DEBUG chatter, werkzeug logs an INFO access line for EVERY request — the
# browser polls /api/run/status every ~1.5s, which buried a web-UI op's real
# records under hundreds of poll lines (2026-08-28 discover-term log) — and
# asyncio announces its event-loop policy at DEBUG. Their WARNING+ records
# (retries, request failures) still land.
_NOISY = ("urllib3", "requests", "charset_normalizer", "playwright",
          "werkzeug", "asyncio")

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


def _level_for(line, err):
    """The record level for one mirrored console line.

    >>> _level_for("  [!] greenhouse arine: HTTP 404", err=False) == logging.WARNING
    True
    >>> _level_for("  42 new job(s)", err=False) == logging.INFO
    True
    >>> _level_for("Traceback (most recent call last):", err=True) == logging.ERROR
    True
    """
    if err:
        return logging.ERROR
    if line.lstrip().startswith(("[!]", "[?]")):
        return logging.WARNING
    return logging.INFO


class SessionLog:
    """One session's log file: a logging handler feeding it, plus the
    line-buffered console mirror. duck-types as a write()-able sink so the
    webapp's stdout tee can stream into it unchanged.

    Notes:
        The handler is attached to the ROOT logger, so any module's
        ``logging.getLogger(__name__)`` records land in the file with no
        wiring — and nowhere else, because no console handler exists.
        close() detaches the handler, restores the root level and stamps
        the footer; it is safe to call more than once (and runs atexit for
        CLI sessions, so a crash still gets a footer after Python prints
        the traceback to the tee'd stderr).
    """

    def __init__(self, mode, invocation, now=None):
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune(log_dir)

        now = now or datetime.now()
        self.path = log_dir / f"session-{now:%Y%m%d-%H%M%S}-{_clean(mode)}.log"
        self._fh = open(self.path, "w", encoding="utf-8", errors="replace",
                        buffering=1)
        self._fh.write(f"# job crawler session\n"
                       f"# started : {now:%Y-%m-%d %H:%M:%S}\n"
                       f"# run     : {invocation}\n\n")
        self._t0 = time.monotonic()
        self._buf = {False: "", True: ""}     # partial console lines, by err
        self._lock = threading.Lock()
        self._closed = False

        self._handler = logging.StreamHandler(self._fh)
        self._handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        root = logging.getLogger()
        self._prev_root_level = root.level
        root.addHandler(self._handler)
        root.setLevel(logging.DEBUG)
        for name in _NOISY:
            noisy = logging.getLogger(name)
            if noisy.level == logging.NOTSET or noisy.level < logging.WARNING:
                noisy.setLevel(logging.WARNING)

    # ── console mirror ──────────────────────────────────────────────────
    def write(self, text):
        """Sink API for a stdout tee: mirror `text` as console records."""
        self.feed(text, err=False)
        return len(text)

    def flush(self):
        pass                                  # records are emitted per line

    def feed(self, text, err=False):
        """Buffer tee'd console output; emit one record per complete line."""
        if self._closed:
            return
        with self._lock:
            self._buf[err] += text
            while "\n" in self._buf[err]:
                line, self._buf[err] = self._buf[err].split("\n", 1)
                self._route(line, err)

    def _route(self, line, err):
        if not line.strip():
            return                            # layout blanks aren't records
        logging.getLogger("console").log(_level_for(line, err), "%s", line)

    # ── teardown ────────────────────────────────────────────────────────
    def close(self):
        if self._closed:
            return
        with self._lock:
            for err in (False, True):
                if self._buf[err].strip():
                    self._route(self._buf[err], err)
                self._buf[err] = ""
        self._closed = True
        root = logging.getLogger()
        root.removeHandler(self._handler)
        root.setLevel(self._prev_root_level)
        try:
            self._fh.write(f"\n# ended   : {datetime.now():%Y-%m-%d %H:%M:%S}"
                           f"  ({time.monotonic() - self._t0:.0f}s)\n")
            self._fh.close()
        except ValueError:
            pass


class _Tee:
    """Writes to the original stream and mirrors into the SessionLog; reads
    everything else (isatty, encoding, ...) from the original stream.

    Notes:
        The mirror side is wrapped so a closed session log (interpreter
        teardown, double finish) can never break console output.
    """

    def __init__(self, stream, session, err=False):
        self._stream = stream
        self._session = session
        self._err = err

    def write(self, text):
        n = self._stream.write(text)
        try:
            self._session.feed(text, err=self._err)
        except ValueError:
            pass
        return n

    def flush(self):
        self._stream.flush()

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
    """Create a fresh SessionLog (file + attached logging handler). The
    caller owns close() — start() below wires it up for CLI runs,
    webapp/ops.py streams its browser-tee'd output into one per UI
    operation.

    Notes:
        Also prunes the log directory down to KEEP files, so every entry
        point that opens a log enforces retention. `now` exists for tests.
    """
    return SessionLog(mode, invocation, now=now)


def start(argv, now=None):
    """Begin mirroring stdout/stderr into a new session log; returns its
    path. finish() (registered atexit) restores the streams, detaches the
    logging handler and stamps a footer with the elapsed time.

    Notes:
        argv is the CLI argument list *without* the program name, exactly
        what run_scraper.main() received. `now` exists for tests.
    """
    session = open_log(_mode(argv), "run_scraper.py " + " ".join(argv), now)

    out, err = sys.stdout, sys.stderr
    sys.stdout = _Tee(out, session)
    sys.stderr = _Tee(err, session, err=True)

    def _finish():
        sys.stdout, sys.stderr = out, err
        session.close()

    _active.append(_finish)
    atexit.register(finish)
    return session.path


def finish():
    """Close every session log opened by start(), restoring the original
    streams. Safe to call more than once.
    """
    while _active:
        _active.pop()()
