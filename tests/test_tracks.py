"""Tracks are configuration, not code: [tracks.*] parsing, engine defaults,
keyword focus, and source assembly through the one crawl pipeline."""

from datetime import datetime, timedelta

import core.store as store
import scrapers.runner as runner


class TestTrackConfig:
    def test_profile_defines_tracks(self, cfg):
        assert len(cfg.UI_TRACKS) >= 1
        assert cfg.DEFAULT_TRACK in cfg.UI_TRACKS

    def test_every_track_names_a_real_engine(self, cfg):
        # Against the registry, not a literal list, so adding an engine
        # doesn't need this test edited.
        assert all(t["engine"] in cfg._ENGINE_CRAWL_DEFAULTS
                   for t in cfg.UI_TRACKS.values())

    def test_retired_engine_names_still_resolve(self, cfg):
        """A profile written against an older engine name keeps working."""
        for legacy, current in cfg.ENGINE_ALIASES.items():
            built = cfg._build_ui_tracks({"t": {"engine": legacy}})
            assert built["t"]["engine"] == current

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
                    "exclude_gate", "tech_title_regex",
                    "dormant_after", "dormant_days"))

    def test_dormancy_knobs_are_whole_numbers(self, cfg):
        for t in cfg.UI_TRACKS.values():
            assert isinstance(t["dormant_after"], int) and t["dormant_after"] >= 1
            assert isinstance(t["dormant_days"], int) and t["dormant_days"] >= 1

    def test_engine_defaults_differ(self, local_track, sweep_track):
        assert local_track["keyword_mode"] == "extend"
        assert sweep_track["keyword_mode"] == "replace"
        assert local_track["geo_gate"] and not sweep_track["geo_gate"]
        assert sweep_track["require_core_anchor"]
        assert not local_track["require_core_anchor"]

    def test_track_for_engine_resolves_both(self, local_track, sweep_track):
        assert local_track["engine"] == "local"
        assert sweep_track["engine"] == "sweep"


class TestKeywordFocus:
    """apply_keyword_focus mutates config's lists IN PLACE — filters.py bound
    those objects at import time, so identity must survive."""

    def test_extend_preserves_base_tiers(self, cfg, local_track, pristine_keywords):
        base = list(cfg.CORE_KEYWORDS)
        runner.apply_keyword_focus(cfg, local_track)
        assert cfg.CORE_KEYWORDS[:len(base)] == base
        assert cfg.ACCEPT_REMOTE is False

    def test_replace_swaps_tiers_and_enables_remote(self, cfg, sweep_track,
                                                    pristine_keywords):
        runner.apply_keyword_focus(cfg, sweep_track)
        track_core = list(cfg.KEYWORDS_BY_TRACK.get(sweep_track["id"], {})
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

    def test_sweep_sources_carry_no_company_row(self, cfg, sweep_track):
        specs = runner.build_sources(cfg, sweep_track)
        assert all(s["company"] is None for s in specs)

    def test_priority_companies_come_first_and_are_starred(self, cfg, sweep_track):
        prio = getattr(cfg, "DISCOVERY_PRIORITY_COMPANIES", [])
        if not prio:
            return                                  # none configured: nothing to order
        specs = runner.build_sources(cfg, sweep_track)
        assert [s for s in specs[:len(prio)] if s["platform"].endswith("*")]

    def test_dormant_companies_drop_out_of_the_source_list(self, cfg,
                                                           local_track,
                                                           tmp_path):
        """The point of dormancy: build_sources must read the crawlable rows,
        not every active one, or the 181 never-productive companies keep
        costing a fetch each run."""
        db_path = tmp_path / "sources.db"
        conn = store.connect(db_path)
        store.upsert_company(conn, {"name": "Awake", "ats": "greenhouse",
                                    "slug": "awake"})
        cid = store.upsert_company(conn, {"name": "Asleep", "ats": "greenhouse",
                                          "slug": "asleep"})
        conn.execute("UPDATE companies SET crawl_state='dormant', "
                     "next_crawl_at=? WHERE id=?",
                     ((datetime.now() + timedelta(days=7)).isoformat(), cid))
        conn.commit()
        conn.close()
        t = {**local_track, "db_path": db_path, "store_tag": None}
        names = {s["name"] for s in runner.build_sources(cfg, t)}
        assert "Awake" in names and "Asleep" not in names

        conn = store.connect(db_path)
        store.reactivate_company(conn, cid)
        conn.close()
        names = {s["name"] for s in runner.build_sources(cfg, t)}
        assert "Asleep" in names

    def test_websearch_toggle_removes_sources(self, cfg, sweep_track):
        with_ws = runner.build_sources(cfg, sweep_track)
        without = runner.build_sources(cfg, sweep_track, include_websearch=False)
        assert len(without) <= len(with_ws)
