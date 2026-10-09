"""The listing walk: one listing read page by page, the rows it gives
deduplicated, the stop rule, and the capped-snapshot bookkeeping.

A listing's pager (a `spec.Pager`: offset, overlap, page or cursor) says
how pages step; `walk` reads them through the caller's request and row
callbacks, so it knows nothing of handles, headers or decoders.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence

from src import config
from src.net import http
from src.net.http import note_capped
from src.net.util import JSON
from . import fields
from .spec import CursorPager, EngineRow, Listing, Pager, Vals


def page_vals(pager: Pager | None, n: int, size: int) -> Vals:
    """The named request values for page `n` (from 0) of `size` rows, a
    listing without a pager reading page 0.

    >>> from .spec import OffsetPager, OverlapPager, PagePager
    >>> page_vals(None, 0, 1)
    {'$size': 1, '$offset': 0, '$page': 0}
    >>> page_vals(PagePager(kind="page", start=1), 2, 100)
    {'$size': 100, '$offset': 200, '$page': 3}
    >>> page_vals(OffsetPager(kind="offset", size=50, start=1), 1, 50)["$offset"]
    51
    >>> page_vals(OverlapPager(kind="overlap", size=500, step=250), 1, 500)["$offset"]
    250
    >>> page_vals(PagePager(kind="page", bare_first=True), 0, 0)["$page"] is None
    True
    """
    if pager is None:
        return {"$size": size, "$offset": n * size, "$page": n}
    return {"$size": size, "$offset": pager.offset(n, size), "$page": pager.page(n)}


def page_size(pager: Pager | None) -> int:
    """Rows a pager asks per page; 0 when unknown or unpaged."""
    return (pager.size or 0) if pager else 0


def page_cap(pager: Pager, budget: int | None, step: int | None) -> int:
    """The most pages a walk stepping `step` rows reads: the pager's
    `pages`, widened to cover the row budget `budget` where one is given."""
    return max(pager.pages, math.ceil(budget / step)) if budget and step else pager.pages


def total_of(pager: Pager | None, payload: JSON) -> int | None:
    """The int total a pager's `total` names on `payload`, else None."""
    t = fields.value(pager.total, payload) if pager and pager.total else None
    return t if isinstance(t, int) else None


def _ids(rows: Sequence[EngineRow]) -> list[str]:
    """The ids of the rows that have one, in order."""
    return [r["id"] for r in rows if r["id"] is not None]


def postings(rows: list[EngineRow] | None) -> bool:
    """Whether `rows` hold a posting: a row with an id."""
    return bool(_ids(rows or []))


#: The row fields whose fill rate says the spec maps them, and the share of
#: rows each must fill by default (a canary's `min_fill` overrides).
FILL_FLOORS = {"title": 0.98, "url": 0.98, "location": 0.5, "description": 0.0,
               "posted_at": 0.0, "remote_hint": 0.0}


def fill_rates(rows: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """Per `FILL_FLOORS` field, the share of `rows` that fill it (1.0 on no rows).

    >>> fill_rates([{"title": "A", "url": "u"}, {"title": "", "url": "v", "posted_at": "x"}])
    {'title': 0.5, 'url': 1.0, 'location': 0.0, 'description': 0.0, 'posted_at': 0.5, 'remote_hint': 0.0}
    >>> fill_rates([])["title"]
    1.0
    """
    return {k: sum(bool(r.get(k)) for r in rows) / len(rows) if rows else 1.0
            for k in FILL_FLOORS}


def fill_misses(rows: Sequence[Mapping[str, object]], floors: Mapping[str, float] | None = None) -> list[str]:
    """The fields of `rows` filled under their floor (`FILL_FLOORS` over `floors`),
    each "field 40% < 98%".

    >>> fill_misses([{"title": "A", "url": ""}], {"location": 0})
    ['url 0% < 98%']
    >>> fill_misses([{"title": "A", "url": "u", "location": "x"}])
    []
    """
    floor = FILL_FLOORS | dict(floors or {})
    return [f"{k} {v:.0%} < {floor[k]:.0%}" for k, v in fill_rates(rows).items() if v < floor[k]]


def ended(pager: Pager, payload: JSON, number: int, rows: list[EngineRow],
          size_known: int | None, n_entries: int, size: int) -> bool:
    """Whether a page-counted walk stops after page `number`: at the page
    `declared` last (no declared page ends it); else once `rows` reach
    the known total; else on a short page (fewer than `size` entries: a
    server may serve fewer than asked)."""
    if pager.declared:
        last = fields.value(pager.declared, payload)
        return not isinstance(last, int) or number >= last
    if size_known is not None:
        return len(rows) >= size_known
    return n_entries < size


def next_url(payload: JSON, pager: CursorPager, home: str) -> str | None:
    """A cursor page's next-page URL, to follow verbatim; None when it
    names none or points outside the listing's own directory `home`
    (served data, not a promise)."""
    nxt = fields.path(payload, pager.next)
    return nxt if isinstance(nxt, str) and nxt.lower().startswith(home.lower()) else None


def scope_failed(scoped_total: int | None, board_total: int | None, cap: int,
                 rows: Sequence[EngineRow] = (),
                 board_page: Sequence[EngineRow] = ()) -> bool:
    """Whether a locality-scoped listing came back unnarrowed: as many
    postings as the whole board, or at least `cap` (the most the pull
    will read). With no scoped total: its `rows` open with every posting
    of the unscoped first page `board_page`, when that lists any.

    >>> scope_failed(82, 2000, 1200), scope_failed(2000, 2000, 1200)
    (False, True)
    >>> scope_failed(1300, None, 1200), scope_failed(None, 2000, 1200), scope_failed(0, 0, 1200)
    (True, False, False)
    >>> a, b, c = ({"id": x} for x in "abc")
    >>> scope_failed(None, None, 0, [a, b, c], [a, b, a]), scope_failed(None, None, 0, [b, a], [a, b])
    (True, False)

    Notes:
        2026-09-09: one board answered every scoped call with all 2000
        reqs for a day; the pull read 60 pages, detail-fetched 1,199 "N
        Locations" rows to rescue them (531s of an 872s crawl), and kept
        all 1,200 as local.
    """
    if not isinstance(scoped_total, int):
        head = list(dict.fromkeys(_ids(board_page)))
        return bool(head) and _ids(rows)[:len(head)] == head
    if scoped_total <= 0:
        return False
    if isinstance(board_total, int) and 0 < board_total <= scoped_total:
        return True
    return scoped_total >= cap


def _fresh(listed: list[EngineRow], seen: set[str | None]) -> list[EngineRow]:
    """The rows of one page that are new: a row whose id an earlier page
    gave is dropped, as is one repeating a row of its own page verbatim
    (a page may list a posting once per location; two copies of one row
    are one). `seen` takes the new ids."""
    new: list[EngineRow] = []
    here: set[str] = set()
    for r in listed:
        rid = r["id"]
        if rid is not None and rid in seen:
            continue
        key = json.dumps(r, sort_keys=True, default=str)
        if key not in here:
            here.add(key)
            new.append(r)
    seen.update(r["id"] for r in new)
    return new


async def walk(spec: Listing,
               ask: Callable[[int, Vals, str | None],
                             Awaitable[tuple[dict[str, str], int | None, JSON,
                                             str | Exception | None]]],
               rows_of: Callable[[dict[str, str], JSON], tuple[int, list[EngineRow]]],
               size: int | None = None, pages: int | None = None, cheap: bool = False,
               scoped: bool = False, budget: int | None = None,
               label: str | None = None) -> tuple[list[EngineRow] | None, int | None]:
    """(rows, total) for one listing `spec`; (None, None) when the first
    request failed. `await ask(n, vals, url)` makes page `n`'s request
    with the named values `vals` (`page_vals`), or follows a cursor's
    served `url`, and answers (parts, status, payload, error); `rows_of(parts,
    payload)` maps a page to (its entry count, its rows); rows are
    deduplicated (`_fresh`), both off the loop. Pages are read one at a
    time, PAGE_DELAY_S apart.
    `size` and `pages` override the pager's; a `cheap` read is one page
    unless `pages` says; else a row `budget` widens the pager's page cap
    at the step the walk takes (`page_cap`), a page pager with no size
    stepping by its first page's postings.

    An offset pager with no size learns it: a page's postings (distinct
    ids) are its entries and the first page's count is the size. The walk
    steps by it where that page reports the board's total, else by one row
    less: pages overlap by a row, so the last page of a board of two or
    more postings is always short, and a server wrapping back past its end
    does not read as capped. An offset or overlap pager whose first page
    serves fewer rows than asked, short of the board's total, steps by
    the served count: the server caps its pages (three Phenom tenants
    serve 10 whatever the size, and a 250-row step read 20 of ~300 rows).

    A later page's failure, once `_ask_page` has retried it, ends the walk
    with the rows so far (reported, so the snapshot reads incomplete);
    `label` names the board in the recovery line. The walk ends at a page listing no
    posting (no row with an id), where `ended` says, or where a cursor's
    `has_next` says so; a total at the pager's `ceiling` is the most the
    server reports, not the board's size, so it ends nothing. The walk
    notes the snapshot capped when it stopped anywhere else (every page
    read with the last still listing postings, a page adding no new
    posting or a cursor refused by `next_url`) with no total proving it
    complete; when it holds fewer rows than the total, unless `scoped` (a
    scope's total counts rows the pull drops) or it ended on its own within
    config.NEAR_COMPLETE_ROWS / NEAR_COMPLETE_FRACTION of the total; and when rows or total
    reach the `ceiling`, unless the walk read past it (this board's server
    serves more; a total above it is the board's own). The capped total
    is the larger of total and rows, unknown on a scoped pull short of the
    ceiling. A `cheap` walk notes nothing."""
    pager = spec.pager
    size = step = size or page_size(pager)
    learn = not size and pager is not None and pager.kind == "offset"
    widen = not (pages or cheap)
    pages = 1 if not pager else pages or (1 if cheap else page_cap(pager, budget,
                                                                   size and pager.stride))
    rows: list[EngineRow] = []
    seen: set[str | None] = set()
    total, size_known, capped, url, n = None, None, False, None, 0
    while True:
        parts, payload, err = await _ask_page(ask, n, page_vals(pager, n, step), url, label)
        if err:
            return (None, None) if n == 0 else (rows, total)
        n_entries, listed = await asyncio.to_thread(rows_of, parts, payload)
        if learn:
            n_entries = len(set(_ids(listed)))
        if n == 0 and pager:
            total, size_known, size, step, pages = _first_page(
                pager, payload, listed, n_entries, size, pages, budget, widen, learn)
        new = await asyncio.to_thread(_fresh, listed, seen)
        rows += new
        if not pager:
            break
        if pager.kind == "cursor":
            if not fields.path(payload, pager.has_next):
                break
            home = fields.fmt(spec.url, parts.get).rsplit("/", 1)[0] + "/"
            url = next_url(payload, pager, home)
            if not url:
                capped = True
                break
        elif not postings(listed) or ended(pager, payload, pager.number(n), rows,
                                           size_known, n_entries, size):
            break
        if not postings(new) or n + 1 >= pages:
            capped = True
            break
        await asyncio.sleep(config.PAGE_DELAY_S)
        n += 1
    if not cheap:
        _note(len(rows), total, size_known, capped, pager.ceiling if pager else None, scoped)
    return rows, total


async def _ask_page(ask: Callable[[int, Vals, str | None],
                                  Awaitable[tuple[dict[str, str], int | None, JSON,
                                                  str | Exception | None]]],
                    n: int, vals: Vals, url: str | None, label: str | None
                    ) -> tuple[dict[str, str], JSON, str | Exception | None]:
    """Page `n`'s (parts, payload, error). A later page's transient failure
    (a transport exception, 429 or 5xx) is asked again config.PAGE_RETRIES
    times, config.PAGE_RETRY_PAUSE_S apart, and the failures a retry repairs
    are withdrawn. An answer is never asked again: a 404, a 200 that would
    not parse, or a failure decided before any request (a message, not an
    exception: a handle missing a part, an unresolvable prelude).

    Notes:
        One lost page left the whole pass incomplete, closing nothing
        (Pfizer read 20 of ~600, 2026-10-06).
    """
    mark = http.failure_mark()
    parts, status, payload, err = await ask(n, vals, url)
    retried = False
    for _ in range(config.PAGE_RETRIES if n else 0):
        if not (isinstance(err, Exception) or status == 429 or (status or 0) >= 500):
            break
        retried = True
        await asyncio.sleep(config.PAGE_RETRY_PAUSE_S)
        parts, status, payload, err = await ask(n, vals, url)
    if retried:
        http.withdraw_failures(mark, f"{label or 'listing'} p{n}", keep=1 if err else 0)
    return parts, payload, err


def _first_page(pager: Pager, payload: JSON, listed: list[EngineRow], n_entries: int,
                size: int, pages: int, budget: int | None, widen: bool, learn: bool
                ) -> tuple[int | None, int | None, int, int, int]:
    """(total, size_known, size, step, pages) once page 0 is read: the
    total it reports (unknown at the ceiling), and the size, step and page
    cap `walk` learns from it."""
    total = total_of(pager, payload)
    size_known = None if total is not None and pager.ceiling and total == pager.ceiling else total
    step = size
    if learn:
        size = n_entries
        step = size - 1 if size_known is None and size > 1 else size
        pages = page_cap(pager, budget, step) if widen else pages
    elif widen and not size:
        pages = page_cap(pager, budget, len(set(_ids(listed))))
    elif pager.kind in ("offset", "overlap") and 0 < n_entries < min(size, size_known or 0):
        size = step = n_entries
        stride = pager.offset(1, step) - pager.offset(0, step)
        pages = page_cap(pager, budget, stride) if widen else pages
    return total, size_known, size, step, pages


def _note(n_rows: int, total: int | None, size_known: int | None, capped: bool,
          ceiling: int | None, scoped: bool) -> None:
    """Note a walk's snapshot capped where `walk` says it reads so.

    >>> from src.net.http import reset_fetch_failures, snapshot_info
    >>> def run(*a):
    ...     reset_fetch_failures(); _note(*a); return snapshot_info()["capped"]
    >>> run(1932, 1933, 1933, False, None, False)    # ended on its own, 1 short
    False
    >>> run(1932, 1933, 1933, True, None, False)     # page budget hit
    True
    >>> run(1800, 1933, 1933, False, None, False)    # well short
    True
    >>> run(2000, 2000, None, False, 2000, False)    # at the ceiling
    True
    """
    at_ceiling = bool(ceiling and n_rows <= ceiling <= max(total or 0, n_rows))
    complete = size_known is not None and n_rows >= size_known
    near = (size_known is not None and not capped
            and size_known - n_rows <= max(config.NEAR_COMPLETE_ROWS,
                                           config.NEAR_COMPLETE_FRACTION * size_known))
    short = size_known is not None and n_rows < size_known and not scoped and not near
    if (capped and not complete) or short or at_ceiling:
        note_capped(max(total, n_rows)
                    if total is not None and (at_ceiling or not scoped) else None)
