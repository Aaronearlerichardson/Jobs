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

import json
import re

from bs4 import BeautifulSoup

from .util import stable_id

_LI_VIEW_RE = re.compile(r"/jobs/view/(\d+)")
_LI_CURRENT_RE = re.compile(r"currentJobId=(\d+)")
_INDEED_JK_RE = re.compile(r"[?&]jk=([0-9a-f]+)", re.I)


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _sel(scope, *selectors):
    for sel in selectors:
        el = scope.select_one(sel)
        if el and _txt(el):
            return _txt(el)
    return ""


def _job(jid, title, company, url, location, description=""):
    title = (title or "").strip()
    if not title or not jid:
        return None
    return {"id": jid, "title": title[:120], "company": (company or "").strip()[:80],
            "url": url or "", "location": (location or "").strip()[:80],
            "description": (description or "")[:4000]}


# ─── LinkedIn ────────────────────────────────────────────────────────────

def parse_linkedin(soup, page_url=""):
    jobs = []
    # Card lists (authenticated + guest markup).
    cards = soup.select("li[data-occludable-job-id], div.job-card-container, "
                        "div.base-card, li.jobs-search-results__list-item")
    for c in cards:
        a = c.select_one("a[href*='/jobs/view/']")
        if not a:
            continue
        m = _LI_VIEW_RE.search(a.get("href", ""))
        jid = m.group(1) if m else c.get("data-occludable-job-id", "")
        title = _sel(c, ".job-card-list__title--link", ".job-card-container__link",
                     "h3.base-search-card__title") or _txt(a)
        title = re.sub(r"(.+?)\1$", r"\1", title)  # LinkedIn doubles the title
        company = _sel(c, ".artdeco-entity-lockup__subtitle",
                       ".job-card-container__primary-description",
                       "h4.base-search-card__subtitle")
        location = _sel(c, ".job-card-container__metadata-wrapper li",
                        ".artdeco-entity-lockup__caption",
                        "span.job-search-card__location")
        j = _job(f"linkedin_{jid}" if jid else f"linkedin_{stable_id(title, company)}",
                 title, company, f"https://www.linkedin.com/jobs/view/{jid}/" if jid else page_url,
                 location)
        if j:
            jobs.append(j)

    # Detail pane (the job open on the right; carries the description).
    m = _LI_CURRENT_RE.search(page_url) or _LI_VIEW_RE.search(page_url)
    title = _sel(soup, ".job-details-jobs-unified-top-card__job-title",
                 "h1.top-card-layout__title")
    if m and title:
        desc = _sel(soup, "#job-details", ".jobs-description__content",
                    ".description__text")
        j = _job(f"linkedin_{m.group(1)}", title,
                 _sel(soup, ".job-details-jobs-unified-top-card__company-name",
                      "a.topcard__org-name-link"),
                 f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
                 _sel(soup, ".job-details-jobs-unified-top-card__primary-description-container",
                      "span.topcard__flavor--bullet"),
                 desc)
        if j:
            jobs.append(j)
    return jobs


# ─── Indeed ──────────────────────────────────────────────────────────────

def parse_indeed(soup, page_url=""):
    jobs = []
    for c in soup.select("div.job_seen_beacon, td.resultContent"):
        a = c.select_one("h2 a[href], a.jcs-JobTitle")
        if not a:
            continue
        href = a.get("href", "")
        m = _INDEED_JK_RE.search(href) or re.search(r"jk=([0-9a-f]+)", str(a.get("data-jk", "")))
        jid = (m.group(1) if m else a.get("data-jk")) or stable_id(href, _txt(a))
        j = _job(f"indeed_{jid}", _txt(a),
                 _sel(c, "[data-testid='company-name']", "span.companyName"),
                 href if href.startswith("http") else f"https://www.indeed.com{href}",
                 _sel(c, "[data-testid='text-location']", "div.companyLocation"))
        if j:
            jobs.append(j)
    return jobs


# ─── Generic (JSON-LD + job-link sweep) ──────────────────────────────────

def parse_jsonld(soup, page_url=""):
    jobs = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else \
            data.get("itemListElement", [data]) if isinstance(data, dict) else []
        for it in items:
            jp = it.get("item", it) if isinstance(it, dict) else {}
            if not isinstance(jp, dict) or jp.get("@type") not in ("JobPosting",):
                continue
            org = jp.get("hiringOrganization") or {}
            loc = jp.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
            location = ", ".join(x for x in (addr.get("addressLocality"),
                                             addr.get("addressRegion")) if x)
            url = jp.get("url") or page_url
            j = _job(f"cap_{stable_id(url, jp.get('title'))}",
                     (jp.get("title") or jp.get("name") or ""),
                     org.get("name", "") if isinstance(org, dict) else str(org),
                     url, location,
                     re.sub(r"<[^>]+>", " ", jp.get("description") or ""))
            if j:
                jobs.append(j)
    return jobs


def parse_generic(soup, page_url=""):
    from .fetchers.company import find_job_links
    root_m = re.match(r"https?://[^/]+", page_url or "")
    root = root_m.group(0) if root_m else ""
    jobs = []
    for a, href, title in find_job_links(soup):
        url = href if href.startswith("http") else root + href
        j = _job(f"cap_{stable_id(url)}", title, "", url, "")
        if j:
            jobs.append(j)
    return jobs


# ─── Entry point ─────────────────────────────────────────────────────────

def _canonical_url(soup):
    el = soup.select_one("link[rel='canonical'][href]") \
        or soup.select_one("meta[property='og:url'][content]")
    return (el.get("href") or el.get("content") or "") if el else ""


def parse_page(url, html):
    """Parse captured page HTML -> (jobs, source_label). Layered parsers;
    de-duplicated by job id, site-specific hits first. When `url` is empty
    (Ctrl+S saves carry none), the site is detected from the canonical URL
    or distinctive DOM markers instead."""
    soup = BeautifulSoup(html, "html.parser")
    if not url:
        url = _canonical_url(soup)
    low = (url or "").lower()
    if not low.startswith("http"):
        if soup.select_one("[data-occludable-job-id], .job-card-container, "
                           ".base-search-card__title"):
            low = "linkedin."
        elif soup.select_one("div.job_seen_beacon, a.jcs-JobTitle"):
            low = "indeed."
    if "linkedin." in low:
        # Site-specific pages skip the generic link sweep — it would re-add
        # the same postings under synthetic ids.
        layers, source = [parse_linkedin, parse_jsonld], "linkedin"
    elif "indeed." in low:
        layers, source = [parse_indeed, parse_jsonld], "indeed"
    else:
        layers, source = [parse_jsonld, parse_generic], "page"

    by_id = {}
    for layer in layers:
        try:
            found = layer(soup, url)
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
                for k, v in j.items():
                    if v and len(str(v)) > len(str(prev.get(k) or "")):
                        prev[k] = v
    return list(by_id.values()), source
