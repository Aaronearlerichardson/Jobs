"""Locality + geo classification (core/locality.py) — the single source of
truth for "is this job where I live?". Fixtures come from the active
profile, so these pass for any configured region."""

import core.locality as locality


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
