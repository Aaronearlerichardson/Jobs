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
from pathlib import Path

import pytest

from scrapers.fetchers import ats_api, hibob, usajobs

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def match_everything(cfg, pristine_keywords):
    """Widen the relevance filter so these tests measure PARSING only.

    Mutated in place — filters.py bound the list objects at import time.
    """
    cfg.CORE_KEYWORDS[:] = [""]
    cfg.DOMAIN_KEYWORDS[:] = []
    cfg.SKILL_KEYWORDS[:] = []
    cfg.INCLUDE_KEYWORDS[:] = [""]
    cfg.EXCLUDE_PHRASES[:] = []
    cfg.EXCLUDE_TITLE_PHRASES[:] = []


@pytest.fixture
def fake_get(monkeypatch):
    """Serve a fixture instead of the network."""
    def _install(payload, status=200):
        class _Resp:
            status_code = status
            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"{status} Client Error")
            def json(self):
                return payload
            @property
            def text(self):
                return json.dumps(payload)
        monkeypatch.setattr(ats_api.SESSION, "get",
                            lambda *a, **k: _Resp())
    return _install


@pytest.fixture
def usajobs_creds(monkeypatch):
    """Credentials the USAJOBS fetcher will accept. Nothing real: the
    session is stubbed, so these never leave the process."""
    monkeypatch.setattr(usajobs.config, "USAJOBS_API_KEY", "test-key")
    monkeypatch.setattr(usajobs.config, "USAJOBS_EMAIL", "someone@example.org")


@pytest.fixture
def usajobs_pages(monkeypatch):
    """Serve a SEQUENCE of fixture pages and record each request.

    Returns the (initially empty) call log, so a test can both drive
    pagination and assert on the params and headers that were sent. Pages
    past the end of the list repeat the last one — a test that asserts a
    stop condition should fail by hanging on its own page cap, not by
    raising IndexError from the stub.
    """
    def _install(payloads, status=200):
        calls = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"{status} Client Error")

            def json(self):
                return self._payload

        def _get(url, **kwargs):
            calls.append({"url": url, "params": kwargs.get("params") or {},
                          "headers": kwargs.get("headers") or {}})
            return _Resp(payloads[min(len(calls) - 1, len(payloads) - 1)])

        monkeypatch.setattr(usajobs.SESSION, "get", _get)
        return calls
    return _install


class TestGreenhouse:
    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("greenhouse_board.json"))
        jobs = ats_api.fetch_greenhouse("databricks", "Databricks")
        assert jobs
        j = jobs[0]
        assert j["id"].startswith("gh_databricks_")
        assert j["title"] and j["url"].startswith("http")
        assert j["company"] == "Databricks"

    def test_location_falls_back_when_absent(self, fake_get, match_everything):
        payload = load("greenhouse_board.json")
        payload["jobs"][0]["location"] = {}
        fake_get(payload)
        assert ats_api.fetch_greenhouse("x", "X")[0]["location"] == "Unknown"

    def test_http_error_returns_empty_not_raises(self, fake_get, match_everything):
        fake_get({}, status=500)
        assert ats_api.fetch_greenhouse("x", "X") == []

    def test_unexpected_shape_returns_empty(self, fake_get, match_everything):
        fake_get(["not", "a", "dict"])
        assert ats_api.fetch_greenhouse("x", "X") == []


class TestLever:
    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("lever_board.json"))
        jobs = ats_api.fetch_lever("veeva", "Veeva")
        assert jobs
        j = jobs[0]
        # Prefixes are the store's dedup namespace: gh_ / lv_ / ashby_.
        assert j["id"].startswith("lv_veeva_")
        assert j["title"] and j["url"].startswith("http")

    def test_http_error_returns_empty(self, fake_get, match_everything):
        fake_get({}, status=404)
        assert ats_api.fetch_lever("x", "X") == []


class TestAshby:
    def test_reads_the_jobs_key(self, fake_get, match_everything):
        """Regression: the payload key is `jobs`, not `jobPostings`.

        Reading the wrong key returned [] for every Ashby board — silently,
        because a missing key is an empty list and the crawler cannot tell
        that from 'nothing matched'."""
        payload = load("ashby_board.json")
        assert "jobs" in payload and "jobPostings" not in payload
        fake_get(payload)
        jobs = ats_api.fetch_ashby("vanta", "Vanta")
        assert jobs, "Ashby parsed zero postings from a non-empty board"

    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("ashby_board.json"))
        j = ats_api.fetch_ashby("vanta", "Vanta")[0]
        assert j["id"].startswith("ashby_vanta_")
        assert j["title"] and j["url"].startswith("http")
        assert j["location"]

    def test_department_and_team_both_feed_relevance(self, fake_get,
                                                     match_everything):
        # `department`/`team` are the real keys; `departmentName` never
        # existed, so department text was invisible to the keyword gate.
        payload = load("ashby_board.json")
        assert any({"department", "team"} & set(j) for j in payload["jobs"])

    def test_remote_hint_from_structured_fields(self, fake_get, match_everything):
        payload = load("ashby_board.json")
        payload["jobs"][0]["isRemote"] = False
        payload["jobs"][0]["workplaceType"] = "Remote"
        fake_get(payload)
        assert ats_api.fetch_ashby("v", "V")[0].get("remote_hint") == "ashby:isRemote"

    def test_posted_at_is_captured(self, fake_get, match_everything):
        fake_get(load("ashby_board.json"))
        jobs = ats_api.fetch_ashby("vanta", "Vanta")
        assert any(j.get("posted_at") for j in jobs)

    def test_http_error_returns_empty(self, fake_get, match_everything):
        fake_get({}, status=403)
        assert ats_api.fetch_ashby("x", "X") == []


class TestHibob:
    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("hibob_board.json"))
        jobs = hibob.fetch_hibob("liquidia", "Liquidia")
        assert jobs
        j = jobs[0]
        assert j["id"].startswith("hibob_liquidia_")
        assert j["title"] and j["url"] == "https://liquidia.careers.hibob.com/jobs"
        assert j["company"] == "Liquidia"

    def test_description_html_is_stripped(self, fake_get, match_everything):
        fake_get(load("hibob_board.json"))
        jobs = hibob.fetch_hibob("liquidia", "Liquidia")
        assert all("<" not in j["description"] for j in jobs)

    def test_location_combines_site_and_workspace_type(self, fake_get,
                                                        match_everything):
        fake_get(load("hibob_board.json"))
        jobs = hibob.fetch_hibob("liquidia", "Liquidia")
        assert jobs[0]["location"] == "USA - Hybrid"

    def test_remote_hint_from_workspace_type(self, fake_get, match_everything):
        fake_get(load("hibob_board.json"))
        jobs = hibob.fetch_hibob("liquidia", "Liquidia")
        remote = [j for j in jobs if j["location"].endswith("Remote")]
        assert remote and remote[0].get("remote_hint") == "hibob:workspaceType"

    def test_http_error_returns_empty(self, fake_get, match_everything):
        fake_get({}, status=401)
        assert hibob.fetch_hibob("x", "X") == []

    def test_unexpected_shape_returns_empty(self, fake_get, match_everything):
        fake_get(["not", "a", "dict"])
        assert hibob.fetch_hibob("x", "X") == []


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
        assert [c["params"]["Page"] for c in calls] == [1, 2]

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
        params = calls[0]["params"]
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
        headers = calls[0]["headers"]
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
                                             monkeypatch, match_everything):
        def _boom(*a, **k):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(usajobs.SESSION, "get", _boom)
        assert usajobs.fetch_usajobs() == []

    def test_unexpected_shape_returns_empty(self, usajobs_creds,
                                            usajobs_pages, match_everything):
        usajobs_pages([["not", "a", "dict"]])
        assert usajobs.fetch_usajobs() == []


class TestRelevanceGate:
    """The filter is applied INSIDE the fetchers — which is why the canary
    widens it before judging a board's health."""

    def test_irrelevant_postings_are_dropped(self, cfg, fake_get,
                                             pristine_keywords):
        cfg.CORE_KEYWORDS[:] = ["quantum basket weaving"]
        cfg.DOMAIN_KEYWORDS[:] = []
        cfg.SKILL_KEYWORDS[:] = []
        cfg.INCLUDE_KEYWORDS[:] = ["quantum basket weaving"]
        fake_get(load("greenhouse_board.json"))
        assert ats_api.fetch_greenhouse("databricks", "Databricks") == []


class TestAshbyKeyAcrossCallSites:
    """Every Ashby reader, not just the one that was patched.

    The `jobs` vs `jobPostings` mix-up was found and fixed in
    `ats_api.fetch_ashby`, but the same line had been copied into the
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
    def ashby_board(self, monkeypatch):
        """Serve BOARD to every module that reads the Ashby posting API."""
        class _Resp:
            status_code = 200
            text = json.dumps(TestAshbyKeyAcrossCallSites.BOARD)

            def json(self):
                return TestAshbyKeyAcrossCallSites.BOARD

        from discovery import local_sourcing, probes
        from scrapers.fetchers import company
        for mod in (probes, local_sourcing):
            monkeypatch.setattr(mod.SESSION, "get", lambda *a, **k: _Resp())
        monkeypatch.setattr(company, "_get_json",
                            lambda *a, **k: TestAshbyKeyAcrossCallSites.BOARD)

    def test_probe_reports_the_real_total(self, ashby_board):
        from discovery.probes import probe_ashby
        assert probe_ashby("susteon") == (True, 2)

    def test_nc_counter_sees_local_jobs(self, ashby_board):
        from core.locality import is_nc
        from discovery.local_sourcing import _nc_count_ashby
        # The fixture board has two jobs in NC. Skip the test if the active
        # profile's locality doesn't include NC — the test would correctly
        # return 0, so there's nothing to test.
        if not is_nc("Morrisville, North Carolina"):
            pytest.skip("profile configures no NC locality")
        assert _nc_count_ashby("susteon") == 2

    def test_mission_scorer_gets_titles(self, ashby_board):
        from discovery.local_sourcing import _sample_titles
        titles = _sample_titles({"ats": "ashby", "slug": "susteon"})
        assert titles == ["Catalysis Scientist", "Lab Technician"]

    def test_company_fetcher_returns_postings(self, ashby_board):
        from scrapers.fetchers.company import fetch_ashby_all
        jobs = fetch_ashby_all("susteon")
        assert [j["title"] for j in jobs] == ["Catalysis Scientist", "Lab Technician"]
        assert jobs[0]["location"] == "Morrisville, North Carolina"

    def test_workday_branch_still_reads_job_postings(self, monkeypatch):
        """Workday really does return `jobPostings`. The two branches sit in
        one function, so a careless sweep would break Workday while fixing
        Ashby — this pins the other direction."""
        class _Resp:
            status_code = 200

            def json(self):
                return {"jobPostings": [{"title": "Clinical Trial Liaison"}]}

        from discovery import local_sourcing
        monkeypatch.setattr(local_sourcing.SESSION, "post", lambda *a, **k: _Resp())
        titles = local_sourcing._sample_titles(
            {"ats": "workday", "slug": ("icon", 3, "broadbean_external")})
        assert titles == ["Clinical Trial Liaison"]
