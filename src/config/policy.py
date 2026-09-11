"""Crawl policy: the profile's [policy] table plus the HTTP constants that
are not profile keys (nothing about a user's field changes how long a
socket should wait, or what user agent a request carries).

The per-ATS company ROSTER lives in the SQLite store (companies table),
not here. Manage it with discover.py --local / --add-board, or
run_scraper.py --import-companies roster.json.
"""

# _self: the config PACKAGE, which is what callers monkeypatch.
# profile.py defines it; two identical copies is one too many for
# a function whose whole job is naming one module.
from .profile import MISSION_TIERS, _self, profile_section

_pol = profile_section("policy")

# =========================================================================
#  HTTP
# =========================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Per-request HTTP timeouts, as (connect, read), split because the two
# phases fail for different reasons (see the robots pair below).
# PROBE_TIMEOUT is for speculative requests (slug probes, name-guessed
# careers pages, board counts), where a dead host should cost little and
# most answers are 404s. FETCH_TIMEOUT is for a board or page known to
# exist, where a slow real server is worth waiting for.
PROBE_TIMEOUT = (3.0, 10.0)
FETCH_TIMEOUT = (5.0, 25.0)

# Gated-site capture (Playwright). Keep roughly current — a stale UA is a
# red flag to fingerprinters.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# =========================================================================
#  [policy]
# =========================================================================

# Conglomerates whose OVERALL mission scores "other" but which run aligned
# subdivisions worth surfacing. Kept ACTIVE, crawled through the keyword
# filter (only aligned roles survive), and ranked at
# MULTI_DIVISION_MISSION_FLOOR rather than their own low company score.
MULTI_DIVISION_COMPANIES = {s.strip().lower()
                            for s in _pol.get("multi_division", [])}
MULTI_DIVISION_MISSION_FLOOR = float(_pol.get("multi_division_mission_floor", 0.6))


def is_multi_division(name):
    """True if `name` is a known multi-division conglomerate (profile policy).

    >>> is_multi_division("")
    False
    >>> is_multi_division(None)
    False
    """
    return (name or "").strip().lower() in MULTI_DIVISION_COMPANIES


# Mission tiers as loaded (highest alignment -> lowest, last is the
# catch-all), and the subset a newly-sourced company is crawled for.
ACTIVE_MISSION_TIERS = tuple(t["name"] for t in MISSION_TIERS if t["active"])


def is_active_mission(tier, name, include_missions=None):
    """The one activation rule: should a newly-sourced company be crawled?

    `tier` is the mission tier from src.claude.score_company_mission, `name`
    the company name, `include_missions` an optional override of the
    profile's active tiers. Returns 1 (crawl it) or 0 (park it) -- an int,
    because it goes straight into the ``companies.active`` column.

    A company is active when ANY of these hold:

    * its tier is one of the active tiers,
    * its tier is ``None`` -- scoring was UNAVAILABLE, not negative,
    * it is a multi-division conglomerate (profile policy).

    >>> tiers = ("green", "blue")
    >>> is_active_mission("green", "Nowhere Robotics", tiers)
    1
    >>> is_active_mission("red", "Nowhere Robotics", tiers)
    0

    An unavailable score must never read as "off-mission". A failed or
    rate-limited call returns ``(None, None, "")``, and treating that as a
    rejection buries a whole discovery sweep in inactive rows:

    >>> is_active_mission(None, "Nowhere Robotics", tiers)
    1

    Omitting `include_missions` falls back to the profile's active tiers,
    so the answer depends on the loaded profile rather than this literal:

    >>> is_active_mission(ACTIVE_MISSION_TIERS[0], "Nowhere Robotics")
    1

    Notes:
        This lived inline at six call sites before it was named, then in
        src/claude/api.py because it reads a tier that the LLM produces.
        Nothing about it is an LLM concern: it is a profile table, a
        None-means-unknown rule, and `is_multi_division` above. Being in
        the claude module made src/store reach up to the LLM layer just to
        default one column, which was the only thing stopping store from
        depending on config alone. Still re-exported as
        `src.claude.api.is_active_mission`, which is where the call sites
        and their comments point.
        tests/test_invariants.py keeps the rule single-sourced.
    """
    tiers = ACTIVE_MISSION_TIERS if include_missions is None else include_missions
    # Through the PACKAGE, not the module global beside it: a test that
    # narrows the conglomerate list patches `config.is_multi_division`, and
    # that is the documented way to reach anything in config (never
    # `from config import`). A bare local call would silently ignore it --
    # which is exactly what tests/test_triage.py caught when this rule
    # moved here.
    return 1 if (tier in tiers or tier is None
                 or _self().is_multi_division(name)) else 0


# Honor robots.txt: skip paths a host asks crawlers to leave alone, and
# obey its Crawl-delay. On by default — it costs one cached request per
# host, and the endpoints this crawler uses are permissive (Lever, for
# instance, publishes `Allow: /` with `Crawl-delay: 1`). See src/net/robots.py.
RESPECT_ROBOTS = bool(_pol.get("respect_robots", True))

# Hosts whose robots.txt is NOT consulted even while RESPECT_ROBOTS is on.
# Crawl-delay pacing still applies. Entries are lowercase hostnames; a
# leading dot matches every subdomain (".peopleadmin.com" covers
# unc.peopleadmin.com). Meant for machine-facing endpoints — a vendor's
# public postings API, an Atom feed — sitting on a host whose robots.txt
# blanket-disallows `*` because it was written for the HTML site.
ROBOTS_EXEMPT_HOSTS = tuple(
    s.strip().lower() for s in _pol.get("robots_exempt_hosts", []) if s.strip())

# Public resolvers the web-search client (src/net/ddg.py) switches to when
# the search library's own resolver is refused. Seen with a VPN up alongside
# a second connected adapter: the OS resolver works, the library's does not.
# An empty list disables the fallback; the run then skips web search.
SEARCH_DNS_FALLBACK = tuple(
    s.strip() for s in _pol.get("search_dns_fallback", ["1.1.1.1", "8.8.8.8"])
    if s.strip())

# robots.txt fetch timeouts, as (connect, read).
#
# Split because the two phases fail for different reasons. Discovery probes a
# lot of speculative `careers.<name>.com` hosts; the ones whose parent domain
# has wildcard DNS resolve to an edge that never completes a handshake, and
# each burns the whole connect timeout. Measured Aug 2026 over 16 live boards
# and company sites: connect median 147 ms, max 437 ms — so 3 s is ~7x the
# slowest real handshake while cutting a dead host's cost by 70%.
#
# The READ timeout stays generous on purpose. A host that connects promptly
# but is slow to serve robots.txt is a real server with a real policy, and
# that is precisely the case where giving up early would have us crawl
# something we were asked not to.
#
# Note the connect budget is per RESOLVED ADDRESS, not per host: a name with
# two A records costs up to 2x before it gives up. That is the socket doing
# the right thing (trying each address), and it is bounded by the record
# count, so it is worth knowing about rather than working around.
ROBOTS_CONNECT_TIMEOUT = float(_pol.get("robots_connect_timeout", 3.0))
ROBOTS_READ_TIMEOUT    = float(_pol.get("robots_read_timeout", 10.0))

# Headless-browser resolution order for the JS probes. "" is Playwright's own
# pinned build; the rest are `channel=` names for browsers already on the
# machine. Trying the system browsers means `pip install` alone is enough —
# no separate `playwright install` download — which is what makes the probes
# work on CI runners and on a machine whose playwright package was upgraded
# without re-fetching its browsers. Order matters: the pinned build first,
# because it is the only one whose version we control.
BROWSER_CHANNELS = [c or None for c in
                    _pol.get("browser_channels", ["", "chrome", "msedge"])]
