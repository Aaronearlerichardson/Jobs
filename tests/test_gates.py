"""Posting gates (core/gates.py): the exclude tables and the technical-title
regex, both resolved per track from configuration rather than code."""

import pytest

import core.gates as gates


@pytest.fixture
def exclude_local(local_track):
    def _exc(title, description="", **kw):
        return gates.exclude_reason(title, description,
                                    track_id=local_track["id"], **kw)
    return _exc


@pytest.fixture
def tables(local_track):
    return gates._exclude_tables(local_track["id"])


class TestExcludeGate:
    def test_configured_role_phrase_excludes(self, tables, exclude_local):
        phrase = next(iter(tables["role_phrases"]), None)
        if not phrase:
            pytest.skip("track configures no role_phrases")
        assert exclude_local(f"Senior {phrase} II")

    def test_configured_title_token_excludes(self, tables, exclude_local):
        token = next(iter(tables["title_tokens"]), None)
        if not token:
            pytest.skip("track configures no title_tokens")
        assert exclude_local(f"{token.upper()} Manager")

    def test_title_token_does_not_fire_in_body(self, tables, exclude_local):
        token = next(iter(tables["title_tokens"]), None)
        if not token:
            pytest.skip("track configures no title_tokens")
        # Title-only by design: the same token in prose must not exclude.
        assert not exclude_local("Software Engineer", f"you'll work with the {token} team")

    def test_configured_defense_term_excludes(self, tables, exclude_local):
        term = next(iter(tables["defense_strong"]), None)
        if not term:
            pytest.skip("track configures no defense_strong terms")
        assert exclude_local("RF Engineer", f"work on {term} systems")

    def test_allow_defense_spares_only_defense(self, tables, exclude_local):
        term = next(iter(tables["defense_strong"]), None)
        if not term:
            pytest.skip("track configures no defense_strong terms")
        assert not exclude_local("RF Engineer", f"work on {term} systems",
                                 allow_defense=True)

    # Word-boundary bug fixes — these must hold whatever the vocabulary is.
    def test_scribe_does_not_match_describe(self, exclude_local):
        assert not exclude_local("Engineer", "you will describe systems")

    def test_defi_does_not_match_defibrillator(self, exclude_local):
        assert not exclude_local("Engineer", "implantable defibrillator")

    def test_unconfigured_track_is_a_noop(self):
        assert gates.exclude_reason("Combat Systems Radar Engineer",
                                    "missile defense",
                                    track_id="no_such_track_xyz") is None

    def test_tables_come_from_config_not_hardcoded_keys(self):
        empty = gates._exclude_tables("no_such_track_xyz")
        assert empty == {k: () for k in empty}


class TestTechnicalTitle:
    def test_engineer_is_technical(self, local_track):
        assert gates.is_technical_role("Quality Engineer", local_track)

    def test_data_manager_is_technical(self, local_track):
        assert gates.is_technical_role("Clinical Data Manager", local_track)

    def test_nurse_is_not_technical(self, local_track):
        assert not gates.is_technical_role("Registered Nurse", local_track)

    def test_controller_is_not_technical(self, sweep_track):
        # A sweep track pulls whole boards, so every back-office title at a
        # relevant employer arrives too: the TITLE has to carry the signal.
        assert not gates.is_technical_role("Corporate Controller", sweep_track)

    def test_tracks_can_differ(self, local_track):
        """Engines share a broad default, but a track can override it — the
        gate reads the track's own regex, never a module-level constant."""
        narrow = dict(local_track, tech_title_regex=r"\bbaker\b")
        assert gates.is_technical_role("Sourdough Baker", narrow)
        assert not gates.is_technical_role("Data Engineer", narrow)
        assert gates.is_technical_role("Data Engineer", local_track)

    def test_empty_title_is_never_technical(self, local_track):
        assert not gates.is_technical_role("", local_track)
        assert not gates.is_technical_role(None, local_track)
