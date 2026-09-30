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
spec schema (spec.py, the models a spec parses into), the field grammar
(fields.py), the decoders (decode.py) and the listing walk (pager.py), all
in src/ats/board/.

The engine is coroutines: a page is decoded and its rows mapped off the
loop (asyncio.to_thread), and a board's pages and detail reads go one at a
time, their delays awaited.

Notes:
    2026-09-18 audit: 124 open rows carried an embedded newline or tab
    (a search-row template wrapping onto two lines, a stray tab between
    city and state), which split triage's one-line DEBUG "drop" record
    into fragments. No open row from an aggregator or feed fetcher
    carried one, so they were left alone rather than given a third copy
    of the rule.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any, Literal, cast

from src import config, runstate
from src.match.locality import LocationRE, location_unknown
from src.net import http
from src.net.http import HEADERS, JSON_HEADERS
from src.net.parallel import SingleFlight
from src.net.util import (cache_dir, clean_field, default_search_text,
                          hashed_cache_path, json_cache_get, json_cache_put, origin_key)
from src.rows import BoardCoords, FetchedJob
from . import decode, fields, pager
from .fields import Reader
from .pager import page_cap, page_size, page_vals, postings, scope_failed, total_of
from .spec import (ROW_FIELDS, Detail, Detect, EngineRow, FillField, Listing, Pager, Rule,
                   Scope, parse)


def loc_ok(loc_re: LocationRE | None, text: str | None) -> bool:
    """Whether `text` passes the location filter (no filter passes all).

    >>> import re
    >>> loc_ok(None, ""), loc_ok(re.compile("NC"), "Durham, NC"), loc_ok(re.compile("NC"), "")
    (True, True, False)
    """
    return loc_re is None or bool(loc_re.search(text or ""))


async def board_jobs(rows: Iterable[EngineRow | None], company_name: str,
                     gate: Callable[..., bool] | None = None,
                     loc_re: LocationRE | None = None,
                     fetch_description: Callable[[EngineRow], Awaitable[str]] | None = None,
                     max_details: int = config.SWEEP_DETAILS,
                     detail_delay: float = config.SWEEP_DETAIL_DELAY_S) -> list[FetchedJob]:
    """Job dicts for the rows that pass `loc_re` and `gate` (see module doc).

    `await fetch_description(row)` is the ATS's detail call, when it has
    one; it runs only for a row the listing gave no body, at most
    `max_details` times per board, `detail_delay` seconds apart. The rows
    are cleaned, placed and screened on their listed text off the loop, so
    `gate` must be pure CPU (no fetch).

    >>> rows = [{"id": "1", "title": "Data Engineer", "url": "u1", "location": "Durham, NC",
    ...          "description": "", "_key": "a"},
    ...         {"id": "2", "title": "Chef", "url": "u2", "location": "Durham, NC",
    ...          "description": ""},
    ...         {"id": "3", "title": "Data Engineer", "url": "u3", "location": "Austin, TX",
    ...          "description": ""},
    ...         None]
    >>> import re
    >>> calls = []
    >>> async def body(row):
    ...     return calls.append(row["id"]) or "body"
    >>> jobs = asyncio.run(board_jobs(rows, "Acme", gate=lambda t, d="": "data" in t.lower(),
    ...                               loc_re=re.compile("NC"), fetch_description=body,
    ...                               detail_delay=0))
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
    >>> jobs = asyncio.run(board_jobs(messy, "Acme"))
    >>> [(j["id"], j["title"], j["location"]) for j in jobs]
    [('4', 'Data Engineer', 'Durham, NC')]
    """
    def screened() -> Iterator[tuple[EngineRow, str, str, bool, bool]]:
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
            yield row, head, desc, gate is None or gate(head), gate is None or gate(head, desc)

    out: list[FetchedJob] = []
    fetched = 0

    async def hydrate(row: EngineRow) -> str | None:
        """The row's body from its detail; None with no detail call or
        `max_details` spent."""
        nonlocal fetched
        if fetch_description is None or fetched >= max_details:
            return None
        fetched += 1
        desc = await fetch_description(row) or ""
        if detail_delay:
            await asyncio.sleep(detail_delay)
        return desc

    for row, head, desc, head_ok, listed_ok in await asyncio.to_thread(list, screened()):
        if not head_ok and not desc and (read := await hydrate(row)) is not None:
            desc, listed_ok = read, gate is None or gate(head, read)
        if not listed_ok:
            continue
        if not desc:
            desc = await hydrate(row) or desc
        # A comprehension cannot build a TypedDict; the keys are the row's own.
        job = cast(FetchedJob, {k: v for k, v in row.items() if not k.startswith("_")})
        job["company"] = company_name
        job["description"] = desc
        out.append(job)
    return out


# --------------------------------------------------------------------------- #
#  The company-fetch shape                                                     #
# --------------------------------------------------------------------------- #

def adapt(jobs: Iterable[FetchedJob], ats: str,
          loc_re: LocationRE | None = None) -> list[FetchedJob]:
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
        # As in board_jobs: a comprehension cannot build a TypedDict.
        job = cast(FetchedJob, {k: j[k] for k in ("id", "title", "url", "location",
                                                 "description", "posted_at", "remote_hint")
                               if k in j})
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
_NAMED: dict[str, Any] = {"$facets": {}, "$search_text": "", "$area": None,
                          "$plain_user_agent": config.PLAIN_USER_AGENT}

#: The run's listings read for closure and deep verify, (ats, handle) ->
#: entries, each read once while concurrent readers of that board wait.
_MEMO = runstate.per_run(SingleFlight)
#: The handle parts `handle.try` and `handle.follow` settled on this run:
#: (ats, handle) -> {part: value}; `_SETTLING` holds a handle's settling.
_VARIANTS: Callable[[], dict[tuple[str, str], dict[str, Any]]] = runstate.per_run(dict)
_SETTLING = runstate.per_run(SingleFlight)


def _fill(tpl: Any, lookup: Callable[[str], Any], vals: dict[str, Any]) -> Any:
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


def _reason(rules: tuple[Rule, ...], rec: dict[str, Any] | None, default: str) -> str | None:
    """The reason the first of closure `rules` holding for `rec` gives (its
    "why", else `default`); None when none holds."""
    for rule in rules if rec is not None else ():
        if fields.holds(rule.when, rec):
            return str(fields.value(rule.why, rec) or default)
    return None


def _readers(fs: dict[str, Any]) -> dict[str, Reader]:
    """A spec's `fields` read once (`fields.reader`): {name: reader} for
    every row field, one reading None where `fs` names none."""
    return {k: fields.reader(fs.get(k)) for k in ROW_FIELDS | set(fs)}


def _detector(entry: Detect) -> tuple[list[re.Pattern[str]], tuple[str | None, ...], set[str]]:
    """A `detect` entry read once: (regexes, transform per group, blocklist)."""
    regexes = [re.compile(rx) for rx in entry.re]
    groups = sum(rx.groups for rx in regexes)
    return regexes, entry.transform or (None,) * groups, {v.lower() for v in entry.blocklist}


def _row_mapper(fs: dict[str, Any],
                free: Any = None) -> Callable[[dict[str, Any], Any], EngineRow]:
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

    def row(parts: dict[str, str], entry: Any) -> EngineRow:
        ctx = dict(parts)
        for k, f in internal:
            ctx[k] = f(entry, ctx)
        t = title(entry, ctx) or ""
        r: EngineRow = {"id": rid(entry, ctx), "title": t, "url": url(entry, ctx) or "",
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
    """One platform, compiled from `config.BOARDS[name]`, parsed into a
    `spec.BoardSpec` (`self.spec`). Every loop lives here; the spec names
    the endpoints and the fields, each field read once, when the board is
    built (`fields.reader`).

    A handle is the store row's board columns joined by the spec's
    separator (`handle`); split on it, its parts fill the spec's templates.
    """

    def __init__(self, name: str, raw: Any) -> None:
        self.name = name
        self.spec = spec = parse(name, raw)
        self._listings = spec.listing
        self.listing_spec = self._listings[0] if self._listings else None
        self.detail_spec = spec.detail
        self.fetchable = bool(self._listings)
        self._hspec = spec.handle
        self._columns, self._part_names, self._sep = (
            self._hspec.columns, self._hspec.names, self._hspec.sep)
        ref = spec.job_ref
        self._ref_re = re.compile(ref.re) if ref else None
        self._ref_parts = ref.parts if ref else ()
        self._closure = spec.closure
        self._pager = self.listing_spec.pager if self.listing_spec else None
        self._rescue_spec = rescue = spec.rescue
        self._always = rescue is not None and rescue.when == "always"
        self._rows = [_row_mapper(alt.fields, rescue.free if rescue else None)
                      for alt in self._listings]
        self._listing_fields = _readers(self.listing_spec.fields if self.listing_spec else {})
        self._detail_fields = _readers(self.detail_spec.fields if self.detail_spec else {})
        self._unknown_re = re.compile(rescue.unknown) if rescue else None
        self._detectors = [_detector(d) for d in spec.detect if d.re]
        self._careers_url = next((d.careers_url for d in spec.detect if d.careers_url), None)

    def __repr__(self) -> str:
        return f"Board({self.name!r})"

    # --- handles and URLs --------------------------------------------------

    def handle(self, company: BoardCoords) -> str | None:
        """The handle string for a store row, or None when a column is empty."""
        vals = [str(company.get(c) or "") for c in self._columns]
        return self._sep.join(vals) if all(vals) else None

    def _parts(self, handle: str) -> dict[str, str]:
        parts = dict(zip(self._part_names, str(handle).split(self._sep)))
        if self._hspec.try_ or self._hspec.follow:
            parts.update(_VARIANTS().get((self.name, str(handle)), {}))
        return parts

    def origin(self, company: BoardCoords | None = None, url: str | None = None) -> str:
        """The origin_key a store row's listing is read from, or the listing
        of the board a posting `url` of this platform names; "" where the row
        alone does not name it (an empty column, or a `handle.try` or
        `handle.follow` part not settled yet that the origin holds).

        >>> BOARDS["icims"].origin({"slug": "careers-acme"})
        'https://careers-acme.icims.com'
        >>> BOARDS["greenhouse"].origin(url="https://job-boards.greenhouse.io/acme/jobs/42")
        'https://boards-api.greenhouse.io'
        """
        ref = url and self.job_ref(url)
        handle = self._handle_of(ref) if ref else self.handle(company or {})
        if not (handle and self.listing_spec):
            return ""
        parts = self._parts(handle)
        # The unsettled parts filled two ways: an origin they move is unknown.
        a, b = (origin_key(fields.fmt(self.listing_spec.url, lambda k: parts.get(k) or fill))
                for fill in "ab")
        return a if a == b else ""

    def job_ref(self, url: str | None,
                company: BoardCoords | None = None) -> dict[str, Any] | None:
        """The named parts a stored posting URL carries, or None when the
        URL is not this platform's. A store row of this platform
        (`company`) supplies its own handle parts in place of the URL's."""
        m = self._ref_re.search(url or "") if self._ref_re else None
        if not m:
            return None
        ref = dict(zip(self._ref_parts, m.groups()))
        handle = self.handle(company) if company and company.get("ats") == self.name else None
        if handle:
            ref.update(zip(self._part_names, handle.split(self._sep)))
        return ref

    def owns_url(self, url: str | None) -> bool:
        return self.job_ref(url) is not None

    @property
    def multi_column(self) -> bool:
        """Whether the handle spans several store columns (a hit carries
        it as a tuple, one value per column)."""
        return len(self._columns) > 1

    def detect(self, blob: str,
               accept: Callable[[str], bool]) -> str | tuple[str, ...] | None:
        """The handle the first of the spec's `detect` matches in `blob`
        names, or None: the first match of an entry's first regex, every
        other regex matching too, no part in its `blocklist`, and a first
        part `accept(part)` allows; the parts transformed, a tuple where
        the handle spans several columns, else joined by `sep`."""
        for regexes, transforms, blocked in self._detectors:
            rest = [rx.search(blob) for rx in regexes[1:]]
            if not all(rest):
                continue
            later = [g for m in rest if m for g in m.groups()]
            for m in regexes[0].finditer(blob):
                raw = [p or "" for p in (*m.groups(), *later)]
                if blocked and blocked.intersection(p.lower() for p in raw) or not accept(raw[0]):
                    continue
                parts = [p if t is None else fields.TRANSFORMS[t](p)
                         for p, t in zip(raw, transforms)]
                return tuple(parts) if self.multi_column else self._sep.join(parts)
        return None

    def careers_url(self, slug: Any, page: str = "") -> str | None:
        """The board's URL rebuilt from a detection's `slug` and the `page`
        that carried it (the spec's `detect.careers_url`), or None where the
        spec rebuilds none."""
        return (fields.fmt(self._careers_url, {"slug": slug, "page": page}.get)
                if self._careers_url else None)

    def _handle_of(self, ref: dict[str, Any]) -> str:
        return self._sep.join(ref.get(p, "") for p in self._part_names)

    # --- requests ----------------------------------------------------------

    async def _fetch(self, req: Listing | Detail, parts: dict[str, str],
                     vals: dict[str, Any] | None = None, label: str | None = None,
                     timeout: tuple[float, float] | None = None, url: str | None = None,
                     hop: bool = True) -> tuple[int | None, Any, str | Exception | None]:
        """(status, payload, error) for one request built from `req` (the
        listing or detail spec) and the handle `parts`; `url`, a served
        next-page URL, replaces the spec's URL and parameters verbatim. A
        parameter given the applied facets ("$facets") carries the values
        applied under its own name; one whose value is None is left off. A
        page whose decoder names another to read in its place (`hop`) is
        followed, once. The answer is decoded off the loop."""
        dec = req.decoder
        vals = {**_NAMED, **(vals or {})}
        kw: dict[str, Any] = {"headers": {**(JSON_HEADERS if dec.kind == "json" else HEADERS),
                                          **_fill(req.headers, parts.get, vals)}}
        if url is None and req.params:
            params = {k: v.get(k) if isinstance(v, dict) else v
                      for k, v in _fill(req.params, parts.get, vals).items()}
            kw["params"] = {k: v for k, v in params.items() if v is not None}
        if url is None and req.json_:
            kw["json"] = _fill(req.json_, parts.get, vals)
        if timeout:
            kw["timeout"] = timeout
        url = url or fields.fmt(req.url, lambda k: parts[k] if k in parts else vals.get(f"${k}"))
        if dec.kind == "json":
            status, payload, err = await http.request_json(req.method, url, label, **kw)
        else:
            status, r, err = await http.request(req.method, url, label, **kw)
            if err or r is None:
                return status, None, err
            try:
                payload = await asyncio.to_thread(
                    lambda: decode.decode(dec, r.text, parts, url, vals["$area"], hop))
            except ValueError:
                return status, None, http.failed(label, "unreadable response")
            if isinstance(payload, dict) and payload.get("hop"):
                return await self._fetch(req, parts, vals, label, timeout, payload["hop"], False)
        if dec.values and payload is not None:
            payload = await asyncio.to_thread(decode.unwrap, payload, dec.values)
        return status, payload, err

    async def _follow(self, handle: str, parts: dict[str, str], label: str | None = None,
                      timeout: tuple[float, float] | None = None) -> str | Exception | None:
        """Settle into `parts` each `handle.follow` part not yet known for
        `handle`: the URL its template redirects to, query and trailing "/"
        dropped (a scheme-less template is https). The error, reported
        under `label`, when one does not answer 200; else None. One caller
        per handle settles at a time; one that waited takes what it settled."""
        key = (self.name, str(handle))
        for name, tpl in self._hspec.follow.items():
            if name in parts:
                continue
            async with _SETTLING().hold(key):
                settled = _VARIANTS().get(key, {})
                if name in settled:
                    parts[name] = settled[name]
                    continue
                url = fields.fmt(tpl, parts.get)
                url = url if re.match(r"(?i)^https?://", url) else f"https://{url}"
                status, r, err = await http.request("GET", url, timeout=timeout)
                if err or status != 200 or r is None:
                    return http.failed(label, f"could not resolve the board's {name}")
                parts[name] = re.sub(r"[?#].*$", "", r.url or url).rstrip("/")
                _VARIANTS().setdefault(key, {})[name] = parts[name]
        return None

    async def _page(self, req: Listing, handle: str, vals: dict[str, Any],
                    label: str | None = None, timeout: tuple[float, float] | None = None,
                    url: str | None = None
                    ) -> tuple[dict[str, Any], int | None, Any, str | Exception | None]:
        """(parts, status, payload, error) for one listing request, after
        `_follow`; a handle missing a part is an error, asked nothing."""
        parts = self._parts(handle)
        if not all(parts.get(p) for p in self._part_names):
            return parts, None, None, http.failed(label, f"handle {handle!r} names no board")
        err = await self._follow(handle, parts, label, timeout)
        if err:
            return parts, None, None, err
        return await self._ask(req, handle, parts, vals, label, timeout, url)

    async def _ask(self, req: Listing | Detail, handle: str, parts: dict[str, str],
                   vals: dict[str, Any] | None = None, label: str | None = None,
                   timeout: tuple[float, float] | None = None, url: str | None = None
                   ) -> tuple[dict[str, Any], int | None, Any, str | Exception | None]:
        """(parts, status, payload, error) for one request. A `handle.try`
        part not yet settled for `handle` is tried value by value, each a
        template over `parts` (quietly), until an answer `_wrong` does not
        reject; one without an error settles it. When none does, the first
        refusal (no answer, 403, 405, 429, 5xx) is the answer: one value's
        404 never outweighs another's timeout. One caller per handle tries
        at a time; one that waited asks once with the value it settled."""
        key = (self.name, str(handle))
        if self._unsettled(key):
            async with _SETTLING().hold(key):
                tries = self._unsettled(key)
                if tries:
                    return await self._settle(key, tries, req, parts, vals, label, timeout, url)
            parts = {**parts, **_VARIANTS()[key]}
        return (parts, *await self._fetch(req, parts, vals, label, timeout, url))

    def _unsettled(self, key: tuple[str, str]) -> dict[str, tuple[str, ...]]:
        return {k: v for k, v in self._hspec.try_.items() if k not in _VARIANTS().get(key, {})}

    async def _settle(self, key: tuple[str, str], tries: dict[str, tuple[str, ...]],
                      req: Listing | Detail, parts: dict[str, str],
                      vals: dict[str, Any] | None, label: str | None, timeout: tuple[float, float] | None,
                      url: str | None
                      ) -> tuple[dict[str, Any], int | None, Any, str | Exception | None]:
        (name, values), = tries.items()
        refused = None
        for v in dict.fromkeys(fields.fmt(t, parts.get) for t in values):
            status, payload, err = await self._fetch(req, {**parts, name: v}, vals, None, timeout,
                                                     url)
            if not self._wrong(req, status, payload):
                if not err:
                    _VARIANTS().setdefault(key, {})[name] = v
                break
            if refused is None and (status is None or status >= 500
                                    or status in (403, 405, 429)):
                refused = v, status, payload, err
        else:
            v, status, payload, err = refused or (v, status, payload, err)
        if err:
            http.failed(label, err)
        return {**parts, name: v}, status, payload, err

    def _wrong(self, req: Listing | Detail, status: int | None, payload: Any) -> bool:
        """Whether an answer rules out the `handle.try` value that got it:
        no answer, a status `handle.accept` refuses, or, under its "total",
        a listing answer with no int total."""
        acc = self._hspec.accept
        if status is None or status in acc.status_not:
            return True
        if acc.status is not None and status not in acc.status:
            return True
        pager = req.pager if isinstance(req, Listing) else None
        return bool(acc.total and pager and pager.total and total_of(pager, payload) is None)

    # --- the listing -------------------------------------------------------

    def _label(self, handle: str, company_name: str = "") -> str:
        return f"{self.name} {company_name or handle}"

    async def _walk(self, handle: str, label: str | None = None, cheap: bool = False,
                    size: int | None = None, pages: int | None = None,
                    vals: dict[str, Any] | None = None, scoped: bool = False,
                    first: bool | str = False, budget: int | None = None,
                    located: bool = False) -> tuple[list[EngineRow] | None, int | None]:
        """(rows, total) from the first listing alternative whose walk
        (`_walk_listing`) yields a posting, else the last one's answer; only
        the first alternative's when `first`."""
        got: tuple[list[EngineRow] | None, int | None] = None, None
        for spec, row in list(zip(self._listings, self._rows))[:1 if first else None]:
            got = await self._walk_listing(spec, row, handle, label, cheap, size, pages, vals,
                                           scoped, budget, located)
            if postings(got[0]):
                break
        return got

    async def _walk_listing(self, spec: Listing,
                            row: Callable[[dict[str, Any], Any], EngineRow], handle: str,
                            label: str | None = None, cheap: bool = False,
                            size: int | None = None, pages: int | None = None,
                            vals: dict[str, Any] | None = None, scoped: bool = False,
                            budget: int | None = None, located: bool = False
                            ) -> tuple[list[EngineRow] | None, int | None]:
        """`pager.walk` over one listing `spec`, its entries mapped by `row`:
        (rows, total), (None, None) when the first request failed. `vals`
        fills named request values (`_NAMED`); `cheap` reads one page (or
        `pages`) at PROBE_TIMEOUT, of `probe_url` unless `located` (the
        rows' locations are read); `budget` rows widen the page cap."""
        paged = spec.pager is not None
        thin = cheap and spec.probe_url and not located
        req = spec.model_copy(update={"url": spec.probe_url}) if thin else spec
        timeout = config.PROBE_TIMEOUT if cheap else None
        dec, vals = spec.decoder, vals or {}

        async def ask(n: int, page: dict[str, Any], url: str | None
                      ) -> tuple[dict[str, Any], Any, str | Exception | None]:
            parts, _status, payload, err = await self._page(
                req, handle, {**vals, **page}, f"{label} p{n}" if label and paged else label,
                timeout, url)
            return parts, payload, err

        def rows_of(parts: dict[str, str], payload: Any) -> tuple[int, list[EngineRow]]:
            entries = decode.entries(payload, dec)
            return len(entries), [row(parts, e) for e in entries]
        return await pager.walk(spec, ask, rows_of, size, pages, cheap, scoped, budget)

    async def listing(self, handle: str, label: str | None = None, cheap: bool = False,
                      rescue_cap: int | None = None) -> list[EngineRow]:
        """Every row on the board, mapped by the spec's fields and, where
        the rescue runs on every pull, filled from at most `rescue_cap`
        details (default the rescue's cap); [] when the listing failed
        (reported under `label` when given). A `cheap` read spends no
        detail read."""
        rows = (await self._walk(handle, label, cheap))[0] or []
        if cheap:
            return rows
        return await self._rescue_all(rows, label, rescue_cap)

    async def _rescue_all(self, rows: list[EngineRow], label: str | None,
                          cap: int | None = None) -> list[EngineRow]:
        """`rows` through an "always" rescue, unscoped; else unchanged."""
        if not self._always:
            return rows
        return await self._rescue(rows, None, False, True, label, cap)

    # --- the locality scope ------------------------------------------------

    async def _scope(self, handle: str, loc_re: LocationRE, sc: Scope,
                     timeout: tuple[float, float] | None = None
                     ) -> tuple[dict[str, Any], str, int | None, list[EngineRow]]:
        """(values, vouched, board_total, board_page) narrowing the listing
        to `loc_re` server-side: the facet values whose label `loc_re`
        matches, read off one unscoped first page (`vouched`: their labels
        joined by " or ", the answer then filtering by facet), else the
        profile's search term (`vouched` ""). `board_total` is that page's
        total, None when unread; `board_page` its rows where the facets
        vouch (what an answer ignoring them repeats), else []. `sc` is the
        first listing's scope."""
        listing = self._listings[0]
        parts, _s, payload, err = await self._page(
            listing, handle, page_vals(self._pager, 0, 1), timeout=timeout)
        applied: dict[str, list[Any]] = {}
        labels: dict[str, None] = {}
        total = None
        if not err:
            total = total_of(self._pager, payload)
            param_re = re.compile(sc.param_re)

            def walk(values: Any, param: Any) -> None:
                for v in values if isinstance(values, list) else []:
                    if not isinstance(v, dict):
                        continue
                    p, label = v.get(sc.param) or param, str(v.get(sc.label) or "")
                    if v.get(sc.id) and loc_re.search(label):
                        applied.setdefault(p, []).append(v[sc.id])
                        labels[label] = None
                    walk(v.get(sc.values), p)
            groups = fields.path(payload, sc.facets)
            for g in groups if isinstance(groups, list) else []:
                if isinstance(g, dict) and param_re.search(str(g.get(sc.param) or "")):
                    walk(g.get(sc.values), g.get(sc.param))
        if applied:
            page = [self._rows[0](parts, e) for e in decode.entries(payload, listing.decoder)]
            return {"$facets": applied, "$search_text": ""}, " or ".join(labels), total, page
        return {"$facets": {}, "$search_text": default_search_text()}, "", total, []

    async def _pull(self, handle: str, label: str, loc_re: LocationRE | None = None,
                    budget: int | None = None) -> list[EngineRow]:
        """The board's rows in `loc_re`'s area (all of them when None), the
        walk's page cap widened to cover `budget` rows where given. A
        facets `scope` narrows the listing server-side (the first
        alternative only, where the facets vouch) and keeps every row they
        vouched for, else the rows `_rescue` shows in the area; a scope the
        board ignored (`pager.scope_failed`) keeps listed and free-text
        matches only.
        Otherwise the whole listing; then an "always" rescue, and the area
        filter (`_in_area`)."""
        scope = self._listings[0].scope
        if loc_re is not None and scope:
            vals, vouched, board_total, page = await self._scope(handle, loc_re, scope)
            rows, total = await self._walk(handle, label, vals=vals, scoped=True, first=vouched,
                                           budget=budget)
            rows = rows or []
            pager = cast(Pager, self._pager)     # a scoped listing pages
            cap = page_size(pager) * page_cap(pager, budget, pager.stride)
            fetch = not scope_failed(total, board_total, cap, rows, page)
            if not fetch:
                print(f"    [!] {label}: locality scope came back unnarrowed "
                      f"({len(rows) if total is None else total} of {board_total or '?'} "
                      f"postings) - keeping listed-location matches only, no detail rescue")
            return await self._rescue(rows, loc_re, fetch and vouched, fetch, label)
        rows = (await self._walk(handle, label, vals={"$area": loc_re}, budget=budget))[0] or []
        return [r for r in await self._rescue_all(rows, label) if self._in_area(r, loc_re)]

    def _in_area(self, row: EngineRow, loc_re: LocationRE | None) -> bool:
        """Whether `row` passes `loc_re` (every row passes None): on its
        location, cleaned; one naming no place by the spec's `unlocated`
        rule ("drop" or "keep")."""
        loc = clean_field(row.get("location"))
        if loc or loc_re is None:
            return loc_ok(loc_re, loc)
        return self.spec.unlocated == "keep"

    async def _rescue(self, rows: list[EngineRow], loc_re: LocationRE | None,
                      vouched: str | Literal[False], fetch: bool, label: str | None,
                      cap: int | None = None) -> list[EngineRow]:
        """The rows in `loc_re`'s area, each carrying the location that
        shows it: the listed one; else the rescue's free text (the listed
        one after it in parentheses); else, where `fetch` allows and the
        listed one matches `rescue.unknown`, the detail's (`_rescued`), at
        most `cap` reads (default `rescue.cap`). A row the scope `vouched`
        for (the board's own area search listed it; `vouched` names that
        search's area) is kept whatever its location reads, past the cap on
        its listed text, the area appended where the location names no
        place in it ("London; Cary, NC"). A listed location
        that passes but matches `unknown` is expanded too, within the cap,
        and kept on its listed text past it. A labelled pull says when the
        budget ran out."""
        unknown = self._unknown_re if fetch else None
        rescue = self._rescue_spec
        fill = rescue.fields if rescue else ()
        cap = (rescue.cap if rescue else 0) if cap is None else cap
        spent, out = 0, []
        for row in rows:
            listed, free = row.get("location") or "", row.get("_free") or ""
            vague = bool(unknown and unknown.search(listed) and self.owns_url(row.get("url")))
            if loc_ok(loc_re, listed):
                if vague and spent < cap:
                    spent += 1
                    await self._rescued(row, fill)
            elif loc_ok(loc_re, free):
                row["location"] = f"{free} ({listed})" if listed else free
            elif vague and spent < cap:
                spent += 1
                await self._rescued(row, fill)
                if not (vouched or loc_ok(loc_re, row["location"])):
                    continue
            elif not vouched:
                continue
            if vouched and not loc_ok(loc_re, row.get("location")):
                row["location"] = "; ".join(filter(None, (row.get("location"), vouched)))
            out.append(row)
        if unknown and spent >= cap and label:
            print(f"    [!] {label}: {'location ' if fill == ('location',) else ''}detail "
                  f"budget ({cap}) spent; later rows "
                  f"{'kept unexpanded' if vouched or loc_re is None else 'dropped'}")
        return out

    async def _rescued(self, row: EngineRow, fill: tuple[FillField, ...]) -> None:
        """Fill `row` in place from its posting's detail: each field in
        `fill` the detail names, the row's own kept where it names none.
        The location comes through `_locate`: a cached one costs no read
        and brings nothing else."""
        loc, rec, fs, ctx = await self._locate(row["url"], True)
        if "location" in fill:
            row["location"] = loc or row.get("location") or ""
        for key in fill if rec else ():
            v = fs[key](rec, ctx) if key != "location" else None
            v = fields.TRANSFORMS["date"](v) if key == "posted_at" else v
            if v:
                row[key] = v
        if rec and fill != ("location",):
            await asyncio.sleep(config.PAGE_DELAY_S)

    async def _locate(self, url: str | None, report: bool = False,
                      company: BoardCoords | None = None
                      ) -> tuple[str, dict[str, Any] | None, dict[str, Reader], dict[str, Any]]:
        """(location, record, field readers, job_ref parts) for the posting
        `url` names: the location its detail gives ("" on a miss) and the
        record read, None when the location came from the cache, where a
        found one stays `rescue.cache_days`, keyed by the posting URL."""
        days = self._rescue_spec.cache_days if self._rescue_spec else 0
        path = hashed_cache_path(cache_dir("loc"), cast(str, url)) if days else None
        hit = await asyncio.to_thread(json_cache_get, path, days * 86400) if path else None
        if hit is not None:
            return hit.get("location") or "", None, {}, {}
        rec, fs, ctx = await self._posting(url, report, company)
        loc = (fs["location"](rec, ctx) or "") if rec else ""
        if loc and path:
            await asyncio.to_thread(json_cache_put, path, {"location": loc})
        return loc, rec, fs, ctx

    # --- the pulls ---------------------------------------------------------

    def _detail_rows(self, fill: bool = False
                     ) -> Callable[[EngineRow], Awaitable[str]] | None:
        """board_jobs' detail callback: a row's body from its detail; with
        `fill`, the row also takes the detail's other fields (`_apply`)."""
        if not self.detail_spec:
            return None
        if not fill:
            return lambda row: self.description_for(row.get("url"), report=True)

        async def read(row: EngineRow) -> str:
            rec, fs, ctx = await self._posting(row.get("url"), True)
            if rec:
                self._apply(row, rec, fs, ctx)
            return row.get("description") or ""
        return read

    async def jobs(self, handle: str, company_name: str = "",
                   gate: Callable[..., bool] | None = None,
                   loc_re: LocationRE | None = None) -> list[FetchedJob]:
        """The sweep: `board_jobs` over the listing (`_pull`), screened by
        `gate`; an `eager` platform's detail reads fill the row as the
        whole-board pull's do."""
        rows = await self._pull(handle, self._label(handle, company_name), loc_re)
        return await board_jobs(rows, company_name, gate=gate,
                                fetch_description=self._detail_rows(self.spec.eager),
                                detail_delay=config.SWEEP_DETAIL_DELAY_S)

    async def whole_board(self, company: BoardCoords, loc_re: LocationRE | None = None,
                          validate: bool = False) -> list[FetchedJob]:
        """The company-vetted pull: every row in `loc_re`'s area (`_pull`),
        adapted, each kept row filled from its detail (`_apply`) where the
        spec is `eager`. The walk reads up to `config.BOARD_MAX_ROWS`; a
        `validate` pull (discovery confirming a board and counting its
        local postings) reads the pager's own page budget."""
        handle = self.handle(company)
        if not handle:
            return []
        rows = await self._pull(handle, self._label(handle), loc_re,
                                None if validate else config.BOARD_MAX_ROWS)
        eager = self._detail_rows(True) if self.spec.eager else None
        jobs = await board_jobs(rows, "", fetch_description=eager,
                                max_details=config.WHOLE_BOARD_DETAILS,
                                detail_delay=config.WHOLE_BOARD_DETAIL_DELAY_S)
        return await asyncio.to_thread(adapt, jobs, self.name)

    async def probe(self, handle: str) -> tuple[bool, int]:
        """(ok, n) for a guessed handle: one cheap read, n postings on it (the
        listing's own total where it reports one), ok when there is at least
        one. Quiet: a miss is the expected answer."""
        n = (await self.alive(handle))[1]
        return n > 0, n

    async def alive(self, handle: str, label: str | None = None) -> tuple[bool, int]:
        """(ok, n) where ok means the board request itself succeeded, empty
        or not (the dead-board check), and n counts its postings; a failed
        request is reported under `label` when given."""
        rows, total = await self._walk(handle, label, cheap=True, size=1)
        n = sum(1 for r in rows or [] if r["id"] is not None)
        return rows is not None, total if total is not None else n

    def gone(self, error: str | None) -> bool:
        """Whether `error`, a failed listing read's report, proves the board
        does not exist: an HTTP 404 on a `prunable` spec.

        Notes:
            Greenhouse harvard and cognitotherapeutics 404'd in the
            2026-09-22 harvest and in both web-UI crawls after it, each a
            live GET the three-day grace (store.HARVEST_DEAD_AFTER_DAYS)
            would have kept spending. Workday is not prunable: its tenants
            404 transiently.
        """
        return self.spec.prunable and bool(re.search(r"\bHTTP 404\b", error or ""))

    async def local_count(self, handle: str, loc_re: LocationRE) -> int:
        """Postings on the board in `loc_re`'s area. Where the spec scopes,
        the scoped total (where the facets vouch but the board reports none,
        the postings on the first scoped page), unless the board ignored the
        scope (`scope_failed`) or reports no total the facets vouch for;
        then, and on every other spec, the rows of a cheap read of the
        listing's own URL, LOCAL_COUNT_SAMPLE_PAGES pages, whose listed
        location or free text passes. 0 when the board is unreadable."""
        scope = self._listings[0].scope
        if scope:
            vals, vouched, board_total, page = await self._scope(handle, loc_re, scope,
                                                                 config.PROBE_TIMEOUT)
            rows, total = await self._walk(handle, cheap=True, size=1, vals=vals, first=vouched)
            if rows is None:
                return 0
            pager = cast(Pager, self._pager)     # a scoped listing pages
            cap = page_size(pager) * pager.pages
            if not scope_failed(total, board_total, cap, rows, page):
                if total is not None:
                    return total
                if vouched:
                    return sum(1 for r in rows if r["id"] is not None)
        rows = (await self._walk(handle, cheap=True, pages=config.LOCAL_COUNT_SAMPLE_PAGES,
                                 located=True))[0] or []
        return sum(1 for r in rows
                   if loc_ok(loc_re, r["location"]) or loc_ok(loc_re, r.get("_free") or ""))

    async def employer_name(self, handle: str) -> str:
        """The employer the listing names on its first posting, or ""."""
        spec, listing = self.spec.employer, self._listings[0]
        if not spec:
            return ""
        req = listing.model_copy(update={"url": listing.probe_url or listing.url})
        _parts, _s, payload, err = await self._page(req, handle, page_vals(self._pager, 0, 1),
                                                    timeout=config.PROBE_TIMEOUT)
        entries = [] if err else decode.entries(payload, listing.decoder)
        return str(fields.value(spec, entries[0]) or "").strip() if entries else ""

    # --- one posting -------------------------------------------------------

    async def _listing_entries(self, handle: str) -> list[dict[str, Any]] | None:
        """The raw first-page listing entries, memoized for
        config.BOARD_MEMO_S so a board with many stale rows is read once
        per pass, however many callers ask at once. None when the listing
        is unreadable or empty."""
        listing = self._listings[0]

        async def read() -> list[dict[str, Any]] | None:
            _parts, _s, payload, err = await self._page(
                listing, handle, page_vals(self._pager, 0, page_size(self._pager)))
            return None if err else decode.entries(payload, listing.decoder) or None
        return cast(list[dict[str, Any]] | None,
                    await _MEMO().do((self.name, handle), read, ttl=config.BOARD_MEMO_S))

    async def _member(self, ref: dict[str, Any], job_id: str | None = None
                      ) -> dict[str, Any] | None:
        """The listing entry for the posting `ref` names: by the posting id
        its URL carries, else by the row id `job_id` (a platform whose
        posting URLs are all the board's own), the rows mapped off the
        loop. None when absent."""
        handle = self._handle_of(ref)
        entries = await self._listing_entries(handle) or []
        if ref.get("jid"):
            want = str(ref["jid"]).lower()
            return next((e for e in entries if str(e.get("id", "")).lower() == want), None)
        parts, want = self._parts(handle), str(job_id or "").lower()
        return await asyncio.to_thread(
            lambda: next((e for e in entries
                          if want and str(self._rows[0](parts, e)["id"] or "").lower() == want),
                         None))

    def row_id(self, handle: str, url: str) -> Any:
        """The id this board's listing gives the posting `url` names, or
        None: the listing's `id` field read with the URL's job_ref parts
        standing in for the listing entry, so it resolves where the spec
        names those parts after the entry keys the id reads."""
        ref = self.job_ref(url)
        return self._rows[0](self._parts(handle), ref)["id"] if ref else None

    async def detail(self, ref: dict[str, Any], report: bool = False, url: str | None = None
                     ) -> tuple[int | None, dict[str, Any] | None, str | Exception | None]:
        """(status, record, error) for the posting `ref` names, through the
        handle's settled `try` parts (tried, where unsettled, as a listing
        request is); `url`, a template, replaces the detail's."""
        handle = self._handle_of(ref)
        own = set(self._part_names) | set(self._hspec.follow)
        label = " ".join(str(x) for x in (self.name, handle, "job",
                                          *(v for k, v in ref.items() if k not in own))
                         if x) if report else None
        parts = {**_VARIANTS().get((self.name, handle), {}), **ref}
        spec = cast(Detail, self.detail_spec)   # asked only of a platform with one
        req = spec.model_copy(update={"url": url}) if url else spec
        _parts, status, payload, err = await self._ask(req, handle, parts, label=label)
        return status, decode.record(payload, spec), err

    async def _posting(self, url: str | None, report: bool = False,
                       company: BoardCoords | None = None
                       ) -> tuple[dict[str, Any] | None, dict[str, Reader], dict[str, Any]]:
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
            return (await self.detail(ref, report))[1], self._detail_fields, ref
        return await self._member(ref), self._listing_fields, ref

    async def description_for(self, url: str | None, report: bool = False) -> str:
        """The posting's description, read live; "" on any miss."""
        rec, fs, ctx = await self._posting(url, report)
        return (fs["description"](rec, ctx) or "") if rec else ""

    @property
    def fills_location(self) -> bool:
        """Whether this platform's detail can name a posting's location."""
        return self.detail_spec is not None and self.detail_spec.location != "never"

    def needs_detail(self, job: FetchedJob) -> bool:
        """Whether `hydrate` would fetch anything: no body yet, or a location
        the listing never resolved that this platform's detail can fill
        for the row's URL."""
        if not job.get("description"):
            return True
        return (self.fills_location and location_unknown(job.get("location"))
                and self.owns_url(job.get("url")))

    async def hydrate(self, job: FetchedJob,
                      company: BoardCoords | None = None) -> FetchedJob:
        """Fill, in place, what `needs_detail` says `job` lacks (`_apply`),
        a new body capped at MAX_DESC_CHARS. A bodied row's location alone
        is read through `_locate` where the spec caches locations.
        `company`, the row's store row, names the board (`job_ref`)."""
        if not self.needs_detail(job):
            return job
        if job.get("description") and self._rescue_spec and self._rescue_spec.cache_days:
            job["location"] = ((await self._locate(job.get("url"), True, company))[0]
                               or job.get("location") or "")
            return job
        rec, fs, ctx = await self._posting(job.get("url"), report=True, company=company)
        if rec:
            had = job.get("description")
            self._apply(job, rec, fs, ctx)
            if not had and job.get("description"):
                job["description"] = job["description"][:config.MAX_DESC_CHARS]
        return job

    def _apply(self, job: EngineRow | FetchedJob, rec: dict[str, Any], fs: dict[str, Reader],
               ctx: dict[str, Any]) -> None:
        """Fill `job` in place from its posting's record: the body when it
        has none; the location as `detail.location` allows ("always",
        "if_unknown": `location_unknown`, or "never", the default); a remote hint and
        a posting date it lacks. Only the body is read off a listing entry
        (a platform with no detail)."""
        desc = fs["description"](rec, ctx)
        if desc and not job.get("description"):
            job["description"] = desc
        if not self.detail_spec:
            return
        policy = self.detail_spec.location
        loc = fs["location"](rec, ctx)
        if loc and (policy == "always"
                    or policy == "if_unknown" and location_unknown(job.get("location"))):
            job["location"] = loc
        hint = fs["remote_hint"](rec, ctx)
        if hint and not job.get("remote_hint"):
            job["remote_hint"] = hint
        posted = fields.TRANSFORMS["date"](fs["posted_at"](rec, ctx))
        if posted and not job.get("posted_at"):
            job["posted_at"] = posted

    def page_headers(self, url: str) -> dict[str, str]:
        """The headers a posting's own page is read with: the detail's
        (filled from the URL's `job_ref`) over the shared defaults."""
        headers = self.detail_spec.headers if self.detail_spec else {}
        return {**HEADERS, **_fill(headers, (self.job_ref(url) or {}).get, _NAMED)}

    async def probe_job(self, url: str, job_id: str | None = None) -> tuple[bool | None, str]:
        """(is_open, reason) for a stored posting URL: True live, False
        positively closed, None unverifiable. (None, "") when the URL is not
        this platform's or its closure is judged from the page."""
        ref, via = self.job_ref(url), self.spec.via
        if ref is None or via == "page":
            return None, ""
        if via == "listing":
            if not (ref.get("jid") or job_id):
                return None, ""
            if not await self._listing_entries(self._handle_of(ref)):
                return None, f"{self.name} api: board unreadable or empty"
            if await self._member(ref, job_id) is not None:
                return True, f"{self.name} api: board lists it"
            return False, f"{self.name} api: board no longer lists it"
        status, rec, err = await self.detail(ref, url=self._closure.url)
        if status is None:
            return None, f"{self.name} api error: {type(err).__name__}"
        if status in (404, 410):
            return False, f"{self.name} api HTTP {status}"
        if status != 200:
            return None, f"{self.name} api HTTP {status}"
        closed = _reason(self._closure.closed, rec, "closed")
        if closed:
            return False, f"{self.name} api: {closed}"
        live = (_reason(self._closure.open, rec, "posting live")
                if self._closure.open else "posting live")
        if not live and self._closure.unmatched and not err:
            return False, f"{self.name} api: {self._closure.unmatched}"
        if not live:
            return None, f"{self.name} api: no open signal"
        return True, f"{self.name} api: {live}"


BOARDS = {name: Board(name, spec) for name, spec in config.BOARDS.items()}


def board_for(ats: str | None) -> Board | None:
    """The engine that fetches `ats`; None when no spec names it or its
    spec only detects the platform (a lead, no listing)."""
    board = BOARDS.get(ats) if ats else None
    return board if board and board.fetchable else None


def board_for_url(url: str | None) -> Board | None:
    """The first board, in spec order, whose job_ref reads `url`; or None."""
    return next((b for b in BOARDS.values() if b.owns_url(url)), None)
