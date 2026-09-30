"""Getro-powered network job boards (a VC portfolio, an industry association).

A Getro board is one host that aggregates the openings of many employers —
the board's members — and names the employer on every posting. The board
itself is a Next.js site whose listing loads from ``api.getro.com``, and
that API host publishes ``Disallow: /`` for every crawler, so the listing
is never read from there. Two things on the BOARD host are enough:

    /sitemap.xml                       every job URL, with <lastmod>
    /companies/<org>/jobs/<id>-<slug>  one job, server-rendered: the page
                                       embeds the full record in
                                       ``<script id="__NEXT_DATA__">``

The sitemap is the complete listing (the server-rendered /jobs page shows
only the first twenty). Each job URL already carries the title as a slug,
so postings are screened on that BEFORE the page is fetched — a board of a
thousand jobs costs one sitemap request plus one page per posting whose
title survives the relevance filter, capped by ``max_details``, newest
first. A title that says nothing ("Associate") never gets its page
fetched; that is the trade for staying polite on a shared host.

Every job carries an ``_employer`` record (name, slug, board host, domain)
read off the same page. What happens to it is somebody else's job:
``src.discovery.apply.attribute_employers`` matches the employer to the
roster and queues the unknown ones for review. That function lived here,
which made this the one module under src/ats that wrote to the store —
a fetcher parses a board and returns job dicts, and stops.

Notes:
    A board that fronts itself with a browser challenge (Cloudflare's
    "Just a moment..." answers even robots.txt with a 403) simply fails
    its sitemap fetch and is reported as any other dead source; there is
    deliberately no headless-browser path here.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Callable
from typing import Any

from src.net import http
from src.net.http import HEADERS, fetch_failed
from src.net.util import host_of, norm_posted_date, strip_html, text_from_html
from src.rows import FetchedJob


def board_host(board_url: str | None) -> str:
    """The board's hostname, lowercased — the ``getro:<host>`` provenance
    tag and the name the crawl reports the source under.

    >>> board_host("https://jobs.example-partners.org/")
    'jobs.example-partners.org'
    >>> board_host("careers.example.org/jobs?x=1")
    'careers.example.org'
    >>> board_host(""), board_host(None)
    ('', '')
    """
    value = (board_url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return host_of(value)


def board_origin(board_url: str | None) -> str:
    """``https://<host>`` for the board, whichever page of it was given.

    >>> board_origin("https://jobs.example.org/companies/acme/jobs/1-x")
    'https://jobs.example.org'
    >>> board_origin("")
    ''
    """
    host = board_host(board_url)
    return f"https://{host}" if host else ""


def title_from_slug(slug: str | None) -> str:
    """The words of a job URL's title slug, as the relevance filter reads
    them. The slug is the only title the sitemap carries.

    >>> title_from_slug("senior-software-engineer-data-platform")
    'senior software engineer data platform'
    >>> title_from_slug("quality-control-inspector-2-30pm-11-00pm")
    'quality control inspector 2 30pm 11 00pm'
    >>> title_from_slug(""), title_from_slug(None)
    ('', '')
    """
    return re.sub(r"[-_]+", " ", slug or "").strip()


def parse_sitemap(xml: str | None, origin: str = "") -> tuple[list[dict[str, str]], list[str]]:
    """(job entries, child sitemap URLs) from one sitemap document.

    Job entries are dicts ``{url, id, org_slug, title_guess, lastmod}``;
    every other URL on the board (company pages, the home page) is
    ignored. A sitemap INDEX yields no entries and the child URLs instead.

    >>> jobs, kids = parse_sitemap('''<urlset>
    ...   <url><loc>https://b.org/companies/acme</loc></url>
    ...   <url><loc>https://b.org/companies/acme/jobs/91-data-engineer</loc>
    ...        <lastmod>2026-08-30T20:02:36Z</lastmod></url>
    ... </urlset>''')
    >>> kids, [(j["id"], j["org_slug"], j["title_guess"], j["lastmod"]) for j in jobs]
    ([], [('91', 'acme', 'data engineer', '2026-08-30T20:02:36Z')])
    >>> parse_sitemap('<sitemapindex><sitemap><loc>https://b.org/s1.xml</loc>'
    ...               '</sitemap></sitemapindex>')
    ([], ['https://b.org/s1.xml'])
    """
    loc = r"<loc>\s*([^<]+?)\s*</loc>"
    children = []
    for block in re.findall(r"<sitemap>(.*?)</sitemap>", xml or "", re.S):
        m = re.search(loc, block)
        if m:
            children.append(m.group(1))
    jobs = []
    for block in re.findall(r"<url>(.*?)</url>", xml or "", re.S):
        m = re.search(loc, block)
        if not m:
            continue
        url = m.group(1)
        pm = re.search(r"/companies/([^/?#]+)/jobs/(\d+)(?:-([^/?#]*))?", url)
        if not pm:
            continue
        lm = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", block)
        jobs.append({
            "url":         url,
            "id":          pm.group(2),
            "org_slug":    pm.group(1),
            "title_guess": title_from_slug(pm.group(3)),
            "lastmod":     lm.group(1) if lm else "",
        })
    return jobs, children


def _current_job(page_html: str | None) -> dict[str, Any] | None:
    """The ``currentJob`` record embedded in a job page, or None."""
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        job = (data["props"]["pageProps"]["initialState"]["jobs"]
               ["currentJob"])
    except (ValueError, KeyError, TypeError):
        return None
    return job if isinstance(job, dict) else None


def parse_job_page(page_html: str | None, board_url: str,
                   page_url: str = "") -> FetchedJob | None:
    """One server-rendered job page as a crawler job dict, or None.

    The job's ``url`` is the employer's OWN posting (the apply link), not
    the board page: that is the link a reader wants, and it is what lets
    the store recognise the same posting when the employer's board is
    crawled directly. The board page is kept under ``_employer``.

    Covered by ``tests/test_fetcher_parsers.py::TestGetro``.
    """
    job = _current_job(page_html)
    if not job:
        return None
    jid = job.get("id")
    # strip_html for the one-line fields, text_from_html for the body: the
    # embedded record carries HTML in both, and a title is not a place for
    # the paragraph breaks a JD needs (src/net/util.py owns both).
    title = strip_html(job.get("title"))
    if not jid or not title:
        return None
    if (job.get("status") not in (None, "active")
            or job.get("closedAt") or job.get("deactivatedAt")):
        return None
    org = job.get("organization")
    org = org if isinstance(org, dict) else {}
    locations = list(dict.fromkeys(
        n for n in (strip_html(loc.get("name") if isinstance(loc, dict) else loc)
                    for loc in job.get("locations") or []) if n))
    host = board_host(board_url)
    return {
        "id":          f"getro_{jid}",
        "company":     strip_html(org.get("name")) or host,
        "title":       title,
        "url":         job.get("url") or page_url,
        "location":    "; ".join(locations),
        "description": text_from_html(job.get("description")),
        "posted_at":   norm_posted_date(job.get("postedAt")),
        # What attribute_employers needs to find (or queue) the employer.
        "_employer": {
            "name":     strip_html(org.get("name")),
            "domain":   (org.get("domain") or "").strip().lower(),
            "slug":     org.get("slug") or "",
            "board":    host,
            "page_url": page_url,
        },
    }


async def _fetch_sitemap(origin: str, label: str) -> list[dict[str, str]]:
    """Every job entry the board's sitemap (or sitemap index) lists, each
    sitemap read once and parsed off the loop. An index is followed one
    level, eight children at most."""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    queue = deque([f"{origin}/sitemap.xml"])
    queued = set(queue)
    fetched = 0
    while queue and fetched <= 8:
        url = queue.popleft()
        fetched += 1
        try:
            r = await http.send("GET", url,
                                headers={**HEADERS, "Accept": "application/xml"})
            r.raise_for_status()
        except Exception as e:
            fetch_failed(f"Getro {label} sitemap", e)
            continue
        jobs, children = await asyncio.to_thread(lambda: parse_sitemap(r.text, origin))
        for j in jobs:
            if j["id"] not in seen:
                seen.add(j["id"])
                entries.append(j)
        fresh = [c for c in dict.fromkeys(children) if c not in queued]
        queued.update(fresh)
        queue.extend(fresh)
    return entries


async def fetch_getro_all(board_url: str, max_details: int = 150, detail_delay: float = 0.3,
                          gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    """Relevant postings from one Getro board, as crawler job dicts.

    Sitemap first; then, newest first, one page fetch per posting whose
    slug title passes `gate`, up to `max_details` (the rest wait for the
    next crawl: newest first, the cap trims the stalest), `detail_delay`
    seconds apart, each page read off the loop. The final relevance
    decision is `gate(title, description)` on the page's full text;
    `gate=None` keeps every posting. Returns [] — never raises — when
    the board is unreachable.

    See tests/test_fetcher_parsers.py::TestGetro.
    """
    origin = board_origin(board_url)
    if not origin:
        return []
    label = board_host(board_url)
    entries = await _fetch_sitemap(origin, label)
    if not entries:
        return []
    entries.sort(key=lambda e: e["lastmod"], reverse=True)

    jobs: list[FetchedJob] = []
    fetched = 0
    for e in entries:
        if gate is not None and not gate(e["title_guess"]):
            continue
        if fetched >= max_details:
            print(f"    [i] Getro {label}: detail cap ({max_details}) reached; "
                  f"older postings wait for the next crawl")
            break
        try:
            r = await http.send("GET", e["url"], headers=HEADERS)
            r.raise_for_status()
        except Exception as ex:
            fetch_failed(f"Getro {label} {e['url']}", ex)
            fetched += 1
            continue
        fetched += 1
        job = await asyncio.to_thread(lambda: parse_job_page(r.text, board_url, e["url"]))
        if job and not job["posted_at"]:
            job["posted_at"] = norm_posted_date(e["lastmod"])
        if job and (gate is None or gate(job["title"], job["description"])):
            jobs.append(job)
        if detail_delay:
            await asyncio.sleep(detail_delay)
    return jobs
