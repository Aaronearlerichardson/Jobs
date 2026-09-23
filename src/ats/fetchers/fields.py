"""The field grammar: how a `config.BOARDS` spec names a value in a payload.

A field spec is a PATH or a dict with one operator:

    "a.b"          the value at a dotted path; "a[].b" maps over a list;
                   "" is the entry itself
    first          [spec, ...]: the first truthy value
    join           [spec, ...] (+ "sep", default " "): the truthy values,
                   lists flattened, joined
    merge          {"primary", "extras"}: `merge_locations`
    format         a template: "{name}" reads a handle part, an internal
                   "_" field, or an entry path, in that order; "{x:8}"
                   keeps 8 characters
    const          a literal
    of             a spec whose value a "transform" is applied to

and optionally "when" (a condition; "else" is used when it fails),
"transform" (a name in TRANSFORMS) and "default" (used for a falsy value).
A condition is one of truthy, falsy, eq, contains, any, all.
"""

import html
import re

from src.net.util import norm_posted_date, text_from_html

TRANSFORMS = {
    "html_text": text_from_html,
    # A JSON string holding "&lt;p&gt;..." has no markup to strip until it
    # is unescaped: stripped first, the tags come back as TEXT.
    "unescape_html_text": lambda s: text_from_html(html.unescape(s)),
    "date": norm_posted_date,
}

_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_.\[\]]+)(?::(\d+))?\}")


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
    """The value at dotted path `p`; a "[]" step maps over a list.

    >>> d = {"a": {"b": 1}, "offices": [{"name": "X"}, {"name": "Y"}, {}]}
    >>> path(d, "a.b"), path(d, "offices[].name"), path(d, "a.c"), path([1], "")
    (1, ['X', 'Y', None], None, [1])
    """
    if p == "":
        return obj
    cur, mapped = [obj], False
    for step in p.split("."):
        many = step.endswith("[]")
        key = step[:-2] if many else step
        nxt = []
        for c in cur:
            v = c.get(key) if isinstance(c, dict) else None
            if not many:
                nxt.append(v)
            elif isinstance(v, list):
                nxt.extend(v)
        cur, mapped = nxt, mapped or many
    return cur if mapped else cur[0]


def _flat(v):
    return [x for item in v for x in _flat(item)] if isinstance(v, list) else [v]


def fmt(template, lookup):
    """`template` with each "{name}" / "{name:n}" token filled by `lookup`.

    >>> fmt("gh_{slug}_{id}_{cid:3}", {"slug": "acme", "id": 7, "cid": "abcdef"}.get)
    'gh_acme_7_abc'
    """
    def fill(m):
        v = lookup(m.group(1))
        s = "" if v is None else str(v)
        return s[:int(m.group(2))] if m.group(2) else s
    return _TOKEN_RE.sub(fill, template)


def value(spec, entry, ctx=None):
    """The value `spec` names in `entry`. `ctx` holds the handle parts and
    any "_" fields already computed, which "format" templates read first.

    >>> e = {"title": "Eng", "loc": "Remote", "offices": [{"name": "Durham, NC"}],
    ...      "isRemote": True, "html": "&lt;p&gt;Hi&lt;/p&gt;"}
    >>> value({"merge": {"primary": "loc", "extras": "offices[].name"}}, e)
    'Remote; Durham, NC'
    >>> value({"first": ["nope", "title"]}, e), value({"join": ["title", "loc"], "sep": ", "}, e)
    ('Eng', 'Eng, Remote')
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
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        return path(entry, spec)
    ctx = ctx or {}
    if "when" in spec and not holds(spec["when"], entry, ctx):
        v = value(spec.get("else"), entry, ctx)
    elif "first" in spec:
        v = next((x for x in (value(s, entry, ctx) for s in spec["first"]) if x), None)
    elif "join" in spec:
        parts = _flat([value(s, entry, ctx) for s in spec["join"]])
        v = spec.get("sep", " ").join(str(p) for p in parts if p)
    elif "merge" in spec:
        m = spec["merge"]
        v = merge_locations(value(m["primary"], entry, ctx),
                            _flat(value(m["extras"], entry, ctx)))
    elif "format" in spec:
        v = fmt(spec["format"],
                lambda k: ctx[k] if k in ctx else path(entry, k))
    elif "const" in spec:
        v = spec["const"]
    else:
        v = value(spec.get("of"), entry, ctx)
    if v not in (None, "") and spec.get("transform"):
        v = TRANSFORMS[spec["transform"]](v)
    if not v and "default" in spec:
        v = spec["default"]
    return v


_OPERATORS = {"first", "join", "merge", "format", "const", "of"}
_MODIFIERS = {"sep", "when", "else", "transform", "default"}
_CONDITIONS = {"any", "all", "truthy", "falsy", "eq", "contains"}


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
    if spec.get("transform") and spec["transform"] not in TRANSFORMS:
        raise ValueError(f"unknown transform {spec['transform']!r}")
    for sub in spec.get("first", []) + spec.get("join", []):
        check(sub)
    for key in ("of", "else"):
        check(spec.get(key))
    if "merge" in spec:
        check(spec["merge"]["primary"])
        check(spec["merge"]["extras"])
    if "when" in spec:
        _check_cond(spec["when"])


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
    searches a list's items joined.

    >>> e = {"t": "Remote", "flag": False, "xs": ["a", "Remote US"]}
    >>> holds({"eq": ["t", "remote"]}, e), holds({"eq": ["flag", False]}, e)
    (True, True)
    >>> holds({"eq": ["missing", False]}, e), holds({"contains": ["xs", "remote"]}, e)
    (False, True)
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
    v = value(arg[0], entry, ctx)
    if op == "eq":
        want = arg[1]
        if isinstance(want, str):
            return v is not None and str(v).lower() == want.lower()
        return type(v) is type(want) and v == want
    if op == "contains":
        hay = " ".join(str(x) for x in _flat(v) if x) if isinstance(v, list) else str(v or "")
        return arg[1].lower() in hay.lower()
    raise ValueError(f"unknown condition {op!r}")
