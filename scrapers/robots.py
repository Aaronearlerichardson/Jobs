"""robots.txt support (RFC 9309).

A site's robots.txt states which paths automated clients are asked not to
fetch, how fast to go (`Crawl-delay`), and where its sitemaps live. It is a
request, not a lock — nothing here is enforced by the server — but honoring
it is what separates a well-behaved crawler from an abusive one, and "we
parse and honor robots.txt" answers most of the responsible-crawling
question in one line.

Fetched once per host and cached. Thread-safe: crawls run several hosts
through a pool, and the per-host crawl delay has to serialize requests to
the SAME host without blocking the others.

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
in config.py. Connect is short because dead name-guesses hang there; read is
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

import contextlib
import re
import socket
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import config

from .http import HEADERS

# How long a parsed robots.txt stays good before we re-fetch it.
CACHE_TTL_SECONDS = 3600


# --------------------------------------------------------------------------- #
#  RFC 9309 §2.2 path matching                                                 #
# --------------------------------------------------------------------------- #

def _pattern_to_re(path):
    """A robots.txt path pattern -> a compiled prefix regex.

    `*` matches any sequence; a trailing `$` anchors the end of the URL path.
    Everything else is literal. An empty pattern matches nothing (an empty
    `Disallow:` means "no restriction", handled by the caller)."""
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    body = "".join(".*" if ch == "*" else re.escape(ch) for ch in path)
    return re.compile(body + ("$" if anchored else ""))


class _Group:
    """One `User-agent:` group's rules, in the order they were written."""

    __slots__ = ("agents", "rules")

    def __init__(self):
        self.agents = []
        self.rules = []            # [(specificity, allow, compiled_pattern)]

    def add_rule(self, path, allow):
        # An empty `Disallow:` is the documented way to say "allow all" —
        # it is not a rule, it is the absence of one.
        if not path and not allow:
            return
        self.rules.append((len(path.rstrip("$")), allow, _pattern_to_re(path)))

    def allows(self, path):
        """RFC 9309 §2.2.2: the longest matching pattern wins; Allow wins a
        tie. No match at all means allowed."""
        best_len, best_allow = -1, True
        for length, allow, rx in self.rules:
            if rx.match(path) and (length > best_len
                                   or (length == best_len and allow)):
                best_len, best_allow = length, allow
        return best_allow


def parse_groups(text):
    """robots.txt body -> [_Group]. Consecutive `User-agent:` lines share one
    group, per §2.2.1."""
    groups, current, expecting_agent = [], None, False
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


def _match_group(groups, user_agent):
    """The group governing `user_agent`: the longest matching product token,
    else the `*` group, else None (= unrestricted)."""
    ua = (user_agent or "").lower()
    best, best_len = None, -1
    wildcard = None
    for g in groups:
        for agent in g.agents:
            if agent == "*":
                wildcard = wildcard or g
            elif agent and agent in ua and len(agent) > best_len:
                best, best_len = g, len(agent)
    return best or wildcard


_quiet_lock = threading.Lock()
_quiet_depth = 0


@contextlib.contextmanager
def quiet():
    """Suppress the per-host "unreachable" notice for SPECULATIVE probes.

    Discovery guesses hostnames from a company name — `red.io`, `410.co`,
    `united.ai` — and fetches them to find out whether they exist. Most do
    not. Announcing "proceeding without restrictions" there describes a
    politeness decision that is never acted on: nothing is crawled, because
    the page fetch fails for the same reason robots.txt did. Reported
    anyway, it buries the case the notice exists for — a host we believe is
    a real board, whose robots.txt we could not read before crawling it.

    Deliberately process-wide rather than thread-local: the speculative
    fetches run on a thread pool, and the point is to cover those workers.
    Operations are serialized (one at a time), so no real crawl is running
    concurrently to be silenced by accident.
    """
    global _quiet_depth
    with _quiet_lock:
        _quiet_depth += 1
    try:
        yield
    finally:
        with _quiet_lock:
            _quiet_depth -= 1


def _is_dns_failure(exc, _depth=6):
    """True when `exc` bottoms out in a name-resolution error — i.e. the host
    does not exist, as opposed to a server that refused, hung, or failed TLS.

    requests wraps the cause rather than exposing it, so this walks the chain
    (`ConnectionError <- MaxRetryError <- NameResolutionError <- gaierror`).
    Depth-bounded: an exception chain can be cyclic.
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
    __slots__ = ("parser", "group", "fetched_at", "disallow_all", "sitemaps",
                 "last_request", "lock")

    def __init__(self, parser=None, group=None, disallow_all=False, sitemaps=()):
        self.parser = parser          # RobotFileParser: Crawl-delay only
        self.group = group            # _Group: the RFC-compliant matcher
        self.fetched_at = time.time()
        self.disallow_all = disallow_all
        self.sitemaps = list(sitemaps)
        self.last_request = 0.0
        self.lock = threading.Lock()      # serializes THIS host's pacing


class RobotsCache:
    """Per-host robots.txt rules, fetched lazily and cached."""

    def __init__(self, user_agent=None, ttl=CACHE_TTL_SECONDS):
        self.user_agent = user_agent or config.USER_AGENT
        self.ttl = ttl
        self._hosts = {}
        self._lock = threading.Lock()     # guards the dicts themselves
        self._inflight = {}               # origin -> lock, one fetcher per host

    # -- internals --------------------------------------------------------

    @staticmethod
    def _origin(url):
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None

    def _fetch(self, origin):
        """Fetch + parse one host's robots.txt. Never raises."""
        # Imported here: http.py builds the session that this module is
        # wired into, so importing it at module scope would be circular.
        import requests
        try:
            r = requests.get(f"{origin}/robots.txt",
                             timeout=(config.ROBOTS_CONNECT_TIMEOUT,
                                      config.ROBOTS_READ_TIMEOUT),
                             headers=HEADERS, allow_redirects=True)
        except Exception as e:
            # "Proceeding without restrictions" is a claim about how we treat
            # a SERVER we could not ask. A hostname that does not resolve has
            # no server to be impolite to and will never be crawled — most
            # candidates here are speculative `careers.<name>.com` guesses —
            # so saying it there is noise that buries the real cases.
            if not _is_dns_failure(e) and not _quiet_depth:
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

    def _fresh(self, origin):
        """The cached rules for `origin` if still within the TTL, else None."""
        with self._lock:
            rules = self._hosts.get(origin)
        return rules if rules and (time.time() - rules.fetched_at) < self.ttl else None

    def _rules(self, url):
        origin = self._origin(url)
        if not origin:
            return None
        rules = self._fresh(origin)
        if rules:
            return rules
        with self._lock:
            gate = self._inflight.setdefault(origin, threading.Lock())
        # One fetch per origin, even when threads arrive together. A sniff
        # fans ~8 candidate PATHS across the same host at once; without this
        # every one of them missed the still-empty cache and fetched its own
        # copy of robots.txt — 8x the requests, and 8x the wait when the host
        # is one that hangs until the timeout.
        with gate:
            rules = self._fresh(origin)            # a waiter's fetch may have landed
            if rules:
                return rules
            fresh = self._fetch(origin)            # outside self._lock: network
            with self._lock:
                self._hosts[origin] = fresh
            return fresh

    # -- public API -------------------------------------------------------

    @staticmethod
    def host_exempt(url):
        """Is `url`'s host on config.ROBOTS_EXEMPT_HOSTS? Exact match, or a
        dotted entry matching the host's suffix (".peopleadmin.com" covers
        unc.peopleadmin.com). Case-insensitive; the port is ignored.

        >>> import config
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
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        for entry in getattr(config, "ROBOTS_EXEMPT_HOSTS", ()):
            if entry.startswith("."):
                if host.endswith(entry) and host != entry[1:]:
                    return True
            elif host == entry:
                return True
        return False

    def allowed(self, url):
        """May we fetch `url`? True when robots is absent/permissive, or
        when the host is exempted in the profile (see host_exempt) — the
        exemption skips the robots.txt fetch for that request entirely,
        while wait_turn still paces the host."""
        if not getattr(config, "RESPECT_ROBOTS", True):
            return True
        if self.host_exempt(url):
            return True
        rules = self._rules(url)
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

    def crawl_delay(self, url):
        """Seconds this host asks us to wait between requests, or None."""
        rules = self._rules(url)
        if not rules or rules.parser is None:
            return None
        try:
            d = rules.parser.crawl_delay(self.user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None

    def sitemaps(self, url):
        """Sitemap URLs the host advertises — a discovery hint, since this
        is exactly where sites publish them."""
        rules = self._rules(url)
        return list(rules.sitemaps) if rules else []

    def wait_turn(self, url):
        """Sleep as long as this host's Crawl-delay requires.

        Per-host lock: two threads hitting the SAME host queue up, while
        other hosts keep going in parallel.
        """
        if not getattr(config, "RESPECT_ROBOTS", True):
            return
        rules = self._rules(url)
        if rules is None:
            return
        delay = self.crawl_delay(url)
        if not delay:
            return
        with rules.lock:
            gap = time.monotonic() - rules.last_request
            if gap < delay:
                time.sleep(delay - gap)
            rules.last_request = time.monotonic()


class RobotsDisallowed(Exception):
    """Raised instead of fetching a path robots.txt asks us to leave alone.

    Fetchers already try/except around their requests and report the reason,
    so this surfaces in the crawl log the same way a 404 would."""


# Process-wide cache: one robots.txt per host per hour, however many
# fetchers and threads are running.
CACHE = RobotsCache()
