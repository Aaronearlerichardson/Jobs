"""Tracks are configuration, not code: [tracks.*] parsing, engine defaults,
keyword focus, and source assembly through the one crawl pipeline."""

import scrapers.runner as runner


class TestTrackConfig:
    def test_profile_defines_tracks(self, cfg):
        assert len(cfg.UI_TRACKS) >= 1
        assert cfg.DEFAULT_TRACK in cfg.UI_TRACKS

    def test_every_track_names_a_real_engine(self, cfg):
        assert all(t["engine"] in ("local", "neural")
                   for t in cfg.UI_TRACKS.values())

    def test_every_track_points_at_a_db(self, cfg):
        assert all(t["db_path"].name.endswith(".db")
                   for t in cfg.UI_TRACKS.values())

    def test_fallback_synthesizes_when_section_absent(self, cfg):
        fallback = cfg._build_ui_tracks(None)
        assert len(fallback) == 2
        assert any(t["default"] for t in fallback.values())

    def test_methodology_keys_are_parsed(self, local_track):
        assert all(k in local_track for k in
                   ("keyword_mode", "sources", "store_tag", "require_core_anchor",
                    "geo_gate", "verify_top", "cost_guard", "email",
                    "exclude_gate", "tech_title_regex"))

    def test_engine_defaults_differ(self, local_track, neural_track):
        assert local_track["keyword_mode"] == "extend"
        assert neural_track["keyword_mode"] == "replace"
        assert local_track["geo_gate"] and not neural_track["geo_gate"]
        assert neural_track["require_core_anchor"]
        assert not local_track["require_core_anchor"]

    def test_track_for_engine_resolves_both(self, local_track, neural_track):
        assert local_track["engine"] == "local"
        assert neural_track["engine"] == "neural"


class TestKeywordFocus:
    """apply_keyword_focus mutates config's lists IN PLACE — filters.py bound
    those objects at import time, so identity must survive."""

    def test_extend_preserves_base_tiers(self, cfg, local_track, pristine_keywords):
        base = list(cfg.CORE_KEYWORDS)
        runner.apply_keyword_focus(cfg, local_track)
        assert cfg.CORE_KEYWORDS[:len(base)] == base
        assert cfg.ACCEPT_REMOTE is False

    def test_replace_swaps_tiers_and_enables_remote(self, cfg, neural_track,
                                                    pristine_keywords):
        runner.apply_keyword_focus(cfg, neural_track)
        track_core = list(cfg.KEYWORDS_BY_TRACK.get(neural_track["id"], {})
                          .get("core", []))
        assert cfg.ACCEPT_REMOTE is True
        if track_core:
            assert cfg.CORE_KEYWORDS == track_core

    def test_list_identity_survives(self, cfg, local_track, pristine_keywords):
        before = id(cfg.CORE_KEYWORDS)
        runner.apply_keyword_focus(cfg, local_track)
        assert id(cfg.CORE_KEYWORDS) == before, \
            "rebinding breaks filters.py's import-time reference"

    def test_core_anchor_reads_the_live_list(self, cfg, local_track,
                                             pristine_keywords):
        runner.apply_keyword_focus(cfg, local_track)
        anchor = cfg.CORE_KEYWORDS[0]
        assert runner.core_anchor(f"Senior {anchor} Engineer") == anchor
        assert runner.core_anchor("Bakery Assistant", "we sell bread") is None


class TestSourceAssembly:
    def test_location_scoped_sources_carry_company_rows(self, cfg, local_track):
        # Empty store (CI) yields no sources; the SHAPE is what's asserted.
        specs = runner.build_sources(cfg, local_track)
        assert all(s["company"] is not None for s in specs)

    def test_sweep_sources_carry_no_company_row(self, cfg, neural_track):
        specs = runner.build_sources(cfg, neural_track)
        assert all(s["company"] is None for s in specs)

    def test_priority_companies_come_first_and_are_starred(self, cfg, neural_track):
        prio = getattr(cfg, "DISCOVERY_PRIORITY_COMPANIES", [])
        if not prio:
            return                                  # none configured: nothing to order
        specs = runner.build_sources(cfg, neural_track)
        assert [s for s in specs[:len(prio)] if s["platform"].endswith("*")]

    def test_websearch_toggle_removes_sources(self, cfg, neural_track):
        with_ws = runner.build_sources(cfg, neural_track)
        without = runner.build_sources(cfg, neural_track, include_websearch=False)
        assert len(without) <= len(with_ws)
