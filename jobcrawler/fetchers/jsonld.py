"""
Generic JSON-LD JobPosting fetcher.

Most modern career pages embed schema.org JobPosting data in a
<script type="application/ld+json"> tag — this is the exact format that
Google for Jobs and other aggregators consume. One parser covers
hundreds of sites with zero per-vendor code.

Use it two ways:

  fetch_jsonld_page(company, url)
      - Parse JSON-LD from a single URL. Returns any JobPosting records
        found on that page. Perfect for individual job-listing pages
        surfaced by websearch / sitemap.

  fetch_jsonld_careers(company, careers_url)
      - Parse JSON-LD from a careers index page. If none found, follow
        job-like links from that page and parse JSON-LD on each.
"""

import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS
from ..util import stable_id


_JOB_URL_HINTS = re.compile(
    r"/(jobs?|careers?|positions?|openings?|vacancies|listings?)/", re.I
)


def extract_jsonld(html):
    """Find every <script type=application/ld+json> block; return parsed objects."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        txt = script.string or script.get_text()
        if not txt:
            continue
        txt = txt.strip().lstrip("\ufeff")
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", txt))
            except Exception:
                continue
        if isinstance(data, list):
            out.extend(data)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                out.extend(data["@graph"])
            else:
                out.append(data)
    return out


def is_jobposting(obj):
    if not isinstance(obj, dict):
        return False
    t = obj.get("@type")
    if isinstance(t, list):
        return any("JobPosting" in str(x) for x in t)
    return "JobPosting" in str(t or "")


def _normalize_location(jp):
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address", {})
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"),
                     addr.get("addressRegion"),
                     addr.get("addressCountry")]
            joined = ", ".join(str(p) for p in parts if p)
            if joined:
                return joined
        if loc.get("name"):
            return str(loc["name"])
    alr = jp.get("applicantLocationRequirements")
    if isinstance(alr, dict) and alr.get("name"):
        return str(alr["name"])
    if jp.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return "Unknown"


def _normalize_description(jp):
    desc = jp.get("description", "") or ""
    return BeautifulSoup(desc, "html.parser").get_text(" ")


def _job_from_posting(jp, company_name, source_url):
    title = jp.get("title", "") or ""
    job_url = jp.get("url") or jp.get("mainEntityOfPage") or source_url
    if isinstance(job_url, dict):
        job_url = job_url.get("@id", source_url)
    location = _normalize_location(jp)
    description = _normalize_description(jp)
    identifier = jp.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value", "")
    jid = str(identifier or stable_id(str(job_url)))
    job = {
        "id":          f"jsonld_{company_name.replace(' ', '_')}_{jid}",
        "company":     company_name,
        "title":       title,
        "url":         str(job_url) if job_url else source_url,
        "location":    location,
        "description": description,
    }
    # Structured remote signal — schema.org marks remote roles explicitly.
    if str(jp.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        job["remote_hint"] = "jsonld:telecommute"
    return job


def fetch_jsonld_page(company_name, page_url, timeout=20):
    """Fetch ONE URL; extract JobPosting records from its JSON-LD."""
    try:
        r = requests.get(page_url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] JSON-LD {company_name} {page_url}: {e}")
        return []

    jobs = []
    for obj in extract_jsonld(r.text):
        if not is_jobposting(obj):
            continue
        job = _job_from_posting(obj, company_name, page_url)
        if is_relevant(job["title"], job["description"]):
            jobs.append(job)
    return jobs


def fetch_jsonld_careers(company_name, careers_url, max_job_urls=50):
    """
    Try the careers index page for JSON-LD. If it has none, follow up to
    `max_job_urls` job-like links from that page and parse each.
    """
    try:
        r = requests.get(careers_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] JSON-LD {company_name}: {e}")
        return []

    jobs = []
    for obj in extract_jsonld(r.text):
        if is_jobposting(obj):
            job = _job_from_posting(obj, company_name, careers_url)
            if is_relevant(job["title"], job["description"]):
                jobs.append(job)
    if jobs:
        return jobs

    # Follow job-like links
    soup = BeautifulSoup(r.text, "html.parser")
    urls, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(careers_url, href)
        if not _JOB_URL_HINTS.search(urlparse(href).path):
            continue
        if href in seen:
            continue
        seen.add(href)
        urls.append(href)
        if len(urls) >= max_job_urls:
            break

    for url in urls:
        jobs.extend(fetch_jsonld_page(company_name, url))
        time.sleep(0.3)
    return jobs
