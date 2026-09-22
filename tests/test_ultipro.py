"""UKG Pro (UltiPro) fetcher: which host serves a board, offline.

The detector accepts boards on both `recruiting2.` and `recruiting.`, and the
stored slug (`CODE|GUID`) drops the host. The fetcher hardcoded recruiting2,
so a board served from recruiting was detected, stored and then 404'd on every
pass: zero jobs, indistinguishable from an empty board. A tenant answers only
on its own host, so the fetcher tries recruiting2 first and falls back to
recruiting on a 404.
"""

import types

import pytest

from conftest import fake_response
from src.ats.fetchers import ultipro
from src.net import http

SLUG = "ACME1000|ad28382f-2fcd-4cbb-bb18-24dd71b05bce"


def _opp(n):
    return {"Id": f"{n:04d}aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "Title": f"Data Engineer {n}",
            "Locations": [{"Address": {"City": "Durham", "State": {"Code": "NC"}}}],
            "BriefDescription": f"<p>Build pipelines {n}.</p>"}


class _Tenant:
    """A UKG Pro tenant: answers the search API on `host` only, 404 anywhere
    else (or `status` on its own host), and records every URL it was asked."""

    def __init__(self, host, opps, status=200, fail_from_page=None):
        self.host, self.opps, self.status = host, opps, status
        self.fail_from_page = fail_from_page
        self.urls = []

    def post(self, url, json=None, **kw):
        self.urls.append(url)
        if f"//{self.host}.ultipro.com/" not in url:
            return fake_response(text="", status=404)
        search = json["opportunitySearch"]
        if self.fail_from_page is not None and \
                search["Skip"] >= self.fail_from_page * search["Top"]:
            return fake_response(text="", status=404)
        if self.status != 200:
            return fake_response(text="", status=self.status)
        page = self.opps[search["Skip"]:search["Skip"] + search["Top"]]
        return fake_response({"opportunities": page, "totalCount": len(self.opps)})

    def hosts_asked(self):
        return [u.split("//")[1].split(".")[0] for u in self.urls]

    # requests.Session() is used as a context manager
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def tenant(monkeypatch):
    """Install a tenant behind `requests.Session()` and forget which hosts
    earlier tests learned (the per-slug memory is process-wide)."""
    monkeypatch.setattr(ultipro, "_HOST_OF", {})
    monkeypatch.setattr(ultipro.time, "sleep", lambda s: None)

    def _install(host, opps=None, **kw):
        t = _Tenant(host, [_opp(1), _opp(2)] if opps is None else opps, **kw)
        monkeypatch.setattr(ultipro, "requests",
                            types.SimpleNamespace(Session=lambda: t))
        http.reset_fetch_failures()
        return t
    return _install


class TestHostFallback:
    def test_a_recruiting2_board_is_fetched_in_one_request(self, tenant):
        t = tenant("recruiting2")
        jobs = ultipro.fetch_ultipro(SLUG, "Acme")
        assert [j["title"] for j in jobs] == ["Data Engineer 1", "Data Engineer 2"]
        assert t.hosts_asked() == ["recruiting2"]
        assert all(j["url"].startswith(
            "https://recruiting2.ultipro.com/ACME1000/JobBoard/") for j in jobs)
        assert http.fetch_failures() == 0

    def test_a_recruiting_only_board_falls_back_on_a_404(self, tenant):
        t = tenant("recruiting")
        jobs = ultipro.fetch_ultipro(SLUG, "Acme")
        assert len(jobs) == 2
        assert t.hosts_asked() == ["recruiting2", "recruiting"]
        assert http.fetch_failures() == 0

    def test_job_urls_and_ids_follow_the_host_that_answered(self, tenant):
        tenant("recruiting")
        job = ultipro.fetch_ultipro(SLUG, "Acme")[0]
        assert job["url"] == (
            "https://recruiting.ultipro.com/ACME1000/JobBoard/"
            "ad28382f-2fcd-4cbb-bb18-24dd71b05bce/OpportunityDetail"
            "?opportunityId=0001aaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        # The id never carried the host, so existing rows keep matching.
        assert job["id"] == "ultipro_ACME1000_0001aaaa-bbb"

    def test_the_working_host_is_remembered_for_the_slug(self, tenant):
        t = tenant("recruiting")
        ultipro.fetch_ultipro(SLUG, "Acme")
        t.urls.clear()
        ultipro.fetch_ultipro(SLUG, "Acme")
        assert t.hosts_asked() == ["recruiting"], "no 404 round trip the second time"

    def test_paging_stays_on_the_host_that_answered(self, tenant):
        t = tenant("recruiting", [_opp(1), _opp(2), _opp(3)])
        assert len(ultipro.parse_board(SLUG, page_size=2)) == 3
        assert t.hosts_asked() == ["recruiting2", "recruiting", "recruiting"]

    def test_a_later_page_404_is_an_error_not_a_host_switch(self, tenant):
        t = tenant("recruiting2", [_opp(1), _opp(2), _opp(3)], fail_from_page=1)
        with pytest.raises(Exception):
            ultipro.parse_board(SLUG, page_size=2)
        assert t.hosts_asked() == ["recruiting2", "recruiting2"]

    def test_a_non_404_error_does_not_try_the_other_host(self, tenant):
        t = tenant("recruiting2", status=500)
        assert ultipro.fetch_ultipro(SLUG, "Acme") == []
        assert t.hosts_asked() == ["recruiting2"]
        assert http.fetch_failures() == 1

    def test_a_board_on_neither_host_is_an_empty_board_with_an_error(
            self, tenant, capsys):
        t = tenant("elsewhere")
        assert ultipro.fetch_ultipro(SLUG, "Acme") == []
        assert t.hosts_asked() == ["recruiting2", "recruiting"]
        assert http.fetch_failures() == 1
        assert "UltiPro Acme" in http.snapshot_info()["last_error"]
        assert "[!] UltiPro Acme" in capsys.readouterr().out
        assert SLUG not in ultipro._HOST_OF

    def test_parse_board_still_raises_when_both_hosts_404(self, tenant):
        """prune_dead_boards and the discovery probe read a raise as
        'the board request fails'; an empty listing is a live board."""
        tenant("elsewhere")
        with pytest.raises(Exception):
            ultipro.parse_board(SLUG)

    def test_an_empty_listing_is_a_live_board(self, tenant):
        tenant("recruiting", [])
        assert ultipro.parse_board(SLUG) == []
        assert http.fetch_failures() == 0
