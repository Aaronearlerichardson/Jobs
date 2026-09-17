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
    offsets 20 and 400), only the first page reports `total`.
    `fail_later`: every page after the first is rejected with a 422 whose
    body still parses as JSON (osv-bioventus.wd501's shape)."""

    def __init__(self, postings, total, fail_later=False):
        self.postings, self.total, self.fail_later = postings, total, fail_later

    def post(self, url, json=None, **kw):
        body = json or {}
        offset, limit = body.get("offset", 0), body.get("limit", 20)
        if offset and self.fail_later:
            return fake_response({"errorCode": "HTTP_422"}, status=422)
        return fake_response({"total": self.total if offset == 0 else 0,
                              "facets": [],
                              "jobPostings": self.postings[offset:offset + limit]})


class _SRPager:
    """`fail_later`: as _WDPager's, with a 406."""

    def __init__(self, content, total, fail_later=False):
        self.content, self.total, self.fail_later = content, total, fail_later

    def get(self, url, **kw):
        offset = int(re.search(r"offset=(\d+)", url).group(1))
        if offset and self.fail_later:
            return fake_response({"message": "Not Acceptable"}, status=406)
        return fake_response({"totalFound": self.total,
                              "content": self.content[offset:offset + 100]})


def _pull_workday(monkeypatch, n, total, scoped, max_pages, fail_later=False):
    postings = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                 "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                for i in range(n)]
    monkeypatch.setattr(wd, "SESSION", _WDPager(postings, total, fail_later))
    monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
    return wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                loc_re=NC_RE if scoped else None,
                                page_size=20, max_pages=max_pages)


def _pull_smartrecruiters(monkeypatch, n, total, scoped, max_pages,
                          fail_later=False):
    content = [{"id": f"r{i}", "name": "Data Engineer",
                "location": {"city": "Durham", "region": "NC", "country": "US"},
                "releasedDate": "2026-01-01T00:00:00Z"} for i in range(n)]
    # fetch_smartrecruiters_all pages through net.http.get_json, which reads
    # the SESSION name bound in net.http (its own definition site), not
    # company.py's re-export of the same object -- patch it there.
    monkeypatch.setattr(http, "SESSION", _SRPager(content, total, fail_later))
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

    def test_a_failed_later_page_is_counted_not_capped(self, monkeypatch, ats):
        """A later page answering an error marks the snapshot INCOMPLETE,
        never the board's honest end and never a cap (snapshot_info's "a
        failure outranks a cap"); page 1's rows still come back.

        2026-09-16: both pagers trusted `r.json()` without a status check,
        so a non-2xx body that still parsed as JSON (a 422 from
        osv-bioventus.wd501 / vhr-unither.wd5 on Workday) read as "no more
        postings".
        """
        pull, page = PAGERS[ats]
        assert len(pull(monkeypatch, page, 500, False, 5, fail_later=True)) == page
        info = http.snapshot_info()
        assert info["incomplete"] and info["fetch_errors"] > 0
        assert not info["capped"], "a failure outranks a cap"


def test_a_whole_board_pull_never_spends_the_locations_rescue(monkeypatch, capsys):
    """loc_re=None (the harvester's shape) keeps every row regardless of
    where it sits, so a multi-site "N Locations" row must cost nothing
    beyond the listing itself: no detail GET, no budget warning, the
    listed text kept as-is (2026-09-17: every harvest pass was spending
    the 150-GET rescue budget on multi-site rows a whole-board pull never
    needed it for). `_WDPager` deliberately defines no `get`, so an
    accidental detail rescue raises AttributeError rather than passing
    quietly.
    """
    postings = [{"title": "Data Engineer", "locationsText": "3 Locations",
                "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                for i in range(5)]
    monkeypatch.setattr(wd, "SESSION", _WDPager(postings, 5))
    monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
    rows = wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                loc_re=None, page_size=20, max_pages=3)
    assert len(rows) == 5
    assert all(r["location"] == "3 Locations" for r in rows)
    assert "[!]" not in capsys.readouterr().out


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


class _OnePageSession:
    """Serves one fixed page of HTML regardless of the requested startrow."""

    def __init__(self, html):
        self.html = html

    def get(self, url, **kw):
        return fake_response(text=self.html)


class TestSuccessFactorsLocation:
    """A slug-less tenant (URLs shaped "City-Title-ST-zip", no comma, so
    _SF_LOC_SLUG_RE never matches) falls back to the row's own markup. The
    standard theme repeats title/location/date in a hidden "visible-phone"
    block and glues the posting date onto the row's flattened text, which
    used to land in the stored location as-is ("<City>, ST, US, <zip> Aug
    31, 2026 <City>, ST": 894 open rows on one tenant, 2026-09-17). The
    theme's own `.jobLocation` cell avoids both; `_clean_sf_location` is the
    backstop for a skin with no such cell (see its doctests).
    """

    @staticmethod
    def _row_html(location_cell, extra=""):
        return (
            '<html><body><table><tr class="data-row">'
            '<td class="colTitle">'
            '<a class="jobTitle-link" href="/job/Perfusionist/1430896200/">Perfusionist</a>'
            '<div class="jobdetail-phone visible-phone">'
            '<a class="jobTitle-link" href="/job/Perfusionist/1430896200/">Perfusionist</a>'
            f'<span class="jobLocation">{location_cell}</span>'
            '<span class="jobDate">Sep 17, 2026</span>'
            '</div></td>'
            f'<td class="colLocation"><span class="jobLocation">{location_cell}</span></td>'
            f'{extra}'
            '<td class="colDate"><span class="jobDate">Sep 17, 2026</span></td>'
            '</tr></table></body></html>')

    def test_the_jobLocation_cell_wins_over_the_flattened_row_text(
            self, monkeypatch):
        monkeypatch.setattr(sf, "SESSION",
                            _OnePageSession(self._row_html("Springfield, IL, US, 62701")))
        rows = list(sf._sf_rows("https://careers.example.edu", "Example",
                                step=25, max_pages=1))
        assert len(rows) == 1
        assert rows[0]["location"] == "Springfield, IL, US, 62701"

    def test_a_skin_with_no_jobLocation_cell_still_gets_a_clean_place(
            self, monkeypatch, local_addr):
        """No `.jobLocation` markup at all: falls back to location_snippet
        on the row text, run through the same date/repeat cleanup."""
        html = ('<html><body><table><tr class="data-row">'
                '<td><a class="jobTitle-link" href="/job/x/1/">Some Title</a>'
                f'<span>{local_addr} Sep 17, 2026 {local_addr}</span></td>'
                '</tr></table></body></html>')
        monkeypatch.setattr(sf, "SESSION", _OnePageSession(html))
        rows = list(sf._sf_rows("https://careers.example.com", "Example",
                                step=25, max_pages=1))
        assert len(rows) == 1
        assert rows[0]["location"] == local_addr
