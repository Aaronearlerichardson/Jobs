"""The one list -> row -> detail loop behind the board-shaped fetchers.

Greenhouse, Lever, Ashby, BambooHR, ADP, Paylocity, Rippling, HiBob, UKG
Pro, Kula, JazzHR and SuccessFactors share a shape: a listing yields rows;
each row has an id, a title, a location and maybe an inline description;
some of them have a per-posting detail call that is worth paying for only
when the row survives the filters. Each module keeps what is genuinely its
own (the endpoints, the row parser, the detail call) and hands `board_jobs`
the rest, so the filter order is decided once:

  1. location (`loc_re`) on the LISTED location, before any detail call:
     an out-of-area posting costs one listing row and nothing more;
  2. relevance (`gate`), cheap fields first: `gate(head)` on the title
     (plus the department where the ATS lists one); only a row that fails
     on those pays for its description, and `gate(head, description)`
     then decides. No gate keeps every row;
  3. description hydration for what is kept, within `max_details`.

`gate=None, loc_re=None` is a whole-board pull (the company-vetted path in
fetchers/company.py); the unvetted sweep passes `gate=is_relevant`.

A row is a dict with `id`, `title`, `url`, `location`, `description` ("" when
the listing carries none) and optionally `head` (the text the gate screens
first; defaults to the title), `posted_at`, `remote_hint`, plus any
"_"-prefixed keys the module's detail call needs, which are stripped from
the output. A module yields None for a listing entry it cannot use.

`title` and `location` are run through `net.util.clean_field` before
anything else sees them (the gates, the store, the session log): a raw
ATS payload's title or location can carry an embedded newline or tab -- a
search-row template that wraps onto two lines, a location cell with a
stray tab between city and state -- and 124 open rows already carry one
(2026-09-18 audit). Stored verbatim, that character splits triage's one-line DEBUG
"drop" record into fragments a session-log reader cannot tell from a new
record ("Calibration | local-tech=title" on its own line).

`clean_field` is applied at each of the two paths' own choke point, not in
every fetcher module: `board_jobs` here covers the unvetted SWEEP, and
`fetchers.company._adapt` covers the company-vetted WHOLE-BOARD pull that
skips this module entirely (including Workday's and iCIMS's, whose listing
builders feed _adapt directly). The three builders inside company.py that
shape the adapted dict themselves, and fetchers/peopleadmin.py, which is
also reached by the sweep, call `clean_field` at their own row builders.
The aggregator and feed fetchers (jsonld, rssfeed, getro, remotive,
usajobs, ...) reach neither choke point; no open row from one of them
carries a bad field today (2026-09-18 audit), so they are left alone
rather than given a third copy of this rule.
"""

import time

from src.net.http import fetch_failed
from src.net.util import clean_field


def loc_ok(loc_re, text):
    """Whether `text` passes the location filter (no filter passes all).

    >>> import re
    >>> loc_ok(None, ""), loc_ok(re.compile("NC"), "Durham, NC"), loc_ok(re.compile("NC"), "")
    (True, True, False)
    """
    return loc_re is None or bool(loc_re.search(text or ""))


def board_jobs(rows, company_name, gate=None, loc_re=None,
               fetch_description=None, max_details=40, detail_delay=0.2):
    """Job dicts for the rows that pass `loc_re` and `gate` (see module doc).

    `fetch_description(row)` is the ATS's detail call, when it has one; it
    runs at most `max_details` times per board, `detail_delay` seconds
    apart, and a failure ("" back) leaves whatever the listing carried.

    >>> rows = [{"id": "1", "title": "Data Engineer", "url": "u1", "location": "Durham, NC",
    ...          "description": "", "_key": "a"},
    ...         {"id": "2", "title": "Chef", "url": "u2", "location": "Durham, NC",
    ...          "description": ""},
    ...         {"id": "3", "title": "Data Engineer", "url": "u3", "location": "Austin, TX",
    ...          "description": ""},
    ...         None]
    >>> import re
    >>> calls = []
    >>> jobs = board_jobs(rows, "Acme", gate=lambda t, d="": "data" in t.lower(),
    ...                   loc_re=re.compile("NC"),
    ...                   fetch_description=lambda r: calls.append(r["id"]) or "body",
    ...                   detail_delay=0)
    >>> [(j["id"], j["company"], j["description"]) for j in jobs]
    [('1', 'Acme', 'body')]

    The in-area "Chef" row paid one detail call to be judged on its
    description; the out-of-area row paid nothing; the module's "_" key
    never reaches the output:

    >>> calls, "_key" in jobs[0]
    (['1', '2'], False)

    A title or location carrying a newline, tab or repeated space -- a
    search-row template that wraps onto two lines -- is cleaned before
    anything (the gate, the location filter, the output) sees it, and a
    title that is nothing BUT whitespace is dropped like a missing one:

    >>> messy = [{"id": "4", "title": "Data\\nEngineer", "url": "u4",
    ...           "location": "Durham,\\tNC", "description": ""},
    ...          {"id": "5", "title": "   ", "url": "u5", "location": "",
    ...           "description": ""}]
    >>> jobs = board_jobs(messy, "Acme")
    >>> [(j["id"], j["title"], j["location"]) for j in jobs]
    [('4', 'Data Engineer', 'Durham, NC')]
    """
    out, fetched = [], 0

    def hydrate(row):
        nonlocal fetched
        fetched += 1
        desc = fetch_description(row) or ""
        if detail_delay:
            time.sleep(detail_delay)
        return desc

    def can_fetch():
        return fetch_description is not None and fetched < max_details

    for row in rows:
        if not row or not row.get("id"):
            continue
        title = clean_field(row.get("title"))
        if not title:
            continue
        row["title"] = title
        row["location"] = clean_field(row.get("location"))
        if not loc_ok(loc_re, row.get("location", "")):
            continue
        head = clean_field(row.pop("head", None)) or title
        desc = row.get("description") or ""
        if gate is not None:
            if not gate(head) and can_fetch():
                desc = hydrate(row) or desc
            if not gate(head, desc):
                continue
        if not desc and can_fetch():
            desc = hydrate(row)
        job = {k: v for k, v in row.items() if not k.startswith("_")}
        job["company"] = company_name
        job["description"] = desc
        out.append(job)
    return out


def board_fetch(label, parse, row, company_name="", gate=None, loc_re=None,
                fetch_description=None, max_details=40, detail_delay=0.2):
    """`board_jobs` over a listing this module still has to go and GET.

    `parse()` returns the raw listing entries and may raise; `row(entry)`
    shapes one of them, or returns None to drop it. A `parse()` that
    raises is reported through `net.http.fetch_failed` and yields no jobs,
    never an exception at the crawl. Everything else is `board_jobs`'.

    No doctest of its own: its callers' public entry points already pin
    every behaviour here. The dead-listing path is covered by
    tests/test_fetcher_parsers.py::TestADeadEndpointIsNeverAnException
    (every registered fetcher, two failure shapes each); the row/filter
    path by `board_jobs`' own doctest.

    Notes:
        Five fetchers -- rippling, workable, hibob, ultipro, paylocity --
        had each written the same four lines around the call above: pull
        the whole board, turn a listing failure into a reported dead
        source rather than an exception at the crawl, then map the raw
        entries through the module's own row builder. Only the label, the
        listing call and the row builder ever differed, and
        `fetch_workable` was written by copying `fetch_rippling` (0.98
        structural similarity, 2026-09-22 clone scan).

        Reporting the listing failure is the part worth having in one
        place: `net.http.fetch_failed` hands back [], which is also what
        an empty board hands back, so a fetcher that swallows the
        exception instead leaves "this board is down" and "this board has
        nothing on it" indistinguishable at every caller (see
        fetch_failed's own note).
    """
    try:
        raw = parse()
    except Exception as e:
        return fetch_failed(label, e)
    return board_jobs((row(j) for j in raw), company_name, gate=gate,
                      loc_re=loc_re, fetch_description=fetch_description,
                      max_details=max_details, detail_delay=detail_delay)
