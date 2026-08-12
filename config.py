"""
Central configuration for the job crawler.

Edit THIS file to change target companies, keywords, and location filters.
Secrets come from environment variables (see top of file).

    PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
    cmd.exe:     set ANTHROPIC_API_KEY=sk-ant-...
    bash/zsh:    export ANTHROPIC_API_KEY=sk-ant-...
"""

import os
import re
import sys
from pathlib import Path

# =========================================================================
#  SECRETS (env-var first, fallbacks kept for local dev only)
# =========================================================================

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "jakdaxter31@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "YOUR_APP_PASSWORD_HERE")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_API_KEY_HERE")
# Screen/mission/expansion calls: Sonnet 5 — near-Opus quality at Sonnet
# pricing ($3/$15 per MTok; intro $2/$10 through 2026-08-31, cheaper than the
# Sonnet 4.6 it replaces). NOTE for 5-family models: thinking is ON by
# default and max_tokens caps thinking+text together — jobcrawler/claude.py
# disables thinking for these small structured-JSON calls.
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL",       "claude-sonnet-5")
# Deep-verify pass over ranking finalists only (~15-30 calls/run, judgment-
# heavy): Opus 5 with adaptive thinking. $5/$25 per MTok, but bounded volume.
CLAUDE_VERIFY_MODEL = os.environ.get("CLAUDE_VERIFY_MODEL", "claude-opus-5")

# CareerOneStop (DOL) Web API — free key exposes the National Labor Exchange
# (NLx) feed, where federal contractors must list openings (VEVRAA). Register
# at https://www.careeronestop.org/Developers/WebAPI/registration.aspx; DOL
# emails a UserId + token. Used by `python crawler.py --nlx "Meta,Google"`.
CAREERONESTOP_USER_ID = os.environ.get("CAREERONESTOP_USER_ID", "")
CAREERONESTOP_TOKEN   = os.environ.get("CAREERONESTOP_TOKEN",   "")

# =========================================================================
#  PATHS
# =========================================================================

# Where the app's data lives (DB, profile.toml, résumé, job_reports/).
# Running from source: this file's directory. In a compiled build (Nuitka
# defines "__compiled__" in every compiled module), resolved in order:
#   1. JOBS_DATA_DIR env var — explicit override.
#   2. The exe's own folder, when it already holds data (local_tech.db or
#      profile.toml) — the copied-to-another-machine case.
#   3. The exe folder's PARENT, when THAT holds local_tech.db — i.e. the
#      dist folder still lives inside the project checkout; use the real
#      project data instead of spawning a second empty DB beside the exe.
#   4. Otherwise the exe's folder (fresh install: a new DB is created there).
if "__compiled__" in globals():
    _exe_dir = Path(sys.argv[0]).resolve().parent
    _env_home = os.environ.get("JOBS_DATA_DIR", "").strip()
    if _env_home:
        SCRIPT_DIR = Path(_env_home)
    elif (_exe_dir / "local_tech.db").exists() or (_exe_dir / "profile.toml").exists():
        SCRIPT_DIR = _exe_dir
    elif (_exe_dir.parent / "local_tech.db").exists():
        SCRIPT_DIR = _exe_dir.parent
    else:
        SCRIPT_DIR = _exe_dir
else:
    SCRIPT_DIR = Path(__file__).parent

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

# The REMOTE-NEURAL track's own store — kept separate from STORE_DB_PATH so
# its (now location-agnostic; see jobcrawler/tracks/remote_neural_run.py)
# sweep of neural/BCI companies never commingles with local-tech's jobs
# table again. Same schema (jobcrawler/store.py); seeded from a one-time
# copy of the companies table.
NEURAL_DB_PATH = SCRIPT_DIR / "neural.db"

# Resume used for per-job fit scoring (gitignored — personal). Extracted
# lazily by jobcrawler/resume.py.
RESUME_PATH = SCRIPT_DIR / "Aaron 2026 Resume.docx"

REPORT_DIR  = SCRIPT_DIR / "job_reports"

# One cap for JD text everywhere — fetchers, hydration, DB storage, and the
# scoring prompt (see jobcrawler/fit.py clip_desc, which keeps head + tail so
# a requirements block at the END of a long posting survives). The old
# per-site caps (2000/2500/4000) silently fed the scorer only the opening
# company boilerplate of long JDs: Ceribell's Sr Manager posting was 20k
# chars with the disqualifying "8+ years TPM/GCP" block at char 7000, and it
# scored 0.69 on the first 2500 chars. ~12k chars ≈ ~3k tokens per scoring
# call — the honesty is worth the marginal cost.
MAX_DESC_CHARS = 12000

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
_dsc  = _PROFILE.get("discovery", {})

# Tiered relevance: a job is relevant if it hits any CORE term, or a DOMAIN
# term AND a SKILL term (see profile.example.toml).
CORE_KEYWORDS   = list(_kw.get("core", []))
DOMAIN_KEYWORDS = list(_kw.get("domain", []))
SKILL_KEYWORDS  = list(_kw.get("skill", []))
# Flat back-compat view; --expand-live appends here (treated as Tier 1).
INCLUDE_KEYWORDS = CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS

EXCLUDE_PHRASES       = list(_exc.get("phrases", []))
EXCLUDE_TITLE_PHRASES = list(_exc.get("title_phrases", []))
# Regex fragments (ORed together in jobcrawler/filters.scrub_boilerplate) for
# benefits/EEO/infra-health idioms that contain domain-looking words without
# meaning them.
EXCLUDE_BOILERPLATE_PHRASES = list(_exc.get("boilerplate_phrases", []))

# Per-track keyword/exclude overrides — [keywords.<track>] / [exclude.<track>]
# tables. Tracks read their own sub-dict (e.g. KEYWORDS_BY_TRACK.get("local_tech"))
# instead of hardcoding their vocabulary; see jobcrawler/tracks/*.py.
KEYWORDS_BY_TRACK = {k: v for k, v in _kw.items() if isinstance(v, dict)}
EXCLUDE_BY_TRACK   = {k: v for k, v in _exc.items() if isinstance(v, dict)}

LOCATION_ONSITE_INCLUDE = list(_loc.get("onsite", []))
LOCATION_REMOTE_INCLUDE = list(_loc.get("remote", []))
ACCEPT_REMOTE           = bool(_loc.get("accept_remote", False))
LOCATION_EXCLUDE        = list(_loc.get("exclude", []))
LOCATION_INCLUDE        = LOCATION_ONSITE_INCLUDE + LOCATION_REMOTE_INCLUDE

# --- Remote-eligibility detection (jobcrawler/remote_filter.py) ---------------
REMOTE_LOC_TOKENS     = list(_loc.get("remote_tokens", []))
REMOTE_BODY_PHRASES   = list(_loc.get("remote_phrases", []))
REMOTE_HARD_NEGATIONS = list(_loc.get("hard_negations", []))
REMOTE_US_MARKERS     = list(_loc.get("us_markers", []))
REMOTE_NON_US_REGIONS = list(_loc.get("non_us_regions", []))

# --- Candidate identity (injected into Claude prompts; jobcrawler/claude.py) --
CANDIDATE_SUMMARY   = (_cand.get("summary") or "").strip()
CANDIDATE_STRENGTHS = list(_cand.get("strengths", []))
CANDIDATE_FIT_CAPS  = list(_cand.get("fit_caps", []))
CANDIDATE_AVOID     = (_cand.get("avoid") or "").strip()

# --- Fit rubric (jobcrawler/fit.py) — optional [fit] block; defaults apply if
#     absent. weights/gate_penalty are dicts; domain_ladder is a list of
#     {score, terms=[...]}; stack_* / region_terms are lists joined to text. ---
_fitp = _PROFILE.get("fit", {})
FIT_WEIGHTS       = _fitp.get("weights") or None
FIT_GATE_PENALTY  = _fitp.get("gate_penalty") or None
FIT_DOMAIN_LADDER = _fitp.get("domain_ladder") or None
FIT_STACK_CORE    = ", ".join(_fitp.get("stack_core", [])) or None
FIT_STACK_ANTI    = ", ".join(_fitp.get("stack_anti", [])) or None
FIT_REGION        = ", ".join(_fitp.get("region_terms", [])) or None
# How many of your own --mark decisions (applied/dismissed, each) are fed to
# the fit scorer as few-shot calibration. None -> default 3; 0 disables.
FIT_DISPOSITION_EXAMPLES = _fitp.get("disposition_examples")
# Deterministic "clearance" gate backstop (jobcrawler/fit.py _CLEARANCE_RE).
# Empty -> fit.py falls back to its own built-in defaults.
FIT_CLEARANCE_VERBS      = list(_fitp.get("clearance_verbs", []))
FIT_CLEARANCE_QUALIFIERS = list(_fitp.get("clearance_qualifiers", []))

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

# --- Discovery sourcing (discover.py --local; jobcrawler/discovery/local_sourcing) -
DISCOVERY_SEED_COMPANIES     = list(_dsc.get("seed_companies", []))
DISCOVERY_WORKDAY_MAJORS     = list(_dsc.get("workday_majors", []))
DISCOVERY_DIRECTORY_URLS     = list(_dsc.get("directory_urls", []))
DISCOVERY_NAME_SEARCH_QUERIES = list(_dsc.get("name_search_queries", []))
# LLM name-brainstorm source for discovery (names verified downstream, so
# hallucinations are harmless). None -> default 50; 0 disables.
DISCOVERY_BRAINSTORM_NAMES   = _dsc.get("brainstorm_names")
DISCOVERY_NAME_BLOCKLIST     = {re.sub(r"[^a-z0-9]", "", n.lower())
                                for n in _dsc.get("name_blocklist", [])}
# Job-aggregator hosts to skip, and generic words to ignore, when resolving a
# search result to a company's own ATS board (jobcrawler/discovery/local_sourcing.py).
DISCOVERY_AGGREGATOR_HOSTS    = tuple(_dsc.get("aggregator_hosts", []))
DISCOVERY_GENERIC_NAME_WORDS  = set(_dsc.get("generic_name_words", []))
# Named company targets a track fetches first (list of {name, ats, slug}).
DISCOVERY_PRIORITY_COMPANIES = [
    (c.get("name"), c.get("ats"), c.get("slug"))
    for c in _dsc.get("priority_companies", [])
]

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

# Remote-leaning web searches used by the REMOTE-NEURAL track specifically
# (jobcrawler/tracks/remote_neural.py) — separate from the general
# WEBSEARCH_QUERIES above. (label, query, max_results).
REMOTE_NEURAL_WEBSEARCH_QUERIES: list[tuple] = [
    ("Neural-ML on WeWorkRemotely",
     '("neural" OR "BCI" OR "EEG" OR "neurotech" OR "brain-computer") '
     '("engineer" OR "scientist") site:weworkremotely.com', 12),
    ("Neural-ML on Himalayas",
     '("neural" OR "BCI" OR "EEG" OR "neural decoding" OR "neurotech") '
     'site:himalayas.app', 12),
    ("Neural-ML on Remote.co",
     '("neural" OR "BCI" OR "EEG" OR "neuroscience") site:remote.co', 12),
    ("Neural-ML remote on Lever",
     '("neural" OR "BCI" OR "EEG" OR "neural signal") '
     '("remote") site:jobs.lever.co', 12),
    ("Neural-ML remote on Ashby",
     '("neural" OR "BCI" OR "EEG" OR "neural decoding") '
     '("remote") site:jobs.ashbyhq.com', 12),
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
