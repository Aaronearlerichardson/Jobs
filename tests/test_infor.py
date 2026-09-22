"""Infor CloudSuite HCM fetcher (src/ats/fetchers/infor.py): cursor paging
that follows the server's own next-page URL, the colon-delimited location
normalizer, and description hydration from a posting's detail form.

`tests/fixtures/infor_job_list.json` and `tests/fixtures/infor_job_detail.json`
are trimmed REAL responses, recorded live from an Infor "Candidate
Experience" board on 2026-09-21 (rows cut to three, the detail form's fields
cut to the ones anything reads, and its prose redacted; every key name and
the `{"value": ...}` nesting are exactly what the host sent).
"""

import json
import re
from pathlib import Path

import pytest

from conftest import fake_response
from src.ats.fetchers import company, infor
from src.ats.signatures import detect
from src.net import http

FIXTURES = Path(__file__).parent / "fixtures"

HOST = "css-acme-prd.inforcloudsuite.com"
ORG = "42"
SLUG = f"{HOST}|{ORG}"
LIST_URL = f"https://{HOST}/hcm/Jobs/list/JobPosting.SearchForJobsResults"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _entry(req, title="Data Engineer", location="US:NC:Morrisville",
           posting=1, category="Professional - Non-Clinical",
           posted="20260921"):
    """One listing row, in the real payload's shape (every value wrapped)."""
    return {"resourceId": f"JobPosting[JobPostingSet]({ORG},{req},{posting})",
            "fields": {
                "Description": {"value": title},
                "Category": {"value": "PROF - NON-CLINICAL"},
                "_op_Category_prd_Description_spc_translation_cp_":
                    {"value": category},
                "LocationOfJobDescriptionForSort": {"value": location},
                "JobRequisition": {"value": req},
                "JobId": {"value": req},
                "JobPosting": {"value": posting},
                "PostingDateRange_prd_Begin": {"value": posted}}}


def _page(entries, next_url=None):
    """One list response. The live board sends `pagingUrls` whether or not
    `hasNext` is set, so the fixture does too."""
    return {"dataViewSet": {
        "data": entries,
        "pagingInfo": {"hasNext": bool(next_url), "pageSize": len(entries)},
        "pagingUrls": {"nextPageUrl": next_url or f"{LIST_URL}?spent"}}}


@pytest.fixture
def infor_board(serve):
    """`serve` one board: list pages keyed by the URL asked for (the first
    page under the bare list endpoint, later pages under whatever
    `nextPageUrl` the previous page named), plus one detail payload for
    any `.JobPostingDisplay` GET."""
    return lambda pages, detail=None, status=200: serve(
        lambda url, **kw: fake_response(
            detail if ".JobPostingDisplay" in url else pages.get(url),
            status=status))


class TestListing:
    """`fetch_infor_all`: the whole-board pull both the company-vetted path
    (fetchers/company.py) and the gated sweep (`fetch_infor`) build on."""

    def test_parses_a_recorded_listing_page(self):
        """The trimmed real fixture, read straight through `_row` -- no
        network stub -- so the field names a live board sends (Description,
        JobRequisition, JobPosting, LocationOfJobDescriptionForSort,
        PostingDateRange_prd_Begin) are pinned against a REAL response."""
        rows = [infor._row("css-unchealthunc-prd.inforcloudsuite.com", "9999",
                           e["fields"])
                for e in load("infor_job_list.json")["dataViewSet"]["data"]]
        assert rows[0]["id"] == "infor_css-unchealthunc-prd_246253_2"
        assert rows[0]["title"] == "RN Clinical Nurse II Per Diem- 7BT Short Stay"
        assert rows[0]["location"] == "Holly Springs, NC, US"
        assert rows[0]["posted_at"] == "2026-09-21"
        assert rows[0]["url"].endswith(
            "JobPosting%5BJobPostingSet%5D%289999%2C246253%2C2%29"
            ".JobPostingDisplay?pagesize=1"
            "&csk.JobBoard=EXTERNAL&csk.HROrganization=9999")
        # The category rides in `head`, so the sweep's keyword gate screens
        # "Statistician/Data Scientist - ISD Analytics" WITH its department.
        assert rows[1]["head"].endswith("Professional - Non-Clinical")
        # A tenant that filled the location levels the other way round is
        # still read as a place, not dropped (see location_str).
        assert rows[2]["location"] == "Chapel Hill, NC"

    def test_walks_pages_by_following_the_servers_own_next_url(self, infor_board):
        """The cursor is opaque: only `pagingUrls.nextPageUrl` knows where
        page 2 starts (see module doc -- a hand-built `pageop=next` re-serves
        page 1), so the walk must follow it verbatim and stop on hasNext."""
        page2 = f"{LIST_URL}?pageop=next&fk=A&lk=B"
        calls = infor_board({
            LIST_URL: _page([_entry(1), _entry(2)], next_url=page2),
            page2: _page([_entry(3)]),
        })
        http.reset_fetch_failures()
        rows = infor.fetch_infor_all(SLUG)
        assert [r["id"] for r in rows] == [f"infor_css-acme-prd_{i}_1"
                                           for i in (1, 2, 3)]
        assert [c.url for c in calls] == [LIST_URL, page2]
        # Only the first request builds the query; the cursor URL carries
        # its own and must not be given a second set.
        assert calls[0].params["csk.HROrganization"] == ORG
        assert calls[0].params["pageop"] == "load"
        assert calls[1].params == {}
        assert not http.snapshot_info()["capped"]

    def test_a_page_of_repeats_ends_the_walk_and_reports_capped(self, infor_board):
        """A cursor that loops back must not spin to max_pages. Stopping
        with the server still claiming a next page is a CAPPED snapshot:
        the rows are real, a missing one proves nothing."""
        page2 = f"{LIST_URL}?pageop=next&fk=A"
        calls = infor_board({
            LIST_URL: _page([_entry(1), _entry(2)], next_url=page2),
            page2: _page([_entry(1), _entry(2)], next_url=page2),
        })
        http.reset_fetch_failures()
        rows = infor.fetch_infor_all(SLUG, max_pages=10)
        assert [r["id"] for r in rows] == ["infor_css-acme-prd_1_1",
                                           "infor_css-acme-prd_2_1"]
        assert len(calls) == 2
        assert http.snapshot_info()["capped"]

    def test_reading_every_page_up_to_max_pages_reports_capped(self, infor_board):
        page2 = f"{LIST_URL}?pageop=next&fk=A"
        infor_board({
            LIST_URL: _page([_entry(1)], next_url=page2),
            page2: _page([_entry(2)], next_url=page2 + "&more"),
        })
        http.reset_fetch_failures()
        rows = infor.fetch_infor_all(SLUG, max_pages=2)
        assert len(rows) == 2
        assert http.snapshot_info()["capped"]

    def test_a_next_url_off_the_board_host_is_refused(self, infor_board):
        """The cursor URL is served data, not a promise: one pointing
        somewhere else ends the walk (capped) instead of being fetched."""
        calls = infor_board({
            LIST_URL: _page([_entry(1)],
                            next_url="https://elsewhere.example.com"
                                     "/hcm/Jobs/list/JobPosting.SearchForJobsResults"),
        })
        http.reset_fetch_failures()
        rows = infor.fetch_infor_all(SLUG)
        assert len(rows) == 1 and len(calls) == 1
        assert http.snapshot_info()["capped"]

    def test_an_empty_board_is_an_empty_list_not_a_failure(self, infor_board):
        infor_board({LIST_URL: _page([])})
        http.reset_fetch_failures()
        assert infor.fetch_infor_all(SLUG) == []
        assert http.snapshot_info() == {"fetch_errors": 0, "incomplete": False,
                                        "capped": False, "capped_total": None,
                                        "last_error": None}

    def test_the_location_filter_applies_to_the_listed_location(self, infor_board):
        infor_board({LIST_URL: _page([_entry(1, location="US:NC:Morrisville"),
                                      _entry(2, location="US:TX:Austin")])})
        rows = infor.fetch_infor_all(SLUG, loc_re=re.compile(r", NC\b"))
        assert [r["id"] for r in rows] == ["infor_css-acme-prd_1_1"]

    def test_a_row_missing_its_key_or_title_is_skipped(self, infor_board):
        infor_board({LIST_URL: _page([
            _entry(1),
            {"resourceId": "x", "fields": {"Description": {"value": ""},
                                           "JobRequisition": {"value": 9},
                                           "JobPosting": {"value": 1}}},
            {"resourceId": "y", "fields": {"Description": {"value": "Analyst"}}},
        ])})
        assert [r["id"] for r in infor.fetch_infor_all(SLUG)] == \
            ["infor_css-acme-prd_1_1"]

    def test_a_slug_without_an_org_is_a_reported_failure(self, infor_board):
        """`<host>|<org>` is the coordinate; half of it names no board, and
        a board that cannot be addressed is a reported miss, not a crash."""
        calls = infor_board({})
        http.reset_fetch_failures()
        assert infor.fetch_infor_all(HOST) == []
        assert calls == []
        assert http.snapshot_info()["incomplete"]


class TestLocation:
    """`location_str`: the geo gate and `is_nc` read a posting's location
    FIELD, so "US:NC:Morrisville" has to come out as an address a person
    (and a "<city>, ST" regex) would recognize."""

    @pytest.mark.parametrize("raw,want", [
        ("US:NC:Morrisville", "Morrisville, NC, US"),
        ("US:NC:Holly Springs", "Holly Springs, NC, US"),
        ("Chapel Hill:NC", "Chapel Hill, NC"),       # levels filled backwards
        ("US:NC", "NC, US"),
        ("Smithfield", "Smithfield"),
        ("", ""),
        (None, ""),
    ])
    def test_normalizes_every_shape_a_tenant_fills_in(self, raw, want):
        assert infor.location_str(raw) == want

    def test_city_and_state_stay_adjacent(self):
        """The one property the locality matcher depends on: whatever else
        the string carries, the city is immediately followed by its state."""
        assert infor.location_str("US:NC:Morrisville").startswith("Morrisville, NC")


class TestDetail:
    """The per-posting detail form: the only place a description lives."""

    def test_reads_a_recorded_detail_form(self, infor_board):
        infor_board({}, detail=load("infor_job_detail.json"))
        url = infor.job_url(HOST, ORG, 207651, 1)
        desc, loc = infor.fetch_infor_description(url)
        assert "analytic data models" in desc
        assert "<p>" not in desc and "&amp;" not in desc      # HTML stripped
        assert loc == "Morrisville, NC, US"                   # off the subtitle

    def test_the_detail_call_addresses_the_record_by_its_encoded_triple(
            self, infor_board):
        calls = infor_board({}, detail=load("infor_job_detail.json"))
        infor.fetch_infor_description(infor.job_url(HOST, ORG, 207651, 1))
        assert calls[0].url.startswith(
            f"https://{HOST}/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D"
            f"%28{ORG}%2C207651%2C1%29.JobPostingDisplay?")
        assert "dependentForm=true" in calls[0].url

    def test_a_url_this_module_did_not_build_is_not_fetched(self, infor_board):
        calls = infor_board({})
        assert infor.fetch_infor_description("https://example.org/jobs/1") == ("", "")
        assert calls == []

    def test_a_dead_detail_endpoint_is_never_an_exception(self, infor_board):
        infor_board({}, detail=None, status=500)
        http.reset_fetch_failures()
        assert infor.fetch_infor_description(
            infor.job_url(HOST, ORG, 1, 1)) == ("", "")
        assert http.snapshot_info()["fetch_errors"] == 1


class TestSweepEntry:
    """`fetch_infor`: the gated sweep path (board.board_jobs) over the same
    rows the company-vetted path reads."""

    def test_the_gate_filters_before_any_detail_call(self, infor_board):
        calls = infor_board({LIST_URL: _page([_entry(1, title="Data Engineer"),
                                              _entry(2, title="Chef")])},
                            detail=load("infor_job_detail.json"))
        out = infor.fetch_infor(SLUG, "Acme",
                                gate=lambda t, d="": "data" in t.lower(),
                                detail_delay=0)
        assert [j["id"] for j in out] == ["infor_css-acme-prd_1_1"]
        assert out[0]["company"] == "Acme"
        assert "_infor" not in out[0]          # module key never reaches output
        # One listing GET, then a detail GET for the Chef row the title gate
        # rejected (board.py pays for a body before dropping) and one for the
        # kept row.
        assert sum(".JobPostingDisplay" in c.url for c in calls) == 2


class TestCompanyDispatch:
    """fetchers/company.py wiring: the dispatch table drives this module's
    listing, and hydrate_description fills a stored row from its URL alone
    (no ATS coordinate survives `_adapt`)."""

    def test_fetch_company_adapts_this_modules_rows(self, infor_board):
        infor_board({LIST_URL: _page([_entry(1)])})
        out = company.fetch_company({"ats": "infor", "slug": SLUG})
        assert [j["id"] for j in out] == ["infor_css-acme-prd_1_1"]
        assert out[0]["ats"] == "infor" and out[0]["_wd"] is None
        assert "company" not in out[0]

    def test_the_location_regex_filters_the_listing(self, infor_board):
        infor_board({LIST_URL: _page([_entry(1, location="US:NC:Morrisville"),
                                      _entry(2, location="US:TX:Austin")])})
        row = {"ats": "infor", "slug": SLUG}
        assert len(company.fetch_company(row)) == 2
        assert len(company.fetch_company(row, re.compile(r", NC\b"))) == 1

    def test_hydrate_description_reads_the_posting_from_its_url(self, infor_board):
        infor_board({}, detail=load("infor_job_detail.json"))
        job = {"ats": "infor", "url": infor.job_url(HOST, ORG, 207651, 1),
               "description": "", "location": ""}
        out = company.hydrate_description(job)
        assert "analytic data models" in out["description"]
        assert out["location"] == "Morrisville, NC, US"

    def test_hydration_keeps_a_location_the_listing_already_named(
            self, infor_board):
        infor_board({}, detail=load("infor_job_detail.json"))
        job = {"ats": "infor", "url": infor.job_url(HOST, ORG, 207651, 1),
               "description": "", "location": "Chapel Hill, NC"}
        assert company.hydrate_description(job)["location"] == "Chapel Hill, NC"

    def test_the_title_sampler_reads_one_page(self, infor_board):
        calls = infor_board({LIST_URL: _page([_entry(1, title="Data Engineer"),
                                              _entry(2, title="Analyst")])})
        titles = company.sample_titles({"ats": "infor", "slug": SLUG}, n=2)
        assert titles == ["Data Engineer", "Analyst"]
        assert len(calls) == 1                       # listing only, no details
        assert calls[0].params["pagesize"] == 2


def test_detect_reads_a_board_url_as_host_and_org():
    """A board URL names BOTH halves of the coordinate; one without the org
    id names no board (`src.ats.signatures.detect`)."""
    url = (f"https://{HOST}/hcm/Jobs/page/JobsHomePage"
           f"?csk.JobBoard=EXTERNAL&csk.HROrganization={ORG}")
    assert detect("", url) == ("fetchable", "infor", SLUG)
    assert detect("", f"https://{HOST}/hcm/Jobs/page/JobsHomePage") is None
    assert detect(f'<a href="{url.replace("&", "&amp;")}">Jobs</a>') == \
        ("fetchable", "infor", SLUG)
