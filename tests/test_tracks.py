"""Tracks are configuration, not code: [tracks.*] parsing, engine defaults
and keyword focus."""

import pytest
from pydantic import ValidationError

from src.match.filters import core_anchor
from src.config.profile_schema import Track, parse


class TestTrackConfig:
    def test_profile_defines_tracks(self, cfg):
        assert len(cfg.UI_TRACKS) >= 1
        assert cfg.DEFAULT_TRACK in cfg.UI_TRACKS

    def test_a_runtime_track_is_frozen_through_its_sources(self, local_track):
        with pytest.raises(ValidationError):
            local_track.geo_gate = False
        with pytest.raises(ValidationError):
            local_track.sources.store = False

    def test_every_track_points_at_a_db(self, cfg):
        assert all(t.db_path.name.endswith(".db")
                   for t in cfg.UI_TRACKS.values())

    def test_fallback_synthesizes_when_section_absent(self, cfg):
        fallback = cfg._build_ui_tracks(None)
        assert len(fallback) == 2
        assert any(t.default for t in fallback.values())

    def test_methodology_keys_are_parsed(self, local_track):
        assert all(hasattr(local_track, k) for k in
                   ("keyword_mode", "sources", "store_tag", "require_core_anchor",
                    "geo_gate", "verify_top", "verify_floor",
                    "email", "exclude_gate", "tech_title_regex"))

    def test_verify_floor_defaults_and_is_overridable(self, cfg):
        assert cfg.UI_TRACKS[cfg.DEFAULT_TRACK].verify_floor == 0.25
        built = cfg._build_ui_tracks({"t": {"verify_floor": 0.4}})
        assert built["t"].verify_floor == 0.4

    def test_engine_defaults_differ(self, local_track, sweep_track):
        assert local_track.keyword_mode == "extend"
        assert sweep_track.keyword_mode == "replace"
        assert local_track.geo_gate and not sweep_track.geo_gate
        assert sweep_track.require_core_anchor
        assert not local_track.require_core_anchor

    def test_track_for_engine_resolves_both(self, local_track, sweep_track):
        assert local_track.engine == "local"
        assert sweep_track.engine == "sweep"

    def test_a_track_built_directly_gets_its_engines_defaults(self):
        t = Track(engine="sweep")
        assert (t.keyword_mode, t.geo_gate, t.verify_top) == ("replace", False, 0)
        assert t == parse({"tracks": {"x": {"engine": "sweep"}}}).tracks["x"]

    def test_a_key_the_track_sets_beats_its_engine_and_sources_merge(self):
        t = Track(engine="neural", verify_top=7, sources={"store": False})
        assert (t.engine, t.verify_top) == ("sweep", 7)
        assert (t.sources.store, t.sources.aggregators) == (False, True)

    def test_an_unknown_engine_is_reported_on_the_engine_key(self):
        with pytest.raises(ValidationError) as err:
            Track(engine="bogus")
        assert [e["loc"] for e in err.value.errors()] == [("engine",)]


class TestKeywordFocus:
    """apply_keyword_focus mutates config's lists IN PLACE — filters.py bound
    those objects at import time, so identity must survive."""

    def test_extend_preserves_base_tiers(self, cfg, local_track, pristine_keywords):
        base = list(cfg.CORE_KEYWORDS)
        cfg.apply_keyword_focus(cfg, local_track)
        assert cfg.CORE_KEYWORDS[:len(base)] == base
        assert cfg.ACCEPT_REMOTE is False

    def test_replace_swaps_tiers_and_enables_remote(self, cfg, sweep_track,
                                                    pristine_keywords):
        cfg.apply_keyword_focus(cfg, sweep_track)
        kw = cfg.KEYWORDS_BY_TRACK.get(sweep_track.id)
        track_core = list(kw.core) if kw else []
        assert cfg.ACCEPT_REMOTE is True
        if track_core:
            assert cfg.CORE_KEYWORDS == track_core

    def test_list_identity_survives(self, cfg, local_track, pristine_keywords):
        before = id(cfg.CORE_KEYWORDS)
        cfg.apply_keyword_focus(cfg, local_track)
        assert id(cfg.CORE_KEYWORDS) == before, \
            "rebinding breaks filters.py's import-time reference"

    def test_core_anchor_reads_the_live_list(self, cfg, local_track,
                                             pristine_keywords):
        cfg.apply_keyword_focus(cfg, local_track)
        anchor = cfg.CORE_KEYWORDS[0]
        assert core_anchor(f"Senior {anchor} Engineer") == anchor
        assert core_anchor("Bakery Assistant", "we sell bread") is None


class TestWidenAndRestore:
    """widen_keywords turns the relevance filter off in place; the snapshot
    helpers are the way back, so the snapshot has to carry everything a
    widening clears. It carried the keyword tiers but not the EXCLUDE_*
    lists, and every test file that ran after tests/test_fetcher_parsers.py
    saw an empty EXCLUDE_TITLE_PHRASES."""

    @pytest.fixture
    def seeded_excludes(self, cfg):
        """Non-empty EXCLUDE_* lists whatever the profile ships (an empty
        list round-trips trivially), put back by plain slice assignment:
        the code under test can't be trusted to clean up after its own
        failure."""
        saved = (list(cfg.EXCLUDE_PHRASES), list(cfg.EXCLUDE_TITLE_PHRASES))
        cfg.EXCLUDE_PHRASES[:] = ["seed phrase"]
        cfg.EXCLUDE_TITLE_PHRASES[:] = ["seed title"]
        yield
        cfg.EXCLUDE_PHRASES[:] = saved[0]
        cfg.EXCLUDE_TITLE_PHRASES[:] = saved[1]

    def test_round_trip_restores_the_exclude_lists(self, cfg, seeded_excludes,
                                                   pristine_keywords):
        phrases, titles = cfg.EXCLUDE_PHRASES, cfg.EXCLUDE_TITLE_PHRASES
        saved = cfg.keyword_snapshot(cfg)
        cfg.widen_keywords(cfg)
        assert phrases == [] and titles == [], "the widening no longer clears them"
        cfg.restore_keywords(saved, cfg)
        assert phrases == ["seed phrase"]
        assert titles == ["seed title"]
        assert cfg.EXCLUDE_PHRASES is phrases, \
            "rebinding breaks filters.py's import-time reference"
        assert cfg.EXCLUDE_TITLE_PHRASES is titles

    def test_round_trip_restores_the_tiers_and_remote(self, cfg,
                                                      pristine_keywords):
        cfg.ACCEPT_REMOTE = False           # widen sets it; start from off
        tiers = {n: list(getattr(cfg, n)) for n in
                 ("CORE_KEYWORDS", "DOMAIN_KEYWORDS", "SKILL_KEYWORDS",
                  "INCLUDE_KEYWORDS")}
        saved = cfg.keyword_snapshot(cfg)
        cfg.widen_keywords(cfg)
        assert cfg.ACCEPT_REMOTE is True
        assert cfg.CORE_KEYWORDS == [""]
        cfg.restore_keywords(saved, cfg)
        assert cfg.ACCEPT_REMOTE is False
        assert {n: list(getattr(cfg, n)) for n in tiers} == tiers
