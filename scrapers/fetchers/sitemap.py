"""
Sitemap-driven job crawler.

Reads a company's sitemap.xml (including nested sitemap indices), finds
URLs that look like job postings, fetches each, and extracts JSON-LD
JobPosting data.

This turns any company with a public sitemap into a supported source —
no per-company scraper required.
"""

import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..http import HEADERS
from .jsonld import fetch_jsonld_page


_JOB_URL_HINTS = re.compile(
    r"/(jobs?|careers?|positions?|openings?|vacancies|listings?)/", re.I
)


def _fetch_sitemap_urls(url, depth=0, max_depth=2):
    """Recurse through sitemap indices. Returns a flat list of <loc> URLs."""
    if depth > max_depth:
        return []
    try:
        r = requests.get(url, timeout=25,
                         headers={**HEADERS, "Accept": "application/xml"})
        r.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "xml")
    nested = soup.find_all("sitemap")
    if nested:
        out = []
        for sm in nested:
            loc = sm.find("loc")
            if loc:
                out.extend(_fetch_sitemap_urls(
                    loc.get_text(strip=True), depth + 1, max_depth))
        return out
    return [u.get_text(strip=True) for u in soup.find_all("loc")]


def fetch_sitemap(company_name, sitemap_url, url_filter=None,
                  max_job_urls=100, per_job_delay=0.3):
    """
    Pull jobs from a company's sitemap.

    url_filter: optional — a regex string or compiled pattern. Applied to
        the URL PATH. Defaults to common job path segments.
    """
    urls = _fetch_sitemap_urls(sitemap_url)
    if not urls:
        print(f"    [!] Sitemap {company_name}: no URLs at {sitemap_url}")
        return []

    if isinstance(url_filter, str):
        pattern = re.compile(url_filter, re.I)
    else:
        pattern = url_filter or _JOB_URL_HINTS

    job_urls = [u for u in urls if pattern.search(urlparse(u).path)]
    if not job_urls:
        print(f"    [!] Sitemap {company_name}: no job-like URLs "
              f"(of {len(urls)} total).")
        return []

    print(f"      -> {len(job_urls)} job URL(s); fetching up to {max_job_urls}")
    jobs = []
    for url in job_urls[:max_job_urls]:
        jobs.extend(fetch_jsonld_page(company_name, url))
        time.sleep(per_job_delay)
    return jobs
