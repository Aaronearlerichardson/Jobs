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

from __future__ import annotations

import asyncio
import logging
import re
from typing import cast
from urllib.parse import urldefrag, urljoin

from lxml import etree
from typing_extensions import TypedDict

from src import config
from src.match.locality import LocationRE
from src.net import http
from src.net.http import HEADERS
from src.net.robots import FETCH_ERRORS
from src.net.util import (LOC_TEXT_RE, cache_dir, clean_field, first,
                          JSON, hashed_cache_path, host_of, json_cache_get,
                          json_cache_put, links, node_text, parse_markup)

_log = logging.getLogger(__name__)
_OFFSITE_RE = config.hosts_re(config.SHARED_HOSTS)


class PageElement(TypedDict, closed=True):
    """One job link `read_page` found."""

    title: str
    href: str
    url: str
    location: str


class Page(TypedDict, total=False, closed=True):
    """`read_page`'s answer: the links, or the page to read instead."""

    elements: list[PageElement]
    hop: str


def find_job_links(tree: etree._Element) -> list[tuple[etree._Element, str, str]]:
    """(anchor, href, title) for each real job-posting link on a careers
    page's parsed `tree`, one per href: nav, login and index links filtered,
    and any href the site's navigation links (an anchor inside a <nav>, or
    inside an element whose class or id names a nav bar or menu), a section
    of the site wherever the page repeats it. The title is the anchor's
    heading (or title-classed) element's text, else its own.

    >>> page = ('<a href="/careers/facilities-engineer-88"><h3>Facilities Engineer</h3> Apply</a>'
    ...         '<a href="/careers/open-positions/">View Current Job Openings</a>'
    ...         '<a href="/jobs/login?loginOnly=1">External Candidate Login</a>')
    >>> [(href, title) for _a, href, title in find_job_links(parse_markup(page))]
    [('/careers/facilities-engineer-88', 'Facilities Engineer')]
    """
    nav_slugs = {
        "open-positions", "open-roles", "career-opportunities", "current-openings",
        "job-openings", "openings", "opportunities", "jobs", "job", "careers",
        "career", "apply", "application", "search", "all", "browse", "students",
        "internships", "benefits", "culture", "life", "teams", "team", "departments",
        "locations", "faq", "contact", "index", "home", "overview",
        "login", "logon", "signin", "sign-in",
    }
    # A class or id naming site navigation: "navbar", "sub-nav", "menu-item",
    # "header__nav", "topnavigation".
    nav_block = (r"(?i)(?:^|[-_])(?:top|sub|main|site|desk|mobile|global)?"
                 r"(?:nav|navbar|navigation|menu)s?(?:$|[-_])")
    anchors = links(tree)
    out: list[tuple[etree._Element, str, str]] = []
    seen = {a.get("href") for a in anchors
            if any(el.tag == "nav" or any(re.search(nav_block, t)
                                          for t in (*(el.get("class") or "").split(),
                                                    el.get("id") or ""))
                   for el in a.iterancestors())}
    for a in anchors:
        href = a.get("href")
        m = re.search(r"/(careers?|jobs?|positions?|openings?|roles?|job)/"
                      r"([a-z0-9][a-z0-9\-_/]{2,})", href, re.I)
        if not m:
            continue
        slug = m.group(2).rstrip("/").split("/")[-1].split("?")[0].lower()
        if slug in nav_slugs or len(slug) < 4:
            continue
        text = node_text(a)
        if not text or len(text) < 4 or re.match(
                r"^(careers?|jobs?|view (all|current|open)|open (positions?|roles?)|"
                r"see (all|open)|apply|search|browse|all (jobs|openings|roles)|"
                r"current openings|open positions|view (job )?openings|join( us)?|"
                r"work (with|at) us|learn more|explore|opportunities|all roles)\b", text, re.I):
            continue
        if href in seen:
            continue
        seen.add(href)
        te = next(a.iterdescendants("h1", "h2", "h3", "h4", "h5"), None)
        if te is None:
            te = first(".//*[contains(@class, 'title')]", a)
        title = node_text(te) if te is not None else text
        out.append((a, href, title))
    return out


def _openings_link(tree: etree._Element, page_url: str) -> str | None:
    """A same-host "see current openings" link, defragmented, or None. Never
    an aggregator's or an ATS vendor's: those are not a custom board."""
    host = host_of(page_url)
    if not host:
        return None
    for a in links(tree):
        href = a.get("href")
        # Defragmented, so an "#open-positions" link reads as this page and
        # the callers' no-self-hop check refuses it.
        absu = urldefrag(urljoin(page_url, href)).url
        if host_of(absu) != host or _OFFSITE_RE.search(absu):
            continue
        text = node_text(a).lower()
        if re.search(r"/(open-positions|open-roles|career-opportunities|current-openings|"
                     r"job-openings|openings|opportunities|positions|jobs)\b", href, re.I) \
                or re.search(r"(current|open|view|see|all).{0,12}(opening|position|role|job)",
                             text, re.I):
            return absu
    return None


def _hop_target(tree: etree._Element, page_url: str) -> str | None:
    """The openings page to read in place of `page_url`, or None."""
    op = _openings_link(tree, page_url)
    return op if op and op.rstrip("/") != page_url.rstrip("/") else None


def _location_near(a: etree._Element, area: LocationRE | None = None) -> str:
    """The place named nearest a job link: in the link, else its parent,
    else its grandparent. Where `area` (a location regex) matches in that
    element its match wins, so a role listed "Alameda, CA | Durham, NC" is
    kept as a Durham job."""
    parent = a.getparent()
    for el in (a, parent, parent.getparent() if parent is not None else None):
        if el is None:
            continue
        text = node_text(el)
        m = (area.search(text) if area is not None else None) or LOC_TEXT_RE.search(text)
        if m:
            return m.group(0)
    return ""


def read_page(tree: etree._Element, page_url: str, area: LocationRE | None = None,
              hop: bool = True) -> Page:
    """A careers page's parsed `tree` as the html decoder's payload:
    {"elements"}, one per job link (its `title`, `href`, absolute `url` and
    `location`, read `_location_near` with `area`), or {"hop"}, the
    openings page to read instead when the page holds fewer than
    CAREERS_PAGE_MIN_LINKS links (only while `hop`).

    >>> page = ('<ul><li><a href="/careers/data-engineer-7">Data Engineer</a> Durham, NC</li>'
    ...         '<li><a href="/careers/open-positions/">Careers</a></li></ul>')
    >>> read_page(parse_markup(page), "https://x.test/careers", hop=False)["elements"]
    [{'title': 'Data Engineer', 'href': '/careers/data-engineer-7', 'url': 'https://x.test/careers/data-engineer-7', 'location': 'Data Engineer Durham, NC'}]
    """
    links = find_job_links(tree)
    target = _hop_target(tree, page_url) if hop and len(links) < config.CAREERS_PAGE_MIN_LINKS else None
    if target:
        return {"hop": target}
    out: list[PageElement] = []
    seen: set[str] = set()
    for a, href, title in links:
        url = urljoin(page_url, href)
        if url not in seen:
            seen.add(url)
            out.append({"title": clean_field(title)[:config.CAREERS_PAGE_TITLE_MAX],
                        "href": href, "url": url,
                        "location": clean_field(_location_near(a, area))
                        [:config.CAREERS_PAGE_LOCATION_MAX]})
    return {"elements": out}


async def _page_tree(url: str) -> etree._Element | None:
    """`url`'s page, parsed off the loop; None on any failure. Silent: a
    probed page that is not a board is an expected answer."""
    try:
        r = await http.send("GET", url, headers=HEADERS)
        if r.status_code != 200:
            return None
        return await asyncio.to_thread(lambda: parse_markup(r.text, url=url))
    except (*FETCH_ERRORS, ValueError, LookupError) as e:
        _log.debug("page tree %s: %s", url, e)
        return None


def _is_board(tree: etree._Element) -> bool:
    return len(find_job_links(tree)) >= config.CAREERS_PAGE_MIN_LINKS


def is_board_page(html: str) -> bool:
    """Whether a page's `html` holds CAREERS_PAGE_MIN_LINKS genuine job
    links; False when it will not parse.

    >>> is_board_page("<a href='/careers/'>Careers</a>")
    False
    """
    return _is_board(parse_markup(html))


async def custom_board_listing_url(page_url: str, html: str | None = None) -> str | None:
    """The URL holding a custom board's listings: `page_url` when it is one
    (CAREERS_PAGE_MIN_LINKS genuine job links), else its openings page one
    hop away when that is; None otherwise, and always for an aggregator or
    ATS vendor host. `html`, when given, is `page_url`'s body.

    A decided verdict is cached BOARD_DETECT_CACHE_S per page URL (short,
    so a board going live or dead is re-checked soon); a failed fetch is
    not cached. Pages are parsed and judged off the loop.

    >>> asyncio.run(custom_board_listing_url("https://www.indeed.com/jobs?q=x",
    ...                                      "<html></html>")) is None
    True
    """
    if _OFFSITE_RE.search(page_url):
        return None
    path = hashed_cache_path(cache_dir("board"), page_url)
    cached = await asyncio.to_thread(json_cache_get, path, config.BOARD_DETECT_CACHE_S)
    if cached is not None:
        return cast(str | None, cast(dict[str, JSON], cached).get("listing"))
    tree = (await asyncio.to_thread(parse_markup, html, url=page_url)
            if html is not None else await _page_tree(page_url))
    if tree is None:
        return None
    result = page_url if await asyncio.to_thread(_is_board, tree) else None
    target = None if result else await asyncio.to_thread(_hop_target, tree, page_url)
    if target:
        t2 = await _page_tree(target)
        result = target if t2 is not None and await asyncio.to_thread(_is_board, t2) else None
    await asyncio.to_thread(json_cache_put, path, {"listing": result})
    return result
