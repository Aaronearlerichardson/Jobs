"""
RemoteOK public job feed.

RemoteOK exposes a single JSON endpoint (https://remoteok.com/api) with
every active listing on the site. No API key, no pagination. First
element is a legal/metadata stub (has no 'id') and must be skipped.

Schema (per job):
    id, slug, epoch, date, company, company_logo, position, tags,
    description, location, salary, apply_url, url, original
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from src.net import http
from src.net.util import JSON, strip_html
from src.rows import FetchedJob


class _Entry(BaseModel):
    """One listing; `object` fields pass through uncoerced."""
    model_config = ConfigDict(frozen=True, extra="ignore")
    id: object = None
    position: str | None = None
    company: str | None = None
    url: str | None = None
    apply_url: str | None = None
    location: str | None = None
    description: object = ""
    tags: list[object] | None = None


async def fetch_remoteok(max_jobs: int = 500, gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    """
    Pull every active listing from RemoteOK, filter to the relevant ones
    (off the loop). Returns a list of job dicts in the standard crawler
    shape.
    """
    data = await http.get_json("https://remoteok.com/api", "RemoteOK", default=[])
    return await asyncio.to_thread(_jobs, data, max_jobs, gate)


def _jobs(data: JSON, max_jobs: int, gate: Callable[..., bool] | None) -> list[FetchedJob]:
    """The feed's payload `data` as fetch_remoteok's job dicts."""
    if not isinstance(data, list):
        raise TypeError("RemoteOK payload is a list")
    jobs: list[FetchedJob] = []
    for entry in data[:max_jobs + 1]:           # +1 for metadata stub
        if not isinstance(entry, dict):
            continue
        job = _Entry.model_validate(entry)
        jid = job.id
        if not jid:                              # legal/metadata stub
            continue

        title    = job.position or ""
        company  = job.company or "RemoteOK"
        url      = job.url or job.apply_url or ""
        location = job.location or "Remote"
        desc     = strip_html(job.description)

        # tags can enrich relevance matching (e.g. "ml", "python")
        tags = job.tags or []
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
