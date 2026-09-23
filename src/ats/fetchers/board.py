"""The one list -> row -> detail loop behind the board-shaped fetchers.

Every board-shaped ATS shares a shape: a listing yields rows; each row
has an id, a title, a location and maybe an inline description; some
have a per-posting detail call that is worth paying for only when the row
survives the filters. What is genuinely per-platform (the endpoints, the
row mapping, the detail call) lives in its `config.BOARDS` spec or, until
it migrates, its own fetcher module; `board_jobs` does the rest, so the
filter order is decided once:

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
every fetcher module: `board_jobs` covers the unvetted SWEEP, and `adapt`
the company-vetted WHOLE-BOARD pull (including Workday's and iCIMS's,
whose listing builders skip `board_jobs` and feed `adapt` directly). The
three builders inside company.py that shape the adapted dict
themselves, and fetchers/peopleadmin.py, which is also reached by the
sweep, call `clean_field` at their own row builders.
The aggregator and feed fetchers (jsonld, rssfeed, getro, remotive,
usajobs, ...) reach neither choke point; no open row from one of them
carries a bad field today (2026-09-18 audit), so they are left alone
rather than given a third copy of this rule.

`Board` (below) is the one engine every spec'd platform runs on: it reads
`config.BOARDS[ats]` and does the listing, the row mapping, the sweep and
whole-board pulls, hydration, probes and closure verdicts for that
platform, so no module outside the spec names one. `BOARDS` holds one per
spec; `board_for(ats)` and `board_for_url(url)` find them.
"""

import re
import time

from src import config
from src.match.locality import location_unknown
from src.net import http
from src.net.http import JSON_HEADERS, fetch_failed
from src.net.util import clean_field

from . import fields


def loc_ok(loc_re, text):
    """Whether `text` passes the location filter (no filter passes all).

    >>> import re
    >>> loc_ok(None, ""), loc_ok(re.compile("NC"), "Durham, NC"), loc_ok(re.compile("NC"), "")
    (True, True, False)
    """
    return loc_re is None or bool(loc_re.search(text or ""))


def board_jobs(rows, company_name, gate=None, loc_re=None,
               fetch_description=None, max_details=config.SWEEP_DETAILS,
               detail_delay=config.SWEEP_DETAIL_DELAY_S):
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
                fetch_description=None, max_details=config.SWEEP_DETAILS,
                detail_delay=config.SWEEP_DETAIL_DELAY_S):
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


# --------------------------------------------------------------------------- #
#  The company-fetch shape                                                     #
# --------------------------------------------------------------------------- #

_JOB_KEYS = ("id", "title", "url", "location", "description", "posted_at",
             "remote_hint", "_wd")


def adapt(jobs, ats, loc_re=None):
    r"""Job dicts in the company-fetch shape: `ats` named, `company`
    dropped (the store row supplies it), the description capped at
    `config.MAX_DESC_CHARS`, `_wd` kept where a fetcher set it.

    >>> adapt([{"id": "x_1", "company": "Acme", "title": "T", "url": "u",
    ...         "location": "Durham, NC", "description": "d", "posted_at": "2026-01-02"}], "x")
    [{'id': 'x_1', 'title': 'T', 'url': 'u', 'location': 'Durham, NC', 'description': 'd', 'posted_at': '2026-01-02', 'ats': 'x', '_wd': None}]

    `title` and `location` are `clean_field`-ed before `loc_re` sees the
    location: this is the whole-board path's choke point, as `board_jobs`
    is the sweep's.

    >>> adapt([{"id": "x_2", "title": "Data\nEngineer", "url": "u",
    ...         "location": "Durham,\tNC", "description": ""}], "x")[0]["location"]
    'Durham, NC'
    """
    out = []
    for j in jobs:
        j["title"] = clean_field(j.get("title"))
        j["location"] = clean_field(j.get("location"))
        if not loc_ok(loc_re, j["location"]):
            continue
        job = {k: j[k] for k in _JOB_KEYS if k in j}
        job["description"] = (j.get("description") or "")[:config.MAX_DESC_CHARS]
        job["ats"] = ats
        job.setdefault("_wd", None)
        out.append(job)
    return out


# --------------------------------------------------------------------------- #
#  The engine                                                                  #
# --------------------------------------------------------------------------- #

#: Top-level spec keys and what each holds; `validate_spec` enforces them.
SPEC_KEYS = {
    "sweep": bool,          # the lightweight sweep pulls it whole; seeds tags.SWEEP
    "prunable": bool,       # prune_dead_boards may deactivate it; two 404s bury it
    "guess": bool,          # discovery may guess its handle from a company name
    "handle": dict,         # {"columns": [...], "sep": "|"}; default one "slug" column
    "job_ref": dict,        # {"re", "parts"}: a stored posting URL -> the handle's
                            # columns plus "jid", the posting id
    "listing": dict,        # {"url", "probe_url", "decoder", "fields"}
    "detail": dict,         # {"url", "fields", "location"}: one posting read back
    "closure": dict,        # {"via": "detail" | "listing" | "page", "open", "closed"}
    "employer": (str, dict),  # a field spec naming the employer on a listing entry
}
_LISTING_KEYS = {"url", "probe_url", "decoder", "fields"}
_DETAIL_KEYS = {"url", "fields", "location"}
_ROW_FIELDS = {"id", "title", "url", "location", "description", "posted_at",
               "remote_hint", "department"}

#: Listings read for closure and deep verify: (ats, handle) -> (expires, entries).
_MEMO = {}


def validate_spec(name, spec):
    """Raise ValueError when `spec` (a `config.BOARDS` entry) breaks the
    schema: an unknown key, a wrong type, a regex that does not compile,
    or a field spec the grammar cannot read.

    >>> validate_spec("x", {"sweep": True, "listing": {"url": "u", "fields": {"id": "id"}}})
    >>> validate_spec("x", {"sweeps": True})
    Traceback (most recent call last):
    ...
    ValueError: x: unknown key 'sweeps'
    """
    def bad(msg):
        raise ValueError(f"{name}: {msg}")
    for key, v in spec.items():
        if key not in SPEC_KEYS:
            bad(f"unknown key {key!r}")
        if not isinstance(v, SPEC_KEYS[key]):
            bad(f"{key} is {type(v).__name__}")
    for key, allowed in (("listing", _LISTING_KEYS), ("detail", _DETAIL_KEYS)):
        extra = set(spec.get(key) or {}) - allowed
        if extra:
            bad(f"unknown {key} key(s) {sorted(extra)}")
        extra = set((spec.get(key) or {}).get("fields") or {}) - _ROW_FIELDS
        if any(not k.startswith("_") for k in extra):
            bad(f"unknown {key} field(s) {sorted(extra)}")
    if spec.get("job_ref"):
        try:
            n = re.compile(spec["job_ref"]["re"]).groups
        except re.error as e:
            bad(f"job_ref.re: {e}")
        if n != len(spec["job_ref"]["parts"]):
            bad("job_ref.parts must name every group")
    try:
        for key in ("listing", "detail"):
            for f in ((spec.get(key) or {}).get("fields") or {}).values():
                fields.check(f)
        for c in ((spec.get("closure") or {}).get(k) for k in ("open", "closed")):
            if c:
                fields.check({"const": 1, "when": c})
        if spec.get("employer"):
            fields.check(spec["employer"])
    except ValueError as e:
        bad(str(e))


class Board:
    """One platform, compiled from `config.BOARDS[name]`. Every loop lives
    here; the spec names the endpoints and the fields.

    A handle is the store row's board columns joined by "|" (the string
    `registry.store_slug` builds); its parts, named after the columns,
    fill the spec's URL templates.
    """

    def __init__(self, name, spec):
        validate_spec(name, spec)
        self.name, self.spec = name, spec
        self.listing_spec = spec.get("listing") or {}
        self.detail_spec = spec.get("detail") or {}
        self.fetchable = bool(self.listing_spec)
        self._columns = (spec.get("handle") or {}).get("columns", ["slug"])
        self._sep = (spec.get("handle") or {}).get("sep", "|")
        ref = spec.get("job_ref")
        self._ref_re = re.compile(ref["re"]) if ref else None
        self._ref_parts = ref["parts"] if ref else []
        self._closure = spec.get("closure") or {}

    def __repr__(self):
        return f"Board({self.name!r})"

    # --- handles and URLs --------------------------------------------------

    def handle(self, company):
        """The handle string for a store row, or None when a column is empty."""
        vals = [str(company.get(c) or "") for c in self._columns]
        return self._sep.join(vals) if all(vals) else None

    def _parts(self, handle):
        return dict(zip(self._columns, str(handle).split(self._sep)))

    def job_ref(self, url):
        """The named parts a stored posting URL carries, or None when the
        URL is not this platform's."""
        m = self._ref_re.search(url or "") if self._ref_re else None
        return dict(zip(self._ref_parts, m.groups())) if m else None

    def owns_url(self, url):
        return self.job_ref(url) is not None

    def _handle_of(self, ref):
        return self._sep.join(ref.get(c, "") for c in self._columns)

    # --- the listing -------------------------------------------------------

    def _label(self, handle, company_name=""):
        return f"{self.name} {company_name or handle}"

    def _read(self, handle, label=None, cheap=False):
        """The listing's entries, or None when the request failed (reported
        under `label` when given). `cheap` reads `probe_url` where the spec
        has one, at PROBE_TIMEOUT: probes, samples, counts."""
        spec = self.listing_spec
        url = spec["probe_url"] if cheap and spec.get("probe_url") else spec["url"]
        kw = {"timeout": config.PROBE_TIMEOUT} if cheap else {}
        _status, data, err = http.request_json(
            "GET", fields.fmt(url, self._parts(handle).get), label,
            headers=JSON_HEADERS, **kw)
        return None if err else self._entries(data)

    def _entries(self, payload):
        """The posting list in a listing payload; a wrong shape is []."""
        wanted = (self.listing_spec.get("decoder") or {}).get("entries", "")
        for p in wanted if isinstance(wanted, list) else [wanted]:
            v = fields.path(payload, p)
            if v is not None:
                return v if isinstance(v, list) else []
        return []

    def _row(self, parts, entry):
        fs = self.listing_spec["fields"]
        ctx = dict(parts)
        for k, s in fs.items():
            if k.startswith("_"):
                ctx[k] = fields.value(s, entry, ctx)

        def get(k):
            return fields.value(fs.get(k), entry, ctx)

        title = get("title") or ""
        row = {"id": get("id"), "title": title, "url": get("url") or "",
               "location": get("location") or "",
               "description": get("description") or "",
               "posted_at": fields.TRANSFORMS["date"](get("posted_at"))}
        hint = get("remote_hint")
        if hint:
            row["remote_hint"] = hint
        dept = get("department")
        row["head"] = f"{title} {dept}" if dept else title
        return row

    def listing(self, handle, label=None, cheap=False):
        """Every row on the board, mapped by the spec's fields; [] when the
        listing failed (reported under `label` when given)."""
        entries = self._read(handle, label, cheap) or []
        parts = self._parts(handle)
        return [self._row(parts, e) for e in entries if isinstance(e, dict)]

    # --- the pulls ---------------------------------------------------------

    def _detail_rows(self):
        """board_jobs' detail callback, for a listing that carries no body."""
        if not self.detail_spec or "description" in self.listing_spec["fields"]:
            return None
        return lambda row: self.description_for(row.get("url"), report=True)

    def jobs(self, handle, company_name="", gate=None, loc_re=None):
        """The sweep: `board_jobs` over the listing, screened by `gate`."""
        rows = self.listing(handle, self._label(handle, company_name))
        return board_jobs(rows, company_name, gate=gate, loc_re=loc_re,
                          fetch_description=self._detail_rows())

    def whole_board(self, company, loc_re=None):
        """The company-vetted pull: every row passing `loc_re`, adapted."""
        handle = self.handle(company)
        if not handle:
            return []
        rows = self.listing(handle, self._label(handle))
        return adapt(board_jobs(rows, "", loc_re=loc_re), self.name)

    def probe(self, handle):
        """(ok, n) for a guessed handle: one cheap read, n postings on it,
        ok when there is at least one. Quiet: a miss is the expected answer."""
        entries = self._read(handle, cheap=True)
        n = len(entries or [])
        return n > 0, n

    def alive(self, handle):
        """(ok, n) where ok means the board request itself succeeded, empty
        or not: the dead-board check."""
        entries = self._read(handle, cheap=True)
        return entries is not None, len(entries or [])

    def local_count(self, handle, is_local):
        """Postings whose listed location `is_local(text)` accepts."""
        return sum(1 for r in self.listing(handle, cheap=True)
                   if is_local(r["location"]))

    def employer_name(self, handle):
        """The employer the listing names on its first posting, or ""."""
        entries = self._read(handle, cheap=True) or []
        spec = self.spec.get("employer")
        return (str(fields.value(spec, entries[0]) or "").strip()
                if spec and entries else "")

    # --- one posting -------------------------------------------------------

    def _listing_entries(self, handle):
        """The raw listing entries, memoized for config.BOARD_MEMO_S so a
        board with many stale rows is read once per pass. None when the
        listing is unreadable or empty."""
        key = (self.name, handle)
        hit = _MEMO.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        entries = self._read(handle) or None
        _MEMO[key] = (time.time() + config.BOARD_MEMO_S, entries)
        return entries

    def _member(self, ref):
        """The listing entry whose id is the posting `ref` names, or None."""
        want = str(ref.get("jid", "")).lower()
        for e in self._listing_entries(self._handle_of(ref)) or []:
            if isinstance(e, dict) and str(e.get("id", "")).lower() == want:
                return e
        return None

    def detail(self, ref, report=False):
        """(status, record, error) for the posting `ref` names."""
        label = f"{self.name} {self._handle_of(ref)} detail" if report else None
        return http.request_json("GET", fields.fmt(self.detail_spec["url"], ref.get),
                                 label, headers=JSON_HEADERS)

    def _posting(self, url, report=False):
        """(record, its field specs) for the posting `url` names, read live:
        the detail endpoint, or the listing entry where the platform has
        none. (None, {}) on any miss."""
        ref = self.job_ref(url)
        if not ref:
            return None, {}
        if self.detail_spec:
            return self.detail(ref, report)[1], self.detail_spec["fields"]
        return self._member(ref), self.listing_spec["fields"]

    def description_for(self, url, report=False):
        """The posting's description, read live; "" on any miss."""
        rec, fs = self._posting(url, report)
        return (fields.value(fs.get("description"), rec) or "") if rec else ""

    def needs_detail(self, job):
        """Whether `hydrate` would fetch anything: no body yet, or a location
        the listing never resolved that this platform's detail can fill."""
        if not job.get("description"):
            return True
        return (bool(self.detail_spec)
                and self.detail_spec.get("location", "if_unknown") != "never"
                and location_unknown(job.get("location")))

    def hydrate(self, job):
        """Fill, in place, what `needs_detail` says `job` lacks: the body,
        and the location as the spec's `detail.location` allows ("always",
        "if_unknown" the default, or "never")."""
        if not self.needs_detail(job):
            return job
        rec, fs = self._posting(job.get("url"), report=True)
        if not rec:
            return job
        desc = fields.value(fs.get("description"), rec)
        if desc and not job.get("description"):
            job["description"] = desc[:config.MAX_DESC_CHARS]
        policy = self.detail_spec.get("location", "if_unknown")
        loc = fields.value(fs.get("location"), rec) if self.detail_spec else None
        if loc and (policy == "always"
                    or policy == "if_unknown" and location_unknown(job.get("location"))):
            job["location"] = loc
        return job

    def probe_job(self, url, job_id=None):
        """(is_open, reason) for a stored posting URL: True live, False
        positively closed, None unverifiable. (None, "") when the URL is not
        this platform's or its closure is judged from the page."""
        ref = self.job_ref(url)
        via = self._closure.get("via", "detail" if self.detail_spec else "page")
        if ref is None or via == "page":
            return None, ""
        if via == "listing":
            entries = self._listing_entries(self._handle_of(ref))
            if not entries:
                return None, f"{self.name} api: board unreadable or empty"
            if self._member(ref) is not None:
                return True, f"{self.name} api: board lists it"
            return False, f"{self.name} api: board no longer lists it"
        status, rec, err = self.detail(ref)
        if status is None:
            return None, f"{self.name} api error: {type(err).__name__}"
        if status in (404, 410):
            return False, f"{self.name} api HTTP {status}"
        if status != 200:
            return None, f"{self.name} api HTTP {status}"
        closed, open_ = self._closure.get("closed"), self._closure.get("open")
        if closed and rec is not None and fields.holds(closed, rec):
            return False, f"{self.name} api: closed"
        if open_ and not (rec is not None and fields.holds(open_, rec)):
            return None, f"{self.name} api: no open signal"
        return True, f"{self.name} api: posting live"


BOARDS = {name: Board(name, spec) for name, spec in config.BOARDS.items()}


def board_for(ats):
    """The engine for `ats`, or None when no spec names it."""
    return BOARDS.get(ats)


def board_for_url(url):
    """The first board, in spec order, whose job_ref reads `url`; or None."""
    return next((b for b in BOARDS.values() if b.owns_url(url)), None)
