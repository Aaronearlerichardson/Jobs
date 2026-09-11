"""Shared HTTP defaults.

A single module-level `SESSION` gives every fetcher connection pooling and
keep-alive, so repeated hits to the same host (greenhouse/lever/ashby/workday
probes, board pagination) reuse one TCP+TLS connection instead of paying a
fresh handshake per request. Call sites use `SESSION.get(...)` /
`SESSION.post(...)` and inherit DEFAULT_TIMEOUT; `HEADERS` stays exported
because many call sites still pass `headers=HEADERS` explicitly (redundant:
the session already carries them as defaults) and robots.py builds its own
requests from it.
"""

import logging
import sys
import threading

from requests import Session
from requests.adapters import HTTPAdapter

from src.config import FETCH_TIMEOUT, USER_AGENT

# File-only request trace (src/session_log.py installs the handler; there
# is no console handler, so this never reaches the terminal). One record
# per request is the single most useful diagnostic when reading a session
# log after the fact: which URLs a pass actually hit, what answered, how
# slowly.
_log = logging.getLogger("http")

# Advertise only gzip/deflate — NOT brotli. requests would otherwise offer `br`
# (brotlicffi is installed), and some servers' chunked brotli responses crash
# that decoder ("can_accept_more_data() is False"), raising ContentDecodingError
# on .text/.content. That failure is silent in fetchers that try/except a fetch:
# the board just looks empty/unreachable (e.g. science.xyz careers pages). gzip
# and deflate are universally supported, so dropping br loses nothing.
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

#: HEADERS plus the JSON Accept every API-shaped board wants. Four
#: fetchers had their own module constant for this and four more built
#: it inline; a header set that must agree across them belongs in one
#: place, beside the UA it extends.
JSON_HEADERS = {**HEADERS, "Accept": "application/json"}

# Every request through SESSION waits this long (connect, read) unless the
# call names its own `timeout=`; passing `timeout=None` also means this
# default, never "wait forever". Fetchers therefore need no timeout
# constant of their own; discovery probes still pass PROBE_TIMEOUT.
DEFAULT_TIMEOUT = FETCH_TIMEOUT


class PoliteSession(Session):
    """A Session that consults robots.txt before every request.

    Doing it here rather than in each fetcher means one chokepoint for the
    whole crawler: every call site inherits the check, the per-host
    `Crawl-delay` pacing, and connection pooling, and there's no way to add
    a fetcher that quietly skips them.

    A disallowed path raises RobotsDisallowed rather than returning a fake
    response — fetchers already try/except their requests and report the
    reason, so it shows up in the crawl log like any other fetch failure.
    Set `[policy] respect_robots = false` in profile.toml to disable (the
    check, not the pooling).
    """

    def request(self, method, url, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DEFAULT_TIMEOUT
        # Imported lazily: robots.py imports HEADERS from this module.
        from .robots import CACHE, RobotsDisallowed
        if not CACHE.allowed(url):
            _log.debug("%s %s -> robots.txt disallow", method, url)
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        CACHE.wait_turn(url)               # honor Crawl-delay, per host
        try:
            r = super().request(method, url, *args, **kwargs)
        except Exception as e:
            _log.debug("%s %s -> %s", method, url, type(e).__name__)
            raise
        _log.debug("%s %s -> %s in %.2fs", method, url, r.status_code,
                   r.elapsed.total_seconds())
        return r


def _build_session():
    s = PoliteSession()
    s.headers.update(HEADERS)
    # Pool a handful of connections per host; discovery probes fan across a
    # few ATS hosts and re-hit each many times. max_retries=0 keeps failure
    # semantics identical to the old bare requests.get (callers try/except).
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _build_session()


def get_json(url, label, default=None, **kw):
    """The endpoint's JSON, or `default` -- reported, never raised.

    Eight fetchers wrote this out, two of them having already named it
    (`api._get_board`, `hnhiring._get_json`). A board that 500s, times out
    or answers with something that will not parse is a DEAD SOURCE, not an
    exception for the crawl to handle: the fan-out is running two hundred
    other boards and one of them being down says nothing about the rest.
    So this reports and returns, and every caller's failure path is the
    same shape.

    `label` names the source in the failure line -- it is the only thing a
    session log has to go on when a board stops answering. `default` is
    what the caller wants back: [] for a board listing, None for a detail
    payload the caller checks.

    No doctest: it would have to pin requests' own error wording, which
    changes between versions. tests/test_fetcher_parsers.py pins the
    contract through the fetchers instead.
    """
    try:
        r = SESSION.get(url, headers={**HEADERS, **kw.pop("headers", {})}, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        fetch_failed(label, e)
        return default


#: Per-THREAD count of fetch failures, because the harvester runs one board
#: per worker thread and the question is always "did THIS board fail?".
_FAILED = threading.local()


def fetch_failed(label, err, indent=4):
    """Report one failed fetch, count it, and hand back [].

    Thirty-seven sites across src/ats had written
    `print(f"    [!] {label}: {e}")` out by hand, and the copies had
    already drifted -- two indents, and ultipro.py using sys.stdout.write
    because print() emits the text and the newline as SEPARATE writes,
    which let another thread splice a line into the middle of one (seen
    fused with a [SNIFF] line in the 2026-08-28 log). One writer means one
    indent and one atomic write for everybody.

    Counting is the point, though. A fetcher that fails soft-returns [],
    and `[]` is also what a board with nothing on it returns, so by the
    time a caller sees the result the difference is gone -- 116 of 620
    boards came back empty in EVERY harvest run of the last 25 logs and
    not one of them was recorded as anything but an ordinary empty board.
    The failure was only ever in the log text. Now it is also a number the
    caller can read.

    Returns [] so a soft-failing fetcher can `return fetch_failed(...)`;
    call it as a statement where the failure path breaks or continues.
    """
    _FAILED.n = getattr(_FAILED, "n", 0) + 1
    sys.stdout.write(f"{' ' * indent}[!] {label}: {err}\n")
    return []


def fetch_failures():
    """Fetch failures reported on this thread since the last reset."""
    return getattr(_FAILED, "n", 0)


def reset_fetch_failures():
    """Start counting this thread's fetch failures from zero."""
    _FAILED.n = 0
