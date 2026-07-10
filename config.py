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
# Precision notes (learned tuning the local track): bare "medical" and
# "cancer" fire on benefits boilerplate ("medical, dental, vision"), and
# catch-all "imaging" matched everything from satellites to cameras — use
# the qualified forms instead.
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
# Precision notes: bare "signal" leaked military RF/SDR roles into a
# clinical search; bare "data"/"analysis"/"software" made the DOMAIN+SKILL
# pair fire on nearly any posting. Qualified forms only.
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
    "Nurses", "nursing", "nurse practitioner",
    "md required", "medical doctor",
     # Exclude "clinical research" but not "clinical research engineer"
    "clinical research coordinator",
]

# Matched against the TITLE only. "manager" used to live in
# EXCLUDE_PHRASES, where it also matched descriptions — killing any
# engineer role whose posting said "reports to the Engineering Manager"
# or "work closely with product managers".
EXCLUDE_TITLE_PHRASES = [
    "manager",
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
ACCEPT_REMOTE = False

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
    # --- discovered 2026-04-23: pharma companies in rtp ---
    "corcepttherapeutics": "Corcept Therapeutics",  # 44 job(s), discovered
    "rti": "RTI International",  # 15 job(s), discovered
    "bandwidth": "Bandwidth Inc.",  # 47 job(s), discovered
    "epicgames": "Epic Games",  # 69 job(s), discovered
    "pendo": "Pendo",  # 17 job(s), discovered
    # --- discovered 2026-04-23: neurotech RTP ---
    # NOTE: greenhouse slug "nuro" is Nuro the autonomous-delivery company,
    # NOT Nurokor/NuroMetrix (slug collision from first-word probing).
    # Kept because its postings are filtered by keywords anyway; remove if
    # the fetch is wasted time.
    "nuro": "Nuro (autonomous delivery)",  # 93 job(s), discovered
    "sas": "SAS Institute",  # 5 job(s), discovered
    # --- discovered 2026-04-23: medical tech companies in RTP ---
    "ceribell": "Ceribell",  # 46 job(s), discovered
    "pairwise": "Pairwise",  # 0 job(s), discovered
    # --- discovered 2026-06-12: bci ---
    "motifneurotech": "Motif Neurotech",  # 1 job(s), discovered — VERIFY: found on greenhouse, not Claude's guess (ashby)
}

LEVER_COMPANIES = {
    "kitware": "Kitware",
    # --- discovered 2026-04-23: pharma companies in rtp ---
    "pryon": "Pryon",  # 6 job(s), discovered
    "spreedly": "Spreedly",  # 11 job(s), discovered
    # --- discovered 2026-06-12: ieeg ---
    "ucsf": "UCSF Weill Institute for Neurosciences (clinical ops/eng roles)",  # 1 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    # --- discovered 2026-06-12: lfp ---
    "turing": "Turing Award (Applied Neuroscience / Dreem by Beacon Biosignals)",  # 17 job(s), discovered — VERIFY: found on greenhouse, not Claude's guess (ashby); first-word slug - confirm it's the same company
    # --- discovered 2026-06-12: fnirs ---
    "starfishneuroscience": "Starfish Neuroscience",  # 1 job(s), discovered — VERIFY: found on greenhouse, not Claude's guess (ashby)
    # --- discovered 2026-06-12: bciwiki:companies ---
    "signal": "Bio-Signal Technologies",  # 0 job(s), discovered — VERIFY: generic slug 'signal' - likely a different company
    "hint": "HiNT",  # 2 job(s), discovered
    "mindful": "Mindful Scientific",  # 10 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
}

ASHBY_COMPANIES: dict[str, str] = {
    # --- discovered 2026-04-23: pharma companies in rtp ---
    "novo": "Novo Nordisk",  # 0 job(s), discovered
    # --- discovered 2026-04-23: neurotech RTP ---
    "brainco": "BrainCo",  # 0 job(s), discovered
    # --- discovered 2026-06-12: bci ---
    "kernel": "Kernel",  # 0 job(s), discovered — VERIFY: found on ashby, not Claude's guess (greenhouse)
    # --- discovered 2026-06-12: neurostimulation ---
    "calahealth": "Cala Health",  # 6 job(s), discovered
    # --- discovered 2026-06-12: cortical ---
    "independent": "BrainFlow (Mindplex / independent OSS backed roles)",  # 1 job(s), discovered
    # --- discovered 2026-06-12: ecog ---
    "turing": "Turing Medical",  # 17 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    # --- discovered 2026-06-12: bciwiki:companies ---
    "alljoined": "Alljoined",  # 0 job(s), discovered
    "alpha": "Alpha MED Scientific",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "amo": "AMO Lab",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "applied": "Applied Neuroscience",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "atlas": "ATLAS Neuroengineering",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "blink": "Blink Device Company",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "Pear-VC": "Blueberry",  # 0 job(s), discovered — VERIFY: sniffed from careers page (ashby)
    "comind": "CoMind",  # 0 job(s), discovered
    "dynamic": "Dynamic Neurotech",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "focus": "Foc.us",  # 0 job(s), discovered
    "helius": "Helius Medical Technologies",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "human": "Human Waves",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "impulse": "Impulse Neiry",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "infinite": "Infinite Biomedical Technologies",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "mainstay": "Mainstay Medical",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "neural-earth": "Neural FLEX",  # 0 job(s), discovered — VERIFY: sniffed from careers page (ashby)
    "neuralace": "NeuraLace Medical",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "omniscient": "Omniscient Neurotechnology",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "quantum": "Quantum Thinkers",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "symbiotic": "Symbiotic Devices",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
}

KULA_COMPANIES = [
    ("Precision Neuroscience", "precision-neuroscience"),
    # --- discovered 2026-06-12: bciwiki:companies ---
    ("Flow Neuroscience", "flow-com"),  # 0 job(s), discovered — VERIFY: sniffed from careers page (kula)
]

# JazzHR boards: {subdomain: company}. Board at https://{sub}.applytojob.com
JAZZHR_COMPANIES: dict[str, str] = {
    "paradromicsinc": "Paradromics",
    # --- discovered 2026-06-12: bciwiki:companies ---
    "chroma": "Chroma NV",  # 2 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "halo": "Halo Neuroscience",  # 2 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "npi": "NPI Electronic",  # 2 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
}

# BambooHR boards: {subdomain: company}. JSON at /careers/list.
BAMBOOHR_COMPANIES: dict[str, str] = {
    "cognixion": "Cognixion",  # 3 job(s), discovered 2026-06-12
    # --- discovered 2026-06-12: bci ---
    "blackrock": "Blackrock Neurotech",  # 2 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    # --- discovered 2026-06-12: neurostimulation ---
    "neuralace": "Neuralace Medical",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    # --- discovered 2026-06-12: cortical ---
    "interaxon": "Muse (InteraXon)",  # 0 job(s), discovered
    # --- discovered 2026-06-12: eeg ---
    "g": "g.tec medical engineering",  # 4 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    # --- discovered 2026-06-12: bciwiki:companies ---
    "aestudio": "AE Studio",  # 6 job(s), discovered
    "attune": "Attune Neurosciences",  # 1 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "cadwell": "Cadwell Industries",  # 16 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "cortex": "Cortex Brainwave Technologies",  # 4 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "harvard": "Harvard Bioscience",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "highland": "Highland Instruments",  # 2 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "inter": "Inter",  # 163 job(s), discovered
    "iris": "Iris Biomedical",  # 12 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "london": "London Cognition",  # 4 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "maxwell": "MaxWell Biosystems",  # 5 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "neu": "NeU",  # 6 job(s), discovered
    "nordicnaturals": "Nordic Neurostim",  # 4 job(s), discovered — VERIFY: sniffed from careers page (greenhouse)
    "omicron": "Omicron T",  # 1 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "peak": "Peak Achievement Training",  # 48 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "reach": "Reach Neuro",  # 2 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "ripple": "Ripple Neuro",  # 148 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "setpoint": "Setpoint Medical",  # 14 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "sonic": "Sonic Concepts",  # 17 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
    "starlabs": "Star Labs Bioscience",  # 0 job(s), discovered
    "upside": "Upside Down Labs",  # 0 job(s), discovered — VERIFY: first-word slug - confirm it's the same company
}

# ADP Workforce Now career centers: (company, cid, ccid). The cid/ccid
# pair comes from the careers-page embed URL (discovery sniffs it).
ADP_COMPANIES: list[tuple[str, str, str]] = [
    ("Synchron", "d290c04e-0230-4cd9-8bf0-f116bfab1405", "19000101_000003"),
    # --- discovered 2026-06-12: bciwiki:companies ---
    ("Aspect Imaging", "7120c628-221c-4769-b7e7-8ab11b78b67f", "9200879253113_2"),  # 1 job(s), discovered — VERIFY: sniffed from careers page (adp)
    ("TMS Center of Colorado", "227380c2-b7ef-4c34-8220-db444305af06", "19000101_000001"),  # 6 job(s), discovered — VERIFY: sniffed from careers page (adp)
    ("Zander Labs", "436434db-fa8e-46eb-9baa-f565ea20b2d9", "19000101_000001"),  # 9 job(s), discovered — VERIFY: sniffed from careers page (adp)
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
WORKDAY_COMPANIES: list[tuple[str, int, str, str]] = [
    # --- discovered 2026-04-23: biotech companies in RTP ---
    ("osv-bioventus", 501, "External", "Bioventus"),  # 0 job(s), discovered
    ("redhat", 5, "jobs", "Red Hat (IBM subsidiary, RTP HQ)"),  # 365 job(s), discovered
    ("vhr-unither", 5, "External", "United Therapeutics"),  # 0 job(s), discovered
    ("askbio", 12, "AskBio", "AskBio"),  # 12 job(s), discovered
    ("bdx", 1, "EXTERNAL_CAREER_SITE_USA", "BD"),  # 512 job(s), discovered
    # --- discovered 2026-04-23: medical tech companies in RTP ---
    ("medtronic", 1, "MedtronicCareers", "Medtronic"),  # 1206 job(s), discovered
    ("iqvia", 1, "IQVIA", "Quintiles IMS (IQVIA)"),  # 1854 job(s), discovered
    # --- discovered 2026-04-23: medical tech companies in RTP ---
    ("cree", 108, "EXT", "Cree / Wolfspeed"),  # 108 job(s), discovered
    ("labcorp", 1, "External", "LabCorp"),  # 1438 job(s), discovered
    # --- discovered 2026-04-23: pharma companies in RTP ---
    ("biibhr", 3, "external", "Biogen"),  # 258 job(s), discovered
    ("gsk", 5, "GSKCareers", "GSK (GlaxoSmithKline) RTP"),  # 793 job(s), discovered
    ("viatris", 5, "External", "Medicago (now acquired/dissolved) / Viatris RTP"),  # 255 job(s), discovered
    # --- discovered 2026-06-12: bciwiki:companies ---
    ("philips", 3, "jobs-and-careers", "Philips"),  # 1044 job(s), discovered
]

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
