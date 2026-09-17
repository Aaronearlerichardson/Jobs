"""Phenom People fetcher (src/ats/fetchers/phenom.py): overlap paging that
survives an unstable listing order, the location filter, and description/
location hydration from a posting's own detail page.

`tests/fixtures/phenom_search_results.html` and
`tests/fixtures/phenom_job_detail.html` are trimmed REAL responses,
recorded live from a Phenom People careers site on 2026-09-16 (the job
count and prose cut down; the JSON field names and shape are exactly what
the site sent, under `phApp.ddo = {...}` the same way the live page embeds
it).
"""

import json
import re
from pathlib import Path

import pytest

from conftest import fake_response
from src.ats.fetchers import company, phenom
from src.discovery.resolve import sniffer
from src.net import http

FIXTURES = Path(__file__).parent / "fixtures"


def load_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _search_html(jobs, total_hits):
    payload = {"eagerLoadRefineSearch": {"status": 200, "hits": len(jobs),
               "totalHits": total_hits, "data": {"jobs": jobs}}}
    return f"<html><body><script>phApp.ddo = {json.dumps(payload)};</script></body></html>"


def _job(req_id, title="Registered Nurse",
        location="Durham, North Carolina, United States",
        posted="2026-08-11T14:53:27.000+0000"):
    return {"reqId": req_id, "jobId": req_id, "title": title,
            "location": location, "postedDate": posted}


@pytest.fixture
def phenom_pages(monkeypatch):
    """Stub `phenom.SESSION.get`: a bare GET (no `from`/`size` params)
    answers the board's locale-resolving root GET (see `_locale_base`),
    simulating the real site's own redirect; a `/search-results` GET is
    answered from `pages`, keyed by the `from` query param -- a real
    Phenom board pages this way, not by page number (see module doc).
    """
    def _install(base, pages, root_ok=True):
        calls = []

        def _get(url, params=None, headers=None, **kw):
            calls.append({"url": url, "params": dict(params or {})})
            if params and "from" in params:
                return fake_response(text=pages.get(params["from"], ""))
            r = fake_response(text="", status=200 if root_ok else 404)
            r.url = base
            return r

        monkeypatch.setattr(phenom.SESSION, "get", _get)
        return calls
    return _install


class TestListing:
    """`fetch_phenom_all`: the whole-board pull both the company-vetted
    path (fetchers/company.py) and the gated sweep (`fetch_phenom`) build
    on."""

    def test_parses_a_recorded_listing_page(self):
        """The trimmed real fixture, read straight through `_parse_ddo` +
        `_job_row` -- no network stub needed, and it pins the exact field
        names a live Phenom board sends (reqId, title, location,
        postedDate) against a REAL recorded response, not a guess."""
        ddo = phenom._parse_ddo(load_text("phenom_search_results.html"))
        ers = ddo["eagerLoadRefineSearch"]
        assert ers["totalHits"] == 906
        rows = [phenom._job_row("https://careers.example.org/us/en", j)
                for j in ers["data"]["jobs"]]
        assert rows[0]["id"] == "phenom_273419"
        assert rows[0]["title"].startswith("Registered Nurse")
        assert rows[0]["location"] == "Durham, North Carolina, United States"
        assert rows[0]["url"] == "https://careers.example.org/us/en/job/273419"
        assert rows[0]["posted_at"] == "2026-08-11"

    def test_resolves_the_locale_base_before_paging(self, phenom_pages):
        """A bare host's `/search-results` 404s into the localized
        homepage and drops the query string, so every pull must resolve
        the locale prefix from the root redirect first (see
        `_locale_base`'s doc)."""
        base = "https://careers.example.org/us/en"
        calls = phenom_pages(base, {
            0: _search_html([_job("1"), _job("2")], total_hits=2),
        })
        rows = phenom.fetch_phenom_all("careers.example.org")
        assert [r["id"] for r in rows] == ["phenom_1", "phenom_2"]
        assert calls[0]["url"] == "https://careers.example.org"
        assert calls[1]["url"] == f"{base}/search-results"

    def test_overlap_paging_survives_reshuffled_pages(self, phenom_pages):
        """Ten jobs, page_size=6 (step=3): the server's own listing order
        for an already-fetched range comes back DIFFERENT on a later
        request (the real instability this fetcher was built to survive
        -- naive sequential paging silently lost ~5% of a live 906-job
        board, see module doc). Overlap plus dedupe-by-id must still
        collect all ten with no duplicates."""
        base = "https://careers.example.org/us/en"
        all_jobs = [_job(str(i)) for i in range(10)]
        pages = {
            0: _search_html(all_jobs[0:6], total_hits=10),
            # Same [3,8] range as above, reshuffled and re-including an
            # id (3) the first page already delivered.
            3: _search_html([all_jobs[5], all_jobs[4], all_jobs[8],
                            all_jobs[7], all_jobs[6], all_jobs[3]], total_hits=10),
            6: _search_html(all_jobs[6:10], total_hits=10),
        }
        phenom_pages(base, pages)
        http.reset_fetch_failures()
        rows = phenom.fetch_phenom_all("careers.example.org", page_size=6)
        ids = [r["id"] for r in rows]
        assert sorted(ids) == [f"phenom_{i}" for i in range(10)]
        assert len(ids) == len(set(ids))  # no duplicate rows
        assert not http.snapshot_info()["capped"]   # all 10 of 10 arrived

    def test_stops_once_a_page_adds_no_new_id(self, phenom_pages):
        """A board that answers past its own (wrong/unresolved) total
        with repeats, rather than an empty page, must not loop until
        max_pages. Stopping short of the total is a capped snapshot: the
        unstable order that repeats a page is what leaves rows unseen."""
        base = "https://careers.example.org/us/en"
        jobs = [_job(str(i)) for i in range(4)]
        pages = {
            0: _search_html(jobs, total_hits=999),  # total unresolved/wrong
            2: _search_html(jobs, total_hits=999),  # nothing new
        }
        calls = phenom_pages(base, pages)
        http.reset_fetch_failures()
        rows = phenom.fetch_phenom_all("careers.example.org", page_size=4, max_pages=10)
        assert sorted(r["id"] for r in rows) == [f"phenom_{i}" for i in range(4)]
        # root + from=0 + from=2, then stopped -- never reached from=4.
        assert len(calls) == 3
        assert http.snapshot_info()["capped_total"] == 999

    def test_location_filter_applies_to_the_listed_location(self, phenom_pages):
        base = "https://careers.example.org/us/en"
        jobs = [_job("1", location="Durham, North Carolina, United States"),
                _job("2", location="Austin, Texas, United States")]
        phenom_pages(base, {0: _search_html(jobs, total_hits=2)})
        rows = phenom.fetch_phenom_all("careers.example.org",
                                       loc_re=re.compile("North Carolina"))
        assert [r["id"] for r in rows] == ["phenom_1"]

    def test_reading_every_page_up_to_max_pages_reports_capped(
            self, phenom_pages):
        """The pager ran out of pages before the board ran out of rows:
        the snapshot is capped (net.http.note_capped), with the board's
        own total."""
        base = "https://careers.example.org/us/en"
        phenom_pages(base, {
            0: _search_html([_job("0"), _job("1")], total_hits=99),
            1: _search_html([_job("1"), _job("2")], total_hits=99),
        })
        http.reset_fetch_failures()
        rows = phenom.fetch_phenom_all("careers.example.org", page_size=2,
                                       max_pages=2)
        assert [r["id"] for r in rows] == ["phenom_0", "phenom_1", "phenom_2"]
        assert http.snapshot_info()["capped_total"] == 99


class TestSweepEntry:
    """`fetch_phenom`: the gated sweep path (board.board_jobs), same rows
    the company-vetted path reads, plus the registry's keyword gate."""

    def test_gate_filters_before_hydrating(self, phenom_pages):
        base = "https://careers.example.org/us/en"
        jobs = [_job("1", title="Registered Nurse"), _job("2", title="Chef")]
        phenom_pages(base, {0: _search_html(jobs, total_hits=2)})
        out = phenom.fetch_phenom("careers.example.org", "Acme",
                                  gate=lambda t, d="": "nurse" in t.lower(),
                                  detail_delay=0)
        assert [j["id"] for j in out] == ["phenom_1"]
        assert out[0]["company"] == "Acme"


class TestCompanyDispatch:
    """fetchers/company.py wiring: FETCHERS prefers `slug` over
    `careers_url`, and hydrate_description's phenom branch fills in the
    description and a better location from the posting's own page."""

    def test_fetchers_entry_prefers_slug_over_careers_url(self, phenom_pages):
        base = "https://careers.example.org/us/en"
        phenom_pages(base, {0: _search_html([_job("1")], total_hits=1)})
        out = company.fetch_company({"ats": "phenom", "slug": "careers.example.org",
                                     "careers_url": "https://wrong.example.org"})
        assert [j["id"] for j in out] == ["phenom_1"]
        assert out[0]["ats"] == "phenom"

    def test_hydrate_description_reads_a_recorded_detail_page(self,
                                                              monkeypatch):
        monkeypatch.setattr(
            phenom.SESSION, "get",
            lambda *a, **k: fake_response(text=load_text("phenom_job_detail.html")))
        job = {"ats": "phenom", "url": "https://careers.example.org/us/en/job/273419",
               "description": "", "location": ""}
        out = company.hydrate_description(job)
        assert out["location"] == "Durham, North Carolina, United States"
        assert "Duke Health" in out["description"]
        assert "<p>" not in out["description"]          # HTML stripped
        assert "&nbsp;" not in out["description"]


def test_sniff_ats_recognizes_a_phenom_board(monkeypatch):
    """A careers page that embeds a Phenom widget endpoint resolves, through
    `sniff_ats`, to a phenom coordinate (the pattern itself is pinned in
    src.ats.signatures.detect's doctest)."""
    root = "https://www.example-health.org/careers"
    page = fake_response(text='<html><script>var ddo = {"widgetApiEndpoint":'
                              '"https://careers.example-health.org/widgets"};'
                              '</script></html>')
    page.url = root
    monkeypatch.setattr(sniffer, "candidate_pages",
                        lambda name, careers_url, **kw: iter([page]))
    assert sniffer.sniff_ats("Example Health") == {
        "ats": "phenom", "slug": "careers.example-health.org",
        "careers_url": root}
