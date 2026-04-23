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
REPORT_DIR  = SCRIPT_DIR / "job_reports"
SESSION_DIR = SCRIPT_DIR / "sessions"
PROFILE_COPY_DIR = SESSION_DIR / "chrome-profile"
CREDENTIALS_PATH          = SCRIPT_DIR / "credentials.json"
CREDENTIALS_TEMPLATE_PATH = SCRIPT_DIR / "credentials.json.template"

# =========================================================================
#  KEYWORD FILTERS
# =========================================================================

INCLUDE_KEYWORDS = [
    "bci", "brain-computer", "brain computer",
    "neural interface", "neural decoding", "neuroprosthetic",
    "closed-loop", "cortical", "electrophysiology",
    "eeg", "ecog", "ieeg", "lfp", "spike sorting",
    "fnirs", "meg", "emg", "neurotech", "neurostimulation",
    "neuroimaging", "computational neuroscience",
    "signal processing", "research engineer",
    "scientific software", "scientific computing", "data pipeline",
    "biomedical engineer", "fmri", "biomedical", "mri", "medical device",
    "real-time", "embedded software", "data manager",
    "MNE-Python", "wearable", "data visualization",
# --- biomedical engineering ---
"biomedical engineering", "bioengineering", "biomechanics",
"medical imaging", "medical robotics", "medical devices",
"implantable", "biosensor", "physiological signal",
"clinical engineering", "rehabilitation engineering",

# --- data science in medicine ---
"clinical data science", "healthcare data science", "medical ai",
"clinical machine learning", "clinical informatics", "health informatics",
"bioinformatics", "biostatistics", "biostatistician",
"clinical trial", "clinical research", "ehr", "electronic health record",
"digital health", "radiology ai", "pathology ai",
"clinical nlp", "medical nlp", "healthcare nlp", "clinical language model",
]

EXCLUDE_PHRASES = [
    "phd required", "ph.d. required", "doctoral degree required",
    "must have a phd", "requires a phd", "ph.d is required",
    "postdoc position", "post-doctoral", "postdoctoral position",
    "nurse", "research coordinator", "clinical coordinator",
    "Technologist"
]

# A job passes only if:
#   - LOCATION_INCLUDE is empty OR location matches at least one entry, AND
#   - location does not match any LOCATION_EXCLUDE entry.
# Lowercase substring matching.
LOCATION_INCLUDE = [
    "research triangle", "durham", "raleigh", "chapel hill", "rtp",
    "carrboro", "nc", "cary", "apex", "north carolina",
    "charlotte", "greensboro", "winston-salem", "asheville",
    "richmond", "virginia", "va",
    # "remote", "work from home", "wfh", "fully remote", "distributed",
    # "anywhere",
    "mid atlantic",
]

LOCATION_EXCLUDE = [
    "remote", "work from home", "wfh", "fully remote", "distributed", "anywhere"
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
WEBSEARCH_QUERIES: list[tuple[str, str, int]] = [
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
    ),
    (
        "Neural jobs on Himalayas",
        '("neural" OR "BCI" OR "EEG" OR "neuroscience" OR "biomedical") '
        'site:himalayas.app',
        15,
    ),
    (
        "Neural jobs on Remote.co",
        '("neural" OR "BCI" OR "EEG" OR "neuroscience" OR "biomedical") '
        'site:remote.co',
        15,
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
