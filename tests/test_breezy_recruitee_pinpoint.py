"""Breezy, Recruitee and Pinpoint (their `config.BOARDS` specs, run by
src.ats.board.engine): the one-request JSON listings, the location each
builds, closure by board membership, Breezy's JSON-LD detail, and the board
detection that promoted them out of the detection-only lead bucket.

`tests/fixtures/{breezy,recruitee,pinpoint}_board.json` and
`breezy_post.html` are trimmed REAL responses recorded live on 2026-10-05
(Highlights Healthcare, Hudson Manpower, Trilon Group): whole entries, every
key as the host sent it, long prose cut to a few hundred characters.
"""

import pytest

from conftest import fake_response, fixture
from src.ats.board import board_for, company
from src.ats.signatures import detect

#: platform -> (handle, a stored posting URL on the fixture's board)
BOARDS = {
    "breezy": ("highlights-healthcare",
               "https://highlights-healthcare.breezy.hr/p/9b5dbab30b78-bilingual-board-certified"),
    "recruitee": ("hudsonmanpower",
                  "https://hudsonmanpower.recruitee.com/o/head-of-recruitment-operations"),
    "pinpoint": ("trilongroup",
                 "https://trilongroup.pinpointhq.com/en/postings/6547427b-f71c-4ab8-9023-8b519a110616"),
}


@pytest.fixture(params=sorted(BOARDS))
def platform(request):
    return request.param


class TestListing:
    """Each listing is one request; the rows carry a body, a place and a link."""

    async def test_rows_carry_title_url_location_and_id(self, serve, platform):
        handle = BOARDS[platform][0]
        serve({"/p/": fake_response(text=fixture("breezy_post.html")),
               "": fake_response(fixture(f"{platform}_board.json"))})
        rows = await company.fetch_company({"ats": platform, "slug": handle})
        assert rows and all(r["title"] and r["url"] and r["location"] and r["description"]
                            and r["id"].startswith(f"{platform}_{handle}_") for r in rows)

    async def test_breezy_reads_the_remote_flag_and_every_location(self, serve):
        serve(fake_response(fixture("breezy_board.json")))
        rows = await board_for("breezy").listing("highlights-healthcare")
        assert [r["location"] for r in rows] == ["Raleigh, NC", "Mooresville, NC", "Durham, NC",
                                                 "San Diego, CA"]
        assert [r.get("remote_hint") for r in rows] == [None, "breezy:is_remote", None, None]

    async def test_recruitee_lists_every_site_of_a_multi_location_offer(self, serve):
        serve(fake_response(fixture("recruitee_board.json")))
        rows = await board_for("recruitee").listing("hudsonmanpower")
        assert rows[0]["location"].count(";") == 2 and "Virginia" in rows[0]["location"]
        assert rows[1]["remote_hint"] == "recruitee:remote"
        assert "<" not in rows[0]["description"]

    async def test_pinpoint_completes_a_bare_city_with_its_province(self, serve):
        serve(fake_response(fixture("pinpoint_board.json")))
        rows = await board_for("pinpoint").listing("trilongroup")
        assert [r["location"] for r in rows] == ["Wilmington, NC", "Remote- USA", "Neptune, New Jersey"]
        assert rows[1]["remote_hint"] == "pinpoint:workplace_type"
        assert "<" not in rows[0]["description"]

    async def test_a_dead_board_reads_as_nothing(self, serve, platform):
        serve(fake_response(status=404))
        assert await company.fetch_company({"ats": platform, "slug": "no-such-board"}) == []


class TestBreezyDetail:
    async def test_the_posting_page_json_ld_names_the_body(self, serve):
        serve(fake_response(text=fixture("breezy_post.html")))
        job = {"ats": "breezy", "location": "Raleigh, NC", "description": "",
               "url": BOARDS["breezy"][1]}
        out = await company.hydrate_description(job)
        assert "Highlights Healthcare ABA is now hiring" in out["description"]
        assert out["location"] == "Raleigh, NC"


class TestClosure:
    """A pulled posting leaves the board's listing: that is the verdict."""

    @pytest.mark.parametrize("listed,want", [(True, True), (False, False)])
    async def test_membership_of_the_board_listing(self, serve, platform, listed, want):
        handle, url = BOARDS[platform]
        board = board_for(platform)
        serve(fake_response(fixture(f"{platform}_board.json")))
        row_id = (await board.listing(handle))[0]["id"]
        # Breezy's URL names the posting; the others are judged on the row id.
        is_open, _why = await board.probe_job(
            url if listed else url.replace("9b5dbab30b78", "0" * 12),
            row_id if listed else f"{platform}_gone")
        assert is_open is want


class TestDetection:
    @pytest.mark.parametrize("url,ats,slug", [
        ("https://highlights-healthcare.breezy.hr/", "breezy", "highlights-healthcare"),
        ("https://highlights-healthcare.breezy.hr/p/9b5dbab30b78-x", "breezy", "highlights-healthcare"),
        ("https://hudsonmanpower.recruitee.com/o/head-of-recruitment-operations", "recruitee",
         "hudsonmanpower"),
        ("https://trilongroup.pinpointhq.com/en/postings/6547427b-f71c", "pinpoint", "trilongroup"),
    ])
    def test_a_board_url_resolves_to_its_subdomain(self, url, ats, slug):
        assert detect("", url) == ("fetchable", ats, slug)

    @pytest.mark.parametrize("url", ["https://app.breezy.hr/signin", "https://www.recruitee.com/",
                                     "https://developers.pinpointhq.com/docs"])
    def test_a_vendor_host_is_no_board(self, url):
        assert detect("", url) is None
