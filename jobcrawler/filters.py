"""Keyword + location filtering.

Reads INCLUDE_KEYWORDS / EXCLUDE_PHRASES / LOCATION_* from config so that
--expand-live and friends can mutate those lists and this module will
see the updated values on the next call.
"""

from config import (
    EXCLUDE_PHRASES,
    INCLUDE_KEYWORDS,
    LOCATION_EXCLUDE,
    LOCATION_INCLUDE,
)


def is_relevant(title, description=""):
    combined = (title + " " + description).lower()
    if any(p in combined for p in EXCLUDE_PHRASES):
        return False
    return any(kw in combined for kw in INCLUDE_KEYWORDS)


def is_location_allowed(location):
    """
    True if location passes LOCATION_INCLUDE/LOCATION_EXCLUDE.
    Empty/unknown locations are treated as allowed.
    """
    if not location:
        return True
    loc = location.lower()
    if any(bad.lower() in loc for bad in LOCATION_EXCLUDE):
        return False
    if LOCATION_INCLUDE:
        return any(good.lower() in loc for good in LOCATION_INCLUDE)
    return True
