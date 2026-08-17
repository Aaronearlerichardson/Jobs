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

# Retired name -> current name. Read-side only: nothing writes these.
ALIASES = {
    "nc_local": LOCAL,
    "neural": SWEEP,
}


def canonical(tag):
    """The current name for a possibly-legacy tag token."""
    t = (tag or "").strip().lower()
    return ALIASES.get(t, t)


def parse(raw):
    """A company row's `tags` string -> a set of canonical tokens."""
    return {canonical(t) for t in (raw or "").split(",") if t.strip()}


def join(tags):
    """A set of tokens -> the stored `tags` string (None when empty)."""
    return ",".join(sorted({canonical(t) for t in tags if t})) or None


def has(raw, tag):
    """True if a company's stored `tags` includes `tag`, legacy names too."""
    return canonical(tag) in parse(raw)
