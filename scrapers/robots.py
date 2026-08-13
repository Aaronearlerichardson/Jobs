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
  * network error  -> fail OPEN (allow) rather than stall the crawl, but
                      say so once per host.
"""

import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import config

from .http import HEADERS

# How long a parsed robots.txt stays good before we re-fetch it.
CACHE_TTL_SECONDS = 3600


class _HostRules:
    __slots__ = ("parser", "fetched_at", "disallow_all", "sitemaps",
                 "last_request", "lock")

    def __init__(self, parser=None, disallow_all=False, sitemaps=()):
        self.parser = parser
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
        self._lock = threading.Lock()     # guards the dict itself

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
            r = requests.get(f"{origin}/robots.txt", timeout=10,
                             headers=HEADERS, allow_redirects=True)
        except Exception as e:
            print(f"    [robots] {origin}: unreachable ({type(e).__name__}); "
                  f"proceeding without restrictions")
            return _HostRules()
        if 500 <= r.status_code < 600:
            return _HostRules(disallow_all=True)
        if r.status_code >= 400:
            return _HostRules()                    # nothing to obey
        parser = RobotFileParser()
        try:
            parser.parse(r.text.splitlines())
        except Exception:
            return _HostRules()
        sitemaps = [ln.split(":", 1)[1].strip()
                    for ln in r.text.splitlines()
                    if ln.strip().lower().startswith("sitemap:")]
        return _HostRules(parser=parser, sitemaps=sitemaps)

    def _rules(self, url):
        origin = self._origin(url)
        if not origin:
            return None
        with self._lock:
            rules = self._hosts.get(origin)
            if rules and (time.time() - rules.fetched_at) < self.ttl:
                return rules
        fresh = self._fetch(origin)                # outside the lock: network
        with self._lock:
            self._hosts[origin] = fresh
        return fresh

    # -- public API -------------------------------------------------------

    def allowed(self, url):
        """May we fetch `url`? True when robots is absent/permissive."""
        if not getattr(config, "RESPECT_ROBOTS", True):
            return True
        rules = self._rules(url)
        if rules is None or rules.parser is None:
            return not (rules and rules.disallow_all)
        try:
            return rules.parser.can_fetch(self.user_agent, url)
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
