"""Candidate careers-page URLs for a company name, and the per-run fetch
pool that answers them.

The careers-page sniffer (sniffer.py) and the Workday probes (probes.py)
both walk the same candidate list for a name, and the stages of one name's
resolution -- careers sniff, root scan, lead sniff, diagnosis, Workday probe
-- each rebuild that list and fetch it again. Everything they share sits
here so neither imports the other: the URL generator, the dead-host memo,
the DNS verdict cache and the bounded page memo, all behind one lock, and
`_fetch_all`, the concurrent fetch that consults them.
"""

import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as fut_wait

import requests

from config import PROBE_TIMEOUT
from core.ats_signatures import FETCHABLE_HOST_RE
from core.names import domain_tokens
from scrapers.http import HEADERS, SESSION

# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("discovery.fetchpool")


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

# Cap on speculative GETs per name. A miss pays every one of them.
_URL_CAP = 12


def candidate_urls(name, careers_url="", patterns=_URL_PATTERNS, cap=_URL_CAP):
    """Careers-page URLs to fetch for `name`, best first, `cap` at most.

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
    if careers_url and not FETCHABLE_HOST_RE.search(careers_url):
        if patterns is _URL_PATTERNS:
            urls.append(careers_url)
        base_m = re.match(r"(https?://[^/]+)", careers_url)
        if base_m:
            for path in dict.fromkeys(p for _, p in patterns):
                urls.append(base_m.group(1) + path)
    toks = domain_tokens(name)
    for host, path in patterns:
        for tok in toks:
            urls.append(f"https://{host.format(tok=tok)}{path}")
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
_DEAD_HOSTS = {}
_DEAD_HOST_TTL = 15 * 60
_DEAD_HOSTS_LOCK = threading.Lock()


def _dead_host(url):
    """The host of `url` if a connection to it was refused within the TTL."""
    m = re.match(r"https?://([^/]+)", url or "")
    host = m.group(1).lower() if m else ""
    with _DEAD_HOSTS_LOCK:
        t = _DEAD_HOSTS.get(host)
        if t is not None and time.time() - t < _DEAD_HOST_TTL:
            return host
        _DEAD_HOSTS.pop(host, None)
    return ""


def _mark_dead_host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    if m:
        with _DEAD_HOSTS_LOCK:
            _DEAD_HOSTS[m.group(1).lower()] = time.time()


# Per-URL outcome memo, url -> (time recorded, Response or None). The
# stages of one name's resolution (careers sniff, root scan, lead sniff,
# diagnosis) each rebuild the candidate list and fetch it again, so a LIVE
# host answered the same GET up to seven times per name (sgs.com, intertek.
# com, and a 403ing infosys.com in the 2026-09-01 add-names runs). Same
# URL, same run, same answer: hand back the first one. Bounded so a long
# discovery run can't hoard page bodies.
_PAGE_MEMO = {}
_PAGE_MEMO_CAP = 512
_PAGE_MEMO_MAX_BYTES = 2 * 1024 * 1024


def _memo_get(url):
    with _DEAD_HOSTS_LOCK:
        hit = _PAGE_MEMO.get(url)
        if hit is None:
            return False, None
        if time.time() - hit[0] >= _DEAD_HOST_TTL:
            del _PAGE_MEMO[url]
            return False, None
        return True, hit[1]


def _memo_put(url, resp):
    if resp is not None and len(resp.content or b"") > _PAGE_MEMO_MAX_BYTES:
        return
    with _DEAD_HOSTS_LOCK:
        if len(_PAGE_MEMO) >= _PAGE_MEMO_CAP:
            oldest = min(_PAGE_MEMO, key=lambda u: _PAGE_MEMO[u][0])
            del _PAGE_MEMO[oldest]
        _PAGE_MEMO[url] = (time.time(), resp)


def _fetch_page(url, timeout=PROBE_TIMEOUT):
    """GET one careers-page candidate. Short timeout: most are speculative
    domain/path guesses that 404 or don't resolve; a real careers page
    answers fast. Returns the Response on 200 with real content, else None.
    Outcomes are memoized per URL for the run (see _PAGE_MEMO), and a host
    that refused a connection is skipped outright (see _DEAD_HOSTS)."""
    known, resp = _memo_get(url)
    if known:
        return resp
    if _dead_host(url):
        _log.debug("skip %s: host refused a connection earlier this run", url)
        return None
    try:
        r = SESSION.get(url, timeout=timeout, headers=HEADERS, allow_redirects=True)
        resp = r if r.status_code == 200 and len(r.text) >= 300 else None
    except requests.exceptions.ConnectionError:
        # requests' ConnectionError covers DNS failure, SSLError and
        # ConnectTimeout; ReadTimeout is a Timeout, not a ConnectionError.
        _mark_dead_host(url)
        return None
    except Exception:
        return None
    _memo_put(url, resp)
    return resp


# Per-host DNS verdicts for the run, host -> (time recorded, resolved?).
# Most candidate hosts are name-guesses that do not exist; each path on one
# used to pay the OS resolver's full failure latency, and the candidates for
# a name are fetched CONCURRENTLY, so a dead host's five paths all paid it
# before _DEAD_HOSTS could learn anything. On a machine whose resolver is
# refusing or timing out (VPN plus a second adapter, 2026-09-02 reresolve:
# 32 names abandoned by the stall watchdog at once, 2 of 50 resolved), that
# was minutes per name. Resolve each host ONCE, bounded, before any GET.
_DNS_CACHE = {}
_DNS_TIMEOUT = 4.0


def _host_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _resolves(host):
    """Whether `host` has an address, cached per run. A failure marks the
    host dead for _fetch_page (see _DEAD_HOSTS); a success is remembered so
    the next stage's rebuilt candidate list does not ask again."""
    with _DEAD_HOSTS_LOCK:
        hit = _DNS_CACHE.get(host)
        if hit is not None and time.time() - hit[0] < _DEAD_HOST_TTL:
            return hit[1]
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ok = True
    except OSError:
        ok = False
    with _DEAD_HOSTS_LOCK:
        _DNS_CACHE[host] = (time.time(), ok)
    if not ok:
        _mark_dead_host(f"https://{host}/")
        _log.debug("skip host %s: does not resolve", host)
    return ok


def _drop_unresolvable(urls, timeout=_DNS_TIMEOUT):
    """`urls` minus every one whose host is known dead or fails a bounded
    DNS lookup. Distinct hosts are resolved concurrently; a host the
    resolver has not answered within `timeout` is skipped for THIS call
    (not marked dead: a slow resolver is not a missing name) and its lookup
    thread is left to finish on its own."""
    hosts = {}
    for u in urls:
        h = _host_of(u)
        if h:
            hosts.setdefault(h, []).append(u)
    todo = [h for h in hosts if not _dead_host(f"https://{h}/")]
    with _DEAD_HOSTS_LOCK:
        todo = [h for h in todo
                if not (_DNS_CACHE.get(h) and
                        time.time() - _DNS_CACHE[h][0] < _DEAD_HOST_TTL)]
    slow = set()
    if todo:
        ex = ThreadPoolExecutor(max_workers=min(8, len(todo)))
        futs = {ex.submit(_resolves, h): h for h in todo}
        _done, pending = fut_wait(futs, timeout=timeout)
        for f in pending:
            slow.add(futs[f])
            _log.debug("skip host %s this pass: resolver silent for %.0fs",
                       futs[f], timeout)
        ex.shutdown(wait=False, cancel_futures=True)
    return [u for u in urls
            if _host_of(u) not in slow and not _dead_host(u)]


def _fetch_all(urls):
    """Fetch candidates concurrently (a miss otherwise pays ~12 sequential
    GETs — the dominant per-candidate latency in a bulk run); results are
    evaluated in priority order regardless of completion order. Every
    requested URL is a key of the result; one on a host that does not
    resolve (see _drop_unresolvable) maps to None without a GET.

    These are GUESSES — `<token>.io`, `<token>.co`, `careers.<token>.com` —
    so their robots.txt failures are expected and say nothing worth logging;
    `robots.quiet()` keeps the notice for hosts we actually mean to crawl.
    """
    from scrapers import robots
    out = dict.fromkeys(urls)
    live = _drop_unresolvable(urls)
    if not live:
        return out
    with robots.quiet():
        with ThreadPoolExecutor(max_workers=min(8, len(live))) as pool:
            out.update(zip(live, pool.map(_fetch_page, live)))
    return out
