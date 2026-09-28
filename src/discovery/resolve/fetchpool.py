"""Candidate careers-page URLs for a company name, and the per-run fetch
pool that answers them.

The careers-page sniffer (sniffer.py) and the Workday probes (probes.py)
both walk the same candidate list for a name, and the stages of one name's
resolution -- careers sniff, root scan, lead sniff, diagnosis, Workday probe
-- each rebuild that list and fetch it again. Everything they share sits
here so neither imports the other: the URL generator, the run's dead-host
memo, DNS verdict cache and bounded page memo, and `_fetch_all`, the
concurrent fetch that consults them.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import socket
import time
from collections.abc import Callable
from typing import Any

from src import config, runstate
from src.config import PROBE_TIMEOUT
from src.match.names import domain_tokens
from src.net import http
from src.net.http import HEADERS, HostBreaker, Unreachable
from src.net.util import host_of, origin_of

# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("src.discovery.resolve.fetchpool")

# A careers_url on a fetchable vendor's host names a board, not the
# company's site: resolve.board reads that board off the URL itself
# (signatures.detect), so it is never sniffed, nor its origin's careers paths.
_FETCHABLE_HOST_RE = config.hosts_re(config.FETCHABLE_HOSTS)


# ─── Candidate careers-page URLs ─────────────────────────────────────────
#
# (host, path) patterns in priority order, applied breadth-first over the
# name's domain tokens (names.domain_tokens) so every token's best guess is
# tried before any token's worst -- including the non-.com TLDs common for
# neurotech / deep-tech startups. The root ("/") sits second: a company
# whose ATS badge is on the homepage has no dedicated /careers page, and a
# Workday-hosted careers site redirects straight from it.
_URL_PATTERNS = [
    ("www.{tok}.com", "/careers"),
    ("www.{tok}.com", "/"),
    ("www.{tok}.com", "/jobs"),
    ("careers.{tok}.com", "/"),
    ("www.{tok}.com", "/en/jobs"),
    ("{tok}.io", "/careers"),
    ("{tok}.ai", "/careers"),
    ("{tok}.bio", "/careers"),
    ("{tok}.xyz", "/careers"),
    ("{tok}.health", "/careers"),
    ("{tok}.co", "/careers"),
    ("jobs.{tok}.com", "/"),
]
ROOT_PATTERNS = [p for p in _URL_PATTERNS if p == ("www.{tok}.com", "/")]


def candidate_urls(name: str, careers_url: str = "",
                   patterns: list[tuple[str, str]] = _URL_PATTERNS,
                   cap: int = 12) -> list[str]:
    """Careers-page URLs to fetch for `name`, best first, `cap` at most:
    each is a speculative GET, and a miss pays every one of them.

    >>> for u in candidate_urls("Merakris Therapeutics")[:4]:
    ...     print(u)
    https://www.merakristherapeutics.com/careers
    https://www.merakris.com/careers
    https://www.merakristherapeutics.com/
    https://www.merakris.com/

    A recorded careers_url (e.g. capture.py's JSON-LD hint) is a far better
    base than a name-guess, so it goes first and its host's other paths
    come next -- oxb.com / united-imaging.com resolve even though the name
    never would:

    >>> for u in candidate_urls("Acme", "https://acme.io/careers")[:5]:
    ...     print(u)
    https://acme.io/careers
    https://acme.io/
    https://acme.io/jobs
    https://acme.io/en/jobs
    https://www.acme.com/careers

    ...unless it is itself a dead slug-guess against a JSON ATS, already
    covered by the slug probes upstream:

    >>> candidate_urls("Acme", "https://boards.greenhouse.io/acme")[0]
    'https://www.acme.com/careers'

    `patterns` narrows the list; ROOT_PATTERNS is the bare-homepage subset
    the failure path scans (see sniffer._scan_root):

    >>> candidate_urls("Merakris Therapeutics", patterns=ROOT_PATTERNS)
    ['https://www.merakristherapeutics.com/', 'https://www.merakris.com/']
    >>> candidate_urls("Acme", "https://acme.io/careers", patterns=ROOT_PATTERNS)[0]
    'https://acme.io/'

    The list is deduped and capped, and a name with no domain tokens and
    no hint has nothing to try:

    >>> len(candidate_urls("A Very Long Multi Word Company Name Ltd")) <= 12
    True
    >>> candidate_urls("")
    []
    """
    urls = []
    if careers_url and not _FETCHABLE_HOST_RE.search(careers_url):
        if patterns is _URL_PATTERNS:
            urls.append(careers_url)
        base = origin_of(careers_url)
        if base:
            urls += [base + path for path in dict.fromkeys(p for _, p in patterns)]
    toks = domain_tokens(name)
    urls += [f"https://{host.format(tok=tok)}{path}" for host, path in patterns for tok in toks]
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if cap and len(out) >= cap:
            break
    return out


# Hosts that refused a connection outright (DNS failure, TLS handshake
# failure, connect timeout) this run, host -> time recorded. The candidate
# list tries ~5 paths per name-guessed host, and the miss path re-derives
# the list up to three more times (root scan, careers sniff, diagnosis), so
# one dead host cost 7 identical GETs per name in the 2026-09-01 add-names
# run. A refused connection says nothing path-specific: skip the host for a
# while. HTTP errors and READ timeouts are not cached — a slow or 404ing
# host may still answer another path.
_DEAD_HOST_TTL = 15 * 60
_DEAD_HOSTS = runstate.per_run(functools.partial(HostBreaker, ttl=_DEAD_HOST_TTL))


# The run's per-URL outcome memo, url -> (time recorded, Response or None).
# The stages of one name's resolution (careers sniff, root scan, lead
# sniff, diagnosis) each rebuild the candidate list and fetch it again, so
# a LIVE host answered the same GET up to seven times per name (sgs.com,
# intertek.com, and a 403ing infosys.com in the 2026-09-01 add-names runs).
# Same URL, same run, same answer: hand back the first one. Bounded (see
# _memo_put) so a long discovery run can't hoard page bodies.
_PAGE_MEMO: Callable[[], dict[str, tuple[float, Any]]] = \
    runstate.per_run(dict)


def _memo_get(url: str) -> tuple[bool, Any]:
    memo = _PAGE_MEMO()
    hit = memo.get(url)
    if hit is None:
        return False, None
    if time.time() - hit[0] >= _DEAD_HOST_TTL:
        del memo[url]
        return False, None
    return True, hit[1]


def _memo_put(url: str, resp: Any) -> None:
    """Remember `url`'s outcome: 512 URLs at most, the oldest dropped
    first, and no body over 2 MB."""
    if resp is not None and len(resp.content or b"") > 2 * 1024 * 1024:
        return
    memo = _PAGE_MEMO()
    if len(memo) >= 512:
        del memo[min(memo, key=lambda u: memo[u][0])]
    memo[url] = (time.time(), resp)


async def _fetch_page(url: str,
                      timeout: float | tuple[float, float] = PROBE_TIMEOUT
                      ) -> Any:
    """GET one careers-page candidate. Short timeout: most are speculative
    domain/path guesses that 404 or don't resolve; a real careers page
    answers fast. Returns the Response on 200 with real content (its text
    read off the loop), else None. Outcomes are memoized per URL for the
    run (see _PAGE_MEMO), and a host that refused a connection is skipped
    outright (see _DEAD_HOSTS)."""
    known, resp = _memo_get(url)
    if known:
        return resp
    if _DEAD_HOSTS().dead(url):
        _log.debug("skip %s: host refused a connection earlier this run", url)
        return None
    try:
        r = await http.send("GET", url, timeout=timeout, headers=HEADERS,
                            allow_redirects=True)
        resp = (r if r.status_code == 200
                and len(await asyncio.to_thread(lambda: r.text)) >= 300 else None)
    except Unreachable:
        # Unreachable (requests' ConnectionError) covers DNS failure,
        # SSLError and ConnectTimeout; ReadTimeout is a Timeout, not one.
        _DEAD_HOSTS().trip(url)
        return None
    except Exception:
        return None
    _memo_put(url, resp)
    return resp


# The run's per-host DNS verdicts, host -> (time recorded, resolved?). Most
# candidate hosts are name-guesses that do not exist; each path on one used
# to pay the OS resolver's full failure latency, and the candidates for a
# name are fetched CONCURRENTLY, so a dead host's five paths all paid it
# before _DEAD_HOSTS could learn anything. On a machine whose resolver is
# refusing or timing out (VPN plus a second adapter, 2026-09-02 reresolve:
# 32 names abandoned by the stall watchdog at once, 2 of 50 resolved), that
# was minutes per name. Resolve each host ONCE, bounded, before any GET.
_DNS_CACHE: Callable[[], dict[str, tuple[float, bool]]] = runstate.per_run(dict)


async def _drop_unresolvable(urls: list[str], timeout: float = 4.0) -> list[str]:
    """`urls` minus every one whose host is known dead or fails a bounded
    DNS lookup. Distinct hosts are resolved concurrently, 8 at a time, each
    lookup off the loop and each once per run: a failure marks the host
    dead for _fetch_page (see _DEAD_HOSTS). A host the resolver has not
    answered within `timeout` is skipped for THIS call (not marked dead: a
    slow resolver is not a missing name), a lookup under way still records
    its verdict when it lands, and one not yet begun never begins.

    Notes:
        The lookup thread posts its verdict before it returns, and
        resolve() awaits that thread directly, so a lookup that landed
        during a loop stall counts as done when the timeout is read. A
        task hop between them (loop.getaddrinfo in its own task) lost
        that race: 4-16 hosts per syn150 run were wrongly "silent"."""
    hosts: dict[str, list[str]] = {}
    for u in urls:
        h = host_of(u)
        if h:
            hosts.setdefault(h, []).append(u)
    dead, verdicts = _DEAD_HOSTS(), _DNS_CACHE()

    def known(h: str) -> bool:
        return bool(verdicts.get(h) and time.time() - verdicts[h][0] < _DEAD_HOST_TTL)

    todo = [h for h in hosts if not dead.dead(f"https://{h}/") and not known(h)]
    slow = set()
    if todo:
        loop, slots = asyncio.get_running_loop(), asyncio.Semaphore(8)

        def record(h: str, ok: bool) -> None:
            verdicts[h] = (time.time(), ok)
            if not ok:
                dead.trip(f"https://{h}/")
                _log.debug("skip host %s: does not resolve", h)

        def lookup(h: str) -> None:
            try:
                socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
                ok = True
            except OSError:
                ok = False
            loop.call_soon_threadsafe(record, h, ok)

        async def resolve(h: str) -> None:
            async with slots:
                if not known(h):
                    await asyncio.to_thread(lookup, h)
        lookups = {asyncio.ensure_future(resolve(h)): h for h in todo}
        _done, pending = await asyncio.wait(lookups, timeout=timeout)
        for f in pending:
            f.cancel()
            slow.add(lookups[f])
            _log.debug("skip host %s this pass: resolver silent for %.0fs",
                       lookups[f], timeout)
    return [u for u in urls
            if host_of(u) not in slow and not dead.dead(u)]


async def _fetch_all(urls: list[str]) -> dict[str, Any]:
    """Fetch candidates concurrently (a miss otherwise pays ~12 sequential
    GETs — the dominant per-candidate latency in a bulk run); results are
    evaluated in priority order regardless of completion order. Every
    requested URL is a key of the result; one on a host that does not
    resolve (see _drop_unresolvable) maps to None without a GET. At most 8
    GETs at once.

    These are GUESSES — `<token>.io`, `<token>.co`, `careers.<token>.com` —
    so their robots.txt failures are expected and say nothing worth logging;
    `robots.quiet()` keeps the notice for hosts we actually mean to crawl.
    """
    from src.net import robots
    out = dict.fromkeys(urls)
    live = await _drop_unresolvable(urls)
    if not live:
        return out
    slots = asyncio.Semaphore(8)

    async def fetch(url: str) -> Any:
        async with slots:
            return await _fetch_page(url)

    with robots.quiet():
        out.update(zip(live, await asyncio.gather(*map(fetch, live))))
    return out
