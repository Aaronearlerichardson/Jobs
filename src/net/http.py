"""The one way out to the network.

Every request, from a thread or a coroutine, goes through `send`: one
aiohttp session on one event loop, the network loop. Until every caller
is async the loop runs on a daemon thread, a thread reaches it through
`run_sync`, and `SESSION` is requests.Session's get and post over it.

requests stays as the model layer only: it prepares each request (URL,
params, body, headers), rules on each redirect, and reads each reply (a
real requests.Response), so what is sent and what is read are what
requests sent and read. Its own I/O is never used
(tests/test_invariants.py).
"""

import asyncio
import atexit
import concurrent.futures
import contextvars
import logging
import socket
import ssl
import sys
import threading
import time
import weakref
from datetime import timedelta
from functools import partial, wraps

import aiohttp
import requests
from requests import certs
from requests.cookies import RequestsCookieJar
from requests.sessions import SessionRedirectMixin, merge_setting
from requests.structures import CaseInsensitiveDict
from requests.utils import default_headers, get_encoding_from_headers
from urllib3.util.ssl_ import create_urllib3_context
from yarl import URL

from src.config import FETCH_TIMEOUT, PLAIN_USER_AGENT, USER_AGENT
from src.net.util import host_of, origin_of

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
PLAIN_HEADERS = {**HEADERS, "User-Agent": PLAIN_USER_AGENT}

# Every request waits this long (connect, read) unless the call names its
# own `timeout=`; passing `timeout=None` also means this default, never
# "wait forever". Fetchers therefore need no timeout constant of their
# own; discovery probes still pass PROBE_TIMEOUT.
DEFAULT_TIMEOUT = FETCH_TIMEOUT

#: The headers under every request's own: a bare requests session's, and
#: the crawler's (the same with HEADERS on top).
_BARE_HEADERS = default_headers()
_CRAWLER_HEADERS = merge_setting(HEADERS, _BARE_HEADERS,
                                 dict_class=CaseInsensitiveDict)

#: requests.Session's redirect limit.
MAX_REDIRECTS = 30

#: What a failed request raises: requests' own classes (see _raised).
#: Unreachable is requests.ConnectionError: a name that does not resolve,
#: a refusal, a failed TLS handshake or a connect timeout.
HTTPError = requests.HTTPError
Unreachable = requests.ConnectionError


# --------------------------------------------------------------------------- #
#  The network loop                                                           #
# --------------------------------------------------------------------------- #

_LOOP = None                    # the network loop, once started
_LOOP_THREAD = None
_START = threading.Lock()
#: thread -> the task its run_sync waits on. Read and written on the loop.
_WAITS = {}
#: Threads whose pool left them running (see abandon).
_ABANDONED = weakref.WeakSet()


def _loop():
    """The network loop, started on its daemon thread at first use and
    stopped at interpreter exit."""
    global _LOOP, _LOOP_THREAD
    with _START:
        if _LOOP is None:
            _LOOP = asyncio.new_event_loop()
            _LOOP_THREAD = threading.Thread(target=_LOOP.run_forever,
                                            name="net-loop", daemon=True)
            _LOOP_THREAD.start()
            atexit.register(_shutdown)
        return _LOOP


def run_sync(coro):
    """`coro`'s result, run on the network loop while this thread waits.

    For code that is not async yet; tests/test_invariants.py keeps it out
    of async code. The task runs in a copy of this thread's context, and
    a fetch failure it reports counts here:

    >>> async def dead():
    ...     return fetch_failed("board", "timeout", indent=0)
    >>> reset_fetch_failures(); run_sync(dead()); fetch_failures()
    [!] board: timeout
    []
    1

    On the loop itself it could only deadlock, so it refuses:

    >>> async def nested():
    ...     return run_sync(dead())
    >>> run_sync(nested())
    Traceback (most recent call last):
    RuntimeError: run_sync on the network loop: await the coroutine instead

    The task's exception is raised here, and anything else that ends the
    wait (Ctrl+C) cancels the task.

    Notes:
        A ContextVar set inside the task changes the copy, never this
        thread's context; the fetch accounting is a holder object for
        that reason (see _account).

        Not asyncio.run_coroutine_threadsafe: its cancelled future raises
        concurrent.futures.CancelledError, an Exception, which the
        fetchers' `except Exception` would swallow, so an abandoned board
        could store a short snapshot as a whole one.
    """
    try:
        loop, me = _loop(), threading.current_thread()
        if me is _LOOP_THREAD:
            raise RuntimeError("run_sync on the network loop: await the coroutine instead")
    except BaseException:
        coro.close()
        raise
    _account()
    ctx, done = contextvars.copy_context(), concurrent.futures.Future()

    def start():
        if me in _ABANDONED:
            coro.close()
            done.set_exception(asyncio.CancelledError())
            return
        task = _WAITS[me] = loop.create_task(coro, context=ctx)
        task.add_done_callback(partial(_settle, me, done))

    loop.call_soon_threadsafe(start)
    try:
        return done.result()
    except BaseException:
        if not done.done():
            loop.call_soon_threadsafe(_cancel, (me,))
        raise


def sync_shim(afn):
    """`afn`, a coroutine function `a<name>`, as the function `<name>` for
    a thread: its coroutine run through run_sync. The shims phase 8
    deletes.

    >>> async def atwice(n):
    ...     return 2 * n
    >>> twice = sync_shim(atwice)
    >>> twice(4), twice.__name__
    (8, 'twice')

    Notes:
        The shim keeps this module as its own, so doctest collects
        `afn`'s docstring once, from `afn`.
    """
    @wraps(afn, assigned=("__doc__",))
    def shim(*args, **kw):
        return run_sync(afn(*args, **kw))
    shim.__name__ = afn.__name__.removeprefix("a")
    shim.__qualname__ = afn.__qualname__.removesuffix(afn.__name__) + shim.__name__
    return shim


def _settle(thread, done, task):
    """Hand `task`'s outcome to `thread`, waiting in run_sync."""
    if _WAITS.get(thread) is task:
        del _WAITS[thread]
    if task.cancelled():
        done.set_exception(asyncio.CancelledError())
    elif task.exception() is not None:
        done.set_exception(task.exception())
    else:
        done.set_result(task.result())


def _cancel(threads):
    """Cancel the tasks `threads` wait on in run_sync. On the loop."""
    for thread in threads:
        task = _WAITS.get(thread)
        if task is not None:
            task.cancel()


def abandon(threads):
    """Cancel the request each of `threads` waits on in run_sync, and
    fail every one they start later with CancelledError: net.parallel's
    pool calls it for the work its block leaves running
    (tests/test_harvest.py::test_ctrl_c_mid_wait_never_starts_the_queued_work)."""
    threads = tuple(threads)
    _ABANDONED.update(threads)
    if threads and _LOOP is not None:
        _LOOP.call_soon_threadsafe(_cancel, threads)


def _shutdown():
    """Close the session and stop the loop: at interpreter exit, so no
    "Unclosed client session" warning is printed."""
    global _LOOP, _SESSION
    if _LOOP is None:
        return
    session, _SESSION = _SESSION, None
    try:
        if session is not None:
            run_sync(session.close())
    finally:
        _LOOP.call_soon_threadsafe(_LOOP.stop)
        _LOOP_THREAD.join(5)
        if not _LOOP_THREAD.is_alive():
            _LOOP.close()
        _LOOP = None


# --------------------------------------------------------------------------- #
#  One request                                                                #
# --------------------------------------------------------------------------- #

_SESSION = None


class _Resolver(aiohttp.ThreadedResolver):
    """aiohttp's ThreadedResolver, its answers and errors unchanged, with
    its lookups on DNS threads of its own rather than the loop's default
    executor: there a lookup could queue behind asyncio.to_thread workers
    that wait on this loop (run_sync), and never run."""

    def __init__(self):
        self._loop = self                # resolve() calls the two below
        self._pool = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="dns")

    def getaddrinfo(self, *args, **kwargs):
        return asyncio.get_running_loop().run_in_executor(
            self._pool, partial(socket.getaddrinfo, *args, **kwargs))

    def getnameinfo(self, *args):
        return asyncio.get_running_loop().run_in_executor(
            self._pool, socket.getnameinfo, *args)


def _session():
    """The one aiohttp session, made on the network loop at first use.

    Notes:
        What requests did, where it matters: its CA bundle and TLS
        context (requests' adapter builds the same one), a cookie jar
        that takes IP-address hosts and sends values unquoted, header
        lines up to http.client's 64 KiB. trust_env stays False: no proxy
        or CA-bundle environment variables, where requests read them.
    """
    global _SESSION
    if _SESSION is None:
        tls = create_urllib3_context()
        tls.load_verify_locations(certs.where())
        tls.sslobject_class = _Handshake
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=100, ssl=tls,
                                           keepalive_timeout=30,
                                           ttl_dns_cache=300,
                                           resolver=_Resolver()),
            timeout=_timeout(DEFAULT_TIMEOUT),
            cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
            max_line_size=65536, max_field_size=65536)
    return _SESSION


def _timeout(t):
    """aiohttp's timeout for requests' `timeout=`: a (connect, read) pair,
    one number for both, or None for DEFAULT_TIMEOUT.

    >>> t = _timeout((3.0, 10.0))
    >>> t.sock_connect, t.sock_read, t.total
    (3.0, 10.0, None)
    >>> _timeout(None).sock_read == DEFAULT_TIMEOUT[1]
    True
    """
    t = DEFAULT_TIMEOUT if t is None else t
    connect, read = t if isinstance(t, tuple) else (t, t)
    return aiohttp.ClientTimeout(total=None, connect=None,
                                 sock_connect=connect, sock_read=read)


def _prepare(method, url, polite=True, headers=None, params=None, data=None,
             json=None):
    """The request requests would send: the URL with its params encoded,
    the body, and `headers` over the session's own (a bare requests
    session's, with HEADERS on top when `polite`).

    >>> p = _prepare("get", "https://A.example/a b", params={"q": "x y"})
    >>> p.method, p.url
    ('GET', 'https://a.example/a%20b?q=x+y')
    >>> _prepare("POST", "https://a.example/", json={"k": 1}).body
    b'{"k": 1}'

    A None header drops the session's:

    >>> h = _prepare("GET", "https://a.example/", headers={"Accept": None}).headers
    >>> h["User-Agent"] == USER_AGENT, "Accept" in h
    (True, False)
    """
    p = requests.PreparedRequest()
    p.prepare(method=method, url=url, params=params or {}, data=data or {},
              json=json,
              headers=merge_setting(headers,
                                    _CRAWLER_HEADERS if polite else _BARE_HEADERS,
                                    dict_class=CaseInsensitiveDict))
    return p


def _headers(raw):
    """A reply's raw header pairs as requests reads them: latin-1, a
    repeated name's values joined by ", " under its first spelling.

    >>> list(_headers([(b"Set-Cookie", b"a=1"), (b"set-cookie", b"b=2")]).items())
    [('Set-Cookie', 'a=1, b=2')]
    """
    joined = {}
    for k, v in raw:
        k, v = k.decode("latin-1"), v.decode("latin-1")
        got = joined.get(k.lower())
        joined[k.lower()] = (got[0], f"{got[1]}, {v}") if got else (k, v)
    return CaseInsensitiveDict(dict(joined.values()))


def _reply(req, status, reason, raw_headers, content, elapsed=0.0):
    """The requests.Response to `req`, built as requests builds it, so
    `.text`, `.json()` and `raise_for_status()` are requests' own: text
    with no charset reads as ISO-8859-1.

    >>> r = _reply(_prepare("GET", "https://a.example/"), 200, "OK",
    ...            [(b"Content-Type", b"text/html")], "é".encode())
    >>> r.url, r.encoding, r.text
    ('https://a.example/', 'ISO-8859-1', 'Ã©')
    """
    r = requests.Response()
    r.status_code, r.reason, r.url, r.request = status, reason, req.url, req
    r.headers = _headers(raw_headers)
    r.encoding = get_encoding_from_headers(r.headers)
    r._content, r._content_consumed = content, True
    r.elapsed = timedelta(seconds=elapsed)
    return r


#: aiohttp's failures, most specific first, and the requests class each
#: is raised as.
_ERRORS = (
    (aiohttp.ConnectionTimeoutError, requests.ConnectTimeout),
    (aiohttp.ServerTimeoutError, requests.ReadTimeout),
    (aiohttp.ClientSSLError, requests.exceptions.SSLError),
    (aiohttp.NonHttpUrlClientError, requests.exceptions.InvalidSchema),
    (aiohttp.InvalidURL, requests.exceptions.InvalidURL),
    (aiohttp.ClientPayloadError, requests.exceptions.ChunkedEncodingError),
    (TimeoutError, requests.Timeout),
    ((OSError, aiohttp.ClientError), requests.ConnectionError),
)


def _raised(e, req=None, handshaking=False):
    """The requests exception for aiohttp's `e`, carrying `e`.

    >>> _raised(aiohttp.SocketTimeoutError("read timed out"))
    ReadTimeout(SocketTimeoutError('read timed out'))
    >>> type(_raised(aiohttp.ServerDisconnectedError())).__name__
    'ConnectionError'

    aiohttp's connect timeout also covers the TLS handshake, which urllib3
    times with the same budget but reports as a read timeout (the host did
    answer). So does this, once the handshake has begun:

    >>> type(_raised(aiohttp.ConnectionTimeoutError(), handshaking=True)).__name__
    'ReadTimeout'
    """
    if handshaking and isinstance(e, aiohttp.ConnectionTimeoutError):
        return requests.ReadTimeout(e, request=req)
    return next(cls for kind, cls in _ERRORS if isinstance(e, kind))(e, request=req)


#: Each hop's [handshake begun]: _Handshake sets it, in the context of the
#: task that made the connection.
_HANDSHAKING = contextvars.ContextVar("handshaking")


class _Handshake(ssl.SSLObject):
    """The session's TLS object. Its handshake beginning means the TCP
    connection was made, which it notes for _raised."""

    def do_handshake(self):
        began = _HANDSHAKING.get(None)
        if began:
            began[0] = True
        return super().do_handshake()


class _Redirects(SessionRedirectMixin):
    """requests' redirect rules without a Session: resolve_redirects(...,
    yield_requests=True) names the next request and sends nothing."""

    max_redirects, trust_env, cookies = MAX_REDIRECTS, False, RequestsCookieJar()


_REDIRECTS = _Redirects()


async def _hop(req, timeout):
    """The requests.Response to the prepared `req`, redirects unfollowed."""
    body = req.body.encode("latin-1") if isinstance(req.body, str) else req.body
    began = [False]
    _HANDSHAKING.set(began)
    t0 = time.monotonic()
    try:
        async with _session().request(req.method, URL(req.url, encoded=True),
                                      headers=req.headers, data=body,
                                      allow_redirects=False,
                                      timeout=timeout) as resp:
            elapsed = time.monotonic() - t0
            content = await resp.read()
    except (aiohttp.ClientError, OSError) as e:
        raise _raised(e, req, began[0]) from e
    return _reply(req, resp.status, resp.reason, resp.raw_headers, content,
                  elapsed)


async def _exchange(method, url, polite=True, timeout=None,
                    allow_redirects=True, **kw):
    """The requests.Response requests would return for one request (its
    keywords: headers, params, data, json), redirects followed by
    requests' rules, MAX_REDIRECTS at most
    (tests/test_robots.py::TestTransport)."""
    req, hops, limit = _prepare(method, url, polite, **kw), [], _timeout(timeout)
    r = await _hop(req, limit)
    while allow_redirects and r.is_redirect:
        if len(hops) >= MAX_REDIRECTS:
            raise requests.TooManyRedirects(
                f"Exceeded {MAX_REDIRECTS} redirects.", response=r)
        hops.append(r)
        req = next(_REDIRECTS.resolve_redirects(r, req, yield_requests=True))
        r = await _hop(req, limit)
    r.history = hops
    return r


async def send(method, url, *, polite=True, **kw):
    """The requests.Response for one request, taking requests' keywords
    (headers, params, data, json, timeout, allow_redirects).

    A polite request is the crawler's: robots.txt may refuse it
    (RobotsDisallowed, raised rather than faked, so it reaches the crawl
    log like any fetch failure), it waits its host's Crawl-delay, and it
    leaves one DEBUG line on the "http" logger. A plain one is what a bare
    requests session sends: its headers, no robots.txt, no trace.

    Notes:
        One chokepoint for the whole crawler: every call site inherits the
        robots check, the pacing and the pooling, and no fetcher can
        quietly skip them. `[policy] respect_robots = false` turns the
        check off (not the pooling).
    """
    method = method.upper()
    if not polite:
        return await _exchange(method, url, polite=False, **kw)
    # Imported here: robots.py imports this module.
    from .robots import CACHE, RobotsDisallowed
    if not await CACHE.allowed(url):
        _log.debug("%s %s -> robots.txt disallow", method, url)
        raise RobotsDisallowed(f"robots.txt disallows {url}")
    await CACHE.wait_turn(url)
    try:
        r = await _exchange(method, url, **kw)
    except Exception as e:
        _log.debug("%s %s -> %s", method, url, type(e).__name__)
        raise
    _log.debug("%s %s -> %s in %.2fs", method, url, r.status_code,
               r.elapsed.total_seconds())
    return r


class HostLimiter:
    """Spaces the turns on one origin: `await wait(url, gap)` returns once
    the last turn on url's origin is `gap` seconds old, holding only the
    calls queued behind it on that origin
    (tests/test_robots.py::test_crawl_delay_spaces_one_origin_only)."""

    def __init__(self):
        self._origins = {}      # origin -> [asyncio.Lock, next turn (monotonic)]

    async def wait(self, url, gap):
        """Wait for url's origin's turn, then book the next `gap` on."""
        slot = self._origins.setdefault(origin_of(url), [asyncio.Lock(), 0.0])
        async with slot[0]:
            await asyncio.sleep(slot[1] - time.monotonic())
            slot[1] = time.monotonic() + gap


#: The process's one limiter: robots.txt's Crawl-delay feeds it.
LIMITER = HostLimiter()


class SyncSession:
    """requests.Session's get and post over `send`, for the threads that
    are not async yet: each call blocks its thread in run_sync and returns
    the requests.Response. `polite` is send's."""

    def __init__(self, polite=True):
        self.polite = polite

    def get(self, url, **kw):
        return run_sync(send("GET", url, polite=self.polite, **kw))

    def post(self, url, **kw):
        return run_sync(send("POST", url, polite=self.polite, **kw))


#: The crawler's session: every fetcher's requests are polite.
SESSION = SyncSession()


async def arequest(method, url, label=None, **kw):
    """(status, response, error) for one polite request, HEADERS under the
    call's own.

    `error` is None on success, else "HTTP n" (status >= 400) or the raised
    exception (`status` and `response` None); it is reported through
    `fetch_failed` under `label` when a label is given. A caller judging a
    status itself (a closure probe reading 404 as "gone") passes no label.
    """
    try:
        r = await send(method, url, headers={**HEADERS, **kw.pop("headers", {})},
                       **kw)
    except Exception as e:
        return None, None, failed(label, e)
    if r.status_code >= 400:
        return r.status_code, r, failed(label, f"HTTP {r.status_code}")
    return r.status_code, r, None


def _json_of(status, r, err, label):
    """arequest_json's answer from arequest's: "empty response" and
    "non-JSON response" are errors too."""
    if err:
        return status, None, err
    if not r.content.strip():
        return status, None, failed(label, "empty response")
    try:
        return status, r.json(), None
    except ValueError:
        return status, None, failed(label, "non-JSON response")


async def arequest_json(method, url, label=None, **kw):
    """(status, payload, error) for one JSON request: `arequest`'s, plus
    "empty response" and "non-JSON response" as errors. The JSON is
    decoded off the loop, its failure counted here (`_account`)."""
    _account()
    return await asyncio.to_thread(_json_of, *await arequest(method, url, label, **kw), label)


async def aget_json(url, label, default=None, **kw):
    """The endpoint's JSON, or `default` -- reported (`arequest_json`'s
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

    No doctest: the exception-path wording is the transport's own, which
    changes between versions. tests/test_fetcher_parsers.py pins the
    contract through the fetchers instead.

    Notes:
        Nine fetchers wrote this out, three of them having already named
        it (`api._get_board`, `hnhiring._get_json`, `company._get_json`).
    """
    _status, data, err = await arequest_json("GET", url, label, **kw)
    return default if err else data


# --------------------------------------------------------------------------- #
#  Fetch accounting                                                           #
# --------------------------------------------------------------------------- #

def failed(label, err):
    """`err`, reported through `fetch_failed` when there is a `label`:
    the one call for a path whose label is optional (a quiet probe passes
    none). Call `fetch_failed` directly where a failure is always news."""
    if label:
        fetch_failed(label, err)
    return err


class _Account:
    """One fetch attempt's accounting (see snapshot_info)."""

    __slots__ = ("n", "last", "capped", "total")

    def __init__(self):
        self.n, self.last, self.capped, self.total = 0, None, False, None


#: The current fetch attempt's accounting. A thread starts with an empty
#: context of its own and keeps it across a pool's work items, so this is
#: per thread under the pools, as threading.local was, and per task under
#: asyncio.
_ACCOUNT = contextvars.ContextVar("fetch_account")


def _account():
    """This context's accounting, made on first use. A holder, not values:
    run_sync's task runs in a copy of the context, and still counts here."""
    acct = _ACCOUNT.get(None)
    if acct is None:
        acct = _Account()
        _ACCOUNT.set(acct)
    return acct


def fetch_failed(label, err, indent=4):
    """Report one failed fetch, count it, and hand back [].

    The line goes out as ONE write, so another thread cannot splice into
    it. The count is the point: a fetcher that fails soft-returns [],
    which is also what a board with nothing on it returns, so the count
    is how a caller still tells the two apart (see snapshot_info).

    Returns [] so a soft-failing fetcher can `return fetch_failed(...)`;
    call it as a statement where the failure path breaks or continues.
    Also remembers `label: err` as this context's last failure (see
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
    acct = _account()
    acct.n += 1
    acct.last = f"{label}: {err}"
    sys.stdout.write(f"{' ' * indent}[!] {label}: {err}\n")
    return []


def fetch_failures():
    """Fetch failures reported in this context since the last reset."""
    return _account().n


def reset_fetch_failures():
    """Start this context's fetch accounting from zero: the failure count,
    the last-failure message, and the capped marker. One call per fetch
    attempt, where it runs (crawl.harvest.harvest_board,
    net.parallel.fetch_all)."""
    _ACCOUNT.set(_Account())


def note_capped(total=None):
    """Record that this context's snapshot was truncated: the board lists
    more than the pull returned. `total` is the board size the API
    reported, None when it reported none.

    A pager calls this, instead of raising, when it stops anywhere but the
    board's honest end: fewer rows than a known total, every page up to
    its page cap read with the last one still full, or a repeated page with no
    total to prove the walk complete. A fetcher that never calls it reads
    as uncapped.

    >>> reset_fetch_failures(); note_capped(50); snapshot_info()
    {'fetch_errors': 0, 'incomplete': False, 'capped': True, 'capped_total': 50, 'last_error': None}

    Notes:
        A capped snapshot is partial, not failed: its rows are real, but a
        row missing from it is no evidence the posting closed. See
        store.sync_job_statuses's `capped` argument.
    """
    acct = _account()
    acct.capped, acct.total = True, total


def snapshot_info():
    """This context's fetch accounting since the last reset, as the callers
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
    acct = _account()
    capped = acct.capped and not acct.n
    return {"fetch_errors": acct.n, "incomplete": acct.n > 0, "capped": capped,
            "capped_total": acct.total if capped else None,
            "last_error": acct.last}


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
