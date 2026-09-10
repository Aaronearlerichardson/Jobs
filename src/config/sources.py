"""Non-company sources, from the profile's [sources] table: forums, web
searches and aggregator feeds. The per-company boards are the store's
roster, not config.

The forum and web-search lists ship EMPTY: they encode a field's
vocabulary, so a generic default would only burn requests. The aggregator
feeds are field-agnostic (they carry every kind of role and are filtered
by your keywords), so they ship ON with sensible defaults. USAJOBS and
Getro are OFF: one needs credentials, the other names a place.
"""

from .profile import profile_section

_src = profile_section("sources")

# Discourse forums with a jobs category — [sources].discourse
# ({ label, url, category_id }). See profile.example.toml.
DISCOURSE_BOARDS = [
    (str(b.get("label") or b.get("url", "")), str(b.get("url", "")),
     int(b.get("category_id", 0)))
    for b in _src.get("discourse", [])
    if b.get("url")
]

# Web searches for the sweep-style crawl (runner.build_sources, enabled by
# [tracks.*].sources.websearch) — [sources].websearch
# ({ label, query, max_results }). DuckDuckGo text search; each result URL is
# parsed for JSON-LD JobPosting.
WEBSEARCH_QUERIES: list[tuple] = [
    (str(q.get("label") or q.get("query", ""))[:60], str(q.get("query", "")),
     int(q.get("max_results", 12)))
    for q in _src.get("websearch", [])
    if q.get("query")
]

# =========================================================================
#  AGGREGATOR FEEDS (non-company-owned job boards, no API key required)
# =========================================================================
#
# Run-to-completion each crawl: one HTTP request returns every active
# listing, so they need no per-company config. Filtering happens in the
# fetcher via is_relevant().

# RemoteOK: single JSON endpoint at https://remoteok.com/api.
REMOTEOK_ENABLED = bool(_src.get("remoteok", True))

# Remotive: https://remotive.com/api/remote-jobs (one category or all).
# Categories: "software-dev", "data", "all-others", etc. None = all.
REMOTIVE_ENABLED   = bool(_src.get("remotive", True))
REMOTIVE_CATEGORY: str | None = _src.get("remotive_category") or None

# Hacker News "Ask HN: Who is hiring?" monthly thread.
# max_threads=2 covers the current + previous month's threads.
HNHIRING_ENABLED     = bool(_src.get("hnhiring", True))
HNHIRING_MAX_THREADS = int(_src.get("hnhiring_max_threads", 2))

# USAJOBS — [sources.usajobs] ({ enabled, keyword, location, radius, series,
# results_per_page }). OFF by default, unlike the feeds above: it is the one
# source needing credentials (USAJOBS_API_KEY / USAJOBS_EMAIL), and its
# scope is a place rather than a topic, so there is no useful default
# search. `series` is the real filter — occupational series codes; omit the
# key for the technical set (see src/ats/fetchers/usajobs.DEFAULT_SERIES).
_usajobs = _src.get("usajobs", {})
USAJOBS_ENABLED  = bool(_usajobs.get("enabled", False))
USAJOBS_KEYWORD: str | None = _usajobs.get("keyword") or None
USAJOBS_LOCATION: str | None = _usajobs.get("location") or None
USAJOBS_RADIUS   = int(_usajobs.get("radius", 50))
# Presence of the key, not truthiness — `series = []` deliberately means
# "every series", which is different from "I didn't configure any".
USAJOBS_SERIES: list[str] | None = ([str(s) for s in (_usajobs.get("series") or [])]
                                   if "series" in _usajobs else None)
USAJOBS_RESULTS_PER_PAGE = int(_usajobs.get("results_per_page", 250))

# Getro network boards — [sources.getro] ({ enabled, boards, max_details }).
# A VC portfolio or association board that lists many employers' openings
# on one host, each posting naming its employer. OFF by default like
# USAJOBS: a board is a place, so there is no generic default. `boards` are
# board URLs (any page; only the host is used). `max_details` caps the
# posting pages fetched per board per crawl (see src/ats/fetchers/getro.py
# — titles are screened before any page fetch).
_getro = _src.get("getro", {})
GETRO_ENABLED = bool(_getro.get("enabled", False))
GETRO_BOARDS: list[str] = [str(b).strip() for b in (_getro.get("boards") or [])
                           if str(b).strip()]
GETRO_MAX_DETAILS = int(_getro.get("max_details", 150))

# Generic RSS/Atom feeds — [sources].rss ({ label, url, location }).
# Defaults to broad remote-job feeds; replace with your field's feeds
# (a society job board, a company blog's careers RSS, a niche aggregator).
_DEFAULT_RSS_FEEDS: list[tuple[str, str, str]] = [
    (
        "WeWorkRemotely - Programming",
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "Remote",
    ),
    (
        "WeWorkRemotely - All Other",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
        "Remote",
    ),
    (
        "Jobicy - All Remote",
        "https://jobicy.com/?feed=job_feed",
        "Remote",
    ),
]
# Presence of the key, not truthiness — `rss = []` deliberately means "no RSS
# feeds", which is different from "I didn't configure any, use the defaults".
RSS_FEEDS: list[tuple[str, str, str]] = ([
    (str(f.get("label") or f.get("url", "")), str(f.get("url", "")),
     str(f.get("location", "Remote")))
    for f in (_src.get("rss") or [])
    if f.get("url")
] if "rss" in _src else _DEFAULT_RSS_FEEDS)
