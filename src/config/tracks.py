"""Tracks: the [tracks.<id>] tables, the per-engine defaults that fill in
what a table leaves out, and the built-in pair used when a profile has no
[tracks] section at all.

Each track bundles a DB, a jobs.track value, ranking knobs, and the UI
filter defaults that flip on switch (src/web/routes.py), plus the crawl
methodology src/crawl/harvest.py (run_track) runs it with. The schema, the per-engine
defaults and the built-in pair live in src/config/profile_schema.py
(Track, ENGINE_DEFAULTS, DEFAULT_TRACKS); this module turns validated
tracks into RuntimeTracks. Code keys off the ENGINE a track resolves
to, never off the user-chosen track id.

Crawl-methodology keys, every one overridable in the track's own table:
  keyword_mode        "extend" (track keywords ADD to the global tiers) or
                      "replace" (track keywords BECOME the tiers)
  accept_remote       config.ACCEPT_REMOTE while this track crawls
  sources             which source families the crawl assembles
    .store            active companies from the track's own DB
    .aggregators      Discourse/RemoteOK/Remotive/HN/RSS feeds
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
  email               email the digest after a crawl (CLI --send overrides)
  digest_min_fit      minimum resume fit a NEW row needs to make the
                      emailed digest (the written digest is unfiltered)
  notify              also raise a Windows desktop toast (needs the
                      optional `winotify` package; silent no-op without it)
  tech_title_regex    the technical-title gate (case-insensitive regex a
                      title must match before any API spend)
  exclude_gate        apply the [exclude.<id>] role/defense/nonclinical
                      tables to postings (False = skip entirely)
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from pydantic import ConfigDict, model_validator

from .paths import DATA_DIR
from .profile import PROFILE, PROFILE_PATH, _self, keyword_snapshot, restore_keywords
from .profile_schema import ENGINE_ALIASES, Track, TrackKeywords, parse
from .sources import FEEDS


class RuntimeTrack(Track):
    """A configured track as the runtime holds it (see _runtime): the
    validated Track, plus its id and DB path, with a blank `label` and
    `track` resolved from the id. Frozen, and `model_copy(update=)` does not
    validate.

    >>> t = RuntimeTrack(id="my_track", db_path=Path("my_track.db"))
    >>> t.label, t.track
    ('my_track', 'my-track')
    """
    model_config = ConfigDict(frozen=True)
    id: str
    db_path: Path

    @model_validator(mode="before")
    @classmethod
    def _named_by_id(cls, data: object) -> object:
        if not isinstance(data, dict) or not isinstance(tid := data.get("id"), str):
            return data
        return {**data, "label": data.get("label") or tid,
                "track": data.get("track") or tid.replace("_", "-")}


def _runtime(tid: str, t: Track) -> RuntimeTrack:
    """A validated Track as the RuntimeTrack every reader takes."""
    return RuntimeTrack(**t.model_dump(), id=tid, db_path=DATA_DIR / (t.db or f"{tid}.db"))


def _build_ui_tracks(raw: Mapping[str, object] | None) -> dict[str, RuntimeTrack]:
    """A [tracks] table (or None -> the built-in pair) as RuntimeTracks,
    validated like a profile's.

    A track's engine fills every methodology key it leaves out (a blank
    string counts as left out); retired engine names resolve; the id
    supplies the label, track and db:

    >>> t = _build_ui_tracks({"my_track": {
    ...     "engine": "neural", "verify_top": 3, "store_tag": "",
    ...     "sources": {"store": True}}})["my_track"]
    >>> t.engine, t.keyword_mode, t.store_tag, t.verify_top
    ('sweep', 'replace', 'sweep', 3)
    >>> t.label, t.track, t.db_path.name
    ('my_track', 'my-track', 'my_track.db')
    >>> sorted(k for k, on in t.sources if on)
    ['aggregators', 'store']

    Notes:
        A [tracks] section that is present but empty falls back to the
        built-in pair too: `{}` is not a way to configure zero tracks.
    """
    return {tid: _runtime(tid, t)
            for tid, t in parse({"tracks": raw or {}}).tracks.items()}


def default_track_id(tracks: Mapping[str, Track]) -> str | None:
    """The id of the track flagged `default`, else the first one, else
    None for an empty table.

    >>> default_track_id({"a": Track(), "b": Track(default=True)})
    'b'
    >>> default_track_id({"a": Track()})
    'a'
    >>> default_track_id({}) is None
    True
    """
    return next((tid for tid, t in tracks.items() if t.default),
                next(iter(tracks), None))


def feed_families(t: Track) -> set[str]:
    """The feed families `t` reads: those the profile turns on
    (FEEDS) whose `sources` switch `t` has on, a null switch meaning
    every track.

    >>> from .profile_schema import TrackSources
    >>> agg = Track(sources=TrackSources(aggregators=True))
    >>> sorted(feed_families(agg) & {"hn", "usajobs"}) == sorted(
    ...     f for f in ("hn", "usajobs") if FEEDS[f]["on"])
    True
    >>> "hn" in feed_families(Track(sources=TrackSources(aggregators=False)))
    False
    """
    return {f for f, fam in FEEDS.items() if fam["on"] and (
        (switch := fam["switch"]) is None or getattr(t.sources, switch))}


UI_TRACKS = {tid: _runtime(tid, t) for tid, t in PROFILE.tracks.items()}
DEFAULT_TRACK = default_track_id(UI_TRACKS)


def track_for_engine(engine: str) -> RuntimeTrack:
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
    cands = [t for t in UI_TRACKS.values() if t.engine == engine]
    if not cands:
        raise SystemExit(f"no [tracks.*] entry with engine={engine!r} "
                         f"in {PROFILE_PATH}")
    return next((t for t in cands if t.default), cands[0])


def apply_keyword_focus(cfg: ModuleType, t: RuntimeTrack) -> None:
    """Point the shared keyword filter at this track's focus. Mutates the
    live list objects in place so filters.is_relevant (which imported them
    at load time) sees the change without a re-import. "extend" adds the
    track's [keywords.<id>] terms to the global tiers (deduped); "replace"
    swaps them in wholesale (and rebuilds the flat INCLUDE view, matching
    the legacy replace semantics). Empty track lists never blank a tier."""
    kw = getattr(cfg, "KEYWORDS_BY_TRACK", {}).get(t.id) or TrackKeywords()
    core, dom, skill = list(kw.core), list(kw.domain), list(kw.skill)
    if t.keyword_mode == "replace":
        if core:
            cfg.CORE_KEYWORDS[:] = core
        if dom:
            cfg.DOMAIN_KEYWORDS[:] = dom
        if skill:
            cfg.SKILL_KEYWORDS[:] = skill
        cfg.INCLUDE_KEYWORDS[:] = (cfg.CORE_KEYWORDS + cfg.DOMAIN_KEYWORDS
                                   + cfg.SKILL_KEYWORDS)
    else:
        for dst, add in ((cfg.CORE_KEYWORDS, core),
                         (cfg.DOMAIN_KEYWORDS, dom),
                         (cfg.SKILL_KEYWORDS, skill)):
            have = {k.lower() for k in dst}
            dst.extend(k for k in add if k.lower() not in have)
    setattr(cfg, "ACCEPT_REMOTE", t.accept_remote)


@contextmanager
def keyword_focus(t: RuntimeTrack) -> Generator[None]:
    """apply_keyword_focus for the duration of a block, then put the shared
    lists back (keyword_snapshot / restore_keywords).

    Notes:
        Lives here, not beside keyword_snapshot in profile.py, because it
        takes a RuntimeTrack and this module imports profile.
    """
    saved = keyword_snapshot()
    apply_keyword_focus(_self(), t)
    try:
        yield
    finally:
        restore_keywords(saved)
