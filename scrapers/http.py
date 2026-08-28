"""Shared HTTP defaults.

A single module-level `SESSION` gives every fetcher connection pooling and
keep-alive, so repeated hits to the same host (greenhouse/lever/ashby/workday
probes, board pagination) reuse one TCP+TLS connection instead of paying a
fresh handshake per request. Call sites use `SESSION.get(...)` /
`SESSION.post(...)`; `HEADERS` stays exported for the few callers that still
pass headers explicitly (the session already carries them as defaults).
"""

import logging

from requests import Session
from requests.adapters import HTTPAdapter

from config import USER_AGENT

# File-only request trace (core/session_log.py installs the handler; there
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
