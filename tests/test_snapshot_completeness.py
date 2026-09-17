"""What each paging fetcher reports about its own snapshot, offline: a
pager that stops short of the board's end calls net.http.note_capped, and
the caller reads net.http.snapshot_info() after the fetch. What the
harvester and the crawl do with that is tested in tests/test_harvest.py and
tests/test_store.py.

Pulls are whole-board (`loc_re=None`, the harvester's call shape) unless a
test says otherwise.
"""

import re

import pytest

from conftest import fake_response
from src.ats.fetchers import company as company_fetch
from src.ats.fetchers import html_scrape as sf
from src.ats.fetchers import workday as wd
from src.net import http

NC_RE = re.compile(r"\bNC\b|North Carolina", re.I)


@pytest.fixture(autouse=True)
def fresh_accounting():
    """One fetch attempt per test, as fetch_all / harvest_board run it."""
    http.reset_fetch_failures()


# --------------------------------------------------------------------------- #
#  Workday and SmartRecruiters: one rule, two APIs                             #
# --------------------------------------------------------------------------- #

class _WDPager:
    """A Workday CXS tenant over a flat posting list. Like the live API
    (probed 2026-09-16: gilead answered total 490 at offset 0, then 0 at
    offsets 20 and 400), only the first page reports `total`."""

    def __init__(self, postings, total):
        self.postings, self.total = postings, total

    def post(self, url, json=None, **kw):
        body = json or {}
        offset, limit = body.get("offset", 0), body.get("limit", 20)
        return fake_response({"total": self.total if offset == 0 else 0,
                              "facets": [],
                              "jobPostings": self.postings[offset:offset + limit]})


class _SRPager:
    def __init__(self, content, total):
        self.content, self.total = content, total

    def get(self, url, **kw):
        offset = int(re.search(r"offset=(\d+)", url).group(1))
        return fake_response({"totalFound": self.total,
                              "content": self.content[offset:offset + 100]})


def _pull_workday(monkeypatch, n, total, scoped, max_pages):
    postings = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                 "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                for i in range(n)]
    monkeypatch.setattr(wd, "SESSION", _WDPager(postings, total))
    monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
    return wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                loc_re=NC_RE if scoped else None,
                                page_size=20, max_pages=max_pages)


def _pull_smartrecruiters(monkeypatch, n, total, scoped, max_pages):
    content = [{"id": f"r{i}", "name": "Data Engineer",
                "location": {"city": "Durham", "region": "NC", "country": "US"},
                "releasedDate": "2026-01-01T00:00:00Z"} for i in range(n)]
    monkeypatch.setattr(company_fetch, "SESSION", _SRPager(content, total))
    return company_fetch.fetch_smartrecruiters_all(
        "acme", loc_re=NC_RE if scoped else None, max_pages=max_pages)


#: ats -> (pull, rows per page)
PAGERS = {"workday": (_pull_workday, 20),
          "smartrecruiters": (_pull_smartrecruiters, 100)}


@pytest.mark.parametrize("ats", sorted(PAGERS))
class TestTotalPagedSnapshot:
    """Capped when every page up to max_pages came back full, or when an
    unscoped pull returned fewer rows than the board's own total."""

    def test_a_complete_board_is_not_capped(self, monkeypatch, ats):
        pull, page = PAGERS[ats]
        assert len(pull(monkeypatch, page // 2, page // 2, False, 10)) == page // 2
        assert not http.snapshot_info()["capped"]

    def test_fewer_rows_than_the_total_is_capped(self, monkeypatch, ats):
        """Paging stopped on a short page, but the board reports far more
        (Stryker and Labcorp on Workday, Dominos on SmartRecruiters)."""
        pull, page = PAGERS[ats]
        assert len(pull(monkeypatch, 2 * page + 5, 3400, False, 10)) == 2 * page + 5
        assert http.snapshot_info()["capped_total"] == 3400

    def test_reading_every_page_is_capped_even_if_the_total_matches(
            self, monkeypatch, ats):
        pull, page = PAGERS[ats]
        assert len(pull(monkeypatch, 3 * page, 3 * page, False, 3)) == 3 * page
        assert http.snapshot_info()["capped"]

    def test_a_scoped_pull_is_never_compared_against_the_total(
            self, monkeypatch, ats):
        """A local subset is SUPPOSED to be smaller than the board."""
        pull, _ = PAGERS[ats]
        assert len(pull(monkeypatch, 5, 500, True, 3)) == 5
        assert not http.snapshot_info()["capped"]


class TestMidWalkFailureIsCountedNotCapped:
    """A page that answers with an error after an earlier page succeeded
    must mark the snapshot INCOMPLETE (net.http.fetch_failures() > 0), not
    read as the board's own honest end and not as a cap -- snapshot_info's
    "a failure outranks a cap" rule.

    2026-09-16: both pagers trusted `r.json()` without a status check, so a
    non-2xx response whose body still parsed as JSON (a 422 from
    osv-bioventus.wd501 / vhr-unither.wd5 on Workday; the same shape would
    fool SmartRecruiters too) read as "no more postings" -- an ordinary,
    unreported end of the walk -- rather than a failure. Page 1's rows must
    still come back: a mid-walk failure is not a reason to lose what was
    already read.
    """

    def test_workday_page2_failure_keeps_page1_and_counts(self, monkeypatch):
        postings = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                     "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                    for i in range(20)]

        class _FlakyPager:
            def post(self, url, json=None, **kw):
                if (json or {}).get("offset", 0) > 0:
                    return fake_response(status=422)      # osv-bioventus.wd501's shape
                return fake_response({"total": 500, "facets": [],
                                      "jobPostings": postings})

        monkeypatch.setattr(wd, "SESSION", _FlakyPager())
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        rows = wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                    loc_re=None, page_size=20, max_pages=5)
        assert len(rows) == 20
        info = http.snapshot_info()
        assert info["incomplete"] and info["fetch_errors"] > 0
        assert not info["capped"], "a failure outranks a cap"

    def test_smartrecruiters_page2_failure_keeps_page1_and_counts(self, monkeypatch):
        content = [{"id": f"r{i}", "name": "Data Engineer",
                    "location": {"city": "Durham", "region": "NC", "country": "US"},
                    "releasedDate": "2026-01-01T00:00:00Z"} for i in range(100)]

        class _FlakyPager:
            def get(self, url, **kw):
                offset = int(re.search(r"offset=(\d+)", url).group(1))
                if offset > 0:
                    return fake_response(status=406)
                return fake_response({"totalFound": 500, "content": content})

        monkeypatch.setattr(company_fetch, "SESSION", _FlakyPager())
        rows = company_fetch.fetch_smartrecruiters_all(
            "acme", loc_re=None, max_pages=5)
        assert len(rows) == 100
        info = http.snapshot_info()
        assert info["incomplete"] and info["fetch_errors"] > 0
        assert not info["capped"]


# --------------------------------------------------------------------------- #
#  SuccessFactors: no total on some skins, repeated pages on some tenants      #
# --------------------------------------------------------------------------- #

def _sf_page_html(rows, total=None):
    """A minimal SF search page: one a.jobTitle-link per row, plus the
    standard theme's pagination label when `total` is given."""
    anchors = "".join(
        f'<a class="jobTitle-link" href="/job/Durham-NC-Title-{i}/{i}/">T{i}</a>'
        for i in rows)
    label = (f'<span class="paginationLabel">Results <b>1</b> of <b>{total}</b></span>'
             if total is not None else "")
    return f"<html><body>{label}{anchors}</body></html>"


class _SFPager:
    """Serves `pages` (row-id lists) by `startrow`; past the end it repeats
    the LAST page, as a wrapping tenant does."""

    def __init__(self, pages, total=None):
        self.pages, self.total = pages, total

    def get(self, url, **kw):
        startrow = int(re.search(r"startrow=(\d+)", url).group(1))
        idx = min(startrow // 25, len(self.pages) - 1)
        return fake_response(text=_sf_page_html(self.pages[idx], self.total))


class TestSuccessFactorsSnapshot:
    @staticmethod
    def walk(monkeypatch, pages, total, max_pages=80):
        monkeypatch.setattr(sf, "SESSION", _SFPager(pages, total))
        monkeypatch.setattr(sf.time, "sleep", lambda *a: None)
        return list(sf._sf_rows("https://careers.example.edu", "Example",
                                step=25, max_pages=max_pages))

    def test_an_empty_page_after_the_whole_total_is_not_capped(
            self, monkeypatch):
        rows = self.walk(monkeypatch, [list(range(25)), list(range(25, 50)), []], 50)
        assert len(rows) == 50
        assert not http.snapshot_info()["capped"]

    def test_an_empty_page_with_no_total_is_not_capped(self, monkeypatch):
        """A custom skin with no pagination label: the empty page is
        trusted as the board's end."""
        assert len(self.walk(monkeypatch, [list(range(25)), []], None)) == 25
        assert not http.snapshot_info()["capped"]

    def test_a_repeated_page_short_of_the_total_is_capped(self, monkeypatch):
        """Bayer's shape: every page repeats the first, 25 of 621."""
        assert len(self.walk(monkeypatch, [list(range(25))] * 10, 621)) == 25
        assert http.snapshot_info()["capped_total"] == 621

    def test_a_repeated_page_with_no_total_is_capped(self, monkeypatch):
        assert len(self.walk(monkeypatch, [list(range(25))] * 5, None)) == 25
        info = http.snapshot_info()
        assert info["capped"] and info["capped_total"] is None

    def test_reading_every_page_is_capped_even_if_the_total_matches(
            self, monkeypatch):
        pages = [list(range(p * 25, p * 25 + 25)) for p in range(4)]
        assert len(self.walk(monkeypatch, pages, 100, max_pages=4)) == 100
        assert http.snapshot_info()["capped"]
