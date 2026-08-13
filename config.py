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
# default and max_tokens caps thinking+text together — core/claude.py
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

# Three roots:
#   SCRIPT_DIR — where the CODE lives (this file's directory; the exe's dir
#                when compiled). Bundled read-only assets live here.
#   APP_HOME   — where profile.toml lives. From source: SCRIPT_DIR. Compiled:
#                probed (see below), because the dist folder may sit INSIDE
#                the project checkout, whose root holds the real profile.
#   DATA_DIR   — where the DATA lives (DBs, résumé, job_reports/, caches,
#                captures, profile backups): JOBS_DATA_DIR env var, else
#                data/ under APP_HOME (legacy flat layouts still probed).
if "__compiled__" in globals():
    _exe_dir = Path(sys.argv[0]).resolve().parent
    SCRIPT_DIR = _exe_dir
    _env_home = os.environ.get("JOBS_DATA_DIR", "").strip()
    # APP_HOME: first place a real profile.toml (or an existing data/) is
    # found — the exe's own folder (copied-to-another-machine layout), else
    # the folder ABOVE the dist dir (dist still inside the checkout), else
    # the exe's folder (fresh install; profile.example.toml fallback).
    APP_HOME = next((d for d in (_exe_dir, _exe_dir.parent)
                     if (d / "profile.toml").exists()
                     or (d / "data" / "local_tech.db").exists()), _exe_dir)
    if _env_home:
        DATA_DIR = Path(_env_home)
    elif (APP_HOME / "data" / "local_tech.db").exists():
        DATA_DIR = APP_HOME / "data"
    elif (APP_HOME / "local_tech.db").exists():
        DATA_DIR = APP_HOME          # legacy flat layout (pre-data/ builds)
    else:
        DATA_DIR = APP_HOME / "data"
else:
    SCRIPT_DIR = Path(__file__).parent
    APP_HOME = SCRIPT_DIR
    _env_home = os.environ.get("JOBS_DATA_DIR", "").strip()
    DATA_DIR = Path(_env_home) if _env_home else APP_HOME / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Unified store: companies (cached mission scores, scope tags) + jobs
# (dedup state, per-track fields, resume-fit scores). Shared by every
# track — see store.py. Named local_tech.db for continuity with the
# pre-merge local-track store; existing DBs migrate in place.
STORE_DB_PATH = DATA_DIR / "local_tech.db"

# The neural sweep track's own store — kept separate from STORE_DB_PATH so
# its location-agnostic sweep of neural/BCI companies never commingles with
# the local track's jobs table again. Same schema; seeded from a one-time
# copy of the companies table.
NEURAL_DB_PATH = DATA_DIR / "neural.db"

# Resume used for per-job fit scoring (gitignored — personal). Extracted
# lazily by resume.py.
RESUME_PATH = DATA_DIR / "Aaron 2026 Resume.docx"

REPORT_DIR  = DATA_DIR / "job_reports"

# One cap for JD text everywhere — fetchers, hydration, DB storage, and the
# scoring prompt (see core/fit.py clip_desc, which keeps head + tail so
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


# Canonical location of the user's (gitignored) profile; the Settings tab
# writes here (core/profile_edit.py). APP_HOME so a compiled build inside
# the checkout finds the project's real profile, not a fresh template copy
# beside the exe. The example template falls back to the bundled copy in
# the dist folder (SCRIPT_DIR) when APP_HOME has none.
PROFILE_PATH = APP_HOME / "profile.toml"
PROFILE_EXAMPLE_PATH = (APP_HOME / "profile.example.toml"
                        if (APP_HOME / "profile.example.toml").exists()
                        else SCRIPT_DIR / "profile.example.toml")


def _load_profile():
    for p in (PROFILE_PATH, PROFILE_EXAMPLE_PATH):
        if p.exists():
            with open(p, "rb") as fh:
                return tomllib.load(fh), p.name
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
# Regex fragments (ORed together in core/filters.scrub_boilerplate) for
# benefits/EEO/infra-health idioms that contain domain-looking words without
# meaning them.
EXCLUDE_BOILERPLATE_PHRASES = list(_exc.get("boilerplate_phrases", []))

# Per-track keyword/exclude overrides — [keywords.<track>] / [exclude.<track>]
# tables. Tracks read their own sub-dict (e.g. KEYWORDS_BY_TRACK.get("local_tech"))
# instead of hardcoding their vocabulary; see scrapers/runner.py.
KEYWORDS_BY_TRACK = {k: v for k, v in _kw.items() if isinstance(v, dict)}
EXCLUDE_BY_TRACK   = {k: v for k, v in _exc.items() if isinstance(v, dict)}

LOCATION_ONSITE_INCLUDE = list(_loc.get("onsite", []))
LOCATION_REMOTE_INCLUDE = list(_loc.get("remote", []))
ACCEPT_REMOTE           = bool(_loc.get("accept_remote", False))
LOCATION_EXCLUDE        = list(_loc.get("exclude", []))
LOCATION_INCLUDE        = LOCATION_ONSITE_INCLUDE + LOCATION_REMOTE_INCLUDE

# --- Remote-eligibility detection (core/remote_filter.py) ---------------
REMOTE_LOC_TOKENS     = list(_loc.get("remote_tokens", []))
REMOTE_BODY_PHRASES   = list(_loc.get("remote_phrases", []))
REMOTE_HARD_NEGATIONS = list(_loc.get("hard_negations", []))
REMOTE_US_MARKERS     = list(_loc.get("us_markers", []))
REMOTE_NON_US_REGIONS = list(_loc.get("non_us_regions", []))

# --- Candidate identity (injected into Claude prompts; core/claude.py) --
CANDIDATE_SUMMARY   = (_cand.get("summary") or "").strip()
CANDIDATE_STRENGTHS = list(_cand.get("strengths", []))
CANDIDATE_FIT_CAPS  = list(_cand.get("fit_caps", []))
CANDIDATE_AVOID     = (_cand.get("avoid") or "").strip()

# --- Fit rubric (core/fit.py) — optional [fit] block; defaults apply if
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
# Deterministic "clearance" gate backstop (core/fit.py _CLEARANCE_RE).
# Empty -> fit.py falls back to its own built-in defaults.
FIT_CLEARANCE_VERBS      = list(_fitp.get("clearance_verbs", []))
FIT_CLEARANCE_QUALIFIERS = list(_fitp.get("clearance_qualifiers", []))

# --- Mission taxonomy (employer-alignment ladder; core/claude.py) -------
# Each tier: {"name", "desc", "band": [lo, hi], "active": bool}.
MISSION_TIERS = [
    {"name": t["name"], "desc": t.get("desc", ""),
     "band": list(t.get("band", [0.0, 1.0])), "active": bool(t.get("active", True))}
    for t in _mis.get("tiers", [])
]
MISSION_BULLSEYE_REGEX = (_mis.get("bullseye_regex") or "").strip()
MISSION_BULLSEYE_TIER  = (_mis.get("bullseye_tier") or "").strip()

# --- Locality (what counts as "local"; core/locality.py) ----------------------
LOCALITY_NAME         = (_lcl.get("name") or "local").strip()
LOCALITY_WORD_TOKENS  = list(_lcl.get("word_tokens", []))
LOCALITY_SUBSTRINGS   = list(_lcl.get("substrings", []))
LOCALITY_STATE_SUFFIX = list(_lcl.get("state_suffix", []))

# --- Discovery sourcing (discover.py --local; discovery/local_sourcing) -
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
# search result to a company's own ATS board (discovery/local_sourcing.py).
DISCOVERY_AGGREGATOR_HOSTS    = tuple(_dsc.get("aggregator_hosts", []))
DISCOVERY_GENERIC_NAME_WORDS  = set(_dsc.get("generic_name_words", []))
# Named company targets a track fetches first (list of {name, ats, slug}).
DISCOVERY_PRIORITY_COMPANIES = [
    (c.get("name"), c.get("ats"), c.get("slug"))
    for c in _dsc.get("priority_companies", [])
]

# --- UI tracks (webapp.py) — [tracks.<id>] tables ------------------------------
# Each track bundles a DB, a jobs.track value, ranking knobs, and the UI
# filter defaults that flip on switch. When the section is absent, the two
# built-in tracks are synthesized so existing installs work unchanged.
_DEFAULT_TRACKS = {
    "local_tech": {
        "label": "Local", "db": "local_tech.db", "track": "local-tech",
        "engine": "local",
        "rank_by": "fit", "min_mission": 0.2, "min_fit_default": 0.0,
        "willing_to_move_default": False, "remote_requires_watch": True,
        "default": True,
    },
    "remote_neural": {
        "label": "Remote neural", "db": "neural.db", "track": "remote-neural",
        "engine": "neural",
        "rank_by": "fit", "min_mission": None, "min_fit_default": 0.5,
        "willing_to_move_default": True, "remote_requires_watch": False,
        "default": False,
    },
}

# Crawl-methodology defaults per engine. Every key is overridable in the
# track's [tracks.<id>] table; the engine just picks which legacy behavior
# bundle applies when a key is absent, so existing profiles keep working.
#   keyword_mode        "extend" (track keywords ADD to the global tiers) or
#                       "replace" (track keywords BECOME the tiers)
#   accept_remote       cfg.ACCEPT_REMOTE while this track crawls
#   sources             which source families the crawl assembles
#     .store            active companies from the track's own DB
#     .priority_companies  [discovery] priority_companies, fetched first
#     .aggregators      Discourse/RemoteOK/Remotive/HN/RSS feeds
#     .websearch        DDG web-search queries
#     .location_scoped  store boards fetched through the locality filter
#                       (whole-board for watched/neural-tagged companies);
#                       False = lightweight ATS sweep, location-agnostic
#   store_tag           only sweep store companies carrying this tag (None=all)
#   require_core_anchor gate: posting must hit a CORE keyword
#   geo_gate            gate: drop non-local non-remote postings (locality
#                       from [locality]); False = stamp remote_eligible only
#   verify_top          deep-verify the top N after scoring (0 = skip)
#   cost_guard          max postings scored per run without confirm (0 = off)
#   email               email the digest after a crawl (CLI --send overrides)
#   tech_title_regex    the technical-title gate (case-insensitive regex a
#                       title must match before any API spend)
#   exclude_gate        apply the [exclude.<id>] role/defense/nonclinical
#                       tables to postings (False = skip entirely)
_ENGINE_CRAWL_DEFAULTS = {
    "local": {
        "keyword_mode": "extend", "accept_remote": False,
        "sources": {"store": True, "priority_companies": False,
                    "aggregators": False, "websearch": False,
                    "location_scoped": True},
        "store_tag": None, "require_core_anchor": False, "geo_gate": True,
        "verify_top": 15, "cost_guard": 0, "email": False,
        "exclude_gate": True,
        "tech_title_regex": (
            r"engineer|scientist|develop|program(mer|ming)?|software|\bdata\b|"
            r"analyst|analytics|machine learning|\bml\b|\bai\b|bioinformatic|"
            r"biostatist|computational|informatics|quality|validation|"
            r"verification|\bqa\b|\btest\b|devops|infrastructure|platform|"
            r"database|statistician|scientific|automation|architect|"
            r"research associate|\br&d\b|modeling|python"),
    },
    "neural": {
        "keyword_mode": "replace", "accept_remote": True,
        "sources": {"store": True, "priority_companies": True,
                    "aggregators": True, "websearch": True,
                    "location_scoped": False},
        "store_tag": "neural", "require_core_anchor": True, "geo_gate": False,
        "verify_top": 0, "cost_guard": 300, "email": False,
        "exclude_gate": False,
        "tech_title_regex": (
            r"\b("
            r"engineer|engineering|developer|scientist|neuroscientist|"
            r"researcher|research|ml|machine learning|deep learning|ai|"
            r"algorithm|algorithms|software|firmware|hardware|data|analytics|"
            r"analyst|computational|quantitative|programmer|architect|"
            r"signal processing|decoding|robotics|systems|sciences|"
            r"technologist|informatics|bioinformatics|neurotech|devops|sre|"
            r"reliability|platform|modeling|simulation"
            r")\b"),
    },
}


def _build_ui_tracks(raw):
    tracks = {}
    for tid, t in (raw or _DEFAULT_TRACKS).items():
        if not isinstance(t, dict):
            continue
        engine = str(t.get("engine") or "local")
        eng_defaults = _ENGINE_CRAWL_DEFAULTS.get(
            engine, _ENGINE_CRAWL_DEFAULTS["local"])
        src = dict(eng_defaults["sources"])
        src.update({k: bool(v) for k, v in (t.get("sources") or {}).items()
                    if k in src})
        tracks[tid] = {
            "id": tid,
            "label": str(t.get("label") or tid),
            "db_path": DATA_DIR / str(t.get("db") or f"{tid}.db"),
            "track": str(t.get("track") or tid.replace("_", "-")),
            # Which crawl machinery this track runs on — "local" (the
            # location-scoped crawler, scrapers/ops.py) or
            # "neural" (the location-agnostic runner, remote_neural_run.py).
            # Code keys ops off the ENGINE, never off the user-chosen id.
            "engine": engine,
            "rank_by": str(t.get("rank_by") or "fit"),
            "min_mission": (float(t["min_mission"])
                            if t.get("min_mission") is not None else None),
            "min_fit_default": float(t.get("min_fit_default", 0.0)),
            "willing_to_move_default": bool(t.get("willing_to_move_default", False)),
            "remote_requires_watch": bool(t.get("remote_requires_watch", False)),
            "default": bool(t.get("default", False)),
            # --- crawl methodology (scrapers/runner.py) -----------
            "keyword_mode": str(t.get("keyword_mode")
                                or eng_defaults["keyword_mode"]),
            "accept_remote": bool(t.get("accept_remote",
                                        eng_defaults["accept_remote"])),
            "sources": src,
            "store_tag": (str(t["store_tag"]) if t.get("store_tag")
                          else eng_defaults["store_tag"]),
            "require_core_anchor": bool(t.get("require_core_anchor",
                                              eng_defaults["require_core_anchor"])),
            "geo_gate": bool(t.get("geo_gate", eng_defaults["geo_gate"])),
            "verify_top": int(t.get("verify_top", eng_defaults["verify_top"])),
            "cost_guard": int(t.get("cost_guard", eng_defaults["cost_guard"])),
            "email": bool(t.get("email", eng_defaults["email"])),
            "exclude_gate": bool(t.get("exclude_gate",
                                       eng_defaults["exclude_gate"])),
            "tech_title_regex": str(t.get("tech_title_regex")
                                    or eng_defaults["tech_title_regex"]),
        }
    return tracks


UI_TRACKS = _build_ui_tracks(_PROFILE.get("tracks"))
DEFAULT_TRACK = next((tid for tid, t in UI_TRACKS.items() if t["default"]),
                     next(iter(UI_TRACKS), None))

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


# Web searches for the sweep-style crawl (runner.build_sources, enabled by
# [tracks.*].sources.websearch). (label, query, max_results). DuckDuckGo
# text search; each result URL is parsed for JSON-LD JobPosting.
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
