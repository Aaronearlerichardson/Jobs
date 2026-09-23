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
import time

from requests import Session
from requests.adapters import HTTPAdapter

from src.config import FETCH_TIMEOUT, USER_AGENT
from src.net.util import host_of

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
    """The endpoint's JSON, or `default` -- reported (`request_json`'s
    reasons), never raised.

    A board that 500s, comes back empty, answers with something that will
    not parse, or times out is a DEAD SOURCE, not an exception for the
    crawl to handle: the fan-out is running two hundred other boards and
    one of them being down says nothing about the rest. So this reports
    and returns, and every caller's failure path is the same shape.

    `label` names the source in the failure line -- it is the only thing a
    session log has to go on when a board stops answering. `default` is
    what the caller wants back: [] for a board listing, None for a detail
    payload the caller checks.

    No doctest: the exception-path wording is requests' own, which changes
    between versions. tests/test_fetcher_parsers.py pins the contract
    through the fetchers instead.

    Notes:
        Nine fetchers wrote this out, three of them having already named
        it (`api._get_board`, `hnhiring._get_json`, `company._get_json`).
    """
    _status, data, err = request_json("GET", url, label, **kw)
    return default if err else data


def request(method, url, label=None, **kw):
    """(status, response, error) for one request through SESSION.

    `error` is None on success, else "HTTP n" (status >= 400) or the raised
    exception (`status` and `response` None); it is reported through
    `fetch_failed` under `label` when a label is given. A caller judging a
    status itself (a closure probe reading 404 as "gone") passes no label.
    """
    try:
        r = getattr(SESSION, method.lower())(
            url, headers={**HEADERS, **kw.pop("headers", {})}, **kw)
    except Exception as e:
        return None, None, failed(label, e)
    if r.status_code >= 400:
        return r.status_code, r, failed(label, f"HTTP {r.status_code}")
    return r.status_code, r, None


def request_json(method, url, label=None, **kw):
    """(status, payload, error) for one JSON request: `request`'s, plus
    "empty response" and "non-JSON response" as errors."""
    status, r, err = request(method, url, label, **kw)
    if err:
        return status, None, err
    if not r.content.strip():
        return status, None, failed(label, "empty response")
    try:
        return status, r.json(), None
    except ValueError:
        return status, None, failed(label, "non-JSON response")


def failed(label, err):
    """`err`, reported through `fetch_failed` when there is a `label`:
    the one call for a path whose label is optional (a quiet probe passes
    none). Call `fetch_failed` directly where a failure is always news."""
    if label:
        fetch_failed(label, err)
    return err


#: Per-THREAD count of fetch failures, because the harvester runs one board
#: per worker thread and the question is always "did THIS board fail?".
_FAILED = threading.local()


def fetch_failed(label, err, indent=4):
    """Report one failed fetch, count it, and hand back [].

    The line goes out as ONE write, so another thread cannot splice into
    it. The count is the point: a fetcher that fails soft-returns [],
    which is also what a board with nothing on it returns, so the count
    is how a caller still tells the two apart (see snapshot_info).

    Returns [] so a soft-failing fetcher can `return fetch_failed(...)`;
    call it as a statement where the failure path breaks or continues.
    Also remembers `label: err` as this thread's last failure (see
    snapshot_info's `last_error`).

    Notes:
        Thirty-seven sites across src/ats had written
        `print(f"    [!] {label}: {e}")` by hand and had drifted: two
        indents, and print()'s separate text and newline writes let another
        thread splice into a line (fused with a [SNIFF] line in the
        2026-08-28 log). And 116 of 620 boards came back empty in EVERY
        harvest run of the last 25 logs, not one of them recorded as
        anything but an ordinary empty board.
    """
    _FAILED.n = getattr(_FAILED, "n", 0) + 1
    _FAILED.last = f"{label}: {err}"
    sys.stdout.write(f"{' ' * indent}[!] {label}: {err}\n")
    return []


def fetch_failures():
    """Fetch failures reported on this thread since the last reset."""
    return getattr(_FAILED, "n", 0)


#: Per-THREAD "the pager stopped before the board's end" marker, beside
#: _FAILED and for the same reason.
_CAPPED = threading.local()


def reset_fetch_failures():
    """Start this thread's fetch accounting from zero: the failure count,
    the last-failure message, and the capped marker. One call per fetch
    attempt, on the thread that runs it (crawl.harvest.harvest_board,
    net.parallel.fetch_all)."""
    _FAILED.n = 0
    _FAILED.last = None
    _CAPPED.hit, _CAPPED.total = False, None


def note_capped(total=None):
    """Record that this thread's snapshot was truncated: the board lists
    more than the pull returned. `total` is the board size the API
    reported, None when it reported none.

    A pager calls this, instead of raising, when it stops anywhere but the
    board's honest end: fewer rows than a known total, every page up to
    max_pages read with the last one still full, or a repeated page with no
    total to prove the walk complete. A fetcher that never calls it reads
    as uncapped.

    >>> reset_fetch_failures(); note_capped(50); snapshot_info()
    {'fetch_errors': 0, 'incomplete': False, 'capped': True, 'capped_total': 50, 'last_error': None}

    Notes:
        A capped snapshot is partial, not failed: its rows are real, but a
        row missing from it is no evidence the posting closed. See
        store.sync_job_statuses's `capped` argument.
    """
    _CAPPED.hit, _CAPPED.total = True, total


def snapshot_info():
    """This thread's fetch accounting since the last reset, as the callers
    record it: the failure count, whether the snapshot is INCOMPLETE (a
    fetch failed partway) or CAPPED (truncated without an error), and the
    LAST failure reported (None when there was none).

    >>> reset_fetch_failures(); snapshot_info()
    {'fetch_errors': 0, 'incomplete': False, 'capped': False, 'capped_total': None, 'last_error': None}

    A failure outranks a cap: an incomplete snapshot closes nothing, so it
    is never also reported capped.

    >>> _ = fetch_failed("board p3", "timeout", indent=0)
    [!] board p3: timeout
    >>> note_capped(50); snapshot_info()
    {'fetch_errors': 1, 'incomplete': True, 'capped': False, 'capped_total': None, 'last_error': 'board p3: timeout'}
    """
    n = fetch_failures()
    capped = getattr(_CAPPED, "hit", False) and not n
    return {"fetch_errors": n, "incomplete": n > 0, "capped": capped,
            "capped_total": getattr(_CAPPED, "total", None) if capped else None,
            "last_error": getattr(_FAILED, "last", None)}


class HostBreaker:
    """Hosts that keep refusing connections, skipped for a while.

    `trip(url)` records one refusal from the host of `url`; `dead(url)` is
    True once `trips` refusals have landed, each within `ttl` seconds of
    the one before, and stays True until `ttl` passes without another.
    Any URL on the host answers, whatever its path, case or port:

    >>> b = HostBreaker(ttl=60, trips=2)
    >>> b.trip("https://a.example/jobs/1"); b.dead("https://a.example/")
    False
    >>> b.trip("https://A.example:443/x"); b.dead("https://a.example/careers")
    True
    >>> b.dead("https://b.example/")
    False

    Once `ttl` has passed, the host is asked again:

    >>> b = HostBreaker(ttl=0); b.trip("https://a.example/")
    >>> b.dead("https://a.example/")
    False
    """

    def __init__(self, ttl, trips=1):
        self.ttl, self.trips = ttl, trips
        self._hits = {}     # host -> (last refusal, refusals in a row)
        self._lock = threading.Lock()

    def trip(self, url):
        host, now = host_of(url), time.time()
        if host:
            with self._lock:
                last, n = self._hits.get(host, (0.0, 0))
                self._hits[host] = (now, n + 1 if now - last < self.ttl else 1)

    def dead(self, url):
        host = host_of(url)
        with self._lock:
            hit = self._hits.get(host)
            if hit and time.time() - hit[0] >= self.ttl:
                del self._hits[host]
                return False
            return bool(hit) and hit[1] >= self.trips
