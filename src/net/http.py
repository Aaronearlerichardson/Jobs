"""The one way out to the network.

Every request goes through `send`, a coroutine on the entry point's event
loop: one aiohttp session per run (src/runstate.py), opened at the run's
first request and closed as it ends.

requests stays as the model layer only: it prepares each request (URL,
params, body, headers), rules on each redirect, and reads each reply (a
real requests.Response), so what is sent and what is read are what
requests sent and read. Its own I/O is never used
(tests/test_invariants.py).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import ssl
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Unpack, cast, override

import aiohttp
import certifi
import requests
from requests.cookies import RequestsCookieJar
from requests.sessions import SessionRedirectMixin, merge_setting
from requests.structures import CaseInsensitiveDict
from requests.utils import default_headers, get_encoding_from_headers
from typing_extensions import TypedDict
from urllib3.util.ssl_ import create_urllib3_context
from yarl import URL

from src import runstate
from src.config import (FETCH_TIMEOUT, PLAIN_USER_AGENT, REFUSAL_TRIPS, REFUSAL_TTL_S,
                        USER_AGENT)
from src.net.util import JSON, host_of, origin_key

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

#: requests.Session's redirect limit.
MAX_REDIRECTS = 30

#: What a failed request raises: requests' own classes (see _raised).
#: Unreachable is requests.ConnectionError: a name that does not resolve,
#: a refusal, a failed TLS handshake or a connect timeout.
HTTPError = requests.HTTPError
Unreachable = requests.ConnectionError
#: Any failed request (HTTPError and Unreachable are kinds of it); `send` also
#: raises robots.RobotsDisallowed, which is not one.
RequestError = requests.RequestException


class HostRefusing(RequestError):
    """A polite request not sent: its host has been blocking us (`send`)."""


# --------------------------------------------------------------------------- #
#  One request                                                                #
# --------------------------------------------------------------------------- #

def _open_session() -> aiohttp.ClientSession:
    """A run's aiohttp session, closed as the run ends.

    Notes:
        What requests did, where it matters: its CA bundle and TLS
        context (requests' adapter builds the same one), a cookie jar
        that takes IP-address hosts and sends values unquoted, header
        lines up to http.client's 64 KiB. trust_env stays False: no proxy
        or CA-bundle environment variables, where requests read them.
        Names resolve through aiohttp's ThreadedResolver, on the loop's
        default executor, where no work waits on the loop.
    """
    tls = create_urllib3_context()
    tls.load_verify_locations(certifi.where())
    tls.sslobject_class = _Handshake
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=100, ssl=tls, keepalive_timeout=30,
                                       ttl_dns_cache=300),
        timeout=_timeout(DEFAULT_TIMEOUT),
        cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
        max_line_size=65536, max_field_size=65536)
    runstate.at_exit(session.close, last=True)
    return session


#: This run's aiohttp session, opened at its first request.
_session = runstate.per_run(_open_session)


def _timeout(t: float | tuple[float, float] | None) -> aiohttp.ClientTimeout:
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


type Param = str | bytes | int | float


class PrepKw(TypedDict, total=False):
    """The request keywords `_prepare` takes."""
    headers: Mapping[str, str | None] | None
    params: Mapping[str, Param | Iterable[Param] | None] | None
    data: Mapping[str, Param | None] | str | bytes | None
    json: JSON


class SendKw(PrepKw, total=False):
    """The keywords `send` takes: `_prepare`'s, plus the transport's."""
    timeout: float | tuple[float, float] | None
    allow_redirects: bool


def _prepare(method: str, url: str, polite: bool = True, **kw: Unpack[PrepKw]
             ) -> requests.PreparedRequest:
    """The request requests would send: the URL with its params encoded,
    the body, and `headers` over the session's own (a bare requests
    session's, with HEADERS on top when `polite`: the crawler's).

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
    bare = default_headers()
    p = requests.PreparedRequest()
    p.prepare(method=method, url=url, params=kw.get("params") or {},
              data=kw.get("data") or {}, json=kw.get("json"),
              headers=merge_setting(
                  kw.get("headers"),
                  merge_setting(HEADERS, bare, dict_class=CaseInsensitiveDict)
                  if polite else bare,
                  dict_class=CaseInsensitiveDict))
    return p


def _headers(raw: Iterable[tuple[bytes, bytes]]) -> CaseInsensitiveDict[str]:
    """A reply's raw header pairs as requests reads them: latin-1, a
    repeated name's values joined by ", " under its first spelling.

    >>> list(_headers([(b"Set-Cookie", b"a=1"), (b"set-cookie", b"b=2")]).items())
    [('Set-Cookie', 'a=1, b=2')]
    """
    joined: dict[str, tuple[str, str]] = {}
    for kb, vb in raw:
        k, v = kb.decode("latin-1"), vb.decode("latin-1")
        got = joined.get(k.lower())
        joined[k.lower()] = (got[0], f"{got[1]}, {v}") if got else (k, v)
    return CaseInsensitiveDict(dict(joined.values()))


class _Response(requests.Response):
    """requests.Response, naming the private fields `_reply` fills (the stubs omit them)."""
    _content: bytes | None
    _content_consumed: bool


def _reply(req: requests.PreparedRequest, status: int, reason: str | None,
           raw_headers: Iterable[tuple[bytes, bytes]], content: bytes,
           elapsed: float = 0.0) -> requests.Response:
    """The requests.Response to `req`, built as requests builds it, so
    `.text`, `.json()` and `raise_for_status()` are requests' own: text
    with no charset reads as ISO-8859-1.

    >>> r = _reply(_prepare("GET", "https://a.example/"), 200, "OK",
    ...            [(b"Content-Type", b"text/html")], "é".encode())
    >>> r.url, r.encoding, r.text
    ('https://a.example/', 'ISO-8859-1', 'Ã©')
    """
    r = _Response()
    # requests types reason and url as str, and leaves them None until set.
    r.status_code, r.request = status, req
    if reason is not None:
        r.reason = reason
    if req.url is not None:
        r.url = req.url
    r.headers = _headers(raw_headers)
    r.encoding = get_encoding_from_headers(r.headers)
    r._content, r._content_consumed = content, True
    r.elapsed = timedelta(seconds=elapsed)
    return r


def _raised(e: BaseException, req: requests.PreparedRequest | None = None,
            handshaking: bool = False) -> requests.RequestException:
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
    # aiohttp's failures, most specific first, and the requests class each
    # is raised as.
    errors = (
        (aiohttp.ConnectionTimeoutError, requests.ConnectTimeout),
        (aiohttp.ServerTimeoutError, requests.ReadTimeout),
        (aiohttp.ClientSSLError, requests.exceptions.SSLError),
        (aiohttp.NonHttpUrlClientError, requests.exceptions.InvalidSchema),
        (aiohttp.InvalidURL, requests.exceptions.InvalidURL),
        (aiohttp.ClientPayloadError, requests.exceptions.ChunkedEncodingError),
        (TimeoutError, requests.Timeout),
        ((OSError, aiohttp.ClientError), requests.ConnectionError),
    )
    return next(cls for kind, cls in errors if isinstance(e, kind))(e, request=req)


#: Each hop's [handshake begun]: _Handshake sets it, in the context of the
#: task that made the connection.
_HANDSHAKING: contextvars.ContextVar[list[bool]] = contextvars.ContextVar("handshaking")


class _Handshake(ssl.SSLObject):
    """The session's TLS object. Its handshake beginning means the TCP
    connection was made, which it notes for _raised."""

    @override
    def do_handshake(self) -> None:
        began = _HANDSHAKING.get(None)
        if began:
            began[0] = True
        return super().do_handshake()


class _Redirects(SessionRedirectMixin):
    """requests' redirect rules without a Session: resolve_redirects(...,
    yield_requests=True) names the next request and sends nothing."""

    max_redirects, trust_env, cookies = MAX_REDIRECTS, False, RequestsCookieJar()


def _target(url: str) -> URL:
    """aiohttp's URL for `url`: as encoded, its host lower-cased, as
    urllib3 sends it. aiohttp's cookie jar matches hosts by case, so a
    redirect to an upper-case host would lose the run's cookies.

    >>> str(_target("https://CSS-A.Example.COM:443/sso?u=a%2Fb"))
    'https://css-a.example.com/sso?u=a%2Fb'
    """
    target = URL(url, encoded=True)
    return target.with_host(target.raw_host.lower()) if target.raw_host else target


async def _hop(req: requests.PreparedRequest, timeout: aiohttp.ClientTimeout) -> requests.Response:
    """The requests.Response to the prepared `req`, redirects unfollowed."""
    method, url = cast(str, req.method), cast(str, req.url)     # prepare() set both
    body = req.body.encode("latin-1") if isinstance(req.body, str) else req.body
    began = [False]
    _HANDSHAKING.set(began)
    t0 = time.monotonic()
    try:
        async with _session().request(method, _target(url),
                                      headers=req.headers,
                                      data=body,
                                      allow_redirects=False,
                                      timeout=timeout) as resp:
            elapsed = time.monotonic() - t0
            content = await resp.read()
    except (aiohttp.ClientError, OSError) as e:
        raise _raised(e, req, began[0]) from e
    return _reply(req, resp.status, resp.reason, resp.raw_headers, content,
                  elapsed)


async def _exchange(method: str, url: str, polite: bool = True,
                    timeout: float | tuple[float, float] | None = None,
                    allow_redirects: bool = True, **kw: Unpack[PrepKw]) -> requests.Response:
    """The requests.Response requests would return for one request (its
    keywords: headers, params, data, json), redirects followed by
    requests' rules, MAX_REDIRECTS at most
    (tests/test_robots.py::TestTransport). A polite request's hop to an
    origin it has not been on yet waits that origin's turn first, as a
    first request to it would (tests/test_robots.py::
    test_a_redirect_into_a_shared_host_waits_its_turn)."""
    req, limit = _prepare(method, url, polite, **kw), _timeout(timeout)
    hops: list[requests.Response] = []
    r = await _hop(req, limit)
    origins = {origin_key(url)}
    while allow_redirects and r.is_redirect:
        if len(hops) >= MAX_REDIRECTS:
            raise requests.TooManyRedirects(
                f"Exceeded {MAX_REDIRECTS} redirects.", response=r)
        hops.append(r)
        req = cast(requests.PreparedRequest,       # what yield_requests yields
                   next(_Redirects().resolve_redirects(r, req, yield_requests=True)))
        url = cast(str, req.url)
        if polite and (origin := origin_key(url)) not in origins:
            origins.add(origin)
            from .robots import CACHE       # robots.py imports this module
            await CACHE().wait_turn(url)
        r = await _hop(req, limit)
    r.history = hops
    return r


def retry_after(r: requests.Response, cap: float = 30.0, default: float = 5.0) -> float:
    """Seconds a reply asks to wait (its Retry-After, when a non-negative
    number), at most `cap`; `default` when it names none.

    >>> from requests import Response
    >>> r = Response(); r.headers["Retry-After"] = "2"
    >>> retry_after(r), retry_after(Response()), retry_after(Response(), default=20)
    (2.0, 5.0, 20)
    >>> r.headers["Retry-After"] = "600"
    >>> retry_after(r), retry_after(r, cap=900)
    (30.0, 600.0)
    >>> r.headers["Retry-After"] = "Wed, 21 Oct 2026 07:28:00 GMT"
    >>> retry_after(r)
    5.0
    """
    try:
        ask = float(r.headers.get("Retry-After", ""))
    except ValueError:
        return default
    return min(ask, cap) if ask >= 0 else default


async def send(method: str, url: str, *, polite: bool = True,
               **kw: Unpack[SendKw]) -> requests.Response:
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

        A host that answers REFUSAL_TRIPS 403/429s in a row is skipped,
        HostRefusing raised unsent, for REFUSAL_TTL_S (`_REFUSING`).
    """
    method = method.upper()
    if not polite:
        return await _exchange(method, url, polite=False, **kw)
    refusing = _REFUSING()
    if refusing.dead(url):
        _log.debug("%s %s -> skipped, host refusing", method, url)
        raise HostRefusing(f"{host_of(url)} refused {REFUSAL_TRIPS} requests in a row")
    # Imported here: robots.py imports this module.
    from .robots import CACHE, RobotsDisallowed
    robots = CACHE()
    if not await robots.allowed(url):
        _log.debug("%s %s -> robots.txt disallow", method, url)
        raise RobotsDisallowed(f"robots.txt disallows {url}")
    await robots.wait_turn(url)
    try:
        r = await _exchange(method, url, **kw)
        if r.status_code == 429:        # told to slow down: the host waits as asked, then retry once
            LIMITER.defer(url, retry_after(r))
            await robots.wait_turn(url)
            r = await _exchange(method, url, **kw)
    except Exception as e:
        _log.debug("%s %s -> %s", method, url, type(e).__name__)
        raise
    _log.debug("%s %s -> %s in %.2fs", method, url, r.status_code,
               r.elapsed.total_seconds())
    if r.status_code in (403, 429):
        refusing.trip(url)
    elif r.status_code < 400:
        refusing.clear(url)
    return r


class _Turn:
    """One origin's lock and its next turn (monotonic)."""

    __slots__ = ("lock", "next")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.next = 0.0


class HostLimiter:
    """Spaces the turns on one origin: `await wait(url, gap)` returns once
    the last turn on url's origin is `gap` seconds old, holding only the
    calls queued behind it on that origin
    (tests/test_robots.py::test_crawl_delay_spaces_one_origin_only)."""

    def __init__(self) -> None:
        self._origins: dict[str, _Turn] = {}

    def _slot(self, url: str) -> _Turn:
        key = origin_key(url)
        if (turn := self._origins.get(key)) is None:
            turn = self._origins[key] = _Turn()
        return turn

    async def wait(self, url: str, gap: float) -> None:
        """Wait for url's origin's turn, then book the next `gap` on."""
        slot = self._slot(url)
        async with slot.lock:
            while (left := slot.next - time.monotonic()) > 0:   # a defer may land mid-sleep
                await asyncio.sleep(left)
            slot.next = time.monotonic() + gap

    def defer(self, url: str, seconds: float) -> None:
        """Push url's origin's next turn out to `seconds` from now (never
        earlier): a 429's Retry-After holds back every call queued on the
        origin, not only the one that earned it.

        >>> async def demo():
        ...     lim, t0 = HostLimiter(), time.monotonic()
        ...     lim.defer("https://a.test/x", 0.05)
        ...     await lim.wait("https://a.test/y", 0)
        ...     return time.monotonic() - t0 >= 0.04
        >>> asyncio.run(demo())
        True
        """
        slot = self._slot(url)
        slot.next = max(slot.next, time.monotonic() + seconds)


#: The process's one limiter: robots.txt's Crawl-delay feeds it. Not run
#: state (src/runstate.py): a host's Crawl-delay spans runs, which follow
#: one another (the web UI's ops, the timed harvester's passes) and can
#: overlap (a web UI request beside an op).
LIMITER = HostLimiter()


async def request(method: str, url: str, label: str | None = None, *,
                  polite: bool = True, **kw: Unpack[SendKw]
                  ) -> tuple[int | None, requests.Response | None, str | Exception | None]:
    """(status, response, error) for one polite request, HEADERS under the
    call's own.

    `error` is None on success, else "HTTP n" (status >= 400) or the raised
    exception (`status` and `response` None); it is reported through
    `fetch_failed` under `label` when a label is given. A caller judging a
    status itself (a closure probe reading 404 as "gone") passes no label.
    """
    own: SendKw = {**kw, "headers": {**HEADERS, **(kw.get("headers") or {})}}
    try:
        r = await send(method, url, polite=polite, **own)
    except Exception as e:
        return None, None, failed(label, e)
    if r.status_code >= 400:
        return r.status_code, r, failed(label, f"HTTP {r.status_code}")
    return r.status_code, r, None


def _json_of(status: int | None, r: requests.Response | None, err: str | Exception | None,
             label: str | None) -> tuple[int | None, JSON, str | Exception | None]:
    """request_json's answer from request's: "empty response" and
    "non-JSON response" are errors too."""
    if err:
        return status, None, err
    reply = cast(requests.Response, r)      # there is one when there is no error
    if not reply.content.strip():
        return status, None, failed(label, "empty response")
    try:
        return status, reply.json(), None
    except ValueError:
        return status, None, failed(label, "non-JSON response")


async def request_json(method: str, url: str, label: str | None = None, *,
                       polite: bool = True, **kw: Unpack[SendKw]
                       ) -> tuple[int | None, JSON, str | Exception | None]:
    """(status, payload, error) for one JSON request: `request`'s, plus
    "empty response" and "non-JSON response" as errors. The JSON is
    decoded off the loop, its failure counted here (`_account`)."""
    _account()
    status, r, err = await request(method, url, label, polite=polite, **kw)
    return await asyncio.to_thread(_json_of, status, r, err, label)


async def get_json(url: str, label: str | None, default: JSON = None, **kw: Unpack[SendKw]) -> JSON:
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

    No doctest: the exception-path wording is the transport's own, which
    changes between versions. tests/test_fetcher_parsers.py pins the
    contract through the fetchers instead.

    Notes:
        Nine fetchers wrote this out, three of them having already named
        it (`api._get_board`, `hnhiring._get_item`, `company._get_json`).
    """
    _status, data, err = await request_json("GET", url, label, **kw)
    return default if err else data


# --------------------------------------------------------------------------- #
#  Fetch accounting                                                           #
# --------------------------------------------------------------------------- #

def failed[E](label: str | None, err: E) -> E:
    """`err`, reported through `fetch_failed` when there is a `label`:
    the one call for a path whose label is optional (a quiet probe passes
    none). Call `fetch_failed` directly where a failure is always news."""
    if label:
        fetch_failed(label, err)
    return err


class _Account:
    """One fetch attempt's accounting (see snapshot_info)."""

    __slots__ = ("n", "last", "capped", "total", "fill_rows", "fill_sum")

    def __init__(self) -> None:
        self.n = 0
        self.last: str | None = None
        self.capped = False
        self.total: int | None = None
        self.fill_rows = 0
        self.fill_sum: dict[str, float] = {}


#: The current fetch attempt's accounting: per task.
_ACCOUNT: contextvars.ContextVar[_Account] = contextvars.ContextVar("fetch_account")


def _account() -> _Account:
    """This context's accounting, made on first use. A holder, not values:
    an asyncio.to_thread worker runs in a copy of its task's context, and
    still counts here."""
    acct = _ACCOUNT.get(None)
    if acct is None:
        acct = _Account()
        _ACCOUNT.set(acct)
    return acct


def fetch_failed[T](label: str, err: object, indent: int = 4) -> list[T]:
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
    if not isinstance(err, HostRefusing):   # `send` logged the skip; the block's 403s printed
        sys.stdout.write(f"{' ' * indent}[!] {label}: {err}\n")
    return []


def fetch_failures() -> int:
    """Fetch failures reported in this context since the last reset."""
    return _account().n


def reset_fetch_failures() -> None:
    """Start this context's fetch accounting from zero: the failure count,
    the last-failure message, and the capped marker. One call per fetch
    attempt, where it runs (crawl.harvest.harvest_board,
    net.parallel.fetch_all)."""
    _ACCOUNT.set(_Account())


def note_capped(total: int | None = None) -> None:
    """Record that this context's snapshot was truncated: the board lists
    more than the pull returned. `total` is the board size the API
    reported, None when it reported none.

    A pager calls this, instead of raising, when it stops anywhere but the
    board's honest end: fewer rows than a known total, every page up to
    its page cap read with the last one still full, or a repeated page with no
    total to prove the walk complete. A fetcher that never calls it reads
    as uncapped.

    >>> reset_fetch_failures(); note_capped(50); snapshot_info()["capped_total"]
    50

    Notes:
        A capped snapshot is partial, not failed: its rows are real, but a
        row missing from it is no evidence the posting closed. See
        store.sync_job_statuses's `capped` argument.
    """
    acct = _account()
    acct.capped, acct.total = True, total


def note_fill(rows: int, rates: dict[str, float]) -> None:
    """Record the per-field fill `rates` of `rows` raw listing rows, before
    any screening drops the unfilled ones; calls pool by row count.

    >>> reset_fetch_failures(); note_fill(2, {"title": 1.0}); note_fill(2, {"title": 0.5})
    >>> snapshot_info()["fill"], snapshot_info()["fill_rows"]
    ({'title': 0.75}, 4)
    """
    if not rows:
        return
    acct = _account()
    acct.fill_rows += rows
    for k, v in rates.items():
        acct.fill_sum[k] = acct.fill_sum.get(k, 0.0) + v * rows


class SnapshotFields(TypedDict, total=False):
    """Snapshot's keys, open so a harvest's BoardStats can extend them."""
    fetch_errors: int
    incomplete: bool
    capped: bool
    capped_total: int | None
    last_error: str | None
    fill: dict[str, float]
    fill_rows: int


class Snapshot(SnapshotFields, total=False, closed=True):
    """What `snapshot_info` reports (every key, though partial dicts read
    the same: a Tally's snap, a harvest's BoardStats, are views of this)."""


def snapshot_info() -> Snapshot:
    """This context's fetch accounting since the last reset, as the callers
    record it: the failure count, whether the snapshot is INCOMPLETE (a
    fetch failed partway) or CAPPED (truncated without an error), the
    LAST failure reported (None when there was none), and the pooled
    `fill` rates of the `fill_rows` raw rows (see note_fill; {} when none).

    >>> reset_fetch_failures(); snapshot_info()
    {'fetch_errors': 0, 'incomplete': False, 'capped': False, 'capped_total': None, 'last_error': None, 'fill': {}, 'fill_rows': 0}

    A failure outranks a cap: an incomplete snapshot closes nothing, so it
    is never also reported capped.

    >>> _ = fetch_failed("board p3", "timeout", indent=0)
    [!] board p3: timeout
    >>> note_capped(50); snapshot_info()
    {'fetch_errors': 1, 'incomplete': True, 'capped': False, 'capped_total': None, 'last_error': 'board p3: timeout', 'fill': {}, 'fill_rows': 0}
    """
    acct = _account()
    capped = acct.capped and not acct.n
    return {"fetch_errors": acct.n, "incomplete": acct.n > 0, "capped": capped,
            "capped_total": acct.total if capped else None,
            "last_error": acct.last,
            "fill": {k: v / acct.fill_rows for k, v in acct.fill_sum.items()},
            "fill_rows": acct.fill_rows}


class HostBreaker:
    """Hosts that keep refusing connections, skipped for a while.

    `trip(url)` records one refusal from the host of `url`; `dead(url)` is
    True once `trips` refusals have landed, each within `ttl` seconds of
    the one before, and stays True until `ttl` passes without another.
    A URL on the host answers, whatever its path, case or port:

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

    `clear(url)`, an answer from the host, restarts its count:

    >>> b = HostBreaker(ttl=60, trips=2)
    >>> b.trip("https://a.example/"); b.clear("https://a.example/x")
    >>> b.trip("https://a.example/"); b.dead("https://a.example/")
    False
    """

    def __init__(self, ttl: float, trips: int = 1) -> None:
        self.ttl, self.trips = ttl, trips
        self._hits: dict[str, tuple[float, int]] = {}  # host -> (last refusal, refusals in a row)

    def trip(self, url: str) -> None:
        host, now = host_of(url), time.time()
        if host:
            last, n = self._hits.get(host, (0.0, 0))
            self._hits[host] = (now, n + 1 if now - last < self.ttl else 1)

    def clear(self, url: str) -> None:
        self._hits.pop(host_of(url), None)

    def dead(self, url: str) -> bool:
        host = host_of(url)
        hit = self._hits.get(host)
        if hit and time.time() - hit[0] >= self.ttl:
            del self._hits[host]
            return False
        return hit is not None and hit[1] >= self.trips


#: This run's blocking hosts (`send`): REFUSAL_TRIPS 403/429s in a row.
_REFUSING = runstate.per_run(lambda: HostBreaker(ttl=REFUSAL_TTL_S, trips=REFUSAL_TRIPS))
