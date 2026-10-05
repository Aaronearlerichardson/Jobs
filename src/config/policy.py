"""Crawl policy: the profile's [policy] table plus the HTTP constants that
are not profile keys (nothing about a user's field changes how long a
socket should wait, or what user agent a request carries).

The per-ATS company ROSTER lives in the SQLite store (companies table),
not here. Manage it with discover.py --local / --add-board, or
run_scraper.py --import-companies roster.json.
"""

from __future__ import annotations

from collections.abc import Collection

from src.rows import CompanyRow
# _self: the config PACKAGE, which is what callers monkeypatch.
# profile.py defines it; two identical copies is one too many for
# a function whose whole job is naming one module.
from .profile import MISSION_TIERS, PROFILE, _self

_pol = PROFILE.policy

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
# exist, where a slow real server is worth waiting for. Its read wait was
# 25 s until 2026-09-25: 10 of 101k harvest GETs ran out (BambooHR details,
# Greenhouse), and a host that never answers stalls in connect instead.
PROBE_TIMEOUT = (3.0, 10.0)
FETCH_TIMEOUT = (5.0, 60.0)

# Wall-clock budget of one pool pass, seconds: fetch_all's, and a fan_out's
# where the caller passes one (net.parallel). Past it, work still queued is
# cancelled and work still running is abandoned. About ten times the
# slowest pass in the 2026-09-10..24 session logs, so only a wedged pass
# meets one: a crawl's source fetch took at most 334 s (17 crawls); triage
# hydration 158 s (21 passes), triage scoring 115 s (20, up to the 300-row
# cap), a deep-verify round 136 s (18) and the closed-URL probe 27 s (8
# passes of up to 100 rows) share PASS_BUDGET_S.
FETCH_BUDGET_S = 3600.0
PASS_BUDGET_S = 1800.0

# max_tokens room for always-on thinking above the reply's own budget.
CLAUDE_THINKING_HEADROOM = 4000
# Claude API (connect, read) timeout: a reply sends nothing until it is done.
CLAUDE_TIMEOUT = 120
# Seconds a call waits for its prompt's first call: one cache write per fan-out.
CLAUDE_GATE_WAIT_S = 90
# Seconds a run's end waits for its board-owner checks still in flight, so
# a paid verdict lands before the run's session closes (claude.board_is_own).
CLAUDE_OWNER_WAIT_S = 30
# Seconds before each retry of a transient 429/5xx: such blips clear fast.
CLAUDE_RETRY_DELAYS_S = (2.0, 8.0)

# Wall-clock cap on one name's headless-browser scrape (discovery's JS scan
# probe). candidate_urls yields up to 12 pages and each can spend 20s in
# goto plus 6s waiting for networkidle, so one name could hold a browser
# for five minutes: discover-local 2026-09-22 sat 338s with no output
# inside the JS pass.
JS_PROBE_BUDGET_S = 60

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
MULTI_DIVISION_COMPANIES = {s.strip().lower() for s in _pol.multi_division}
MULTI_DIVISION_MISSION_FLOOR = _pol.multi_division_mission_floor

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
                              for s in _pol.watch_division_titles
                              if s.strip())


def is_multi_division(name: str | None) -> bool:
    """True if `name` is a known multi-division conglomerate (profile policy).

    >>> is_multi_division("")
    False
    >>> is_multi_division(None)
    False
    """
    return (name or "").strip().lower() in MULTI_DIVISION_COMPANIES


# Mission tiers as loaded (highest alignment -> lowest, last is the
# catch-all), and the subset a newly-sourced company is crawled for.
ACTIVE_MISSION_TIERS = tuple(t.name for t in MISSION_TIERS if t.active)


def tier_for_score(score: float | None, tier: str | None = None) -> str | None:
    """The mission tier `score` belongs to: `tier` when the score is inside
    its band, else the band that holds it. A score in the gap between two
    bands keeps `tier` when it is one of the two bands on either side, and
    otherwise takes the nearest. `tier` comes back unchanged when there is
    no score to read.

    The mission model names a tier and a score separately and does not
    keep them in step. The score is the finer judgment, and ranking and
    remote trust read it, so the tier follows it.

    >>> tier_for_score(1.0) == MISSION_TIERS[0].name
    True
    >>> tier_for_score(0.0) == MISSION_TIERS[-1].name
    True
    >>> tier_for_score(None, "adjacent")
    'adjacent'
    >>> top, bottom = MISSION_TIERS[-2], MISSION_TIERS[-1]
    >>> mid = (bottom.band[1] + top.band[0]) / 2
    >>> tier_for_score(mid, top.name) == top.name
    True

    Notes:
        On 2026-10-05, 49 of 202 `core-mission` companies scored below
        that band's floor (Eight Sleep 0.5, Medtronic 0.55), with reasons
        that read "not neurotech".
    """
    if score is None:
        return tier

    gap = {t.name: max(t.band[0] - score, score - t.band[1], 0) for t in MISSION_TIERS}
    nearest = sorted(gap, key=lambda name: gap[name])
    if gap[nearest[0]] == 0:
        return tier if tier is not None and gap.get(tier) == 0 else nearest[0]
    return tier if tier in nearest[:2] else nearest[0]


def is_active_mission(tier: str | None, name: str | None,
                      include_missions: Collection[str] | None = None) -> int:
    """The one activation rule: should a newly-sourced company be crawled?

    `tier` is the mission tier from src.claude.api.score_company_mission, `name`
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

# How long an inactive board never mission-scored waits between
# whole-board harvests, instead of the harvester's ordinary MIN_AGE_HOURS
# freshness rule (offmission_inactive's "deferred"); the census behind the
# default and the --min-age-hours interaction live with the one reader,
# src.crawl.harvest.plan.
HARVEST_OFFMISSION_HOURS = _pol.harvest_offmission_hours


def offmission_inactive(c: CompanyRow) -> str:
    """What the harvester (src.crawl.harvest.plan) does with an inactive
    board off the mission: "stopped", left out, when it was mission-scored
    into a tier the profile marks inactive (is_active_mission's answer,
    the multi-division exemption included); "deferred", read every
    HARVEST_OFFMISSION_HOURS, when it was never scored (it may be a fresh
    lead); "" for an active board or tier. Reactivating or re-tiering a
    company is what brings a stopped board back.

    >>> [offmission_inactive({"mission_tier": t, "active": a}) for t, a in
    ...  (("other", 0), (None, 0), ("other", 1), ("core-mission", 0))]
    ['stopped', 'deferred', '', '']

    Notes:
        A NULL tier reads as off-mission HERE, unlike is_active_mission,
        where an unscored company is active; the asymmetry is pinned in
        tests/test_invariants.py
        (TestOffmissionInactiveIsNotTheActivationRule). This decides only
        a harvest, never activation or crawl eligibility.

        Until 2026-09-26 a scored board was deferred too: 347 companies,
        all tier `other`, holding 67,312 open postings, were read weekly;
        triage dropped 84,234 of their postings as off-mission, and the
        best fit score among them was 0.36.
    """
    tier = c.get("mission_tier")
    off = not c.get("active") and (tier is None or tier not in ACTIVE_MISSION_TIERS)
    return ("deferred" if is_active_mission(tier, c.get("name")) else "stopped") if off else ""


# =========================================================================
#  Whole-board page budget (src.ats.board.engine)
# =========================================================================

# Rows a whole-board pull reads before giving up: each pager's own `pages`,
# widened to cover it at the step the walk takes (pager.page_cap). Default
# 3,000: a live measurement across the ten biggest Workday/SmartRecruiters
# boards (2026-09-18, see the Phase 4 harvest worker's report) found
# ThermoFisher's DEDUPED distinct-posting count at ~2,815 -- bigger than
# Eurofins's previously-assumed high-water mark of 2,579 -- so the default
# carries headroom above the biggest board actually observed. One budget
# for every board a harvest or crawl pull reads: until 2026-09-26 an
# off-mission, inactive one kept its pager's narrower default, which left
# five Workday boards capped (a capped snapshot closes nothing) and ~1,900
# of their rows queued for the closed-URL probe. Discovery's validation
# pulls (Board.whole_board's `validate`) keep the pager's own budget.
BOARD_MAX_ROWS = _pol.board_max_rows

# One value per rule for every ATS board (src.ats.board.engine): the
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
# between two of those detail GETs (and between two closed-URL probes on
# one host), the probes one host takes per pass (one or two GETs each: a
# host cut the crawler off after 151 detail GETs on 2026-09-10), and the
# pages a local count samples where it cannot ask the board for its area.
HYDRATE_CAP_PER_RUN = 100
HYDRATE_DELAY_S = 1.0
CLOSED_PROBE_PER_HOST = 25
LOCAL_COUNT_SAMPLE_PAGES = 5

# The careers-page reader (src.ats.board.custom): the job links a page
# needs to be a board, the most characters a title and a location keep, and
# how long a detection verdict is reused. The hosts it never reads as a
# company's own board derive from config.BOARDS (boards.py).
CAREERS_PAGE_MIN_LINKS = 3
CAREERS_PAGE_TITLE_MAX = 90
CAREERS_PAGE_LOCATION_MAX = 70
BOARD_DETECT_CACHE_S = 6 * 3600


# Honor robots.txt: skip paths a host asks crawlers to leave alone, and
# obey its Crawl-delay. On by default — it costs one cached request per
# host, and the endpoints this crawler uses are permissive (Lever, for
# instance, publishes `Allow: /` with `Crawl-delay: 1`). See src/net/robots.py.
RESPECT_ROBOTS = _pol.respect_robots

# Hosts whose robots.txt is NOT consulted even while RESPECT_ROBOTS is on.
# Crawl-delay pacing still applies. Entries are lowercase hostnames; a
# leading dot matches every subdomain (".peopleadmin.com" covers
# unc.peopleadmin.com). Meant for machine-facing endpoints — a vendor's
# public postings API, an Atom feed — sitting on a host whose robots.txt
# blanket-disallows `*` because it was written for the HTML site.
ROBOTS_EXEMPT_HOSTS = tuple(
    s.strip().lower() for s in _pol.robots_exempt_hosts if s.strip())

# Public resolvers the web-search client (src/net/ddg.py) switches to when
# the search library's own resolver is refused. Seen with a VPN up alongside
# a second connected adapter: the OS resolver works, the library's does not.
# An empty list disables the fallback; the run then skips web search.
SEARCH_DNS_FALLBACK = tuple(
    s.strip() for s in _pol.search_dns_fallback if s.strip())

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
ROBOTS_CONNECT_TIMEOUT = _pol.robots_connect_timeout
ROBOTS_READ_TIMEOUT    = _pol.robots_read_timeout

# Headless-browser resolution order for the JS probes. "" is Playwright's own
# pinned build; the rest are `channel=` names for browsers already on the
# machine. Trying the system browsers means `pip install` alone is enough —
# no separate `playwright install` download — which is what makes the probes
# work on CI runners and on a machine whose playwright package was upgraded
# without re-fetching its browsers. Order matters: the pinned build first,
# because it is the only one whose version we control.
BROWSER_CHANNELS = [c or None for c in _pol.browser_channels]
