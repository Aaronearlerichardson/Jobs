"""robots.txt support (RFC 9309).

A site's robots.txt states which paths automated clients are asked not to
fetch, how fast to go (`Crawl-delay`), and where its sitemaps live. It is a
request, not a lock — nothing here is enforced by the server — but honoring
it is what separates a well-behaved crawler from an abusive one, and "we
parse and honor robots.txt" answers most of the responsible-crawling
question in one line.

Fetched once per host and cached for the run (src/runstate.py): the
requests that want one host's rules at once share one fetch, and the
per-host crawl delay (net.http.LIMITER) spaces requests to the SAME host
without holding up the others.

Failure semantics follow RFC 9309 §2.3.1:
  * 2xx            -> parse and obey.
  * 4xx (incl 401/403/404) -> no restrictions; crawl freely.
  * 5xx            -> treat as "disallow all" while the site is unwell —
                      a server in trouble is the last one to hammer.
  * network error  -> fail OPEN (allow) rather than stall the crawl, and say
                      so once per host — unless the host never resolved, in
                      which case there is no server to be polite to and
                      nothing will be crawled.

The fetch timeout is split into (connect, read) — see ROBOTS_CONNECT_TIMEOUT
in src/config/policy.py. Connect is short because dead name-guesses hang there; read is
generous because a slow-but-real server is the case worth waiting for.

Parsing and matching are `protego`'s, through `_HostRules.parse`, whose
doctests pin the rules this module relies on: `*` and `$` in a path, the
longest match winning with Allow breaking a tie, a user agent's repeated
groups merging, and a product token matching at a word boundary.

`[policy] respect_robots = false` turns all of it off: `allowed` is then True
for every URL, and neither it nor `wait_turn` fetches a robots.txt.
`robots_exempt_hosts` does the same for the hosts it names.

Notes:
    The path matching was hand-written here until 2026-09-30, because
    `urllib.robotparser`'s is `filename.startswith(rule.path)` with the first
    rule in file order winning. Stdlib treats `*` and `$` as literal
    characters: Hacker News publishes `Allow: /*.json$` + `Disallow: /`, a
    deliberate carve-out for the API this crawler uses, and stdlib read it as
    "disallow everything", so the HN source silently returned nothing. And it
    takes the first match in file order, so a host that writes `Disallow: /`
    before its `Allow:` carve-outs was over-blocked, while wildcard
    `Disallow:` patterns never matched at all. protego does both to the RFC
    and adds no dependency of its own; the hand-written matcher agreed with
    it on every decision from a differential run over well-formed files
    (docs/dependency-scan.md) and departed from the RFC in two places, both
    now fixed by the swap.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import Iterator

from protego import Protego

from src import config, runstate
from . import http
from .util import host_of, origin_key


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Suppress the per-host "unreachable" notice for SPECULATIVE probes.

    Discovery guesses hostnames from a company name — `red.io`, `410.co`,
    `united.ai` — and fetches them to find out whether they exist. Most do
    not. Announcing "proceeding without restrictions" there describes a
    politeness decision that is never acted on: nothing is crawled, because
    the page fetch fails for the same reason robots.txt did. Reported
    anyway, it buries the case the notice exists for — a host we believe is
    a real board, whose robots.txt we could not read before crawling it.

    Deliberately run-wide rather than per task: the speculative fetches
    run as tasks of their own (resolve.fetchpool._fetch_all), and the point
    is to cover every one of them. Another run's notices are its own.
    """
    rules = CACHE()
    rules.quiet += 1
    try:
        yield
    finally:
        rules.quiet -= 1


def _is_dns_failure(exc: BaseException | None, _depth: int = 6) -> bool:
    """True when `exc` bottoms out in a name-resolution error — i.e. the host
    does not exist, as opposed to a server that refused, hung, or failed TLS.

    The failure arrives wrapped (net.http raises requests' ConnectionError
    from aiohttp's DNS error, from the gaierror), so this walks the chain:

    >>> wrapped = OSError("Cannot connect to host")
    >>> wrapped.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    >>> _is_dns_failure(wrapped), _is_dns_failure(TimeoutError("timed out"))
    (True, False)

    Depth-bounded, and proof against a cycle:

    >>> a, b = Exception("a"), Exception("b")
    >>> a.__cause__, b.__cause__ = b, a
    >>> _is_dns_failure(a)
    False
    >>> def chain(links):
    ...     exc = socket.gaierror(11001, "getaddrinfo failed")
    ...     for _ in range(links):
    ...         outer, outer.__cause__ = OSError("wrapped"), exc
    ...         exc = outer
    ...     return exc
    >>> _is_dns_failure(chain(3)), _is_dns_failure(chain(9))
    (True, False)
    """
    seen = set()
    while exc is not None and _depth > 0 and id(exc) not in seen:
        if isinstance(exc, socket.gaierror):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
        _depth -= 1
    return False


class _HostRules:
    """What one host's robots.txt asks of `user_agent`; no `protego` means it asks nothing."""

    __slots__ = ("protego", "user_agent", "disallow_all")

    def __init__(self, protego: Protego | None = None, user_agent: str = "",
                 disallow_all: bool = False) -> None:
        self.protego = protego
        self.user_agent = user_agent
        self.disallow_all = disallow_all

    @classmethod
    def parse(cls, text: str, user_agent: str) -> _HostRules:
        """The rules in a robots.txt body, as `user_agent` reads them.

        >>> ua = "Mozilla/5.0 (compatible; JobBot/1.0)"
        >>> def ask(text, *paths):
        ...     rules = _HostRules.parse(text, ua)
        ...     return [rules.allows("https://x.test" + p) for p in paths]

        `*` and a closing `$` are wildcards. Hacker News' carve-out reopens
        its JSON API only:

        >>> ask("User-agent: *\\nAllow: /*.json$\\nDisallow: /", "/v0/item/1.json", "/v0/other")
        [True, False]

        The longest matching rule wins, Allow winning a tie, and a query
        string is part of what is matched:

        >>> ask("User-agent: *\\nDisallow: /\\nAllow: /api/\\nDisallow: /api/internal/\\n"
        ...     "Disallow: /a\\nAllow: /a", "/api/x", "/api/internal/x", "/a")
        [True, False, True]
        >>> ask("User-agent: *\\nDisallow: /*?feed=", "/?feed=x", "/x"), ask("User-agent: *\\nDisallow:", "/x")
        ([False, True], [True])

        A user agent's groups merge wherever they stand, and the product
        token has to start a word: `bot` is not `JobBot`.

        >>> ask("User-agent: jobbot\\nDisallow: /a\\n\\nUser-agent: *\\nDisallow: /c\\n\\n"
        ...     "User-agent: jobbot\\nDisallow: /b", "/a", "/b", "/c")
        [False, False, True]
        >>> ask("User-agent: bot\\nDisallow: /", "/"), ask("User-agent: jobbot\\nDisallow: /", "/")
        ([True], [False])

        A `$` counts toward its rule's length, and a Crawl-delay line ends the
        group it follows, so the next User-agent starts another:

        >>> ask("User-agent: *\\nAllow: /x\\nDisallow: /x$", "/x", "/x/y")
        [False, True]
        >>> ask("User-agent: jobbot\\nCrawl-delay: 5\\nUser-agent: other\\nDisallow: /", "/")
        [True]

        Crawl-delay and the sitemaps come from the same group and file:

        >>> r = _HostRules.parse("Sitemap: https://x.test/s.xml\\nUser-agent: *\\nCrawl-delay: 2", ua)
        >>> r.crawl_delay, r.sitemaps
        (2.0, ['https://x.test/s.xml'])
        >>> _HostRules.parse("User-agent: *\\nCrawl-delay: soon", ua).crawl_delay is None
        True
        >>> _HostRules.parse("User-agent: *\\nCrawl-delay: 0.5", ua).crawl_delay
        0.5
        """
        return cls(Protego.parse(text), user_agent)

    def allows(self, url: str) -> bool:
        """May `user_agent` fetch `url`?"""
        if self.disallow_all:
            return False
        return self.protego is None or self.protego.can_fetch(url, self.user_agent)

    @property
    def crawl_delay(self) -> float | None:
        """Seconds the host asks for between requests, or None."""
        return self.protego.crawl_delay(self.user_agent) if self.protego else None

    @property
    def sitemaps(self) -> list[str]:
        """Sitemap URLs the file lists."""
        return list(self.protego.sitemaps) if self.protego else []


class RobotsCache:
    """Per-host robots.txt rules, fetched lazily and cached."""

    def __init__(self, user_agent: str | None = None, ttl: float = 3600) -> None:
        self.user_agent = user_agent or config.USER_AGENT
        self.ttl = ttl          # seconds a parsed robots.txt stays good
        # origin -> (started, the fetch's Task)
        self._fetches: dict[str, tuple[float, asyncio.Task[_HostRules]]] = {}
        self.quiet = 0          # open quiet() blocks

    # -- internals --------------------------------------------------------

    async def _fetch(self, origin: str) -> _HostRules:
        """Fetch + parse one host's robots.txt. Never raises."""
        try:
            r = await http.send("GET", f"{origin}/robots.txt", polite=False,
                                timeout=(config.ROBOTS_CONNECT_TIMEOUT,
                                         config.ROBOTS_READ_TIMEOUT),
                                headers=http.HEADERS, allow_redirects=True)
        except Exception as e:
            # "Proceeding without restrictions" is a claim about how we treat
            # a SERVER we could not ask. A hostname that does not resolve has
            # no server to be impolite to and will never be crawled — most
            # candidates here are speculative `careers.<name>.com` guesses —
            # so saying it there is noise that buries the real cases.
            if not _is_dns_failure(e) and not self.quiet:
                print(f"    [robots] {origin}: unreachable ({type(e).__name__}); "
                      f"proceeding without restrictions")
            return _HostRules()
        if 500 <= r.status_code < 600:
            return _HostRules(disallow_all=True)
        if r.status_code >= 400:
            return _HostRules()                    # nothing to obey
        try:
            return _HostRules.parse(r.text, self.user_agent)
        except Exception:
            return _HostRules()

    async def _rules(self, url: str) -> _HostRules | None:
        origin = origin_key(url)
        if not origin:
            return None
        # One fetch per origin, even when requests arrive together. A sniff
        # fans ~8 candidate PATHS across the same host at once; without this
        # every one of them missed the still-empty cache and fetched its own
        # copy of robots.txt — 8x the requests, and 8x the wait when the host
        # is one that hangs until the timeout. Shielded: a caller that is
        # cancelled leaves the fetch to the others.
        got = self._fetches.get(origin)
        if got is None or time.monotonic() - got[0] >= self.ttl:
            got = self._fetches[origin] = (
                time.monotonic(),
                asyncio.get_running_loop().create_task(self._fetch(origin)))
        return await asyncio.shield(got[1])

    # -- public API -------------------------------------------------------

    @staticmethod
    def host_exempt(url: str) -> bool:
        """Is `url`'s host on config.ROBOTS_EXEMPT_HOSTS? Exact match, or a
        dotted entry matching the host's suffix (".peopleadmin.com" covers
        unc.peopleadmin.com). Case-insensitive; the port is ignored.

        >>> from src import config
        >>> _saved = getattr(config, "ROBOTS_EXEMPT_HOSTS", ())
        >>> config.ROBOTS_EXEMPT_HOSTS = ("api.smartrecruiters.com", ".peopleadmin.com")
        >>> RobotsCache.host_exempt("https://api.smartrecruiters.com/v1/companies/x/postings")
        True
        >>> RobotsCache.host_exempt("https://unc.peopleadmin.com/postings/search.atom")
        True
        >>> RobotsCache.host_exempt("https://peopleadmin.com/")
        False
        >>> RobotsCache.host_exempt("https://jobs.smartrecruiters.com/x")
        False
        >>> RobotsCache.host_exempt("https://aa.smartrecruiters.com/x"), RobotsCache.host_exempt("")
        (False, False)
        >>> config.ROBOTS_EXEMPT_HOSTS = _saved
        """
        host = host_of(url)
        if not host:
            return False
        for entry in getattr(config, "ROBOTS_EXEMPT_HOSTS", ()):
            if entry.startswith("."):
                if host.endswith(entry):
                    return True
            elif host == entry:
                return True
        return False

    async def allowed(self, url: str) -> bool:
        """May we fetch `url`? True when robots is absent/permissive, or
        when the host is exempted in the profile (see host_exempt) — the
        exemption skips the robots.txt fetch for that request entirely,
        while wait_turn still paces the host."""
        if not getattr(config, "RESPECT_ROBOTS", True):
            return True
        if self.host_exempt(url):
            return True
        rules = await self._rules(url)
        try:
            return rules is None or rules.allows(url)
        except Exception:
            return True

    async def crawl_delay(self, url: str) -> float | None:
        """Seconds this host asks us to wait between requests, or None."""
        rules = await self._rules(url)
        return rules.crawl_delay if rules else None

    async def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs the host advertises — a discovery hint, since this
        is exactly where sites publish them."""
        rules = await self._rules(url)
        return rules.sitemaps if rules else []

    async def wait_turn(self, url: str) -> None:
        """Wait as long as this host's Crawl-delay requires (a turn on
        net.http.LIMITER): requests to the SAME host queue up, while other
        hosts keep going."""
        if not getattr(config, "RESPECT_ROBOTS", True):
            return
        delay = await self.crawl_delay(url)
        if delay:
            await http.LIMITER.wait(url, delay)


class RobotsDisallowed(Exception):
    """Raised instead of fetching a path robots.txt asks us to leave alone.

    Fetchers already try/except around their requests and report the reason,
    so this surfaces in the crawl log the same way a 404 would."""


#: This run's cache: one robots.txt per host per hour, however many
#: fetchers are running.
CACHE = runstate.per_run(RobotsCache)
