"""Workable fetcher (src/ats/fetchers/workable.py): the single-request
widget listing, the "City, Region, Country" location normalizer, the
per-posting description call, and the board detection that promoted
Workable out of the detection-only lead bucket.

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
from src.ats.fetchers import workable
from src.ats.signatures import detect
from src.net import http

FIXTURES = Path(__file__).parent / "fixtures"

SLUG = "eupry-aps"
WIDGET_URL = f"https://apply.workable.com/api/v1/widget/accounts/{SLUG}"
JOB_API_ROOT = f"https://apply.workable.com/api/v1/accounts/{SLUG}/jobs/"


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


class TestListing:
    """The widget endpoint: one GET, the whole board, no descriptions."""

    def test_maps_the_fields_of_a_recorded_listing(self):
        """The trimmed real fixture read straight through `_row` -- no
        network stub -- so the field names a live board sends (title,
        shortcode, department, city/state/country, locations[],
        published_on) are pinned against a REAL response."""
        rows = [workable._row(SLUG, j) for j in load("workable_board.json")["jobs"]]
        assert len(rows) == 4
        assert rows[0]["id"] == "workable_eupry-aps_D68529D654"
        assert rows[0]["title"] == "Field Engineer"
        assert rows[0]["location"] == "Raleigh, North Carolina, United States"
        assert rows[0]["posted_at"] == "2026-05-06"
        # The tenant-path form, NOT the listing's slug-less short link:
        # a stored URL has to name the account it came from (module doc).
        assert rows[0]["url"] == "https://apply.workable.com/eupry-aps/j/D68529D654/"
        assert "workable.com/j/" not in rows[0]["url"]
        # The listing carries no body at all -- that is what the detail
        # call is for, and an "empty description" here is not a miss.
        assert rows[0]["description"] == ""
        # The department rides in `head`, so the sweep's keyword gate
        # screens the title WITH it.
        assert rows[0]["head"] == "Field Engineer Customer Support"
        # An on-site posting gets no remote hint.
        assert "remote_hint" not in rows[0]
        assert [r["title"] for r in rows] == [
            "Field Engineer", "Junior Customer Support",
            "US Office Support and Operations Intern",
            "Validation Team Manager (Raleigh, NC)"]

    def test_every_posting_on_the_recorded_board_is_local(self):
        """Why this board is on the roster at all: `match.locality.is_nc`
        has to read the normalized location as the profile's own area.
        This board is real and Raleigh-specific, so the claim only holds
        under a profile whose [locality] covers NC; skip elsewhere rather
        than assert something CI's example profile was never going to
        agree with."""
        from src.match.locality import is_nc
        if not is_nc("Raleigh, North Carolina, United States"):
            pytest.skip("profile's locality doesn't cover this board's real location")
        rows = [workable._row(SLUG, j) for j in load("workable_board.json")["jobs"]]
        assert all(is_nc(r["location"]) for r in rows)

    def test_the_whole_board_comes_back_in_one_request(self, workable_board):
        calls = workable_board(load("workable_board.json"))
        http.reset_fetch_failures()
        jobs = workable.fetch_workable(SLUG, "Eupry", max_details=0)
        assert len(jobs) == 4
        assert [c.url for c in calls] == [WIDGET_URL]
        assert calls[0].headers.get("Accept") == "application/json"
        assert jobs[0]["company"] == "Eupry"
        assert "_shortcode" not in jobs[0]      # module key never escapes
        assert not http.snapshot_info()["capped"]

    def test_an_empty_board_is_an_empty_list_not_a_failure(self, workable_board):
        """An account with nothing published answers 200 with jobs: [] --
        a real empty board, not a miss (verified live: the separate `eupry`
        account does exactly this)."""
        workable_board(_board([]))
        http.reset_fetch_failures()
        assert workable.fetch_workable(SLUG, "Eupry") == []
        assert http.snapshot_info() == {"fetch_errors": 0, "incomplete": False,
                                        "capped": False, "capped_total": None,
                                        "last_error": None}

    def test_a_row_missing_its_shortcode_or_title_is_skipped(self, workable_board):
        workable_board(_board([
            _job(),
            {"title": "Analyst", "shortcode": "", "locations": []},
            {"shortcode": "AAAAAAAAAA", "title": "   ", "locations": []},
        ]))
        jobs = workable.fetch_workable(SLUG, "Eupry", max_details=0)
        assert [j["id"] for j in jobs] == ["workable_eupry-aps_D68529D654"]

    def test_the_location_filter_applies_to_the_listed_location(
            self, workable_board):
        workable_board(_board([
            _job("AAAA111111", city="Raleigh", state="North Carolina"),
            _job("BBBB222222", city="Austin", state="Texas"),
        ]))
        jobs = workable.fetch_workable(SLUG, "Eupry", max_details=0,
                                       loc_re=re.compile(r"North Carolina"))
        assert [j["id"] for j in jobs] == ["workable_eupry-aps_AAAA111111"]


class TestLocation:
    """`location_str`: the geo gate and `is_nc` read a posting's location
    FIELD, so the payload's split city/region/country has to come out as an
    address a person (and the locality matcher) would recognize."""

    @pytest.mark.parametrize("job,want", [
        ({"city": "Raleigh", "state": "North Carolina",
          "country": "United States"}, "Raleigh, North Carolina, United States"),
        # Workable lets a tenant leave any level blank; every partial form
        # has to stay readable.
        ({"city": "Copenhagen", "country": "Denmark"}, "Copenhagen, Denmark"),
        ({"state": "North Carolina", "country": "United States"},
         "North Carolina, United States"),
        ({"country": "United States"}, "United States"),
        # No place named and not flagged remote: the board driver's
        # "Unknown", which `locality.location_unknown` reads as "nothing to
        # judge yet".
        ({}, "Unknown"),
        ({"telecommuting": True}, "Remote"),
    ])
    def test_normalizes_every_shape_a_tenant_fills_in(self, job, want):
        assert workable.location_str(job) == want

    def test_city_and_state_stay_adjacent(self):
        """The one property the locality matcher depends on: whatever else
        the string carries, the city is immediately followed by its state."""
        assert workable.location_str(
            {"city": "Raleigh", "state": "North Carolina"}
        ).startswith("Raleigh, North Carolina")

    def test_a_multi_site_posting_names_every_office(self, local_addr, elsewhere):
        """`locations[]` is the full list while the flat fields show only
        the primary site. Joined with ";", which is the separator
        `locality.is_nc` reads one office at a time -- a posting whose
        SECOND office is local must stay local. `elsewhere`/`local_addr`
        keep this independent of which profile is loaded."""
        from src.match.locality import is_nc
        far_city, far_rest = elsewhere.split(", ", 1)
        loc_city, loc_state = local_addr.split(", ", 1)
        loc = workable.location_str(_job(
            city=far_city, state="", country=far_rest,
            locations=[{"city": far_city, "region": "", "country": far_rest,
                        "hidden": False},
                       {"city": loc_city, "region": loc_state,
                        "country": "United States", "hidden": False}]))
        assert loc == f"{far_city}, {far_rest}; {loc_city}, {loc_state}, United States"
        assert is_nc(loc)

    def test_a_hidden_office_is_not_a_location(self):
        """`hidden` is the employer's own "do not show this on the board"
        flag; publishing it would put a place in the geo gate that the
        posting does not claim."""
        assert workable.location_str(
            {"telecommuting": True,
             "locations": [{"city": "Austin", "region": "Texas", "hidden": True}]}
        ) == "Remote"

    def test_a_remote_posting_carries_the_boards_own_flag(self):
        row = workable._row(SLUG, _job("CCCC333333", telecommuting=True,
                                       city="", state="", country="",
                                       locations=[]))
        assert row["remote_hint"] == "workable:telecommuting"
        assert row["location"] == "Remote"


class TestDetail:
    """The per-posting detail call: the only place a description lives."""

    def test_reads_a_recorded_detail_payload(self, workable_board):
        calls = workable_board(detail=load("workable_job_detail.json"))
        desc = workable.fetch_description(SLUG, "D68529D654")
        assert "wireless monitoring" in desc
        # description AND requirements, in that order -- Workable splits
        # the JD across the two and the qualifications are in the second.
        assert "Physical requirements" in desc
        assert desc.index("wireless monitoring") < desc.index("Physical requirements")
        # benefits is shared boilerplate and stays out of the JD budget.
        assert "vacation days" not in desc
        # HTML stripped, entities unescaped.
        assert "<p>" not in desc and "&amp;" not in desc and "&" in desc
        assert calls[0].url == f"{JOB_API_ROOT}D68529D654"

    def test_a_dead_detail_endpoint_is_an_empty_body_never_an_exception(
            self, workable_board):
        """A pulled posting 404s here. A missing body is not a crash and
        not a reported board failure -- the row simply keeps what the
        listing gave it (fetchers/board.py)."""
        workable_board(load("workable_board.json"), detail=None,
                       detail_status=404)
        http.reset_fetch_failures()
        assert workable.fetch_description(SLUG, "ZZZZZZZZZZ") == ""
        jobs = workable.fetch_workable(SLUG, "Eupry", detail_delay=0)
        assert len(jobs) == 4 and all(j["description"] == "" for j in jobs)

    def test_the_gate_filters_before_paying_for_bodies(self, workable_board):
        """fetchers/board.py's order: a row whose TITLE passes costs a
        detail call, and so does one the title gate rejected (it is judged
        on its body before being dropped) -- but an out-of-area row costs
        nothing at all."""
        calls = workable_board(
            _board([_job("AAAA111111", title="Data Engineer"),
                    _job("BBBB222222", title="Chef"),
                    _job("CCCC333333", title="Data Engineer", city="Austin",
                         state="Texas")]),
            detail=load("workable_job_detail.json"))
        jobs = workable.fetch_workable(
            SLUG, "Eupry", gate=lambda t, d="": "data" in t.lower(),
            loc_re=re.compile(r"North Carolina"), detail_delay=0)
        assert [j["id"] for j in jobs] == ["workable_eupry-aps_AAAA111111"]
        assert "wireless monitoring" in jobs[0]["description"]
        details = [c.url for c in calls if c.url.startswith(JOB_API_ROOT)]
        assert details == [f"{JOB_API_ROOT}AAAA111111",
                           f"{JOB_API_ROOT}BBBB222222"]

    def test_max_details_caps_the_per_posting_spend(self, workable_board):
        calls = workable_board(load("workable_board.json"),
                               detail=load("workable_job_detail.json"))
        jobs = workable.fetch_workable(SLUG, "Eupry", max_details=2,
                                       detail_delay=0)
        assert len(jobs) == 4
        assert sum(c.url.startswith(JOB_API_ROOT) for c in calls) == 2
        assert [bool(j["description"]) for j in jobs] == [True, True, False, False]


class TestUrls:
    """A stored row's URL is the only coordinate that survives
    `company._adapt`, so both detail coordinates have to be readable back
    out of it."""

    def test_a_job_url_round_trips_to_its_coordinates(self):
        url = workable.job_url(SLUG, "D68529D654")
        assert workable.job_ref_from_url(url) == (SLUG, "D68529D654")

    @pytest.mark.parametrize("url", [
        # The short link names no account: nothing can be fetched from it.
        "https://apply.workable.com/j/D68529D654",
        "https://apply.workable.com/eupry-aps/",
        "https://example.org/jobs/1",
        "",
        None,
    ])
    def test_anything_this_module_did_not_build_is_not_a_reference(self, url):
        assert workable.job_ref_from_url(url) is None

    def test_a_stored_url_is_attributable_to_its_employer(self, db):
        """apply.workable.com is a shared host, so `store.company_by_host`
        insists on the board's own path prefix. The tenant-path URL shape
        is what lets a captured page land on the right roster row."""
        import src.store as store
        store.upsert_company(db, {"name": "Eupry", "ats": "workable",
                                  "slug": SLUG,
                                  "careers_url": workable.board_url(SLUG)})
        hit = store.company_by_host(db, workable.job_url(SLUG, "D68529D654"))
        assert hit and hit["name"] == "Eupry"
        assert store.company_by_host(
            db, "https://apply.workable.com/someone-else/j/AAAA111111/") is None


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

    def test_the_probe_confirms_a_board_with_a_live_count(self, monkeypatch):
        """A fetchable platform is one a slug can be CONFIRMED on
        (signatures.py's own definition). The probe reads the fetcher's own
        parser, so the two cannot drift."""
        from src.discovery.resolve import probes
        assert probes.PROBES["workable"] is probes.probe_workable
        monkeypatch.setattr(workable, "parse_board",
                            lambda h, **kw: load("workable_board.json")["jobs"])
        assert probes.probe_workable(SLUG) == (True, 4)
        # An account with nothing published is not a board worth a row.
        monkeypatch.setattr(workable, "parse_board", lambda h, **kw: [])
        assert probes.probe_workable("eupry") == (False, 0)
        # A dead slug raises inside the parser and is reported, not raised.
        def _boom(handle, **kw):
            raise RuntimeError("404")
        monkeypatch.setattr(workable, "parse_board", _boom)
        assert probes.probe_workable("no-such-account") == (False, 0)


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


# The company-vetted dispatch table lives in fetchers/company.py, which is
# being refactored in parallel (the probe/closure extraction of 2026-09-22),
# so the four lines that wire Workable into it are deferred to the owner --
# see this change's report. These tests are the acceptance check for that
# wiring and light up the moment it lands; until then they skip rather than
# fail against a file this change did not touch.
_WIRED = "workable" in __import__(
    "src.ats.fetchers.company", fromlist=["FETCHERS"]).FETCHERS

needs_wiring = pytest.mark.skipif(
    not _WIRED,
    reason="fetchers/company.py not yet wired for workable (FETCHERS / "
           "_TITLE_SAMPLERS / hydrate_description) -- deferred edit")


@needs_wiring
class TestCompanyDispatch:
    """fetchers/company.py wiring: the dispatch table drives this module's
    listing, and hydrate_description fills a stored row from its URL alone
    (no ATS coordinate survives `_adapt`)."""

    def test_fetch_company_adapts_this_modules_rows(self, workable_board):
        from src.ats.fetchers import company
        workable_board(load("workable_board.json"),
                       detail=load("workable_job_detail.json"))
        out = company.fetch_company({"ats": "workable", "slug": SLUG})
        assert [j["id"] for j in out][0] == "workable_eupry-aps_D68529D654"
        assert out[0]["ats"] == "workable" and out[0]["_wd"] is None
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
        job = {"ats": "workable", "url": workable.job_url(SLUG, "D68529D654"),
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
