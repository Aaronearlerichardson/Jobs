"""The field grammar: how a `config.BOARDS` spec names a value in a payload.

A field spec is a PATH or a dict with one operator:

    "a.b"          the value at a dotted path; "a[].b" maps over a list,
                   "a[0].b" takes one item; "" is the entry itself; a name
                   starting "_" reads an internal field already computed
    first          [spec, ...]: the first truthy value that is not a dict
    join           [spec, ...] (+ "sep", default " "; "max" keeps the first
                   n): the truthy values, lists flattened, dicts dropped,
                   each trimmed (a blank one dropped), joined
    each           a list path, with "do" (a spec read on each item) and
                   "skip" (a condition dropping an item): the values
    merge          {"primary", "extras"}: `merge_locations`
    format         a template: "{name}" reads a handle part, an internal
                   "_" field, or an entry path, in that order; "{x:8}"
                   keeps 8 characters, "{x|t}" applies transform t
    const          a literal
    of             a spec whose value a "transform" is applied to

and optionally "when" (a condition; "else" is used when it fails),
"transform" (a name in TRANSFORMS, "name:arg" passing it an argument) and
"default" (used for a falsy value). A condition is one of truthy, falsy,
eq, contains, past, any, all.

`value(spec, entry)` reads one value; `reader(spec)` reads the spec once
and returns the callable the engine keeps, so a row costs no spec
interpretation.
"""

from __future__ import annotations

import functools
import html
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Literal, overload
from urllib.parse import unquote

from src.match.locality import MONTH_ABBRS, location_snippet
from src.net.util import (JSON, LOC_TEXT_RE, clean_field, host_of, norm_posted_date,
                          origin_of, stable_id, text_from_html)

#: A field spec read once (`reader`): (entry, ctx) -> the value.
Reader = Callable[[JSON, Mapping[str, JSON]], JSON]

#: A row field's spec read once (`str_reader`): (entry, ctx) -> its text.
StrReader = Callable[[JSON, Mapping[str, JSON]], str | None]

#: What a transform gives: text, a count (`int`), or None for no value.
Transform = Callable[..., str | int | None]


def _text(v: JSON) -> str:
    """A payload value as markup text: a list's items joined, a dict none."""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return "" if isinstance(v, dict) else str(v)


def _after(text: str, marker: str) -> str:
    """`text` from just past the first whole-word `marker`, or all of it
    when that leaves nothing (a page's chrome ahead of its body).

    >>> _after("Apply Engineer Durham Description Build things", "Description")
    'Build things'
    """
    body = re.sub(rf"^.*?\b{re.escape(marker)}\b", "", text, count=1, flags=re.S).strip()
    return body or text


def _ymd(v: JSON) -> str | None:
    """A YYYYMMDD value as an ISO date; None for zeros or anything else.

    >>> _ymd("20260921"), _ymd(20260921), _ymd("00000000"), _ymd("")
    ('2026-09-21', '2026-09-21', None, None)
    """
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", str(v).strip())
    return f"{m[1]}-{m[2]}-{m[3]}" if m and m[1] != "0000" else None


def _colon_location(raw: JSON) -> str:
    """A colon-delimited, broadest-first location ("US:NC:Morrisville") as
    a readable one, a two-letter state kept beside its city.

    >>> _colon_location("US:NC:Morrisville"), _colon_location("Chapel Hill:NC")
    ('Morrisville, NC, US', 'Chapel Hill, NC')
    >>> _colon_location("US:NC"), _colon_location("Smithfield")
    ('NC, US', 'Smithfield')
    """
    code = re.compile(r"^[A-Za-z]{2}$")
    parts = [p.strip() for p in str(raw).split(":") if p.strip()]
    country, parts = (parts[0], parts[1:]) if len(parts) > 1 and code.match(parts[0]) else ("", parts)
    state = next((parts.pop(i) for i, p in enumerate(parts) if code.match(p)), "")
    return ", ".join(x for x in (", ".join(parts), state, country) if x)


def _host(v: JSON) -> str:
    """A URL's host, or `v` itself when it is a bare host."""
    return host_of(str(v)) if "://" in str(v) else str(v)


def _host_part(v: JSON) -> str:
    """The host in `v`, a URL or a bare host (with or without a path),
    lowercased; "" when it names none.

    >>> _host_part("unc.peopleadmin.com"), _host_part("https://Jobs.NCSU.edu/postings/all_jobs.atom")
    ('unc.peopleadmin.com', 'jobs.ncsu.edu')
    >>> _host_part("/postings/all_jobs.atom"), _host_part("")
    ('', '')
    """
    m = re.match(r"^(?:[a-z][a-z0-9+.-]*://)?([^/?#]+)", str(v).strip(), re.I)
    return m.group(1).lower() if m else ""


def _group(v: JSON, regex: str) -> str | None:
    """The first group of `regex`'s first match in `v`; None when none.

    >>> _group("/job/US-NC-Durham/Eng_R1", "^/job/([^/]+)/"), _group("/x", "^/job/([^/]+)/")
    ('US-NC-Durham', None)
    """
    m = re.search(regex, str(v))
    return m.group(1) if m else None


def _int(v: JSON) -> int | None:
    """A count written with thousands separators as an int; None otherwise.

    >>> _int("1,621"), _int(" 25 "), _int("n/a")
    (1621, 25, None)
    """
    s = str(v).replace(",", "").strip()
    return int(s) if s.isdigit() else None


def _cut_date_tail(v: JSON) -> str:
    """The place, with a glued-on posting date and whatever follows it (a
    theme's repeated title/location) cut off.

    >>> _cut_date_tail("Durham, NC, US, 27710 Aug 31, 2026 Durham, NC")
    'Durham, NC, US, 27710'
    >>> _cut_date_tail("remote, IT Aug 26, 2026 7637 Europe, remote, I"), _cut_date_tail("Durham, NC")
    ('remote, IT', 'Durham, NC')
    """
    date_tail = rf"\s+(?:{'|'.join(MONTH_ABBRS)})[a-z]*\.?\s+\d{{1,2}},\s*\d{{4}}\b.*$"
    return re.sub(date_tail, "", str(v), flags=re.I).strip(" ,-")


def _strip_labels(v: JSON) -> str:
    r"""An anchor's text with the screen-reader label ahead of its title
    ("Requisition Title", or a bare "Title" on its own line) dropped and
    the whitespace collapsed. A bare "Title" is a label only when a line
    break follows it.

    >>> _strip_labels("Requisition Title Data Engineer"), _strip_labels("Title \n \nSr. Engineer")
    ('Data Engineer', 'Sr. Engineer')
    >>> _strip_labels("Title IX Coordinator"), _strip_labels("  Software   Developer ")
    ('Title IX Coordinator', 'Software Developer')

    Notes:
        Three tenants stored 15 rows titled "Title \n \nSr. Process
        Engineer" on 2026-09-01.
    """
    text = re.sub(r"^\s*Requisition Title\s*", "", str(v))
    text = re.sub(r"^\s*Title\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _loc_text(v: JSON) -> str | None:
    """The first "City, ST"-shaped phrase in `v` (net.util.LOC_TEXT_RE), or
    None.

    >>> _loc_text("Research Associate Durham, NC Full time"), _loc_text("Engineer")
    ('Research Associate Durham, NC', None)
    """
    m = LOC_TEXT_RE.search(str(v))
    return m.group(0).strip() if m else None


_UNARY: dict[str, Callable[[JSON], str | int | None]] = {
    "html_text": lambda v: text_from_html(_text(v)),
    # A JSON string holding "&lt;p&gt;..." has no markup to strip until it
    # is unescaped: stripped first, the tags come back as TEXT.
    "unescape_html_text": lambda v: text_from_html(html.unescape(_text(v))),
    # The whole host as one id-safe token: tenants on their own domains
    # share a first label.
    "host_key": lambda v: re.sub(r"[^a-z0-9]+", "_", _host(v).lower()).strip("_"),
    "host_label": lambda v: _host(v).split(".", 1)[0],
    "origin": lambda v: origin_of(str(v)),
    # The site's origin without its scheme or a leading "www.".
    "host_nowww": lambda v: re.sub(r"^https?://(www\.)?", "", origin_of(str(v))),
    "dash_space": lambda v: str(v).replace("-", " "),
    "underscore": lambda v: str(v).replace("-", "_"),
    "lower": lambda v: str(v).lower(),
    "unquote": lambda v: unquote(str(v)),
    "rstrip_slash": lambda v: str(v).rstrip("/"),
}

_ARGUED: dict[str, Callable[[JSON, str], str | int | None]] = {
    "before": lambda v, marker: str(v).split(marker, 1)[0].strip(),
    "alnum_tail": lambda v, n: re.sub(r"[^a-z0-9]+", "", str(v).lower())[-int(n):],
    # The last n characters lowercased, each run of anything but letters
    # and digits one "-".
    "url_key": lambda v, n: re.sub(r"[^a-z0-9]+", "-", str(v).lower())[-int(n):],
}

TRANSFORMS: dict[str, Transform] = {
    **_UNARY,
    **_ARGUED,
    "date": norm_posted_date,
    "ymd": _ymd,
    "after_marker": _after,
    "colon_location": _colon_location,
    "host": _host_part,
    "group": _group,
    "one_line": clean_field,
    "int": _int,
    "snippet": location_snippet,
    "place": functools.partial(location_snippet, default=""),
    "loc_text": _loc_text,
    "cut_date_tail": _cut_date_tail,
    "strip_labels": _strip_labels,
    # A stable id for an entry the listing gives none: never hash(),
    # which Python salts per process.
    "stable_id": stable_id,
}

_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_.\[\]]+)(?::(\d+))?(?:\|([a-z_]+))?\}")


def merge_locations(primary: JSON, extras: list[JSON] | None) -> str:
    """One location string carrying every location a posting names: the
    primary field first, then any secondary office/location not already
    present in it.

    >>> merge_locations("Remote", ["Durham, NC", "Remote"])
    'Remote; Durham, NC'
    >>> merge_locations("", ["Tokyo, Japan"])
    'Tokyo, Japan'
    >>> merge_locations(None, [])
    ''
    >>> merge_locations(["Durham"], ["Tokyo"])
    'Tokyo'

    Notes:
        Multi-location postings often show only "Remote" (or one HQ city)
        up front while the site that matters hides in the secondary list;
        the location regex and the geo logic must see them all.
    """
    loc = (text(primary) or "").strip()
    seen = loc.lower()
    for e in extras or []:
        e = e.strip() if isinstance(e, str) else ""
        if e and e.lower() not in seen:
            loc = f"{loc}; {e}" if loc else e
            seen = loc.lower()
    return loc


def path(obj: JSON, p: str) -> JSON:
    """The value at dotted path `p`; a "[]" step maps over a list, "[n]"
    takes its nth item.

    >>> d = {"a": {"b": 1}, "offices": [{"name": "X"}, {"name": "Y"}, {}]}
    >>> path(d, "a.b"), path(d, "offices[].name"), path(d, "offices[1].name")
    (1, ['X', 'Y', None], 'Y')
    >>> path(d, "a.c"), path(d, "offices[9].name"), path([1], "")
    (None, None, [1])
    """
    return _getter(p)(obj)


def _step(s: str) -> tuple[str, str | None]:
    """One path step as (key, index): `a[3]` -> ('a', '3'), `a[]` -> ('a', ''), `a` -> ('a', None).

    >>> _step("a[3]"), _step("a[]"), _step("a"), _step("a[b]")
    (('a', '3'), ('a', ''), ('a', None), ('a[b]', None))
    """
    m = re.search(r"\[(\d*)\]$", s)
    return (s[:m.start()], m.group(1)) if m else (s, None)


@functools.cache
def _getter(p: str) -> Callable[[JSON], JSON]:
    """`path` for one `p`, parsed once: a callable obj -> value."""
    if p == "":
        return lambda obj: obj
    split = [_step(s) for s in p.split(".")]
    if all(index is None for _key, index in split):
        keys = tuple(key for key, _index in split)

        def chain(obj: JSON) -> JSON:
            for k in keys:
                obj = obj.get(k) if isinstance(obj, dict) else None
            return obj
        return chain
    mapped = any(index == "" for _key, index in split)
    steps = [(key, index if index in (None, "") else int(index)) for key, index in split]

    def walk(obj: JSON) -> JSON:
        cur = [obj]
        for key, index in steps:
            nxt: list[JSON] = []
            for c in cur:
                v = c.get(key) if isinstance(c, dict) else None
                if index is None:
                    nxt.append(v)
                elif isinstance(index, int):
                    nxt.append(v[index] if isinstance(v, list) and index < len(v) else None)
                else:
                    nxt.extend(v if isinstance(v, list) else [])
            cur = nxt
        return cur if mapped else (cur[0] if cur else None)
    return walk


def _flat(v: JSON) -> list[JSON]:
    return [x for item in v for x in _flat(item)] if isinstance(v, list) else [v]


@overload
def fmt(template: str, lookup: Callable[[str], JSON], strict: Literal[False] = False) -> str: ...


@overload
def fmt(template: str, lookup: Callable[[str], JSON], strict: bool) -> str | None: ...


def fmt(template: str, lookup: Callable[[str], JSON], strict: bool = False) -> str | None:
    """`template` with each "{name}" / "{name:n}" token filled by `lookup`;
    None when `strict` and a token is empty.

    >>> fmt("gh_{slug}_{id}_{cid:3}", {"slug": "acme", "id": 7, "cid": "abcdef"}.get)
    'gh_acme_7_abc'
    >>> fmt("gh_{slug}_{id}", {"slug": "acme"}.get, strict=True) is None
    True
    >>> fmt("{t}/{t|underscore}", {"t": "vhr-unither"}.get)
    'vhr-unither/vhr_unither'
    """
    out: list[str] = []
    empty = False
    for literal, name, n, transform in _template(template):
        out.append(literal)
        if name is None:
            continue
        v = lookup(name)
        s = "" if v is None else str(v)
        if s and transform:
            s = str(_transform(transform)(s))
        if not s.strip():
            empty = True
        out.append(s if n is None else s[:n])
    return None if strict and empty else "".join(out)


@functools.cache
def _template(template: str) -> tuple[tuple[str, str | None, int | None, str | None], ...]:
    """`template` parsed once: (literal, token name, n, transform) per
    token, the text after the last as a (literal, None, None, None)."""
    parts: list[tuple[str, str | None, int | None, str | None]] = []
    at = 0
    for m in _TOKEN_RE.finditer(template):
        parts.append((template[at:m.start()], m.group(1),
                      int(m.group(2)) if m.group(2) else None, m.group(3)))
        at = m.end()
    parts.append((template[at:], None, None, None))
    return tuple(parts)


@functools.cache
def _transform(name: str) -> Callable[[JSON], JSON]:
    """The transform `name` ("t", or "t:arg" passing it an argument) as a
    callable, looked up in TRANSFORMS when called."""
    t, _, arg = name.partition(":")
    if arg:
        return lambda v: TRANSFORMS[t](v, arg)
    return lambda v: TRANSFORMS[t](v)


def value(spec: JSON, entry: JSON, ctx: Mapping[str, JSON] | None = None,
          strict: bool = False) -> JSON:
    """The value `spec` names in `entry`. `ctx` holds the handle parts and
    any "_" fields already computed, which "format" templates read first.

    >>> e = {"title": "Eng", "loc": "Remote", "offices": [{"name": "Durham, NC"}],
    ...      "isRemote": True, "html": "&lt;p&gt;Hi&lt;/p&gt;"}
    >>> value({"merge": {"primary": "loc", "extras": "offices[].name"}}, e)
    'Remote; Durham, NC'
    >>> value({"first": ["nope", "title"]}, e), value({"join": ["title", "loc"], "sep": ", "}, e)
    ('Eng', 'Eng, Remote')
    >>> value({"join": ["c", "s", "n"], "sep": ", "}, {"c": "Durham\\t", "s": " ", "n": "US"})
    'Durham, US'
    >>> value({"first": ["offices[0]", "title"]}, e)
    'Eng'
    >>> value({"format": "x_{slug}_{title}"}, e, {"slug": "acme"})
    'x_acme_Eng'
    >>> value({"const": "hint", "when": {"eq": ["isRemote", True]}}, e)
    'hint'
    >>> value({"const": "hint", "when": {"contains": ["offices[].name", "tokyo"]}}, e) is None
    True
    >>> value({"of": "html", "transform": "unescape_html_text"}, e)
    'Hi'
    >>> [value({"of": "u", "transform": t}, {"u": "https://Jobs.NCSU.edu/Careers/Data_Engineer"})
    ...  for t in ("host_key", "url_key:21", "alnum_tail:16")]
    ['jobs_ncsu_edu', 'careers-data-engineer', 'eersdataengineer']
    >>> value({"of": "missing", "default": "Unknown"}, e)
    'Unknown'
    >>> value({"each": "xs", "do": {"join": ["c", "s"], "sep": ", "}, "skip": {"truthy": "h"}},
    ...       {"xs": [{"c": "Raleigh", "s": "NC"}, {"c": "Austin", "h": True}]})
    ['Raleigh, NC']

    `strict` makes a "format" with an empty token None: an id built from
    a missing key is no id. `reader` is the same, read once.
    """
    return reader(spec, strict)(entry, ctx or {})


def text(v: JSON) -> str | None:
    """`v` as the text of a row field: text as is, a number as digits, None
    for None and for what is no text (a bool, a list, a dict).

    >>> text("a"), text(7), text(2.0), text(2.5), text(True), text(["x"]), text(None)
    ('a', '7', '2', '2.5', None, None, None)
    """
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def str_reader(spec: JSON, strict: bool = False) -> StrReader:
    """`reader(spec)` for a row field that holds text (id, title, url,
    location, description, posted_at, remote_hint): its value through `text`."""
    read = reader(spec, strict)
    return lambda entry, ctx: text(read(entry, ctx))


def reader(spec: JSON, strict: bool = False) -> Reader:
    """Field spec `spec` read once: a callable (entry, ctx) -> the value
    `value` would give. The engine builds one per spec field when a board
    is built, so a row costs no spec interpretation."""
    if spec is None:
        return lambda entry, ctx: None
    if isinstance(spec, str):
        return _path_reader(spec)
    spec = _as_map(spec)
    body = _operator(spec, strict)
    when = _condition(_as_map(spec["when"])) if "when" in spec else None
    other = reader(spec.get("else"))
    transform = _transform(_as_str(spec["transform"])) if spec.get("transform") else None
    has_default, default = "default" in spec, spec.get("default")
    if when is None and transform is None and not has_default:
        return body

    def read(entry: JSON, ctx: Mapping[str, JSON]) -> JSON:
        v = body(entry, ctx) if when is None or when(entry, ctx) else other(entry, ctx)
        if transform is not None and v not in (None, ""):
            v = transform(v)
        if not v and has_default:
            v = default
        return v
    return read


@functools.cache
def _path_reader(p: str) -> Reader:
    """A path spec's reader: an internal "_" field from ctx, else `path`."""
    get = _getter(p)
    if p.startswith("_"):
        return lambda entry, ctx: ctx[p] if ctx and p in ctx else get(entry)
    return lambda entry, ctx: get(entry)


# The four `_as_*` guards share a shape on purpose: each narrows to a different
# type, which one generic `isinstance` helper could only return through a cast.
def _as_map(v: JSON) -> Mapping[str, JSON]:
    """`v` as a mapping; `check` has vouched for a loaded spec."""
    if not isinstance(v, Mapping):
        raise TypeError(f"not a mapping: {v!r}")
    return v


def _as_str(v: JSON) -> str:
    """`v` as text; `check` has vouched for a loaded spec."""
    if not isinstance(v, str):
        raise TypeError(f"not text: {v!r}")
    return v


def _as_iter(v: JSON) -> Iterable[JSON]:
    """`v` as something to iterate; `check` has vouched for a loaded spec."""
    if not isinstance(v, Iterable):
        raise TypeError(f"not iterable: {v!r}")
    return v


def _operator(spec: Mapping[str, JSON], strict: bool) -> Reader:
    """The reader of a dict spec's operator (`of` by default), without its
    modifiers."""
    if "first" in spec:
        subs = [reader(s) for s in _as_iter(spec["first"])]

        def first(entry: JSON, ctx: Mapping[str, JSON]) -> JSON:
            for f in subs:
                x = f(entry, ctx)
                if x and not isinstance(x, dict):
                    return x
            return None
        return first
    if "join" in spec:
        subs, sep, cap = ([reader(s) for s in _as_iter(spec["join"])],
                          _as_str(spec.get("sep", " ")), spec.get("max"))
        if cap is not None and not isinstance(cap, int):
            raise TypeError(f"not a count: {cap!r}")

        def join(entry: JSON, ctx: Mapping[str, JSON]) -> str:
            parts = [s for s in (str(p).strip() for p in _flat([f(entry, ctx) for f in subs])
                                 if p and not isinstance(p, dict)) if s]
            return sep.join(parts[:cap])
        return join
    if "each" in spec:
        items_of, do = _getter(_as_str(spec["each"])), reader(spec.get("do", ""))
        skip = _condition(_as_map(spec["skip"])) if spec.get("skip") else None

        def each(entry: JSON, ctx: Mapping[str, JSON]) -> JSON:
            items = items_of(entry)
            return [do(i, ctx) for i in (items if isinstance(items, list) else [])
                    if not (skip and isinstance(i, dict) and skip(i, ctx))]
        return each
    if "merge" in spec:
        merge = _as_map(spec["merge"])
        primary, extras = reader(merge["primary"]), reader(merge["extras"])
        return lambda entry, ctx: merge_locations(primary(entry, ctx), _flat(extras(entry, ctx)))
    if "format" in spec:
        template = _as_str(spec["format"])
        getters = {name: _getter(name) for _lit, name, _n, _t in _template(template)
                   if name is not None}

        def form(entry: JSON, ctx: Mapping[str, JSON]) -> str | None:
            def get(k: str) -> JSON:
                return ctx[k] if ctx and k in ctx else getters[k](entry)
            return fmt(template, get, strict)
        return form
    if "const" in spec:
        const = spec["const"]
        return lambda entry, ctx: const
    return reader(spec.get("of"))


def check(spec: JSON) -> None:
    """Raise ValueError when the grammar cannot read field spec `spec`.

    >>> check({"of": "content", "transform": "html_text"})
    >>> check({"of": "content", "transform": "nope"})
    Traceback (most recent call last):
    ...
    ValueError: unknown transform 'nope'
    """
    if spec is None or isinstance(spec, str):
        return
    if not isinstance(spec, dict):
        raise ValueError(f"field spec {spec!r} is not a path or a dict")
    operators = {"first", "join", "each", "merge", "format", "const", "of"}
    modifiers = {"sep", "max", "do", "skip", "when", "else", "transform", "default"}
    ops = set(spec) & operators
    if len(ops) > 1 or set(spec) - operators - modifiers:
        raise ValueError(f"bad field spec keys {sorted(spec)}")
    if spec.get("transform") and _as_str(spec["transform"]).partition(":")[0] not in TRANSFORMS:
        raise ValueError(f"unknown transform {spec['transform']!r}")
    if "format" in spec:
        check_template(_as_str(spec["format"]))
    for sub in (s for op in ("first", "join") if op in spec for s in _as_iter(spec[op])):
        check(sub)
    for key in ("of", "else", "do"):
        check(spec.get(key))
    if "skip" in spec:
        _check_cond(spec["skip"])
    if "merge" in spec:
        merge = _as_map(spec["merge"])
        check(merge["primary"])
        check(merge["extras"])
    if "when" in spec:
        _check_cond(spec["when"])


def check_template(template: str) -> None:
    """Raise ValueError when a "{x|t}" token in `template` names an
    unknown transform.

    >>> check_template("{tenant|nope}")
    Traceback (most recent call last):
    ...
    ValueError: unknown transform 'nope'
    """
    for m in _TOKEN_RE.finditer(template):
        if m.group(3) and m.group(3) not in TRANSFORMS:
            raise ValueError(f"unknown transform {m.group(3)!r}")


def _check_cond(cond: JSON) -> None:
    conditions = {"any", "all", "truthy", "falsy", "eq", "contains", "past"}
    if not isinstance(cond, dict) or len(cond) != 1 or set(cond) - conditions:
        raise ValueError(f"bad condition {cond!r}")
    (op, arg), = cond.items()
    if op in ("any", "all"):
        for c in _as_iter(arg):
            _check_cond(c)
    elif op in ("eq", "contains"):
        check(list(_as_iter(arg))[0])
    else:
        check(arg)


def holds(cond: Mapping[str, JSON], entry: JSON, ctx: Mapping[str, JSON] | None = None) -> bool:
    """Whether condition `cond` holds for `entry`. `eq` compares strings
    case-insensitively and anything else by type and value; `contains`
    searches a list's items joined, for a needle that is literal or, as
    "$path", another field's value; `past` holds for an ISO date before
    today.

    >>> e = {"t": "Remote", "flag": False, "xs": ["a", "Remote US"], "id": "7",
    ...      "u": "/jobs/7-eng", "end": "2020-01-31"}
    >>> holds({"eq": ["t", "remote"]}, e), holds({"eq": ["flag", False]}, e)
    (True, True)
    >>> holds({"eq": ["missing", False]}, e), holds({"contains": ["xs", "remote"]}, e)
    (False, True)
    >>> holds({"contains": ["u", "$id"]}, e), holds({"past": "end"}, e), holds({"past": "t"}, e)
    (True, True, False)
    >>> holds({"any": [{"truthy": "flag"}, {"falsy": "missing"}]}, e)
    True
    """
    return _condition(cond)(entry, ctx or {})


def _condition(cond: Mapping[str, JSON]) -> Callable[[JSON, Mapping[str, JSON]], bool]:
    """Condition `cond` read once: a callable (entry, ctx) -> `holds`."""
    (op, arg), = cond.items()
    if op in ("any", "all"):
        subs, agg = [_condition(_as_map(c)) for c in _as_iter(arg)], any if op == "any" else all
        return lambda entry, ctx: agg(c(entry, ctx) for c in subs)
    if op in ("truthy", "falsy", "past"):
        f = reader(arg)
        if op == "truthy":
            return lambda entry, ctx: bool(f(entry, ctx))
        if op == "falsy":
            return lambda entry, ctx: not f(entry, ctx)

        def past(entry: JSON, ctx: Mapping[str, JSON]) -> bool:
            v = str(f(entry, ctx) or "")
            return bool(re.match(r"\d{4}-\d{2}-\d{2}", v)) and v[:10] < time.strftime("%Y-%m-%d")
        return past
    pair = list(_as_iter(arg))
    f = reader(pair[0])
    if op == "eq":
        want = pair[1]
        if isinstance(want, str):
            low = want.lower()

            def eq_text(entry: JSON, ctx: Mapping[str, JSON]) -> bool:
                v = f(entry, ctx)
                return v is not None and str(v).lower() == low
            return eq_text

        def eq(entry: JSON, ctx: Mapping[str, JSON]) -> bool:
            v = f(entry, ctx)
            return type(v) is type(want) and v == want
        return eq
    if op == "contains":
        needle = _as_str(pair[1])
        of_needle = reader(needle[1:]) if needle.startswith("$") else None
        low = needle.lower()

        def contains(entry: JSON, ctx: Mapping[str, JSON]) -> bool:
            v = f(entry, ctx)
            n = str(of_needle(entry, ctx) or "").lower() if of_needle else low
            hay = " ".join(str(x) for x in _flat(v) if x) if isinstance(v, list) else str(v or "")
            return bool(n) and n in hay.lower()
        return contains
    raise ValueError(f"unknown condition {op!r}")
