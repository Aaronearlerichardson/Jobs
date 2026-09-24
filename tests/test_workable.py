"""Workable (its `config.BOARDS` spec, run by src.ats.fetchers.board): the
single-request widget listing, the "City, Region, Country" location it
builds, the per-posting description call, and the board detection that
promoted Workable out of the detection-only lead bucket.

`tests/fixtures/workable_board.json` and `tests/fixtures/workable_job_detail.json`
are trimmed REAL responses, recorded live on 2026-09-21 from Eupry's board
(account slug `eupry-aps`) -- the four postings the widget endpoint served,
verbatim, and one posting's detail form with its three prose blobs cut to a
couple of sentences that keep their real markup. Every key name and the
nesting are exactly what the host sent.
"""

import json
import re
from pathlib import Path

import pytest

from conftest import fake_response
from src.ats.fetchers.board import board_for
from src.ats.signatures import detect

FIXTURES = Path(__file__).parent / "fixtures"

SLUG = "eupry-aps"
WIDGET_URL = f"https://apply.workable.com/api/v1/widget/accounts/{SLUG}"
JOB_API_ROOT = f"https://apply.workable.com/api/v1/accounts/{SLUG}/jobs/"
JOB_URL = f"https://apply.workable.com/{SLUG}/j/D68529D654/"
WORKABLE = board_for("workable")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _job(shortcode="D68529D654", title="Field Engineer",
         department="Customer Support", city="Raleigh",
         state="North Carolina", country="United States",
         published="2026-05-06", telecommuting=False, locations=None):
    """One listing entry, in the real payload's shape."""
    entry = {"title": title, "shortcode": shortcode, "code": "",
             "employment_type": "Full-time", "telecommuting": telecommuting,
             "department": department,
             "url": f"https://apply.workable.com/j/{shortcode}",
             "shortlink": f"https://apply.workable.com/j/{shortcode}",
             "published_on": published, "created_at": published,
             "country": country, "city": city, "state": state}
    if locations is None:
        locations = [{"country": country, "countryCode": "US", "city": city,
                      "region": state, "hidden": False}]
    entry["locations"] = locations
    return entry


def _board(jobs, name="Eupry"):
    return {"name": name, "description": None, "jobs": jobs}


@pytest.fixture
def workable_board(serve):
    """`serve` one account: the widget payload under the listing URL, and
    one detail payload for any per-posting GET."""
    return lambda board=None, detail=None, status=200, detail_status=200: serve(
        lambda url, **kw: (fake_response(detail, status=detail_status)
                           if url.startswith(JOB_API_ROOT)
                           else fake_response(board, status=status)))


def _location(serve, **entry):
    """The location the spec builds for one listing entry."""
    serve(fake_response(_board([_job(**entry)])))
    return WORKABLE.listing(SLUG)[0]["location"]


class TestLocation:
    """The geo gate and `is_nc` read a posting's location FIELD, so the
    payload's split city/region/country has to come out as an address a
    person (and the locality matcher) would recognize."""

    @pytest.mark.parametrize("entry,want", [
        (dict(city="Raleigh", state="North Carolina", country="United States"),
         "Raleigh, North Carolina, United States"),
        # Workable lets a tenant leave any level blank.
        (dict(city="Copenhagen", state="", country="Denmark"), "Copenhagen, Denmark"),
        (dict(city="", state="", country="United States"), "United States"),
        # No place named and not flagged remote: "Unknown", which
        # `locality.location_unknown` reads as "nothing to judge yet".
        (dict(city="", state="", country=""), "Unknown"),
        (dict(city="", state="", country="", telecommuting=True), "Remote"),
    ])
    def test_normalizes_every_shape_a_tenant_fills_in(self, serve, entry, want):
        assert _location(serve, locations=[], **entry) == want

    def test_a_multi_site_posting_names_every_office(self, serve, local_addr,
                                                     elsewhere):
        """`locations[]` is the full list while the flat fields show only
        the primary site, joined with ";", the separator `locality.is_nc`
        reads one office at a time."""
        from src.match.locality import is_nc
        far_city, far_rest = elsewhere.split(", ", 1)
        loc_city, loc_state = local_addr.split(", ", 1)
        loc = _location(serve, city=far_city, state="", country=far_rest,
                        locations=[{"city": far_city, "region": "", "country": far_rest,
                                    "hidden": False},
                                   {"city": loc_city, "region": loc_state,
                                    "country": "United States", "hidden": False}])
        assert loc == f"{far_city}, {far_rest}; {loc_city}, {loc_state}, United States"
        assert is_nc(loc)

    def test_a_hidden_office_is_not_a_location(self, serve):
        """`hidden` is the employer's own "do not show this" flag."""
        assert _location(serve, city="", state="", country="", telecommuting=True,
                         locations=[{"city": "Austin", "region": "Texas",
                                     "hidden": True}]) == "Remote"


class TestDetection:
    """src.ats.signatures: Workable was a detection-only lead until
    2026-09-22 (recognised, unfetchable). A lead is a miss reason; a
    fetchable board is a coordinate the crawl can act on."""

    @pytest.mark.parametrize("url", [
        "https://apply.workable.com/eupry-aps/",
        "https://apply.workable.com/eupry-aps/j/D68529D654/",
        "https://apply.workable.com/api/v1/widget/accounts/eupry-aps",
    ])
    def test_every_board_url_shape_resolves_to_the_account_slug(self, url):
        assert detect("", url) == ("fetchable", "workable", SLUG)

    def test_a_link_on_a_careers_page_is_found_in_the_body_too(self):
        page = '<a href="https://apply.workable.com/eupry-aps/">Open roles</a>'
        assert detect(page) == ("fetchable", "workable", SLUG)

    def test_the_slugless_short_link_names_no_board(self):
        assert detect("", "https://apply.workable.com/j/D68529D654") is None

    def test_workable_is_no_longer_a_lead(self):
        from src.ats.signatures import ATS_LEAD_PATTERNS, ATS_LINK_PATTERNS
        assert "workable" not in {a for a, _ in ATS_LEAD_PATTERNS}
        assert "workable" in {a for a, _ in ATS_LINK_PATTERNS}

    def test_the_probe_confirms_a_board_with_a_live_count(self, serve):
        """A fetchable platform is one a slug can be CONFIRMED on
        (signatures.py's own definition)."""
        from src.discovery.resolve import probes
        serve(fake_response(load("workable_board.json")))
        assert probes.PROBES["workable"](SLUG) == (True, 4)
        # An account with nothing published is not a board worth a row: an
        # account slug is not the company name ("eupry" is a different,
        # empty account), so a guessed slug confirms only with postings.
        serve(fake_response(_board([])))
        assert probes.PROBES["workable"]("eupry") == (False, 0)
        serve(fake_response(status=404))
        assert probes.PROBES["workable"]("no-such-account") == (False, 0)


class TestRegistry:
    """src/ats/registry.py: the sweep's thunk table and the seed tag."""

    def test_the_registry_knows_workable(self):
        from src import tags
        from src.ats.registry import ATS_REGISTRY, LIGHTWEIGHT, seed_tag_for
        assert "workable" in ATS_REGISTRY
        assert seed_tag_for("workable") == tags.LOCAL
        # The seed-tag rule is "SWEEP iff LIGHTWEIGHT"
        # (tests/test_fetcher_parsers.py pins it); Workable seeds LOCAL, so
        # it must stay out of LIGHTWEIGHT.
        assert "workable" not in LIGHTWEIGHT

    def test_the_registry_thunk_gates_and_names_the_company(self, workable_board):
        from src.ats.registry import ATS_REGISTRY
        workable_board(load("workable_board.json"),
                       detail=load("workable_job_detail.json"))
        mk, _tag, _pause = ATS_REGISTRY["workable"]
        jobs = mk("Eupry", SLUG)()
        assert all(j["company"] == "Eupry" for j in jobs)


class TestCompanyDispatch:
    """fetchers/company.py: the dispatch table drives the spec's listing,
    and hydrate_description fills a stored row from its URL alone (no ATS
    coordinate survives `adapt`)."""

    def test_fetch_company_adapts_this_modules_rows(self, workable_board):
        from src.ats.fetchers import company
        workable_board(load("workable_board.json"),
                       detail=load("workable_job_detail.json"))
        out = company.fetch_company({"ats": "workable", "slug": SLUG})
        assert [j["id"] for j in out][0] == "workable_eupry-aps_D68529D654"
        assert out[0]["ats"] == "workable"
        assert "company" not in out[0]

    def test_the_location_regex_filters_the_listing(self, workable_board):
        from src.ats.fetchers import company
        workable_board(_board([_job("AAAA111111", city="Raleigh",
                                    state="North Carolina"),
                               _job("BBBB222222", city="Austin",
                                    state="Texas")]))
        row = {"ats": "workable", "slug": SLUG}
        assert len(company.fetch_company(row)) == 2
        assert len(company.fetch_company(row, re.compile("North Carolina"))) == 1

    def test_hydrate_description_reads_the_posting_from_its_url(
            self, workable_board):
        from src.ats.fetchers import company
        workable_board(detail=load("workable_job_detail.json"))
        job = {"ats": "workable", "url": JOB_URL,
               "description": "", "location": "Raleigh, North Carolina"}
        out = company.hydrate_description(job)
        assert "wireless monitoring" in out["description"]
        assert out["location"] == "Raleigh, North Carolina"

    def test_the_title_sampler_reads_the_listing_only(self, workable_board):
        from src.ats.fetchers import company
        calls = workable_board(load("workable_board.json"),
                               detail=load("workable_job_detail.json"))
        titles = company.sample_titles({"ats": "workable", "slug": SLUG}, n=2)
        assert titles == ["Field Engineer", "Junior Customer Support"]
        assert [c.url for c in calls] == [WIDGET_URL]   # no detail spend


def test_a_stored_url_is_attributable_to_its_employer(db):
    """apply.workable.com is a shared host, so `store.company_by_host`
    insists on the board's own path prefix. The tenant-path URL the spec
    builds is what lets a captured page land on the right roster row."""
    import src.store as store
    store.upsert_company(db, {"name": "Eupry", "ats": "workable", "slug": SLUG,
                              "careers_url": f"https://apply.workable.com/{SLUG}/"})
    hit = store.company_by_host(db, JOB_URL)
    assert hit and hit["name"] == "Eupry"
    assert store.company_by_host(
        db, "https://apply.workable.com/someone-else/j/AAAA111111/") is None
