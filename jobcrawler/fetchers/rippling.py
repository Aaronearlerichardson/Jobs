"""Rippling ATS public board fetcher.

Rippling-hosted boards live at ``ats.rippling.com/<slug>/jobs`` (a client-
rendered SPA), but the data comes from a clean public JSON API:

    GET  api.rippling.com/platform/api/ats/v1/board/<slug>/jobs         -> listing
    GET  api.rippling.com/platform/api/ats/v1/board/<slug>/jobs/<uuid>  -> one job

The listing carries uuid / name / department / workLocation; the per-job
endpoint adds the full description (an HTML ``{company, role}`` dict) and a
``companyName`` for attribution. The store slug is the board slug (e.g.
``blackrockneurotech``).

Replaces the old ``custom`` treatment of Rippling boards, whose static HTML
scrape returned nothing because the board is client-rendered.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS

_API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
_JSON = {**HEADERS, "Accept": "application/json"}


def parse_board(slug, timeout=20):
    """Return the raw listing (list of job dicts) for one board slug."""
    r = requests.get(_API.format(slug=slug), timeout=timeout, headers=_JSON)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("jobs") or [])


def location_str(job):
    wl = job.get("workLocation")
    if isinstance(wl, dict) and wl.get("label"):
        return wl["label"]
    wls = job.get("workLocations")
    if isinstance(wls, list) and wls:
        return ", ".join(str(x) for x in wls[:3])
    return "Unknown"


def fetch_description(slug, uuid, timeout=15):
    """Full JD text for one posting. Rippling's description is a
    ``{company, role}`` HTML dict — 'role' is the actual JD (put first);
    'company' is the shared boilerplate."""
    try:
        r = requests.get(f"{_API.format(slug=slug)}/{uuid}", timeout=timeout, headers=_JSON)
        r.raise_for_status()
        d = r.json().get("description")
    except Exception:
        return ""
    if isinstance(d, dict):
        parts = [d.get("role"), d.get("company")]
    else:
        parts = [d]
    html = " ".join(p for p in parts if isinstance(p, str) and p)
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _dept(job):
    d = job.get("department") or {}
    return d.get("label", "") if isinstance(d, dict) else str(d)


def fetch_rippling(slug, company_name, max_details=40, detail_delay=0.2):
    """Keyword-gated fetch (for sweeping unvetted boards): title/department
    screen first, hydrate the description only when the cheap fields didn't
    already decide relevance."""
    try:
        raw = parse_board(slug)
    except Exception as e:
        print(f"    [!] Rippling {company_name}: {e}")
        return []
    out, fetched = [], 0
    for j in raw:
        uuid = j.get("uuid") or ""
        title = (j.get("name") or "").strip()
        if not uuid or not title:
            continue
        head = f"{title} {_dept(j)}"
        desc = ""
        if not is_relevant(head) and fetched < max_details:
            desc = fetch_description(slug, uuid)
            fetched += 1
            time.sleep(detail_delay)
        if not is_relevant(head, desc):
            continue
        if not desc and fetched < max_details:
            desc = fetch_description(slug, uuid)
            fetched += 1
            time.sleep(detail_delay)
        out.append({"id": f"rippling_{slug}_{uuid[:12]}", "company": company_name,
                    "title": title,
                    "url": j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid}",
                    "location": location_str(j), "description": desc})
    return out
