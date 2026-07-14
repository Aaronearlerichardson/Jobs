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
