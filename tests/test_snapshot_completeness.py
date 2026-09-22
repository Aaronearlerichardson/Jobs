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
    body still parses as JSON (osv-bioventus.wd501's shape).

    `pages` serves EXPLICIT pages (a list of posting lists) by
    offset // limit instead of slicing one flat list, repeating the LAST
    page once the walk runs past the end -- Workday's own posting order is
    unstable enough that a page can hand back postings an EARLIER page
    already returned (2026-09-18: that duplication, un-deduped, is why
    live boards showed MORE rows than their own declared total, e.g. "ICON
    plc: 1200 job(s) ... capped of 840"). One pager with that parameter
    rather than a second class: only the line that picks the page differs.
    """

    def __init__(self, postings=None, total=None, fail_later=False,
                 pages=None):
        self.postings = postings or []
        self.total, self.fail_later, self.pages = total, fail_later, pages

    def _page(self, offset, limit):
        if self.pages is None:
            return self.postings[offset:offset + limit]
        return self.pages[min(offset // limit, len(self.pages) - 1)]

    def post(self, url, json=None, **kw):
        body = json or {}
        offset, limit = body.get("offset", 0), body.get("limit", 20)
        if offset and self.fail_later:
            return fake_response({"errorCode": "HTTP_422"}, status=422)
        return fake_response({"total": self.total if offset == 0 else 0,
                              "facets": [],
                              "jobPostings": self._page(offset, limit)})


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


class TestWorkdayDedupe:
    """fetch_workday_all dedupes by posting id WITHIN one pull, the same
    shape as fetchers/phenom.py's fetch_phenom_all: a seen-id set, and a
    page that contributes no new id ends the walk early rather than
    counting as a page toward the cap."""

    def test_a_fully_repeated_page_stops_the_walk_without_capping(
            self, monkeypatch):
        page0 = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                  "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                 for i in range(20)]
        # Every page after the first hands back the SAME 20 postings.
        monkeypatch.setattr(wd, "SESSION",
                            _WDPager(total=20, pages=[page0, page0, page0]))
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        rows = wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                    loc_re=None, page_size=20, max_pages=10)
        assert len(rows) == 20, "deduped: the repeated page added nothing new"
        assert not http.snapshot_info()["capped"], (
            "a page that repeats what an earlier one already returned IS "
            "the board's honest end this pager can detect, not a cap")

    def test_a_partially_repeated_page_only_keeps_the_new_rows(
            self, monkeypatch):
        """A page can mix genuinely new postings with ones an earlier page
        already served (a partial shuffle, not a full wrap-around): only
        the new ones are kept, and the walk keeps going past it."""
        page0 = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                  "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                 for i in range(20)]
        page1 = page0[:10] + [
            {"title": "Data Engineer", "locationsText": "US, NC, Durham",
             "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
            for i in range(20, 30)]
        monkeypatch.setattr(wd, "SESSION",
                            _WDPager(total=30, pages=[page0, page1]))
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        rows = wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                    loc_re=None, page_size=20, max_pages=10)
        assert len(rows) == 30, "20 from page 0, 10 NEW ones from page 1"
        assert not http.snapshot_info()["capped"]


def test_capped_total_is_never_smaller_than_the_rows_returned(monkeypatch):
    """2026-09-18: several live boards showed a capped snapshot SMALLER
    than the number of rows they had just handed back -- "ICON plc: 1200
    job(s) ... capped of 840", "Blue Cross Blue Shield: 1200 ... capped of
    40" -- because the un-deduped pager's row count and Workday's own
    page-0 `total` disagreed and the total was trusted outright.
    capped_total must never contradict what the caller can see with its
    own eyes: it is now the LARGER of the two, never Workday's `total`
    alone.
    """
    postings = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                for i in range(60)]
    # The board's own reported total (50) UNDERCOUNTS the 60 distinct
    # postings three full pages actually return.
    monkeypatch.setattr(wd, "SESSION", _WDPager(postings, 50))
    monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
    rows = wd.fetch_workday_all("acme", 5, "Site", search_text="",
                                loc_re=None, page_size=20, max_pages=3)
    assert len(rows) == 60
    info = http.snapshot_info()
    assert info["capped"]
    assert info["capped_total"] == 60, (
        "capped_total must never be smaller than the rows just returned")


class TestWorkdayTotalCeiling:
    """Workday caps its OWN reported `total`, and what it will serve, at
    wd.WD_TOTAL_CEILING. At the ceiling the rows returned and the reported
    total AGREE, so the "fewer rows than the total" rule reads the pull as
    complete and the board diff closes everything that sat past it --
    2026-09-18: Abbott and NVIDIA each fetched exactly 2000 rows at a
    reported total of exactly 2000, neither was tagged capped, and 70 and 10
    live reqs were closed. A board UNDER the ceiling is genuinely complete
    and must stay uncapped, or nothing on it would ever close again.
    """

    def _pages_for(self, n):
        return n // 20 + 5

    def test_a_board_at_the_ceiling_is_capped(self, monkeypatch):
        n = wd.WD_TOTAL_CEILING
        rows = _pull_workday(monkeypatch, n, n, False, self._pages_for(n))
        assert len(rows) == n
        info = http.snapshot_info()
        assert info["capped"], "rows == the reported total AT the ceiling"
        assert info["capped_total"] == n

    def test_the_ceiling_is_read_off_the_rows_when_no_total_is_reported(
            self, monkeypatch):
        """The row count alone reaches the ceiling, so the pull caps itself
        even though page 0 declared no usable total."""
        n = wd.WD_TOTAL_CEILING
        rows = _pull_workday(monkeypatch, n, None, False, self._pages_for(n))
        assert len(rows) == n
        info = http.snapshot_info()
        assert info["capped"]
        assert info["capped_total"] == n, "falls back to the rows returned"

    def test_a_board_just_under_the_ceiling_is_not_capped(self, monkeypatch):
        n = wd.WD_TOTAL_CEILING - 5
        rows = _pull_workday(monkeypatch, n, n, False, self._pages_for(n))
        assert len(rows) == n
        assert not http.snapshot_info()["capped"], (
            "a genuinely complete board must keep closing its vanished rows")

    def test_a_small_board_is_untouched_by_the_rule(self, monkeypatch):
        assert len(_pull_workday(monkeypatch, 12, 12, False, 5)) == 12
        assert not http.snapshot_info()["capped"]


class TestPageBudget:
    """The Workday/SmartRecruiters page cap is a [policy] setting
    (config.BOARD_MAX_ROWS) applied through config.board_max_pages, which
    is gated by the SAME off-mission/inactive predicate the harvester's
    own long-interval cadence uses (config.is_offmission_inactive) -- see
    fetchers/company.py's FETCHERS['workday']/['smartrecruiters']."""

    def _wd_company(self, **extra):
        return {"ats": "workday", "wd_tenant": "acme", "wd_pod": 5,
                "wd_site": "Site", "active": 1, "mission_tier": "adjacent",
                **extra}

    def test_a_mission_worth_it_board_reads_past_the_narrow_default(
            self, monkeypatch):
        postings = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                    "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                    for i in range(1300)]     # > the narrow 60-page/1200-row cap
        monkeypatch.setattr(wd, "SESSION", _WDPager(postings, 1300))
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        monkeypatch.setattr(company_fetch.config, "BOARD_MAX_ROWS", 1400)
        rows = company_fetch.fetch_company(self._wd_company(), None)
        assert len(rows) == 1300, "the wider budget read past the old 1,200-row cap"

    def test_an_offmission_inactive_board_keeps_the_narrow_default(
            self, monkeypatch):
        postings = [{"title": "Data Engineer", "locationsText": "US, NC, Durham",
                    "externalPath": f"/job/x/{i}", "postedOn": "Posted Today"}
                    for i in range(1300)]
        monkeypatch.setattr(wd, "SESSION", _WDPager(postings, 1300))
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        monkeypatch.setattr(company_fetch.config, "BOARD_MAX_ROWS", 1400)
        company = self._wd_company(active=0, mission_tier="other")
        rows = company_fetch.fetch_company(company, None)
        assert len(rows) == 1200, (
            "a board a track's own mission gate discards anyway keeps the "
            "narrower, pre-2026-09-18 read")


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
            self, serve):
        serve(self._row_html("Springfield, IL, US, 62701"))
        rows = list(sf._sf_rows("https://careers.example.edu", "Example",
                                step=25, max_pages=1))
        assert len(rows) == 1
        assert rows[0]["location"] == "Springfield, IL, US, 62701"

    def test_a_skin_with_no_jobLocation_cell_still_gets_a_clean_place(
            self, serve, local_addr):
        """No `.jobLocation` markup at all: falls back to location_snippet
        on the row text, run through the same date/repeat cleanup."""
        html = ('<html><body><table><tr class="data-row">'
                '<td><a class="jobTitle-link" href="/job/x/1/">Some Title</a>'
                f'<span>{local_addr} Sep 17, 2026 {local_addr}</span></td>'
                '</tr></table></body></html>')
        serve(html)
        rows = list(sf._sf_rows("https://careers.example.com", "Example",
                                step=25, max_pages=1))
        assert len(rows) == 1
        assert rows[0]["location"] == local_addr
