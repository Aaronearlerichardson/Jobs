"""
Central configuration for the job crawler.

Edit THIS file to change target companies, keywords, and location filters.
Secrets come from environment variables (see top of file).

    PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
    cmd.exe:     set ANTHROPIC_API_KEY=sk-ant-...
    bash/zsh:    export ANTHROPIC_API_KEY=sk-ant-...
"""

import os
from pathlib import Path

# =========================================================================
#  SECRETS (env-var first, fallbacks kept for local dev only)
# =========================================================================

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "jakdaxter31@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "YOUR_APP_PASSWORD_HERE")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_API_KEY_HERE")
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL",       "claude-sonnet-4-6")

# CareerOneStop (DOL) Web API — free key exposes the National Labor Exchange
# (NLx) feed, where federal contractors must list openings (VEVRAA). Register
# at https://www.careeronestop.org/Developers/WebAPI/registration.aspx; DOL
# emails a UserId + token. Used by `python crawler.py --nlx "Meta,Google"`.
CAREERONESTOP_USER_ID = os.environ.get("CAREERONESTOP_USER_ID", "")
CAREERONESTOP_TOKEN   = os.environ.get("CAREERONESTOP_TOKEN",   "")

# =========================================================================
#  PATHS
# =========================================================================

SCRIPT_DIR  = Path(__file__).parent

# Unified store: companies (cached mission scores, scope tags) + jobs
# (dedup state, per-track fields, resume-fit scores). Shared by every
# track — see jobcrawler/store.py. Named local_tech.db for continuity with
# the pre-merge local-track store; existing DBs migrate in place.
STORE_DB_PATH = SCRIPT_DIR / "local_tech.db"

# Back-compat aliases. DB_PATH used to be a standalone per-track seen-jobs
# DB (seen_jobs_remote.db); jobcrawler/db.py now adapts old callers onto
# the unified store.
DB_PATH            = STORE_DB_PATH
LOCAL_TECH_DB_PATH = STORE_DB_PATH

# Resume used for per-job fit scoring (gitignored — personal). Extracted
# lazily by jobcrawler/resume.py.
RESUME_PATH = SCRIPT_DIR / "Aaron 2026 Resume.docx"

REPORT_DIR  = SCRIPT_DIR / "job_reports"

# =========================================================================
#  SEARCH PROFILE (keywords / locations / policy) — loaded from TOML
# =========================================================================
#
# Your search criteria live in profile.toml (gitignored), NOT in this file —
# so the crawler stays generic and your terms are easy to edit, share, or
# reset. Falls back to the checked-in profile.example.toml when profile.toml
# is absent. See profile.example.toml for the schema + the relevance model.

import tomllib


def _load_profile():
    for fname in ("profile.toml", "profile.example.toml"):
        p = SCRIPT_DIR / fname
        if p.exists():
            with open(p, "rb") as fh:
                return tomllib.load(fh), fname
    return {}, None


_PROFILE, PROFILE_SOURCE = _load_profile()
_kw   = _PROFILE.get("keywords", {})
_exc  = _PROFILE.get("exclude", {})
_loc  = _PROFILE.get("locations", {})
_pol  = _PROFILE.get("policy", {})
_cand = _PROFILE.get("candidate", {})
_mis  = _PROFILE.get("mission", {})
_lcl  = _PROFILE.get("locality", {})

# Tiered relevance: a job is relevant if it hits any CORE term, or a DOMAIN
# term AND a SKILL term (see profile.example.toml).
CORE_KEYWORDS   = list(_kw.get("core", []))
DOMAIN_KEYWORDS = list(_kw.get("domain", []))
SKILL_KEYWORDS  = list(_kw.get("skill", []))
# Flat back-compat view; --expand-live appends here (treated as Tier 1).
INCLUDE_KEYWORDS = CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS

EXCLUDE_PHRASES       = list(_exc.get("phrases", []))
EXCLUDE_TITLE_PHRASES = list(_exc.get("title_phrases", []))

LOCATION_ONSITE_INCLUDE = list(_loc.get("onsite", []))
LOCATION_REMOTE_INCLUDE = list(_loc.get("remote", []))
ACCEPT_REMOTE           = bool(_loc.get("accept_remote", False))
LOCATION_EXCLUDE        = list(_loc.get("exclude", []))
LOCATION_INCLUDE        = LOCATION_ONSITE_INCLUDE + LOCATION_REMOTE_INCLUDE

# --- Candidate identity (injected into Claude prompts; jobcrawler/claude.py) --
CANDIDATE_SUMMARY   = (_cand.get("summary") or "").strip()
CANDIDATE_STRENGTHS = list(_cand.get("strengths", []))
CANDIDATE_FIT_CAPS  = list(_cand.get("fit_caps", []))
CANDIDATE_AVOID     = (_cand.get("avoid") or "").strip()

# --- Mission taxonomy (employer-alignment ladder; jobcrawler/claude.py) -------
# Each tier: {"name", "desc", "band": [lo, hi], "active": bool}.
MISSION_TIERS = [
    {"name": t["name"], "desc": t.get("desc", ""),
     "band": list(t.get("band", [0.0, 1.0])), "active": bool(t.get("active", True))}
    for t in _mis.get("tiers", [])
]
MISSION_BULLSEYE_REGEX = (_mis.get("bullseye_regex") or "").strip()
MISSION_BULLSEYE_TIER  = (_mis.get("bullseye_tier") or "").strip()

# --- Locality (what counts as "local"; jobcrawler/nc.py) ----------------------
LOCALITY_NAME         = (_lcl.get("name") or "local").strip()
LOCALITY_WORD_TOKENS  = list(_lcl.get("word_tokens", []))
LOCALITY_SUBSTRINGS   = list(_lcl.get("substrings", []))
LOCALITY_STATE_SUFFIX = list(_lcl.get("state_suffix", []))

# =========================================================================
#  HTTP
# =========================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# =========================================================================
#  NON-ATS SOURCES + POLICY
# =========================================================================
#
# The per-ATS company ROSTER now lives in the SQLite store (companies
# table), not here. Manage it with:  discover.py --local / --add-board /
# --apply,  or  crawler.py --import-companies roster.json.  What remains
# below is non-ATS sources (forums / custom scrapes) and crawl policy.

DISCOURSE_BOARDS = [
    ("MNE Forum Jobs",           "https://mne.discourse.group", 9),
    ("Neurostars Announcements", "https://neurostars.org",      6),
]

# (company_name, page_url, css_selector_or_None)
CUSTOM_COMPANIES: list[tuple[str, str, str | None]] = []

# Conglomerates (from profile.toml [policy]) whose OVERALL mission scores
# "other" but which run aligned subdivisions worth surfacing. Kept ACTIVE,
# crawled through the keyword filter (only aligned roles survive), and ranked
# at MULTI_DIVISION_MISSION_FLOOR rather than their own low company score.
MULTI_DIVISION_COMPANIES = {s.strip().lower()
                            for s in _pol.get("multi_division", [])}
MULTI_DIVISION_MISSION_FLOOR = float(_pol.get("multi_division_mission_floor", 0.6))


def is_multi_division(name):
    """True if `name` is a known multi-division conglomerate (profile policy)."""
    return (name or "").strip().lower() in MULTI_DIVISION_COMPANIES
# =========================================================================
#  NEW GENERIC SOURCES (JSON-LD + sitemap + web search)
# =========================================================================
#
# These cover the bulk of Google-for-Jobs-visible listings without any
# per-vendor scraper. Modern career pages embed schema.org JobPosting
# records in <script type="application/ld+json">; we parse that directly.

# (company_name, careers_page_url)
# Fetcher will look for JSON-LD on the index page first, then follow
# job-like links and parse JSON-LD from each.
JSONLD_COMPANIES: list[tuple[str, str]] = [
    # ("Example Co", "https://example.com/careers"),
]

# (company_name, sitemap_url, url_filter_regex_or_None)
# url_filter_regex is applied to the URL path.  None = default job-URL hints.
SITEMAP_COMPANIES: list[tuple[str, str, str | None]] = [
    # ("Example Co", "https://example.com/sitemap.xml", r"/jobs?/"),
]

# (label, query_string, max_results)
# DuckDuckGo text search; each result URL is then parsed for JSON-LD.
# Use site: / inurl: operators to narrow.  Free, no API key, rate-limited
# by DDG (a few queries per minute is comfortable).
#
# NOTE: the Greenhouse site: query was dropped - DDG's index for
# boards.greenhouse.io is extremely stale (every hit we tried 404'd).
# Lever's is also stale but less so; we keep it as a long-tail sweep.
# The aggregator queries below (weworkremotely, himalayas, remote.co)
# cover non-company-owned boards where the job URLs stay live.
WEBSEARCH_QUERIES: list[tuple] = [
    (
        "Neural engineers on Lever",
        '("neural" OR "BCI" OR "EEG") ("engineer" OR "scientist") '
        'site:jobs.lever.co',
        15,
    ),
    (
        "Neural engineers on Ashby",
        '("neural" OR "BCI" OR "EEG") ("engineer" OR "scientist") '
        'site:jobs.ashbyhq.com',
        15,
    ),
    (
        "Neural jobs on WeWorkRemotely",
        '("neural" OR "BCI" OR "EEG" OR "neuroscience" OR "biomedical") '
        'site:weworkremotely.com',
        15,
        True,                                       # remote-only board
    ),
    (
        "Neural jobs on Himalayas",
        '("neural" OR "BCI" OR "EEG" OR "neuroscience" OR "biomedical") '
        'site:himalayas.app',
        15,
        True,                                       # remote-only board
    ),
    (
        "Neural jobs on Remote.co",
        '("neural" OR "BCI" OR "EEG" OR "neuroscience" OR "biomedical") '
        'site:remote.co',
        15,
        True,                                       # remote-only board
    ),
    (
        "Scientific computing on Wellfound",
        '("neural" OR "biomedical" OR "neuroscience" OR "signal processing") '
        '("engineer" OR "scientist") site:wellfound.com',
        15,
    ),
    (
        "Research jobs on BuiltIn",
        '("neural" OR "neuroscience" OR "BCI" OR "biomedical") '
        '("engineer" OR "scientist") site:builtin.com',
        15,
    ),
]

# =========================================================================
#  AGGREGATOR FEEDS (non-company-owned job boards, no API key required)
# =========================================================================
#
# These feeds are run-to-completion each crawl: one HTTP request returns
# every active listing, so they don't need per-company config.  Filtering
# happens in the fetcher via is_relevant().

# RemoteOK: single JSON endpoint at https://remoteok.com/api.
# Set to False to skip entirely.
REMOTEOK_ENABLED = True

# Remotive: https://remotive.com/api/remote-jobs (one category or all).
# Categories: "software-dev", "data", "all-others", etc. None = all.
REMOTIVE_ENABLED   = True
REMOTIVE_CATEGORY: str | None = None

# Hacker News "Ask HN: Who is hiring?" monthly thread.
# max_threads=2 covers the current + previous month's threads.
HNHIRING_ENABLED     = True
HNHIRING_MAX_THREADS = 2

# Generic RSS/Atom feeds. (label, url, default_location)
# Seeded with WeWorkRemotely category feeds; add Jobicy, RemoteRocketship,
# company blog RSS, etc.
RSS_FEEDS: list[tuple[str, str, str]] = [
    (
        "WeWorkRemotely - Programming",
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "Remote",
    ),
    (
        "WeWorkRemotely - Full-Stack",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
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

# =========================================================================
#  GATED-SITE CAPTURE CONFIG (Playwright)
# =========================================================================

# Keep roughly current — a stale UA is a red flag to fingerprinters.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
