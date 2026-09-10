"""Track construction: a profile's [tracks.<id>] tables -> runtime track dicts.

A track bundles a DB, a jobs.track value, ranking knobs, the UI filter
defaults that flip on switch, and the crawl methodology src/crawl/runner.py
runs it with. Every methodology key is overridable in the track's own
table; the track's ENGINE only picks which defaults bundle fills in the
keys it leaves out, so an older profile keeps working unchanged.

This module is the logic only. The tables it consumes — the built-in
tracks synthesized when a profile has no [tracks] section, the per-engine
defaults, the retired engine names — are string data, so they live
config-side (config.tracks) and are passed in. Code keys off the ENGINE
a track resolves to, never off the user-chosen track id.
"""

from src import tags


def mission_floor(v):
    """A track's `remote_mission_floor` as a float, or None when the track
    switches the admission off.

    >>> mission_floor(0.85)
    0.85
    >>> mission_floor(False) is None
    True
    >>> mission_floor(None) is None
    True

    Notes:
        TOML has no null, so `false` is how a profile disables the key
        rather than merely lowering it.
    """
    return None if v is None or v is False else float(v)


def resolve_engine(engine, aliases):
    """The current engine name for `engine`, mapping retired names through
    `aliases` and defaulting an unset value to "local".

    >>> resolve_engine("neural", {"neural": "sweep"})
    'sweep'
    >>> resolve_engine("sweep", {"neural": "sweep"})
    'sweep'
    >>> resolve_engine(None, {})
    'local'
    """
    engine = str(engine or "local")
    return aliases.get(engine, engine)


def build_tracks(raw, *, data_dir, default_tracks, engine_defaults, aliases):
    """Parse `raw` (the profile's [tracks] table, or None) into
    {track_id: track dict}.

    `data_dir` roots each track's `db_path`; `default_tracks` is used
    when `raw` is None or empty; `engine_defaults` maps engine ->
    methodology defaults; `aliases` maps retired engine names to current
    ones. Non-table entries under [tracks] are skipped.

    >>> from pathlib import Path
    >>> eng = {"local": {
    ...     "keyword_mode": "extend", "accept_remote": False,
    ...     "sources": {"store": True, "websearch": False},
    ...     "store_tag": None, "require_core_anchor": False, "geo_gate": True,
    ...     "remote_mission_floor": 0.85, "verify_top": 15, "cost_guard": 0,
    ...     "email": False, "digest_min_fit": 0.4, "notify": False,
    ...     "exclude_gate": True, "dormant_after": 4, "dormant_days": 7,
    ...     "tech_title_regex": r"\\bengineer\\b"}}
    >>> built = build_tracks({"my_track": {"engine": "old", "verify_top": 3,
    ...                                    "sources": {"websearch": True,
    ...                                                "unknown": True}},
    ...                       "junk": "not a table"},
    ...                      data_dir=Path("/d"), default_tracks={},
    ...                      engine_defaults=eng, aliases={"old": "local"})
    >>> sorted(built)
    ['my_track']
    >>> t = built["my_track"]
    >>> t["engine"], t["track"], t["db_path"].name, t["verify_top"]
    ('local', 'my-track', 'my_track.db', 3)
    >>> t["sources"]                      # unknown source keys are dropped
    {'store': True, 'websearch': True}
    >>> t["geo_gate"], t["remote_mission_floor"], t["label"]
    (True, 0.85, 'my_track')

    Notes:
        A [tracks] section that is present but empty falls back to
        `default_tracks` too — `{}` is not a way to configure zero tracks.
    """
    tracks = {}
    for tid, t in (raw or default_tracks).items():
        if not isinstance(t, dict):
            continue
        engine = resolve_engine(t.get("engine"), aliases)
        eng_defaults = engine_defaults.get(engine, engine_defaults["local"])
        src = dict(eng_defaults["sources"])
        src.update({k: bool(v) for k, v in (t.get("sources") or {}).items()
                    if k in src})
        tracks[tid] = {
            "id": tid,
            "label": str(t.get("label") or tid),
            "db_path": data_dir / str(t.get("db") or f"{tid}.db"),
            "track": str(t.get("track") or tid.replace("_", "-")),
            # Which crawl machinery this track runs on — "local" (the
            # location-scoped crawl) or "sweep" (the location-agnostic
            # whole-board crawl); both run through src/crawl/runner.py.
            "engine": engine,
            "rank_by": str(t.get("rank_by") or "fit"),
            "min_mission": (float(t["min_mission"])
                            if t.get("min_mission") is not None else None),
            "min_fit_default": float(t.get("min_fit_default", 0.0)),
            "willing_to_move_default": bool(t.get("willing_to_move_default", False)),
            "remote_requires_watch": bool(t.get("remote_requires_watch", False)),
            "default": bool(t.get("default", False)),
            # --- crawl methodology (src/crawl/runner.py) -----------
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
            "remote_mission_floor": mission_floor(
                t.get("remote_mission_floor",
                      eng_defaults["remote_mission_floor"])),
            "verify_top": int(t.get("verify_top", eng_defaults["verify_top"])),
            "cost_guard": int(t.get("cost_guard", eng_defaults["cost_guard"])),
            "email": bool(t.get("email", eng_defaults["email"])),
            "digest_min_fit": float(t.get("digest_min_fit",
                                          eng_defaults["digest_min_fit"])),
            "notify": bool(t.get("notify", eng_defaults["notify"])),
            "exclude_gate": bool(t.get("exclude_gate",
                                       eng_defaults["exclude_gate"])),
            "dormant_after": int(t.get("dormant_after",
                                       eng_defaults["dormant_after"])),
            "dormant_days": int(t.get("dormant_days",
                                      eng_defaults["dormant_days"])),
            "tech_title_regex": str(t.get("tech_title_regex")
                                    or eng_defaults["tech_title_regex"]),
        }
    return tracks


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
