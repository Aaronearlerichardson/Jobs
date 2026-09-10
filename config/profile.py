"""The search profile: profile.toml loaded, and its per-section tables
turned into the names the crawler reads.

Your search criteria live in profile.toml (gitignored), NOT in code — so
the crawler stays generic and your terms are easy to edit, share, or reset.
Falls back to the checked-in profile.example.toml when profile.toml is
absent. See profile.example.toml for the schema + the relevance model.

Two ways to read a section from here:

* the module-level names below (CORE_KEYWORDS, MISSION_TIERS, ...), each a
  parsed, typed view of one profile key — what every current reader uses;
* `profile_section(name)`, the raw table for a section, for a reader that
  wants a key this module does not already type.
"""

import re
import tomllib
from pathlib import Path

from .paths import APP_HOME, DATA_DIR, SCRIPT_DIR
from .secrets import env


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
    """(parsed TOML, filename) of the first profile that exists — yours,
    else the example — or ({}, None). The two are NOT merged: a key your
    profile leaves out takes the code default, not the example's value."""
    for p in (PROFILE_PATH, PROFILE_EXAMPLE_PATH):
        if p.exists():
            with open(p, "rb") as fh:
                return tomllib.load(fh), p.name
    return {}, None


_PROFILE, PROFILE_SOURCE = _load_profile()


def profile_section(name):
    """The loaded profile's [name] table, or {} when the section is absent
    or is not a table.

    The live dict, not a copy: cheap, and a reader that wants to inspect
    keys this module does not already expose gets exactly what TOML parsed.
    Sub-tables ([keywords.local]) sit inside their parent's dict.

    >>> isinstance(profile_section("keywords"), dict)
    True
    >>> profile_section("no-such-section")
    {}
    """
    v = _PROFILE.get(name)
    return v if isinstance(v, dict) else {}


_kw   = profile_section("keywords")
_exc  = profile_section("exclude")
_loc  = profile_section("locations")
_cand = profile_section("candidate")
_mis  = profile_section("mission")
_lcl  = profile_section("locality")
_dsc  = profile_section("discovery")
_fitp = profile_section("fit")

# =========================================================================
#  KEYWORDS / EXCLUDES / LOCATIONS
# =========================================================================

# Tiered relevance: a job is relevant if it hits any CORE term, or a DOMAIN
# term AND a SKILL term (see profile.example.toml).
CORE_KEYWORDS   = list(_kw.get("core", []))
DOMAIN_KEYWORDS = list(_kw.get("domain", []))
SKILL_KEYWORDS  = list(_kw.get("skill", []))
# Flat view of the three tiers (discover.py --from-keywords, tools/expand.py
# and the source checkers read it). scrapers/runner.py rebuilds it IN PLACE
# when a track swaps its keyword focus, so hold the list, not a copy.
INCLUDE_KEYWORDS = CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS

EXCLUDE_PHRASES       = list(_exc.get("phrases", []))
EXCLUDE_TITLE_PHRASES = list(_exc.get("title_phrases", []))
# Regex fragments (ORed together in core/filters.scrub_boilerplate) for
# benefits/EEO/infra-health idioms that contain domain-looking words without
# meaning them.
EXCLUDE_BOILERPLATE_PHRASES = list(_exc.get("boilerplate_phrases", []))

# Per-track keyword/exclude overrides — [keywords.<track>] / [exclude.<track>]
# tables. Tracks read their own sub-dict (e.g. KEYWORDS_BY_TRACK.get("local"))
# instead of hardcoding their vocabulary; see scrapers/runner.py.
KEYWORDS_BY_TRACK = {k: v for k, v in _kw.items() if isinstance(v, dict)}
EXCLUDE_BY_TRACK  = {k: v for k, v in _exc.items() if isinstance(v, dict)}

# Mutated at runtime: scrapers/runner.py sets it to the crawling track's
# `accept_remote` and webapp/ops.py restores it between operations. Read it
# through the package (`config.ACCEPT_REMOTE`), never from-import it.
ACCEPT_REMOTE    = bool(_loc.get("accept_remote", False))
LOCATION_EXCLUDE = list(_loc.get("exclude", []))
# [locations] onsite + remote, flattened — nothing reads the two halves apart.
LOCATION_INCLUDE = list(_loc.get("onsite", [])) + list(_loc.get("remote", []))

# --- Remote-eligibility detection (core/remote_filter.py) ---------------
REMOTE_LOC_TOKENS     = list(_loc.get("remote_tokens", []))
REMOTE_BODY_PHRASES   = list(_loc.get("remote_phrases", []))
REMOTE_HARD_NEGATIONS = list(_loc.get("hard_negations", []))
REMOTE_US_MARKERS     = list(_loc.get("us_markers", []))
REMOTE_NON_US_REGIONS = list(_loc.get("non_us_regions", []))

# --- Candidate identity (injected into Claude prompts; core/claude.py,
#     core/fit.py) --
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

# =========================================================================
#  FIT / MISSION / LOCALITY / DISCOVERY
# =========================================================================

# --- Fit rubric (core/fit.py) — optional [fit] block; defaults apply if
#     absent. weights/gate_penalty are dicts; domain_ladder is a list of
#     {score, terms=[...]}; stack_* / region_terms are lists joined to text. ---
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
    string or a { name, notes } table so the simple case stays a one-liner.

    >>> _seed_entry("  Acme Robotics ")
    {'name': 'Acme Robotics', 'notes': ''}
    >>> _seed_entry({"name": "Acme", "notes": "seen at a meetup"})
    {'name': 'Acme', 'notes': 'seen at a meetup'}
    >>> _seed_entry("   ") is None and _seed_entry({"notes": "x"}) is None
    True
    """
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
# search result to a company's own ATS board (discovery/websearch_board.py).
DISCOVERY_AGGREGATOR_HOSTS    = tuple(_dsc.get("aggregator_hosts", []))
DISCOVERY_GENERIC_NAME_WORDS  = set(_dsc.get("generic_name_words", []))
# Named company targets a track fetches first (list of {name, ats, slug}).
DISCOVERY_PRIORITY_COMPANIES = [
    (c.get("name"), c.get("ats"), c.get("slug"))
    for c in _dsc.get("priority_companies", [])
]
