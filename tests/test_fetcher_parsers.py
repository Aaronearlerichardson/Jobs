"""ATS response parsing, against recorded fixtures.

The live canary (`tools/check_boards.py`, run nightly) answers "is the
endpoint still there?". These answer the other half: "given that response,
do we parse it correctly?" — offline, deterministic, and fast.

Both halves are needed. The Ashby fetcher read `jobPostings` from a payload
whose key is `jobs`, so it returned zero for every Ashby board while looking
perfectly healthy: no exception, no error log, just an empty list that the
crawler treats as "no matches". A test like `test_ashby_reads_the_jobs_key`
fails loudly the moment that regresses.

Fixtures are real responses with the prose redacted — the shape is what's
under test, and nobody's job descriptions need committing.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from conftest import fake_response
from src.match.filters import is_relevant
from src.ats.board import BOARDS, board_for, board_for_url, company, fields
from src.ats.board import engine as board
from src.ats.feeds import discourse, getro, remoteok, remotive, usajobs
from src.discovery import apply
from src.net import http

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name):
    """A fixture served verbatim — the XML feeds aren't JSON."""
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def match_everything(cfg, pristine_keywords):
    """Widen the relevance filter so these tests measure PARSING only.

    The same widening the canaries in tools/ apply, through the same
    function, so a test cannot pass against a filter the canary doesn't
    actually use. `pristine_keywords` puts the profile back after.
    """
    cfg.widen_keywords(cfg)


@pytest.fixture
def usajobs_creds(monkeypatch):
    """Credentials the USAJOBS fetcher will accept. Nothing real: the
    session is stubbed, so these never leave the process."""
    monkeypatch.setattr(usajobs.config, "USAJOBS_API_KEY", "test-key")
    monkeypatch.setattr(usajobs.config, "USAJOBS_EMAIL", "someone@example.org")


@pytest.fixture
def usajobs_pages(serve):
    """`serve` a SEQUENCE of fixture pages, all at `status`, and return the
    request log. Pages past the end repeat the last one -- a test that
    asserts a stop condition should fail by hanging on its own page cap,
    not by raising IndexError from the stub."""
    return lambda payloads, status=200: serve(
        [fake_response(p, status=status) for p in payloads])


#: Spec'd platforms with a recorded listing: (ats, handle, listing fixture,
#: detail fixture or None). Trimmed real responses. A listing given as
#: {URL fragment: fixture} answers each request by the first fragment its
#: URL holds.
BOARD_FIXTURES = [
    ("greenhouse", "databricks", "greenhouse_board.json", None),
    ("lever", "veeva", "lever_board.json", None),
    ("ashby", "vanta", "ashby_board.json", None),
    ("bamboohr", "imec", "bamboohr_board.json", "bamboohr_detail.json"),
    ("rippling", "blackrockneurotech", "rippling_board.json", "rippling_detail.json"),
    ("hibob", "liquidia", "hibob_board.json", None),
    ("workable", "eupry-aps", "workable_board.json", "workable_job_detail.json"),
    ("paylocity", "d527ad39-680d-45fa-9178-38a81898aec2", "paylocity_board.html",
     "paylocity_detail.html"),
    ("ultipro", "BAY1006BML|0669eed3-5441-4f8e-a7b1-c5df596a4dfe", "ultipro_board.json", None),
    ("adp", "7120c628-221c-4769-b7e7-8ab11b78b67f|9200879253113_2", "adp_board.json",
     "adp_detail.json"),
    ("smartrecruiters", "Guidehealth", "smartrecruiters_board.json",
     "smartrecruiters_detail.json"),
    ("workday", "askbio|12|AskBio", "workday_board.json", "workday_detail.json"),
    ("infor", "css-unchealthunc-prd.inforcloudsuite.com|9999", "infor_job_list.json",
     "infor_job_detail.json"),
    ("phenom", "careers.example.org", "phenom_search_results.html", "phenom_job_detail.html"),
    ("jazzhr", "paradromicsinc", {"/apply/JW9uu3rCh2/": "jazzhr_detail_nold.html",
                                  "/apply/": "jazzhr_detail.html",
                                  "applytojob.com/": "jazzhr_board.html"}, None),
    ("jobvite", "neogenomics", "jobvite_board.html", "jobvite_detail.html"),
    ("kula", "precision-neuroscience", "kula_board.html", None),
    ("successfactors", "https://careers.chiesi.com/",
     {"startrow=0": "successfactors_board.html", "startrow=3": "successfactors_board_p2.html"},
     None),
    ("icims", "uscareers-fujifilm", "icims_board.html", "icims_detail.html"),
    ("custom", "https://careers.foundationmedicine.com/jobs/search", "custom_board.html", None),
    ("wpjson", "https://www.restor3d.com/company/careers/", "wpjson_board.json", None),
]

#: Where the root redirect lands for a board whose handle follows one
#: (`handle.follow`): the first request such a board makes.
REDIRECTS = {"phenom": "https://careers.example.org/us/en"}


def _fixture_response(name):
    return (fake_response(text=load_text(name)) if name.endswith(".html")
            else fake_response(load(name)))


class TestSpecdBoardsReadTheirListings:
    """The engine reads each recorded listing (and, for a row with no body,
    the recorded detail) into exactly the rows <ats>_rows.json holds:
    recorded from each platform's fetcher module before the move to
    config.BOARDS, except where a row changed on purpose (paylocity: the
    detail page's body, not the listing's teaser; phenom: ids carry the
    host, "phenom_<host_key>_<reqId>"; jobvite: title and location
    clean_field-ed like every other sweep row; icims: no searchLocation
    on an unscoped pull, a place from the row's location column or the
    posting page, never the title; custom and wpjson: their whole-board
    rows, which had no sweep, in the sweep's shape; jazzhr: a posting
    whose page has no JSON-LD kept as the index names it, its body the
    page's; successfactors: pages stepped by the rows the tenant serves, a
    place read off the slug only where it leads the title)."""

    @pytest.mark.parametrize("ats,handle,listing,detail", BOARD_FIXTURES)
    def test_rows_match_the_recording(self, serve, monkeypatch, tmp_path, ats,
                                      handle, listing, detail):
        monkeypatch.setattr(board.time, "sleep", lambda s: None)
        monkeypatch.setattr(board.config, "DATA_DIR", tmp_path)
        if isinstance(listing, dict):
            serve({fragment: _fixture_response(name) for fragment, name in listing.items()})
        else:
            serve(([fake_response(url=REDIRECTS[ats])] if ats in REDIRECTS else []) + [
                _fixture_response(listing),
                _fixture_response(detail) if detail else fake_response(status=404)])
        assert board_for(ats).jobs(handle, "Acme") == load(f"{ats}_rows.json")

    def test_an_icims_row_reads_its_location_column(self, serve, monkeypatch, tmp_path):
        """A tenant's column is a labelled header, a labelled field, or the
        map-marked city, state and country; a row with none takes the
        posting page's, never a place-shaped title."""
        monkeypatch.setattr(board.time, "sleep", lambda s: None)
        monkeypatch.setattr(board.config, "DATA_DIR", tmp_path)

        def card(n, title, fields=""):
            return (f'<li class="iCIMS_JobCardItem"><div class="row">{fields}'
                    f'<div class="title"><a class="iCIMS_Anchor" href="https://acme.icims.com'
                    f'/jobs/{n}/x/job"><h3>{title}</h3></a></div></div></li>')

        def dd(label, value, marker='<span class="glyphicons-map-marker"></span>'):
            return (f'<dl><dt>{marker}<span class="field-label">{label}</span></dt>'
                    f'<dd><span>{value}</span></dd></dl>')
        page = "<ul>" + "".join([
            card(1, "Chemist", '<div class="header"><span class="field-label">Location</span>'
                               '<span>US-NC-Morrisville</span></div>'),
            card(2, "Engineer", dd("Job Locations", "US-NC-Winston-Salem", "")),
            card(3, "Analyst", dd("Country (Full Name)", "United States")
                 + dd("Primary Location : State/Province", "North Carolina")
                 + dd("City", "Cary HQ")),
            card(4, "Manager, Risk, Fraud")]) + "</ul>"
        serve({"/jobs/search": fake_response(text=page),
               "/jobs/4/": _fixture_response("icims_detail.html")})
        assert [r["location"] for r in board_for("icims").listing("acme")] == [
            "US-NC-Morrisville", "US-NC-Winston-Salem", "Cary HQ, North Carolina, United States",
            "Remote, US"]

    @pytest.mark.parametrize("form", [
        "",
        '<select name="searchLocation"><option value="1-2-Durham">US-NC-Durham</option></select>',
    ], ids=["no-area-option", "area-filter-ignored"])
    def test_an_unvouched_tenant_is_judged_on_its_listed_places(self, serve, monkeypatch,
                                                               tmp_path, capsys, form):
        """Nothing vouches for a tenant whose form offers no area option, nor
        for one whose search, reporting no total, opens with the unscoped
        page's postings (it ignored the filter): the pull keeps, and the
        local count samples, the listing's own location column."""
        monkeypatch.setattr(board.time, "sleep", lambda s: None)
        monkeypatch.setattr(board.config, "DATA_DIR", tmp_path)
        page = form + "<ul>" + "".join(
            f'<li><div class="header"><span class="field-label">Location</span><span>{loc}'
            f'</span></div><a class="iCIMS_Anchor" href="https://acme.icims.com/jobs/{n}/x/job">'
            f'<h3>Chemist</h3></a></li>'
            for n, loc in ((1, "US-NC-Durham"), (2, "US-TX-Austin"))) + "</ul>"
        serve({"/jobs/search": fake_response(text=page)})
        icims, nc = board_for("icims"), re.compile(r"\bNC\b")
        assert [r["location"] for r in icims.jobs("acme", "Acme", loc_re=nc)] == ["US-NC-Durham"]
        assert ("unnarrowed" in capsys.readouterr().out) == bool(form)
        assert icims.local_count("acme", nc) == 1

    def test_a_located_pull_asks_the_search_forms_area_options(self, serve, monkeypatch,
                                                               tmp_path):
        """A search reading no free-text place (iCIMS lists nothing for
        searchLocation=NC) is scoped by its form's own location options:
        every one whose label the area matches, asked together."""
        monkeypatch.setattr(board.time, "sleep", lambda s: None)
        monkeypatch.setattr(board.config, "DATA_DIR", tmp_path)
        log = serve({"searchLocation": _fixture_response("icims_board_located.html"),
                     "/jobs/search": _fixture_response("icims_board.html"),
                     "/jobs/388": _fixture_response("icims_detail_located.html")})
        rows = board_for("icims").jobs("uscareers-fujifilm", "Acme", loc_re=re.compile(r"\bNC\b"))
        assert next(r.params["searchLocation"] for r in log if "searchLocation" in r.params) == [
            "12781--Holly Springs, NC", "12781-12817-Durham", "12781-12817-Holly Springs"]
        assert [(r["id"], r["location"]) for r in rows] == [
            ("icims_uscareers-fujifilm_38891", "Holly Springs, NC, US"),
            ("icims_uscareers-fujifilm_38885", "Holly Springs, NC, US")]

    #: (stored URL, listed location, detail fixture, the location after):
    #: `detail.location` "always" replaces a listed one, "if_unknown" only
    #: fills a missing one.
    HYDRATE = [
        ("https://careers.example.org/us/en/job/273419", "Durham, NC",
         "phenom_job_detail.html", "Durham, North Carolina, United States"),
        ("https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/JobPosting%5BJobPostingSet"
         "%5D%2842%2C207651%2C1%29.JobPostingDisplay?pagesize=1", "",
         "infor_job_detail.json", "Morrisville, NC, US"),
        ("https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/JobPosting%5BJobPostingSet"
         "%5D%2842%2C207651%2C1%29.JobPostingDisplay?pagesize=1", "Chapel Hill, NC",
         "infor_job_detail.json", "Chapel Hill, NC"),
        ("https://askbio.wd12.myworkdayjobs.com/en-US/AskBio/job/Durham-NC/Eng_R1",
         "2 Locations", "workday_detail.json",
         "USA - Pennsylvania - West Point; USA - New Jersey - Rahway"),
        ("https://jobs.jobvite.com/neogenomics/job/oiiRyfwJ", "", "jobvite_detail.html",
         "Ft Myers, Florida, United States"),
        ("https://uscareers-fujifilm.icims.com/jobs/38896/national-security-manager/job"
         "?in_iframe=1", "", "icims_detail.html", "Remote, US"),
    ]

    @pytest.mark.parametrize("url,listed,detail,location", HYDRATE)
    def test_a_stored_row_hydrates_from_its_url(self, serve, url, listed, detail,
                                                location):
        """Only the URL survives the store: the engine reads the posting's
        coordinates back out of it (`job_ref`) for its detail."""
        serve(_fixture_response(detail))
        job = company.hydrate_description({"ats": board_for_url(url).name, "url": url,
                                           "description": "", "location": listed})
        assert job["location"] == location
        assert job["description"] and "<p>" not in job["description"]

    @pytest.mark.parametrize("ats", sorted(b.name for b in BOARDS.values()
                                           if b.fetchable))
    def test_an_unexpected_shape_is_an_empty_board(self, serve, ats):
        serve(fake_response(["not", "a", "board"] if ats != "lever"
                            else {"not": "a list"}))
        assert board_for(ats).jobs("x", "X") == []


class TestPeopleAdmin:
    """The Atom feeds behind the university tenants.

    The roster's PeopleAdmin row sat at zero jobs while looking healthy.
    Two things were wrong at once: the fetcher asked for `search.atom` (the
    tenant's saved search) instead of `all_jobs.atom` (the whole board), and
    the entries it did parse were then thrown away by a location filter —
    a PeopleAdmin entry has no location field at all, so every posting was
    unlocated and every posting failed the filter.
    """

    UNC = "peopleadmin_unc_all_jobs.atom"
    NCSU = "peopleadmin_ncsu_all_jobs.atom"
    EMPTY = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<feed xmlns="http://www.w3.org/2005/Atom">'
             '<title>Nowhere U: All Jobs</title></feed>')

    @staticmethod
    def place(monkeypatch, where):
        """Every posting names `where` ("" for none): the suite runs on
        whatever profile is loaded, and `place` only recognises THAT
        profile's places."""
        monkeypatch.setitem(fields.TRANSFORMS, "place", lambda v: where)

    def test_reads_both_tenants_feeds(self, serve, monkeypatch):
        """NC State serves PeopleAdmin from its own hostname and is keyed on
        its feed URL; ids carry the whole host, so the tenants cannot
        collide. `<author><name>` (the hiring department) leads the body,
        which arrives as escaped HTML and must not stay that way."""
        self.place(monkeypatch, "")
        serve({"all_jobs.atom": load_text(self.UNC)})
        unc = board_for("peopleadmin").jobs("unc.peopleadmin.com", "UNC")
        serve({"all_jobs.atom": load_text(self.NCSU)})
        ncsu = board_for("peopleadmin").jobs(
            "https://jobs.ncsu.edu/postings/all_jobs.atom", "NC State")
        assert (len(unc), len(ncsu)) == (7, 8)
        j = unc[0]
        assert (j["id"], j["title"], j["url"], j["posted_at"], j["location"]) == (
            "pa_unc_peopleadmin_com_323091", "Surgical Oncologist Faculty Appointment",
            "https://unc.peopleadmin.com/postings/323091", "2026-07-28", "")
        assert j["description"].startswith("Surgery - Surgical Oncology - 414020")
        assert "Position type: Faculty." in j["description"]
        assert all("<" not in j["description"] for j in unc)
        assert ncsu[0]["id"] == "pa_jobs_ncsu_edu_230936"
        assert all(j["url"].startswith("https://jobs.ncsu.edu/postings/") for j in ncsu)

    def test_search_atom_answers_only_when_all_jobs_cannot(self, serve):
        """An erroring or empty `all_jobs.atom` is a miss, not an answer: a
        tenant still has a saved search."""
        calls = serve({"all_jobs.atom": load_text(self.UNC),
                       "search.atom": load_text(self.NCSU)})
        assert len(board_for("peopleadmin").jobs("unc.peopleadmin.com")) == 7
        assert calls == ["https://unc.peopleadmin.com/postings/all_jobs.atom"]
        for miss in (404, self.EMPTY):
            calls = serve({"all_jobs.atom": miss, "search.atom": load_text(self.UNC)})
            assert len(board_for("peopleadmin").jobs("unc.peopleadmin.com")) == 7
            assert calls[-1].endswith("/postings/search.atom")

    def test_a_location_filter_passes_postings_naming_no_place(self, serve,
                                                               monkeypatch):
        """Unlocated postings are the campus's (`unlocated: keep`); one that
        names a place is still filtered on it."""
        serve({"all_jobs.atom": load_text(self.UNC)})
        row = {"ats": "peopleadmin", "careers_url": "unc.peopleadmin.com"}
        self.place(monkeypatch, "")
        jobs = company.fetch_company(row, re.compile("nowhere-at-all"))
        assert len(jobs) == 7 and jobs[0]["ats"] == "peopleadmin"
        self.place(monkeypatch, "Chapel Hill, NC")
        assert company.fetch_company(row, re.compile("Raleigh")) == []
        assert len(company.fetch_company(row, re.compile("Chapel Hill"))) == 7


class TestUsajobs:
    """The federal board. Credentialed, paginated, and — unlike every other
    fetcher here — allowed to be switched off by a missing env var, so the
    no-credentials path is as much a contract as the parsing is."""

    def test_parses_postings(self, usajobs_creds, usajobs_pages,
                             match_everything):
        usajobs_pages([load("usajobs_search.json")])
        jobs = usajobs.fetch_usajobs(location="Research Triangle Park, "
                                              "North Carolina", radius=25)
        assert len(jobs) == 2
        j = jobs[0]
        assert j["id"] == "usajobs_830216800"
        assert j["title"] == "IT Specialist (Data Management)"
        assert j["url"] == "https://www.usajobs.gov/job/830216800"

    def test_company_is_the_organization_not_the_department(
            self, usajobs_creds, usajobs_pages, match_everything):
        """`OrganizationName` is the lab a reader recognizes; the cabinet
        department it reports to is not. The department stays in the body
        so it remains searchable."""
        usajobs_pages([load("usajobs_search.json")])
        j = usajobs.fetch_usajobs()[1]
        assert j["company"] == ("National Institute of Environmental "
                                "Health Sciences")
        assert j["description"].startswith(
            "Department of Health and Human Services.")

    def test_every_duty_station_is_kept(self, usajobs_creds, usajobs_pages,
                                        match_everything):
        """One vacancy open at two campuses must not lose the local one."""
        usajobs_pages([load("usajobs_search.json")])
        assert usajobs.fetch_usajobs()[1]["location"] == (
            "Research Triangle Park, North Carolina; Bethesda, Maryland")

    def test_description_carries_summary_duties_quals_and_pay(
            self, usajobs_creds, usajobs_pages, match_everything):
        usajobs_pages([load("usajobs_search.json")])
        desc = usajobs.fetch_usajobs()[0]["description"]
        assert "Job summary redacted" in desc
        assert "First major duty redacted" in desc
        assert "Second major duty redacted" in desc
        assert "Qualification summary redacted" in desc
        assert "Salary: $99,908 - $129,878 Per Year" in desc

    def test_posted_at_is_normalized(self, usajobs_creds, usajobs_pages,
                                     match_everything):
        usajobs_pages([load("usajobs_search.json")])
        assert [j["posted_at"] for j in usajobs.fetch_usajobs()] == [
            "2026-08-03", "2026-08-10"]

    def test_url_falls_back_to_apply_uri(self, usajobs_creds, usajobs_pages,
                                         match_everything):
        usajobs_pages([load("usajobs_search.json")])
        assert usajobs.fetch_usajobs()[1]["url"] == (
            "https://www.usajobs.gov/job/830216801/apply")

    def test_remote_hint_only_on_remote_announcements(
            self, usajobs_creds, usajobs_pages, match_everything):
        """`remote_signal_for` treats ANY hint as decisive, so stamping a
        non-remote posting would advertise it as remote-eligible."""
        usajobs_pages([load("usajobs_search.json")])
        jobs = usajobs.fetch_usajobs()
        assert "remote_hint" not in jobs[0]
        assert jobs[1]["remote_hint"] == "usajobs:RemoteIndicator"

    def test_pages_until_the_reported_total(self, usajobs_creds,
                                            usajobs_pages, match_everything):
        """SearchResultCountAll is 3 with 2 per page, so a single-page read
        would silently drop the last announcement."""
        page2 = load("usajobs_search.json")
        item = copy.deepcopy(page2["SearchResult"]["SearchResultItems"][0])
        item["MatchedObjectId"] = "830216802"
        page2["SearchResult"]["SearchResultItems"] = [item]
        page2["SearchResult"]["SearchResultCount"] = 1
        calls = usajobs_pages([load("usajobs_search.json"), page2])

        jobs = usajobs.fetch_usajobs()
        assert [j["id"] for j in jobs] == [
            "usajobs_830216800", "usajobs_830216801", "usajobs_830216802"]
        assert [c.params["Page"] for c in calls] == [1, 2]

    def test_stops_on_an_empty_page(self, usajobs_creds, usajobs_pages,
                                    match_everything):
        """A total that overstates what the API returns must not spin."""
        empty = {"SearchResult": {"SearchResultCountAll": 99,
                                  "SearchResultItems": []}}
        calls = usajobs_pages([load("usajobs_search.json"), empty])
        assert len(usajobs.fetch_usajobs()) == 2
        assert len(calls) == 2

    def test_series_and_location_become_query_params(
            self, usajobs_creds, usajobs_pages, match_everything):
        calls = usajobs_pages([load("usajobs_search.json")])
        usajobs.fetch_usajobs(keyword="data", location="Durham, NC",
                              radius=25, series=["2210", "1550"])
        params = calls[0].params
        assert params["JobCategoryCode"] == "2210;1550"
        assert params["LocationName"] == "Durham, NC"
        assert params["Radius"] == 25
        assert params["Keyword"] == "data"

    def test_credentials_travel_in_the_documented_headers(
            self, usajobs_creds, usajobs_pages, match_everything):
        """The API keys off `Authorization-Key` plus the REGISTERED address
        as User-Agent; the shared session's browser UA would be rejected."""
        calls = usajobs_pages([load("usajobs_search.json")])
        usajobs.fetch_usajobs()
        headers = calls[0].headers
        assert headers["Authorization-Key"] == "test-key"
        assert headers["User-Agent"] == "someone@example.org"
        assert headers["Host"] == "data.usajobs.gov"

    def test_no_credentials_returns_empty_without_fetching(
            self, monkeypatch, usajobs_pages, match_everything):
        monkeypatch.setattr(usajobs.config, "USAJOBS_API_KEY", "")
        monkeypatch.setattr(usajobs.config, "USAJOBS_EMAIL", "")
        calls = usajobs_pages([load("usajobs_search.json")])
        assert usajobs.fetch_usajobs() == []
        assert calls == []

    def test_email_alone_is_not_enough(self, monkeypatch, usajobs_pages,
                                       match_everything):
        monkeypatch.setattr(usajobs.config, "USAJOBS_API_KEY", "")
        monkeypatch.setattr(usajobs.config, "USAJOBS_EMAIL", "a@b.org")
        usajobs_pages([load("usajobs_search.json")])
        assert usajobs.fetch_usajobs() == []

    def test_http_error_returns_empty(self, usajobs_creds, usajobs_pages,
                                      match_everything):
        usajobs_pages([load("usajobs_search.json")], status=401)
        assert usajobs.fetch_usajobs() == []

    def test_request_exception_returns_empty(self, usajobs_creds,
                                             serve, match_everything):
        serve(RuntimeError("connection reset"))
        assert usajobs.fetch_usajobs() == []

    def test_unexpected_shape_returns_empty(self, usajobs_creds,
                                            usajobs_pages, match_everything):
        usajobs_pages([["not", "a", "dict"]])
        assert usajobs.fetch_usajobs() == []


class TestRelevanceGate:
    """The keyword filter is a `gate` PARAMETER of every fetcher, injected
    by the ATS registry (and the runner's feed thunks) rather than
    imported by the fetcher modules. A fetcher called with no gate keeps
    every posting; the registry's thunk keeps only the relevant ones —
    which is why the canary widens the filter before judging a board's
    health."""

    @pytest.fixture
    def nothing_matches(self, cfg, pristine_keywords):
        cfg.CORE_KEYWORDS[:] = ["quantum basket weaving"]
        cfg.DOMAIN_KEYWORDS[:] = []
        cfg.SKILL_KEYWORDS[:] = []
        cfg.INCLUDE_KEYWORDS[:] = ["quantum basket weaving"]

    def test_irrelevant_postings_are_dropped_by_the_gate(
            self, serve, nothing_matches):
        serve(fake_response(load("greenhouse_board.json")))
        assert board_for("greenhouse").jobs("databricks", "Databricks",
                                            gate=is_relevant) == []

    def test_no_gate_keeps_everything(self, serve, nothing_matches):
        serve(fake_response(load("greenhouse_board.json")))
        assert board_for("greenhouse").jobs("databricks", "Databricks")

    def test_the_registry_thunk_is_gated(self, serve, nothing_matches):
        from src.ats.registry import sweep
        serve(fake_response(load("greenhouse_board.json")))
        assert sweep("greenhouse", "Databricks", "databricks")() == []

    def test_no_fetcher_module_imports_the_filter_or_config_timeouts(self):
        """The point of the parameter: a fetcher module is reusable with
        any gate and any session, so none of them may bind the keyword
        filter or the timeout constant at import. The few that read
        `config` need something else from it (credentials, locality,
        the data dir)."""
        import re
        from pathlib import Path
        pkg = Path(company.__file__).parent
        for src in pkg.glob("*.py"):
            text = src.read_text(encoding="utf-8")
            assert not re.search(r"^from core\.filters import", text, re.M), src.name
            assert not re.search(r"^from config import", text, re.M), src.name
            assert "FETCH_TIMEOUT" not in text, src.name


class TestAshbyKeyAcrossCallSites:
    """Every Ashby reader, not just the one that was patched.

    The `jobs` vs `jobPostings` mix-up was found and fixed in
    `api.fetch_ashby`, but the same line had been copied into the
    discovery probe, the NC counter, the mission-scoring title sampler and
    the company fetcher. All four kept reading `jobPostings`, so Ashby
    boards probed live-but-empty, never counted a local job, and were
    mission-scored with no titles at all. Pinning every call site together
    is what stops the next copy from going stale on its own.

    The distinction is real, not cosmetic: Workday's API genuinely returns
    `jobPostings`, which is why the wrong key looked plausible.
    """

    BOARD = {"apiVersion": "1", "jobs": [
        {"id": "j1", "title": "Catalysis Scientist",
         "location": "Morrisville, North Carolina", "jobUrl": "https://x/1",
         "descriptionPlain": "...", "publishedAt": "2026-05-28T00:00:00Z",
         "secondaryLocations": [], "isRemote": False},
        {"id": "j2", "title": "Lab Technician",
         "location": "Durham, NC", "jobUrl": "https://x/2",
         "descriptionPlain": "...", "publishedAt": "2026-06-24T00:00:00Z",
         "secondaryLocations": [], "isRemote": False},
    ]}

    @pytest.fixture
    def ashby_board(self, serve):
        """Serve BOARD to every module that reads the Ashby posting API."""
        serve(fake_response(TestAshbyKeyAcrossCallSites.BOARD))

    def test_probe_reports_the_real_total(self, ashby_board):
        assert board_for("ashby").probe("susteon") == (True, 2)

    def test_nc_counter_sees_local_jobs(self, ashby_board):
        from src.match.locality import is_nc
        from src.discovery.resolve.probes import _nc_count
        # The fixture board has two jobs in NC. Skip the test if the active
        # profile's locality doesn't include NC — the test would correctly
        # return 0, so there's nothing to test.
        if not is_nc("Morrisville, North Carolina"):
            pytest.skip("profile configures no NC locality")
        assert _nc_count("ashby", "susteon") == 2

    def test_mission_scorer_gets_titles(self, ashby_board):
        from src.discovery.local_sourcing import _sample_titles
        titles = _sample_titles({"ats": "ashby", "slug": "susteon"})
        assert titles == ["Catalysis Scientist", "Lab Technician"]

    def test_company_fetcher_returns_postings(self, ashby_board):
        from src.ats.board.company import fetch_company
        jobs = fetch_company({"ats": "ashby", "slug": "susteon"})
        assert [j["title"] for j in jobs] == ["Catalysis Scientist", "Lab Technician"]
        assert jobs[0]["location"] == "Morrisville, North Carolina"
        assert jobs[0]["ats"] == "ashby" and jobs[0]["posted_at"] == "2026-05-28"


class TestGetro:
    """A Getro network board, read from its own host only: the sitemap is
    the listing, each posting is a server-rendered page carrying the
    record in ``__NEXT_DATA__``. ``api.getro.com`` (which the board's own
    JavaScript would call) disallows every crawler, so nothing here may
    ever ask it for anything.
    """

    BOARD = "https://jobs.example-network.org"
    IDS = ("91000001", "91000002", "91000003", "91000004")

    @pytest.fixture
    def board(self, serve):
        routes = {"sitemap.xml": load_text("getro_sitemap.xml")}
        for jid in self.IDS:
            routes[f"/jobs/{jid}-"] = load_text(f"getro_job_{jid}.html")
        return serve(routes)

    def test_parses_the_board_newest_first(self, board, match_everything):
        jobs = getro.fetch_getro_all(self.BOARD, detail_delay=0)
        assert [j["id"] for j in jobs] == [
            "getro_91000002", "getro_91000001", "getro_91000004"]

    def test_the_record_becomes_a_job_dict(self, board, match_everything):
        j = {x["id"]: x for x in getro.fetch_getro_all(
            self.BOARD, detail_delay=0)}["getro_91000001"]
        assert j["company"] == "Acme Analytics"
        assert j["title"] == "Senior Data Engineer"
        # The employer's OWN posting, not the board page.
        assert j["url"] == "https://boards.greenhouse.io/acmeanalytics/jobs/4000001"
        assert j["location"] == "Durham, NC, USA; Raleigh, NC, USA"
        assert j["posted_at"] == "2026-08-28"
        assert "Python pipelines" in j["description"]
        assert "<" not in j["description"]
        assert j["via"] == "getro:jobs.example-network.org"
        assert j["_employer"]["domain"] == "acme-analytics.example"
        assert j["_employer"]["slug"] == "acme-analytics"

    def test_posted_at_falls_back_to_the_sitemap_lastmod(
            self, board, match_everything):
        j = {x["id"]: x for x in getro.fetch_getro_all(
            self.BOARD, detail_delay=0)}["getro_91000002"]
        assert j["posted_at"] == "2026-08-30"

    def test_a_closed_posting_the_sitemap_still_lists_is_dropped(
            self, board, match_everything):
        ids = [j["id"] for j in getro.fetch_getro_all(self.BOARD, detail_delay=0)]
        assert "getro_91000003" not in ids

    def test_titles_are_screened_before_any_page_fetch(self, board):
        jobs = getro.fetch_getro_all(
            self.BOARD, detail_delay=0,
            gate=lambda title, desc="": "data" in title.lower())
        assert [j["id"] for j in jobs] == ["getro_91000001", "getro_91000004"]
        pages = [u for u in board if "/jobs/" in u]
        assert all("91000001" in u or "91000004" in u for u in pages)
        assert not any("api.getro.com" in u for u in board)

    def test_the_detail_cap_trims_the_oldest(self, board, match_everything):
        jobs = getro.fetch_getro_all(self.BOARD, max_details=2, detail_delay=0)
        assert [j["id"] for j in jobs] == ["getro_91000002"]
        assert not any("91000001" in u or "91000004" in u for u in board)

    def test_a_challenged_board_returns_empty(self, serve,
                                              match_everything):
        # Cloudflare's "Just a moment..." answers the sitemap with a 403.
        serve({"sitemap.xml": 403})
        assert getro.fetch_getro_all(self.BOARD, detail_delay=0) == []

    def test_a_sitemap_index_is_followed(self, serve, match_everything):
        index = ('<sitemapindex><sitemap><loc>'
                 'https://jobs.example-network.org/sitemaps/jobs-1.xml'
                 '</loc></sitemap></sitemapindex>')
        routes = {"sitemap.xml": index,
                  "sitemaps/jobs-1.xml": load_text("getro_sitemap.xml")}
        for jid in self.IDS:
            routes[f"/jobs/{jid}-"] = load_text(f"getro_job_{jid}.html")
        serve(routes)
        assert len(getro.fetch_getro_all(self.BOARD, detail_delay=0)) == 3

    def test_a_page_without_the_record_is_skipped(self, serve,
                                                   match_everything):
        routes = {"sitemap.xml": load_text("getro_sitemap.xml"),
                  "/jobs/91000001-": "<html><body>moved</body></html>"}
        for jid in self.IDS[1:]:
            routes[f"/jobs/{jid}-"] = load_text(f"getro_job_{jid}.html")
        serve(routes)
        ids = [j["id"] for j in getro.fetch_getro_all(self.BOARD, detail_delay=0)]
        assert ids == ["getro_91000002", "getro_91000004"]


class TestGetroAttribution:
    """Board-sourced jobs name their employer; the crawl links each to the
    roster and queues the employers the roster lacks -- never activating
    one on its own.

    The subject is src.discovery.apply.attribute_employers, not the
    getro parser. It used to live in the fetcher, which made that the
    one module under src/ats that wrote to the store; the jobs it acts
    on are still shaped by getro, which is why the cases stay here.
    """

    BOARD = "jobs.example-network.org"
    GH_URL = "https://boards.greenhouse.io/acmeanalytics/jobs/4000001"

    def _job(self, name, url, jid="1", slug="", domain=""):
        return {"id": f"getro_{jid}", "company": name, "title": "Data Engineer",
                "url": url, "location": "Durham, NC, USA", "description": "",
                "via": f"getro:{self.BOARD}",
                "_employer": {"name": name, "domain": domain, "slug": slug,
                              "board": self.BOARD, "page_url": ""}}

    def test_links_to_the_roster_row_owning_the_board(self, db):
        from src import store
        cid = store.upsert_company(db, {"name": "Acme Analytics Inc",
                                        "ats": "greenhouse",
                                        "slug": "acmeanalytics", "active": 1})
        job = self._job("Acme Analytics", self.GH_URL)
        kept = apply.attribute_employers(db, [job])
        assert kept == [job] and job["company_id"] == cid
        assert len(store.get_companies(db, active_only=False)) == 1

    def test_a_copy_the_roster_crawl_already_stored_is_dropped(self, db):
        from src import store
        cid = store.upsert_company(db, {"name": "Acme Analytics",
                                        "ats": "greenhouse",
                                        "slug": "acmeanalytics", "active": 1})
        store.upsert_job(db, {"job_id": "gh_acmeanalytics_4000001",
                              "company_id": cid, "company_name": "Acme Analytics",
                              "title": "Data Engineer", "url": self.GH_URL})
        assert apply.attribute_employers(
            db, [self._job("Acme Analytics", self.GH_URL)]) == []

    def test_a_pending_row_does_not_own_a_crawl_yet(self, db):
        from src import store
        cid = store.upsert_company(db, store.mark_pending(
            {"name": "Acme Analytics", "ats": "greenhouse",
             "slug": "acmeanalytics"}))
        store.upsert_job(db, {"job_id": "gh_acmeanalytics_4000001",
                              "company_id": cid, "title": "Data Engineer",
                              "url": self.GH_URL})
        job = self._job("Acme Analytics", self.GH_URL)
        assert apply.attribute_employers(db, [job]) == [job]
        assert job["company_id"] == cid

    def test_links_by_name_when_the_apply_link_names_no_ats(self, db):
        from src import store
        cid = store.upsert_company(db, {"name": "Orbit Health", "ats": "custom",
                                        "careers_url": "https://orbit.health/jobs",
                                        "active": 1})
        job = self._job("Orbit Health", "https://orbit.health/jobs/analyst")
        apply.attribute_employers(db, [job])
        assert job["company_id"] == cid
        assert len(store.get_companies(db, active_only=False)) == 1

    def test_an_unknown_employer_is_queued_for_review(self, db):
        from src import tags
        from src import store
        job = self._job("Orbit Health", "https://orbit.health/jobs/analyst",
                        slug="orbit-health", domain="orbit.health")
        assert apply.attribute_employers(db, [job]) == [job]
        (row,) = store.pending_companies(db)
        assert row["name"] == "Orbit Health"
        assert row["source"] == f"getro:{self.BOARD}"
        assert row["careers_url"] == "https://orbit.health"
        assert tags.has(row["tags"], tags.PENDING)
        assert job["company_id"] == row["id"]
        assert store.get_company(db, row["id"])["active"] == 0
        assert store.crawlable_companies(db) == []
        assert not store.is_confirmed_company(db, "Orbit Health")

    def test_the_apply_link_supplies_the_candidates_board(self, db):
        from src import tags
        from src import store
        apply.attribute_employers(
            db, [self._job("Acme Analytics", self.GH_URL, slug="acme-analytics")])
        (row,) = store.pending_companies(db)
        assert (row["ats"], row["slug"]) == ("greenhouse", "acmeanalytics")
        assert tags.has(row["tags"], tags.SWEEP)
        assert tags.has(row["tags"], tags.PENDING)

    def test_a_rejected_name_stays_rejected(self, db):
        from src import store
        store.block_name(db, "Bolt Logistics", "not a company")
        kept = apply.attribute_employers(
            db, [self._job("Bolt Logistics", "https://bolt.example/careers/3")])
        assert kept == []
        assert store.get_companies(db, active_only=False) == []

    def test_a_preview_run_writes_nothing(self, db):
        from src import store
        job = self._job("Orbit Health", "https://orbit.health/jobs/analyst")
        assert apply.attribute_employers(db, [job], commit=False) == [job]
        assert "company_id" not in job
        assert store.get_companies(db, active_only=False) == []

    def test_jobs_without_an_employer_pass_through(self, db):
        plain = {"id": "usajobs_1", "url": "https://www.usajobs.gov/job/1"}
        assert apply.attribute_employers(db, [plain]) == [plain]


class TestJobvite:
    """What the fixture contract above does not reach: the listing's
    fallback page and the whole-board pull's in-area detail reads."""

    def test_a_dead_search_falls_back_to_the_jobs_page(self, serve):
        """Listing alternatives: some tenants replace the search page with a
        landing page, so the second listing answers."""
        calls = serve({"search": 500, "/neogenomics/jobs": load_text("jobvite_board.html"),
                       "/job/": load_text("jobvite_detail.html")})
        jobs = company.fetch_company({"ats": "jobvite", "slug": "neogenomics"})
        assert len(jobs) == 3 and [c.url.rsplit("/", 1)[-1] for c in calls[:2]] == [
            "search", "jobs"]

    def test_company_fetch_pays_only_for_in_region_pages(self, serve):
        calls = serve({"search": load_text("jobvite_board.html"),
                       "/job/": load_text("jobvite_detail.html")})
        jobs = company.fetch_company({"ats": "jobvite", "slug": "neogenomics"},
                                     re.compile("Florida"))
        assert [j["id"] for j in jobs] == ["jv_neogenomics_oiiRyfwJ"]
        assert jobs[0]["posted_at"] == "2025-11-06" and jobs[0]["ats"] == "jobvite"
        assert [c.url for c in calls if "/job/" in c.url] == [
            "https://jobs.jobvite.com/neogenomics/job/oiiRyfwJ"]


class TestOneFetcherPerAts:
    """The sweep's registry and the company-vetted dispatch table drive the
    SAME fetcher module per ATS: the registry adds the keyword gate, the
    dispatch table a location regex, and neither keeps a copy of the
    parser. Two copies of the Ashby reader once drifted (see
    TestAshbyKeyAcrossCallSites); one implementation cannot.
    """

    def test_the_dispatch_table_adapts_the_module_fetcher(self, serve,
                                                          match_everything):
        from src.ats.board import company
        serve(fake_response(load("greenhouse_board.json")))
        module = board_for("greenhouse").jobs("databricks", "Databricks")
        vetted = company.fetch_company({"ats": "greenhouse", "slug": "databricks"})
        assert [j["id"] for j in vetted] == [j["id"] for j in module]
        assert all(j["ats"] == "greenhouse" and "company" not in j for j in vetted)

    def test_the_location_regex_filters_the_listing(self, serve,
                                                    match_everything):
        from src.ats.board import company
        serve(fake_response(load("greenhouse_board.json")))
        everything = company.fetch_company({"ats": "greenhouse", "slug": "x"})
        nowhere = company.fetch_company({"ats": "greenhouse", "slug": "x"},
                                        re.compile("nowhere-at-all"))
        assert everything and nowhere == []


class TestTitleSampling:
    """The mission scorer's title sample (company.sample_titles) is read
    through the same per-ATS fetchers as every other pull, at listing cost.

    It used to be hand-written requests for four ATS families and nothing
    for the other sixteen, so a company on one of them was mission-scored
    from its name alone: "Studycast", the cloud-PACS product of a Raleigh
    medical-imaging vendor (Rippling board core-sound-imaging), came back
    `other` / 0.05 as a study-education platform.
    """

    RIPPLING = [{"uuid": f"0000000{i}-0000-0000-0000-000000000000",
                 "name": name, "workLocation": {"label": "Raleigh, NC"}}
                for i, name in enumerate(["PACS Support Engineer",
                                          "Imaging Software Developer",
                                          "PACS Support Engineer"])]

    def test_a_rippling_board_is_sampled_at_listing_cost(self, serve):
        urls = serve(fake_response(self.RIPPLING))
        titles = company.sample_titles(
            {"ats": "rippling", "slug": "core-sound-imaging"})
        assert titles == ["PACS Support Engineer", "Imaging Software Developer"]
        assert len(urls) == 1 and "core-sound-imaging" in urls[0]

    def test_a_bamboohr_board_is_sampled_at_listing_cost(self, serve):
        urls = serve(fake_response({"result": [
            {"id": 7, "jobOpeningName": "Field Service Engineer",
             "location": {"city": "Cary", "state": "NC"}}]}))
        assert company.sample_titles({"ats": "bamboohr", "slug": "acme"}) == \
            ["Field Service Engineer"]
        assert [u.rsplit("/", 1)[-1] for u in urls] == ["list"]

    def test_a_one_request_family_samples_through_its_company_fetcher(
            self, serve):
        serve(fake_response(load("hibob_board.json")))
        row = {"ats": "hibob", "slug": "acme"}
        whole = [j["title"] for j in company.fetch_company(row)]
        assert whole and company.sample_titles(row, n=50) == whole
        assert company.sample_titles(row, n=1) == whole[:1]

    def test_titles_are_distinct_and_capped(self, serve):
        serve(fake_response(self.RIPPLING))
        row = {"ats": "rippling", "slug": "core-sound-imaging"}
        assert company.sample_titles(row, n=2) == [
            "PACS Support Engineer", "Imaging Software Developer"]
        assert company.sample_titles(row, n=1) == ["PACS Support Engineer"]

    @pytest.mark.parametrize("row", [
        {"ats": "rippling", "slug": "gone"},
        {"ats": "bamboohr", "slug": "gone"},
        {"ats": "custom", "careers_url": None},   # nothing to fetch
        {"ats": "no-such-ats"},                   # nothing that could
    ])
    def test_an_unreadable_board_samples_empty_and_never_raises(
            self, row, serve, capsys):
        serve(OSError("connection refused"))
        http.reset_fetch_failures()
        assert company.sample_titles(row) == []


class TestMissionContext:
    """What the mission scorer is TOLD about a board: its live titles, else
    the board's own address. Every scoring call site asks
    local_sourcing.mission_context, so none sends a bare name."""

    BOARD = {"name": "Studycast", "ats": "rippling", "slug": "core-sound-imaging",
             "careers_url": "https://ats.rippling.com/core-sound-imaging/jobs"}

    def test_live_titles_are_the_context(self, monkeypatch):
        from src.discovery import local_sourcing
        monkeypatch.setattr(local_sourcing, "_sample_titles",
                            lambda h: ["PACS Engineer", "", "Sales Lead"])
        assert local_sourcing.mission_context(self.BOARD) == \
            "PACS Engineer | Sales Lead"

    def test_no_titles_means_the_board_address(self, monkeypatch):
        from src.discovery import local_sourcing
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        ctx = local_sourcing.mission_context(self.BOARD)
        assert "core-sound-imaging" in ctx and "no open postings" in ctx

    def test_a_board_with_no_address_stays_empty(self, monkeypatch):
        from src.discovery import local_sourcing
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        assert local_sourcing.mission_context({"ats": "custom"}) == ""

    def test_the_scorer_is_sent_the_context_for_an_unsampled_family(
            self, serve, monkeypatch):
        """Through the real sampler: a Rippling board (no branch of its own
        before 2026-09-18) reaches the scorer with its titles, and once its
        board is empty, with its address."""
        from src.discovery import local_sourcing
        sent = []
        monkeypatch.setattr(
            "src.claude.api.score_company_mission",
            lambda name, context="": sent.append(context) or ("adjacent", .5, ""))
        serve(fake_response(TestTitleSampling.RIPPLING))
        local_sourcing._score_hit(self.BOARD)
        assert sent[-1] == "PACS Support Engineer | Imaging Software Developer"
        serve(fake_response([]))
        local_sourcing._score_hit(self.BOARD)
        assert "core-sound-imaging" in sent[-1]


class TestADeadEndpointIsNeverAnException:
    """Every JSON-pulling fetcher goes through net.http.get_json, and the
    scraped ones report through net.http.fetch_failed, so the "a dead
    source reports and returns empty" contract is ONE contract, checked
    for both failure shapes (a refused socket and an HTTP error reach
    get_json's handler by different routes).

    Parametrised over the fetchable specs themselves, so a board platform
    is covered the day it is specced; FEEDS are the sources outside them.

    Notes:
        A hand-kept list of calls stood here until 2026-09-22 and was never
        extended: Workable, HiBob and PeopleAdmin grew per-file copies (the
        last two asserting [] but not the COUNT), and ten fetchers had no
        dead-listing test at all.
    """

    #: One registry row every fetcher can read its coordinates from; the
    #: ATSes whose slug is structured get their own.
    ROW = {"slug": "acme", "careers_url": "https://acme.test/careers",
           "wd_tenant": "acme", "wd_pod": 5, "wd_site": "External"}
    SLUGS = {"adp": "cid|ccid", "ultipro": "CODE|GUID",
             "infor": "css-acme-prd.inforcloudsuite.com|42"}
    FEEDS = {
        "remoteok": lambda: remoteok.fetch_remoteok(),
        "remotive": lambda: remotive.fetch_remotive(),
        "discourse": lambda: discourse.fetch_discourse(
            "Forum", "https://forum.test", 1),
    }

    @pytest.fixture(params=["refused", "http-500"])
    def dead_source(self, request, serve):
        serve(OSError("connection refused") if request.param == "refused"
              else fake_response(status=500))

    @pytest.mark.parametrize("name", sorted(n for n in BOARDS if board_for(n)) + sorted(FEEDS))
    def test_reports_and_returns_empty(self, name, dead_source, capsys,
                                       match_everything):
        http.reset_fetch_failures()
        if name in self.FEEDS:
            got = self.FEEDS[name]()
        else:
            got = company.fetch_company({**self.ROW, "ats": name,
                                         "slug": self.SLUGS.get(name, "acme")})
        assert got == []
        # A dead source must be COUNTED, not just logged: an uncounted
        # failure and a genuinely empty board are the same [] to a caller
        # that only checks the return value (see net.http.fetch_failed).
        assert http.fetch_failures() > 0, (
            f"{name} returned [] without counting the failure")
        assert "[!]" in capsys.readouterr().out, "a dead source must be reported"
