"""Jobvite career-site fetcher.

A Jobvite tenant's public site lives at ``https://jobs.jobvite.com/<tenant>``.
Its listing is server-rendered HTML — no JSON feed on this host — in two
places that share one row markup (``li.job`` with ``a.jv-job-list-name``
and ``p.jv-job-list-location``):

    /<tenant>/search?p=N   fifty rows a page, ``p`` zero-based, until a
                           page comes back empty
    /<tenant>/jobs         every row on one page; some tenants replace it
                           with a custom landing page that lists nothing,
                           which is why ``search`` is asked first

A job page ``/<tenant>/job/<id>`` embeds a schema.org JobPosting in JSON-LD
(title, description, location, datePosted), which ``fetchers.jsonld``
already knows how to read. Titles and locations come from the listing;
the page is fetched only for the description, screened the way the
BambooHR fetcher screens — cheap fields first, the page within a budget.

``jobs.jobvite.com`` publishes no robots.txt (a 404), which RFC 9309 reads
as "no restrictions". Ids are ``jv_<tenant>_<id>``.
"""

import re
import time

from bs4 import BeautifulSoup

from core.filters import is_relevant
from ..http import SESSION, HEADERS
from ..util import norm_posted_date
from .jsonld import (_normalize_description, _normalize_location,
                     extract_jsonld, is_jobposting)

BASE = "https://jobs.jobvite.com"
MAX_PAGES = 20

_TENANT_RE = re.compile(r"jobs\.jobvite\.com/([a-z0-9][a-z0-9_-]*)", re.I)
_JOB_HREF_RE = re.compile(r"/([a-z0-9][a-z0-9_-]*)/job/([A-Za-z0-9]+)")


def tenant_of(value):
    """The tenant slug in `value` — a bare slug, or any URL on the site.

    >>> tenant_of("acme")
    'acme'
    >>> tenant_of("https://jobs.jobvite.com/acme/job/oAbCdEf1")
    'acme'
    >>> tenant_of("https://jobs.jobvite.com/Acme-Labs/search?p=1")
    'acme-labs'
    >>> tenant_of(""), tenant_of(None)
    ('', '')
    """
    value = (value or "").strip()
    m = _TENANT_RE.search(value)
    if m:
        return m.group(1).lower()
    return "" if "/" in value or "." in value else value.lower()


def parse_listing(html, tenant):
    """The listing rows on one page, as job dicts without descriptions.

    >>> rows = parse_listing('''<ul class="jv-job-list">
    ...   <li class="job"><a class="jv-job-list-name" href="/acme/job/oAb1">
    ...       Data Engineer</a><p class="jv-job-list-location"> Durham, NC </p></li>
    ...   <li class="job"><a class="jv-job-list-name" href="/other/x">skip</a></li>
    ... </ul>''', "acme")
    >>> [(r["id"], r["title"], r["location"], r["url"]) for r in rows]
    [('jv_acme_oAb1', 'Data Engineer', 'Durham, NC', 'https://jobs.jobvite.com/acme/job/oAb1')]
    """
    soup = BeautifulSoup(html or "", "html.parser")
    rows, seen = [], set()
    for a in soup.select("a.jv-job-list-name[href]"):
        m = _JOB_HREF_RE.search(a["href"])
        if not m or m.group(1).lower() != tenant.lower():
            continue
        jid = m.group(2)
        title = a.get_text(" ", strip=True)
        if not title or jid in seen:
            continue
        seen.add(jid)
        li = a.find_parent("li")
        loc_el = li.select_one(".jv-job-list-location") if li else None
        rows.append({
            "id":          f"jv_{tenant}_{jid}",
            "company":     "",
            "title":       title,
            "url":         f"{BASE}/{tenant}/job/{jid}",
            "location":    loc_el.get_text(" ", strip=True) if loc_el else "",
            "description": "",
            "posted_at":   None,
        })
    return rows


def parse_detail(html):
    """{description, posted_at, location} from a job page's JSON-LD, or {}."""
    for obj in extract_jsonld(html or ""):
        if not is_jobposting(obj):
            continue
        out = {"description": re.sub(r"\s+", " ",
                                     _normalize_description(obj)).strip(),
               "posted_at": norm_posted_date(obj.get("datePosted"))}
        loc = _normalize_location(obj)
        if loc and loc != "Unknown":
            out["location"] = loc
        if str(obj.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            out["remote_hint"] = "jsonld:telecommute"
        return out
    return {}


def _listing(tenant, label):
    """Every row on the tenant's site: the paged search, else /jobs."""
    rows, seen = [], set()
    for page in range(MAX_PAGES):
        try:
            r = SESSION.get(f"{BASE}/{tenant}/search", params={"p": page},
                            timeout=20, headers=HEADERS)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] Jobvite {label} search p={page}: {e}")
            break
        new = [row for row in parse_listing(r.text, tenant)
               if row["id"] not in seen]
        if not new:
            break
        seen.update(row["id"] for row in new)
        rows.extend(new)
    if rows:
        return rows
    try:
        r = SESSION.get(f"{BASE}/{tenant}/jobs", timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Jobvite {label} jobs: {e}")
        return []
    return parse_listing(r.text, tenant)


def _hydrate(rows, label, max_details, detail_delay):
    """Fetch job pages for `rows`, in order, until the budget runs out."""
    n = 0
    for row in rows:
        if n >= max_details:
            break
        n += 1
        try:
            r = SESSION.get(row["url"], timeout=15, headers=HEADERS)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] Jobvite {label} {row['url']}: {e}")
            continue
        detail = parse_detail(r.text)
        row["description"] = detail.get("description", "")
        row["posted_at"] = detail.get("posted_at")
        if detail.get("location") and not row.get("location"):
            row["location"] = detail["location"]
        if detail.get("remote_hint"):
            row["remote_hint"] = detail["remote_hint"]
        if detail_delay:
            time.sleep(detail_delay)


def fetch_jobvite_board(tenant, want=None, max_details=40, detail_delay=0.2):
    """Every row on the site, ungated, with pages fetched for the rows
    `want(row)` accepts (all of them when None) within `max_details`.

    The company-vetted, location-scoped callers use this: they decide
    relevance themselves and only pay for the pages they will keep.
    """
    tenant = tenant_of(tenant)
    if not tenant:
        return []
    rows = _listing(tenant, tenant)
    _hydrate([r for r in rows if want is None or want(r)],
             tenant, max_details, detail_delay)
    return rows


def fetch_jobvite(tenant, company_name, max_details=40, detail_delay=0.2):
    """Relevant postings from one Jobvite tenant.

    Rows relevant on their title get their page first; the rest get one
    while the budget lasts, so a generic title can still qualify on its
    description. Returns [] — never raises — when the site is unreachable.

    See tests/test_fetcher_parsers.py::TestJobvite.
    """
    tenant = tenant_of(tenant)
    if not tenant:
        return []
    label = company_name or tenant
    rows = _listing(tenant, label)
    first = [r for r in rows if is_relevant(r["title"])]
    rest = [r for r in rows if not is_relevant(r["title"])]
    _hydrate(first + rest, label, max_details, detail_delay)
    jobs = []
    for row in rows:
        if not is_relevant(row["title"], row.get("description", "")):
            continue
        jobs.append({**row, "company": company_name})
    return jobs
