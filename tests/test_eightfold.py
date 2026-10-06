"""Eightfold (its `config.BOARDS` spec, run by src.ats.board.engine): the
tenant's company domain found by asking, the PCSX search and the older
`apply/v2` API a tenant without PCSX serves, and the posting read back.

`tests/fixtures/eightfold_*` are trimmed REAL answers recorded 2026-10-05:
`pcsx_search.json` from CACI's PCSX search, `apply_search.json` and
`apply_job.json` from Albemarle's `apply/v2` API (a tenant PCSX 403s).
"""

import pytest

from conftest import fake_response, fixture, no_pacing
from src.ats.board import board_for
from src.ats.signatures import detect

EIGHTFOLD = board_for("eightfold")


@pytest.fixture
def tenant(monkeypatch, serve):
    """Serve one tenant: `domain` is the only one its API answers to, `pcsx`
    whether it runs PCSX (else that API 403s)."""
    no_pacing(monkeypatch)

    def _install(domain="caci.com", pcsx=True):
        def reply(url, params=None, **kw):
            params = params or {}
            if params.get("domain") != domain:
                return fake_response(text="", status=404)
            if "/api/pcsx/search" in url:
                if not pcsx:
                    return fake_response({"message": "PCSX is not enabled"}, status=403)
                page = fixture("eightfold_pcsx_search.json")
                if params["start"]:
                    page["data"]["positions"] = []
                return fake_response(page)
            if "/api/apply/v2/jobs/" in url:
                return fake_response(fixture("eightfold_apply_job.json"))
            if pcsx:
                return fake_response({"message": "Not authorized for PCSX"}, status=403)
            return fake_response(fixture("eightfold_apply_search.json") if not params["start"]
                                 else {"positions": [], "count": 2})
        return serve(reply)
    return _install


class TestListing:
    async def test_a_pcsx_board_maps_title_url_location_and_remote(self, tenant):
        tenant()
        rows = await EIGHTFOLD.listing("caci.eightfold.ai")
        assert [r["title"] for r in rows] == [
            "Systems Engineer, Mid-level", "Network Engineer Intern - Summer 2027",
            "Project Manager"]
        assert rows[0]["id"] == "eightfold_caci_1443153815392"
        assert rows[0]["url"] == "https://caci.eightfold.ai/careers/job/1443153815392"
        assert rows[0]["location"] == "Sterling, VA, US; Denver, CO, US"
        assert rows[0]["posted_at"] == "2026-10-04"
        assert [bool(r.get("remote_hint")) for r in rows] == [False, False, True]

    async def test_a_tenant_without_pcsx_is_read_through_the_older_api(self, tenant):
        tenant(domain="albemarle.com", pcsx=False)
        rows = await EIGHTFOLD.listing("albemarle.eightfold.ai")
        assert [r["id"] for r in rows] == ["eightfold_albemarle_1099556324897",
                                           "eightfold_albemarle_1099556190478"]
        assert all(r["location"] and r["url"].startswith("https://albemarle.") for r in rows)

    async def test_the_domain_is_the_first_tld_that_answers(self, tenant):
        log = tenant(domain="caci.org")
        assert await EIGHTFOLD.listing("caci.eightfold.ai")
        asked = [r.params["domain"] for r in log if "/pcsx/search" in r.url]
        assert asked[:2] == ["caci.com", "caci.org"] and set(asked[2:]) == {"caci.org"}


class TestPosting:
    async def test_the_detail_reads_the_description_and_a_gone_posting_is_closed(self, tenant):
        tenant(domain="albemarle.com", pcsx=False)
        url = "https://albemarle.eightfold.ai/careers/job/1099556324897"
        assert "essential element" in await EIGHTFOLD.description_for(url)
        is_open, _why = await EIGHTFOLD.probe_job(url)
        assert is_open is True

    async def test_a_404_on_the_posting_closes_it(self, serve):
        serve(fake_response({"message": "Job with ID 1 not found"}, status=404))
        url = "https://acme.eightfold.ai/careers/job/1"
        assert (await EIGHTFOLD.probe_job(url))[0] is False


class TestDetection:
    @pytest.mark.parametrize("text,slug", [
        ("https://arcadis.eightfold.ai/careers", "arcadis.eightfold.ai"),
        ('<a href="https://caci.eightfold.ai/careers/job/1443153815392">x</a>', "caci.eightfold.ai"),
    ])
    def test_a_tenant_host_is_a_fetchable_board(self, text, slug):
        assert detect(text) == ("fetchable", "eightfold", slug)

    @pytest.mark.parametrize("host", ["www", "app", "apply", "docs", "support"])
    def test_the_vendors_own_hosts_are_no_board(self, host):
        assert detect(f"https://{host}.eightfold.ai/") is None
