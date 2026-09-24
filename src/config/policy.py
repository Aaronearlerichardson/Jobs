"""Crawl policy: the profile's [policy] table plus the HTTP constants that
are not profile keys (nothing about a user's field changes how long a
socket should wait, or what user agent a request carries).

The per-ATS company ROSTER lives in the SQLite store (companies table),
not here. Manage it with discover.py --local / --add-board, or
run_scraper.py --import-companies roster.json.
"""

import math

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

# A bare platform UA, for hosts whose WAF refuses a Chrome UA that arrives
# without Chrome's client-hint headers (a requests session claiming Chrome).
PLAIN_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

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

# TITLE vocabulary that passes the division keyword gate at a conglomerate
# the roster ALSO carries a `watch` tag for. The division gate
# (src.match.filters.is_relevant, called by src.crawl.triage.row_verdict
# and src.ops.maintenance._keep_job) asks a conglomerate's postings for the
# profile's own health/bio/science vocabulary, because a corporate mission
# score says nothing about the division that is hiring. At a WATCHED
# conglomerate that question is the wrong one: the watch tag already means
# "I want this employer's technical roles", and its aligned division is a
# plain engineering org whose postings never use that vocabulary.
#
# Matched against the TITLE only, with word boundaries (filters.BOUNDED) --
# the same scope and matcher as the per-track [exclude.<id>] title_tokens,
# and for the same reason: every posting BODY at such an employer mentions
# AI, GPUs and software somewhere, so a body-scoped list would admit its
# sales and marketing boards wholesale. The profile-wide [exclude] gate
# still runs first, so a title this list would otherwise admit still loses
# to an [exclude] phrase ("Senior Manager, Software Engineering" is still a
# "manager"). Empty (the default) leaves the division gate exactly as it
# was for every company.
WATCH_DIVISION_TITLES = tuple(s.strip().lower()
                              for s in _pol.get("watch_division_titles", [])
                              if s.strip())


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


# =========================================================================
#  Background harvester cadence
# =========================================================================

# How long a board that is BOTH off-mission and itself inactive waits
# between whole-board harvests, instead of the harvester's ordinary
# MIN_AGE_HOURS freshness rule. Such a board is still fetched every pass --
# "harvest every board" stays true -- just on this longer interval; the
# predicate, the census behind the default and the --min-age-hours
# interaction all live with the one reader, src.crawl.harvest.plan.
HARVEST_OFFMISSION_HOURS = float(_pol.get("harvest_offmission_hours", 168))


def is_offmission_inactive(c):
    """True for a board that is BOTH off-mission (mission-scored into a
    tier the profile marks inactive, or never mission-scored at all) AND
    itself inactive -- the one predicate both the harvester's long-interval
    cadence (HARVEST_OFFMISSION_HOURS, read by src.crawl.harvest.plan) and
    its page-budget gate (board_max_pages, below) key off, so a board
    triage's own mission gate discards anyway is never also read on the
    wider, slower page budget. A NULL tier reads as off-mission HERE
    (unlike is_active_mission, where an unscored company is treated as
    active) -- an inactive row nobody has bothered to mission-score is
    exactly as low-priority as one scored into the catch-all tier, and
    this predicate only ever narrows a harvest CADENCE or BUDGET, never
    activation or crawl eligibility.

    >>> is_offmission_inactive({"mission_tier": "other", "active": 0})
    True
    >>> is_offmission_inactive({"mission_tier": None, "active": 0})
    True
    >>> is_offmission_inactive({"mission_tier": "core-mission", "active": 0})
    False
    >>> is_offmission_inactive({"mission_tier": "other", "active": 1})
    False

    Notes:
        A multi-division conglomerate scored into an inactive tier is
        exempt already: that exemption (is_multi_division, applied when
        the row's `active` was last written) is what keeps it `active`,
        so the `active` check above is enough and nothing here re-checks
        is_multi_division.

        Moved here from src.crawl.harvest (whose plan() calls it for
        the off-mission harvest cadence) so src.ats.fetchers.company could
        read the SAME rule for board_max_pages without importing crawl --
        ats sits BELOW crawl in the import DAG. The asymmetry with
        is_active_mission above -- an unscored row is ACTIVE but is
        off-mission for a cadence or a budget -- is pinned in
        tests/test_invariants.py
        (TestOffmissionInactiveIsNotTheActivationRule).
    """
    tier = c.get("mission_tier")
    return not c.get("active") and (tier is None
                                     or tier not in ACTIVE_MISSION_TIERS)


# =========================================================================
#  Whole-board page budget (src.ats.fetchers.board)
# =========================================================================

# Rows a mission-worth-it Workday or SmartRecruiters whole-board pull reads
# before giving up (see board_max_pages, below, and each fetcher's own
# max_pages parameter). Default 3,000: a live measurement across the ten
# biggest Workday/SmartRecruiters boards (2026-09-18, see the Phase 4
# harvest worker's report) found ThermoFisher's DEDUPED distinct-posting
# count at ~2,815 -- bigger than Eurofins's previously-assumed high-water
# mark of 2,579 -- so the default carries headroom above the biggest board
# actually observed, not just the one first flagged. An off-mission,
# INACTIVE board (is_offmission_inactive) keeps its fetcher's own
# narrower default instead -- see board_max_pages.
BOARD_MAX_ROWS = int(_pol.get("board_max_rows", 3000))

# One value per rule for every ATS board (src.ats.fetchers.board): the
# pause between two listing pages; detail GETs a sweep pull may spend
# screening rows, and a vetted whole-board pull hydrating them, with the
# pause between two; and how long a listing read for closure checks or
# deep verify is reused. A host's robots Crawl-delay still applies on top
# (net.robots waits out whichever is longer).
PAGE_DELAY_S = 0.3
SWEEP_DETAILS = 40
SWEEP_DETAIL_DELAY_S = 0.2
WHOLE_BOARD_DETAILS = 200
WHOLE_BOARD_DETAIL_DELAY_S = 0.15
BOARD_MEMO_S = 600.0
# Stored rows one board may hydrate per harvest or triage run, the pause
# between two of those detail GETs, and the pages a local count samples
# when a board ignores its locality scope.
HYDRATE_CAP_PER_RUN = 100
HYDRATE_DELAY_S = 1.0
LOCAL_COUNT_SAMPLE_PAGES = 5

# The careers-page reader (src.ats.fetchers.custom): the job links a page
# needs to be a board, the most characters a title and a location keep,
# how long a detection verdict is reused, and the hosts never read as a
# company's own board (job aggregators and ATS vendors; regex fragments).
CAREERS_PAGE_MIN_LINKS = 3
CAREERS_PAGE_TITLE_MAX = 90
CAREERS_PAGE_LOCATION_MAX = 70
BOARD_DETECT_CACHE_S = 6 * 3600
CAREERS_PAGE_OFFSITE_HOSTS = (
    "indeed", "linkedin", "glassdoor", "ziprecruiter", "simplyhired", "monster",
    "dice", "greenhouse", r"lever\.co", "ashbyhq", "myworkdayjobs",
    "smartrecruiters", "icims", "paylocity", "bamboohr", "jobvite",
    r"google\.com", "builtin")


def board_max_pages(company, page_size, offmission_pages):
    """max_pages for a paged whole-board listing pull:
    BOARD_MAX_ROWS's wider budget (in pages of `page_size`) for a
    mission-worth-it board, or `offmission_pages` -- the fetcher's own
    narrower default -- for one is_offmission_inactive. The SAME
    gate the harvester's cadence already uses for these boards, not a
    second rule: one a track's own mission gate discards on every triage
    pass never earns the wider, slower read either.

    >>> mission = {"active": 1, "mission_tier": "adjacent"}
    >>> board_max_pages(mission, 20, 60) >= 60
    True
    >>> stale = {"active": 0, "mission_tier": "other"}
    >>> board_max_pages(stale, 20, 60)
    60

    Notes:
        2026-09-18 census (live store): 25 Workday/SmartRecruiters boards
        were reading every page of their old budget without a natural
        stop -- 17 of them mission-worth-it, 8 off-mission-inactive. The
        Phase 4 harvest worker's report measures the wider budget's cost
        on the 17 that a track can actually surface; the 8 keep today's
        narrower read rather than paying it for nothing.
    """
    if is_offmission_inactive(company):
        return offmission_pages
    # Through the PACKAGE, not this module's own global -- same rule as
    # is_active_mission's _self().is_multi_division(name) above: a test
    # that patches config.BOARD_MAX_ROWS must actually reach this read.
    wide_pages = math.ceil(_self().BOARD_MAX_ROWS / page_size)
    return max(offmission_pages, wide_pages)


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
