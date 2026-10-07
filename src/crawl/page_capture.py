"""Extract jobs + companies from manually captured page HTML.

The supply side of the manual capture flow: you browse LinkedIn / Indeed /
any job board logged in as yourself, and either click the userscript button
(POSTs the live DOM to capture.py's local server) or save the page with
Ctrl+S and run `python capture.py <files>`. This module turns that HTML
into normalized job dicts:

    {id, title, company, url, location, description}

Parsing is layered: site-specific selectors for LinkedIn and Indeed cards,
then JSON-LD JobPosting blocks, then a generic job-link sweep — whichever
layers hit, results are merged and de-duplicated by job id.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

from src import config
from src.net.util import (JSON, first, host_of, jsonld_scripts, links, node_text, parse_markup,
                          stable_id, strip_html, xpath)
from src.rows import FetchedJob

if TYPE_CHECKING:
    from lxml import etree


def _txt(el: etree._Element | None) -> str:
    return re.sub(r"\s+", " ", node_text(el)) if el is not None else ""


def _sel(scope: etree._Element, *paths: str) -> str:
    for path in paths:
        el = first(path, scope)
        if el is not None and _txt(el):
            return _txt(el)
    return ""


def _class(name: str) -> str:
    """An XPath test: the element's class attribute lists `name`."""
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def _first_string(tree: etree._Element, rx: re.Pattern[str]) -> Any:
    """The first text node in `tree` (script text included) that `rx`
    searches, or None."""
    return next((s for s in tree.xpath("//text()") if rx.search(s)), None)


def _company_site(*urls: object) -> str:
    """First real company-owned website (scheme+host) among the given URLs,
    skipping aggregator/ATS/social hosts. Recorded on a lead as careers_url so
    the resolver can probe {domain}/careers instead of guessing the domain from
    the name (which misses acronym/hyphenated domains: OXB->oxb.com,
    'United Imaging'->united-imaging.com). '' if none qualifies."""
    # Aggregator / ATS / social hosts — a URL on one of these is NOT the
    # company's own website, so it can't seed a careers-page guess for the lead
    # resolver. Only a company-owned domain is worth recording.
    agg_host = config.hosts_re(config.SHARED_HOSTS + (
        "facebook.", "twitter.", "x.com", "youtube.", "instagram.", "crunchbase",
        "wellfound", "schema.org"))
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        m = re.match(r"https?://([^/]+)", u.strip())
        if not m or agg_host.search(m.group(1)):
            continue
        return f"https://{m.group(1)}"
    return ""


def _job(jid: str, title: str | None, company: str | None, url: str | None,
         location: str | None, description: str | None = "",
         company_url: str = "") -> FetchedJob | None:
    title = (title or "").strip()
    if not title or not jid:
        return None
    j: FetchedJob = {"id": jid, "title": title[:120],
                         "company": (company or "").strip()[:80],
                         "url": url or "", "location": (location or "").strip()[:80],
                         "description": (description or "")[:config.MAX_DESC_CHARS]}
    if company_url:
        j["company_url"] = company_url
    return j


# ─── LinkedIn ────────────────────────────────────────────────────────────
#
# Three markup generations, all seen in the wild (live DOM and Ctrl+S saves):
#   1. current obfuscated classes: job anchors carry no stable class names,
#      but their visible strings are [Title, "Company · Location", "Posted…"];
#   2. classic authed cards (.job-card-container / artdeco lockups);
#   3. guest/logged-out cards (.base-card).
# Detail pages are parsed from the <title> tag ("Job | Company | LinkedIn"),
# the "(Remote/Hybrid/On-site)" location string, and the "About the job"
# section. NOTE: "Top job picks" collection pages are virtualized — a Ctrl+S
# save contains almost no job data; save Job tracker / search / detail pages.

_NONTITLE_RE = re.compile(r"^(apply|easy apply|save|saved|dismiss|x)$", re.I)


def _split_company_loc(text: str) -> tuple[str, str]:
    company, _, location = text.partition("\u00b7")
    return company.strip(), location.strip()


def parse_linkedin(tree: etree._Element, page_url: str = "") -> list[FetchedJob]:
    """A LinkedIn page's jobs: its job anchors and guest cards, and a
    detail page's own posting.

    >>> page = ('<li><a href="/jobs/view/123/"><span>Data Engineer</span>'
    ...         '<span>Acme \\u00b7 Durham, NC</span></a></li>')
    >>> [(j["id"], j["title"], j["company"], j["location"]) for j in parse_linkedin(parse_markup(page))]
    [('linkedin_123', 'Data Engineer', 'Acme', 'Durham, NC')]
    >>> page = ('<title>Data Engineer | Acme | LinkedIn</title><p>Durham, NC (Hybrid)</p>'
    ...         '<div><h2>About the job</h2><p>' + 'Build pipelines. ' * 30 + '</p></div>')
    >>> [(j["company"], j["location"], j["description"][:30]) for j in parse_linkedin(parse_markup(page))]
    [('Acme', 'Durham, NC (Hybrid)', 'About the job Build pipelines.')]
    """
    li_view = re.compile(r"/jobs/view/(\d+)")
    jobs = []
    # Job anchors (generations 1 + 2). Visible strings first; classic-card
    # selectors as fallback for the older markup.
    for a in xpath("//a[contains(@href, '/jobs/view/')]")(tree):
        m = li_view.search(a.get("href", ""))
        if not m:
            continue
        parts = node_text(a, None)
        if not parts or _NONTITLE_RE.match(parts[0]):
            continue
        title = re.sub(r"(.+?)\1$", r"\1", parts[0])   # LinkedIn doubles titles
        company = location = ""
        for p in parts[1:4]:
            if "\u00b7" in p:
                company, location = _split_company_loc(p)
                break
        card = next(a.iterancestors("li"), None)
        if card is None:
            card = a.getparent()
        if not company and card is not None:
            company = _sel(card, f".//*[{_class('artdeco-entity-lockup__subtitle')}]",
                           f".//*[{_class('job-card-container__primary-description')}]",
                           f".//h4[{_class('base-search-card__subtitle')}]")
            location = location or _sel(
                card, f".//li[ancestor::*[{_class('job-card-container__metadata-wrapper')}]]",
                f".//*[{_class('artdeco-entity-lockup__caption')}]",
                f".//span[{_class('job-search-card__location')}]")
        j = _job(f"linkedin_{m.group(1)}", title, company,
                 f"https://www.linkedin.com/jobs/view/{m.group(1)}/", location)
        if j:
            jobs.append(j)

    # Guest cards (generation 3): title/company live outside the anchor.
    for c in xpath(f"//div[{_class('base-card')}]")(tree):
        a = first(".//a[contains(@href, '/jobs/view/')]", c)
        if a is None:
            continue
        m = li_view.search(a.get("href", ""))
        title = _sel(c, f".//h3[{_class('base-search-card__title')}]")
        j = _job(f"linkedin_{m.group(1)}" if m else f"linkedin_{stable_id(title)}",
                 title, _sel(c, f".//h4[{_class('base-search-card__subtitle')}]"),
                 a.get("href", "").split("?")[0],
                 _sel(c, f".//span[{_class('job-search-card__location')}]"))
        if j:
            jobs.append(j)

    # Detail page: <title> is "Job Title | Company | LinkedIn" (with an
    # unread-count "(9) " prefix on live DOM). No stable numeric id is
    # recoverable, so the id hashes title+company.
    te = first("//title", tree)
    t = node_text(te) if te is not None else ""
    tm = re.match(r"^(?:\(\d+\)\s*)?(.+?)\s*\|\s*(.+?)\s*\|\s*LinkedIn$", t)
    if tm:
        title, company = tm.group(1), tm.group(2)
        loc_el = _first_string(tree, re.compile(r"\((Remote|Hybrid|On-site)\)"))
        location = re.sub(r"\s+", " ", str(loc_el)).strip() if loc_el else ""
        desc, marker = "", _first_string(tree, re.compile(r"^\s*About the job\s*$"))
        # The element holding the marker: a tail's is its element's parent.
        sec = None if marker is None else \
            marker.getparent() if marker.is_text else marker.getparent().getparent()
        for _ in range(5):
            if sec is None or len(node_text(sec)) > 400:
                break
            sec = sec.getparent()
        if sec is not None:
            desc = node_text(sec)
        j = _job(f"linkedin_{stable_id(title, company)}", title, company,
                 page_url or "", location, desc)
        if j:
            twin = next((x for x in jobs
                         if x["title"].lower() == j["title"].lower()
                         and not x["company"]), None)
            if twin is not None:
                # Same job seen as a bare anchor: keep its numeric id/url,
                # take the rich fields from the title-tag parse.
                twin.update({"company": j["company"], "location": j["location"],
                            "description": j["description"]})
            else:
                jobs.append(j)
    return jobs


# ─── Indeed ──────────────────────────────────────────────────────────────

def parse_indeed(tree: etree._Element, page_url: str = "") -> list[FetchedJob]:
    """An Indeed results page's job cards.

    >>> page = ('<div class="job_seen_beacon"><h2><a href="/viewjob?jk=ab12">Data Engineer</a></h2>'
    ...         '<span data-testid="company-name">Acme</span>'
    ...         '<div data-testid="text-location">Durham, NC</div></div>')
    >>> [(j["id"], j["title"], j["company"], j["url"], j["location"])
    ...  for j in parse_indeed(parse_markup(page))]
    [('indeed_ab12', 'Data Engineer', 'Acme', 'https://www.indeed.com/viewjob?jk=ab12', 'Durham, NC')]
    """
    jobs = []
    cards = f"//div[{_class('job_seen_beacon')}] | //td[{_class('resultContent')}]"
    title_link = f".//a[@href][ancestor::h2] | .//a[{_class('jcs-JobTitle')}]"
    for c in xpath(cards)(tree):
        a = first(title_link, c)
        if a is None:
            continue
        href = a.get("href", "")
        m = re.search(r"[?&]jk=([0-9a-f]+)", href, re.I) or re.search(r"jk=([0-9a-f]+)", str(a.get("data-jk", "")))
        jid = (m.group(1) if m else a.get("data-jk")) or stable_id(href, _txt(a))
        j = _job(f"indeed_{jid}", _txt(a),
                 _sel(c, ".//*[@data-testid='company-name']",
                      f".//span[{_class('companyName')}]"),
                 href if href.startswith("http") else f"https://www.indeed.com{href}",
                 _sel(c, ".//*[@data-testid='text-location']",
                      f".//div[{_class('companyLocation')}]"))
        if j:
            jobs.append(j)
    return jobs


# ─── Meta Careers (metacareers.com) ──────────────────────────────────────
#
# Meta's careers site is custom-built (not a standard ATS) and blocks
# server-side fetches, so the ONLY way in is a live browser capture
# (userscript button or Ctrl+S). Job links are stable —
#   /profile/job_details/<numeric id>/
# — but Meta's CSS classes are obfuscated, so titles come from the link text
# and locations from a "City, ST" / "Remote" heuristic rather than selectors.
# (The local-tech NC gate still applies downstream, so out-of-NC Meta roles
# are dropped at ingest — as intended.)


def parse_metacareers(tree: etree._Element, page_url: str = "") -> list[FetchedJob]:
    """A metacareers page's job cards, and a detail page's own posting.

    >>> page = ('<div><a href="/profile/job_details/42/">Research Scientist</a>'
    ...         '<span>Menlo Park, CA</span></div>')
    >>> [(j["id"], j["title"], j["location"]) for j in parse_metacareers(parse_markup(page))]
    [('meta_42', 'Research Scientist', 'Menlo Park, CA')]
    """
    meta_job = re.compile(r"/profile/job_details/(\d+)")
    meta_loc = re.compile(
        r"([A-Z][A-Za-z.\-]+(?:\s[A-Z][A-Za-z.\-]+)*,\s*[A-Z]{2}\b"
        r"|Remote(?:,\s*[A-Za-z .]+)?|Multiple Locations)")
    jobs: list[FetchedJob] = []
    seen: set[str] = set()
    # Listing/search page: one card per job, each linking to a job_details URL.
    for a in xpath("//a[contains(@href, '/profile/job_details/')]")(tree):
        m = meta_job.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        jid = m.group(1)
        seen.add(jid)
        card = next(a.iterancestors("div", "li"), a)
        strings = [s for s in node_text(a, None) if not _NONTITLE_RE.match(s)]
        title = strings[0] if strings else ""
        if not title:
            title = _txt(next(card.iterdescendants("h1", "h2", "h3", "h4"), None))
        # Search for the location in the card text with the TITLE removed —
        # titles like "Engineer, Reality Labs" carry their own comma and would
        # otherwise bleed into the greedy "City, ST" match.
        rest = node_text(card).replace(title, " ", 1) if title else node_text(card)
        lm = meta_loc.search(rest)
        j = _job(f"meta_{jid}", title, "Meta",
                 f"https://www.metacareers.com/profile/job_details/{jid}/",
                 lm.group(1) if lm else "")
        if j:
            jobs.append(j)

    # Single job-detail page: emit/enrich from the title tag + og:description.
    dm = meta_job.search(page_url or "")
    if dm:
        jid = dm.group(1)
        og = first("//meta[@property='og:title'][@content]", tree)
        te = first("//title", tree)
        raw = (og.get("content") if og is not None else "") or \
            (node_text(te) if te is not None else "")
        title = re.sub(r"\s*[|\-–—]\s*Meta\b.*$", "", raw).strip() or raw
        ogd = first("//meta[@property='og:description'][@content]", tree)
        desc = ogd.get("content", "") if ogd is not None else ""
        body = node_text(tree).replace(title, " ", 1) if title else node_text(tree)
        lm = meta_loc.search(body)
        j = _job(f"meta_{jid}", title, "Meta",
                 f"https://www.metacareers.com/profile/job_details/{jid}/",
                 lm.group(1) if lm else "", desc)
        if j:
            twin = next((x for x in jobs if x["id"] == j["id"]), None)
            if twin:  # listing card + open detail: keep the richer fields
                for k, v in j.items():      # k: any key of j, not a literal
                    if v and len(str(v)) > len(str(twin.get(k) or "")):
                        cast(dict[str, Any], twin)[k] = v
            else:
                jobs.append(j)
    return jobs


# ─── Generic (JSON-LD + job-link sweep + job-card sweep) ─────────────────

def parse_jsonld(tree: etree._Element, page_url: str = "") -> list[FetchedJob]:
    """A page's schema.org JobPostings, the employer's own site kept as
    `company_url`.

    >>> page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Chemist",'
    ...         ' "url": "https://x.test/j/1", "hiringOrganization": {"name": "Acme", "sameAs":'
    ...         ' "https://acme.example/about"}, "jobLocation": {"address": {"addressLocality":'
    ...         ' "Durham", "addressRegion": "NC"}}}</script>')
    >>> [(j["title"], j["company"], j["location"], j["company_url"])
    ...  for j in parse_jsonld(parse_markup(page))]
    [('Chemist', 'Acme', 'Durham, NC', 'https://acme.example')]

    A string, list or otherwise non-object `address` is read without error:
    a string gives no location, a list its first object.

    >>> def loc(addr):
    ...     d = {"@type": "JobPosting", "title": "T", "url": "https://x.test/1",
    ...          "jobLocation": {"address": addr}}
    ...     tag = '<script type="application/ld+json">%s</script>' % json.dumps(d)
    ...     return [j["location"] for j in parse_jsonld(parse_markup(tag))]
    >>> loc("Durham, NC"), loc({"addressLocality": "Cary"}), loc([{"addressRegion": "NC"}])
    ([''], ['Cary'], ['NC'])
    """
    jobs = []
    for tag in jsonld_scripts(tree):
        try:
            data: JSON = json.loads(tag.text or "")
        except Exception:
            continue
        listed = data.get("itemListElement", [data]) if isinstance(data, Mapping) else data
        for it in listed if isinstance(listed, list) else []:
            jp = it.get("item", it) if isinstance(it, Mapping) else None
            if not isinstance(jp, Mapping) or jp.get("@type") != "JobPosting":
                continue
            org = jp.get("hiringOrganization") or {}
            loc = jp.get("jobLocation")
            if isinstance(loc, list):
                loc = loc[0] if loc else None
            addr = loc.get("address") if isinstance(loc, Mapping) else None
            if isinstance(addr, list):
                addr = addr[0] if addr else None
            parts: list[JSON] = ([addr.get("addressLocality"), addr.get("addressRegion")]
                     if isinstance(addr, Mapping) else [])
            location = ", ".join(x for x in parts if x and isinstance(x, str))
            url = jp.get("url")
            url = url if url and isinstance(url, str) else page_url
            # schema.org marks the employer's own site in hiringOrganization
            # (sameAs / url) — capture it as the lead's careers_url hint.
            org_site = ""
            if isinstance(org, Mapping):
                same = org.get("sameAs")
                same = same if isinstance(same, list) else [same]
                org_site = _company_site(*same, org.get("url"))
            name = jp.get("title") or jp.get("name") or ""
            org_name = org.get("name", "") if isinstance(org, Mapping) else str(org)
            j = _job(f"cap_{stable_id(url, jp.get('title'))}",
                     name if isinstance(name, str) else None,
                     org_name if isinstance(org_name, str) else None,
                     url, location,
                     strip_html(jp.get("description")),
                     company_url=org_site)
            if j:
                jobs.append(j)
    return jobs


# The "any careers site" sweep, in two passes. find_job_links (the custom
# board fetcher's own detector) takes anchors whose PATH says job —
# /jobs/<id>-<slug>, /job/<slug>, /careers/<slug> — which covers what a
# browser save of a JS-rendered board carries once the cards are in the DOM:
# a Workday-fed WordPress table, an iCIMS Attract (Jibe) results list, a
# /jobs/<id>-<slug> results page. Hosted boards that key a posting on a bare
# id under the tenant (jobs.polymer.co/<tenant>/<id>,
# apply.workable.com/<tenant>/j/<id>/) say nothing job-shaped in the path,
# so the second pass takes an anchor on a job-board host whose LAST path
# segment is id-shaped and that carries its own heading — the shape every
# job card shares, whatever renders it.


def _card_scopes(a: etree._Element) -> Iterator[etree._Element]:
    """The elements a job card's fields can live in: the anchor, its parent,
    and the grandparent only while that is still ONE card -- every link in it
    points where this one does (a table row that links the same posting from
    each cell). A whole results list would lend a neighbour's location to a
    card that has none."""
    yield a
    parent = a.getparent()
    if parent is not None:
        yield parent
        gp = parent.getparent()
        if gp is not None and len(set(xpath(".//a/@href")(gp))) == 1:
            yield gp


def _card_title(a: etree._Element) -> str:
    te = next(a.iterdescendants("h1", "h2", "h3", "h4", "h5"), None)
    if te is None:
        te = first(".//*[contains(@data-ui, 'title') or contains(@class, 'title')]", a)
    return _txt(te)


def _card_location(a: etree._Element, title: str = "") -> str:
    """Location for a job card: the smallest element inside the card whose
    whole text is a place ("Cambridge, MA", "Remote"), else the first place
    named in the card's text once the title is taken out of it."""
    # "City, ST" | "Remote" (optionally qualified) | "Multiple Locations". At most
    # four words before the comma: enough for "Research Triangle Park, NC",
    # too few to swallow a title that precedes the place in one run of text.
    place = re.compile(
        r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,3},\s*[A-Z]{2}\b"
        r"|\bRemote\b(?:\s*[-–,(]\s*[A-Za-z .]+\)?)?|Multiple Locations)")
    for scope in _card_scopes(a):
        for el in scope.iterdescendants("li", "span", "td", "div", "p", "small"):
            t = _txt(el)
            if not t or len(t) > 60 or (title and title in t):
                continue
            if place.fullmatch(t):
                return t
    for scope in _card_scopes(a):
        text = node_text(scope)
        if title:
            text = text.replace(title, " ", 1)
        m = place.search(text)
        if m:
            return re.sub(r"^(?:Full|Part)[- ]time\s+", "", m.group(1), flags=re.I)
    return ""


def parse_generic(tree: etree._Element, page_url: str = "") -> list[FetchedJob]:
    from src.ats.board.custom import find_job_links
    jobs: list[FetchedJob] = []
    seen: set[str] = set()

    def _emit(a: etree._Element, href: str, title: str) -> None:
        url = urljoin(page_url or "", href)
        key = url.split("?")[0].rstrip("/")
        if key in seen:
            return
        seen.add(key)
        j = _job(f"cap_{stable_id(url)}", title, "", url,
                 _card_location(a, title))
        if j:
            jobs.append(j)

    for a, href, title in find_job_links(tree):
        _emit(a, href, title)

    for a in links(tree):
        href = a.get("href").split("?")[0]
        host = host_of(urljoin(page_url or "", href))
        # /jobs/<id>/<slug>/job (iCIMS Attract) ends in a nav-looking "job"
        # segment that find_job_links refuses; an id straight after /jobs/
        # is a posting.
        if re.search(r"/jobs?/[0-9]{3,}(?:/|$)", href, re.I):
            pass
        elif not re.search(r"/(?:j/)?([0-9]{4,}|[A-Z0-9]{6,})/?$", href) or (
                "/j/" not in href
                and not re.match(r"^(jobs|careers|apply|boards|talent|recruiting)\.",
                                 host, re.I)):
            continue
        title = _card_title(a) or _txt(a)
        if len(title) >= 4 and not _NONTITLE_RE.match(title):
            _emit(a, a.get("href"), title)
    return jobs


# ─── Entry point ─────────────────────────────────────────────────────────

def _canonical_url(tree: etree._Element) -> str:
    el = first("//link[normalize-space(@rel)='canonical'][@href]", tree)
    if el is None:
        el = first("//meta[@property='og:url'][@content]", tree)
    return (el.get("href") or el.get("content") or "") if el is not None else ""


def page_url(html: str, url: str = "") -> str:
    """The URL a captured page came from: `url` when the caller knows it
    (userscript POST, Chrome's "saved from url" comment), else the page's own
    canonical / og:url tag. What capture.py hands to store.company_by_host.

    >>> page_url("<html><head><link rel='canonical' href='https://jobs.x.org/a'>",
    ...          "")
    'https://jobs.x.org/a'
    >>> page_url("<html>", "https://y.org/")
    'https://y.org/'
    >>> page_url("<html>")
    ''
    """
    if url:
        return url
    return _canonical_url(parse_markup(html))


def parse_page(url: str, html: str) -> tuple[list[FetchedJob], str]:
    """Parse captured page HTML -> (jobs, source_label). Layered parsers;
    de-duplicated by job id, site-specific hits first. When `url` is empty
    (Ctrl+S saves carry none), the site is detected from the canonical URL
    or distinctive DOM markers instead.

    An id-tailed card with a heading is a posting only on a job-board host
    (or a /j/ path); a card naming no place borrows none from a neighbour:

    >>> parse_page("", '<link rel="canonical" href="https://www.acme.com/news">'
    ...            '<a href="https://www.acme.com/press/10001"><h2>New office</h2></a>')
    ([], 'page')
    >>> jobs, _ = parse_page("", '<link rel="canonical" href="https://jobs.acme.org/search"><ul>'
    ...     '<li><a href="/jobs/1001-analyst">Analyst</a> <span>Durham, NC</span></li>'
    ...     '<li><a href="/jobs/1002-engineer">Engineer</a></li></ul>')
    >>> [(j["title"], j["location"]) for j in jobs]
    [('Analyst', 'Durham, NC'), ('Engineer', '')]
    """
    tree = parse_markup(html, url=url)
    if not url:
        url = _canonical_url(tree)
    low = (url or "").lower()
    if not low.startswith("http"):
        te = first("//title", tree)
        t = node_text(te, "") if te is not None else ""
        linkedin_marks = (
            "//a[contains(@href, 'linkedin.com/jobs/view/')] | //*[@data-occludable-job-id]"
            f" | //*[{_class('job-card-container')}] | //*[{_class('base-search-card__title')}]"
            " | //link[contains(@href, 'licdn.com')] | //img[contains(@src, 'licdn.com')]")
        indeed_marks = f"//div[{_class('job_seen_beacon')}] | //a[{_class('jcs-JobTitle')}]"
        if t.endswith("LinkedIn") or first(linkedin_marks, tree) is not None:
            low = "linkedin."
        elif first(indeed_marks, tree) is not None:
            low = "indeed."
        elif first("//a[contains(@href, '/profile/job_details/')]", tree) is not None:
            low = "metacareers."
    layers: list[Callable[[etree._Element, str], list[FetchedJob]]]
    if "linkedin." in low:
        # Site-specific pages skip the generic link sweep — it would re-add
        # the same postings under synthetic ids.
        layers = [parse_linkedin, parse_jsonld]
        source = "linkedin"
    elif "indeed." in low:
        layers = [parse_indeed, parse_jsonld]
        source = "indeed"
    elif "metacareers." in low:
        layers = [parse_metacareers]
        source = "metacareers"
    else:
        layers = [parse_jsonld, parse_generic]
        source = "page"

    by_id: dict[str, FetchedJob] = {}
    for layer in layers:
        found: list[FetchedJob]
        try:
            found = layer(tree, url)
        except Exception as e:
            print(f"    [!] {layer.__name__}: {e}")
            found = []
        seen_urls = {(jj.get("url") or "").split("?")[0].rstrip("/")
                     for jj in by_id.values()}
        for j in found:
            prev = by_id.get(j["id"])
            if prev is None:
                u = (j.get("url") or "").split("?")[0].rstrip("/")
                if u and u in seen_urls:
                    continue                      # same posting, later layer
                by_id[j["id"]] = j
            else:
                # Same job seen twice (e.g. results card + open detail pane):
                # merge, keeping the richer field from either.
                for k, v in j.items():      # k: any key of j, not a literal
                    if v and len(str(v)) > len(str(prev.get(k) or "")):
                        cast(dict[str, Any], prev)[k] = v
    return list(by_id.values()), source
