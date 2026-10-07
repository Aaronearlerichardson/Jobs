"""The search profile: profile.toml loaded, and its per-section tables
turned into the names the crawler reads.

Your search criteria live in profile.toml (gitignored), NOT in code — so
the crawler stays generic and your terms are easy to edit, share, or reset.
Falls back to the checked-in profile.example.toml when profile.toml is
absent. src/config/profile_schema.py is the schema and holds every
default; profile.example.toml documents it.

The profile is validated once, here, at import: a key the schema does not
know, or a value of the wrong type, stops the import with a ProfileError
naming every bad key path. The module-level names below (CORE_KEYWORDS,
MISSION_TIERS, ...) are each a view of one validated key; several are
mutated at runtime, so they stay plain module attributes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from types import ModuleType

from .paths import APP_HOME, DATA_DIR, SCRIPT_DIR
from .profile_schema import TrackExclude, TrackKeywords, parse
from .secrets import SETTINGS


# Canonical location of YOUR profile; the Settings tab writes here
# (src/config/profile_edit.py). Precedence:
#   1. JOBS_PROFILE          — explicit override (full path to the file)
#   2. <APP_HOME>/profile.toml — an EXISTING in-checkout profile. Keeps older
#      installs working, and lets anyone deliberately keep the profile beside
#      the code; a compiled build inside the checkout finds the real profile
#      rather than a fresh template copy beside the exe.
#   3. <DATA_DIR>/profile.toml — the default home for a fresh clone, and the
#      file the Settings tab creates on first save.
# The bundled profile.example.toml is the read-only fallback when none of the
# above exists, so the app runs immediately after a clone.
def _resolve_profile_path() -> Path:
    if SETTINGS.jobs_profile:
        return SETTINGS.jobs_profile.expanduser()
    if (APP_HOME / "profile.toml").exists():
        return APP_HOME / "profile.toml"
    return DATA_DIR / "profile.toml"


PROFILE_PATH = _resolve_profile_path()
PROFILE_EXAMPLE_PATH = (APP_HOME / "profile.example.toml"
                        if (APP_HOME / "profile.example.toml").exists()
                        else SCRIPT_DIR / "profile.example.toml")


def _load_profile() -> tuple[dict[str, object], Path | None]:
    """(parsed TOML, path) of the first profile that exists (yours, else
    the example), or ({}, None). The two are NOT merged: a key your profile
    leaves out takes the schema default, not the example's value."""
    for p in (PROFILE_PATH, PROFILE_EXAMPLE_PATH):
        if p.exists():
            with open(p, "rb") as fh:
                return tomllib.load(fh), p
    return {}, None


_PROFILE, _SOURCE_PATH = _load_profile()
PROFILE_SOURCE = _SOURCE_PATH.name if _SOURCE_PATH else None
PROFILE = parse(_PROFILE, _SOURCE_PATH or "profile")

# =========================================================================
#  KEYWORDS / EXCLUDES / LOCATIONS
# =========================================================================

# Tiered relevance: a job is relevant if it hits any CORE term, or a DOMAIN
# term AND a SKILL term (see profile.example.toml).
CORE_KEYWORDS   = list(PROFILE.keywords.core)
DOMAIN_KEYWORDS = list(PROFILE.keywords.domain)
SKILL_KEYWORDS  = list(PROFILE.keywords.skill)
# Flat view of the three tiers (discover.py --from-keywords, tools/expand.py
# and the source checkers read it). src/crawl/runner.py rebuilds it IN PLACE
# when a track swaps its keyword focus, so hold the list, not a copy.
INCLUDE_KEYWORDS = CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS

#: What `widen_keywords` empties. EXCLUDE_* are not part of the keyword
#: FOCUS (a track swap leaves them alone), but they are part of "the
#: profile is not judging this posting", so widening clears them too.
_WIDENED_EMPTY = ("DOMAIN_KEYWORDS", "SKILL_KEYWORDS", "EXCLUDE_PHRASES",
                  "EXCLUDE_TITLE_PHRASES")

#: Every list the snapshot helpers below carry, in order: the lists a
#: track's keyword focus mutates, then whatever else a widening clears.
#: Derived from `_WIDENED_EMPTY` rather than written out again -- the
#: EXCLUDE_* lists were emptied by every widening and put back by none,
#: because the save/restore kept its own copy of the names.
_SNAPSHOT_LISTS = tuple(dict.fromkeys(
    ("CORE_KEYWORDS", "DOMAIN_KEYWORDS", "SKILL_KEYWORDS", "INCLUDE_KEYWORDS")
    + _WIDENED_EMPTY))


def keyword_snapshot(cfg: ModuleType | None = None) -> tuple[list[str] | bool, ...]:
    """The shared keyword and exclude lists and ACCEPT_REMOTE as they stand
    now.

    Two things rewrite them IN PLACE -- that is the contract
    src/match/filters.py depends on, having bound the list objects at
    import -- so anything that runs either has to put them back:
    `src.crawl.runner.apply_keyword_focus` (the four keyword lists and
    ACCEPT_REMOTE) and `widen_keywords` (those, plus the EXCLUDE_* lists).
    Four places did, each with its own copy of the same five-line save and
    five-line restore: the crawl's triage pass, the web UI's operation
    runner, and the test suite's keyword fixture.
    """
    cfg = _self() if cfg is None else cfg
    return (*(list(getattr(cfg, n)) for n in _SNAPSHOT_LISTS),
            bool(getattr(cfg, "ACCEPT_REMOTE", False)))


def restore_keywords(snapshot: tuple[list[str] | bool, ...],
                     cfg: ModuleType | None = None) -> None:
    """Put a `keyword_snapshot` back, in place."""
    cfg = _self() if cfg is None else cfg
    for name, saved in zip(_SNAPSHOT_LISTS, snapshot):
        getattr(cfg, name)[:] = saved
    setattr(cfg, "ACCEPT_REMOTE", snapshot[-1])


def widen_keywords(cfg: ModuleType | None = None) -> None:
    """Turn the relevance filter off, in place: everything is relevant.

    For measuring a SOURCE rather than the profile. Every fetcher applies
    `is_relevant` internally, so without this a healthy board that simply
    doesn't match your search terms reports identically to a dead one --
    which is the whole question the canaries in tools/ exist to answer.

    "" rather than [] for the two ANY-of tiers: `is_relevant` treats an
    empty CORE list as "nothing to match" and drops the posting, while ""
    is a substring of any text and so admits it.

    In place, and only in place: src/match/filters.py bound these list
    objects at import, so rebinding them would leave the gate reading the
    originals. Pair it with `keyword_snapshot` / `restore_keywords` if the
    process has anything to do afterwards -- three copies of this lived in
    two canaries and a test fixture, and the test fixture's copy was the
    one that forgot ACCEPT_REMOTE. The snapshot carries the EXCLUDE_* lists
    this clears too, so a widen and a restore leave the profile as it was.
    """
    cfg = _self() if cfg is None else cfg
    cfg.CORE_KEYWORDS[:] = [""]
    cfg.INCLUDE_KEYWORDS[:] = [""]
    for name in _WIDENED_EMPTY:
        getattr(cfg, name)[:] = []
    setattr(cfg, "ACCEPT_REMOTE", True)


def _self() -> ModuleType:
    """The config PACKAGE, which is what every caller mutates -- its
    attributes are these module's objects, re-exported."""
    import src.config as _cfg
    return _cfg


EXCLUDE_PHRASES       = list(PROFILE.exclude.phrases)
EXCLUDE_TITLE_PHRASES = list(PROFILE.exclude.title_phrases)
# Titles a title_phrase must NOT drop: blanked out of the title before the
# title_phrases walk (src/match/filters._excluded), so "manager" can keep
# dropping Program/Engineering Manager while "Clinical Data Manager" —
# an individual-contributor data role — survives.
EXCLUDE_TITLE_EXEMPT_PHRASES = list(PROFILE.exclude.title_exempt_phrases)
# Regex fragments (ORed together in src/match/filters.scrub_boilerplate) for
# benefits/EEO/infra-health idioms that contain domain-looking words without
# meaning them.
EXCLUDE_BOILERPLATE_PHRASES = list(PROFILE.exclude.boilerplate_phrases)

# Per-track keyword/exclude overrides — [keywords.<track>] / [exclude.<track>]
# tables. Tracks read their own model (e.g. KEYWORDS_BY_TRACK.get("local"))
# instead of hardcoding their vocabulary; see src/crawl/runner.py.
KEYWORDS_BY_TRACK: dict[str, TrackKeywords] = {**PROFILE.keywords.__pydantic_extra__}
EXCLUDE_BY_TRACK: dict[str, TrackExclude] = {**PROFILE.exclude.__pydantic_extra__}

# Mutated at runtime: src/crawl/runner.py sets it to the crawling track's
# `accept_remote` and src/dispatch/background.py restores it between operations. Read it
# through the package (`config.ACCEPT_REMOTE`), never from-import it.
ACCEPT_REMOTE    = PROFILE.locations.accept_remote
LOCATION_EXCLUDE = list(PROFILE.locations.exclude)
# [locations] onsite + remote, flattened — nothing reads the two halves apart.
LOCATION_INCLUDE = PROFILE.locations.onsite + PROFILE.locations.remote

# --- Remote-eligibility detection (src/match/locality.py) -------------------
REMOTE_LOC_TOKENS     = list(PROFILE.locations.remote_tokens)
REMOTE_BODY_PHRASES   = list(PROFILE.locations.remote_phrases)
REMOTE_HARD_NEGATIONS = list(PROFILE.locations.hard_negations)
REMOTE_US_MARKERS     = list(PROFILE.locations.us_markers)
REMOTE_NON_US_REGIONS = list(PROFILE.locations.non_us_regions)

# --- Candidate identity (injected into Claude prompts; src/claude/api.py,
#     src/claude/fit.py) --
CANDIDATE_SUMMARY   = PROFILE.candidate.summary.strip()
CANDIDATE_STRENGTHS = list(PROFILE.candidate.strengths)
CANDIDATE_FIT_CAPS  = list(PROFILE.candidate.fit_caps)
CANDIDATE_AVOID     = PROFILE.candidate.avoid.strip()

# =========================================================================
#  RÉSUMÉ
# =========================================================================
#
# Your résumé drives per-job fit scoring (src/claude/resume.py extracts the text
# lazily). It is personal data, so it lives in DATA_DIR, not the checkout.
# Nothing here is required — with no résumé the crawler still runs and fit
# scoring simply turns itself off.
#
#   1. JOBS_RESUME               — explicit path override
#   2. [candidate] resume = "…"  — a filename (relative to DATA_DIR) or path
#   3. the first resume.* in DATA_DIR, else the only document in there
#
# Formats src/claude/resume.py can read. PDF is deliberately absent — it would be
# read as garbled bytes rather than text, which is worse than no résumé.
RESUME_SUFFIXES = (".docx", ".txt", ".md")


def _resolve_resume_path() -> Path:
    override = SETTINGS.jobs_resume or PROFILE.candidate.resume.strip()
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

# --- Fit rubric (src/claude/fit.py). weights/gate_penalty are complete
#     dicts (the profile's entries over the schema defaults); domain_ladder is
#     a list of LadderRung(score, terms); stack_* / region_terms are joined to text,
#     None when unset so fit.py derives them from the rest of the profile. ---
FIT_WEIGHTS       = PROFILE.fit.weights.model_dump()
FIT_GATE_PENALTY  = PROFILE.fit.gate_penalty.model_dump()
FIT_DOMAIN_LADDER = list(PROFILE.fit.domain_ladder) or None
FIT_STACK_CORE    = ", ".join(PROFILE.fit.stack_core) or None
FIT_STACK_ANTI    = ", ".join(PROFILE.fit.stack_anti) or None
FIT_REGION        = ", ".join(PROFILE.fit.region_terms) or None
# How many of your own --mark decisions (applied/dismissed, each) are fed to
# the fit scorer as few-shot calibration; 0 disables.
FIT_DISPOSITION_EXAMPLES = PROFILE.fit.disposition_examples
# Deterministic "clearance" gate backstop (src/claude/fit.py _CLEARANCE_RE).
# Empty -> fit.py falls back to its own built-in defaults.
FIT_CLEARANCE_VERBS      = list(PROFILE.fit.clearance_verbs)
FIT_CLEARANCE_QUALIFIERS = list(PROFILE.fit.clearance_qualifiers)

# --- Mission taxonomy (employer-alignment ladder; src/claude/api.py) -------
# Each tier: MissionTier(name, desc, band=[lo, hi], active).
MISSION_TIERS = list(PROFILE.mission.tiers)
MISSION_BULLSEYE_REGEX = PROFILE.mission.bullseye_regex.strip()
MISSION_BULLSEYE_TIER  = PROFILE.mission.bullseye_tier.strip()

# --- Locality (what counts as "local"; src/match/locality.py) ----------------------
LOCALITY_NAME         = PROFILE.locality.name
LOCALITY_WORD_TOKENS  = list(PROFILE.locality.word_tokens)
LOCALITY_SUBSTRINGS   = list(PROFILE.locality.substrings)
LOCALITY_STATE_SUFFIX = list(PROFILE.locality.state_suffix)

# --- Discovery sourcing (discover.py --local; discovery/local_sourcing) -
DISCOVERY_SEED_COMPANIES = [{"name": s.name, "notes": s.notes.strip()}
                            for s in PROFILE.discovery.seed_companies]
DISCOVERY_SEED_NAMES         = [s["name"] for s in DISCOVERY_SEED_COMPANIES]
# Discovery terms that pull the seeds in (empty = always). See src/discovery/seeds.py.
DISCOVERY_SEED_TRIGGERS      = list(PROFILE.discovery.seed_triggers)
DISCOVERY_SCAN_MAJORS        = list(PROFILE.discovery.scan_majors)
DISCOVERY_DIRECTORY_PAGES    = list(PROFILE.discovery.directory_urls)
DISCOVERY_DIRECTORY_URLS     = [p.url for p in DISCOVERY_DIRECTORY_PAGES]
DISCOVERY_DOMAIN_LOOKUP      = PROFILE.discovery.domain_lookup
DISCOVERY_DOMAIN_LOOKUP_URLS = list(PROFILE.discovery.domain_lookup_urls)
DISCOVERY_DOMAIN_HOSTS       = list(PROFILE.discovery.domain_hosts)
DISCOVERY_LOCALE_PATHS       = list(PROFILE.discovery.locale_paths)
DISCOVERY_REGISTRIES         = list(PROFILE.discovery.registries)
DISCOVERY_REGISTRY_SPECIALTIES = list(PROFILE.discovery.registry_specialties)
DISCOVERY_NAME_SEARCH_QUERIES = list(PROFILE.discovery.name_search_queries)
# LLM name-brainstorm source for discovery (names verified downstream, so
# hallucinations are harmless); 0 disables.
DISCOVERY_BRAINSTORM_NAMES   = PROFILE.discovery.brainstorm_names
DISCOVERY_NAME_BLOCKLIST     = {re.sub(r"[^a-z0-9]", "", n.lower())
                                for n in PROFILE.discovery.name_blocklist}
# Cap on how many still-unresolved names discover_local's bulk pass will
# send through the websearch fallback (DDG-bound, so uncapped would risk
# minutes of rate-limit stalls across a full ~100+ name gather); 0 disables
# the bulk websearch pass entirely.
DISCOVERY_WEBSEARCH_CAP      = PROFILE.discovery.websearch_cap
# Job-aggregator hosts to skip, and generic words to ignore, when resolving a
# search result to a company's own ATS board (src/discovery/websearch_board.py).
DISCOVERY_AGGREGATOR_HOSTS    = tuple(PROFILE.discovery.aggregator_hosts)
DISCOVERY_GENERIC_NAME_WORDS  = set(PROFILE.discovery.generic_name_words)
# Named company targets a track fetches first, as (name, ats, slug).
DISCOVERY_PRIORITY_COMPANIES = [(c.name, c.ats, c.slug)
                                for c in PROFILE.discovery.priority_companies]
