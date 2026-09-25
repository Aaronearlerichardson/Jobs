"""The listing walk: one listing read page by page, the rows it gives
deduplicated, the stop rule, and the capped-snapshot bookkeeping.

A listing's pager (a `spec.Pager`: offset, overlap, page or cursor) says
how pages step; `walk` reads them through the caller's request and row
callbacks, so it knows nothing of handles, headers or decoders.
"""

import json
import math
import time

from src import config
from src.net.http import note_capped

from . import fields


def page_vals(pager, n, size):
    """The named request values for page `n` (from 0) of `size` rows, a
    listing without a pager reading page 0.

    >>> from .spec import OffsetPager, OverlapPager, PagePager
    >>> page_vals(None, 0, 1)
    {'$size': 1, '$offset': 0, '$page': 0}
    >>> page_vals(PagePager(kind="page", start=1), 2, 100)
    {'$size': 100, '$offset': 200, '$page': 3}
    >>> page_vals(OffsetPager(kind="offset", size=50, start=1), 1, 50)["$offset"]
    51
    >>> why = "rows shift, 2026-09"
    >>> page_vals(OverlapPager(kind="overlap", size=500, step=250, why=why), 1, 500)["$offset"]
    250
    >>> page_vals(PagePager(kind="page", bare_first=True, why=why), 0, 0)["$page"] is None
    True
    """
    if pager is None:
        return {"$size": size, "$offset": n * size, "$page": n}
    return {"$size": size, "$offset": pager.offset(n, size), "$page": pager.page(n)}


def page_size(pager):
    """Rows a pager asks per page; 0 when unknown or unpaged."""
    return (pager.size or 0) if pager else 0


def page_cap(pager, budget, step):
    """The most pages a walk stepping `step` rows reads: the pager's
    `pages`, widened to cover the row budget `budget` where one is given."""
    return max(pager.pages, math.ceil(budget / step)) if budget and step else pager.pages


def total_of(pager, payload):
    """The int total a pager's `total` names on `payload`, else None."""
    t = fields.value(pager.total, payload) if pager and pager.total else None
    return t if isinstance(t, int) else None


def postings(rows):
    """Whether `rows` hold a posting: a row with an id."""
    return any(r["id"] is not None for r in rows or [])


def ended(pager, payload, number, rows, size_known, n_entries, size):
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


def next_url(payload, pager, home):
    """A cursor page's next-page URL, to follow verbatim; None when it
    names none or points outside the listing's own directory `home`
    (served data, not a promise)."""
    nxt = fields.path(payload, pager.next)
    return nxt if isinstance(nxt, str) and nxt.lower().startswith(home.lower()) else None


def scope_failed(scoped_total, board_total, cap, rows=(), board_page=()):
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
        head = list(dict.fromkeys(r["id"] for r in board_page if r["id"] is not None))
        return bool(head) and [r["id"] for r in rows if r["id"] is not None][:len(head)] == head
    if scoped_total <= 0:
        return False
    if isinstance(board_total, int) and 0 < board_total <= scoped_total:
        return True
    return scoped_total >= cap


def _fresh(listed, seen):
    """The rows of one page that are new: a row whose id an earlier page
    gave is dropped, as is one repeating a row of its own page verbatim
    (a page may list a posting once per location; two copies of one row
    are one). `seen` takes the new ids."""
    new, here = [], set()
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


def walk(spec, ask, rows_of, size=None, pages=None, cheap=False, scoped=False, budget=None):
    """(rows, total) for one listing `spec`; (None, None) when the first
    request failed. `ask(n, vals, url)` makes page `n`'s request with the
    named values `vals` (`page_vals`), or follows a cursor's served `url`,
    and answers (parts, payload, error); `rows_of(parts, payload)` maps a
    page to (its entry count, its rows); rows are deduplicated (`_fresh`).
    `size` and `pages` override the pager's; a `cheap` read is one page
    unless `pages` says; else a row `budget` widens the pager's page cap
    at the step the walk takes (`page_cap`).

    An offset pager with no size learns it: a page's postings (distinct
    ids) are its entries and the first page's count is the size. The walk
    steps by it where that page reports the board's total, else by one row
    less: pages overlap by a row, so the last page of a board of two or
    more postings is always short, and a server wrapping back past its end
    does not read as capped.

    A later page's failure ends the walk with the rows so far (reported,
    so the snapshot reads incomplete). The walk ends at a page listing no
    posting (no row with an id), where `ended` says, or where a cursor's
    `has_next` says so; a total at the pager's `ceiling` is the most the
    server reports, not the board's size, so it ends nothing. The walk
    notes the snapshot capped when it stopped anywhere else (every page
    read with the last still listing postings, a page adding no new
    posting or a cursor refused by `next_url`) with no total proving it
    complete; when it holds fewer rows than the total, unless `scoped` (a
    scope's total counts rows the pull drops); and when rows or total
    reach the `ceiling`. The capped total is the larger of total and rows,
    unknown on a scoped pull short of the ceiling. A `cheap` walk notes
    nothing."""
    pager = spec.pager
    size = step = size or page_size(pager)
    learn = not size and pager is not None and pager.kind == "offset"
    widen = not (pages or cheap)
    pages = 1 if not pager else pages or (1 if cheap else page_cap(pager, budget,
                                                                   size and pager.stride))
    ceiling = pager.ceiling if pager else None
    rows, seen, total, size_known, capped, url, n = [], set(), None, None, False, None, 0
    while True:
        parts, payload, err = ask(n, page_vals(pager, n, step), url)
        if err:
            return (None, None) if n == 0 else (rows, total)
        if n == 0 and pager and pager.total:
            total = total_of(pager, payload)
            size_known = None if total is not None and ceiling and total >= ceiling else total
        n_entries, listed = rows_of(parts, payload)
        if learn:
            n_entries = len({r["id"] for r in listed if r["id"] is not None})
            if n == 0:
                size = n_entries
                step = size - 1 if size_known is None and size > 1 else size
                pages = page_cap(pager, budget, step) if widen else pages
        new = _fresh(listed, seen)
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
        time.sleep(config.PAGE_DELAY_S)
        n += 1
    at_ceiling = bool(ceiling) and max(total or 0, len(rows)) >= ceiling
    complete = size_known is not None and len(rows) >= size_known
    short = size_known is not None and len(rows) < size_known and not scoped
    if not cheap and ((capped and not complete) or short or at_ceiling):
        known = total is not None and (at_ceiling or not scoped)
        note_capped(max(total, len(rows)) if known else None)
    return rows, total
