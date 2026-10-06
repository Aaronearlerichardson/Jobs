"""Teamtailor (its `config.BOARDS` spec): a tenant's JSON Feed (`/jobs.json`),
the place it builds from the posting's city and country code, the cursor a
board over 100 postings follows, and closure by the posting's own page.

`tests/fixtures/teamtailor_board.json` is four postings of one tenant's feed
(Slater Consult), recorded live on 2026-10-06: every key as the host sent it,
the two prose blobs of each posting cut to a few hundred characters.
"""

import copy

import pytest

from conftest import fake_response, fixture
from src.ats.board import board_for, company
from src.ats.signatures import detect

BOARD = board_for("teamtailor")
SLUG = "slaterconsult.teamtailor.com"
POSTING = f"https://{SLUG}/jobs/5583037-automation-engineer"


class TestListing:
    async def test_a_row_is_a_posting_with_its_place_body_and_date(self, serve):
        serve(fake_response(fixture("teamtailor_board.json")))
        rows = await BOARD.listing(SLUG)
        assert [(r["id"], r["location"]) for r in rows] == [
            ("teamtailor_slaterconsult_teamtailor_com_8312455", "Holstebro, DK; Szczecin, Poland, PL"),
            ("teamtailor_slaterconsult_teamtailor_com_8305651", "Panama City, PA"),
            ("teamtailor_slaterconsult_teamtailor_com_6676516", "Morrisville, US"),
            ("teamtailor_slaterconsult_teamtailor_com_5583037", "Raleigh, US")]
        assert rows[3]["url"] == POSTING and rows[3]["posted_at"] == "2025-02-20"
        assert "<" not in rows[3]["description"] and "seeking" in rows[3]["description"]

    async def test_a_local_city_reads_as_local_without_its_state(self, serve):
        """The feed names no region: the city and country code still place it."""
        from src.match.locality import is_nc
        serve(fake_response(fixture("teamtailor_board.json")))
        rows = await BOARD.listing(SLUG)
        assert [is_nc(r["location"]) for r in rows] == [False, False, True, True]

    async def test_a_board_over_a_hundred_postings_follows_the_feed_cursor(self, serve):
        def feed(page):
            d = copy.deepcopy(fixture("teamtailor_board.json"))
            d["items"] = [dict(it, url=it["url"].replace("/jobs/", f"/jobs/9{page}")) for it in d["items"]]
            if page == 1:
                d["next_url"] = f"https://{SLUG}/jobs.json?page=2&per_page=100"
            return fake_response(d)
        log = serve({"page=2": feed(2), "": feed(1)})
        rows = await BOARD.listing(SLUG)
        assert len(rows) == 8 and len({r["id"] for r in rows}) == 8
        assert [r.url for r in log][-1].endswith("jobs.json?page=2&per_page=100")

    async def test_a_feed_naming_another_host_is_not_followed(self, serve):
        d = copy.deepcopy(fixture("teamtailor_board.json"))
        d["next_url"] = "https://elsewhere.example/jobs.json?page=2"
        log = serve(fake_response(d))
        assert len(await BOARD.listing(SLUG)) == 4 and len(log) == 1

    async def test_a_dead_board_reads_as_nothing(self, serve):
        serve(fake_response(status=404))
        assert await company.fetch_company({"ats": "teamtailor", "slug": "nobody.teamtailor.com"}) == []


class TestClosure:
    """The posting's own page: 404 once it is pulled."""

    @pytest.mark.parametrize("status,want", [(200, True), (404, False)])
    async def test_a_pulled_posting_is_a_404(self, serve, status, want):
        serve(fake_response(text="<html><body>x</body></html>" if status == 200 else "",
                            status=status))
        assert (await BOARD.probe_job(POSTING, "x"))[0] is want

    def test_a_posting_url_names_its_tenant(self):
        assert BOARD.job_ref(POSTING) == {"slug": SLUG, "jid": "5583037"}
        assert BOARD.job_ref("https://careers.example.com/jobs/5583037-automation-engineer") is None


class TestDetection:
    @pytest.mark.parametrize("url", [f"https://{SLUG}/jobs", POSTING, f"https://{SLUG}/"])
    def test_a_tenant_url_is_a_fetchable_board(self, url):
        assert detect("", url) == ("fetchable", "teamtailor", SLUG)

    @pytest.mark.parametrize("url", ["https://www.teamtailor.com/en/", "https://app.teamtailor.com/login"])
    def test_a_vendor_host_is_no_board(self, url):
        assert detect("", url) is None
