"""
Canonical company scope TAGS, and the aliases that map older names onto them.

A company row's `tags` column is a comma-joined token set describing HOW the
company should be crawled, not what it does:

    local   — crawl its board through the locality filter (heavyweight ATSes
              whose whole board is expensive to pull, so we ask for a region)
    sweep   — crawl its whole board (lightweight JSON ATSes; also the tag a
              location-agnostic track sweeps on via [tracks.*].store_tag)
    watch   — human-set: fetch this board every crawl and flag anything new
              regardless of rank or geography

The first two used to be named after one user's search — "nc_local" for a
North-Carolina local pass and "neural" for a BCI-focused sweep. They were
never region- or field-specific in behaviour, so they now carry names that
say what they do. `canonical()` keeps existing stores and hand-written
profiles working, and `migrate_sql_expr` rewrites the stored tokens in place.

This module deliberately imports nothing — store, config, and the scrapers
all depend on it.
"""

LOCAL = "local"
SWEEP = "sweep"
WATCH = "watch"
# Roster review queue (2026-09): a company an automated discovery path
# resolved but a person has not confirmed. Written active=0 with this
# tag; confirming drops the tag and applies the mission rule; rejecting
# removes the row and blocklists the name. Never crawled while pending.
PENDING = "pending-review"

# Retired name -> current name. Read-side only: nothing writes these.
ALIASES = {
    "nc_local": LOCAL,
    "neural": SWEEP,
}


def canonical(tag):
    """The current name for a possibly-legacy tag token.

    Case- and whitespace-insensitive; unknown tokens pass through unchanged
    so a hand-written profile is never silently rewritten.

    >>> canonical("watch")
    'watch'
    >>> canonical("  NC_Local ")
    'local'
    >>> canonical("neural")
    'sweep'
    >>> canonical("something-new")
    'something-new'

    Missing input is a token-free company, not an error:

    >>> canonical(None), canonical("")
    ('', '')
    """
    t = (tag or "").strip().lower()
    return ALIASES.get(t, t)


def parse(raw):
    """A company row's `tags` string -> a set of canonical tokens.

    Set order is not part of the contract, so sort before comparing —
    the house style for any function whose output is unordered:

    >>> sorted(parse("local,watch"))
    ['local', 'watch']

    Legacy tokens are canonicalised, and a legacy/current pair collapses
    to one token rather than two:

    >>> sorted(parse("nc_local, neural"))
    ['local', 'sweep']
    >>> sorted(parse("nc_local,local"))
    ['local']

    Blanks and empty fields yield the empty set:

    >>> parse("local,,  ,") == {"local"}
    True
    >>> parse(None) == set()
    True
    """
    return {canonical(t) for t in (raw or "").split(",") if t.strip()}


def join(tags):
    """A set of tokens -> the stored `tags` string (None when empty).

    Output is sorted, so the same token set always stores the same string
    and a row does not churn between crawls:

    >>> join({"watch", "local"})
    'local,watch'
    >>> join(["neural", "nc_local"])
    'local,sweep'

    Empty means SQL NULL, not the empty string — `tags IS NULL` is how the
    store asks "no scope tokens":

    >>> join([]) is None
    True
    >>> join(["", None]) is None
    True

    join/parse round-trip on any canonical token set:

    >>> parse(join({"local", "watch"})) == {"local", "watch"}
    True
    """
    return ",".join(sorted({canonical(t) for t in tags if t})) or None


def has(raw, tag):
    """True if a company's stored `tags` includes `tag`, legacy names too.

    Either side may be legacy — a stored `nc_local` answers to `local`, and
    a caller still asking for `nc_local` gets the row stored as `local`:

    >>> has("local,watch", "watch")
    True
    >>> has("nc_local", "local")
    True
    >>> has("local", "nc_local")
    True
    >>> has("local", "sweep")
    False

    A NULL tags column is simply no tokens:

    >>> has(None, "local")
    False
    """
    return canonical(tag) in parse(raw)
