"""Small shared helpers."""

import hashlib
import html
import os
import re
from datetime import datetime, timedelta

from src import config

# City, ST  |  City, State  |  Remote — a location as a careers page prints
# it, for reading one off a listing row's text (custom boards, iCIMS).
LOC_TEXT_RE = re.compile(r"[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)*,\s*"
                         r"(?:[A-Z]{2}|[A-Z][a-z]+)\b|\bremote\b", re.I)


def cache_dir(*parts):
    """A directory under the data dir's `.cache/` for a fetcher's disk
    cache (board detection, Workday and iCIMS location lookups). Not
    created here: callers mkdir when they first write."""
    return config.DATA_DIR.joinpath(".cache", *parts)


def default_search_text():
    """A free-text place term derived from the profile's [locality], for
    the search boxes that narrow a board server-side (Workday's
    `searchText`, the discovery probe's local count).

    Prefer a spelled-out state/region suffix ("california") over a two-letter
    abbreviation, which matches far too much in a free-text field; fall back
    to the longest place name. "" when no locality is configured, which
    simply means an unnarrowed board pull."""
    words = [s for s in config.LOCALITY_STATE_SUFFIX if len(s) > 2]
    if words:
        return max(words, key=len)
    places = [s for s in config.LOCALITY_SUBSTRINGS if s]
    return max(places, key=len) if places else ""


def worker_count(env_var, floor=4):
    """Default thread-pool size: n_cpus - 1, overridable via `env_var`.

    Discovery and crawl fetching are network-I/O-bound (profiling a 677-
    company discovery run showed ~95% of wall time in socket/SSL reads and
    the headless browser, with the CPU near 10%). So threads mostly sit
    blocked on the network, and n_cpus-1 is a floor, not a ceiling — set
    the env var higher (e.g. 32) to push more concurrent requests and
    saturate the link. Adding CPU cores does NOT raise throughput here.
    """
    v = os.environ.get(env_var, "").strip()
    if v.isdigit() and int(v) > 0:
        return int(v)
    return max((os.cpu_count() or 9) - 1, floor)


_TAG_RE   = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def strip_html(s):
    """Markup out, one line of readable text back. "" for anything falsy.

    Entities first, THEN tags: unescaping later would turn a literal
    "&lt;script&gt;" in the copy into a tag this has already decided not to
    strip. Feeds hand us HTML fragments (RemoteOK/Remotive descriptions,
    RSS <description>, HN comment bodies), and the gate that reads the
    result matches on substrings, so collapsing whitespace matters as much
    as dropping the tags -- a keyword split across a newline inside a <li>
    would otherwise miss.

    >>> strip_html("<p>Hello&nbsp;&amp; welcome</p>\\n<li>EEG  work</li>")
    'Hello & welcome EEG work'
    >>> strip_html(None)
    ''
    """
    if not s:
        return ""
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(s))).strip()


def stable_id(*parts) -> str:
    """Deterministic short hash for building job IDs.

    Python's built-in hash() is salted per process (PYTHONHASHSEED), so
    IDs built from it change every run and the seen-jobs dedupe never
    matches — every RSS/scrape job re-surfaces as "new" forever. This
    sha1-based ID is stable across runs and machines.
    """
    key = "||".join(str(p) for p in parts)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_REL_DAYS_RE = re.compile(r"(\d+)\s*\+?\s*days?\s+ago", re.I)


def norm_posted_date(value):
    """Normalize an ATS posting-date value to 'YYYY-MM-DD', or None.

    The formats seen in the wild (verified against live boards):
      * ISO datetimes with timezone — Greenhouse `first_published`,
        SmartRecruiters `releasedDate`, JSON-LD `datePosted`
      * epoch MILLISECONDS as a string — Lever `createdAt`
      * relative text — Workday's `postedOn` ("Posted 3 Days Ago",
        "Posted 30+ Days Ago", "Posted Today"). "30+" parses as 30, so
        treat old Workday dates as a floor, not an exact day.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        if n > 1e11:          # epoch milliseconds (Lever)
            n /= 1000.0
        if n > 1e8:           # sanity: on/after ~1973
            try:
                return datetime.fromtimestamp(n).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return None
        return None
    text = str(value).strip()
    m = _ISO_DATE_RE.match(text)
    if m:
        return m.group(1)
    low = text.lower()
    if "today" in low or "just posted" in low:
        return datetime.now().strftime("%Y-%m-%d")
    if "yesterday" in low:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    m = _REL_DAYS_RE.search(low)
    if m:
        return (datetime.now() - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    return None
