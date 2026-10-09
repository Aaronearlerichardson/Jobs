"""Small shared helpers."""

from __future__ import annotations

import functools
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Literal, cast, overload
from urllib.parse import urlsplit

from cssselect import HTMLTranslator
from lxml import etree
from yarl import URL

from src import config
from src.rows import JSON, dig  # noqa: F401 (the facade: defined in rows, below config)

_log = logging.getLogger(__name__)

# City, ST  |  City, State  |  Remote — a location as a careers page prints
# it, for reading one off a listing row's text (custom boards, iCIMS).
LOC_TEXT_RE = re.compile(r"[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)*,\s*"
                         r"(?:[A-Z]{2}|[A-Z][a-z]+)\b|\bremote\b", re.I)


def cache_dir(*parts: str) -> Path:
    """A directory under the data dir's `.cache/` for a fetcher's disk
    cache (board detection, Workday and iCIMS location lookups). Not
    created here: callers mkdir when they first write."""
    return config.DATA_DIR.joinpath(".cache", *parts)


def hashed_cache_path(base_dir: Path, key: str) -> Path:
    """The JSON cache file for `key` under `base_dir`: sha1(key) + ".json",
    so callers never handle raw keys as filenames."""
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return base_dir / f"{h}.json"


def json_cache_get(path: Path, ttl: float) -> JSON:
    """The JSON value stored at `path`, or None when the file is absent,
    unreadable, or older than `ttl` seconds.

    Shared by every mtime-TTL disk cache in the crawl (DDG search results,
    board detection, Workday and iCIMS locations): each caller picks its
    own base directory and value shape (a raw value, or a wrapper that
    lets it cache a negative result distinctly from a miss)."""
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        return cast(JSON, json.loads(path.read_text("utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:      # unreadable file, corrupt JSON
        _log.debug("cache miss %s: %s", path, e)
        return None


def json_cache_put(path: Path, value: object) -> None:
    """Best-effort JSON write to `path`; a cache failure never fails the
    caller."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:
        _log.warning("cache write %s failed: %s", path, e)


def default_search_text() -> str:
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


def worker_count(setting: str, floor: int = 4) -> int:
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


def text_from_html(raw: str | None) -> str:
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
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    txt = re.sub(r"(?i)<(/p|/li|/h[1-6]|br\s*/?|/div)\s*>", "\n", txt)
    txt = _TAG_RE.sub(" ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


def strip_html(s: object) -> str:
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


#: Per thread: its parsers and compiled XPath (`_parser`, `xpath`).
_local = threading.local()


def _parser(xml: bool, utf8: bool) -> etree.XMLParser | etree.HTMLParser:
    """This thread's lxml parser, XML (recovering, no entities, no network)
    or HTML, told its input is UTF-8 when `utf8`. Threads sharing one parser
    take turns, so each keeps its own: asyncio.to_thread's workers, which
    parse for async code, as well."""
    name = ("xml" if xml else "html") + ("8" if utf8 else "")
    p = getattr(_local, name, None)
    if p is None:
        enc = "utf-8" if utf8 else None
        # huge_tree: without it the HTML tree builder stops, silently, at 256
        # levels of nesting or a 10 MB text node. Not for XML, where it would
        # also lift libxml2's entity-expansion limits.
        p = (etree.XMLParser(recover=True, resolve_entities=False, no_network=True,
                             encoding=enc) if xml
             else etree.HTMLParser(huge_tree=True, encoding=enc))
        setattr(_local, name, p)
    return p


def parse_markup(markup: str | bytes | None, xml: bool = False, url: str = "") -> etree._Element:
    """`markup` (str or bytes) as an lxml tree, its root element: HTML, or
    XML when `xml` (a feed: HTML reads <link> as a void tag and loses its
    URL). The one parser choice in src/.

    Blank markup is an empty document (a childless <html>). Content after
    a doctyped page's </html> joins the root. HTML lxml
    cannot parse, or gives up on partway (a fatal error), is parsed by
    html5lib instead; XML lxml cannot parse is an empty document. Either is
    logged with `url`'s host and the reason.

    >>> parse_markup("<rss><item><link>https://x.test/1</link></item></rss>", xml=True).findtext(".//link")
    'https://x.test/1'
    >>> len(parse_markup(" \\n")), len(parse_markup('<b class="x\\x00"></b><a href="/2">').findall(".//a"))
    (0, 1)
    >>> len(parse_markup(b'<b class="x\\x00"></b><a href="/2">').findall(".//a")), len(parse_markup(
    ...     b"<svg\\x00><title>t</title></svg>").findall(".//title"))
    (1, 1)
    >>> parse_markup("<!DOCTYPE html><html><body></body></html><input id=jobs>").find(".//input").get("id")
    'jobs'

    Notes:
        A str is parsed as its UTF-8 bytes: lxml refuses a str carrying an
        XML encoding declaration, and a <meta charset> must not re-decode
        text already decoded. Bytes are the parser's to decode. A NUL in a
        str reads as U+FFFD, as the HTML standard has it: lxml's tree
        builder dropped the rest of a careers page after one in a class
        attribute (2026-09). html5lib's SVG and MathML elements lose their
        namespace, as lxml's HTML parser has none.
    """
    if not markup or markup.isspace():
        return etree.Element("html")
    utf8 = isinstance(markup, str)
    parser = _parser(xml, utf8)
    try:
        root = etree.fromstring(markup.replace("\x00", "\ufffd").encode("utf-8", "replace")
                                if isinstance(markup, str) else markup, parser)
    except (etree.LxmlError, ValueError, LookupError) as e:
        reason = f"lxml: {e}"
    else:
        fatal = None if xml else parser.error_log.filter_from_fatals()
        if root is not None and not fatal:
            for sib in list(root.itersiblings(etree.Element)):
                root.extend(sib)
            return root
        reason = f"lxml gave up: {fatal[0].message.strip()}" if fatal else "lxml found no document"
    host = host_of(url) or "?"
    if xml:
        _log.info("unreadable XML from %s: %s", host, reason)
        return etree.Element("html")
    _log.info("html5lib parse of %s: %s", host, reason)
    root = _html5lib().parse(markup, treebuilder="lxml", namespaceHTMLElements=False).getroot()
    for el in root.iter(etree.Element):
        if el.tag[0] == "{":
            el.tag = el.tag.split("}", 1)[1]
    return root


@functools.cache
def _html5lib() -> ModuleType:
    """html5lib, imported on first use (about 120 ms), its warnings about
    names it had to coerce silenced."""
    import html5lib
    from html5lib.constants import DataLossWarning
    warnings.filterwarnings("ignore", category=DataLossWarning)
    return cast(ModuleType, html5lib)


def xpath(expr: str) -> etree.XPath:
    """`expr`, an XPath 1.0 expression, compiled (plain-str results) once
    per thread: threads sharing one compiled expression take turns. Raises
    lxml's XPathSyntaxError when it will not compile.

    >>> xpath("//a[contains(@href, $part)]/@href")(parse_markup("<a href='/x/1'><a href='/y/2'>"),
    ...                                            part="/y/")
    ['/y/2']
    """
    cache = getattr(_local, "xpaths", None)
    if cache is None:
        cache = _local.xpaths = dict[str, etree.XPath]()
    xp = cache.get(expr)
    if xp is None:
        xp = cache[expr] = etree.XPath(expr, smart_strings=False)
    return xp


def first(expr: str, scope: etree._Element, **variables: str) -> etree._Element | None:
    """The first node `xpath(expr)` finds from `scope`, its $names filled
    from `variables`; None when it finds none.

    >>> page = parse_markup("<p>a</p><p>b</p>")
    >>> first("//p", page).text, first("//p[. = $t]", page, t="b").text, first("//i", page)
    ('a', 'b', None)
    """
    hit = xpath(expr)(scope, **variables)
    return cast(etree._Element, hit[0]) if hit else None


def links(tree: etree._Element) -> list[etree._Element]:
    """Every <a> with an href in `tree`'s document."""
    return cast(list[etree._Element], xpath("//a[@href]")(tree))


def jsonld_scripts(tree: etree._Element) -> list[etree._Element]:
    """Every schema.org JSON-LD block in `tree`'s document: its
    <script type="application/ld+json"> elements."""
    return cast(list[etree._Element], xpath("//script[@type='application/ld+json']")(tree))


@overload
def named(scope: etree._Element, name: str, one: Literal[True]) -> etree._Element | None: ...
@overload
def named(scope: etree._Element, name: str, one: Literal[False] = False) -> list[etree._Element]: ...
def named(scope: etree._Element, name: str, one: bool = False) -> etree._Element | None | list[etree._Element]:
    """The elements at or below `scope` whose local name is `name`, in any
    namespace (an Atom feed's are in its own); with `one`, the first, or
    None.

    >>> feed = parse_markup('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title>'
    ...                     '</entry><entry/></feed>', xml=True)
    >>> len(named(feed, "entry")), named(feed, "title", one=True).text, named(feed, "item", one=True)
    (2, 'A', None)
    """
    expr = "descendant-or-self::*[local-name()=$name]"
    return first(expr, scope, name=name) if one else cast(list[etree._Element], xpath(expr)(scope, name=name))


@functools.cache
def css(selector: str, relative: bool = False) -> str:
    """`selector`, CSS in cssselect's HTML dialect, as XPath for `xpath` and
    `first`: matching the element it runs on and everything below (from
    the root, the whole document), or only what is below when `relative`.
    Raises cssselect's SelectorError on CSS the dialect cannot read.

    >>> page = parse_markup('<ul><li><a href="/job/1">A</a><p class="loc">Durham</p></li>'
    ...                     '<li><a href="/job/2">B</a></li></ul>')
    >>> [a.get("href") for a in xpath(css("li:has(.loc) a[href*='/job/']"))(page)]
    ['/job/1']
    >>> li = page.find(".//li")
    >>> len(xpath(css("li"))(li)), first(css("li", relative=True), li)
    (1, None)
    """
    return HTMLTranslator().css_to_xpath(
        selector, prefix="descendant::" if relative else "descendant-or-self::")


@overload
def node_text(el: etree._Element, sep: str = " ", strip: bool = True) -> str: ...
@overload
def node_text(el: etree._Element, sep: None, strip: bool = True) -> list[str]: ...
def node_text(el: etree._Element, sep: str | None = " ", strip: bool = True) -> str | list[str]:
    r"""`el`'s text: its text nodes, none inside script, style, template or
    a ruby annotation, each stripped and the blank ones dropped when
    `strip`, joined by `sep` (a list when `sep` is None).

    >>> p = parse_markup("<p> Data <b>Engineer</b><script>x()</script>\n</p>").find(".//p")
    >>> node_text(p), node_text(p, "|", strip=False), node_text(p, None)
    ('Data Engineer', ' Data |Engineer|\n', ['Data', 'Engineer'])

    Notes:
        BeautifulSoup's get_text(sep, strip), which the page readers were
        written against, bar its folding of a whitespace-only string to
        one space or newline. A walk, not XPath: an `ancestor::` test per
        text node ran 14x slower over the recorded pages.
    """
    no_text = frozenset(("script", "style", "template", "rt", "rp"))
    parts: list[str] = []
    if next(el.iterancestors(*no_text), None) is None:
        walk = etree.iterwalk(el, events=("start", "end", "comment", "pi"))
        for event, node in walk:
            if event != "start":
                if node is not el and node.tail:
                    parts.append(node.tail)
            elif node.tag in no_text:
                walk.skip_subtree()
            elif node.text:
                parts.append(node.text)
    if strip:
        parts = [s for s in map(str.strip, parts) if s]
    return parts if sep is None else sep.join(parts)


def clean_field(text: str | None) -> str:
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


def clean_url(u: str | None) -> str | None:
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
    return re.sub(r"\s*[\r\n\t]\s*", "", u).strip() if u else u


def host_of(url: str | None) -> str:
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


def origin_of(url: str | None) -> str:
    """`url`'s `scheme://netloc`, host case and port kept, to build URLs
    on (`origin_key` is the one to key by); "" when it names no host.

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


def origin_key(url: str | None) -> str:
    """`url`'s origin as one key, however it is spelled (yarl's
    `URL.origin`): host lower-cased, credentials and the scheme's default
    port dropped; "" when it names no host or a bad port.

    >>> origin_key("HTTPS://u@CSS-X.Example.COM:443/sso"), origin_key("http://[::1]:8443/")
    ('https://css-x.example.com', 'http://[::1]:8443')
    >>> origin_key("careers/jobs"), origin_key("http://h:x/"), origin_key(None)
    ('', '', '')
    """
    try:
        return str(URL(url or "").origin())
    except ValueError:
        return ""


def stable_id(*parts: object) -> str:
    """Deterministic short hash for building job IDs.

    Python's built-in hash() is salted per process (PYTHONHASHSEED), so
    IDs built from it change every run and the seen-jobs dedupe never
    matches — every RSS/scrape job re-surfaces as "new" forever. This
    sha1-based ID is stable across runs and machines.
    """
    key = "||".join(str(p) for p in parts)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def norm_posted_date(value: object) -> str | None:
    """Normalize an ATS posting-date value to 'YYYY-MM-DD', or None.

    The formats seen in the wild (verified against live boards):
      * ISO datetimes with timezone — Greenhouse `first_published`,
        SmartRecruiters `releasedDate`, JSON-LD `datePosted`
      * epoch MILLISECONDS as a string — Lever `createdAt`
      * epoch seconds — Eightfold `postedTs`, midnight UTC of the day.
        Epochs are read in UTC, not the machine's zone, which put every
        Eightfold date a day early west of Greenwich.
      * relative text — Workday's `postedOn` ("Posted 3 Days Ago",
        "Posted 30+ Days Ago", "Posted Today"). "30+" parses as 30, so
        treat old Workday dates as a floor, not an exact day.
      * spelled dates — Amazon's "August 20, 2026" and "July  8, 2026"
      * US numeric dates anywhere in the text — iCIMS's "3 hours ago
        (10/8/2026 2:04 PM)"

    >>> norm_posted_date("August 20, 2026"), norm_posted_date("Jul 8, 2026")
    ('2026-08-20', '2026-07-08')
    >>> norm_posted_date("Smarch 3, 2026") is None
    True
    >>> norm_posted_date("3 hours ago (10/8/2026 2:04 PM)"), norm_posted_date("13/8/2026") is None
    ('2026-10-08', True)
    >>> norm_posted_date(1791158400), norm_posted_date("1791158400000")
    ('2026-10-05', '2026-10-05')
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        if n > 1e11:          # epoch milliseconds (Lever)
            n /= 1000.0
        if n > 1e8:           # sanity: on/after ~1973
            try:
                return datetime.fromtimestamp(n, UTC).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return None
        return None
    text = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(1[0-2]|0?[1-9])/(3[01]|[12]\d|0?[1-9])/(\d{4})\b", text)
    if m:
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
    low = text.lower()
    if "today" in low or "just posted" in low:
        return datetime.now().strftime("%Y-%m-%d")
    if "yesterday" in low:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*\+?\s*days?\s+ago", low, re.I)
    if m:
        return (datetime.now() - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"^([a-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})$", low)
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    if m and m[1] in months:
        return f"{m[3]}-{months.index(m[1]) + 1:02d}-{int(m[2]):02d}"
    return None
