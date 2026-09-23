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
     on those, and has no body yet, pays for its description, and
     `gate(head, description)` then decides. No gate keeps every row;
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

import json
import re
import time

from bs4 import BeautifulSoup

from src import config
from src.match.locality import location_unknown
from src.net import http
from src.net.http import HEADERS, JSON_HEADERS, note_capped
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
    runs only for a row the listing gave no body, at most `max_details`
    times per board, `detail_delay` seconds apart.

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
            if not gate(head) and not desc and can_fetch():
                desc = hydrate(row)
            if not gate(head, desc):
                continue
        if not desc and can_fetch():
            desc = hydrate(row)
        job = {k: v for k, v in row.items() if not k.startswith("_")}
        job["company"] = company_name
        job["description"] = desc
        out.append(job)
    return out


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
    "eager": bool,          # a whole-board pull reads each kept row's detail
    "handle": dict,         # {"columns", "parts", "sep", "try", "accept",
                            #  "follow"}: the store columns joined by sep;
                            # `parts` names the pieces (default the columns);
                            # `try` lists values for one part, tried until one
                            # answers with a status outside
                            # `accept.status_not`; `follow` maps a part to a
                            # URL template it is the redirect target of; both
                            # settled once per handle
    "job_ref": dict,        # {"re", "parts"}: a stored posting URL -> handle
                            # parts plus the posting's own ("jid", or the
                            # listing entry keys its id reads)
    "listing": dict,        # {"url", "method", "params", "json", "headers",
                            #  "probe_url", "decoder", "pager", "fields"}
    "detail": dict,         # {"url", "method", "params", "json", "headers",
                            #  "decoder", "record", "fields", "location"}:
                            # one posting read back
    "closure": dict,        # {"via": "detail" | "listing" | "page", "open",
                            #  "closed"}: each a condition, or a list of
                            # {"when", "why"} rules, "why" a field spec
                            # naming the reason
    "employer": (str, dict),  # a field spec naming the employer on a listing entry
}
_LISTING_KEYS = {"url", "method", "params", "json", "headers", "probe_url",
                 "decoder", "pager", "fields"}
_DETAIL_KEYS = {"url", "method", "params", "json", "headers", "decoder",
                "record", "fields", "location"}
_ROW_FIELDS = {"id", "title", "url", "location", "description", "posted_at",
               "remote_hint", "department"}
_DECODERS = {"json", "json_in_html", "html"}
#: offset: "$offset" steps a page; overlap: it steps "step" < "size", so
#: pages overlap; cursor: each page names the next ("next", ended by
#: "has_next").
_PAGERS = {"offset", "overlap", "cursor"}

#: Listings read for closure and deep verify: (ats, handle) -> (expires, entries).
_MEMO = {}
#: The handle parts `handle.try` and `handle.follow` settled on:
#: (ats, handle) -> {part: value}.
_VARIANTS = {}


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
        part = spec.get(key) or {}
        extra = set(part) - allowed
        if extra:
            bad(f"unknown {key} key(s) {sorted(extra)}")
        extra = set(part.get("fields") or {}) - _ROW_FIELDS
        if any(not k.startswith("_") for k in extra):
            bad(f"unknown {key} field(s) {sorted(extra)}")
        if (part.get("decoder") or {}).get("kind", "json") not in _DECODERS:
            bad(f"{key}.decoder.kind")
    pager = (spec.get("listing") or {}).get("pager")
    if pager and (pager.get("kind") not in _PAGERS or not pager.get("size")):
        bad("listing.pager needs a known kind and a size")
    if pager and pager["kind"] == "overlap" and not 0 < pager.get("step", 0) < pager["size"]:
        bad("listing.pager: an overlap steps less than a page")
    if pager and pager["kind"] == "cursor" and not (pager.get("next") and pager.get("has_next")):
        bad("listing.pager: a cursor needs next and has_next")
    if not all(isinstance(v, str) for v in ((spec.get("handle") or {}).get("follow") or {}).values()):
        bad("handle.follow maps a part to a URL template")
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
            for rule in _rules(c):
                if set(rule) - {"when", "why"} or "when" not in rule:
                    raise ValueError(f"closure rule {rule!r}")
                fields.check({"const": 1, "when": rule["when"]})
                fields.check(rule.get("why"))
        if spec.get("employer"):
            fields.check(spec["employer"])
    except ValueError as e:
        bad(str(e))


def _fill(tpl, lookup, vals):
    """A request template filled in: "$size"/"$offset" by value (typed),
    every other string as a `fields.fmt` template.

    >>> _fill({"top": "$size", "q": "{code}", "f": []}, {"code": "AC"}.get, {"$size": 50})
    {'top': 50, 'q': 'AC', 'f': []}
    """
    if isinstance(tpl, dict):
        return {k: _fill(v, lookup, vals) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [_fill(v, lookup, vals) for v in tpl]
    if isinstance(tpl, str):
        return vals[tpl] if tpl in vals else fields.fmt(tpl, lookup)
    return tpl


def _decode(dec, text):
    """A non-JSON response body as data; None when it holds none. Raises
    ValueError when embedded JSON will not parse."""
    if dec.get("kind") == "json_in_html":
        m = re.search(dec["regex"], text)
        return json.JSONDecoder().raw_decode(text, m.end())[0] if m else None
    el = BeautifulSoup(text, "html.parser").select_one(dec["select"])
    return {"text": el.get_text(" ", strip=True)} if el else None


def _first_path(payload, wanted, kind):
    """The first value of type `kind` at one of the paths `wanted` (one or
    a list) in `payload`, or None."""
    for p in wanted if isinstance(wanted, list) else [wanted]:
        v = fields.path(payload, p)
        if isinstance(v, kind):
            return v
    return None


def _unwrap(v, key):
    """`v` with every dict holding `key` replaced by that key's value: a
    decoder's `values`, for a payload that wraps each value in a dict.

    >>> _unwrap({"f": {"A": {"value": 1, "size": 3}}, "n": [{"value": "x"}]}, "value")
    {'f': {'A': 1}, 'n': ['x']}
    """
    if isinstance(v, dict):
        return v[key] if key in v else {k: _unwrap(x, key) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x, key) for x in v]
    return v


def _rules(c):
    """A closure condition as its rule list: a list already, else one rule."""
    return c if isinstance(c, list) else [{"when": c}] if c else []


def _reason(c, rec, default):
    """The reason the first of closure rules `c` holding for `rec` gives
    (its "why", else `default`); None when none holds.

    >>> _reason([{"when": {"falsy": "a"}, "why": {"const": "gone"}}], {}, "closed")
    'gone'
    >>> _reason({"truthy": "a"}, {"a": 1}, "open"), _reason({"truthy": "a"}, {}, "open")
    ('open', None)
    """
    for rule in _rules(c) if rec is not None else []:
        if fields.holds(rule["when"], rec):
            return str(fields.value(rule.get("why"), rec) or default)
    return None


class Board:
    """One platform, compiled from `config.BOARDS[name]`. Every loop lives
    here; the spec names the endpoints and the fields.

    A handle is the store row's board columns joined by "|" (the string
    `registry.store_slug` builds); split on the spec's separator, its
    parts fill the spec's templates.
    """

    def __init__(self, name, spec):
        validate_spec(name, spec)
        self.name, self.spec = name, spec
        self.listing_spec = spec.get("listing") or {}
        self.detail_spec = spec.get("detail") or {}
        self.fetchable = bool(self.listing_spec)
        self._hspec = spec.get("handle") or {}
        self._columns = self._hspec.get("columns", ["slug"])
        self._part_names = self._hspec.get("parts", self._columns)
        self._sep = self._hspec.get("sep", "|")
        ref = spec.get("job_ref")
        self._ref_re = re.compile(ref["re"]) if ref else None
        self._ref_parts = ref["parts"] if ref else []
        self._closure = spec.get("closure") or {}
        self._pager = self.listing_spec.get("pager") or {}

    def __repr__(self):
        return f"Board({self.name!r})"

    # --- handles and URLs --------------------------------------------------

    def handle(self, company):
        """The handle string for a store row, or None when a column is empty."""
        vals = [str(company.get(c) or "") for c in self._columns]
        return self._sep.join(vals) if all(vals) else None

    def _parts(self, handle):
        parts = dict(zip(self._part_names, str(handle).split(self._sep)))
        parts.update(_VARIANTS.get((self.name, str(handle)), {}))
        return parts

    def job_ref(self, url):
        """The named parts a stored posting URL carries, or None when the
        URL is not this platform's."""
        m = self._ref_re.search(url or "") if self._ref_re else None
        return dict(zip(self._ref_parts, m.groups())) if m else None

    def owns_url(self, url):
        return self.job_ref(url) is not None

    def _handle_of(self, ref):
        return self._sep.join(ref.get(p, "") for p in self._part_names)

    # --- requests ----------------------------------------------------------

    def _fetch(self, req, parts, vals=None, label=None, timeout=None, url=None):
        """(status, payload, error) for one request built from `req` (the
        listing or detail spec) and the handle `parts`; `url`, a served
        next-page URL, replaces the spec's URL and parameters verbatim."""
        dec = req.get("decoder") or {}
        kind = dec.get("kind", "json")
        vals = vals or {}
        kw = {"headers": {**(JSON_HEADERS if kind == "json" else HEADERS),
                          **_fill(req.get("headers") or {}, parts.get, vals)}}
        for key in ("params", "json") if url is None else ():
            if req.get(key):
                kw[key] = _fill(req[key], parts.get, vals)
        if timeout:
            kw["timeout"] = timeout
        url, method = url or fields.fmt(req["url"], parts.get), req.get("method", "GET")
        if kind == "json":
            status, payload, err = http.request_json(method, url, label, **kw)
        else:
            status, r, err = http.request(method, url, label, **kw)
            if err:
                return status, None, err
            try:
                payload = _decode(dec, r.text)
            except ValueError:
                return status, None, http.failed(label, "unreadable response")
        if dec.get("values") and payload is not None:
            payload = _unwrap(payload, dec["values"])
        return status, payload, err

    def _follow(self, handle, parts, label=None, timeout=None):
        """Settle into `parts` each `handle.follow` part not yet known for
        `handle`: the URL its template redirects to, query and trailing "/"
        dropped (a scheme-less template is https). The error, reported
        under `label`, when one does not answer 200; else None."""
        for name, tpl in (self._hspec.get("follow") or {}).items():
            if name in parts:
                continue
            url = fields.fmt(tpl, parts.get)
            url = url if re.match(r"(?i)^https?://", url) else f"https://{url}"
            status, r, err = http.request("GET", url, **({"timeout": timeout} if timeout else {}))
            if err or status != 200:
                return http.failed(label, f"could not resolve the board's {name}")
            parts[name] = re.sub(r"[?#].*$", "", r.url or url).rstrip("/")
            _VARIANTS.setdefault((self.name, str(handle)), {})[name] = parts[name]
        return None

    def _page(self, req, handle, vals, label=None, timeout=None, url=None):
        """(parts, status, payload, error) for one listing request, after
        `_follow`; a handle missing a part is an error, asked nothing. A
        `handle.try` part not yet settled for this handle is
        tried value by value (quietly) until one answers with a status
        outside `handle.accept.status_not`; a success settles it."""
        parts = self._parts(handle)
        if not all(parts.get(p) for p in self._part_names):
            return parts, None, None, http.failed(label, f"handle {handle!r} names no board")
        err = self._follow(handle, parts, label, timeout)
        if err:
            return parts, None, None, err
        tries = self._hspec.get("try") or {}
        key = (self.name, str(handle))
        if not tries or set(tries) <= set(_VARIANTS.get(key, {})):
            return (parts, *self._fetch(req, parts, vals, label, timeout, url))
        wrong = (self._hspec.get("accept") or {}).get("status_not", [])
        (name, values), = tries.items()
        for v in values:
            status, payload, err = self._fetch(req, {**parts, name: v}, vals, None, timeout, url)
            if status is not None and status not in wrong:
                if not err:
                    _VARIANTS.setdefault(key, {})[name] = v
                break
        if err:
            http.failed(label, err)
        return {**parts, name: v}, status, payload, err

    # --- the listing -------------------------------------------------------

    def _label(self, handle, company_name=""):
        return f"{self.name} {company_name or handle}"

    def _entries(self, payload):
        """The postings (dicts) in a listing payload: the first of the
        spec's entry paths holding a list; a wrong shape is []."""
        wanted = (self.listing_spec.get("decoder") or {}).get("entries", "")
        return [e for e in _first_path(payload, wanted, list) or []
                if isinstance(e, dict)]

    def _row(self, parts, entry):
        fs = self.listing_spec["fields"]
        ctx = dict(parts)
        for k, s in fs.items():
            if k.startswith("_"):
                ctx[k] = fields.value(s, entry, ctx)

        def get(k):
            return fields.value(fs.get(k), entry, ctx)

        title = get("title") or ""
        row = {"id": fields.value(fs.get("id"), entry, ctx, strict=True),
               "title": title, "url": get("url") or "",
               "location": get("location") or "",
               "description": get("description") or ""}
        for key, v in (("posted_at", fields.TRANSFORMS["date"](get("posted_at"))),
                       ("remote_hint", get("remote_hint"))):
            if v:
                row[key] = v
        dept = get("department")
        row["head"] = f"{title} {dept}" if dept else title
        return row

    def _next(self, payload, parts):
        """A cursor page's next-page URL, to follow verbatim; None when it
        names none or points outside the listing's own directory (served
        data, not a promise)."""
        nxt = fields.path(payload, self._pager["next"])
        home = fields.fmt(self.listing_spec["url"], parts.get).rsplit("/", 1)[0] + "/"
        return nxt if isinstance(nxt, str) and nxt.lower().startswith(home.lower()) else None

    def _walk(self, handle, label=None, cheap=False, size=None, pages=None):
        """(rows, total) for the board, deduped by id; (None, None) when the
        first request failed. A later page's failure ends the walk with the
        rows so far (reported, so the snapshot reads incomplete). The walk
        ends at an empty page, at the total (else a short page: a server
        may serve fewer than asked), or where a cursor's `has_next` says
        so; one that stops anywhere else notes the snapshot
        capped: every page read with the last still full, a page adding
        nothing new or a cursor refused (`_next`) with no total proving the
        walk complete, or fewer rows than the total. `cheap` reads one page
        of `probe_url` at PROBE_TIMEOUT."""
        spec, pager = self.listing_spec, self._pager
        req = {**spec, "url": spec["probe_url"]} if cheap and spec.get("probe_url") else spec
        size = size or pager.get("size", 0)
        step = pager.get("step") or size
        pages = 1 if cheap or not pager else pages or pager.get("pages", 1)
        timeout = config.PROBE_TIMEOUT if cheap else None
        rows, seen, total, capped, url = [], set(), None, False, None
        for n in range(pages):
            page_label = f"{label} p{n}" if label and pager else label
            parts, _status, payload, err = self._page(
                req, handle, {"$size": size, "$offset": n * step}, page_label, timeout, url)
            if err:
                return (None, None) if n == 0 else (rows, total)
            if n == 0 and pager.get("total"):
                t = fields.path(payload, pager["total"])
                total = t if isinstance(t, int) else None
            entries = self._entries(payload)
            new = [r for r in (self._row(parts, e) for e in entries)
                   if r["id"] is None or r["id"] not in seen]
            seen.update(r["id"] for r in new)
            rows += new
            if not pager:
                break
            if pager["kind"] == "cursor":
                if not fields.path(payload, pager["has_next"]):
                    break
                url = self._next(payload, parts)
                if not url:
                    capped = True
                    break
            elif not entries or (len(rows) >= total if total is not None
                                 else len(entries) < size):
                break
            if not new or n + 1 == pages:
                capped = True
                break
            time.sleep(config.PAGE_DELAY_S)
        if not cheap and (capped or total is not None) and \
                (total is None or len(rows) < total):
            note_capped(total)
        return rows, total

    def listing(self, handle, label=None, cheap=False):
        """Every row on the board, mapped by the spec's fields; [] when the
        listing failed (reported under `label` when given)."""
        return self._walk(handle, label, cheap)[0] or []

    # --- the pulls ---------------------------------------------------------

    def _detail_rows(self):
        """board_jobs' detail callback: a row's body from its detail."""
        if not self.detail_spec:
            return None
        return lambda row: self.description_for(row.get("url"), report=True)

    def jobs(self, handle, company_name="", gate=None, loc_re=None):
        """The sweep: `board_jobs` over the listing, screened by `gate`."""
        rows = self.listing(handle, self._label(handle, company_name))
        return board_jobs(rows, company_name, gate=gate, loc_re=loc_re,
                          fetch_description=self._detail_rows())

    def whole_board(self, company, loc_re=None):
        """The company-vetted pull: every row passing `loc_re`, adapted, with
        each kept row's detail read where the spec is `eager`."""
        handle = self.handle(company)
        if not handle:
            return []
        pager = self._pager
        pages = (config.board_max_pages(company, pager.get("step") or pager["size"],
                                        pager.get("pages", 1)) if pager else None)
        rows = self._walk(handle, self._label(handle), pages=pages)[0] or []
        eager = self._detail_rows() if self.spec.get("eager") else None
        return adapt(board_jobs(rows, "", loc_re=loc_re, fetch_description=eager,
                                max_details=config.WHOLE_BOARD_DETAILS,
                                detail_delay=config.WHOLE_BOARD_DETAIL_DELAY_S),
                     self.name)

    def probe(self, handle):
        """(ok, n) for a guessed handle: one cheap read, n postings on it (the
        listing's own total where it reports one), ok when there is at least
        one. Quiet: a miss is the expected answer."""
        n = self.alive(handle)[1]
        return n > 0, n

    def alive(self, handle):
        """(ok, n) where ok means the board request itself succeeded, empty
        or not: the dead-board check."""
        rows, total = self._walk(handle, cheap=True, size=1)
        return rows is not None, total if total is not None else len(rows or [])

    def local_count(self, handle, is_local):
        """Postings on one cheap read whose listed location `is_local` accepts."""
        return sum(1 for r in self.listing(handle, cheap=True)
                   if is_local(r["location"]))

    def employer_name(self, handle):
        """The employer the listing names on its first posting, or ""."""
        spec = self.spec.get("employer")
        if not spec:
            return ""
        req = {**self.listing_spec, "url": self.listing_spec.get("probe_url")
               or self.listing_spec["url"]}
        _parts, _s, payload, err = self._page(req, handle, {"$size": 1, "$offset": 0},
                                              timeout=config.PROBE_TIMEOUT)
        entries = [] if err else self._entries(payload)
        return str(fields.value(spec, entries[0]) or "").strip() if entries else ""

    # --- one posting -------------------------------------------------------

    def _listing_entries(self, handle):
        """The raw first-page listing entries, memoized for
        config.BOARD_MEMO_S so a board with many stale rows is read once
        per pass. None when the listing is unreadable or empty."""
        key = (self.name, handle)
        hit = _MEMO.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        _parts, _s, payload, err = self._page(
            self.listing_spec, handle, {"$size": self._pager.get("size", 0), "$offset": 0})
        entries = None if err else self._entries(payload) or None
        _MEMO[key] = (time.time() + config.BOARD_MEMO_S, entries)
        return entries

    def _member(self, ref, job_id=None):
        """The listing entry for the posting `ref` names: by the posting id
        its URL carries, else by the row id `job_id` (a platform whose
        posting URLs are all the board's own). None when absent."""
        handle = self._handle_of(ref)
        entries = self._listing_entries(handle) or []
        if ref.get("jid"):
            want = str(ref["jid"]).lower()
            return next((e for e in entries if str(e.get("id", "")).lower() == want), None)
        parts, want = self._parts(handle), str(job_id or "").lower()
        return next((e for e in entries
                     if want and str(self._row(parts, e)["id"] or "").lower() == want), None)

    def row_id(self, handle, url):
        """The id this board's listing gives the posting `url` names, or
        None: the listing's `id` field read with the URL's job_ref parts
        standing in for the listing entry, so it resolves where the spec
        names those parts after the entry keys the id reads."""
        ref = self.job_ref(url)
        return self._row(self._parts(handle), ref)["id"] if ref else None

    def detail(self, ref, report=False):
        """(status, record, error) for the posting `ref` names."""
        own = set(self._part_names) | set(self._hspec.get("follow") or {})
        label = " ".join(str(x) for x in (self.name, self._handle_of(ref), "job",
                                          *(v for k, v in ref.items() if k not in own))
                         if x) if report else None
        status, payload, err = self._fetch(self.detail_spec, ref, label=label)
        rec = _first_path(payload, self.detail_spec.get("record", ""), dict) if payload else None
        return status, rec, err

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
            if not (ref.get("jid") or job_id):
                return None, ""
            if not self._listing_entries(self._handle_of(ref)):
                return None, f"{self.name} api: board unreadable or empty"
            if self._member(ref, job_id) is not None:
                return True, f"{self.name} api: board lists it"
            return False, f"{self.name} api: board no longer lists it"
        status, rec, err = self.detail(ref)
        if status is None:
            return None, f"{self.name} api error: {type(err).__name__}"
        if status in (404, 410):
            return False, f"{self.name} api HTTP {status}"
        if status != 200:
            return None, f"{self.name} api HTTP {status}"
        closed = _reason(self._closure.get("closed"), rec, "closed")
        if closed:
            return False, f"{self.name} api: {closed}"
        live = (_reason(self._closure["open"], rec, "posting live")
                if self._closure.get("open") else "posting live")
        if not live:
            return None, f"{self.name} api: no open signal"
        return True, f"{self.name} api: {live}"


BOARDS = {name: Board(name, spec) for name, spec in config.BOARDS.items()}


def board_for(ats):
    """The engine for `ats`, or None when no spec names it."""
    return BOARDS.get(ats)


def board_for_url(url):
    """The first board, in spec order, whose job_ref reads `url`; or None."""
    return next((b for b in BOARDS.values() if b.owns_url(url)), None)
