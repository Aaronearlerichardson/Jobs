"""
Single source of truth for LOCALITY detection (the local track's "is this
job in my area?" gate).

The terms come from profile.toml [locality] — not hard-coded — so the local
track works for any region. Module name kept as `nc` for import stability;
NC_RE / NC_HQ_RE / is_nc are the historical public names (region-agnostic
now). fetchers/company, discovery/local_sourcing, discovery/sniffer, and the
local track all delegate here.
"""

import re

import config

# Word-boundary for short/ambiguous tokens (so "nc" doesn't hit "clinic",
# "sf" doesn't hit "surf"); substring for distinctive multi-char names.
_WB = [t for t in config.LOCALITY_WORD_TOKENS if t]
_SUB = [t for t in config.LOCALITY_SUBSTRINGS if t]

# Public: usable directly as the `loc_re` parameter of the company fetchers.
NC_RE = re.compile(
    "|".join([rf"\b{re.escape(t)}\b" for t in _WB]
             + [re.escape(t) for t in _SUB])
    or r"(?!x)x",   # match-nothing when no locality terms are configured
    re.I,
)

# Stricter "<place>, ST" address form — a company-HQ/office signal that holds
# even when a company has zero current openings. Built from every place term
# followed (within a few chars) by a configured state suffix.
_PLACES = [re.escape(t) for t in (_WB + _SUB)]
_SUFFIX = [re.escape(s) for s in config.LOCALITY_STATE_SUFFIX if s]
NC_HQ_RE = re.compile(
    (rf"\b(?:{'|'.join(_PLACES)})\b[\s,.\-]{{0,4}}(?:{'|'.join(_SUFFIX)})\b"
     if _PLACES and _SUFFIX else r"(?!x)x"),
    re.I,
)


def is_nc(text):
    """True if `text` names a configured-local location (profile [locality])."""
    return bool(NC_RE.search(text or ""))


# --------------------------------------------------------------------------- #
#  Pulling a location OUT of scraped page text                                 #
# --------------------------------------------------------------------------- #
#
# Custom/legacy boards (SuccessFactors tables, PeopleAdmin feeds, hand-rolled
# careers pages) publish no location field — the city is just words in the
# row. Scrapers recover it by searching that text for a place they recognise,
# which means the vocabulary has to come from [locality]; a hard-coded city
# list only ever works for the person who wrote it.

_SHORT_TOKEN = 4          # <= this many chars: match on word boundaries


def _alt(term):
    esc = re.escape(term)
    return rf"\b{esc}\b" if len(term) <= _SHORT_TOKEN else esc


_SNIPPET_ALTS = [_alt(t) for t in (_WB + _SUB + [s for s in config.LOCALITY_STATE_SUFFIX if s])]
# "Remote" always counts: it's a location on every board, in every field.
_SNIPPET_ALTS.append(r"\bremote\b")

# A recognised place plus up to 40 chars of trailing context, so "Durham" in
# a table cell comes back as "Durham, NC 27701" rather than the bare word.
LOCATION_SNIPPET_RE = re.compile(
    rf"(?:{'|'.join(_SNIPPET_ALTS)})[^|\n]{{0,40}}", re.I)


def location_snippet(text, default="See posting"):
    """The first location-looking phrase in `text`, or `default`."""
    m = LOCATION_SNIPPET_RE.search(text or "")
    return m.group(0).strip(" ,-") if m else default


def geo_mode(location, description=""):
    """Classify a posting's geography: "onsite" (configured locality),
    "remote", or None (neither). Onsite wins when a posting is both local
    and remote-friendly — a "Remote; Durham, NC" multi-location posting is
    LOCAL material, not a remote drop. Both the location FIELD and the body
    text are checked against the full locality regex (NC_RE — profile
    [locality]) so "hybrid from our Durham office" still counts as onsite.
    Remote detection delegates to core.remote_filter (workforce-
    context phrases, hard negations) instead of a bare token list."""
    from .remote_filter import remote_signal
    if NC_RE.search(f"{location or ''} {description or ''}"):
        return "onsite"
    if remote_signal(location, description):
        return "remote"
    return None
