"""UKG Pro (UltiPro): which host serves a board, offline.

The detector accepts boards on both `recruiting2.` and `recruiting.`, and the
stored slug (`CODE|GUID`) drops the host. A tenant answers only on its own
host, so the spec's `handle.try` asks recruiting2 first, falls back to
recruiting on a 404, and remembers the winner (the engine's `handle.try`
mechanism, in src.ats.fetchers.board, with UKG Pro its first user).
"""

import pytest

from conftest import fake_response
from src.ats.fetchers import board
from src.ats.fetchers.board import board_for
from src.net import http

SLUG = "ACME1000|ad28382f-2fcd-4cbb-bb18-24dd71b05bce"
UKG = board_for("ultipro")


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


@pytest.fixture
def tenant(monkeypatch, serve):
    """Serve a tenant; the engine's page pause is skipped."""
    monkeypatch.setattr(board.time, "sleep", lambda s: None)

    def _install(host, opps=None, **kw):
        t = _Tenant(host, [_opp(1), _opp(2)] if opps is None else opps, **kw)
        serve(t.post)
        http.reset_fetch_failures()
        return t
    return _install


class TestHostFallback:
    def test_a_recruiting2_board_is_fetched_in_one_request(self, tenant):
        t = tenant("recruiting2")
        jobs = UKG.jobs(SLUG, "Acme")
        assert [j["title"] for j in jobs] == ["Data Engineer 1", "Data Engineer 2"]
        assert t.hosts_asked() == ["recruiting2"]
        assert http.fetch_failures() == 0

    def test_a_recruiting_only_board_falls_back_on_a_404(self, tenant):
        t = tenant("recruiting")
        job = UKG.jobs(SLUG, "Acme")[0]
        assert t.hosts_asked() == ["recruiting2", "recruiting"]
        assert http.fetch_failures() == 0
        # URLs follow the host that answered; the id never carried the host,
        # so existing rows keep matching.
        assert job["url"] == (
            "https://recruiting.ultipro.com/ACME1000/JobBoard/"
            "ad28382f-2fcd-4cbb-bb18-24dd71b05bce/OpportunityDetail"
            "?opportunityId=0001aaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert job["id"] == "ultipro_ACME1000_0001aaaa-bbb"

    def test_the_working_host_is_remembered_and_paging_stays_on_it(self, tenant):
        t = tenant("recruiting", [_opp(n) for n in range(101)])
        assert len(UKG.jobs(SLUG, "Acme")) == 101
        assert t.hosts_asked() == ["recruiting2", "recruiting", "recruiting"]
        t.urls.clear()
        UKG.jobs(SLUG, "Acme")
        assert t.hosts_asked() == ["recruiting", "recruiting"], "no 404 round trip"

    def test_a_later_page_404_is_an_error_not_a_host_switch(self, tenant):
        t = tenant("recruiting2", [_opp(n) for n in range(101)], fail_from_page=1)
        assert len(UKG.jobs(SLUG, "Acme")) == 100
        assert t.hosts_asked() == ["recruiting2", "recruiting2"]
        assert http.snapshot_info()["incomplete"]

    def test_a_non_404_error_does_not_try_the_other_host(self, tenant):
        t = tenant("recruiting2", status=500)
        assert UKG.jobs(SLUG, "Acme") == []
        assert t.hosts_asked() == ["recruiting2"]
        assert http.fetch_failures() == 1
        # An error settles no host: the board is found once it answers.
        t.host, t.status = "recruiting", 200
        assert len(UKG.jobs(SLUG, "Acme")) == 2

    def test_a_board_on_neither_host_is_dead_and_not_remembered(self, tenant):
        t = tenant("elsewhere")
        assert UKG.jobs(SLUG, "Acme") == []
        assert http.fetch_failures() == 1
        assert UKG.alive(SLUG) == (False, 0)
        assert t.hosts_asked() == ["recruiting2", "recruiting"] * 2

    def test_an_empty_listing_is_a_live_board(self, tenant):
        tenant("recruiting", [])
        assert UKG.alive(SLUG) == (True, 0)
        assert http.fetch_failures() == 0
