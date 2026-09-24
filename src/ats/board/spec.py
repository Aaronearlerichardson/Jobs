"""The schema of a `config.BOARDS` spec, and `validate_spec`, which enforces it.

`Board` (engine.py) validates every spec it is built from, so a spec that
breaks the schema fails at import, not on the first board that reads it.
"""

import re

from . import fields

#: Top-level spec keys and what each holds; `validate_spec` enforces them.
SPEC_KEYS = {
    "sweep": bool,          # the lightweight sweep pulls it whole; seeds tags.SWEEP
    "prunable": bool,       # prune_dead_boards may deactivate it; two 404s bury it
    "guess": bool,          # discovery may guess its handle from a company name
    "eager": bool,          # a whole-board pull reads each kept row's detail
    "handle": dict,         # {"columns", "parts", "sep", "try", "accept",
                            #  "follow"}: the store columns joined by sep;
                            # `parts` names the pieces (default the columns);
                            # `try` lists templates for one part, tried until
                            # an answer `accept` allows ("status": those
                            # statuses only; "status_not": none of these;
                            # "total": a listing answer carries an int
                            # total); `follow` maps a part to a URL template
                            # it is the redirect target of; both settled
                            # once per handle
    "job_ref": dict,        # {"re", "parts"}: a stored posting URL -> handle
                            # parts plus the posting's own ("jid", or the
                            # listing entry keys its id reads)
    "listing": (dict, list),  # {"url", "method", "params", "json", "headers",
                            #  "probe_url", "decoder", "pager", "scope",
                            #  "fields"}; a list is alternatives, tried in
                            # order until one yields rows, each later one
                            # taking what it does not set from the first
    "rescue": dict,         # {"when", "unknown", "cap", "cache_days", "free",
                            #  "fields"}: on a scoped pull ("when": "scoped",
                            # the default) or every pull ("always"), a row
                            # whose listed location matches `unknown` takes
                            # its detail's `fields` (default ["location"]),
                            # at most `cap` reads a pull, a location cached
                            # `cache_days`; the field spec `free` is tried
                            # against loc_re first
    "detail": dict,         # {"url", "method", "params", "json", "headers",
                            #  "decoder", "record", "fields", "location"}:
                            # one posting read back
    "closure": dict,        # {"via": "detail" | "listing" | "page", "url",
                            #  "open", "closed", "unmatched"}: each a
                            # condition, or a list of {"when", "why"} rules,
                            # "why" a field spec naming the reason;
                            # `unmatched` is the reason a readable answer
                            # neither rule matches closes the posting; `url`
                            # asks a posting's own endpoint in place of its
                            # detail's
    "employer": (str, dict),  # a field spec naming the employer on a listing entry
    "unlocated": str,       # a location filter's verdict on a row naming no
                            # place: "drop" (the default), "keep", or
                            # "title" (the filter reads its title)
    "detect": list,         # [{"re", "host", "transform", "blocklist",
                            #   "careers_url"}]: how a URL or page names a
                            # board (src.ats.signatures.detect); `re` lists
                            # regexes that must all match, their groups the
                            # handle's parts in order, each through its
                            # `transform` (a fields.TRANSFORMS name or
                            # None); a part in `blocklist` rejects a match;
                            # `careers_url` rebuilds the board's URL from
                            # the parts; `host` is the vendor's host, and an
                            # entry with no `re` names only that
    "canary": dict,         # {"name", "handle", "min_jobs"}: the public
                            # board tools/check_boards.py probes
}
_LISTING_KEYS = {"url", "method", "params", "json", "headers", "probe_url",
                 "decoder", "pager", "scope", "fields"}
_DETAIL_KEYS = {"url", "method", "params", "json", "headers", "decoder",
                "record", "fields", "location"}
ROW_FIELDS = {"id", "title", "url", "location", "description", "posted_at",
              "remote_hint", "department"}
#: json: the payload itself; json_in_html: one JSON value found by `regex`;
#: jsonld: {"postings"}, the page's schema.org JobPostings; html:
#: {"elements", "page"} (decode.elements), or the careers-page reader's
#: where `select` is "$job_links" (`custom.read_page`); atom: {"entries"}.
#: Each kind's keys beside "kind", "entries" and "values".
_DECODERS = {"json": set(), "json_in_html": {"regex"}, "jsonld": set(),
             "html": {"select", "context", "cells", "base"}, "atom": set()}
#: offset: "$offset" steps a page; overlap: it steps "step" < "size", so
#: pages overlap; page: "$page" counts pages from "start" (left off the
#: first page's request under "bare_first"; a page pager may leave `size`
#: unknown); cursor: each page names the next ("next", ended by
#: "has_next"). Any may set "ceiling", the most rows the server serves,
#: or "declared", a field spec naming the last page on each page.
_PAGERS = {"offset", "overlap", "page", "cursor"}
_PAGER_KEYS = {"kind", "size", "step", "pages", "start", "bare_first", "next",
               "has_next", "total", "ceiling", "declared"}
#: A facets scope: the facet groups at `facets` on an unscoped first page,
#: each group's (or value's) `param`, the groups whose param `param_re`
#: matches, and each value's `values` (nested), `id` and `label`.
_SCOPE_KEYS = {"kind", "facets", "param", "param_re", "values", "id", "label"}
#: A param scope: `params` asked of the first listing, unpaged, in place of
#: its own; `located`, the location a row it returned takes when it names
#: none.
_PARAM_SCOPE_KEYS = {"kind", "params", "located"}
_RESCUE_KEYS = {"when", "unknown", "cap", "cache_days", "free", "fields"}
_HANDLE_KEYS = {"columns", "parts", "sep", "try", "accept", "follow"}
_ACCEPT_KEYS = {"status", "status_not", "total"}
_CLOSURE_KEYS = {"via", "url", "open", "closed", "unmatched"}
_VIAS = {"detail", "listing", "page"}
_DETECT_KEYS = {"re", "host", "transform", "blocklist", "careers_url"}
_CANARY_KEYS = {"name", "handle", "min_jobs"}


def validate_spec(name, spec):
    """Raise ValueError when `spec` (a `config.BOARDS` entry) breaks the
    schema: an unknown key, a wrong type, a regex that does not compile,
    or a field spec the grammar cannot read.

    >>> validate_spec("x", {"sweep": True, "listing": {"url": "u", "fields": {"id": "id"}}})
    >>> validate_spec("x", {"sweeps": True})
    Traceback (most recent call last):
    ...
    ValueError: x: unknown key 'sweeps'

    A listing closure reads the first page only, so it never pages:

    >>> validate_spec("x", {"listing": {"url": "u", "fields": {"id": "id"},
    ...                                 "pager": {"kind": "offset", "size": 10}},
    ...                     "closure": {"via": "listing"}})
    Traceback (most recent call last):
    ...
    ValueError: x: closure.via listing reads one page: its listing cannot page
    """
    try:
        _validate(spec)
    except ValueError as e:
        raise ValueError(f"{name}: {e}") from None


def _validate(spec):
    """`validate_spec` without the name prefix."""
    for key, v in spec.items():
        if key not in SPEC_KEYS:
            raise ValueError(f"unknown key {key!r}")
        if not isinstance(v, SPEC_KEYS[key]):
            raise ValueError(f"{key} is {type(v).__name__}")
    listings = alternatives(spec.get("listing"))
    detail = spec.get("detail") or {}
    for part in listings:
        _check_part("listing", _LISTING_KEYS, part)
        _check_pager(part.get("pager"))
    _check_part("detail", _DETAIL_KEYS, detail)
    _check_handle(spec.get("handle") or {})
    scope = (listings[0] if listings else {}).get("scope")
    if scope and not (scope.get("kind") == "facets" and set(scope) == _SCOPE_KEYS
                      or scope.get("kind") == "param" and "params" in scope
                      and set(scope) <= _PARAM_SCOPE_KEYS):
        raise ValueError(f"listing.scope: a facets scope names {sorted(_SCOPE_KEYS)}, "
                         f"a param scope `params` of {sorted(_PARAM_SCOPE_KEYS)}")
    if spec.get("unlocated", "drop") not in ("drop", "keep", "title"):
        raise ValueError("unlocated is drop, keep or title")
    rescue = spec.get("rescue")
    if rescue and (set(rescue) - _RESCUE_KEYS or not isinstance(rescue.get("cap"), int)
                   or rescue.get("when", "scoped") not in ("scoped", "always")
                   or set(rescue.get("fields") or []) - ROW_FIELDS
                   or not ((scope or rescue.get("when") == "always") and detail)):
        raise ValueError("rescue: a detail, a scoped listing unless `when` is always, an int cap")
    _check_closure(spec.get("closure") or {}, listings, detail)
    ref = spec.get("job_ref") or {}
    for where, rx in (("job_ref.re", ref.get("re")),
                      ("rescue.unknown", (rescue or {}).get("unknown")),
                      ("listing.scope.param_re", (scope or {}).get("param_re"))):
        _compiles(where, rx)
    if ref and re.compile(ref["re"]).groups != len(ref["parts"]):
        raise ValueError("job_ref.parts must name every group")
    for t in [alt.get("url") or "" for alt in listings]:
        fields.check_template(t)
    fields.check((rescue or {}).get("free"))
    if spec.get("employer"):
        fields.check(spec["employer"])
    for entry in spec.get("detect", []):
        _check_detect(entry)
    canary = spec.get("canary")
    if canary is not None and (set(canary) - _CANARY_KEYS
                               or not isinstance(canary.get("handle"), str)
                               or not isinstance(canary.get("min_jobs", 1), int)):
        raise ValueError(f"canary: a str handle, an int min_jobs, of {sorted(_CANARY_KEYS)}")


def _compiles(where, rx):
    """Raise ValueError when regex `rx` (None passes) does not compile."""
    try:
        if rx:
            re.compile(rx)
    except re.error as e:
        raise ValueError(f"{where}: {e}") from None


def _check_part(key, allowed, part):
    """A listing alternative's or the detail's keys, fields and decoder."""
    extra = set(part) - allowed
    if extra:
        raise ValueError(f"unknown {key} key(s) {sorted(extra)}")
    extra = set(part.get("fields") or {}) - ROW_FIELDS
    if any(not k.startswith("_") for k in extra):
        raise ValueError(f"unknown {key} field(s) {sorted(extra)}")
    for f in (part.get("fields") or {}).values():
        fields.check(f)
    dec = part.get("decoder") or {}
    kind = dec.get("kind", "json")
    if kind not in _DECODERS or set(dec) - {"kind", "entries", "values"} - _DECODERS[kind]:
        raise ValueError(f"{key}.decoder: a known kind with its own keys")
    if kind == "json_in_html":
        if not isinstance(dec.get("regex"), str):
            raise ValueError(f"{key}.decoder: json_in_html finds its JSON by a regex")
        _compiles(f"{key}.decoder.regex", dec["regex"])
    if kind == "html":
        select = dec.get("select")
        selects = select if isinstance(select, list) else [select]
        if not select or not all(isinstance(s, str) and s for s in selects):
            raise ValueError(f"{key}.decoder: an html decoder selects (a CSS template or a list)")
        for s in selects:
            fields.check_template(s)


def _check_pager(pager):
    """One listing alternative's pager."""
    if not pager:
        return
    kind = pager.get("kind")
    if kind not in _PAGERS or set(pager) - _PAGER_KEYS:
        raise ValueError(f"listing.pager: a known kind, of {sorted(_PAGER_KEYS)}")
    if not (pager.get("size") or kind == "page"):
        raise ValueError("listing.pager needs a size")
    if kind == "overlap" and not 0 < pager.get("step", 0) < pager["size"]:
        raise ValueError("listing.pager: an overlap steps less than a page")
    if kind != "overlap" and "step" in pager:
        raise ValueError("listing.pager: only an overlap pager steps; an offset one steps a page")
    if kind == "cursor" and not (pager.get("next") and pager.get("has_next")):
        raise ValueError("listing.pager: a cursor needs next and has_next")
    if not isinstance(pager.get("ceiling", 0), int):
        raise ValueError("listing.pager.ceiling is a row count")
    fields.check(pager.get("total"))
    fields.check(pager.get("declared"))


def _check_handle(hspec):
    """The handle: its keys, one `try` part of templates, `accept`, `follow`."""
    if set(hspec) - _HANDLE_KEYS:
        raise ValueError(f"handle: of {sorted(_HANDLE_KEYS)}")
    if not all(isinstance(v, str) for v in (hspec.get("follow") or {}).values()):
        raise ValueError("handle.follow maps a part to a URL template")
    tries = hspec.get("try")
    if set(hspec.get("accept") or {}) - _ACCEPT_KEYS or tries is not None and (
            len(tries) != 1 or not all(isinstance(t, list) for t in tries.values())):
        raise ValueError("handle: one `try` part (a list of templates), "
                         "`accept` of status, status_not, total")
    for templates in (tries or {}).values():
        for t in templates:
            fields.check_template(t)


def _check_closure(closure, listings, detail):
    """The closure: its keys, a known `via` its spec can serve, its rules."""
    if set(closure) - _CLOSURE_KEYS:
        raise ValueError(f"closure: of {sorted(_CLOSURE_KEYS)}")
    via = closure.get("via", "detail" if detail else "page")
    if via not in _VIAS:
        raise ValueError(f"closure.via is one of {sorted(_VIAS)}")
    if via == "detail" and not detail:
        raise ValueError("closure.via detail needs a detail")
    if via == "listing" and (not listings or any(alt.get("pager") for alt in listings)):
        raise ValueError("closure.via listing reads one page: its listing cannot page")
    if not isinstance(closure.get("unmatched", ""), str):
        raise ValueError("closure.unmatched is a reason")
    fields.check_template(closure.get("url") or "")
    for c in (closure.get(k) for k in ("open", "closed")):
        for rule in rules(c):
            if set(rule) - {"when", "why"} or "when" not in rule:
                raise ValueError(f"closure rule {rule!r}")
            fields.check({"const": 1, "when": rule["when"]})
            fields.check(rule.get("why"))


def _check_detect(entry):
    """One `detect` entry: its keys, regexes that compile, a transform per group."""
    if not isinstance(entry, dict) or set(entry) - _DETECT_KEYS:
        raise ValueError(f"detect: an entry of {sorted(_DETECT_KEYS)}")
    regexes = entry.get("re", [])
    if not isinstance(regexes, list) or not (regexes or entry.get("host")):
        raise ValueError("detect.re is a list of regexes (or the entry names a host)")
    for rx in regexes:
        _compiles("detect.re", rx)
    groups = sum(re.compile(rx).groups for rx in regexes)
    if regexes and not groups:
        raise ValueError("detect.re captures the handle")
    transforms = entry.get("transform", [None] * groups)
    if len(transforms) != groups or any(t is not None and t not in fields.TRANSFORMS
                                        for t in transforms):
        raise ValueError("detect.transform: a known transform or None per group")
    if not all(isinstance(v, str) for v in entry.get("blocklist", [])):
        raise ValueError("detect.blocklist lists values")
    fields.check_template(entry.get("careers_url") or "")


def alternatives(listing):
    """A spec's listing as its alternatives, each later one completed from
    the first.

    >>> alternatives([{"url": "a", "fields": {}}, {"url": "b"}])
    [{'url': 'a', 'fields': {}}, {'url': 'b', 'fields': {}}]
    """
    alts = listing if isinstance(listing, list) else [listing] if listing else []
    return [alt if i == 0 else {**alts[0], **alt} for i, alt in enumerate(alts)]


def rules(c):
    """A closure condition as its rule list: a list already, else one rule."""
    return c if isinstance(c, list) else [{"when": c}] if c else []
