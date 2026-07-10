"""Keyword + location filtering.

Reads CORE/DOMAIN/SKILL_KEYWORDS, EXCLUDE_PHRASES, and LOCATION_* from
config at call time, so `--expand-live` and friends can mutate those
lists and this module will see the updated values on the next call.

Relevance model (tiered):
  1. CORE match  -> standalone signal, relevant.
  2. DOMAIN + SKILL match -> adjacent medical/bio domain where your
     transferable skills apply. Relevant.
"""

import re

import config
from config import (
    CORE_KEYWORDS,
    DOMAIN_KEYWORDS,
    EXCLUDE_PHRASES,
    EXCLUDE_TITLE_PHRASES,
    INCLUDE_KEYWORDS,
    LOCATION_EXCLUDE,
    LOCATION_INCLUDE,
    LOCATION_ONSITE_INCLUDE,
    LOCATION_REMOTE_INCLUDE,
    SKILL_KEYWORDS,
)


# --------------------------------------------------------------------- #
#  Relevance                                                             #
# --------------------------------------------------------------------- #

def _kw_match(kw, text):
    """
    Case-insensitive keyword hit. Short single-token alphabetic keywords
    (acronyms: eeg, bci, ecog, ieeg, meg, mri, dsp, ...) use word-boundary
    matching — plain substring fires inside ordinary words ("ecog" in
    "recognized", "meg" in "omega") and floods aggregator sources with
    off-topic roles. Multi-word phrases and longer tokens stay substring
    so "subcortical" still matches "cortical".
    """
    k = kw.lower()
    if k.isalpha() and len(k) <= 5:
        return re.search(rf"\b{re.escape(k)}\b", text) is not None
    return k in text


def _kw_in(text, keywords):
    return any(_kw_match(k, text) for k in keywords)


def _excluded(title, text):
    """EXCLUDE_PHRASES match anywhere; EXCLUDE_TITLE_PHRASES title-only."""
    if any(p.lower() in text for p in EXCLUDE_PHRASES):
        return True
    t = (title or "").lower()
    return any(p.lower() in t for p in EXCLUDE_TITLE_PHRASES)


# DOMAIN+SKILL pairing only reads the posting head. Specific CORE terms
# (eeg, bci, neural decoding) are signal wherever they appear, but generic
# domain words deep in a posting are usually benefits boilerplate —
# "medical, dental, vision" + "data" would tier-match nearly every US job
# ad if the pairing scanned full text.
_PAIR_SCAN_CHARS = 1200


def is_relevant(title, description=""):
    text = (title + " " + description).lower()
    if _excluded(title, text):
        return False

    # Tier 1: core neurotech / specific job titles. Full-text scan.
    if _kw_in(text, CORE_KEYWORDS):
        return True

    # Legacy / dynamically-added keywords (not in any tier) act like Tier 1.
    tiered = {k.lower() for k in CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS}
    extras = [k for k in INCLUDE_KEYWORDS if k.lower() not in tiered]
    if extras and _kw_in(text, extras):
        return True

    # Tier 2 x Tier 3: adjacent medical/bio domain + transferable skill.
    # Head-only scan — see _PAIR_SCAN_CHARS.
    head = (title + " " + description[:_PAIR_SCAN_CHARS]).lower()
    return _kw_in(head, DOMAIN_KEYWORDS) and _kw_in(head, SKILL_KEYWORDS)


def classify_relevance(title, description=""):
    """
    Debug helper - returns which tier caused a match, or None.
    Not used by the crawler; handy for tuning the lists.
    """
    text = (title + " " + description).lower()
    if _excluded(title, text):
        return None
    if _kw_in(text, CORE_KEYWORDS):
        return "CORE"
    tiered = {k.lower() for k in CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS}
    extras = [k for k in INCLUDE_KEYWORDS if k.lower() not in tiered]
    if extras and _kw_in(text, extras):
        return "EXTRA"
    head = (title + " " + description[:_PAIR_SCAN_CHARS]).lower()
    if _kw_in(head, DOMAIN_KEYWORDS) and _kw_in(head, SKILL_KEYWORDS):
        return "DOMAIN+SKILL"
    return None


# --------------------------------------------------------------------- #
#  Location                                                              #
# --------------------------------------------------------------------- #

# Short tokens (<= 3 chars) use word-boundary matching. Everything longer
# uses plain substring so multi-word filter entries keep working.
_SHORT_TOKEN_LEN = 3


def _loc_match(token, text):
    """
    Match `token` (already lowercased) inside `text` (already lowercased).
    Short tokens use \\b...\\b to avoid "nc" matching inside "clinical".
    """
    t = token.lower()
    if len(t) <= _SHORT_TOKEN_LEN:
        return re.search(rf"\b{re.escape(t)}\b", text) is not None
    return t in text


def is_location_allowed(location):
    """
    True if `location` passes the configured filters.

    Order of checks:
      1. Empty location -> allowed (many fetchers return "Unknown").
      2. LOCATION_EXCLUDE hit -> denied.
      3. LOCATION_ONSITE_INCLUDE hit -> allowed.
      4. ACCEPT_REMOTE and LOCATION_REMOTE_INCLUDE hit -> allowed.
    """
    if not location:
        return True
    loc = location.lower()
    if any(_loc_match(bad, loc) for bad in LOCATION_EXCLUDE):
        return False
    if any(_loc_match(good, loc) for good in LOCATION_ONSITE_INCLUDE):
        return True
    if getattr(config, "ACCEPT_REMOTE", True):
        if any(_loc_match(good, loc) for good in LOCATION_REMOTE_INCLUDE):
            return True
    # Legacy / dynamic entries appended to LOCATION_INCLUDE.
    bucketed = {t.lower() for t in LOCATION_ONSITE_INCLUDE + LOCATION_REMOTE_INCLUDE}
    extras = [t for t in LOCATION_INCLUDE if t.lower() not in bucketed]
    if extras and any(_loc_match(good, loc) for good in extras):
        return True
    # If no filter lists are populated at all, fall through and allow.
    if not (LOCATION_ONSITE_INCLUDE or LOCATION_REMOTE_INCLUDE or LOCATION_INCLUDE):
        return True
    return False
