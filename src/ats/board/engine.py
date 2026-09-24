"""The one list -> row -> detail loop behind the board-shaped fetchers.

Every board-shaped ATS shares a shape: a listing yields rows; each row
has an id, a title, a location and maybe an inline description; some
have a per-posting detail call that is worth paying for only when the row
survives the filters. What is genuinely per-platform (the endpoints, the
row mapping, the detail call) lives in its `config.BOARDS` spec;
`board_jobs` does the rest, so the filter order is decided once:

  1. location (`loc_re`) on the LISTED location, before any detail call:
     an out-of-area posting costs one listing row and nothing more;
  2. relevance (`gate`), cheap fields first: `gate(head)` on the title
     (plus the department where the ATS lists one); only a row that fails
     on those, and has no body yet, pays for its description, and
     `gate(head, description)` then decides. No gate keeps every row;
  3. description hydration for what is kept, within `max_details`.

`gate=None, loc_re=None` is a whole-board pull (the company-vetted path in
board/company.py); the unvetted sweep passes `gate=is_relevant`.

A row is a dict with `id`, `title`, `url`, `location`, `description` ("" when
the listing carries none) and optionally `head` (the text the gate screens
first; defaults to the title), `posted_at`, `remote_hint`, plus the spec's
"_"-prefixed fields, which are stripped from the output.

`title` and `location` are run through `net.util.clean_field` before
anything else sees them (the gates, the store, the session log), at each
path's choke point: `board_jobs` for the unvetted SWEEP, `adapt` for the
company-vetted WHOLE-BOARD pull. The feed fetchers (src/ats/feeds/:
rssfeed, getro, remotive, usajobs, ...) reach neither.

`Board` (below) is the one engine every spec'd platform runs on: it reads
`config.BOARDS[ats]` and does the listing, the row mapping, the sweep and
whole-board pulls, hydration, probes and closure verdicts for that
platform, so no module outside the spec names one. `BOARDS` holds one per
spec; `board_for(ats)` and `board_for_url(url)` find them. Its parts: the
spec schema (spec.py), the field grammar (fields.py), the decoders
(decode.py) and the listing walk (pager.py), all in src/ats/board/.

Notes:
    2026-09-18 audit: 124 open rows carried an embedded newline or tab
    (a search-row template wrapping onto two lines, a stray tab between
    city and state), which split triage's one-line DEBUG "drop" record
    into fragments. No open row from an aggregator or feed fetcher
    carried one, so they were left alone rather than given a third copy
    of the rule.
"""

import re
import time

from src import config
from src.match.locality import location_unknown
from src.net import http
from src.net.http import HEADERS, JSON_HEADERS, note_capped
from src.net.util import (cache_dir, clean_field, default_search_text,
                          hashed_cache_path, json_cache_get, json_cache_put,
                          locality_abbr)

from . import decode, fields, pager
from .pager import page_vals, postings, scope_failed
from .spec import ROW_FIELDS, alternatives, rules, validate_spec


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
             "remote_hint")


def adapt(jobs, ats, loc_re=None):
    r"""Job dicts in the company-fetch shape: `ats` named, `company`
    dropped (the store row supplies it), the description capped at
    `config.MAX_DESC_CHARS`.

    >>> adapt([{"id": "x_1", "company": "Acme", "title": "T", "url": "u",
    ...         "location": "Durham, NC", "description": "d", "posted_at": "2026-01-02"}], "x")
    [{'id': 'x_1', 'title': 'T', 'url': 'u', 'location': 'Durham, NC', 'description': 'd', 'posted_at': '2026-01-02', 'ats': 'x'}]

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
        out.append(job)
    return out


# --------------------------------------------------------------------------- #
#  The engine                                                                  #
# --------------------------------------------------------------------------- #

#: The named request values every listing request can use, and their
#: unscoped defaults ("$size", "$offset" and "$page" come from the pager).
#: "$area" is the location regex a pull is filtered by, for a decoder that
#: chooses among the places a row names; "$plain_user_agent" a bare
#: platform UA, for a WAF refusing a Chrome UA without Chrome's client hints.
_NAMED = {"$facets": {}, "$search_text": "", "$area": None,
          "$plain_user_agent": config.PLAIN_USER_AGENT}

#: Listings read for closure and deep verify: (ats, handle) -> (expires, entries).
_MEMO = {}
#: The handle parts `handle.try` and `handle.follow` settled on:
#: (ats, handle) -> {part: value}.
_VARIANTS = {}


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


def _reason(c, rec, default):
    """The reason the first of closure rules `c` holding for `rec` gives
    (its "why", else `default`); None when none holds.

    >>> _reason([{"when": {"falsy": "a"}, "why": {"const": "gone"}}], {}, "closed")
    'gone'
    >>> _reason({"truthy": "a"}, {"a": 1}, "open"), _reason({"truthy": "a"}, {}, "open")
    ('open', None)
    """
    for rule in rules(c) if rec is not None else []:
        if fields.holds(rule["when"], rec):
            return str(fields.value(rule.get("why"), rec) or default)
    return None


def _readers(fs):
    """A spec's `fields` read once (`fields.reader`): {name: reader} for
    every row field, one reading None where `fs` names none."""
    fs = fs or {}
    return {k: fields.reader(fs.get(k)) for k in ROW_FIELDS | set(fs)}


def _detector(entry):
    """A `detect` entry read once: (regexes, transform per group, blocklist)."""
    regexes = [re.compile(rx) for rx in entry["re"]]
    groups = sum(rx.groups for rx in regexes)
    return (regexes, entry.get("transform", [None] * groups),
            {v.lower() for v in entry.get("blocklist", [])})


def _row_mapper(fs, free=None):
    """`fs` (a listing's `fields`) read once, as a callable
    (parts, entry) -> row: the "_" fields computed first, in order, into
    the context the others read; `id` strict; `posted_at` a date; `head`
    the title plus the department; `_free` the rescue's free text."""
    internal = [(k, fields.reader(s)) for k, s in fs.items() if k.startswith("_")]
    rid = fields.reader(fs.get("id"), strict=True)
    title, url, location, description, posted, hint, dept = (
        fields.reader(fs.get(k)) for k in ("title", "url", "location", "description",
                                           "posted_at", "remote_hint", "department"))
    free = fields.reader(free) if free else None

    def row(parts, entry):
        ctx = dict(parts)
        for k, f in internal:
            ctx[k] = f(entry, ctx)
        t = title(entry, ctx) or ""
        r = {"id": rid(entry, ctx), "title": t, "url": url(entry, ctx) or "",
             "location": location(entry, ctx) or "",
             "description": description(entry, ctx) or ""}
        when, why = fields.TRANSFORMS["date"](posted(entry, ctx)), hint(entry, ctx)
        if when:
            r["posted_at"] = when
        if why:
            r["remote_hint"] = why
        d = dept(entry, ctx)
        r["head"] = f"{t} {d}" if d else t
        if free:
            r["_free"] = free(entry, ctx) or ""
        return r
    return row


class Board:
    """One platform, compiled from `config.BOARDS[name]`. Every loop lives
    here; the spec names the endpoints and the fields, each field read
    once, when the board is built (`fields.reader`).

    A handle is the store row's board columns joined by the spec's
    separator (`handle`); split on it, its parts fill the spec's templates.
    """

    def __init__(self, name, spec):
        validate_spec(name, spec)
        self.name, self.spec = name, spec
        self._listings = alternatives(spec.get("listing"))
        self.listing_spec = self._listings[0] if self._listings else {}
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
        self._rescue_spec = spec.get("rescue") or {}
        free = self._rescue_spec.get("free")
        self._rows = [_row_mapper(alt.get("fields") or {}, free) for alt in self._listings]
        self._listing_fields = _readers(self.listing_spec.get("fields"))
        self._detail_fields = _readers(self.detail_spec.get("fields"))
        unknown = self._rescue_spec.get("unknown")
        self._unknown_re = re.compile(unknown) if unknown else None
        detect = spec.get("detect", [])
        self._detectors = [_detector(d) for d in detect if d.get("re")]
        self._careers_url = next((d["careers_url"] for d in detect if d.get("careers_url")), None)

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

    def job_ref(self, url, company=None):
        """The named parts a stored posting URL carries, or None when the
        URL is not this platform's. A store row of this platform
        (`company`) supplies its own handle parts in place of the URL's."""
        m = self._ref_re.search(url or "") if self._ref_re else None
        if not m:
            return None
        ref = dict(zip(self._ref_parts, m.groups()))
        handle = self.handle(company) if (company or {}).get("ats") == self.name else None
        if handle:
            ref.update(zip(self._part_names, handle.split(self._sep)))
        return ref

    def owns_url(self, url):
        return self.job_ref(url) is not None

    @property
    def multi_column(self):
        """Whether the handle spans several store columns (a hit carries
        it as a tuple, one value per column)."""
        return len(self._columns) > 1

    def detect(self, blob, accept):
        """The handle the first of the spec's `detect` matches in `blob`
        names, or None: the first match of an entry's first regex, every
        other regex matching too, no part in its `blocklist`, and a first
        part `accept(part)` allows; the parts transformed, a tuple where
        the handle spans several columns, else joined by `sep`."""
        for regexes, transforms, blocked in self._detectors:
            rest = [rx.search(blob) for rx in regexes[1:]]
            if not all(rest):
                continue
            later = [g for m in rest for g in m.groups()]
            for m in regexes[0].finditer(blob):
                raw = [p or "" for p in (*m.groups(), *later)]
                if blocked and blocked.intersection(p.lower() for p in raw) or not accept(raw[0]):
                    continue
                parts = [p if t is None else fields.TRANSFORMS[t](p)
                         for p, t in zip(raw, transforms)]
                return tuple(parts) if self.multi_column else self._sep.join(parts)
        return None

    def careers_url(self, slug):
        """The board's URL rebuilt from a detection's `slug` (the spec's
        `detect.careers_url`), or None where the spec rebuilds none."""
        return fields.fmt(self._careers_url, {"slug": slug}.get) if self._careers_url else None

    def _handle_of(self, ref):
        return self._sep.join(ref.get(p, "") for p in self._part_names)

    # --- requests ----------------------------------------------------------

    def _fetch(self, req, parts, vals=None, label=None, timeout=None, url=None,
               hop=True):
        """(status, payload, error) for one request built from `req` (the
        listing or detail spec) and the handle `parts`; `url`, a served
        next-page URL, replaces the spec's URL and parameters verbatim. A
        parameter whose value is None is left off. A page whose decoder
        names another to read in its place (`hop`) is followed, once."""
        dec = req.get("decoder") or {}
        kind = dec.get("kind", "json")
        vals = {**_NAMED, **(vals or {})}
        kw = {"headers": {**(JSON_HEADERS if kind == "json" else HEADERS),
                          **_fill(req.get("headers") or {}, parts.get, vals)}}
        for key in ("params", "json") if url is None else ():
            if req.get(key):
                kw[key] = _fill(req[key], parts.get, vals)
        if kw.get("params"):
            kw["params"] = {k: v for k, v in kw["params"].items() if v is not None}
        if timeout:
            kw["timeout"] = timeout
        url = url or fields.fmt(req["url"], lambda k: parts[k] if k in parts else vals.get(f"${k}"))
        method = req.get("method", "GET")
        if kind == "json":
            status, payload, err = http.request_json(method, url, label, **kw)
        else:
            status, r, err = http.request(method, url, label, **kw)
            if err:
                return status, None, err
            try:
                payload = decode.decode(dec, r.text, parts, url, vals["$area"], hop)
            except ValueError:
                return status, None, http.failed(label, "unreadable response")
            if isinstance(payload, dict) and payload.get("hop"):
                return self._fetch(req, parts, vals, label, timeout, payload["hop"], False)
        if dec.get("values") and payload is not None:
            payload = decode.unwrap(payload, dec["values"])
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
        `_follow`; a handle missing a part is an error, asked nothing."""
        parts = self._parts(handle)
        if not all(parts.get(p) for p in self._part_names):
            return parts, None, None, http.failed(label, f"handle {handle!r} names no board")
        err = self._follow(handle, parts, label, timeout)
        if err:
            return parts, None, None, err
        return self._ask(req, handle, parts, vals, label, timeout, url)

    def _ask(self, req, handle, parts, vals=None, label=None, timeout=None, url=None):
        """(parts, status, payload, error) for one request. A `handle.try`
        part not yet settled for `handle` is tried value by value, each a
        template over `parts` (quietly), until an answer `_wrong` does not
        reject; one without an error settles it. When none does, the first
        refusal (no answer, 403, 405, 429, 5xx) is the answer: one value's
        404 never outweighs another's timeout."""
        key = (self.name, str(handle))
        tries = {k: v for k, v in (self._hspec.get("try") or {}).items()
                 if k not in _VARIANTS.get(key, {})}
        if not tries:
            return (parts, *self._fetch(req, parts, vals, label, timeout, url))
        (name, values), = tries.items()
        refused = None
        for v in dict.fromkeys(fields.fmt(t, parts.get) for t in values):
            status, payload, err = self._fetch(req, {**parts, name: v}, vals, None, timeout, url)
            if not self._wrong(req, status, payload):
                if not err:
                    _VARIANTS.setdefault(key, {})[name] = v
                break
            if refused is None and (status is None or status >= 500
                                    or status in (403, 405, 429)):
                refused = v, status, payload, err
        else:
            v, status, payload, err = refused or (v, status, payload, err)
        if err:
            http.failed(label, err)
        return {**parts, name: v}, status, payload, err

    def _wrong(self, req, status, payload):
        """Whether an answer rules out the `handle.try` value that got it:
        no answer, a status `handle.accept` refuses, or, under its "total",
        a listing answer with no int total."""
        acc = self._hspec.get("accept") or {}
        if status is None or status in acc.get("status_not", ()):
            return True
        if "status" in acc and status not in acc["status"]:
            return True
        total = (req.get("pager") or {}).get("total")
        return bool(acc.get("total") and total
                    and not isinstance(fields.value(total, payload), int))

    # --- the listing -------------------------------------------------------

    def _label(self, handle, company_name=""):
        return f"{self.name} {company_name or handle}"

    def _walk(self, handle, label=None, cheap=False, size=None, pages=None, vals=None,
              scoped=False):
        """(rows, total) from the first listing alternative whose walk
        (`_walk_listing`) yields a posting, else the last one's answer."""
        got = None, None
        for spec, row in zip(self._listings, self._rows):
            got = self._walk_listing(spec, row, handle, label, cheap, size, pages, vals, scoped)
            if postings(got[0]):
                break
        return got

    def _walk_listing(self, spec, row, handle, label=None, cheap=False, size=None, pages=None,
                      vals=None, scoped=False):
        """`pager.walk` over one listing `spec`, its entries mapped by `row`:
        (rows, total), (None, None) when the first request failed. `vals`
        fills named request values (`_NAMED`); `cheap` reads one page (or
        `pages`) of `probe_url` at PROBE_TIMEOUT."""
        paged = bool(spec.get("pager"))
        req = {**spec, "url": spec["probe_url"]} if cheap and spec.get("probe_url") else spec
        timeout = config.PROBE_TIMEOUT if cheap else None
        dec, vals = spec.get("decoder") or {}, vals or {}

        def ask(n, page, url):
            parts, _status, payload, err = self._page(
                req, handle, {**vals, **page}, f"{label} p{n}" if label and paged else label,
                timeout, url)
            return parts, payload, err

        def rows_of(parts, payload):
            entries = decode.entries(payload, dec)
            return len(entries), [row(parts, e) for e in entries]
        return pager.walk(spec, ask, rows_of, size, pages, cheap, scoped)

    def listing(self, handle, label=None, cheap=False, rescue_cap=None):
        """Every row on the board, mapped by the spec's fields and, where
        the rescue runs on every pull, filled from at most `rescue_cap`
        details (default the rescue's cap); [] when the listing failed
        (reported under `label` when given). A `cheap` read spends no
        detail read unless the rescue fills the rows' titles."""
        rows = self._walk(handle, label, cheap)[0] or []
        if cheap and "title" not in self._rescue_spec.get("fields", ["location"]):
            return rows
        return self._rescue_all(rows, label, rescue_cap)

    def _rescue_all(self, rows, label, cap=None):
        """`rows` through an "always" rescue, unscoped; else unchanged."""
        if self._rescue_spec.get("when") != "always":
            return rows
        return self._rescue(rows, None, False, True, label, cap)

    # --- the locality scope ------------------------------------------------

    def _scope(self, handle, loc_re, timeout=None):
        """(values, vouched, board_total) narrowing the listing to `loc_re`
        server-side: the facet values whose label `loc_re` matches, read off
        one unscoped first page (`vouched`: the answer then filters by
        facet), else the profile's search term. `board_total` is that
        page's total, None when unread."""
        sc = self.listing_spec["scope"]
        _parts, _s, payload, err = self._page(
            self.listing_spec, handle, page_vals(self._pager, 0, 1), timeout=timeout)
        applied, total = {}, None
        if not err:
            t = fields.value(self._pager.get("total"), payload)
            total = t if isinstance(t, int) else None
            param_re = re.compile(sc["param_re"])

            def walk(values, param):
                for v in values if isinstance(values, list) else []:
                    if not isinstance(v, dict):
                        continue
                    p = v.get(sc["param"]) or param
                    if v.get(sc["id"]) and loc_re.search(str(v.get(sc["label"]) or "")):
                        applied.setdefault(p, []).append(v[sc["id"]])
                    walk(v.get(sc["values"]), p)
            groups = fields.path(payload, sc["facets"])
            for g in groups if isinstance(groups, list) else []:
                if isinstance(g, dict) and param_re.search(str(g.get(sc["param"]) or "")):
                    walk(g.get(sc["values"]), g.get(sc["param"]))
        if applied:
            return {"$facets": applied, "$search_text": ""}, True, total
        return {"$facets": {}, "$search_text": default_search_text()}, False, total

    def _pull(self, handle, label, loc_re=None, pages=None):
        """The board's rows in `loc_re`'s area (all of them when None). A
        facets `scope` narrows the listing server-side and keeps the rows
        its rescue shows in the area (`_rescue`); a scope the board ignored
        (`pager.scope_failed`) keeps listed and free-text matches only. A
        param scope asks the first listing once (`_scoped_walk`), the whole
        listing read instead when that lists no posting. Otherwise the
        whole listing; then an "always" rescue, and the area filter
        (`_in_area`)."""
        scope = self.listing_spec.get("scope") or {}
        if loc_re is not None and scope.get("kind") == "facets":
            vals, vouched, board_total = self._scope(handle, loc_re)
            rows, total = self._walk(handle, label, pages=pages, vals=vals, scoped=True)
            cap = self._pager["size"] * (pages or self._pager.get("pages", 1))
            fetch = not scope_failed(total, board_total, cap)
            if not fetch:
                print(f"    [!] {label}: locality scope came back unnarrowed ({total} of "
                      f"{board_total or '?'} postings) - keeping listed-location matches "
                      f"only, no detail rescue")
            return self._rescue(rows or [], loc_re, vouched, fetch, label)
        rows = (self._scoped_walk(handle, label, scope)
                if loc_re is not None and scope.get("kind") == "param" else None)
        if rows is None:
            rows = self._walk(handle, label, pages=pages, vals={"$area": loc_re})[0] or []
        return [r for r in self._rescue_all(rows, label) if self._in_area(r, loc_re)]

    def _scoped_walk(self, handle, label, scope):
        """The rows of one unpaged request of the first listing asked with
        the param `scope`'s `params` in place of its own, each naming no
        place taking the scope's `located`; None when a param fills empty
        or the answer lists no posting, [] when it failed (reported)."""
        vals = {**_NAMED, "$locality_abbr": locality_abbr()}
        if not all(_fill(scope["params"], {}.get, vals).values()):
            return None
        spec = {**self.listing_spec, "params": scope["params"], "pager": None}
        rows = self._walk_listing(spec, self._rows[0], handle, label, vals=vals)[0]
        if rows is None or not postings(rows):
            return [] if rows is None else None
        here = self._located()
        for r in rows:
            r["location"] = r["location"] or here
        return rows

    def _in_area(self, row, loc_re):
        """Whether `row` passes `loc_re` (every row passes None): on its
        location, cleaned; one naming no place by the spec's `unlocated`
        rule ("drop", "keep", or "title": on its title)."""
        loc = clean_field(row.get("location"))
        if loc or loc_re is None:
            return loc_ok(loc_re, loc)
        how = self.spec.get("unlocated", "drop")
        return how == "keep" or how == "title" and loc_ok(loc_re, clean_field(row.get("title")))

    def _located(self):
        """The location a param scope gives a row naming none; "" when none."""
        located = (self.listing_spec.get("scope") or {}).get("located")
        return _fill(located, {}.get, {"$locality_abbr": locality_abbr()}) if located else ""

    def _unknown(self, location):
        """Whether `location` names no place (`location_unknown`), or only
        the `_located` label a param scope gave a row that named none."""
        label = self._located()
        return location_unknown(location) or bool(label) and (location or "").strip() == label

    def _rescue(self, rows, loc_re, vouched, fetch, label, cap=None):
        """The rows in `loc_re`'s area, each carrying the location that
        shows it: the listed one; else the rescue's free text (the listed
        one after it in parentheses); else, where `fetch` allows and the
        listed one matches `rescue.unknown`, the detail's (`_rescued`), at
        most `cap` reads (default `rescue.cap`). Past the cap such a row
        stays on its listed text when the scope `vouched` for it, else it
        is dropped. A listed location that passes but matches `unknown` is
        expanded too, within the cap; where the rescue fills a row's title,
        one left past the cap is no posting, and the snapshot is noted
        capped. A labelled pull says when the budget ran out."""
        unknown = self._unknown_re if fetch else None
        fill = self._rescue_spec.get("fields", ["location"])
        cap = self._rescue_spec.get("cap", 0) if cap is None else cap
        spent, left, out = 0, 0, []
        for row in rows:
            listed, free = row.get("location") or "", row.get("_free") or ""
            vague = bool(unknown and unknown.search(listed) and self.owns_url(row.get("url")))
            if loc_ok(loc_re, listed):
                if vague and spent < cap:
                    spent += 1
                    self._rescued(row, fill)
                elif vague:
                    left += 1
            elif loc_ok(loc_re, free):
                row["location"] = f"{free} ({listed})" if listed else free
            elif not vague or (spent >= cap and not vouched):
                continue
            elif spent < cap:
                spent += 1
                self._rescued(row, fill)
                if not loc_ok(loc_re, row["location"]):
                    continue
            out.append(row)
        if unknown and spent >= cap and label:
            print(f"    [!] {label}: {'location ' if fill == ['location'] else ''}detail "
                  f"budget ({cap}) spent; later rows "
                  f"{'kept unexpanded' if vouched else 'dropped'}")
            if left and "title" in fill:
                note_capped(len(rows))
        return out

    def _rescued(self, row, fill):
        """Fill `row` in place from its posting's detail: each field in
        `fill` the detail names, the row's own kept where it names none.
        The location comes through `_locate`: a cached one costs no read
        and brings nothing else."""
        loc, rec, fs, ctx = self._locate(row["url"], True)
        if "location" in fill:
            row["location"] = loc or row.get("location") or ""
        for key in fill if rec else ():
            v = fs[key](rec, ctx) if key != "location" else None
            v = fields.TRANSFORMS["date"](v) if key == "posted_at" else v
            if v:
                row[key] = v
        if rec and fill != ["location"]:
            time.sleep(config.PAGE_DELAY_S)

    def _locate(self, url, report=False, company=None):
        """(location, record, field readers, job_ref parts) for the posting
        `url` names: the location its detail gives ("" on a miss) and the
        record read, None when the location came from the cache, where a
        found one stays `rescue.cache_days`, keyed by the posting URL."""
        days = self._rescue_spec.get("cache_days")
        path = hashed_cache_path(cache_dir("loc"), url) if days else None
        hit = json_cache_get(path, days * 86400) if path else None
        if hit is not None:
            return hit.get("location") or "", None, {}, {}
        rec, fs, ctx = self._posting(url, report, company)
        loc = (fs["location"](rec, ctx) or "") if rec else ""
        if loc and path:
            json_cache_put(path, {"location": loc})
        return loc, rec, fs, ctx

    # --- the pulls ---------------------------------------------------------

    def _detail_rows(self, fill=False):
        """board_jobs' detail callback: a row's body from its detail; with
        `fill`, the row also takes the detail's other fields (`_apply`)."""
        if not self.detail_spec:
            return None
        if not fill:
            return lambda row: self.description_for(row.get("url"), report=True)

        def read(row):
            rec, fs, ctx = self._posting(row.get("url"), True)
            if rec:
                self._apply(row, rec, fs, ctx)
            return row.get("description") or ""
        return read

    def jobs(self, handle, company_name="", gate=None, loc_re=None):
        """The sweep: `board_jobs` over the listing (`_pull`), screened by
        `gate`; an `eager` platform's detail reads fill the row as the
        whole-board pull's do."""
        rows = self._pull(handle, self._label(handle, company_name), loc_re)
        return board_jobs(rows, company_name, gate=gate,
                          fetch_description=self._detail_rows(self.spec.get("eager")))

    def whole_board(self, company, loc_re=None):
        """The company-vetted pull: every row in `loc_re`'s area (`_pull`),
        adapted, each kept row filled from its detail (`_apply`) where the
        spec is `eager`. A pager of known page size reads up to
        `config.board_max_pages`."""
        handle = self.handle(company)
        if not handle:
            return []
        p = self._pager
        pages = (config.board_max_pages(company, p.get("step") or p["size"], p.get("pages", 1))
                 if p.get("size") else None)
        rows = self._pull(handle, self._label(handle), loc_re, pages)
        eager = self._detail_rows(True) if self.spec.get("eager") else None
        return adapt(board_jobs(rows, "", fetch_description=eager,
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
        or not (the dead-board check), and n counts its postings."""
        rows, total = self._walk(handle, cheap=True, size=1)
        n = sum(1 for r in rows or [] if r["id"] is not None)
        return rows is not None, total if total is not None else n

    def local_count(self, handle, loc_re):
        """Postings on the board in `loc_re`'s area. Where the spec scopes,
        the scoped total, unless the board ignored the scope; then, and on
        every other spec, the rows of a cheap read (LOCAL_COUNT_SAMPLE_PAGES
        pages of a scoped spec) whose listed location or free text passes.
        0 when the board is unreadable."""
        pages = None
        if (self.listing_spec.get("scope") or {}).get("kind") == "facets":
            vals, _vouched, board_total = self._scope(handle, loc_re, config.PROBE_TIMEOUT)
            rows, total = self._walk(handle, cheap=True, size=1, vals=vals)
            if rows is None:
                return 0
            if not scope_failed(total, board_total,
                                self._pager["size"] * self._pager.get("pages", 1)):
                return total or 0
            pages = config.LOCAL_COUNT_SAMPLE_PAGES
        rows = self._walk(handle, cheap=True, pages=pages)[0] or []
        return sum(1 for r in rows
                   if loc_ok(loc_re, r["location"]) or loc_ok(loc_re, r.get("_free") or ""))

    def employer_name(self, handle):
        """The employer the listing names on its first posting, or ""."""
        spec = self.spec.get("employer")
        if not spec:
            return ""
        req = {**self.listing_spec, "url": self.listing_spec.get("probe_url")
               or self.listing_spec["url"]}
        _parts, _s, payload, err = self._page(req, handle, page_vals(self._pager, 0, 1),
                                              timeout=config.PROBE_TIMEOUT)
        entries = [] if err else decode.entries(payload, self.listing_spec.get("decoder") or {})
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
            self.listing_spec, handle, page_vals(self._pager, 0, self._pager.get("size", 0)))
        entries = None if err else decode.entries(
            payload, self.listing_spec.get("decoder") or {}) or None
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
                     if want and str(self._rows[0](parts, e)["id"] or "").lower() == want), None)

    def row_id(self, handle, url):
        """The id this board's listing gives the posting `url` names, or
        None: the listing's `id` field read with the URL's job_ref parts
        standing in for the listing entry, so it resolves where the spec
        names those parts after the entry keys the id reads."""
        ref = self.job_ref(url)
        return self._rows[0](self._parts(handle), ref)["id"] if ref else None

    def detail(self, ref, report=False, url=None):
        """(status, record, error) for the posting `ref` names, through the
        handle's settled `try` parts (tried, where unsettled, as a listing
        request is); `url`, a template, replaces the detail's."""
        handle = self._handle_of(ref)
        own = set(self._part_names) | set(self._hspec.get("follow") or {})
        label = " ".join(str(x) for x in (self.name, handle, "job",
                                          *(v for k, v in ref.items() if k not in own))
                         if x) if report else None
        parts = {**_VARIANTS.get((self.name, handle), {}), **ref}
        req = {**self.detail_spec, "url": url} if url else self.detail_spec
        _parts, status, payload, err = self._ask(req, handle, parts, label=label)
        return status, decode.record(payload, self.detail_spec), err

    def _posting(self, url, report=False, company=None):
        """(record, its field readers, the posting's `job_ref` parts) for
        the posting `url` names (with `company`), read live: the detail
        endpoint, or the listing entry where the platform has none. A
        platform with a detail but no `job_ref` (its posting URLs are on
        any host) reads the URL it is handed, as the part `url`.
        (None, {}, {}) on any miss."""
        ref = self.job_ref(url, company)
        if ref is None and self._ref_re is None and self.detail_spec and url:
            ref = {"url": url}
        if not ref:
            return None, {}, {}
        if self.detail_spec:
            return self.detail(ref, report)[1], self._detail_fields, ref
        return self._member(ref), self._listing_fields, ref

    def description_for(self, url, report=False):
        """The posting's description, read live; "" on any miss."""
        rec, fs, ctx = self._posting(url, report)
        return (fs["description"](rec, ctx) or "") if rec else ""

    @property
    def fills_location(self):
        """Whether this platform's detail can name a posting's location."""
        return bool(self.detail_spec) and self.detail_spec.get("location", "if_unknown") != "never"

    def needs_detail(self, job):
        """Whether `hydrate` would fetch anything: no body yet, or a location
        the listing never resolved that this platform's detail can fill
        for the row's URL."""
        if not job.get("description"):
            return True
        return (self.fills_location and self._unknown(job.get("location"))
                and self.owns_url(job.get("url")))

    def hydrate(self, job, company=None):
        """Fill, in place, what `needs_detail` says `job` lacks (`_apply`),
        a new body capped at MAX_DESC_CHARS. A bodied row's location alone
        is read through `_locate` where the spec caches locations.
        `company`, the row's store row, names the board (`job_ref`)."""
        if not self.needs_detail(job):
            return job
        if job.get("description") and self._rescue_spec.get("cache_days"):
            job["location"] = (self._locate(job.get("url"), True, company)[0]
                               or job.get("location"))
            return job
        rec, fs, ctx = self._posting(job.get("url"), report=True, company=company)
        if rec:
            had = job.get("description")
            self._apply(job, rec, fs, ctx)
            if not had and job.get("description"):
                job["description"] = job["description"][:config.MAX_DESC_CHARS]
        return job

    def _apply(self, job, rec, fs, ctx):
        """Fill `job` in place from its posting's record: the body when it
        has none; the location as `detail.location` allows ("always",
        "if_unknown" the default: `_unknown`, or "never"); a remote hint and
        a posting date it lacks. Only the body is read off a listing entry
        (a platform with no detail)."""
        desc = fs["description"](rec, ctx)
        if desc and not job.get("description"):
            job["description"] = desc
        if not self.detail_spec:
            return
        policy = self.detail_spec.get("location", "if_unknown")
        loc = fs["location"](rec, ctx)
        if loc and (policy == "always"
                    or policy == "if_unknown" and self._unknown(job.get("location"))):
            job["location"] = loc
        hint = fs["remote_hint"](rec, ctx)
        if hint and not job.get("remote_hint"):
            job["remote_hint"] = hint
        posted = fields.TRANSFORMS["date"](fs["posted_at"](rec, ctx))
        if posted and not job.get("posted_at"):
            job["posted_at"] = posted

    def page_headers(self, url):
        """The headers a posting's own page is read with: the detail's
        (filled from the URL's `job_ref`) over the shared defaults."""
        return {**HEADERS, **_fill(self.detail_spec.get("headers") or {},
                                   (self.job_ref(url) or {}).get, _NAMED)}

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
        status, rec, err = self.detail(ref, url=self._closure.get("url"))
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
        if not live and self._closure.get("unmatched") and not err:
            return False, f"{self.name} api: {self._closure['unmatched']}"
        if not live:
            return None, f"{self.name} api: no open signal"
        return True, f"{self.name} api: {live}"


BOARDS = {name: Board(name, spec) for name, spec in config.BOARDS.items()}


def board_for(ats):
    """The engine that fetches `ats`; None when no spec names it or its
    spec only detects the platform (a lead, no listing)."""
    board = BOARDS.get(ats)
    return board if board and board.fetchable else None


def board_for_url(url):
    """The first board, in spec order, whose job_ref reads `url`; or None."""
    return next((b for b in BOARDS.values() if b.owns_url(url)), None)
