"""
Remotive public job feed.

Remotive exposes a JSON endpoint at
https://remotive.com/api/remote-jobs that returns every active listing
in a single payload. No API key, no pagination, permissive CORS.

Optional `category` parameter narrows by slug
(e.g. "software-dev", "data"). We don't use it by default: our own
is_relevant() filter is narrower than any single Remotive category.

Schema (per job under data['jobs']):
    id, url, title, company_name, category, tags, job_type,
    publication_date, candidate_required_location, salary, description
"""

import html
import re

import requests

from ..filters import is_relevant
from ..http import HEADERS

API_URL = "https://remotive.com/api/remote-jobs"


def _strip_html(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def fetch_remotive(category=None, max_jobs=None):
    """
    Pull Remotive's job feed; return relevant listings.

    `category`: optional slug, e.g. "software-dev". None = all categories.
    `max_jobs`: cap iteration (debugging). None = all.
    """
    url = API_URL
    if category:
        url = f"{API_URL}?category={category}"

    try:
        r = requests.get(url, timeout=25, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [!] Remotive: {e}")
        return []

    entries = data.get("jobs", []) or []
    if max_jobs is not None:
        entries = entries[:max_jobs]

    jobs = []
    for entry in entries:
        jid      = entry.get("id")
        title    = entry.get("title") or ""
        company  = entry.get("company_name") or "Remotive"
        jurl     = entry.get("url") or ""
        location = entry.get("candidate_required_location") or "Remote"
        desc     = _strip_html(entry.get("description", ""))[:600]

        tags     = entry.get("tags") or []
        tag_text = " ".join(str(t) for t in tags if t)
        cat      = entry.get("category") or ""

        if not is_relevant(title, desc + " " + tag_text + " " + cat):
            continue

        jobs.append({
            "id":          f"remotive_{jid}",
            "company":     company,
            "title":       title,
            "url":         jurl,
            "location":    location,
            "description": desc,
        })
    return jobs
