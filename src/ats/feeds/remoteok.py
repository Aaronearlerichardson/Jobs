"""
RemoteOK public job feed.

RemoteOK exposes a single JSON endpoint (https://remoteok.com/api) with
every active listing on the site. No API key, no pagination. First
element is a legal/metadata stub (has no 'id') and must be skipped.

Schema (per job):
    id, slug, epoch, date, company, company_logo, position, tags,
    description, location, salary, apply_url, url, original
"""

import asyncio

from src.net import http
from src.net.util import strip_html


async def afetch_remoteok(max_jobs=500, gate=None):
    """
    Pull every active listing from RemoteOK, filter to the relevant ones
    (off the loop). Returns a list of job dicts in the standard crawler
    shape.
    """
    data = await http.aget_json("https://remoteok.com/api", "RemoteOK", default=[])
    return await asyncio.to_thread(_jobs, data, max_jobs, gate)


fetch_remoteok = http.sync_shim(afetch_remoteok)


def _jobs(data, max_jobs, gate):
    """The feed's payload `data` as fetch_remoteok's job dicts."""
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
        desc     = strip_html(entry.get("description", ""))

        # tags can enrich relevance matching (e.g. "ml", "python")
        tags = entry.get("tags") or []
        tag_text = " ".join(str(t) for t in tags if t)

        if gate is not None and not gate(title, desc + " " + tag_text):
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
