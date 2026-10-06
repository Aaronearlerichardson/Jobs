"""Gem (its `config.BOARDS` spec): the public job-board API, one list for the
whole board, the place it builds from the posting's offices, and the
per-posting call that judges closure.

`tests/fixtures/gem_board.json` is four postings of one board's list
(ResProp Management, `resprop`) and `gem_job_post.json` one posting's own
answer, recorded live on 2026-10-06: every key as the host sent it, the two
prose blobs of each cut to a few hundred characters.
"""

import copy

import pytest

from conftest import fake_response, fixture
from src.ats.board import board_for, company
from src.ats.signatures import detect

BOARD = board_for("gem")
SLUG = "resprop"
JID = "am9icG9zdDod14nfMKbLHoHk2zS1wEih"
POSTING = f"https://jobs.gem.com/{SLUG}/{JID}"


class TestListing:
    async def test_a_row_is_a_posting_with_its_offices_body_and_date(self, serve):
        serve(fake_response(fixture("gem_board.json")))
        rows = await BOARD.listing(SLUG)
        assert [r["id"] for r in rows][1] == f"gem_{SLUG}_{JID}"
        assert rows[1]["location"] == "Charlotte, NC, United States; Nashville, United States"
        assert rows[1]["url"] == POSTING and rows[1]["posted_at"] == "2026-02-18"
        assert rows[0]["location"].count(";") == 8 and "<" not in rows[0]["description"]

    async def test_a_posting_naming_no_place_reads_as_unknown(self, serve):
        serve(fake_response(fixture("gem_board.json")))
        assert (await BOARD.listing(SLUG))[2]["location"] == "Unknown"

    async def test_a_remote_posting_is_hinted_and_placed(self, serve):
        board = copy.deepcopy(fixture("gem_board.json"))
        board[2]["location_type"] = "remote"
        serve(fake_response(board))
        rows = await BOARD.listing(SLUG)
        assert (rows[2]["location"], rows[2]["remote_hint"]) == ("Remote", "gem:location_type")
        assert "remote_hint" not in rows[1]

    async def test_a_dead_board_reads_as_nothing(self, serve):
        serve(fake_response(status=404))
        assert await company.fetch_company({"ats": "gem", "slug": "no-such-board"}) == []


class TestPosting:
    async def test_the_posting_call_names_the_body(self, serve):
        serve(fake_response(fixture("gem_job_post.json")))
        assert "ResProp Management" in await BOARD.description_for(POSTING)

    @pytest.mark.parametrize("status,want", [(200, True), (404, False)])
    async def test_a_pulled_posting_is_a_404(self, serve, status, want):
        serve(fake_response(fixture("gem_job_post.json") if status == 200 else None, status=status))
        assert (await BOARD.probe_job(POSTING, "x"))[0] is want

    def test_a_posting_url_names_its_board(self):
        assert BOARD.job_ref(POSTING) == {"slug": SLUG, "jid": JID}
        assert BOARD.job_ref("https://jobs.gem.com/the-swift-group/4123291008") == {
            "slug": "the-swift-group", "jid": "4123291008"}


class TestDetection:
    @pytest.mark.parametrize("url", [f"https://jobs.gem.com/{SLUG}", POSTING])
    def test_a_board_url_is_a_fetchable_board(self, url):
        assert detect("", url) == ("fetchable", "gem", SLUG)

    def test_the_api_host_is_no_board(self):
        assert detect("", "https://api.gem.com/job_board/v0/resprop/job_posts/") is None
