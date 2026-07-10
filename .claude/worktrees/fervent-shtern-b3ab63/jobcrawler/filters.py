"""Keyword + location filtering.

Reads CORE/DOMAIN/SKILL_KEYWORDS, EXCLUDE_PHRASES, and LOCATION_* from
config at call time, so `--expand-live` and friends can mutate those
lists and this module will see the updated values on the next call.

Relevance model (tiered):
  1. CORE match  -> standalone signal, relevant.
  2. DOMAIN + SKILL match -> adjacent medical/bio domain where your
     transferable skills apply. Relevant.
  3. Any legacy entry in INCLUDE_KEYWORDS that isn't in a tier is treated
     as Tier 1 (standalone). This is how --expand-live additions and
     manually-edited compat lists behave.

Location model:
  * Short tokens (<= 3 chars: "nc", "va", "rtp", "wfh") use word-boundary
    matching so "clinical" doesn't match "nc" and "nevada" doesn't match
    "va". Everything else uses plain substring.
"""

import re

import config
from config import (
    CORE_KEYWORDS,
    DOMAIN_KEYWORDS,
    EXCLUDE_PHRASES,
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

def _kw_in(text, keywords):
    """Case-insensitive substring hit against any keyword."""
    return any(k.lower() in text for k in keywords)


def is_relevant(title, description=""):
    text = (title + " " + description).lower()
    if any(p.lower() in text for p in EXCLUDE_PHRASES):
        return False

    # Tier 1: core neurotech / specific job titles.
    if _kw_in(text, CORE_KEYWORDS):
        return True

    # Legacy / dynamically-added keywords (not in any tier) act like Tier 1.
    tiered = {k.lower() for k in CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS}
    extras = [k for k in INCLUDE_KEYWORDS if k.lower() not in tiered]
    if extras and _kw_in(text, extras):
        return True

    # Tier 2 x Tier 3: adjacent medical/bio domain + transferable skill.
    return _kw_in(text, DOMAIN_KEYWORDS) and _kw_in(text, SKILL_KEYWORDS)


def classify_relevance(title, description=""):
    """
    Debug helper - returns which tier caused a match, or None.
    Not used by the crawler; handy for tuning the lists.
    """
    text = (title + " " + description).lower()
    if any(p.lower() in text for p in EXCLUDE_PHRASES):
        return None
    if _kw_in(text, CORE_KEYWORDS):
        return "CORE"
    tiered = {k.lower() for k in CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS}
    extras = [k for k in INCLUDE_KEYWORDS if k.lower() not in tiered]
    if extras and _kw_in(text, extras):
        return "EXTRA"
    if _kw_in(text, DOMAIN_KEYWORDS) and _kw_in(text, SKILL_KEYWORDS):
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
      5. Legacy/dynamic entries in LOCATION_INCLUDE not in either bucket
         -> allowed (this is how --expand-location-live additions behave).
      6. Otherwise denied.

    ACCEPT_REMOTE is read off the live config module so mutating it at
    runtime (tests, quick toggles) takes effect without re-importing.
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
