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

# =========================================================================
#  PATHS
# =========================================================================

SCRIPT_DIR  = Path(__file__).parent
DB_PATH     = SCRIPT_DIR / "seen_jobs_remote.db"

# Per-branch isolated dedupe store for the LOCAL-CLINICAL-ML track. Kept
# distinct from DB_PATH so a concurrent crawl on another branch (e.g.
# track-remote-neural) can never lock-contend or cross-pollute dedup state.
# The --local-clinical run points config.DB_PATH here at startup.
LOCAL_CLINICAL_DB_PATH = SCRIPT_DIR / "jobs_local_clinical.db"

# LOCAL-TECH company + job store (companies table w/ cached mission score +
# jobs table w/ resume-fit score). See jobcrawler/store.py.
LOCAL_TECH_DB_PATH = SCRIPT_DIR / "local_tech.db"

# Résumé used for per-job fit scoring (gitignored — personal). Extracted
# lazily by jobcrawler/resume.py.
RESUME_PATH = SCRIPT_DIR / "Aaron 2026 Resume.docx"
REPORT_DIR  = SCRIPT_DIR / "job_reports"
SESSION_DIR = SCRIPT_DIR / "sessions"
PROFILE_COPY_DIR = SESSION_DIR / "chrome-profile"
CREDENTIALS_PATH          = SCRIPT_DIR / "credentials.json"
CREDENTIALS_TEMPLATE_PATH = SCRIPT_DIR / "credentials.json.template"

# =========================================================================
#  KEYWORD FILTERS
# =========================================================================

# -----------------------------------------------------------------------
# Tiered relevance matching.
#
#   A job is relevant if ANY of:
#     1. it matches a CORE_KEYWORDS term (standalone signal), OR
#     2. it matches both a DOMAIN_KEYWORDS term AND a SKILL_KEYWORDS term,
#        i.e. an adjacent medical/bio domain where your transferable skills
#        apply.
#
#   This lets CORE stay narrow ("only neurotech" signal) while DOMAIN+SKILL
#   pulls in adjacent medical/ML roles where your resume is still strong,
#   without opening the floodgates to generic SaaS roles that happen to use
#   PyTorch or have a "backend" in the title.
# -----------------------------------------------------------------------

# Tier 1: explicit neurotech / specific job titles. Any hit => relevant.
CORE_KEYWORDS = [
    # BCI / neural interfaces
    "bci", "brain-computer", "brain computer",
    "neural interface", "neural decoding", "neuroprosthetic",
    "neurotech", "neurostimulation", "closed-loop", "cortical",
    # Electrophysiology modalities
    "eeg", "ecog", "ieeg", "lfp", "fnirs", "meg", "emg",
    "spike sorting", "electrophysiology",
    # Neuroscience (specific enough to stand alone)
    "neuroscience", "neuroscientist", "neuroimaging",
    "computational neuroscience",
    # Tooling you specifically know
    "mne-python",
    # Specific job titles where the title alone = clear signal
    "biomedical engineer", "signal processing engineer",
    "neural engineer",
]

# Tier 2: adjacent medical/bio domains. Needs a SKILL_KEYWORDS pair to pass.
DOMAIN_KEYWORDS = [
    "biomedical", "medical device", "medical imaging",
    "mri", "fmri", "ultrasound",
    "wearable", "implantable",
    "physiological", "biosignal", "biosensor",
    "clinical", "clinical trial", "digital health", "healthtech",
    "radiology", "pathology", "cardiology", "sleep",
    "biostatistics", "bioinformatics",
    "ehr", "electronic health record",
]

# Tier 3: transferable technical skills. Only counts paired with DOMAIN.
# Terms here should describe things YOU can do - the filter will pair them
# with a DOMAIN term to confirm the role is in a relevant area.
SKILL_KEYWORDS = [
    "pytorch", "tensorflow",
    "signal processing", "time series", "dsp",
    "machine learning", "deep learning",
    "scientific software", "scientific computing",
    "research engineer", "data pipeline",
    "real-time", "embedded software", "firmware",
    "numpy", "scipy",
    "data manager", "backend",
]

# Backward-compat view. Referenced by --expand-live, --from-keywords, and
# the keyword-report. Mutations here (e.g. --expand-live appending) are
# treated as Tier 1 (standalone) by is_relevant().
INCLUDE_KEYWORDS = CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS

EXCLUDE_PHRASES = [
    "phd required", "ph.d. required", "doctoral degree required",
    "must have a phd", "requires a phd", "ph.d is required",
    "postdoc", "post-doc", "post-doctoral",
    "postdoc position", "postdoctoral position",
]

# -----------------------------------------------------------------------
# Two buckets: physical locations you're willing to commute to, and
# remote-work markers. A job passes the location gate iff:
#   * EXCLUDE doesn't match, AND
#   * at least one of:
#       - an ONSITE term matches,
#       - ACCEPT_REMOTE is True and a REMOTE term matches,
#       - a legacy/dynamic LOCATION_INCLUDE term matches (not in a bucket).
#
# Short tokens ("nc", "va", "rtp") use word-boundary matching so that
# "Clinical Research, MA" doesn't spuriously pass the "nc" filter.
# -----------------------------------------------------------------------

LOCATION_ONSITE_INCLUDE = [
    "research triangle", "durham", "raleigh", "chapel hill", "rtp",
    "carrboro", "nc", "cary", "apex", "north carolina",
    "charlotte", "greensboro", "winston-salem", "asheville",
    "richmond", "virginia", "va", "mid atlantic",
]

LOCATION_REMOTE_INCLUDE = [
    "remote", "work from home", "wfh", "fully remote",
    "distributed", "anywhere",
]

# Master switch for remote listings. Flip to False for a pure-local crawl.
ACCEPT_REMOTE = True

LOCATION_EXCLUDE: list[str] = []

# Back-compat union view. Referenced by --expand-location-live (which
# appends here) and by the report's dup-check. Additions go here first
# and are treated as "allowed" by the filter until you classify them.
LOCATION_INCLUDE = LOCATION_ONSITE_INCLUDE + LOCATION_REMOTE_INCLUDE

# =========================================================================
#  HTTP
# =========================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# =========================================================================
#  TARGET COMPANIES (per-ATS dispatch)
# =========================================================================

GREENHOUSE_COMPANIES = {
    "neuralink":        "Neuralink",
    "beaconbiosignals": "Beacon Biosignals",
    "neuropace":        "NeuroPace",
}

LEVER_COMPANIES = {
    "kitware": "Kitware",
}

ASHBY_COMPANIES: dict[str, str] = {}

KULA_COMPANIES = [
    ("Precision Neuroscience", "precision-neuroscience"),
]

DISCOURSE_BOARDS = [
    ("MNE Forum Jobs",           "https://mne.discourse.group", 9),
    ("Neurostars Announcements", "https://neurostars.org",      6),
]

# (company_name, page_url, css_selector_or_None)
CUSTOM_COMPANIES: list[tuple[str, str, str | None]] = []

# (company_name, base_url).  Scraper appends /search/ + paging.
SUCCESSFACTORS_COMPANIES = [
    ("Duke University", "https://careers.duke.edu"),
    ("Duke Health",     "https://careers.dukehealth.org"),
]

# (tenant, wd_pod, site, company_name)
WORKDAY_COMPANIES: list[tuple[str, int, str, str]] = []

# (host, company_name)
PEOPLEADMIN_COMPANIES = [
    ("unc.peopleadmin.com", "UNC Chapel Hill"),
]

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

# (label, query_string, max_results [, remote_only])
# DuckDuckGo text search; each result URL is then parsed for JSON-LD.
# Use site: / inurl: operators to narrow.  Free, no API key, rate-limited
# by DDG (a few queries per minute is comfortable).
#
# The optional 4th element flags a query as remote-only — the orchestrator
# skips those entries when ACCEPT_REMOTE is False so a pure-local crawl
# doesn't burn DDG budget on boards that carry nothing but remote roles.
# Missing = False (general query; run unconditionally).
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

SITE_CONFIGS = {
    "linkedin": {
        "login_url":  "https://www.linkedin.com/login",
        "verify_url": "https://www.linkedin.com/feed/",
        "logged_in_url_markers":  ["/feed"],
        "logged_out_url_markers": ["/login", "/uas/login", "/checkpoint", "/authwall"],
    },
    "indeed": {
        "login_url":  "https://secure.indeed.com/auth",
        "verify_url": "https://myjobs.indeed.com/",
        "logged_in_url_markers":  ["myjobs.indeed.com"],
        "logged_out_url_markers": ["/auth", "/account/login"],
    },
    "wellfound": {
        "login_url":  "https://wellfound.com/login",
        "verify_url": "https://wellfound.com/jobs",
        "logged_in_url_markers":  ["/jobs", "/candidate", "/user"],
        "logged_out_url_markers": ["/login", "/signup"],
    },
}

# Keep roughly current — a stale UA is a red flag to fingerprinters.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
