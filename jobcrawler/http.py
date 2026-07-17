"""Shared HTTP defaults.

A single module-level `SESSION` gives every fetcher connection pooling and
keep-alive, so repeated hits to the same host (greenhouse/lever/ashby/workday
probes, board pagination) reuse one TCP+TLS connection instead of paying a
fresh handshake per request. Call sites use `SESSION.get(...)` /
`SESSION.post(...)`; `HEADERS` stays exported for the few callers that still
pass headers explicitly (the session already carries them as defaults).
"""

from requests import Session
from requests.adapters import HTTPAdapter

from config import USER_AGENT

HEADERS = {"User-Agent": USER_AGENT}


def _build_session():
    s = Session()
    s.headers.update(HEADERS)
    # Pool a handful of connections per host; discovery probes fan across a
    # few ATS hosts and re-hit each many times. max_retries=0 keeps failure
    # semantics identical to the old bare requests.get (callers try/except).
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _build_session()
