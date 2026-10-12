"""Avature (its `config.BOARDS` spec): the portal's `SearchJobs` page read as
HTML, its "of N results" legend as the total, a posting's labelled fields.

`tests/fixtures/avature_*` are trimmed REAL pages recorded 2026-10-05 from
Unifi's portal (careers.unifiservice.com/careers): three result rows of its
first page and one posting's two detail sections. `avature_folders.html` is
two role folders and the legend of Epic's folder-layout portal
(epic.avature.net/Careers), recorded 2026-10-09, prose redacted.
"""

import pytest

from conftest import fake_response, fixture, no_pacing
from src.ats.board import board_for
from src.ats.signatures import detect

AVATURE = board_for("avature")
BASE = "https://careers.unifiservice.com/careers"


@pytest.fixture
def portal(monkeypatch, serve):
    no_pacing(monkeypatch)

    def reply(url, params=None, **kw):
        if "/SearchJobs" in url:
            return fake_response(text=fixture("avature_search.html") if not (params or {}).get("jobOffset")
                                 else "<html></html>")
        return fake_response(text=fixture("avature_detail.html"))
    return serve(reply)


class TestListing:
    async def test_a_result_row_is_a_title_url_and_the_tenants_place(self, portal):
        rows = await AVATURE.listing(BASE)
        assert [r["title"] for r in rows][0] == "Security Cart/Employee Screener - BOS"
        assert rows[0]["id"] == "avature_careers_unifiservice_com_13788"
        assert rows[0]["url"].endswith("/JobDetail/425-BOS-Delta-Security-Cart-Employee-Screener/13788")
        # The list names only a country: the rescue reads the city off the page.
        assert rows[0]["location"] == "Morrisville, North Carolina"

    async def test_the_legend_gives_the_board_total(self, portal):
        assert await AVATURE.probe(BASE) == (True, 720)

    async def test_a_folder_layout_portal_lists_its_role_folders(self, monkeypatch, serve):
        no_pacing(monkeypatch)
        serve(lambda url, params=None, **kw: fake_response(
            text=fixture("avature_folders.html")
            if "/SearchJobs" in url and not (params or {}).get("folderOffset")
            else "<html></html>"))
        rows = await AVATURE.listing("https://epic.avature.net/Careers")
        assert [(r["id"], r["title"]) for r in rows] == [
            ("avature_epic_avature_net_740", "Software Developer"),
            ("avature_epic_avature_net_742", "Technical Solutions Engineer")]


class TestPosting:
    async def test_the_detail_names_the_city_and_the_description(self, portal):
        status, rec, _err = await AVATURE.detail(
            {"base": BASE, "kind": "Job", "jid": "14712"})
        assert status == 200
        assert (rec["city"], rec["state"]) == ("Morrisville", "North Carolina")
        assert "Cabin Supervisor" in rec["description"]


class TestDetection:
    @pytest.mark.parametrize("text,base", [
        (f"{BASE}/SearchJobs", BASE),
        (f"{BASE}/JobDetail/Cabin-Supervisor/14712", BASE),
        ("https://epic.avature.net/Careers/FolderDetail/Verona-Wisconsin-United-States-Trainer/12",
         "https://epic.avature.net/Careers"),
        ("https://jobs.example.com/en_US/careers/SearchJobs?x=1",
         "https://jobs.example.com/en_US/careers"),
    ])
    def test_a_portal_url_is_a_board_keyed_on_its_base(self, text, base):
        assert detect(text) == ("fetchable", "avature", base)

    def test_a_page_with_no_portal_path_is_no_board(self):
        assert detect("https://example.com/careers") is None
