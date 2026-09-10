"""One term matcher (core/filters.token_pattern / token_in) sits behind every
vocabulary gate; each gate keeps its own short/long threshold. These pin the
rule and, per caller, the behaviour the threshold was chosen for."""

import re

import pytest

import core.filters as filters
import core.gates as gates
import core.locality as locality
import core.remote_filter as remote_filter


class TestRule:
    def test_short_alpha_terms_match_on_word_boundaries(self):
        assert filters.token_in("rome", "rome, italy", 4)
        assert not filters.token_in("rome", "chrome browser", 4)

    def test_long_terms_are_substrings(self):
        assert filters.token_in("weapon", "weapons systems", 3)
        assert filters.token_in("cortical", "subcortical recordings", 5)

    def test_non_alpha_terms_never_get_boundaries(self):
        # `\b` needs a word character inside it, so a bounded "c++" or
        # "u.s." could never match at all.
        assert "\\b" not in filters.token_pattern("c++", 5)
        assert filters.token_in("c++", "senior c++ developer", 5)
        assert filters.token_in("u.s.", "remote, u.s. only", 5)

    def test_the_threshold_belongs_to_the_caller(self):
        assert filters.token_in("radar", "radars", 3)          # substring
        assert not filters.token_in("radar", "radars", 5)      # bounded

    def test_term_case_is_ignored_against_lowercase_text(self):
        assert filters.token_in("Boston", "bostonian", 4)
        assert filters.token_in("SDR", "an sdr role", 3)

    def test_thresholds_are_the_documented_ones(self):
        assert (filters.SHORT_KEYWORD, filters.SHORT_EXCLUDE,
                filters.SHORT_REMOTE, filters.SHORT_PLACE) == (5, 3, 3, 4)


class TestKeywords:
    """SHORT_KEYWORD = 5: acronyms are bounded, real words inflect."""

    def test_acronym_does_not_fire_inside_a_word(self):
        assert not filters._kw_in("a recognized leader", ["ecog"])
        assert not filters._kw_in("omega labs", ["meg"])

    def test_acronym_matches_as_a_word(self):
        assert filters._kw_in("an ecog array", ["ecog"])

    def test_longer_terms_keep_their_inflections(self):
        assert filters._kw_in("subcortical", ["cortical"])


class TestExclusion:
    """SHORT_EXCLUDE = 3: only two/three-letter tokens are bounded; the
    plural-prone defense nouns stay substrings."""

    @pytest.fixture
    def defense_track(self, local_track):
        tables = gates._exclude_tables(local_track["id"])
        if not (tables["defense_strong"] or tables["defense_weak"]):
            pytest.skip("track configures no defense terms")
        return local_track["id"]

    def test_rf_radar_context_is_bounded(self, defense_track):
        assert gates.exclude_reason(
            "Radar Engineer", "rf/microwave radar arrays",
            track_id=defense_track) == "defense: military radar"
        assert gates.exclude_reason(
            "Radar Engineer", "perf radar dashboards",
            track_id=defense_track) is None

    def test_plural_defense_terms_are_substrings(self):
        assert gates._tok_in("drone", "drones and uavs")
        assert not gates._tok_in("uav", "suave design")


class TestRemote:
    """SHORT_REMOTE = 3: wfh / us / uk are bounded, region names are not."""

    def test_wfh_is_bounded(self):
        if "wfh" not in remote_filter._LOC_REMOTE_TOKENS:
            pytest.skip("profile drops the wfh token")
        assert remote_filter.remote_signal("WFH", "") == "location:wfh"
        assert remote_filter.remote_signal("", "swfhx") is None

    def test_region_codes_are_bounded(self):
        if "uk" not in remote_filter._NON_US_REGIONS \
                or "us" not in remote_filter._US_MARKERS:
            pytest.skip("profile drops the us/uk codes")
        assert remote_filter.us_eligible("Remote (US)")
        assert not remote_filter.us_eligible("Remote - UK")
        # "campus" contains "us" but names no region: the default (eligible).
        assert remote_filter.us_eligible("Main Campus")

    def test_region_names_are_substrings(self):
        if "america" not in remote_filter._US_MARKERS:
            pytest.skip("profile drops the america marker")
        assert remote_filter.us_eligible("Remote - Americas")


class TestPlaces:
    """SHORT_PLACE = 4: state codes and four-letter towns are bounded in
    the snippet regex whichever [locality] list they came from."""

    def test_four_letter_town_is_bounded(self):
        alt = re.compile(filters.token_pattern("rome", filters.SHORT_PLACE),
                         re.I)
        assert alt.search("Rome, GA 30161")
        assert not alt.search("Chrome extension")

    def test_configured_short_terms_are_bounded_in_the_snippet_regex(self, cfg):
        short = [t for t in (cfg.LOCALITY_WORD_TOKENS + cfg.LOCALITY_SUBSTRINGS
                             + cfg.LOCALITY_STATE_SUFFIX)
                 if t and t.isalpha() and len(t) <= filters.SHORT_PLACE]
        if not short:
            pytest.skip("profile configures no short locality terms")
        for t in short:
            assert rf"\b{re.escape(t)}\b" in locality.LOCATION_SNIPPET_RE.pattern
