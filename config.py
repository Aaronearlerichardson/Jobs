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

# Dependency-free (imports nothing, not even this module) — safe here.
from core import tags

# =========================================================================
#  SECRETS (env-var first, fallbacks kept for local dev only)
# =========================================================================

def env(name, default=""):
    """An env var's value, treating BLANK as unset.

    `os.environ.get(name, default)` returns "" when the variable exists but
    is empty — so `ANTHROPIC_API_KEY=""` (a CI runner exporting it, a shell
    profile clearing it) read as "a key is configured" and the scorers tried
    to authenticate with nothing instead of degrading to their offline
    fallbacks. Blank means absent everywhere in this file.
    """
    return (os.environ.get(name) or "").strip() or default


# Digest email is opt-in and OFF until you set both of these — there is no
# built-in address. Blank GMAIL_ADDRESS simply disables emailing (core/digest.py).
GMAIL_ADDRESS      = env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD", "YOUR_APP_PASSWORD_HERE")
ANTHROPIC_API_KEY  = env("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_API_KEY_HERE")
# Screen/mission/expansion calls: Sonnet 5 — near-Opus quality at Sonnet
# pricing ($3/$15 per MTok; intro $2/$10 through 2026-08-31, cheaper than the
# Sonnet 4.6 it replaces). NOTE for 5-family models: thinking is ON by
# default and max_tokens caps thinking+text together — core/claude.py
# disables thinking for these small structured-JSON calls.
CLAUDE_MODEL       = env("CLAUDE_MODEL", "claude-sonnet-5")
# Deep-verify pass over ranking finalists only (~15-30 calls/run, judgment-
# heavy): Opus 5 with adaptive thinking. $5/$25 per MTok, but bounded volume.
CLAUDE_VERIFY_MODEL = env("CLAUDE_VERIFY_MODEL", "claude-opus-5")

# CareerOneStop (DOL) Web API — free key exposes the National Labor Exchange
# (NLx) feed, where federal contractors must list openings (VEVRAA). Register
# at https://www.careeronestop.org/Developers/WebAPI/registration.aspx; DOL
# emails a UserId + token. Used by `python crawler.py --nlx "Meta,Google"`.
CAREERONESTOP_USER_ID = env("CAREERONESTOP_USER_ID")
CAREERONESTOP_TOKEN   = env("CAREERONESTOP_TOKEN")

# =========================================================================
#  PATHS
# =========================================================================

# Three roots:
#   SCRIPT_DIR — where the CODE lives (this file's directory; the exe's dir
#                when compiled). Bundled read-only assets live here.
#   APP_HOME   — the checkout / install root. From source: SCRIPT_DIR.
#                Compiled: probed (see below), because the dist folder may sit
#                INSIDE the project checkout, whose root holds the real data.
#   DATA_DIR   — where YOUR data lives (DBs, résumé, profile.toml,
#                job_reports/, caches, captures, backups). Resolved by
#                `_resolve_data_dir` below — by default a per-user directory
#                on your machine, OUTSIDE the checkout, so cloning the repo
#                gives you the stock experience and your own data survives
#                `git pull`, a re-clone, or deleting the checkout.

APP_NAME = "JobCrawler"

# Current DB filename, then the pre-rename one — probed when deciding whether
# a directory is an existing install.
_DB_NAMES = ("jobs.db", "local_tech.db")


def _platform_data_dir():
    """The conventional per-user application-data directory for this OS.

    Windows: %LOCALAPPDATA%\\JobCrawler
    macOS:   ~/Library/Application Support/JobCrawler
    Linux:   $XDG_DATA_HOME/job-crawler (default ~/.local/share/job-crawler)
    """
    if sys.platform == "win32":
        base = env("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = env("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "job-crawler"


def _looks_like_install(d):
    """True if `d` already holds this app's data — a store, or a profile."""
    return ((d / "profile.toml").exists()
            or any((d / n).exists() for n in _DB_NAMES))


def _resolve_data_dir(app_home):
    """Where this machine's data lives, in precedence order:

    1. JOBS_DATA_DIR — an explicit override, always wins.
    2. <app_home>/data — an EXISTING in-checkout data dir. Never orphan an
       install that predates the per-user default, and an easy opt-in for
       anyone who deliberately wants portable/self-contained data: make the
       folder and it is used.
    3. <app_home> itself, if a DB sits there — the legacy flat layout.
    4. The per-user OS data directory. The default for a fresh clone.
    """
    override = env("JOBS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if (app_home / "data").is_dir():
        return app_home / "data"
    if any((app_home / n).exists() for n in _DB_NAMES):
        return app_home
    return _platform_data_dir()


if "__compiled__" in globals():
    _exe_dir = Path(sys.argv[0]).resolve().parent
    SCRIPT_DIR = _exe_dir
    # APP_HOME: first place that looks like an install — the exe's own folder
    # (copied-to-another-machine layout), else the folder ABOVE the dist dir
    # (dist still inside the checkout), else the exe's folder.
    APP_HOME = next((d for d in (_exe_dir, _exe_dir.parent)
                     if _looks_like_install(d) or (d / "data").is_dir()),
                    _exe_dir)
else:
    SCRIPT_DIR = Path(__file__).parent
    APP_HOME = SCRIPT_DIR

DATA_DIR = _resolve_data_dir(APP_HOME)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# The store: companies (cached mission scores, scope tags) + jobs (dedup
# state, resume-fit scores, track membership). ONE file for every track —
# jobs.track is a comma-separated SET, so a posting that belongs to two
# tracks is one row visible to both (see store.track_set).
STORE_DB_PATH = DATA_DIR / "jobs.db"

# A track can still get its own file via [tracks.*].db — the default is
# that they all share jobs.db.

# RESUME_PATH is resolved after the profile loads (it can name the file) —
# see "RÉSUMÉ" below.

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


# Canonical location of YOUR profile; the Settings tab writes here
# (core/profile_edit.py). Precedence:
#   1. JOBS_PROFILE          — explicit override (full path to the file)
#   2. <APP_HOME>/profile.toml — an EXISTING in-checkout profile. Keeps older
#      installs working, and lets anyone deliberately keep the profile beside
#      the code; a compiled build inside the checkout finds the real profile
#      rather than a fresh template copy beside the exe.
#   3. <DATA_DIR>/profile.toml — the default home for a fresh clone, and the
#      file the Settings tab creates on first save.
# The bundled profile.example.toml is the read-only fallback when none of the
# above exists, so the app runs immediately after a clone.
def _resolve_profile_path():
    override = env("JOBS_PROFILE")
    if override:
        return Path(override).expanduser()
    if (APP_HOME / "profile.toml").exists():
        return APP_HOME / "profile.toml"
    return DATA_DIR / "profile.toml"


PROFILE_PATH = _resolve_profile_path()
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
_src  = _PROFILE.get("sources", {})

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

# =========================================================================
#  RÉSUMÉ
# =========================================================================
#
# Your résumé drives per-job fit scoring (core/resume.py extracts the text
# lazily). It is personal data, so it lives in DATA_DIR, not the checkout.
# Nothing here is required — with no résumé the crawler still runs and fit
# scoring simply turns itself off.
#
#   1. JOBS_RESUME               — explicit path override
#   2. [candidate] resume = "…"  — a filename (relative to DATA_DIR) or path
#   3. the first resume.* in DATA_DIR, else the only document in there
#
# Formats core/resume.py can read. PDF is deliberately absent — it would be
# read as garbled bytes rather than text, which is worse than no résumé.
RESUME_SUFFIXES = (".docx", ".txt", ".md")


def _resolve_resume_path():
    override = env("JOBS_RESUME") or (_cand.get("resume") or "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_absolute() else DATA_DIR / p
    for suffix in RESUME_SUFFIXES:                    # resume.docx, resume.txt…
        p = DATA_DIR / f"resume{suffix}"
        if p.exists():
            return p
    # Otherwise: the single readable document sitting in DATA_DIR, whatever
    # it's named ("Jane Doe 2026 Resume.docx"). Ambiguity is not guessed at —
    # two candidates means you name one in [candidate].resume.
    found = sorted(p for p in DATA_DIR.glob("*")
                   if p.suffix.lower() in RESUME_SUFFIXES and p.is_file())
    return found[0] if len(found) == 1 else DATA_DIR / "resume.docx"


RESUME_PATH = _resolve_resume_path()

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


def _seed_entry(e):
    """A [discovery].seed_companies entry -> {"name", "notes"}. Accepts a bare
    string or a { name, notes } table so the simple case stays a one-liner."""
    if isinstance(e, str) and e.strip():
        return {"name": e.strip(), "notes": ""}
    if isinstance(e, dict) and e.get("name"):
        return {"name": str(e["name"]).strip(),
                "notes": str(e.get("notes", "")).strip()}
    return None


DISCOVERY_SEED_COMPANIES = [s for s in (_seed_entry(e)
                                        for e in _dsc.get("seed_companies", []))
                            if s]
DISCOVERY_SEED_NAMES         = [s["name"] for s in DISCOVERY_SEED_COMPANIES]
# Discovery terms that pull the seeds in (empty = always). See discovery/seeds.py.
DISCOVERY_SEED_TRIGGERS      = list(_dsc.get("seed_triggers", []))
DISCOVERY_WORKDAY_MAJORS     = list(_dsc.get("workday_majors", []))
DISCOVERY_DIRECTORY_URLS     = list(_dsc.get("directory_urls", []))
DISCOVERY_NAME_SEARCH_QUERIES = list(_dsc.get("name_search_queries", []))
# LLM name-brainstorm source for discovery (names verified downstream, so
# hallucinations are harmless). None -> default 50; 0 disables.
DISCOVERY_BRAINSTORM_NAMES   = _dsc.get("brainstorm_names")
DISCOVERY_NAME_BLOCKLIST     = {re.sub(r"[^a-z0-9]", "", n.lower())
                                for n in _dsc.get("name_blocklist", [])}
# Cap on how many still-unresolved names discover_local's bulk pass will
# send through the websearch fallback (DDG-bound, so uncapped would risk
# minutes of rate-limit stalls across a full ~100+ name gather). None -> a
# small built-in default; 0 disables the bulk websearch pass entirely.
DISCOVERY_WEBSEARCH_CAP      = _dsc.get("websearch_cap")
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
    "local": {
        "label": "Local", "db": "jobs.db", "track": "local",
        "engine": "local",
        "rank_by": "fit", "min_mission": 0.2, "min_fit_default": 0.0,
        "willing_to_move_default": False, "remote_requires_watch": True,
        "default": True,
    },
    "remote": {
        "label": "Remote", "db": "jobs.db", "track": "remote",
        "engine": "sweep",
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

# The default technical-title gate: a posting whose TITLE doesn't match this
# never costs an API call. Deliberately broad and field-neutral — it is a
# cheap "is this a technical seat at all?" filter, not your search. Narrow it
# (or widen it for a non-engineering field) per track with `tech_title_regex`.
_DEFAULT_TECH_TITLE_REGEX = (
    r"\b("
    r"engineer|engineering|developer|develop|software|programmer|programming|"
    r"architect|devops|sre|reliability|infrastructure|platform|security|"
    r"data|database|analyst|analytics|statistician|quantitative|"
    r"scientist|science|sciences|scientific|research|researcher|"
    r"ml|machine learning|deep learning|ai|algorithm|algorithms|modeling|"
    r"simulation|computational|informatics|bioinformatics|biostatistics|"
    r"firmware|hardware|embedded|robotics|systems|automation|technologist|"
    r"quality|validation|verification|qa|test|r&d|python"
    r")\b"
)

_ENGINE_CRAWL_DEFAULTS = {
    # "local" — a location-scoped crawl of the companies in your store. Asks
    # each board for YOUR region, so it stays cheap on huge employers.
    "local": {
        "keyword_mode": "extend", "accept_remote": False,
        "sources": {"store": True, "priority_companies": False,
                    "aggregators": False, "websearch": False,
                    "location_scoped": True},
        "store_tag": None, "require_core_anchor": False, "geo_gate": True,
        "verify_top": 15, "cost_guard": 0, "email": False,
        "exclude_gate": True,
        "tech_title_regex": _DEFAULT_TECH_TITLE_REGEX,
    },
    # "sweep" — a location-AGNOSTIC sweep: whole boards, plus aggregator feeds
    # and web search, gated hard on your CORE keywords so the wider net
    # doesn't flood the digest. (Named "neural" before v2 — see ENGINE_ALIASES.)
    "sweep": {
        "keyword_mode": "replace", "accept_remote": True,
        "sources": {"store": True, "priority_companies": True,
                    "aggregators": True, "websearch": True,
                    "location_scoped": False},
        "store_tag": tags.SWEEP, "require_core_anchor": True, "geo_gate": False,
        "verify_top": 0, "cost_guard": 300, "email": False,
        "exclude_gate": False,
        "tech_title_regex": _DEFAULT_TECH_TITLE_REGEX,
    },
}

# Retired engine name -> current one, so a profile written against the old
# names keeps working (see core/tags.py for the same treatment of store tags).
ENGINE_ALIASES = {"neural": "sweep"}


def _build_ui_tracks(raw):
    tracks = {}
    for tid, t in (raw or _DEFAULT_TRACKS).items():
        if not isinstance(t, dict):
            continue
        engine = str(t.get("engine") or "local")
        engine = ENGINE_ALIASES.get(engine, engine)
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
            "store_tag": (tags.canonical(t["store_tag"]) if t.get("store_tag")
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

# Discourse forums with a jobs category, from profile [sources].discourse
# ({ label, url, category_id }). Empty by default — these are field-specific
# (a research forum, a language community), so there is no sensible universal
# set. See profile.example.toml.
DISCOURSE_BOARDS = [
    (str(b.get("label") or b.get("url", "")), str(b.get("url", "")),
     int(b.get("category_id", 0)))
    for b in _src.get("discourse", [])
    if b.get("url")
]

# Conglomerates (from profile.toml [policy]) whose OVERALL mission scores
# "other" but which run aligned subdivisions worth surfacing. Kept ACTIVE,
# crawled through the keyword filter (only aligned roles survive), and ranked
# at MULTI_DIVISION_MISSION_FLOOR rather than their own low company score.
MULTI_DIVISION_COMPANIES = {s.strip().lower()
                            for s in _pol.get("multi_division", [])}
MULTI_DIVISION_MISSION_FLOOR = float(_pol.get("multi_division_mission_floor", 0.6))

# Honor robots.txt: skip paths a host asks crawlers to leave alone, and
# obey its Crawl-delay. On by default — it costs one cached request per
# host, and the endpoints this crawler uses are permissive (Lever, for
# instance, publishes `Allow: /` with `Crawl-delay: 1`). See scrapers/robots.py.
RESPECT_ROBOTS = bool(_pol.get("respect_robots", True))

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
ROBOTS_CONNECT_TIMEOUT = float(_pol.get("robots_connect_timeout", 3.0))
ROBOTS_READ_TIMEOUT    = float(_pol.get("robots_read_timeout", 10.0))

# Headless-browser resolution order for the JS probes. "" is Playwright's own
# pinned build; the rest are `channel=` names for browsers already on the
# machine. Trying the system browsers means `pip install` alone is enough —
# no separate `playwright install` download — which is what makes the probes
# work on CI runners and on a machine whose playwright package was upgraded
# without re-fetching its browsers. Order matters: the pinned build first,
# because it is the only one whose version we control.
BROWSER_CHANNELS = [c or None for c in
                    _pol.get("browser_channels", ["", "chrome", "msedge"])]


def is_multi_division(name):
    """True if `name` is a known multi-division conglomerate (profile policy)."""
    return (name or "").strip().lower() in MULTI_DIVISION_COMPANIES


# Web searches for the sweep-style crawl (runner.build_sources, enabled by
# [tracks.*].sources.websearch), from profile [sources].websearch
# ({ label, query, max_results }). DuckDuckGo text search; each result URL is
# parsed for JSON-LD JobPosting. Empty by default — the queries encode YOUR
# field's vocabulary, so a generic default would only burn requests.
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
# These feeds are run-to-completion each crawl: one HTTP request returns
# every active listing, so they don't need per-company config.  Filtering
# happens in the fetcher via is_relevant().

# These are field-agnostic (they carry every kind of role and are filtered by
# your keywords), so unlike the forum/websearch lists above they ship ON with
# sensible defaults. Override any of them in profile [sources].

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

# Generic RSS/Atom feeds — profile [sources].rss ({ label, url, location }).
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

# =========================================================================
#  GATED-SITE CAPTURE CONFIG (Playwright)
# =========================================================================

# Keep roughly current — a stale UA is a red flag to fingerprinters.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
