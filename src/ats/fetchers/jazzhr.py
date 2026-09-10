"""JazzHR public job-board fetcher.

JazzHR-hosted boards live at ``https://<subdomain>.applytojob.com/``. The
index page lists every open role as ``/apply/<id>/<slug>`` links, and each
of those detail pages embeds a schema.org JobPosting in JSON-LD (title,
location, full description) — exactly what ``fetchers.jsonld`` already
parses. So this fetcher discovers the per-job URLs, reads each page into a
row (there is no cheaper listing to screen first) and hands the rows to
``board.board_jobs`` for the location and relevance filters.

Ids are ``jsonld_<subdomain>_<posting id>``: keyed on the board, not on
the display name, so the sweep and the company-vetted pull agree.
"""

import re
import time

from src.net.http import SESSION, HEADERS
from .board import board_jobs
from .jsonld import _job_from_posting, extract_jsonld, is_jobposting

_APPLY_RE = re.compile(r"/apply/[A-Za-z0-9]+/[A-Za-z0-9_-]+")


def _rows(subdomain, urls, label, per_job_delay):
    """One row per JobPosting found on each posting page."""
    for url in urls:
        try:
            r = SESSION.get(url, headers=HEADERS)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] JazzHR {label} {url}: {e}")
            continue
        for obj in extract_jsonld(r.text):
            if is_jobposting(obj):
                yield _job_from_posting(obj, subdomain, url)
        time.sleep(per_job_delay)


def fetch_jazzhr(company_name, subdomain, gate=None, loc_re=None, max_jobs=60,
                 per_job_delay=0.3):
    base = f"https://{subdomain}.applytojob.com"
    label = company_name or subdomain
    try:
        r = SESSION.get(base + "/", headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] JazzHR {label}: {e}")
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
    return board_jobs(_rows(subdomain, urls, label, per_job_delay), company_name,
                      gate=gate, loc_re=loc_re)
