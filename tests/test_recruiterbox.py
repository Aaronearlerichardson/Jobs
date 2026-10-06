"""Recruiterbox / Trakstar Hire (its `config.BOARDS` spec): the board's
server-rendered job list, read 25 cards a page with `?p=`, and the posting
page's description block for the body (its JSON-LD holds raw line breaks in a
string, which no JSON parser takes).

`tests/fixtures/recruiterbox_board.html` is the six postings of one tenant's
list (APR, `aprco`) and `recruiterbox_job.html` one posting's description
(Planate), both trimmed REAL pages recorded 2026-10-06: every card verbatim,
the rest of the page cut to the script block that names the board's total.
"""

import pytest

from conftest import fake_response, fixture
from src.ats.board import board_for, company
from src.ats.signatures import detect

BOARD = board_for("recruiterbox")
SLUG = "aprco"
POSTING = f"https://{SLUG}.hire.trakstar.com/jobs/fk0ztte/"


def page_of(total, tag):
    """The fixture's board page as part of a board of `total` rows: its ids
    prefixed `tag`, its script block naming `total`."""
    html = fixture("recruiterbox_board.html").replace("total_results:  '6'", f"total_results:  '{total}'")
    return html.replace("/jobs/", f"/jobs/{tag}")


class TestListing:
    async def test_a_card_is_a_posting_with_its_place_and_link(self, serve):
        serve(fake_response(text=fixture("recruiterbox_board.html")))
        rows = await BOARD.listing(SLUG)
        assert [(r["id"], r["title"], r["location"]) for r in rows][:2] == [
            ("recruiterbox_aprco_fk0ztte", "CONTRACT PROJECT MANAGER", "Remote"),
            ("recruiterbox_aprco_fk0zzsa", "Events & Experiential Production Strategist",
             "United States")]
        assert rows[0]["url"] == POSTING and len(rows) == 6

    async def test_a_place_is_city_state_country_and_a_fully_remote_posting_is_hinted(self, serve):
        html = fixture("recruiterbox_board.html")
        serve(fake_response(text=html.replace(
            '<span class="meta-job-location-city cut-text ">\n                        Remote</span>',
            '<span class="meta-job-location-city cut-text ">\n                        Raleigh</span>,'
            '<span class="meta-job-location-state">North Carolina</span>,'
            '<span class="meta-job-location-country">United States</span>', 1)))
        rows = await BOARD.listing(SLUG)
        assert rows[0]["location"] == "Raleigh, North Carolina, United States"
        assert [bool(r.get("remote_hint")) for r in rows] == [True, True, True, False, False, False]

    async def test_a_board_longer_than_a_page_is_walked_by_its_page_number(self, serve):
        log = serve(lambda url, params=None, **kw: fake_response(
            text=page_of(12, f"p{(params or {}).get('p')}")))
        rows = await BOARD.listing(SLUG)
        assert len(rows) == 12 and len({r["id"] for r in rows}) == 12
        assert [r.params["p"] for r in log] == [1, 2]

    async def test_a_dead_board_reads_as_nothing(self, serve):
        serve(fake_response(status=404))
        assert await company.fetch_company({"ats": "recruiterbox", "slug": "no-such-board"}) == []


class TestPosting:
    async def test_the_page_gives_the_body_as_text(self, serve):
        serve(fake_response(text=fixture("recruiterbox_job.html")))
        job = {"ats": "recruiterbox", "location": "Grand Junction, Colorado, United States",
               "description": "", "url": "https://planate.hire.trakstar.com/jobs/fk0z8au/"}
        out = await company.hydrate_description(job)
        assert "Service-Disabled Veteran-Owned" in out["description"]
        assert "<" not in out["description"] and "&lt;" not in out["description"]

    def test_a_posting_url_names_its_board_on_either_host(self):
        assert BOARD.job_ref(POSTING) == {"slug": SLUG, "jid": "fk0ztte"}
        assert BOARD.job_ref("https://aprco.recruiterbox.com/jobs/fk0ztte/") == {"slug": SLUG,
                                                                                "jid": "fk0ztte"}

    @pytest.mark.parametrize("status,want", [(200, True), (404, False)])
    async def test_a_pulled_posting_is_a_404(self, serve, status, want):
        serve(fake_response(text=fixture("recruiterbox_job.html") if status == 200 else "",
                            status=status))
        assert (await BOARD.probe_job(POSTING, "x"))[0] is want


class TestDetection:
    @pytest.mark.parametrize("url", [
        "https://aprco.hire.trakstar.com/",
        POSTING,
        "https://aprco.recruiterbox.com/jobs/fk0ztte/",
    ])
    def test_a_tenant_url_is_a_fetchable_board(self, url):
        assert detect("", url) == ("fetchable", "recruiterbox", SLUG)

    @pytest.mark.parametrize("url", ["https://www.recruiterbox.com/", "https://app.hire.trakstar.com/"])
    def test_a_vendor_host_is_no_board(self, url):
        assert detect("", url) is None
