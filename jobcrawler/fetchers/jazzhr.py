"""JazzHR public job-board fetcher.

JazzHR-hosted boards live at ``https://<subdomain>.applytojob.com/``. The
index page lists every open role as ``/apply/<id>/<slug>`` links, and each
of those detail pages embeds a schema.org JobPosting in JSON-LD (title,
location, full description) — exactly what ``fetchers.jsonld`` already
parses. So this fetcher just discovers the per-job URLs and delegates
parsing/relevance to ``fetch_jsonld_page``.

Used for Paradromics (subdomain ``paradromicsinc``), whose careers site is
"""

import re
import time

import requests

from ..http import HEADERS
from .jsonld import fetch_jsonld_page

_APPLY_RE = re.compile(r"/apply/[A-Za-z0-9]+/[A-Za-z0-9_-]+")


def fetch_jazzhr(company_name, subdomain, max_jobs=60, per_job_delay=0.3):
    base = f"https://{subdomain}.applytojob.com"
    try:
        r = requests.get(base + "/", timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] JazzHR {company_name}: {e}")
        return []

    seen, urls = set(), []
    for path in _APPLY_RE.findall(r.text):
        url = base + path
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= max_jobs:
            break

    jobs = []
    for url in urls:
        jobs.extend(fetch_jsonld_page(company_name, url))
        time.sleep(per_job_delay)
    return jobs
