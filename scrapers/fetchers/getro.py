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

Employer attribution (``attribute_employers``) runs on the caller's thread
once the crawl has gated the jobs: the employer named on the board is
matched to the roster by its own ATS coordinates (the apply link usually
points at its Greenhouse/Lever/... board) or by name, and an employer the
roster has never seen becomes a REVIEW CANDIDATE — never an active row.

Notes:
    A board that fronts itself with a browser challenge (Cloudflare's
    "Just a moment..." answers even robots.txt with a 403) simply fails
    its sitemap fetch and is reported as any other dead source; there is
    deliberately no headless-browser path here.
"""

import json
import re
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from config import FETCH_TIMEOUT
from core.filters import is_relevant
from ..http import SESSION, HEADERS
from ..util import norm_posted_date

# Job pages beyond this many are left for the next crawl. Newest first, so
# the cap trims the stalest postings, not the freshest.
DEFAULT_MAX_DETAILS = 150
DETAIL_DELAY = 0.3
# A sitemap index is followed one level; this bounds how many children.
MAX_CHILD_SITEMAPS = 8

_JOB_PATH_RE = re.compile(r"/companies/([^/?#]+)/jobs/(\d+)(?:-([^/?#]*))?")
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>")
_SITEMAP_BLOCK_RE = re.compile(r"<sitemap>(.*?)</sitemap>", re.S)


def board_host(board_url):
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
    return (urlparse(value).hostname or "").lower()


def board_origin(board_url):
    """``https://<host>`` for the board, whichever page of it was given.

    >>> board_origin("https://jobs.example.org/companies/acme/jobs/1-x")
    'https://jobs.example.org'
    >>> board_origin("")
    ''
    """
    host = board_host(board_url)
    return f"https://{host}" if host else ""


def title_from_slug(slug):
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


def parse_sitemap(xml, origin=""):
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
    children = []
    for block in _SITEMAP_BLOCK_RE.findall(xml or ""):
        m = _LOC_RE.search(block)
        if m:
            children.append(m.group(1))
    jobs = []
    for block in _URL_BLOCK_RE.findall(xml or ""):
        m = _LOC_RE.search(block)
        if not m:
            continue
        url = m.group(1)
        pm = _JOB_PATH_RE.search(url)
        if not pm:
            continue
        lm = _LASTMOD_RE.search(block)
        jobs.append({
            "url":         url,
            "id":          pm.group(2),
            "org_slug":    pm.group(1),
            "title_guess": title_from_slug(pm.group(3)),
            "lastmod":     lm.group(1) if lm else "",
        })
    return jobs, children


def _text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def _current_job(page_html):
    """The ``currentJob`` record embedded in a job page, or None."""
    m = _NEXT_DATA_RE.search(page_html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        job = (data["props"]["pageProps"]["initialState"]["jobs"]
               ["currentJob"])
    except (ValueError, KeyError, TypeError):
        return None
    return job if isinstance(job, dict) else None


def parse_job_page(page_html, board_url, page_url=""):
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
    title = _text(job.get("title"))
    if not jid or not title:
        return None
    if (job.get("status") not in (None, "active")
            or job.get("closedAt") or job.get("deactivatedAt")):
        return None
    org = job.get("organization") if isinstance(job.get("organization"), dict) else {}
    locations = []
    for loc in job.get("locations") or []:
        name = _text(loc.get("name") if isinstance(loc, dict) else loc)
        if name and name not in locations:
            locations.append(name)
    host = board_host(board_url)
    return {
        "id":          f"getro_{jid}",
        "company":     _text(org.get("name")) or host,
        "title":       title,
        "url":         job.get("url") or page_url,
        "location":    "; ".join(locations),
        "description": _text(job.get("description")),
        "posted_at":   norm_posted_date(job.get("postedAt")),
        "via":         f"getro:{host}",
        # What attribute_employers needs to find (or queue) the employer.
        "_employer": {
            "name":     _text(org.get("name")),
            "domain":   (org.get("domain") or "").strip().lower(),
            "slug":     org.get("slug") or "",
            "board":    host,
            "page_url": page_url,
        },
    }


def _fetch_sitemap(origin, label):
    """Every job entry the board's sitemap (or sitemap index) lists."""
    entries, seen = [], set()
    queue = [f"{origin}/sitemap.xml"]
    fetched = 0
    while queue and fetched <= MAX_CHILD_SITEMAPS:
        url = queue.pop(0)
        fetched += 1
        try:
            r = SESSION.get(url, timeout=FETCH_TIMEOUT,
                            headers={**HEADERS, "Accept": "application/xml"})
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] Getro {label} sitemap: {e}")
            continue
        jobs, children = parse_sitemap(r.text, origin)
        for j in jobs:
            if j["id"] not in seen:
                seen.add(j["id"])
                entries.append(j)
        queue.extend(c for c in children if c not in queue)
    return entries


def fetch_getro_all(board_url, max_details=DEFAULT_MAX_DETAILS,
                    detail_delay=DETAIL_DELAY):
    """Relevant postings from one Getro board, as crawler job dicts.

    Sitemap first; then, newest first, one page fetch per posting whose
    slug title passes the relevance filter, up to `max_details`. The final
    relevance decision uses the page's full description. Returns [] —
    never raises — when the board is unreachable.

    See tests/test_fetcher_parsers.py::TestGetro.
    """
    origin = board_origin(board_url)
    if not origin:
        return []
    label = board_host(board_url)
    entries = _fetch_sitemap(origin, label)
    if not entries:
        return []
    entries.sort(key=lambda e: e["lastmod"], reverse=True)

    jobs, fetched = [], 0
    for e in entries:
        if not is_relevant(e["title_guess"]):
            continue
        if fetched >= max_details:
            print(f"    [i] Getro {label}: detail cap ({max_details}) reached; "
                  f"older postings wait for the next crawl")
            break
        try:
            r = SESSION.get(e["url"], timeout=FETCH_TIMEOUT, headers=HEADERS)
            r.raise_for_status()
        except Exception as ex:
            print(f"    [!] Getro {label} {e['url']}: {ex}")
            fetched += 1
            continue
        fetched += 1
        job = parse_job_page(r.text, board_url, e["url"])
        if job and not job["posted_at"]:
            job["posted_at"] = norm_posted_date(e["lastmod"])
        if job and is_relevant(job["title"], job["description"]):
            jobs.append(job)
        if detail_delay:
            time.sleep(detail_delay)
    return jobs


# --------------------------------------------------------------------------- #
#  Employer attribution                                                       #
# --------------------------------------------------------------------------- #

def _coords_from_urls(urls):
    """Roster-shaped board coordinates for the employer, read off its
    apply links, or None when none of them names a known ATS."""
    from discovery.sniffer import _detect, _pack
    for url in urls:
        hit = _detect("", url or "")
        if not hit or hit[0] not in ("fetchable", "semi"):
            continue
        packed = _pack(hit[1], hit[2], url)
        row = {"ats": packed["ats"], "careers_url": packed.get("careers_url")}
        if packed["ats"] == "workday":
            t, pod, site = packed["triple"]
            row.update({"wd_tenant": t, "wd_pod": pod, "wd_site": site})
        else:
            row["slug"] = packed.get("slug")
        return row
    return None


def attribute_employers(conn, jobs, commit=True):
    """Link each board-sourced job to its employer's roster row, queueing
    employers the roster lacks for review. Returns the jobs to keep.

    Jobs without an ``_employer`` record pass through untouched. For the
    rest, per employer:

    * a roster row that owns the same board (the apply link's ATS
      coordinates — ``core.store.company_by_board``) or the same name gets
      ``company_id`` stamped on the jobs. When that row is ACTIVE and
      confirmed, its own crawl covers the board, so a posting the store
      already holds under the employer's URL is dropped here rather than
      stored twice;
    * a name the reviewer rejected (``core.store.block_name``) drops its
      jobs — that decision was "not a company", and it sticks;
    * anything else becomes a review candidate under `commit`:
      ``core.store.mark_pending`` (inactive, tagged pending-review), with
      ``source = "getro:<board host>"`` and the ATS coordinates when the
      apply link revealed them. Never an active row.

    See tests/test_fetcher_parsers.py::TestGetroAttribution.
    """
    from core import store, tags
    from scrapers.sources import seed_tag_for

    groups = {}
    for j in jobs:
        emp = j.get("_employer")
        if isinstance(emp, dict) and (emp.get("name") or emp.get("slug")):
            groups.setdefault(emp.get("slug") or emp["name"].lower(),
                              []).append(j)
    if not groups:
        return list(jobs)

    blocked = store.blocked_name_keys(conn)
    drop = set()
    for key, group in groups.items():
        emp = group[0]["_employer"]
        name = emp.get("name") or key
        coords = _coords_from_urls([j.get("url") for j in group])
        row = store.company_by_board(conn, coords) if coords else None
        if row is None:
            cid = store.company_id_by_name(conn, name)
            row = store.get_company(conn, cid) if cid else None
        if row is not None:
            crawled = bool(row.get("active")) and not tags.has(
                row.get("tags"), tags.PENDING)
            for j in group:
                j["company_id"] = row["id"]
                if crawled and j.get("url") and conn.execute(
                        "SELECT 1 FROM jobs WHERE url=? LIMIT 1",
                        (j["url"],)).fetchone():
                    drop.add(id(j))
            continue
        if store._name_key(name) in blocked:
            drop.update(id(j) for j in group)
            continue
        if not commit:
            continue
        via = f"getro:{emp.get('board') or ''}"
        careers_url = ((coords or {}).get("careers_url")
                       or (f"https://{emp['domain']}" if emp.get("domain")
                           else None))
        candidate = {"name": name, "careers_url": careers_url,
                     "source": via,
                     "notes": f"employer on the {emp.get('board')} board; "
                              f"{len(group)} relevant posting(s)"}
        if coords:
            candidate.update(coords)
            candidate["careers_url"] = careers_url
            candidate["tags"] = seed_tag_for(coords["ats"])
        cid = store.upsert_company(conn, store.mark_pending(candidate))
        for j in group:
            j["company_id"] = cid
    return [j for j in jobs if id(j) not in drop]
