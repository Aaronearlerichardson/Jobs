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
"""

import html
import re
import time
from urllib.parse import unquote

from src.match.locality import MONTH_ABBRS, location_snippet
from src.net.util import (LOC_TEXT_RE, clean_field, host_of, norm_posted_date,
                          origin_of, stable_id, text_from_html)


def _text(v):
    """A payload value as markup text: a list's items joined, a dict none."""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return "" if isinstance(v, dict) else str(v)


def _after(text, marker):
    """`text` from just past the first whole-word `marker`, or all of it
    when that leaves nothing (a page's chrome ahead of its body).

    >>> _after("Apply Engineer Durham Description Build things", "Description")
    'Build things'
    """
    body = re.sub(rf"^.*?\b{re.escape(marker)}\b", "", text, count=1, flags=re.S).strip()
    return body or text


def _ymd(v):
    """A YYYYMMDD value as an ISO date; None for zeros or anything else.

    >>> _ymd("20260921"), _ymd(20260921), _ymd("00000000"), _ymd("")
    ('2026-09-21', '2026-09-21', None, None)
    """
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", str(v).strip())
    return f"{m[1]}-{m[2]}-{m[3]}" if m and m[1] != "0000" else None


def _colon_location(raw):
    """A colon-delimited, broadest-first location ("US:NC:Morrisville") as
    a readable one, a two-letter state kept beside its city.

    >>> _colon_location("US:NC:Morrisville"), _colon_location("Chapel Hill:NC")
    ('Morrisville, NC, US', 'Chapel Hill, NC')
    >>> _colon_location("US:NC"), _colon_location("Smithfield")
    ('NC, US', 'Smithfield')
    """
    code = re.compile(r"^[A-Za-z]{2}$")
    parts = [p.strip() for p in str(raw).split(":") if p.strip()]
    country = parts.pop(0) if len(parts) > 1 and code.match(parts[0]) else ""
    state = next((parts.pop(i) for i, p in enumerate(parts) if code.match(p)), "")
    return ", ".join(x for x in (", ".join(parts), state, country) if x)


def _host(v):
    """A URL's host, or `v` itself when it is a bare host."""
    return host_of(v) if "://" in str(v) else str(v)


def _host_part(v):
    """The host in `v`, a URL or a bare host (with or without a path),
    lowercased; "" when it names none.

    >>> _host_part("unc.peopleadmin.com"), _host_part("https://Jobs.NCSU.edu/postings/all_jobs.atom")
    ('unc.peopleadmin.com', 'jobs.ncsu.edu')
    >>> _host_part("/postings/all_jobs.atom"), _host_part("")
    ('', '')
    """
    m = re.match(r"^(?:[a-z][a-z0-9+.-]*://)?([^/?#]+)", str(v).strip(), re.I)
    return m.group(1).lower() if m else ""


def _host_key(v):
    """A URL's host, or a bare host, as one id-safe token: the whole host,
    since tenants on their own domains share a first label.

    >>> _host_key("careers.dukehealth.org"), _host_key("https://jobs.ncsu.edu/x")
    ('careers_dukehealth_org', 'jobs_ncsu_edu')
    """
    return re.sub(r"[^a-z0-9]+", "_", _host(v).lower()).strip("_")


def _group(v, regex):
    """The first group of `regex`'s first match in `v`; None when none.

    >>> _group("/job/US-NC-Durham/Eng_R1", "^/job/([^/]+)/"), _group("/x", "^/job/([^/]+)/")
    ('US-NC-Durham', None)
    """
    m = re.search(regex, str(v))
    return m.group(1) if m else None


def _int(v):
    """A count written with thousands separators as an int; None otherwise.

    >>> _int("1,621"), _int(" 25 "), _int("n/a")
    (1621, 25, None)
    """
    s = str(v).replace(",", "").strip()
    return int(s) if s.isdigit() else None


def _url_key(v, n):
    """The last `n` characters of `v` lowercased, each run of anything but
    letters and digits one "-".

    >>> _url_key("https://x.org/Careers/Data_Engineer", "21")
    'careers-data-engineer'
    """
    return re.sub(r"[^a-z0-9]+", "-", str(v).lower())[-int(n):]


def _alnum_tail(v, n):
    """The last `n` lowercase letters and digits of `v`, all else dropped.

    >>> _alnum_tail("https://careers.example.edu", "16")
    'areersexampleedu'
    """
    return re.sub(r"[^a-z0-9]+", "", str(v).lower())[-int(n):]


#: A posting date glued onto a location cell, and whatever follows it.
_DATE_TAIL_RE = re.compile(
    rf"\s+(?:{'|'.join(MONTH_ABBRS)})[a-z]*\.?\s+\d{{1,2}},\s*\d{{4}}\b.*$", re.I)


def _cut_date_tail(v):
    """The place, with a glued-on posting date and whatever follows it (a
    theme's repeated title/location) cut off.

    >>> _cut_date_tail("Durham, NC, US, 27710 Aug 31, 2026 Durham, NC")
    'Durham, NC, US, 27710'
    >>> _cut_date_tail("remote, IT Aug 26, 2026 7637 Europe, remote, I"), _cut_date_tail("Durham, NC")
    ('remote, IT', 'Durham, NC')
    """
    return _DATE_TAIL_RE.sub("", str(v)).strip(" ,-")


def _strip_labels(v):
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


def _loc_text(v):
    """The first "City, ST"-shaped phrase in `v` (net.util.LOC_TEXT_RE), or
    None.

    >>> _loc_text("Research Associate Durham, NC Full time"), _loc_text("Engineer")
    ('Research Associate Durham, NC', None)
    """
    m = LOC_TEXT_RE.search(str(v))
    return m.group(0).strip() if m else None


TRANSFORMS = {
    "html_text": lambda v: text_from_html(_text(v)),
    # A JSON string holding "&lt;p&gt;..." has no markup to strip until it
    # is unescaped: stripped first, the tags come back as TEXT.
    "unescape_html_text": lambda v: text_from_html(html.unescape(_text(v))),
    "date": norm_posted_date,
    "ymd": _ymd,
    "after_marker": _after,
    "before": lambda v, marker: str(v).split(marker, 1)[0].strip(),
    "colon_location": _colon_location,
    "host_key": _host_key,
    "host_label": lambda v: _host(v).split(".", 1)[0],
    "host": _host_part,
    "origin": lambda v: origin_of(str(v)),
    # The site's origin without its scheme or a leading "www.".
    "host_nowww": lambda v: re.sub(r"^https?://(www\.)?", "", origin_of(str(v))),
    "group": _group,
    "dash_space": lambda v: str(v).replace("-", " "),
    "underscore": lambda v: str(v).replace("-", "_"),
    "lower": lambda v: str(v).lower(),
    "unquote": lambda v: unquote(str(v)),
    "rstrip_slash": lambda v: str(v).rstrip("/"),
    "one_line": clean_field,
    "int": _int,
    "alnum_tail": _alnum_tail,
    "snippet": location_snippet,
    "place": lambda v: location_snippet(v, ""),
    "loc_text": _loc_text,
    "cut_date_tail": _cut_date_tail,
    "strip_labels": _strip_labels,
    "url_key": _url_key,
    # A stable id for an entry the listing gives none: never hash(),
    # which Python salts per process.
    "stable_id": stable_id,
}

_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_.\[\]]+)(?::(\d+))?(?:\|([a-z_]+))?\}")
_STEP_RE = re.compile(r"^(.*?)(?:\[(\d*)\])?$")


def merge_locations(primary, extras):
    """One location string carrying every location a posting names: the
    primary field first, then any secondary office/location not already
    present in it.

    >>> merge_locations("Remote", ["Durham, NC", "Remote"])
    'Remote; Durham, NC'
    >>> merge_locations("", ["Tokyo, Japan"])
    'Tokyo, Japan'
    >>> merge_locations(None, [])
    ''

    Notes:
        Multi-location postings often show only "Remote" (or one HQ city)
        up front while the site that matters hides in the secondary list;
        the location regex and the geo logic must see them all.
    """
    loc = (primary or "").strip()
    seen = loc.lower()
    for e in extras or []:
        e = (e or "").strip() if isinstance(e, str) else ""
        if e and e.lower() not in seen:
            loc = f"{loc}; {e}" if loc else e
            seen = loc.lower()
    return loc


def path(obj, p):
    """The value at dotted path `p`; a "[]" step maps over a list, "[n]"
    takes its nth item.

    >>> d = {"a": {"b": 1}, "offices": [{"name": "X"}, {"name": "Y"}, {}]}
    >>> path(d, "a.b"), path(d, "offices[].name"), path(d, "offices[1].name")
    (1, ['X', 'Y', None], 'Y')
    >>> path(d, "a.c"), path(d, "offices[9].name"), path([1], "")
    (None, None, [1])
    """
    if p == "":
        return obj
    cur, mapped = [obj], False
    for step in p.split("."):
        key, index = _STEP_RE.match(step).groups()
        nxt = []
        for c in cur:
            v = c.get(key) if isinstance(c, dict) else None
            if index is None:
                nxt.append(v)
            elif index == "":
                nxt.extend(v if isinstance(v, list) else [])
            else:
                i = int(index)
                nxt.append(v[i] if isinstance(v, list) and i < len(v) else None)
        cur, mapped = nxt, mapped or index == ""
    return cur if mapped else (cur[0] if cur else None)


def _flat(v):
    return [x for item in v for x in _flat(item)] if isinstance(v, list) else [v]


def fmt(template, lookup, strict=False):
    """`template` with each "{name}" / "{name:n}" token filled by `lookup`;
    None when `strict` and a token is empty.

    >>> fmt("gh_{slug}_{id}_{cid:3}", {"slug": "acme", "id": 7, "cid": "abcdef"}.get)
    'gh_acme_7_abc'
    >>> fmt("gh_{slug}_{id}", {"slug": "acme"}.get, strict=True) is None
    True
    >>> fmt("{t}/{t|underscore}", {"t": "vhr-unither"}.get)
    'vhr-unither/vhr_unither'
    """
    empty = []

    def fill(m):
        v = lookup(m.group(1))
        s = "" if v is None else str(v)
        if s and m.group(3):
            s = str(_transform(m.group(3), s))
        if not s.strip():
            empty.append(m.group(1))
        return s[:int(m.group(2))] if m.group(2) else s
    out = _TOKEN_RE.sub(fill, template)
    return None if strict and empty else out


def _transform(name, v):
    name, _, arg = name.partition(":")
    return TRANSFORMS[name](v, arg) if arg else TRANSFORMS[name](v)


def value(spec, entry, ctx=None, strict=False):
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
    >>> value({"of": "missing", "default": "Unknown"}, e)
    'Unknown'
    >>> value({"each": "xs", "do": {"join": ["c", "s"], "sep": ", "}, "skip": {"truthy": "h"}},
    ...       {"xs": [{"c": "Raleigh", "s": "NC"}, {"c": "Austin", "h": True}]})
    ['Raleigh, NC']

    `strict` makes a "format" with an empty token None: an id built from
    a missing key is no id.
    """
    ctx = ctx or {}
    if spec is None:
        return None
    if isinstance(spec, str):
        return ctx[spec] if spec.startswith("_") and spec in ctx else path(entry, spec)
    if "when" in spec and not holds(spec["when"], entry, ctx):
        v = value(spec.get("else"), entry, ctx)
    elif "first" in spec:
        v = next((x for x in (value(s, entry, ctx) for s in spec["first"])
                  if x and not isinstance(x, dict)), None)
    elif "join" in spec:
        parts = [s for s in (str(p).strip() for p in _flat([value(s, entry, ctx)
                                                             for s in spec["join"]])
                             if p and not isinstance(p, dict)) if s]
        v = spec.get("sep", " ").join(parts[:spec.get("max")])
    elif "each" in spec:
        items = path(entry, spec["each"])
        v = [value(spec.get("do", ""), i, ctx)
             for i in (items if isinstance(items, list) else [])
             if not (spec.get("skip") and isinstance(i, dict)
                     and holds(spec["skip"], i, ctx))]
    elif "merge" in spec:
        m = spec["merge"]
        v = merge_locations(value(m["primary"], entry, ctx),
                            _flat(value(m["extras"], entry, ctx)))
    elif "format" in spec:
        v = fmt(spec["format"],
                lambda k: ctx[k] if k in ctx else path(entry, k), strict)
    elif "const" in spec:
        v = spec["const"]
    else:
        v = value(spec.get("of"), entry, ctx)
    if v not in (None, "") and spec.get("transform"):
        v = _transform(spec["transform"], v)
    if not v and "default" in spec:
        v = spec["default"]
    return v


_OPERATORS = {"first", "join", "each", "merge", "format", "const", "of"}
_MODIFIERS = {"sep", "max", "do", "skip", "when", "else", "transform", "default"}
_CONDITIONS = {"any", "all", "truthy", "falsy", "eq", "contains", "past"}


def check(spec):
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
    ops = set(spec) & _OPERATORS
    if len(ops) > 1 or set(spec) - _OPERATORS - _MODIFIERS:
        raise ValueError(f"bad field spec keys {sorted(spec)}")
    if spec.get("transform") and spec["transform"].partition(":")[0] not in TRANSFORMS:
        raise ValueError(f"unknown transform {spec['transform']!r}")
    if "format" in spec:
        check_template(spec["format"])
    for sub in spec.get("first", []) + spec.get("join", []):
        check(sub)
    for key in ("of", "else", "do"):
        check(spec.get(key))
    if "skip" in spec:
        _check_cond(spec["skip"])
    if "merge" in spec:
        check(spec["merge"]["primary"])
        check(spec["merge"]["extras"])
    if "when" in spec:
        _check_cond(spec["when"])


def check_template(template):
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


def _check_cond(cond):
    if not isinstance(cond, dict) or len(cond) != 1 or set(cond) - _CONDITIONS:
        raise ValueError(f"bad condition {cond!r}")
    (op, arg), = cond.items()
    if op in ("any", "all"):
        for c in arg:
            _check_cond(c)
    elif op in ("eq", "contains"):
        check(arg[0])
    else:
        check(arg)


def holds(cond, entry, ctx=None):
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
    (op, arg), = cond.items()
    if op == "any":
        return any(holds(c, entry, ctx) for c in arg)
    if op == "all":
        return all(holds(c, entry, ctx) for c in arg)
    if op == "truthy":
        return bool(value(arg, entry, ctx))
    if op == "falsy":
        return not value(arg, entry, ctx)
    if op == "past":
        v = str(value(arg, entry, ctx) or "")
        return bool(re.match(r"\d{4}-\d{2}-\d{2}", v)) and v[:10] < time.strftime("%Y-%m-%d")
    v = value(arg[0], entry, ctx)
    if op == "eq":
        want = arg[1]
        if isinstance(want, str):
            return v is not None and str(v).lower() == want.lower()
        return type(v) is type(want) and v == want
    if op == "contains":
        needle = arg[1]
        if needle.startswith("$"):
            needle = str(value(needle[1:], entry, ctx) or "")
        hay = " ".join(str(x) for x in _flat(v) if x) if isinstance(v, list) else str(v or "")
        return bool(needle) and needle.lower() in hay.lower()
    raise ValueError(f"unknown condition {op!r}")
