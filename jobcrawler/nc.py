"""
Single source of truth for North-Carolina locality detection.

Previously each of fetchers/company, discovery/local_sourcing,
discovery/sniffer, discovery/ats_dork, and the local track carried its own
NC-token regex; they now delegate here.
"""

import re

# Word-boundary for short/ambiguous tokens (so "nc" doesn't hit "clinic",
# "cary" doesn't hit "scary"); substring for distinctive multi-char names.
_WB = ("nc", "rtp", "cary", "apex")
_SUB = ("north carolina", "durham", "raleigh", "chapel hill", "morrisville",
        "research triangle", "holly springs", "clayton", "franklinton",
        "burlington", "wake forest", "pittsboro", "winston-salem", "greensboro")

# Public: usable directly as the `loc_re` parameter of the company fetchers.
NC_RE = re.compile(
    "|".join([rf"\b{re.escape(t)}\b" for t in _WB] + [re.escape(t) for t in _SUB]),
    re.I,
)

# Stricter "<Triangle city>, NC" address form — a company-HQ/office signal that
# holds even when a company has zero current openings.
NC_HQ_RE = re.compile(
    r"\b(durham|raleigh|chapel hill|morrisville|cary|research triangle|\brtp\b|"
    r"holly springs|clayton|apex|wake forest|pittsboro|winston-salem|greensboro)\b"
    r"[\s,.\-]{0,4}(nc\b|north carolina)", re.I)


def is_nc(text):
    """True if `text` names an NC / Research-Triangle location."""
    return bool(NC_RE.search(text or ""))
