"""Tracks: the [tracks.<id>] tables, the per-engine defaults that fill in
what a table leaves out, and the built-in pair used when a profile has no
[tracks] section at all.

Each track bundles a DB, a jobs.track value, ranking knobs, and the UI
filter defaults that flip on switch (src/web/routes.py), plus the crawl
methodology src/crawl/runner.py runs it with. The schema, the per-engine
defaults and the built-in pair live in src/config/profile_schema.py
(Track, ENGINE_DEFAULTS, DEFAULT_TRACKS); this module turns validated
tracks into the runtime dicts. Code keys off the ENGINE a track resolves
to, never off the user-chosen track id.

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
  verify_floor        screen score floor (0..1) for the OTHER rows verify_top
                      deep-verifies: local/remote triage_status='fit' rows
                      the screen scored at or above this that the top-N slice
                      alone never reaches (see ops.verify_top)
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

from .paths import DATA_DIR
from .profile import PROFILE, PROFILE_PATH
from .profile_schema import ENGINE_ALIASES, parse


def _runtime(tid, t):
    """A validated Track as the runtime dict every reader indexes."""
    d = t.model_dump(exclude={"db"})
    d.update(id=tid, label=t.label or tid,
             track=t.track or tid.replace("_", "-"),
             db_path=DATA_DIR / (t.db or f"{tid}.db"))
    return d


def _build_ui_tracks(raw):
    """A [tracks] table (or None -> the built-in pair) as runtime track
    dicts, validated like a profile's.

    A track's engine fills every methodology key it leaves out (a blank
    string counts as left out); retired engine names resolve; the id
    supplies the label, track and db:

    >>> t = _build_ui_tracks({"my_track": {
    ...     "engine": "neural", "verify_top": 3, "store_tag": "",
    ...     "sources": {"websearch": False}}})["my_track"]
    >>> t["engine"], t["keyword_mode"], t["store_tag"], t["verify_top"]
    ('sweep', 'replace', 'sweep', 3)
    >>> t["label"], t["track"], t["db_path"].name
    ('my_track', 'my-track', 'my_track.db')
    >>> sorted(k for k, on in t["sources"].items() if on)
    ['aggregators', 'priority_companies', 'store']

    Notes:
        A [tracks] section that is present but empty falls back to the
        built-in pair too: `{}` is not a way to configure zero tracks.
    """
    return {tid: _runtime(tid, t)
            for tid, t in parse({"tracks": raw or {}}).tracks.items()}


def default_track_id(tracks):
    """The id of the track flagged `default`, else the first one, else
    None for an empty table.

    >>> default_track_id({"a": {"default": False}, "b": {"default": True}})
    'b'
    >>> default_track_id({"a": {"default": False}})
    'a'
    >>> default_track_id({}) is None
    True
    """
    return next((tid for tid, t in tracks.items() if t["default"]),
                next(iter(tracks), None))


UI_TRACKS = {tid: _runtime(tid, t) for tid, t in PROFILE.tracks.items()}
DEFAULT_TRACK = default_track_id(UI_TRACKS)


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
