"""Non-company sources, from the profile's [sources] table: forums and
aggregator feeds. The per-company boards are the store's roster, not config.

The forum list ships EMPTY: it encodes a field's vocabulary, so a generic
default would only burn requests. The aggregator
feeds are field-agnostic (they carry every kind of role and are filtered
by your keywords), so they ship ON with sensible defaults. USAJOBS and
Getro are OFF: one needs credentials, the other names a place.
"""

from __future__ import annotations

from typing_extensions import TypedDict

from .profile import PROFILE

_src = PROFILE.sources

# Discourse forums with a jobs category — [sources].discourse
# ({ label, url, category_id }). See profile.example.toml.
DISCOURSE_BOARDS = [(b.label or b.url, b.url, b.category_id)
                    for b in _src.discourse]

# =========================================================================
#  AGGREGATOR FEEDS (non-company-owned job boards, no API key required)
# =========================================================================
#
# Each is pulled whole by the harvest (src/crawl/harvest.py feed_plan),
# stored unfiltered and judged by triage. FEEDS, below, is the one
# table of feed families.

# Remotive: https://remotive.com/api/remote-jobs (one category or all).
# Categories: "software-dev", "data", "all-others", etc. None = all.
REMOTIVE_CATEGORY: str | None = _src.remotive_category or None

# Hacker News "Ask HN: Who is hiring?" monthly thread.
# max_threads=2 covers the current + previous month's threads.
HNHIRING_MAX_THREADS = _src.hnhiring_max_threads

# USAJOBS, [sources.usajobs]. OFF by default, unlike the feeds above: it is
# the one source needing credentials (USAJOBS_API_KEY / USAJOBS_EMAIL), and
# its scope is a place rather than a topic, so there is no useful default
# search. `series` is the real filter: occupational series codes. None (key
# omitted) means the technical set in src/ats/feeds/usajobs.DEFAULT_SERIES;
# [] means every series.
_usajobs = _src.usajobs
USAJOBS_KEYWORD: str | None = _usajobs.keyword or None
USAJOBS_LOCATION: str | None = _usajobs.location or None
USAJOBS_RADIUS   = _usajobs.radius
USAJOBS_SERIES: list[str] | None = _usajobs.series
USAJOBS_RESULTS_PER_PAGE = _usajobs.results_per_page

# Getro network boards — [sources.getro] ({ enabled, boards, max_details }).
# A VC portfolio or association board that lists many employers' openings
# on one host, each posting naming its employer. OFF by default like
# USAJOBS: a board is a place, so there is no generic default. `boards` are
# board URLs (any page; only the host is used). `max_details` caps the
# posting pages fetched per board per crawl (see src/ats/feeds/getro.py:
# the listing's newest max_details postings are fetched unscreened).
# Notes: when Getro is switched on, store its listing bodiless and let triage
# hydrate only the survivors, as boards do (not done; Getro is off, no boards).
_getro = _src.getro
GETRO_BOARDS: list[str] = [b.strip() for b in _getro.boards if b.strip()]
GETRO_MAX_DETAILS = _getro.max_details

# Generic RSS/Atom feeds, [sources].rss ({ label, url, location }). The
# schema's default is a set of broad remote-job feeds; `rss = []` turns RSS
# off, and a list of your own replaces them.
RSS_FEEDS: list[tuple[str, str, str]] = [(f.label or f.url, f.url, f.location)
                                         for f in _src.rss]

# Board directory — [sources.board_directory] (src/discovery/board_directory.py):
# the jobhive dataset of live postings, read for boards the roster lacks.
BOARD_DIRECTORY = _src.board_directory

# Google Jobs — [sources.google_jobs] (the `google_jobs` registry,
# src/discovery/registries.py): the searches an Apify actor runs for
# employers hiring near you, and what one run may cost.
GOOGLE_JOBS = _src.google_jobs

class FeedFamily(TypedDict):
    """One feed family: `switch` is the track `sources` switch that enables it
    (null: every track), `on` whether the profile turns it on, and `calls` the
    fetches it makes -- an instance label (null for a family with one source)
    and the keyword arguments of its fetcher (src/ats/feeds.FETCH)."""
    switch: str | None
    on: bool
    calls: list[tuple[str | None, dict[str, str | int | bool | list[str] | None]]]


#: Every feed family once; a row's `source` is "<family>" or
#: "<family>:<instance>". src/crawl/harvest.feed_plan iterates this and
#: src/ats/feeds.FETCH maps each family to its fetcher. (RemoteOK is the one
#: JSON endpoint at https://remoteok.com/api.)
FEEDS: dict[str, FeedFamily] = {
    "remoteok": {"switch": "aggregators", "on": _src.remoteok,
                 "calls": [(None, {})]},
    "remotive": {"switch": "aggregators", "on": _src.remotive,
                 "calls": [(None, {"category": REMOTIVE_CATEGORY})]},
    "hn": {"switch": "aggregators", "on": _src.hnhiring,
           "calls": [(None, {"max_threads": HNHIRING_MAX_THREADS})]},
    "rss": {"switch": "aggregators", "on": bool(RSS_FEEDS),
            "calls": [(label, {"source_label": label, "url": url,
                               "default_location": loc,
                               "remote_board": loc.strip().lower() == "remote"})
                      for label, url, loc in RSS_FEEDS]},
    "discourse": {"switch": "aggregators", "on": bool(DISCOURSE_BOARDS),
                  "calls": [(name, {"display_name": name, "base_url": base,
                                    "category_id": cat})
                            for name, base, cat in DISCOURSE_BOARDS]},
    "usajobs": {"switch": None, "on": _usajobs.enabled,
                "calls": [(None, {"keyword": USAJOBS_KEYWORD,
                                  "location": USAJOBS_LOCATION,
                                  "radius": USAJOBS_RADIUS,
                                  "series": USAJOBS_SERIES,
                                  "results_per_page": USAJOBS_RESULTS_PER_PAGE})]},
    "getro": {"switch": None, "on": _getro.enabled and bool(GETRO_BOARDS),
              "calls": [(None, {"board_url": b, "max_details": GETRO_MAX_DETAILS})
                        for b in GETRO_BOARDS]},
}

# Wikidata's API: the domain lookup's fallback (src/discovery/resolve/domain.py)
# and tools/miss_cleanup.py.
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# The two paid APIs discovery may use, each only with its key set:
# Firecrawl's search (src/net/firecrawl.py) and Apify's actor runs
# (src/net/apify.py).
FIRECRAWL_API = "https://api.firecrawl.dev/v2"
APIFY_API = "https://api.apify.com/v2"
