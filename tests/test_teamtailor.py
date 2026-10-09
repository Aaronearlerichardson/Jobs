"""Teamtailor (its `config.BOARDS` spec): a tenant's JSON Feed (`/jobs.json`),
the place it builds from the posting's city and country code, the cursor a
board over 100 postings follows, and closure by the posting's own page.

`tests/fixtures/teamtailor_board.json` is four postings of one tenant's feed
(Slater Consult), recorded live on 2026-10-06: every key as the host sent it,
the two prose blobs of each posting cut to a few hundred characters.
"""

import copy

from conftest import fake_response, fixture
from src.ats.board import board_for

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

    async def test_a_local_city_reads_as_local_without_its_state(self, serve, cfg):
        """The feed names no region: the city and country code still place it."""
        from src.match.locality import is_nc
        city = next(s for s in cfg.LOCALITY_SUBSTRINGS if s not in cfg.LOCALITY_STATE_SUFFIX)
        d = copy.deepcopy(fixture("teamtailor_board.json"))
        for it in d["items"][2:]:            # Morrisville, Raleigh -> the profile's own city
            it["_jobposting"]["jobLocation"][0]["address"]["addressLocality"] = city.title()
        serve(fake_response(d))
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
