"""
RemoteOK public job feed.

RemoteOK exposes a single JSON endpoint (https://remoteok.com/api) with
every active listing on the site. No API key, no pagination. First
element is a legal/metadata stub (has no 'id') and must be skipped.

Schema (per job):
    id, slug, epoch, date, company, company_logo, position, tags,
    description, location, salary, apply_url, url, original
"""

import html
import re

import requests

from ..filters import is_relevant
from ..http import HEADERS

API_URL = "https://remoteok.com/api"


def _strip_html(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def fetch_remoteok(max_jobs=500):
    """
    Pull every active listing from RemoteOK, filter to the relevant ones.
    Returns a list of job dicts in the standard crawler shape.
    """
    try:
        r = requests.get(API_URL, timeout=25, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [!] RemoteOK: {e}")
        return []

    jobs = []
    for entry in data[:max_jobs + 1]:           # +1 for metadata stub
        if not isinstance(entry, dict):
            continue
        jid = entry.get("id")
        if not jid:                              # legal/metadata stub
            continue

        title    = entry.get("position") or ""
        company  = entry.get("company")  or "RemoteOK"
        url      = entry.get("url") or entry.get("apply_url") or ""
        location = entry.get("location") or "Remote"
        desc     = _strip_html(entry.get("description", ""))

        # tags can enrich relevance matching (e.g. "ml", "python")
        tags = entry.get("tags") or []
        tag_text = " ".join(str(t) for t in tags if t)

        if not is_relevant(title, desc + " " + tag_text):
            continue

        jobs.append({
            "id":          f"remoteok_{jid}",
            "company":     company,
            "title":       title,
            "url":         url,
            "location":    location,
            "description": desc,
            # RemoteOK is a remote-only board; `location` is the candidate
            # region requirement, not an office.
            "remote_hint": "board:remoteok",
        })
    return jobs
