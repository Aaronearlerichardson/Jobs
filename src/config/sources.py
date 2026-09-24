"""Non-company sources, from the profile's [sources] table: forums, web
searches and aggregator feeds. The per-company boards are the store's
roster, not config.

The forum and web-search lists ship EMPTY: they encode a field's
vocabulary, so a generic default would only burn requests. The aggregator
feeds are field-agnostic (they carry every kind of role and are filtered
by your keywords), so they ship ON with sensible defaults. USAJOBS and
Getro are OFF: one needs credentials, the other names a place.
"""

from .profile import PROFILE

_src = PROFILE.sources

# Discourse forums with a jobs category — [sources].discourse
# ({ label, url, category_id }). See profile.example.toml.
DISCOURSE_BOARDS = [(b.label or b.url, b.url, b.category_id)
                    for b in _src.discourse]

# Web searches for the sweep-style crawl (runner.build_sources, enabled by
# [tracks.*].sources.websearch) — [sources].websearch
# ({ label, query, max_results }). DuckDuckGo text search; each result URL is
# parsed for JSON-LD JobPosting.
WEBSEARCH_QUERIES: list[tuple] = [
    ((q.label or q.query)[:60], q.query, q.max_results)
    for q in _src.websearch
]

# =========================================================================
#  AGGREGATOR FEEDS (non-company-owned job boards, no API key required)
# =========================================================================
#
# Run-to-completion each crawl: one HTTP request returns every active
# listing, so they need no per-company config. Filtering happens in the
# fetcher via is_relevant().

# RemoteOK: single JSON endpoint at https://remoteok.com/api.
REMOTEOK_ENABLED = _src.remoteok

# Remotive: https://remotive.com/api/remote-jobs (one category or all).
# Categories: "software-dev", "data", "all-others", etc. None = all.
REMOTIVE_ENABLED   = _src.remotive
REMOTIVE_CATEGORY: str | None = _src.remotive_category or None

# Hacker News "Ask HN: Who is hiring?" monthly thread.
# max_threads=2 covers the current + previous month's threads.
HNHIRING_ENABLED     = _src.hnhiring
HNHIRING_MAX_THREADS = _src.hnhiring_max_threads

# USAJOBS, [sources.usajobs]. OFF by default, unlike the feeds above: it is
# the one source needing credentials (USAJOBS_API_KEY / USAJOBS_EMAIL), and
# its scope is a place rather than a topic, so there is no useful default
# search. `series` is the real filter: occupational series codes. None (key
# omitted) means the technical set in src/ats/feeds/usajobs.DEFAULT_SERIES;
# [] means every series.
_usajobs = _src.usajobs
USAJOBS_ENABLED  = _usajobs.enabled
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
# posting pages fetched per board per crawl (see src/ats/feeds/getro.py
# — titles are screened before any page fetch).
_getro = _src.getro
GETRO_ENABLED = _getro.enabled
GETRO_BOARDS: list[str] = [b.strip() for b in _getro.boards if b.strip()]
GETRO_MAX_DETAILS = _getro.max_details

# Generic RSS/Atom feeds, [sources].rss ({ label, url, location }). The
# schema's default is a set of broad remote-job feeds; `rss = []` turns RSS
# off, and a list of your own replaces them.
RSS_FEEDS: list[tuple[str, str, str]] = [(f.label or f.url, f.url, f.location)
                                         for f in _src.rss]
