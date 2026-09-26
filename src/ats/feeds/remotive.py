"""
Remotive public job feed.

Remotive exposes a JSON endpoint at
https://remotive.com/api/remote-jobs that returns every active listing
in a single payload. No API key, no pagination, permissive CORS.

Optional `category` parameter narrows by slug
(e.g. "software-dev", "data"). We don't use it by default: the caller's
relevance gate is narrower than any single Remotive category.
"""

import asyncio

from src.net import http
from src.net.util import strip_html


async def afetch_remotive(category=None, max_jobs=None, gate=None):
    """
    Pull Remotive's job feed; return relevant listings, read off the loop.

    `category`: optional slug, e.g. "software-dev". None = all categories.
    `max_jobs`: cap iteration (debugging). None = all.
    """
    url = "https://remotive.com/api/remote-jobs"
    if category:
        url = f"{url}?category={category}"

    data = await http.aget_json(url, "Remotive", default={})
    return await asyncio.to_thread(_jobs, data, max_jobs, gate)


fetch_remotive = http.sync_shim(afetch_remotive)


def _jobs(data, max_jobs, gate):
    """The feed's payload `data` as fetch_remotive's job dicts."""
    entries = (data.get("jobs") or []) if isinstance(data, dict) else []
    if max_jobs is not None:
        entries = entries[:max_jobs]

    jobs = []
    for entry in entries:
        jid      = entry.get("id")
        title    = entry.get("title") or ""
        company  = entry.get("company_name") or "Remotive"
        jurl     = entry.get("url") or ""
        location = entry.get("candidate_required_location") or "Remote"
        desc     = strip_html(entry.get("description", ""))

        tags     = entry.get("tags") or []
        tag_text = " ".join(str(t) for t in tags if t)
        cat      = entry.get("category") or ""

        if gate is not None and not gate(title, desc + " " + tag_text + " " + cat):
            continue

        jobs.append({
            "id":          f"remotive_{jid}",
            "company":     company,
            "title":       title,
            "url":         jurl,
            "location":    location,
            "description": desc,
            # Remotive is a remote-only board; `location` is the candidate
            # region requirement (e.g. "USA Only", "Worldwide").
            "remote_hint": "board:remotive",
        })
    return jobs
