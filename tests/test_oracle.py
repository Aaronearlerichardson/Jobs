"""Oracle Recruiting Cloud (its `config.BOARDS` spec, run by src.ats.board.engine):
the one-wrapper listing, the offices it merges, the per-requisition detail
call, closure, and the board detection that promotes it out of the lead bucket.

`tests/fixtures/oracle_board.json` and `tests/fixtures/oracle_job_detail.json`
are trimmed REAL responses, recorded live on 2026-10-05 from UL Solutions'
site (`ULSolutionsCareers` on tenant host `fa-eups-saasfaprod1`): four of the
444 listed requisitions verbatim (`TotalJobsCount` set to 4 so the fixture is
a consistent board), and one requisition's detail with its prose blobs cut to
a couple of items that keep their real markup.
"""

import pytest

from conftest import fake_response, fixture, no_pacing
from src.ats.board import board_for
from src.ats.signatures import detect
from src import tags
from src.ats.board import board_for_url
from src.ats.board import company
from src.ats.registry import seed_tag_for
from src.match.locality import is_nc

HOST = "fa-eups-saasfaprod1.fa.ocs.oraclecloud.com"
SITE = "ULSolutionsCareers"
HANDLE = f"{HOST}|{SITE}"
JOB_URL = f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/10139"
ORACLE = board_for("oracle")


def _board(requisitions, total=None):
    """A listing answer in the real wrapper's shape."""
    return {"items": [{"SiteNumber": SITE, "TotalJobsCount": len(requisitions) if total is None else total,
                       "requisitionList": requisitions}], "hasMore": False}


REQ = fixture("oracle_board.json")["items"][0]["requisitionList"][0]


def _req(**fields):
    """One listing entry: the fixture's first requisition, `fields` over it."""
    return {**REQ, **fields}


@pytest.fixture
def oracle_board(serve):
    """`serve` one site: the listing under its finder, one detail answer for
    any per-requisition GET."""
    return lambda board=None, detail=None: serve(
        {"RequisitionDetails": fake_response(detail), "findReqs": fake_response(board)})


class TestListing:
    async def test_rows_carry_the_fields_a_review_needs(self, oracle_board):
        oracle_board(fixture("oracle_board.json"))
        rows = await ORACLE.listing(HANDLE)
        assert [r["id"] for r in rows] == [f"oracle_fa-eups-saasfaprod1_{i}"
                                           for i in (10139, 10947, 11068, 11129)]
        assert rows[0]["title"] == "Engineer - Electrical Distribution"
        assert rows[0]["url"] == JOB_URL
        assert rows[0]["posted_at"] == "2026-10-05"

    async def test_a_multi_site_requisition_names_every_office(self, oracle_board,
                                                               local_addr, elsewhere):
        """The flat `PrimaryLocation` shows one office; `secondaryLocations`
        holds the rest, which `locality.is_nc` reads one at a time."""
        oracle_board(_board([_req(PrimaryLocation=elsewhere, secondaryLocations=[
            {"Name": f"{local_addr}, United States"}, {"Name": elsewhere}])]))
        loc = (await ORACLE.listing(HANDLE))[0]["location"]
        assert loc == f"{elsewhere}; {local_addr}, United States"
        assert is_nc(loc)

    @pytest.mark.parametrize("code,hinted", [("ORA_REMOTE", True), ("ORA_HYBRID", False),
                                             (None, False)])
    async def test_only_a_remote_workplace_is_a_remote_hint(self, oracle_board, code, hinted):
        oracle_board(_board([_req(WorkplaceTypeCode=code)]))
        row = (await ORACLE.listing(HANDLE))[0]
        assert bool(row.get("remote_hint")) is hinted

    async def test_the_walk_steps_the_offset_until_the_total(self, oracle_board, monkeypatch):
        no_pacing(monkeypatch)
        calls = oracle_board(_board([_req(Id=str(i)) for i in range(200)], total=201))
        await ORACLE.listing(HANDLE)
        assert [c.url.split("offset=")[1][:3] for c in calls][:2] == ["0", "200"]

    async def test_the_probe_reads_one_row_and_the_boards_total(self, oracle_board):
        calls = oracle_board(fixture("oracle_board.json"))
        assert await ORACLE.alive(HANDLE) == (True, 4)
        assert "limit=1," in calls[0].url


class TestDetail:
    async def test_the_description_joins_the_prose_and_drops_the_boilerplate(self, oracle_board):
        oracle_board(fixture("oracle_board.json"), fixture("oracle_job_detail.json"))
        job = {"ats": "oracle", "url": JOB_URL, "description": "", "location": "Raleigh, NC"}
        out = await company.hydrate_description(job)
        assert "Determines project scope" in out["description"]
        assert "1-4 years of experience" in out["description"]
        assert "global leader in applied safety science" not in out["description"]

    async def test_a_pulled_requisition_is_closed_and_a_served_one_open(self, oracle_board):
        oracle_board(detail=fixture("oracle_job_detail.json"))
        assert await ORACLE.probe_job(JOB_URL) == (True, "oracle api: posting live")
        oracle_board(detail={"items": [], "count": 0, "hasMore": False})
        is_open, why = await ORACLE.probe_job(JOB_URL)
        assert is_open is False and "no longer served" in why


class TestDetection:
    """Oracle was a detection-less platform until 2026-10: no spec named it."""

    @pytest.mark.parametrize("url", [
        f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/jobs",
        JOB_URL,
        f"https://{HOST.upper()}/hcmUI/CandidateExperience/fr/sites/{SITE}/requisitions",
        f"https://{HOST}/?keyword=&mode=jobs&lang=en&site_number={SITE}#10139",
    ])
    def test_every_board_url_shape_resolves_to_the_host_and_site(self, url):
        assert detect("", url) == ("fetchable", "oracle", HANDLE)

    def test_a_pod_less_tenant_host_is_a_board_too(self):
        assert detect("", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/")[2] \
            == "jpmc.fa.oraclecloud.com|CX_1001"

    def test_a_host_with_no_site_names_no_board(self):
        assert detect("", f"https://{HOST}/") is None

    def test_a_posting_url_is_attributable_to_the_platform(self):
        assert board_for_url(JOB_URL) is ORACLE
        assert ORACLE.job_ref(JOB_URL) == {"host": HOST, "site": SITE, "jid": "10139"}


class TestCompanyDispatch:
    async def test_fetch_company_adapts_this_modules_rows(self, oracle_board):
        oracle_board(fixture("oracle_board.json"), fixture("oracle_job_detail.json"))
        out = await company.fetch_company({"ats": "oracle", "slug": HANDLE})
        assert len(out) == 4 and out[0]["ats"] == "oracle" and "company" not in out[0]

    def test_the_registry_seeds_it_local(self):
        assert seed_tag_for("oracle") == tags.LOCAL
