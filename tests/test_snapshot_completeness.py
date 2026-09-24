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
from src import config
from src.ats.board import engine as board
from src.ats.board import company as company_fetch
from src.ats.board import board_for
from src.net import http

NC_RE = re.compile(r"\bNC\b|North Carolina", re.I)


@pytest.fixture(autouse=True)
def fresh_accounting():
    """One fetch attempt per test, as fetch_all / harvest_board run it."""
    http.reset_fetch_failures()


# --------------------------------------------------------------------------- #
#  Workday: a scoped, ceilinged offset pager (config.BOARDS["workday"])        #
# --------------------------------------------------------------------------- #

WD = {"ats": "workday", "wd_tenant": "acme", "wd_pod": 5, "wd_site": "Site",
      "active": 1, "mission_tier": "adjacent"}


def _posting(loc, path, title="Data Engineer"):
    return {"title": title, "locationsText": loc, "externalPath": path}


def _postings(n, loc="US, NC, Durham"):
    return [_posting(loc, f"/job/x/{i}") for i in range(n)]


@pytest.fixture
def cxs(serve, monkeypatch, tmp_path):
    """`serve` a Workday tenant. A listing POST pages `postings`, or
    `scoped` when it carries a facet or a search term (unless
    `ignores_scope`); page 0 alone reports a total (`totals`: the board's
    and the scoped answer's, default their lengths). A detail GET names
    `detail` as the posting's location. A hyphenated tenant id in the
    path answers 422. Location lookups are cached under tmp_path."""
    monkeypatch.setattr(board.time, "sleep", lambda s: None)
    monkeypatch.setattr(board.config, "DATA_DIR", tmp_path)

    def _install(postings, scoped=None, totals=None, ignores_scope=False,
                 detail="US, NC, Durham"):
        board_total, scoped_total = totals or (len(postings), len(scoped or []))

        def reply(url, json=None, **kw):
            if re.search(r"/cxs/[^/]*-", url):
                return fake_response({"errorCode": "HTTP_422"}, status=422)
            if json is None:
                return fake_response({"jobPostingInfo": {
                    "title": "T", "jobDescription": "<p>Build things.</p>",
                    "location": detail, "remoteType": "Remote"}})
            use = (bool(json["appliedFacets"] or json["searchText"])
                   and scoped is not None and not ignores_scope)
            rows, total = (scoped, scoped_total) if use else (postings, board_total)
            return fake_response({
                "total": total if json["offset"] == 0 else 0,
                "jobPostings": rows[json["offset"]:json["offset"] + json["limit"]],
                "facets": [{"facetParameter": "locations", "values": [
                    {"id": "nc-id", "descriptor": "North Carolina"}]}]})
        return serve(reply)
    return _install


class TestWorkdaySnapshot:
    """What a Workday pull reports about its snapshot, beyond the engine's
    pager rules (TestEnginePagers)."""

    @pytest.mark.parametrize("n,total,capped", [
        (2000, 2000, True),     # rows == the reported total, AT the ceiling
        (2000, None, True),     # no total: the rows alone reach the ceiling
        (1995, 1995, False),    # a board under the ceiling is complete
    ])
    def test_the_ceiling_caps_a_window_of_the_board(self, cxs, n, total, capped):
        """The API reports a bigger board as 2000 and serves 2000 rows, so
        at the ceiling rows and total AGREE and would read as complete.

        Notes:
            2026-09-18: Abbott and NVIDIA each fetched exactly 2000 rows at
            a reported total of 2000, untagged, and 70 and 10 live reqs
            were closed.
        """
        cxs(_postings(n), totals=(total, 0))
        assert len(board_for("workday").whole_board(WD)) == n
        assert http.snapshot_info()["capped"] is capped

    def test_a_scoped_pull_is_never_compared_against_its_total(self, cxs):
        """A scope's total counts rows the pull then drops for locality."""
        cxs(_postings(40, "US, TX, Austin"), scoped=_postings(5), totals=(40, 12))
        assert len(board_for("workday").whole_board(WD, NC_RE)) == 5
        assert not http.snapshot_info()["capped"]

    def test_a_whole_board_pull_never_spends_the_location_rescue(self, cxs, capsys):
        """loc_re=None keeps every row wherever it sits, so a multi-site
        "N Locations" row costs no detail GET and keeps its listed text.

        Notes:
            2026-09-17: every harvest pass was spending the 150-GET rescue
            budget on multi-site rows a whole-board pull never needed.
        """
        calls = cxs(_postings(5, "3 Locations"))
        rows = board_for("workday").whole_board(WD)
        assert [r["location"] for r in rows] == ["3 Locations"] * 5
        assert [c.method for c in calls] == ["POST"]
        assert "[!]" not in capsys.readouterr().out

    @pytest.mark.parametrize("company,rows", [
        (WD, 1300),                                             # reads on
        ({**WD, "active": 0, "mission_tier": "other"}, 1200),   # the spec's 60 pages
    ])
    def test_the_page_budget_widens_for_a_mission_worth_it_board(
            self, cxs, monkeypatch, company, rows):
        """config.board_max_pages: BOARD_MAX_ROWS for a board a track can
        surface, the spec's own page budget for one off-mission and
        inactive."""
        cxs(_postings(1300))
        monkeypatch.setattr(board.config, "BOARD_MAX_ROWS", 1400)
        assert len(company_fetch.fetch_company(company, None)) == rows


class TestWorkdayScope:
    """A pull scoped to a locality asks the board for it (a location facet,
    else a search term) and rescues rows whose listing names no place.

    Notes:
        2026-09-09: one board answered every scoped call with its whole
        board; the pull read 60 pages, detail-fetched 1,199 "N Locations"
        rows (531s of an 872s crawl) and kept all 1,200 as local.
    """

    def test_a_scope_the_board_ignored_keeps_listed_matches_only(self, cxs, capsys):
        rows = ([_posting("5 Locations", f"/job/US-CA-Santa-Clara/Eng_{i}")
                 for i in range(40)]
                + [_posting("US, NC, Durham", "/job/US-NC-Durham/Eng_NC1"),
                   _posting("3 Locations", "/job/US-NC-Durham/Eng_NC2")])
        calls = cxs(rows, scoped=rows[:2], ignores_scope=True)
        out = board_for("workday").whole_board(WD, NC_RE)
        assert [(j["id"], j["location"]) for j in out] == [
            ("wd_acme_Eng_NC1", "US, NC, Durham"),
            ("wd_acme_Eng_NC2", "US NC Durham (3 Locations)")]
        assert "GET" not in [c.method for c in calls], "no detail rescue"
        assert "unnarrowed" in capsys.readouterr().out

    def test_a_narrowed_scope_expands_multi_location_rows(self, cxs):
        calls = cxs(_postings(30, "5 Locations"),
                    scoped=[_posting("5 Locations", "/job/US-CA-Santa-Clara/Eng_NC")])
        out = board_for("workday").whole_board(WD, NC_RE)
        assert [j["location"] for j in out] == ["US, NC, Durham"]
        assert [c.method for c in calls].count("GET") == 1

    def test_the_rescue_has_a_per_pull_budget(self, cxs, capsys):
        """Past `rescue.cap`, a row the facet vouched for stays on its
        listed text; the budget line says so."""
        spec = config.BOARDS["workday"]
        capped = board.Board("workday", {**spec, "rescue": {**spec["rescue"], "cap": 4}})
        local = [_posting("5 Locations", f"/job/US-CA-Santa-Clara/Eng_{i}") for i in range(6)]
        calls = cxs(local + _postings(30, "US, TX, Austin"), scoped=local)
        out = capped.whole_board(WD, NC_RE)
        assert [c.method for c in calls].count("GET") == 4
        assert [j["location"] for j in out].count("5 Locations") == 2
        assert "detail budget" in capsys.readouterr().out

    def test_the_local_count_is_the_scoped_total(self, cxs):
        cxs(_postings(50, "x"), scoped=_postings(3))
        assert board_for("workday").local_count("acme|5|Site", NC_RE) == 3

    def test_an_ignored_scope_counts_listed_locations_on_a_sample(
            self, cxs, monkeypatch):
        """Never the whole board reported as local."""
        rows = (_postings(150, "US, CA, Santa Clara")
                + [_posting("US, NC, Durham", "/job/US-NC-Durham/Eng_NC")])
        cxs(rows, scoped=[], ignores_scope=True)
        monkeypatch.setattr(board.config, "LOCAL_COUNT_SAMPLE_PAGES", 2)
        assert board_for("workday").local_count("acme|5|Site", NC_RE) == 0

    def test_a_hyphenated_tenant_is_read_through_its_underscore_id(self, cxs):
        """The CXS path takes the tenant's internal id, the underscore form
        of a hyphenated host (the hyphen form 422s): tried once, then
        settled for the pull and a stored row's detail, whose remote type
        rides along as a hint."""
        calls = cxs(_postings(3))
        company = {**WD, "wd_tenant": "vhr-unither"}
        rows = board_for("workday").whole_board(company)
        job = company_fetch.hydrate_description(
            {"ats": "workday", "url": rows[0]["url"], "description": "", "location": ""},
            company)
        assert len(rows) == 3 and job["location"] == "US, NC, Durham"
        assert job["remote_hint"] == "workday:remoteType"
        assert [c.url.split("/cxs/")[1].split("/")[0] for c in calls] == [
            "vhr-unither", "vhr_unither", "vhr_unither"]


# --------------------------------------------------------------------------- #
#  The board engine's pagers (config.BOARDS listing.pager)                    #
# --------------------------------------------------------------------------- #

def _engine(kind, handle=None, url="https://x.test/list", **pager):
    """A one-field spec on the engine, paged by `kind` (the pager's other
    keys given), listing `items` at `url`."""
    spec = {"listing": {"url": url, "params": {"o": "$offset", "n": "$size"},
                        "decoder": {"kind": "json", "entries": "items"},
                        "pager": {"kind": kind, **pager},
                        "fields": {"id": {"format": "t_{id}"}, "title": "title",
                                   "url": {"format": "https://x.test/{id}"}}}}
    return board.Board("t", {**spec, **({"handle": handle} if handle else {})})


#: An overlap pager's reason (the kind is a workaround).
_SHIFTS = "rows shift between requests, 2026-09"


def _items(ids):
    return [{"id": i, "title": "Data Engineer"} for i in ids]


@pytest.fixture
def offset_board(serve, monkeypatch):
    """`serve` an offset-paged board: `pages` maps an offset to the ids
    served there, `total` is on every page; `fail_from` answers 500 at and
    after that offset."""
    monkeypatch.setattr(board.time, "sleep", lambda s: None)

    def _install(pages, total=None, fail_from=None):
        def reply(url, params=None, **kw):
            o = params["o"]
            if fail_from is not None and o >= fail_from:
                return fake_response(status=500)
            return fake_response({"total": total, "items": _items(pages.get(o, []))})
        return serve(reply)
    return _install


class TestEnginePagers:
    """What the engine's page walk reports about its snapshot, per pager
    kind: the rows a whole-board pull returns, and whether they are the
    whole board (a capped snapshot closes nothing)."""

    @pytest.mark.parametrize("pages,total,capped_total", [
        ({0: [0, 1], 2: [2, 3], 4: [4]}, 5, None),         # the total, reached
        ({0: [0, 1], 2: [2]}, 3400, 3400),                 # far short of it
    ])
    def test_the_total_decides_whether_the_walk_was_whole(
            self, offset_board, pages, total, capped_total):
        offset_board(pages, total)
        rows = _engine("offset", size=2, pages=9, total="total").listing("h", "t h")
        assert [r["id"] for r in rows] == [f"t_{i}" for o in pages for i in pages[o]]
        assert http.snapshot_info()["capped_total"] == capped_total

    def test_every_page_full_with_no_total_is_capped(self, offset_board):
        offset_board({0: [0, 1], 2: [2, 3]})
        assert len(_engine("offset", size=2, pages=2).listing("h", "t h")) == 4
        info = http.snapshot_info()
        assert info["capped"] and info["capped_total"] is None

    def test_a_short_page_short_of_the_total_reads_on(self, offset_board):
        """A server may serve fewer rows than asked (two Phenom tenants
        serve 10 whatever the size): the total, not the page, says when the
        board ends."""
        calls = offset_board({0: [0, 1], 5: [5, 6]}, total=7)
        rows = _engine("offset", size=5, pages=9, total="total").listing("h", "t h")
        assert [r["id"] for r in rows] == ["t_0", "t_1", "t_5", "t_6"]
        assert [c.params["o"] for c in calls] == [0, 5, 10]
        assert http.snapshot_info()["capped_total"] == 7

    def test_a_failed_later_page_is_counted_not_capped(self, offset_board):
        offset_board({0: [0, 1], 2: [2, 3]}, total=4, fail_from=2)
        assert len(_engine("offset", size=2, pages=9, total="total").listing("h", "t h")) == 2
        info = http.snapshot_info()
        assert info["incomplete"] and not info["capped"]

    def test_overlapping_pages_survive_a_reshuffled_order(self, offset_board):
        """The listing order is unstable between requests: a row can shift
        across a page boundary. Half-page overlap plus dedupe by id still
        collects every row once."""
        offset_board({0: [0, 1, 2, 3, 4, 5], 3: [5, 4, 8, 7, 6, 3], 6: [6, 7, 8, 9]},
                     total=10)
        rows = _engine("overlap", size=6, step=3, pages=9, total="total",
                       why=_SHIFTS).listing("h", "t h")
        assert sorted(r["id"] for r in rows) == sorted(f"t_{i}" for i in range(10))
        assert len(rows) == 10 and not http.snapshot_info()["capped"]

    def test_a_page_adding_nothing_new_ends_the_walk_capped(self, offset_board):
        calls = offset_board({0: [0, 1, 2, 3], 2: [0, 1, 2, 3]}, total=999)
        rows = _engine("overlap", size=4, step=2, pages=9, total="total",
                       why=_SHIFTS).listing("h", "t h")
        assert len(rows) == 4 and len(calls) == 2
        assert http.snapshot_info()["capped_total"] == 999

    def test_a_cursor_is_followed_verbatim(self, serve):
        """The next-page URL carries opaque keys (rebuilt by hand, it
        re-serves page 1): it is followed as served, and `has_next` ends
        the walk."""
        nxt = "https://x.test/list?op=next&fk=A"
        calls = serve({nxt: fake_response({"items": _items([2]), "more": False}),
                       "x.test/list": fake_response({"items": _items([0, 1]), "more": True,
                                                     "next": nxt})})
        rows = _engine("cursor", size=2, pages=9, next="next", has_next="more").listing("h", "t h")
        assert [r["id"] for r in rows] == ["t_0", "t_1", "t_2"]
        assert [c.params for c in calls] == [{"o": 0, "n": 2}, {}]
        assert not http.snapshot_info()["capped"]

    @pytest.mark.parametrize("nxt", ["https://x.test/list?again",
                                     "https://elsewhere.test/list?p=2"])
    def test_a_looping_or_foreign_cursor_ends_the_walk_capped(self, serve, nxt):
        """A cursor served back to the same rows, or pointing outside the
        listing's own directory (served data, not a promise), ends the
        walk; the rows are real, a missing one proves nothing."""
        calls = serve(fake_response({"items": _items([0, 1]), "more": True, "next": nxt}))
        rows = _engine("cursor", size=2, pages=9, next="next", has_next="more").listing("h", "t h")
        assert len(rows) == 2 and len(calls) == (2 if "x.test" in nxt else 1)
        assert http.snapshot_info()["capped"]

    def test_a_followed_part_is_resolved_once_per_handle(self, serve):
        """`handle.follow`: the base a board's root redirects to, asked on
        the handle's first listing and remembered."""
        calls = serve(lambda url, **kw: fake_response(
            {"items": _items([0])} if "/list" in url else None, url="https://x.test/us/en"))
        b = _engine("offset", handle={"follow": {"base": "{slug}"}}, url="{base}/list",
                    size=9, pages=1)
        assert len(b.listing("x.test")) == len(b.listing("x.test")) == 1
        assert [c.url for c in calls] == ["https://x.test", "https://x.test/us/en/list",
                                          "https://x.test/us/en/list"]

    def test_a_handle_missing_a_part_names_no_board(self, serve):
        calls = serve(fake_response({"items": _items([0])}))
        b = _engine("offset", handle={"parts": ["host", "org"]}, size=9, pages=1)
        assert b.listing("x.test", "t x.test") == [] and calls == []
        assert http.snapshot_info()["incomplete"]


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


def _sf_pages(pages, total=None):
    """A reply serving `pages` (row-id lists) by `startrow`; past the end it
    repeats the LAST page, as a wrapping tenant does."""
    def reply(url, **kw):
        startrow = int(re.search(r"startrow=(\d+)", url).group(1))
        return fake_response(text=_sf_page_html(pages[min(startrow // 25, len(pages) - 1)], total))
    return reply


class TestSuccessFactorsSnapshot:
    """The page label's total is what proves a SuccessFactors walk whole."""

    @staticmethod
    def walk(serve, monkeypatch, pages, total):
        monkeypatch.setattr(board.time, "sleep", lambda *a: None)
        calls = serve(_sf_pages(pages, total))
        return board_for("successfactors").listing("https://careers.example.edu", "t"), calls

    def test_reaching_the_labelled_total_is_not_capped(self, serve, monkeypatch):
        rows, calls = self.walk(serve, monkeypatch, [list(range(25)), list(range(25, 50))], 50)
        assert len(rows) == len(calls) * 25 == 50
        assert not http.snapshot_info()["capped"]

    def test_a_repeated_page_short_of_the_total_is_capped(self, serve, monkeypatch):
        """Bayer's shape: a page adding nothing new, 25 of 621."""
        rows, _ = self.walk(serve, monkeypatch, [list(range(25))] * 10, 621)
        assert len(rows) == 25
        assert http.snapshot_info()["capped_total"] == 621

    def test_a_repeated_page_with_no_total_is_capped(self, serve, monkeypatch):
        rows, _ = self.walk(serve, monkeypatch, [list(range(25))] * 5, None)
        info = http.snapshot_info()
        assert len(rows) == 25 and info["capped"] and info["capped_total"] is None


class TestSuccessFactorsLocation:
    """A slug-less tenant (URLs shaped "City-Title-ST-zip", no comma, so the
    "<City>,-<ST>-" slug rule never matches) falls back to the row's own
    markup. The standard theme repeats title/location/date in a hidden
    "visible-phone" block and glues the posting date onto the row's
    flattened text, which used to land in the stored location as-is
    ("<City>, ST, US, <zip> Aug 31, 2026 <City>, ST": 894 open rows on one
    tenant, 2026-09-17). The theme's own `.jobLocation` cell avoids both;
    the cut_date_tail transform is the backstop for a skin with no such
    cell (see fields._cut_date_tail's doctests).
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
        rows = board_for("successfactors").listing("https://careers.example.edu")
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
        rows = board_for("successfactors").listing("https://careers.example.com")
        assert len(rows) == 1
        assert rows[0]["location"] == local_addr


# --------------------------------------------------------------------------- #
#  Posting pages as the listing (config.BOARDS rescue "when": "always")        #
# --------------------------------------------------------------------------- #

class TestPostingPagesAsTheListing:
    """A board whose index names nothing but posting links (jazzhr): each
    row is its posting page's JSON-LD, within the rescue's per-pull budget."""

    def test_past_the_budget_the_pull_is_capped(self, serve, monkeypatch, capsys):
        """A posting left unread is no row, so the snapshot is partial."""
        monkeypatch.setattr(board.time, "sleep", lambda s: None)
        spec = config.BOARDS["jazzhr"]
        b = board.Board("jazzhr", {**spec, "rescue": {**spec["rescue"], "cap": 1}})
        posting = ('<script type="application/ld+json">{"@type": "JobPosting", '
                   '"title": "Data Engineer", "jobLocation": {"address": '
                   '{"addressLocality": "Durham", "addressRegion": "NC"}}}</script>')
        serve({"/apply/": posting, "applytojob.com/": "".join(
            f"<a href='/apply/Id{i}/Posting-{i}'>x</a>" for i in range(3))})
        rows = b.whole_board({"ats": "jazzhr", "slug": "acme"})
        assert [(r["title"], r["location"]) for r in rows] == [("Data Engineer", "Durham, NC")]
        assert http.snapshot_info()["capped_total"] == 3
        assert "detail budget (1) spent" in capsys.readouterr().out
