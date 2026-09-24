"""Posting gates (src/match/gates.py): the exclude tables and the technical-title
regex, both resolved per track from configuration rather than code.

The profile-wide title gate (src/match/filters._excluded) is here too: it is
the other half of "does this TITLE disqualify the posting", and the two gates
are only separable once you know which one dropped a row."""

import pytest

import src.match.filters as filters
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


class TestSiliconTitleTokens:
    """Chip-design titles ([exclude.<id>].title_tokens, 2026-09-18).

    "Senior ASIC Design Engineer", "Mask Design Engineer" and "Memory
    Controller Verification Engineer" all pass the broad technical-title
    regex, and at a multi-division employer only the division gate can
    refuse them -- a gate that needs a body, so each one spends a detail GET
    out of the board's hydration budget before it drops (NVIDIA's waiting
    list, 2026-09-18). Dropping them at the FREE gate is worth it only if
    the tokens cannot fire on the neurotech-device vocabulary this search is
    for, which is what the second half of this class pins.

    Synthetic vocabulary, like TestClinicalServiceGate: the shape has to be
    exercised whether or not the loaded profile configures it. The last test
    is the exception -- it asks the LOADED profile the same question.
    """

    #: Titles that must survive a silicon vocabulary however it is written:
    #: neurotech device work, and the medical-device senses of the words
    #: silicon shares with it.
    DEVICE_TITLES = [
        "Firmware Engineer, Implant Embedded Systems",
        "Embedded Software Engineer", "FPGA Engineer",
        "Signal Processing Engineer", "Senior DSP Engineer",
        "Packaging Engineer II", "Software Verification Engineer",
        "Design Verification Engineer, Combination Products",
        "Basic Research Scientist",          # `\\basic\\b` must not fire here
    ]

    @pytest.fixture
    def silicon_track(self, exclude_vocab):
        return exclude_vocab("silicon_test", title_tokens=[
            "asic", "vlsi", "rtl", "dft", "physical design", "mask design",
            "analog ic", "layout engineer", "memory controller",
            "semiconductor"])

    @pytest.mark.parametrize("title", [
        "Senior ASIC Design Engineer", "Mask Design Engineer",
        "Memory Controller Verification Engineer",
        "Principal Physical Design Engineer", "RTL Design Engineer",
        "Analog IC Layout Engineer", "Senior VLSI CAD Software Engineer",
        "Senior DFT Engineer", "Semiconductor Process Development Engineer",
    ])
    def test_chip_titles_drop_before_any_fetch(self, silicon_track, title):
        assert gates.exclude_reason(title, track_id=silicon_track)

    @pytest.mark.parametrize("title", DEVICE_TITLES)
    def test_neurotech_device_titles_survive(self, silicon_track, title):
        assert gates.exclude_reason(title, track_id=silicon_track) is None

    def test_a_silicon_token_never_fires_from_the_body(self, silicon_track):
        # Title-only, like every other title_token: a posting that merely
        # mentions the neighbouring team must not drop.
        assert gates.exclude_reason(
            "Data Engineer", "you will sit beside the ASIC group",
            track_id=silicon_track) is None

    @pytest.mark.parametrize("title", DEVICE_TITLES)
    def test_the_loaded_profile_spares_the_device_vocabulary(self, local_track,
                                                             title):
        if not gates.exclude_reason("Senior ASIC Design Engineer",
                                    track_id=local_track["id"]):
            pytest.skip("profile configures no silicon title tokens")
        assert gates.exclude_reason(title, track_id=local_track["id"]) is None


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


class TestFieldOccupationTitles:
    """A field name wrapped in `\\b` matches the field and nothing built on
    it: "bioinformatics" cannot match "Bioinformatician", because the
    occupation noun is a SUFFIX on the stem and there is no word boundary
    between them. Duke Health's "Bioinformatician II" (Durham, 2026-09-11)
    was dropped at the free title gate on every configured track that way,
    and so was every Biostatistician, Statistician, Informaticist and
    Epidemiologist in the store -- 85 distinct stored titles, all on-lane.
    The fix is stems (`bioinformatic\\w*`), here and in any track's own
    `tech_title_regex`.
    """

    #: field name -> occupation nouns built on the same stem.
    FIELDS = [
        ("Bioinformatics", ["Bioinformatician II", "Bioinformaticist"]),
        ("Biostatistics", ["Biostatistician", "Senior Biostatistician II"]),
        ("Statistics", ["Statistician", "Statistical Programmer"]),
        ("Informatics", ["Clinical Informaticist", "Informaticist"]),
        ("Epidemiology", ["Epidemiologist", "Senior Epidemiologist, RWE"]),
    ]

    @pytest.mark.parametrize("title", [t for _, occ in FIELDS for t in occ])
    def test_the_audit_titles_pass_the_default_gate(self, title, local_track):
        assert gates.is_technical_role(title, local_track)

    @pytest.mark.parametrize("field,occupations", FIELDS)
    def test_every_track_admitting_a_field_admits_its_occupations(
            self, cfg, field, occupations):
        """Profile-agnostic: a track that does not admit the field at all is
        not in this business, but one that does must not stop at the noun."""
        for t in cfg.UI_TRACKS.values():
            if not gates.is_technical_role(field, t):
                continue
            for occ in occupations:
                assert gates.is_technical_role(occ, t), f"{t['id']}: {occ}"

    @pytest.mark.parametrize("title", [
        "Registered Nurse", "Patient Access Representative",
        "Sales Account Executive", "Warehouse Associate",
        "Staffing Coordinator", "Barista",
    ])
    def test_the_stems_do_not_admit_off_lane_titles(self, title, local_track):
        assert not gates.is_technical_role(title, local_track)


class TestTitleExemptPhrases:
    """[exclude].title_exempt_phrases, the profile-wide title gate's
    exception list (src/match/filters._excluded).

    A title phrase is a WORD and a job title is not: "manager" is there to
    drop the people-managing titles, and it was also dropping every
    Clinical/Scientific/Research/Laboratory Data Manager -- an
    individual-contributor data role, and one of the titles this search is
    for. The exemption blanks the exempt phrase out of the title and then
    runs the ordinary title walk over what is left, so the OTHER phrases
    still apply to the same title.

    The vocabulary is injected rather than read off the active profile:
    the suite has to pass on profile.example.toml (what CI checks out) and
    on a real profile.toml alike. The last test is the exception -- it
    asks whether the LOADED profile actually carries the exemption that
    this mechanism was added for.
    """

    @pytest.fixture
    def vocab(self, cfg):
        """Factory: `vocab(["manager"], ["data manager"])` sets the
        profile-wide exclude lists for one test.

        In place, never rebound: src.match.filters bound these list objects
        at import (the same contract `pristine_keywords` exists for), so a
        rebind here would leave the gate reading the originals. EXCLUDE_
        PHRASES is emptied too -- this class is about the TITLE half, and a
        profile whose body phrases happen to hit the fixture titles would
        otherwise decide the assertion.
        """
        saved = (list(cfg.EXCLUDE_PHRASES), list(cfg.EXCLUDE_TITLE_PHRASES),
                 list(cfg.EXCLUDE_TITLE_EXEMPT_PHRASES))

        def _set(title_phrases, exempt, phrases=()):
            cfg.EXCLUDE_PHRASES[:] = list(phrases)
            cfg.EXCLUDE_TITLE_PHRASES[:] = title_phrases
            cfg.EXCLUDE_TITLE_EXEMPT_PHRASES[:] = exempt
        yield _set
        cfg.EXCLUDE_PHRASES[:] = saved[0]
        cfg.EXCLUDE_TITLE_PHRASES[:] = saved[1]
        cfg.EXCLUDE_TITLE_EXEMPT_PHRASES[:] = saved[2]

    @staticmethod
    def _excluded(title):
        # _excluded's `text` is the already-scrubbed title+body; these
        # fixtures have no body, so the title IS the text.
        return filters._excluded(title, title.lower())

    @pytest.fixture
    def manager_vocab(self, vocab):
        vocab(["manager"], ["data manager"])

    def test_qualified_data_manager_titles_survive(self, manager_vocab):
        for qualifier in ("Clinical", "Scientific", "Research", "Laboratory"):
            assert not self._excluded(f"{qualifier} Data Manager"), qualifier

    def test_a_bare_data_manager_survives(self, manager_vocab):
        # The profile's own candidate asks for this title by name, so the
        # exemption is the phrase itself, not a qualifier-plus-phrase.
        assert not self._excluded("Data Manager")
        assert not self._excluded("Senior Data Manager II")

    def test_people_managing_titles_are_still_excluded(self, manager_vocab):
        assert self._excluded("Engineering Manager")
        assert self._excluded("Program Manager")
        assert self._excluded("Manager, Data Engineering")
        assert self._excluded("Manager")

    def test_case_does_not_matter_on_either_side(self, vocab):
        vocab(["MANAGER"], ["Data Manager"])
        assert not self._excluded("clinical data manager")
        assert self._excluded("engineering MANAGER")

    def test_the_other_title_phrases_still_judge_an_exempt_title(self, vocab):
        # Blanking the exempt phrase, rather than skipping the gate, is what
        # keeps this true: only "data manager" is spared, not the title.
        vocab(["manager", "intern"], ["data manager"])
        assert self._excluded("Data Manager Intern")
        assert not self._excluded("Clinical Data Manager")

    def test_body_phrases_are_untouched_by_a_title_exemption(self, vocab):
        vocab(["manager"], ["data manager"], phrases=["phd required"])
        assert filters._excluded("Clinical Data Manager",
                                 "clinical data manager. phd required.")

    def test_no_exemptions_configured_changes_nothing(self, vocab):
        vocab(["manager"], [])
        assert self._excluded("Clinical Data Manager")

    def test_the_loaded_profile_spares_a_clinical_data_manager(self, cfg,
                                                               vocab):
        """The bug this was written for, against whatever profile is
        loaded: a profile that excludes "manager" has to exempt the
        data-manager titles, or it drops a role it also lists as a target.

        The vocabulary is read from the profile TABLE and installed through
        the same fixture as the rest of the class, not read off the config
        globals: a widened run (config.widen_keywords, which
        restore_keywords does not put back) empties those globals, and this
        assertion would then pass by having nothing to exclude.
        """
        exc = cfg.PROFILE.exclude
        title_phrases = list(exc.title_phrases)
        if not any("manager" in p.lower() for p in title_phrases):
            pytest.skip("profile does not exclude 'manager' titles")
        vocab(title_phrases, list(exc.title_exempt_phrases))

        assert not self._excluded("Clinical Data Manager")
        assert not self._excluded("Scientific Data Manager")
        assert self._excluded("Engineering Manager")
