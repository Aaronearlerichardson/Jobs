"""The careers-page reader: a self-hosted ("custom") careers board, read
structure-agnostically.

A job-detail link is /careers|jobs|positions|openings|roles|job/<slug>, but
index, nav and login pages share that shape ("/careers/open-positions",
"/jobs/login"), so generic slugs, nav-ish link text and links in the
site's navigation are refused and a specific slug is required
(`find_job_links`). A page with too few such links names its
"current openings" page, one hop away, on the same host (`read_page`).

The `custom` spec reads a board through `read_page` (the html decoder's
`"$job_links"`); discovery asks `custom_board_listing_url` whether a page
is a board at all. The reader's constants live in config
(CAREERS_PAGE_*, BOARD_DETECT_CACHE_S).
"""

import re
from urllib.parse import urldefrag, urljoin

from src import config
from src.net.http import HEADERS, SESSION
from src.net.util import (LOC_TEXT_RE, cache_dir, clean_field,
                          hashed_cache_path, host_of, json_cache_get,
                          json_cache_put, parse_markup)

_JOB_HREF_RE = re.compile(r"/(careers?|jobs?|positions?|openings?|roles?|job)/"
                          r"([a-z0-9][a-z0-9\-_/]{2,})", re.I)
_NAV_SLUGS = {
    "open-positions", "open-roles", "career-opportunities", "current-openings",
    "job-openings", "openings", "opportunities", "jobs", "job", "careers",
    "career", "apply", "application", "search", "all", "browse", "students",
    "internships", "benefits", "culture", "life", "teams", "team", "departments",
    "locations", "faq", "contact", "index", "home", "overview",
    "login", "logon", "signin", "sign-in",
}
#: A class or id naming site navigation: "navbar", "sub-nav", "menu-item",
#: "header__nav", "topnavigation".
_NAV_BLOCK_RE = re.compile(r"(?i)(?:^|[-_])(?:top|sub|main|site|desk|mobile|global)?"
                           r"(?:nav|navbar|navigation|menu)s?(?:$|[-_])")
_NAV_TEXT_RE = re.compile(
    r"^(careers?|jobs?|view (all|current|open)|open (positions?|roles?)|"
    r"see (all|open)|apply|search|browse|all (jobs|openings|roles)|"
    r"current openings|open positions|view (job )?openings|join( us)?|"
    r"work (with|at) us|learn more|explore|opportunities|all roles)\b", re.I)
_OPENINGS_HREF_RE = re.compile(
    r"/(open-positions|open-roles|career-opportunities|current-openings|"
    r"job-openings|openings|opportunities|positions|jobs)\b", re.I)
_OPENINGS_TEXT_RE = re.compile(
    r"(current|open|view|see|all).{0,12}(opening|position|role|job)", re.I)
_OFFSITE_RE = config.hosts_re(config.SHARED_HOSTS)


def _in_navigation(a):
    """Whether anchor `a` sits in the site's navigation: a <nav>, or an
    element whose class or id names a nav bar or menu (`_NAV_BLOCK_RE`)."""
    return any(el.name == "nav" or any(_NAV_BLOCK_RE.search(t)
                                       for t in (*(el.get("class") or ()), el.get("id") or ""))
               for el in a.parents)


def find_job_links(soup):
    """(anchor, href, title) for each real job-posting link on a careers
    page, one per href: nav, login and index links filtered, and any href
    the site's navigation links (`_in_navigation`), a section of the site
    wherever the page repeats it. The title is the anchor's heading (or
    [class*=title]) element's text, else its own."""
    anchors = soup.find_all("a", href=True)
    out, seen = [], {a["href"] for a in anchors if _in_navigation(a)}
    for a in anchors:
        m = _JOB_HREF_RE.search(a["href"])
        if not m:
            continue
        slug = m.group(2).rstrip("/").split("/")[-1].split("?")[0].lower()
        if slug in _NAV_SLUGS or len(slug) < 4:
            continue
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 4 or _NAV_TEXT_RE.match(text):
            continue
        if a["href"] in seen:
            continue
        seen.add(a["href"])
        te = a.find(["h1", "h2", "h3", "h4", "h5"]) or a.select_one("[class*='title']")
        title = te.get_text(" ", strip=True) if te else text
        out.append((a, a["href"], title))
    return out


def _openings_link(soup, page_url):
    """A same-host "see current openings" link, defragmented, or None. Never
    an aggregator's or an ATS vendor's: those are not a custom board."""
    host = host_of(page_url)
    if not host:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Defragmented, so an "#open-positions" link reads as this page and
        # the callers' no-self-hop check refuses it.
        absu = urldefrag(urljoin(page_url, href)).url
        if host_of(absu) != host or _OFFSITE_RE.search(absu):
            continue
        text = a.get_text(" ", strip=True).lower()
        if _OPENINGS_HREF_RE.search(href) or _OPENINGS_TEXT_RE.search(text):
            return absu
    return None


def _hop_target(soup, page_url):
    """The openings page to read in place of `page_url`, or None."""
    op = _openings_link(soup, page_url)
    return op if op and op.rstrip("/") != page_url.rstrip("/") else None


def _location_near(a, area=None):
    """The place named nearest a job link: in the link, else its parent,
    else its grandparent. Where `area` (a location regex) matches in that
    element its match wins, so a role listed "Alameda, CA | Durham, NC" is
    kept as a Durham job."""
    for el in (a, a.parent, a.parent.parent if a.parent else None):
        if el is None:
            continue
        text = el.get_text(" ", strip=True)
        m = (area.search(text) if area is not None else None) or LOC_TEXT_RE.search(text)
        if m:
            return m.group(0)
    return ""


def read_page(html, page_url, area=None, hop=True):
    """A careers page as the html decoder's payload: {"elements"}, one per
    job link (its `title`, `href`, absolute `url` and `location`, read
    `_location_near` with `area`), or {"hop"}, the openings page to read
    instead when the page holds fewer than CAREERS_PAGE_MIN_LINKS links
    (only while `hop`).

    >>> page = ('<ul><li><a href="/careers/data-engineer-7">Data Engineer</a> Durham, NC</li>'
    ...         '<li><a href="/careers/open-positions/">Careers</a></li></ul>')
    >>> read_page(page, "https://x.test/careers", hop=False)["elements"]
    [{'title': 'Data Engineer', 'href': '/careers/data-engineer-7', 'url': 'https://x.test/careers/data-engineer-7', 'location': 'Data Engineer Durham, NC'}]
    """
    soup = parse_markup(html)
    links = find_job_links(soup)
    target = _hop_target(soup, page_url) if hop and len(links) < config.CAREERS_PAGE_MIN_LINKS else None
    if target:
        return {"hop": target}
    out, seen = [], set()
    for a, href, title in links:
        url = urljoin(page_url, href)
        if url not in seen:
            seen.add(url)
            out.append({"title": clean_field(title)[:config.CAREERS_PAGE_TITLE_MAX],
                        "href": href, "url": url,
                        "location": clean_field(_location_near(a, area))
                        [:config.CAREERS_PAGE_LOCATION_MAX]})
    return {"elements": out}


def _anchor_soup(url):
    """`url`'s anchors, parsed; None on any failure. Silent: a probed page
    that is not a board is an expected answer."""
    try:
        r = SESSION.get(url, headers=HEADERS)
        if r.status_code != 200:
            return None
        return parse_markup(r.text)
    except Exception:
        return None


def _is_board(soup):
    return len(find_job_links(soup)) >= config.CAREERS_PAGE_MIN_LINKS


def is_board_page(html):
    """Whether a page's `html` holds CAREERS_PAGE_MIN_LINKS genuine job
    links; False when it will not parse."""
    try:
        return _is_board(parse_markup(html))
    except Exception:
        return False


def custom_board_listing_url(page_url, html=None):
    """The URL holding a custom board's listings: `page_url` when it is one
    (CAREERS_PAGE_MIN_LINKS genuine job links), else its openings page one
    hop away when that is; None otherwise, and always for an aggregator or
    ATS vendor host. `html`, when given, is `page_url`'s body.

    A decided verdict is cached BOARD_DETECT_CACHE_S per page URL (short,
    so a board going live or dead is re-checked soon); a failed fetch is
    not cached.
    """
    if _OFFSITE_RE.search(page_url):
        return None
    path = hashed_cache_path(cache_dir("board"), page_url)
    cached = json_cache_get(path, config.BOARD_DETECT_CACHE_S)
    if cached is not None:
        return cached.get("listing")
    soup = (parse_markup(html)
            if html is not None else _anchor_soup(page_url))
    if soup is None:
        return None
    result = page_url if _is_board(soup) else None
    target = None if result else _hop_target(soup, page_url)
    if target:
        s2 = _anchor_soup(target)
        result = target if s2 is not None and _is_board(s2) else None
    json_cache_put(path, {"listing": result})
    return result
