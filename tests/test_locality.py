"""Locality + geo classification (src/match/locality.py) — the single source of
truth for "is this job where I live?". Fixtures come from the active
profile, so these pass for any configured region."""

import pytest

import src.match.locality as locality


def test_configured_place_is_local(local_addr):
    assert locality.is_nc(local_addr)


def test_unconfigured_place_is_not(elsewhere):
    assert not locality.is_nc(elsewhere)


def test_short_tokens_need_word_boundaries(cfg):
    # "nc" must not fire inside "clinic" — unless the profile really does
    # list a locality substring that appears in it.
    haystack = " ".join(cfg.LOCALITY_SUBSTRINGS).lower()
    assert not locality.is_nc("outpatient clinic") or "clinic" in haystack


class TestGeoMode:
    def test_local_address_is_onsite(self, local_addr):
        assert locality.geo_mode(local_addr) == "onsite"

    def test_remote_location_is_remote(self):
        assert locality.geo_mode("Remote - US") == "remote"

    def test_out_of_area_is_neither(self, elsewhere):
        assert locality.geo_mode(elsewhere) is None

    def test_distributed_training_is_not_remote(self):
        # "distributed"/"anywhere" only count in a workforce phrase.
        assert locality.geo_mode("", "we do distributed training at scale") is None

    def test_onsite_wins_over_remote_when_both(self, local_addr):
        # "Remote; Durham, NC" is LOCAL material, not a remote drop.
        assert locality.geo_mode(f"Remote; {local_addr}") == "onsite"


@pytest.fixture(scope="module")
def local_city(cfg):
    """A configured locality substring that names a city rather than a
    state, title-cased ('Durham', 'Oakland'). Skips if there is none."""
    states = set(locality._ALL_US_STATES)
    states |= {s.lower() for s in cfg.LOCALITY_STATE_SUFFIX}
    city = next((s for s in cfg.LOCALITY_SUBSTRINGS
                 if len(s) > 4 and s.lower() not in states), None)
    if not city:
        pytest.skip("profile configures no city-like locality substring")
    return city.title()


@pytest.fixture(scope="module")
def other_state():
    """(name, postal code) of a US state that is not the profile's own and
    contains no locality token, e.g. ('Alabama', 'AL')."""
    name = next(n for n in sorted(locality._OTHER_STATES)
                if not locality._NC_TOKEN_RE.search(n))
    return name.title(), locality._OTHER_STATES[name].upper()


@pytest.fixture(scope="module")
def same_name_elsewhere(local_city, other_state):
    """The configured city's name in another US state ('Durham, Alabama'):
    local by name only."""
    return f"{local_city}, {other_state[0]}"


@pytest.fixture(scope="module")
def own(cfg):
    """The profile's own two-letter state suffix, upper-cased ('NC')."""
    abbr = next((s for s in cfg.LOCALITY_STATE_SUFFIX if len(s) == 2), None)
    if not abbr:
        pytest.skip("profile configures no two-letter state suffix")
    return abbr.upper()


class TestSegments:
    """is_nc and NC_RE judge a location one ";"/"|" segment at a time."""

    def test_city_named_in_another_state_is_not_local(
            self, same_name_elsewhere, local_city, other_state):
        assert not locality.is_nc(same_name_elsewhere)
        assert not locality.is_nc(f"{local_city}, {other_state[1]}")
        assert not locality.is_nc(f"US - {other_state[1]} - {local_city}")

    def test_city_named_in_another_country_is_not_local(self, local_city):
        assert not locality.is_nc(f"UK - County {local_city} - Barnard Castle")
        assert not locality.is_nc(f"US, Blue Bell; Canada, {local_city}")

    def test_one_local_segment_keeps_the_row_local(self, local_city, own,
                                                    other_state):
        assert locality.is_nc(f"{local_city}, {own}; Springfield, {other_state[1]}")
        assert locality.is_nc(f"Springfield, {other_state[1]} | {local_city}, {own}")

    def test_naming_the_configured_state_wins_within_a_segment(
            self, local_city, own, other_state):
        assert locality.is_nc(f"{other_state[0]} County, {local_city}, {own}")

    def test_a_bare_city_stays_local(self, local_city):
        # Including the one false positive the rule cannot see: a
        # same-named city elsewhere, written with no state at all.
        assert locality.is_nc(local_city)
        assert locality.is_nc(f"Medical Center {local_city} - 252 Main St")

    def test_search_matches_inside_the_local_segment_only(
            self, local_city, own, same_name_elsewhere):
        m = locality.NC_RE.search(f"{same_name_elsewhere}; {local_city}, {own}")
        assert m is not None and m.start() == len(same_name_elsewhere) + 2
        assert locality.NC_RE.search(same_name_elsewhere) is None


class TestSnippet:
    """location_snippet keeps a place and its address tail, nothing more."""

    def test_stops_at_a_glued_on_posting_date(self, local_city, own):
        place = f"{local_city}, {own}, US, 27710"
        text = f"{place} Aug 31, 2026 {local_city}, {own}"
        assert locality.location_snippet(text) == place

    def test_stops_where_prose_starts(self, local_city):
        assert locality.location_snippet(
            f"{local_city} is seeking two tenure-tr") == local_city
        assert locality.location_snippet(
            f"{local_city} School of Nursing is searching") == f"{local_city} School"
