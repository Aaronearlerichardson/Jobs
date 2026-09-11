"""Tracks: the [tracks.<id>] tables, the per-engine defaults that fill in
what a table leaves out, and the built-in pair used when a profile has no
[tracks] section at all.

Each track bundles a DB, a jobs.track value, ranking knobs, and the UI
filter defaults that flip on switch (src/web/routes.py), plus the crawl
methodology src/crawl/runner.py runs it with. The tables are data and live
here; the building logic is src/config/track_build.py.

Crawl-methodology keys, every one overridable in the track's own table:
  keyword_mode        "extend" (track keywords ADD to the global tiers) or
                      "replace" (track keywords BECOME the tiers)
  accept_remote       config.ACCEPT_REMOTE while this track crawls
  sources             which source families the crawl assembles
    .store            active companies from the track's own DB
    .priority_companies  [discovery] priority_companies, fetched first
    .aggregators      Discourse/RemoteOK/Remotive/HN/RSS feeds
    .websearch        DDG web-search queries
    .location_scoped  store boards fetched through the locality filter
                      (whole-board for watched/sweep-tagged companies);
                      False = lightweight ATS sweep, location-agnostic
  store_tag           only sweep store companies carrying this tag (None=all)
  require_core_anchor gate: posting must hit a CORE keyword
  geo_gate            gate: drop non-local non-remote postings (locality
                      from [locality]); False = stamp remote_eligible only
  remote_mission_floor
                      mission score at which a company that is NOT watched
                      still earns watch-grade remote treatment: its whole
                      board is fetched, its explicitly-remote postings pass
                      the geo gate, and they enter the location-scoped
                      ranking. Multi-division conglomerates never qualify
                      this way (a conglomerate's corporate mission score
                      says nothing about the division that is hiring).
                      `false` turns the admission off entirely.
  verify_top          deep-verify the top N after scoring (0 = skip)
  cost_guard          max postings scored per run without confirm (0 = off)
  email               email the digest after a crawl (CLI --send overrides)
  digest_min_fit      minimum resume fit a NEW row needs to make the
                      emailed digest (the written digest is unfiltered)
  notify              also raise a Windows desktop toast (needs the
                      optional `winotify` package; silent no-op without it)
  tech_title_regex    the technical-title gate (case-insensitive regex a
                      title must match before any API spend)
  exclude_gate        apply the [exclude.<id>] role/defense/nonclinical
                      tables to postings (False = skip entirely)
  dormant_after       consecutive empty DAYS before a company goes dormant
  dormant_days        how long a dormant company is skipped before its next
                      (weekly) retry -- see store.record_crawl_outcome
"""

from src import tags
from src.config import track_build as _tracks

from .paths import DATA_DIR
from .profile import PROFILE_PATH, profile_section

# The two built-in tracks, synthesized when [tracks] is absent so existing
# installs work unchanged.
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

# Crawl-methodology defaults per engine. The engine just picks which
# behavior bundle applies when a key is absent from the track's table.
_ENGINE_CRAWL_DEFAULTS = {
    # "local" — a location-scoped crawl of the companies in your store. Asks
    # each board for YOUR region, so it stays cheap on huge employers.
    "local": {
        "keyword_mode": "extend", "accept_remote": False,
        "sources": {"store": True, "priority_companies": False,
                    "aggregators": False, "websearch": False,
                    "location_scoped": True},
        "store_tag": None, "require_core_anchor": False, "geo_gate": True,
        "remote_mission_floor": 0.85,
        "verify_top": 15, "cost_guard": 0, "email": False,
        "digest_min_fit": 0.4, "notify": False,
        "exclude_gate": True, "dormant_after": 4, "dormant_days": 7,
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
        "remote_mission_floor": 0.85,
        "verify_top": 0, "cost_guard": 300, "email": False,
        "digest_min_fit": 0.4, "notify": False,
        "exclude_gate": False, "dormant_after": 4, "dormant_days": 7,
        "tech_title_regex": _DEFAULT_TECH_TITLE_REGEX,
    },
}

# Retired engine name -> current one, so a profile written against the old
# names keeps working (see src/tags.py for the same treatment of store tags).
ENGINE_ALIASES = {"neural": "sweep"}

# Kept under its old name: tests and src/crawl/runner.py reach it via config.
_mission_floor = _tracks.mission_floor


def _build_ui_tracks(raw):
    """The profile's [tracks] table (or None -> the built-in pair) as
    runtime track dicts. See src.config.track_build.build_tracks."""
    return _tracks.build_tracks(raw, data_dir=DATA_DIR,
                                default_tracks=_DEFAULT_TRACKS,
                                engine_defaults=_ENGINE_CRAWL_DEFAULTS,
                                aliases=ENGINE_ALIASES)


UI_TRACKS = _build_ui_tracks(profile_section("tracks") or None)
DEFAULT_TRACK = _tracks.default_track_id(UI_TRACKS)


def track_for_engine(engine):
    """The configured track to use when an engine-level entry point is
    invoked without naming a track: the default-flagged track with that
    engine, else the first. Legacy engine names resolve too.

    Lived in src/crawl/runner.py, which made it the only reason src/ops
    imported src/crawl -- a deferred import, inside `_default_track`,
    dodging the cycle that a module-level one would have made obvious. It
    reads nothing but UI_TRACKS and ENGINE_ALIASES, both of which are
    here; it was never a crawl function.
    """
    engine = ENGINE_ALIASES.get(engine, engine)
    cands = [t for t in UI_TRACKS.values() if t["engine"] == engine]
    if not cands:
        raise SystemExit(f"no [tracks.*] entry with engine={engine!r} "
                         f"in {PROFILE_PATH}")
    return next((t for t in cands if t["default"]), cands[0])
