"""Zoho Recruit (its `config.BOARDS` spec). `zohorecruit_board.html` is a
career site (PSC Biotech) trimmed to three of its 50 postings, recorded live
on 2026-10-07, the bodies cut. `zohorecruit_job.html` is a posting page cut
to its title and one script.
"""

from conftest import fake_response, fixture
from src.ats.board import board_for
from src.net import http

ZOHO = board_for("zohorecruit")


def _full_page(n: int) -> str:
    """The board page with `n` postings in its `jobs` input."""
    jobs = ",".join(f"{{&#34;id&#34;:&#34;{i}&#34;,&#34;Posting_Title&#34;:&#34;Role {i}&#34;}}"
                    for i in range(n))
    return f'<input type="hidden" value="[{jobs}]" id="jobs">'


class TestZohoRecruit:
    async def test_a_row_is_a_posting_with_its_place_and_industry(self, serve):
        serve(fake_response(text=fixture("zohorecruit_board.html")))
        rows = await ZOHO.listing("biotech")
        assert [r["title"] for r in rows] == ["Project Engineer", "Product Manager II",
                                              "MDR Specialist"]
        assert rows[0]["id"] == "zohorecruit_biotech_474128000070267033"
        assert rows[0]["url"] == "https://biotech.zohorecruit.com/jobs/Careers/474128000070267033"
        assert rows[0]["location"] == "Boulder, Colorado, United States"
        assert rows[0]["head"].endswith("Pharma")
        assert rows[1]["location"] == "Remote" and rows[1]["remote_hint"] == "zohorecruit:remote"

    async def test_a_page_short_of_fifty_is_complete(self, serve):
        http.reset_fetch_failures()
        log = serve(fake_response(text=_full_page(49)))
        assert len(await ZOHO.listing("biotech")) == 49
        assert len(log) == 1
        assert not http.snapshot_info()["capped"]

    async def test_a_page_of_fifty_reads_as_capped(self, serve):
        http.reset_fetch_failures()
        serve(fake_response(text=_full_page(50)))
        assert len(await ZOHO.listing("biotech")) == 50
        assert http.snapshot_info()["capped"]

    def test_a_posting_url_names_its_company_and_posting(self):
        assert ZOHO.job_ref("https://biotech.zohorecruit.com/jobs/Careers/474128000070267033/Project-Engineer") == {
            "slug": "biotech", "jid": "474128000070267033"}
