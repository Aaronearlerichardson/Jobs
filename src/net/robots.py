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

Path matching is implemented here rather than taken from
`urllib.robotparser`, whose matcher is `filename.startswith(rule.path)` with
first-rule-in-file-order winning. That breaks RFC 9309 in both directions:

  * §2.2.3 requires `*` (any sequence) and `$` (end of match) — stdlib
    treats both as literal characters. Hacker News publishes
    `Allow: /*.json$` + `Disallow: /`, which is a deliberate carve-out for
    exactly the API this crawler uses; stdlib reads it as "disallow
    everything" and the HN source silently returned nothing on every crawl.
  * §2.2.2 requires the MOST SPECIFIC (longest) match to win, with Allow
    breaking ties. Stdlib takes the first match in file order, so a host
    that writes `Disallow: /` before its `Allow:` carve-outs is over-blocked,
    and — the direction that actually matters for politeness — wildcard
    `Disallow:` patterns never match at all, so we would fetch paths the
    host asked us to leave alone.

RobotFileParser is still used for `Crawl-delay` (its parsing of that is
fine, and it is not part of the RFC's matching rules).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import time
from collections.abc import Iterable, Iterator
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from src import config, runstate
from . import http
from .util import host_of, origin_of


# --------------------------------------------------------------------------- #
#  RFC 9309 §2.2 path matching                                                 #
# --------------------------------------------------------------------------- #

def _pattern_to_re(path: str) -> re.Pattern[str]:
    """A robots.txt path pattern -> a compiled prefix regex.

    `*` matches any sequence; a trailing `$` anchors the end of the URL path.
    Everything else is literal. An empty pattern matches nothing (an empty
    `Disallow:` means "no restriction", handled by the caller).

    >>> [bool(_pattern_to_re(pat).match(path)) for pat, path in (
    ...     ("/a.b", "/a.b"), ("/a.b", "/axb"), ("/a*b", "/axxxb"), ("/a*b", "/ab"),
    ...     ("/p", "/prefix/deep"), ("/*?feed=", "/?feed=x"))]
    [True, False, True, True, True, True]
    >>> bool(_pattern_to_re("/x$").match("/x")), bool(_pattern_to_re("/x$").match("/x/y"))
    (True, False)
    """
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    body = "".join(".*" if ch == "*" else re.escape(ch) for ch in path)
    return re.compile(body + ("$" if anchored else ""))


class _Group:
    """One `User-agent:` group's rules, in the order they were written."""

    __slots__ = ("agents", "rules")

    def __init__(self) -> None:
        self.agents: list[str] = []
        self.rules: list[tuple[int, bool, re.Pattern[str]]] = []  # (specificity, allow, pattern)

    def add_rule(self, path: str, allow: bool) -> None:
        # An empty `Disallow:` is the documented way to say "allow all" —
        # it is not a rule, it is the absence of one.
        if not path and not allow:
            return
        self.rules.append((len(path.rstrip("$")), allow, _pattern_to_re(path)))

    def allows(self, path: str) -> bool:
        """RFC 9309 §2.2.2: the longest matching pattern wins; Allow wins a
        tie. No match at all means allowed.

        Hacker News' carve-out (§2.2.3 wildcards) reopens its JSON API only:

        >>> [hn] = parse_groups("User-agent: *\\nAllow: /*.json$\\nDisallow: /")
        >>> hn.allows("/v0/item/38912345.json"), hn.allows("/v0/whatever")
        (True, False)
        >>> [g] = parse_groups("User-agent: *\\nDisallow: /\\nAllow: /api/\\n"
        ...                    "Disallow: /api/internal/\\nDisallow: /a\\nAllow: /a")
        >>> g.allows("/api/x"), g.allows("/api/internal/x"), g.allows("/a")
        (True, False, True)
        """
        best_len, best_allow = -1, True
        for length, allow, rx in self.rules:
            if rx.match(path) and (length > best_len
                                   or (length == best_len and allow)):
                best_len, best_allow = length, allow
        return best_allow


def parse_groups(text: str | None) -> list[_Group]:
    """robots.txt body -> [_Group]. Consecutive `User-agent:` lines share one
    group, per §2.2.1; comments are dropped, and an empty `Disallow:` is no
    rule at all.

    >>> [g] = parse_groups("# hi\\n\\nUser-agent: a  # us\\nUser-agent: b\\n"
    ...                    "Disallow: /x  # no\\nDisallow:")
    >>> g.agents, len(g.rules), g.allows("/x")
    (['a', 'b'], 1, False)
    >>> parse_groups("# just a comment"), parse_groups(None)
    ([], [])
    """
    groups: list[_Group] = []
    current: _Group | None = None
    expecting_agent = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if current is None or not expecting_agent:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
            expecting_agent = True
        elif field in ("allow", "disallow") and current is not None:
            current.add_rule(value, field == "allow")
            expecting_agent = False
    return groups


def _match_group(groups: list[_Group], user_agent: str | None) -> _Group | None:
    """The group governing `user_agent`: the longest matching product token,
    else the `*` group, else None (= unrestricted).

    >>> groups = parse_groups("User-agent: Googlebot\\nAllow: /\\n\\nUser-agent: *\\nDisallow: /")
    >>> [_match_group(groups, ua).allows("/x") for ua in ("Googlebot/2.1", "Chrome")]
    [True, False]
    >>> _match_group([], "Chrome") is None
    True
    """
    ua = (user_agent or "").lower()
    best: _Group | None = None
    best_len = -1
    wildcard: _Group | None = None
    for g in groups:
        for agent in g.agents:
            if agent == "*":
                wildcard = wildcard or g
            elif agent and agent in ua and len(agent) > best_len:
                best, best_len = g, len(agent)
    return best or wildcard


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

    Depth-bounded, because an exception chain can be cyclic:

    >>> a, b = Exception("a"), Exception("b")
    >>> a.__cause__, b.__cause__ = b, a
    >>> _is_dns_failure(a)
    False
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
    __slots__ = ("parser", "group", "disallow_all", "sitemaps")

    def __init__(self, parser: RobotFileParser | None = None, group: _Group | None = None,
                 disallow_all: bool = False, sitemaps: Iterable[str] = ()) -> None:
        self.parser = parser          # RobotFileParser: Crawl-delay only
        self.group = group            # _Group: the RFC-compliant matcher
        self.disallow_all = disallow_all
        self.sitemaps = list(sitemaps)


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
        parser = RobotFileParser()
        try:
            parser.parse(r.text.splitlines())          # Crawl-delay only
            group = _match_group(parse_groups(r.text), self.user_agent)
        except Exception:
            return _HostRules()
        sitemaps = [ln.split(":", 1)[1].strip()
                    for ln in r.text.splitlines()
                    if ln.strip().lower().startswith("sitemap:")]
        return _HostRules(parser=parser, group=group, sitemaps=sitemaps)

    async def _rules(self, url: str) -> _HostRules | None:
        origin = origin_of(url)
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
        >>> config.ROBOTS_EXEMPT_HOSTS = _saved
        """
        host = host_of(url)
        if not host:
            return False
        for entry in getattr(config, "ROBOTS_EXEMPT_HOSTS", ()):
            if entry.startswith("."):
                if host.endswith(entry) and host != entry[1:]:
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
        if rules is None or rules.group is None:
            return not (rules and rules.disallow_all)
        try:
            p = urlparse(url)
            path = p.path or "/"
            if p.query:                       # rules can match the query too
                path = f"{path}?{p.query}"
            return rules.group.allows(path)
        except Exception:
            return True

    async def crawl_delay(self, url: str) -> float | None:
        """Seconds this host asks us to wait between requests, or None."""
        rules = await self._rules(url)
        if not rules or rules.parser is None:
            return None
        try:
            d = rules.parser.crawl_delay(self.user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None

    async def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs the host advertises — a discovery hint, since this
        is exactly where sites publish them."""
        rules = await self._rules(url)
        return list(rules.sitemaps) if rules else []

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
