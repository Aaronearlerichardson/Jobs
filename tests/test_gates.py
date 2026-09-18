"""Posting gates (src/match/gates.py): the exclude tables and the technical-title
regex, both resolved per track from configuration rather than code."""

import pytest

import src.match.gates as gates


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

    # -- clinical_titles / clinical_markers, against WHATEVER the active
    # profile configures (skips like the rest of this class when it
    # configures none) -----------------------------------------------------
    def test_configured_clinical_title_excludes(self, tables, exclude_local):
        term = next(iter(tables["clinical_titles"]), None)
        if not term:
            pytest.skip("track configures no clinical_titles")
        assert exclude_local(f"Senior {term.title()} II")

    def test_configured_marker_spares_the_configured_title(self, tables,
                                                            exclude_local):
        term = next(iter(tables["clinical_titles"]), None)
        marker = next(iter(tables["clinical_markers"]), None)
        if not term or not marker:
            pytest.skip("track configures no clinical_titles/clinical_markers")
        assert not exclude_local(f"{marker.title()} {term.title()}")


class TestClinicalServiceGate:
    """clinical_titles / clinical_markers ([exclude.<id>], added
    2026-09-18): a title naming a hands-on clinical-service occupation
    ("CT Technologist", "Medical Lab Scientist", "Nurse Practitioner...")
    passes the free tech_title_regex gate on a word it shares with
    engineering roles by coincidence ("technologist", "scientist",
    "quality" are all in the engine default) and would otherwise cost a
    hydration fetch and a Claude fit call for nothing -- 88 of 113 scored
    rows in the 2026-09-18 Duke Health pass, every one 0.00-0.05.

    A synthetic track (monkeypatched into config.EXCLUDE_BY_TRACK), not
    the `tables`/`exclude_local` fixtures above -- these tests pin the
    vocabulary SHAPE itself and must exercise it whether or not the
    active profile (profile.toml vs. the profile.example.toml CI runs
    against) happens to configure clinical_titles."""

    @pytest.fixture
    def clinical_track(self, exclude_vocab):
        return exclude_vocab(
            "clinical_test",
            clinical_titles=["technologist", "technician", "nurse",
                             "medical lab"],
            clinical_markers=["research", "data", "engineer"])

    def _exc(self, clinical_track, title, description=""):
        return gates.exclude_reason(title, description,
                                    track_id=clinical_track)

    def test_clinical_title_without_a_marker_excludes(self, clinical_track):
        assert self._exc(clinical_track, "CT Technologist")
        assert self._exc(clinical_track,
                         "Medical Lab Scientist - Central Automated Lab")
        assert self._exc(clinical_track,
                         "Nurse Practitioner - Palliative Care")

    def test_research_marker_in_title_spares_the_drop(self, clinical_track):
        # PINS the requirement: a genuine research/data/engineering role
        # that merely contains a clinical-sounding occupation word must
        # keep scoring -- the exact titles the 2026-09-18 audit named as
        # must-not-drop.
        assert not self._exc(clinical_track, "Research Technician")
        assert not self._exc(clinical_track, "Research Laboratory Technician")

    def test_marker_in_description_also_spares_the_drop(self, clinical_track):
        assert not self._exc(clinical_track, "Clinical Lab Technician",
                             "you will support our research pipeline")

    def test_clinical_title_only_fires_in_the_title(self, clinical_track):
        # Body prose mentioning the occupation word must not exclude an
        # unrelated role -- title-only, like title_tokens.
        assert not self._exc(clinical_track, "Software Engineer",
                             "you'll work with the technologist team")


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
