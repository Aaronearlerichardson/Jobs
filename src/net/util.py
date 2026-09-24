"""Small shared helpers."""

import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

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


def hashed_cache_path(base_dir, key):
    """The JSON cache file for `key` under `base_dir`: sha1(key) + ".json",
    so callers never handle raw keys as filenames."""
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return base_dir / f"{h}.json"


def json_cache_get(path, ttl):
    """The JSON value stored at `path`, or None when the file is absent,
    unreadable, or older than `ttl` seconds.

    Shared by every mtime-TTL disk cache in the crawl (DDG search results,
    board detection, Workday and iCIMS locations): each caller picks its
    own base directory and value shape (a raw value, or a wrapper that
    lets it cache a negative result distinctly from a miss)."""
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


def json_cache_put(path, value):
    """Best-effort JSON write to `path`; a cache failure never fails the
    caller."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    except Exception:
        pass


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


def locality_abbr():
    """The profile's first [locality] state suffix, upper-cased ("NC"): a
    search term for a board that parses a state code. "" when none is
    configured."""
    return (config.LOCALITY_STATE_SUFFIX or [""])[0].upper()


def worker_count(setting, floor=4):
    """Thread-pool size: config.SETTINGS.<setting> (the CRAWLER_WORKERS,
    DISCOVERY_WORKERS or HARVEST_WORKERS variable) when set, else
    n_cpus - 1 and at least `floor`.

    Discovery and crawl fetching are network-I/O-bound (profiling a 677-
    company discovery run showed ~95% of wall time in socket/SSL reads and
    the headless browser, with the CPU near 10%). So threads mostly sit
    blocked on the network, and n_cpus-1 is a floor, not a ceiling — set
    the variable higher (e.g. 32) to push more concurrent requests and
    saturate the link. Adding CPU cores does NOT raise throughput here.
    """
    return (getattr(config.SETTINGS, setting)
            or max((os.cpu_count() or 9) - 1, floor))


_TAG_RE    = re.compile(r"<[^>]+>")
_SPACE_RE  = re.compile(r"\s+")
_SCRIPT_RE = re.compile(r"(?is)<(script|style).*?</\1>")
_BLOCK_RE  = re.compile(r"(?i)<(/p|/li|/h[1-6]|br\s*/?|/div)\s*>")
_HSPACE_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n\s*\n+")


def text_from_html(raw):
    r"""An HTML job description as readable text, paragraph breaks kept.

    The stripper every ATS description goes through. Nine fetchers had
    written their own (four on BeautifulSoup's ``get_text(" ")``, four on
    ``get_text(" ", strip=True)``, one on a bare tag regex with NO entity
    unescaping, so literal "&amp;"/"&nbsp;" reached the store), and a
    description that reads differently per platform is a gate that reads
    differently per platform: the keyword filters, the fit prompt and the
    digest all match on this text.

    Three things `strip_html` does not do, and a JD body needs:

      * script/style blocks go with their CONTENT, not just their tags --
        a careers page's embedded JSON (Getro's whole ``__NEXT_DATA__``
        record, a JSON-LD blob) would otherwise land in the body as text;
      * a block END tag becomes a newline, so bullets and paragraphs stay
        apart instead of running together into one wall of words;
      * only spaces/tabs collapse, so those newlines survive.

    >>> text_from_html("<h2>Role</h2><ul><li>EEG  work</li><li>ML</li></ul>")
    'Role\n EEG work\n ML'
    >>> text_from_html("<p>Hello&nbsp;&amp; welcome</p><script>x=1</script>")
    'Hello\xa0& welcome'
    >>> text_from_html(None)
    ''

    Notes:
        Entities are unescaped LAST, the opposite of strip_html's order:
        a JD that writes "travel: &lt;20%" means the text "<20%", and
        unescaping first would let the tag regex eat from there to the
        next ">". A board whose API hands back ESCAPED markup (Greenhouse
        `content`) therefore has to unescape at the call site, before the
        markup is markup -- see the unescape_html_text transform in
        board/fields.py.
    """
    if not raw:
        return ""
    txt = _SCRIPT_RE.sub(" ", raw)
    txt = _BLOCK_RE.sub("\n", txt)
    txt = _TAG_RE.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _HSPACE_RE.sub(" ", txt)
    txt = _BLANKS_RE.sub("\n\n", txt)
    return txt.strip()


def strip_html(s):
    """Markup out, one line of readable text back. "" for anything falsy;
    any other non-string is str()-ed first (payloads are untrusted).

    Entities first, THEN tags: unescaping later would turn a literal
    "&lt;script&gt;" in the copy into a tag this has already decided not to
    strip. Feeds hand us HTML fragments (RemoteOK/Remotive descriptions,
    RSS <description>, HN comment bodies), and the gate that reads the
    result matches on substrings, so collapsing whitespace matters as much
    as dropping the tags -- a keyword split across a newline inside a <li>
    would otherwise miss.

    >>> strip_html("<p>Hello&nbsp;&amp; welcome</p>\\n<li>EEG  work</li>")
    'Hello & welcome EEG work'
    >>> strip_html(None), strip_html(42)
    ('', '42')
    """
    if not s:
        return ""
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(str(s)))).strip()


def clean_field(text):
    r"""`text` with every run of whitespace -- a newline, a tab, repeated
    spaces -- collapsed to one space, and the ends trimmed. None reads as
    "".

    strip_html's whitespace half, without its markup half: a listing's
    `title` or `location` is plain text a payload already handed over, and
    running it through strip_html would silently eat anything shaped like
    a tag ("Engineer <Level 3>").

    >>> clean_field("Calibration\nTechnician")
    'Calibration Technician'
    >>> clean_field("Durham,\tNC")
    'Durham, NC'
    >>> clean_field("  Data   Engineer  ")
    'Data Engineer'
    >>> clean_field(None)
    ''

    A value that is nothing BUT whitespace cleans to the empty string, not
    a string that merely looks empty -- callers that treat "" as "no
    title"/"no location" (board.engine.board_jobs itself; every reader
    downstream) see it as absent rather than as a title made of blanks:

    >>> clean_field("   \n\t  ")
    ''
    """
    return _SPACE_RE.sub(" ", text or "").strip()


_URL_BREAK_RE = re.compile(r"\s*[\r\n\t]\s*")


def clean_url(u):
    """`u` trimmed, minus any whitespace run holding a line break or tab.
    A lone space stays (requests percent-encodes it); falsy passes through.

    >>> clean_url("https://h.com \\r\\n\\t/job/DPC 1/\\r\\n")
    'https://h.com/job/DPC 1/'
    >>> clean_url(None)

    Notes:
        225 BioSpace rows were stored as "https://jobs.biospace.com
        \\r\\n\\t/job/...", and requests rejects those before reaching the
        network. Duke Health's Phenom ids ("job/DPC VCT 03") carry real
        spaces, which is why only runs with a break or tab go.
    """
    return _URL_BREAK_RE.sub("", u).strip() if u else u


def host_of(url):
    """The host `url` names, lowercased, without port or credentials;
    "" when it names none.

    >>> host_of("https://user@Jobs.Example.com:8443/careers?x=1")
    'jobs.example.com'
    >>> host_of("careers"), host_of("http://[::1"), host_of(None)
    ('', '', '')
    """
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def origin_of(url):
    """`url`'s `scheme://netloc`, host case and port kept; "" when it
    names no host.

    >>> origin_of("https://Jobs.Example.com:8443/careers?x=1")
    'https://Jobs.Example.com:8443'
    >>> origin_of("careers/jobs"), origin_of("http://[::1"), origin_of(None)
    ('', '', '')
    """
    try:
        p = urlsplit(url or "")
    except ValueError:
        return ""
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


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
