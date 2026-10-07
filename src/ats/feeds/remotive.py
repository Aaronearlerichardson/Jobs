"""
Remotive public job feed.

Remotive exposes a JSON endpoint at
https://remotive.com/api/remote-jobs that returns every active listing
in a single payload. No API key, no pagination, permissive CORS.

Optional `category` parameter narrows by slug
(e.g. "software-dev", "data"). We don't use it by default: the caller's
relevance gate is narrower than any single Remotive category.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from src.net import http
from src.net.util import strip_html
from src.rows import FetchedJob


class _Entry(BaseModel):
    """One listing; `object` fields pass through uncoerced."""
    model_config = ConfigDict(frozen=True, extra="ignore")
    id: object = None
    title: str | None = None
    company_name: str | None = None
    url: str | None = None
    candidate_required_location: str | None = None
    description: str | None = ""
    tags: list[object] | None = None
    category: str | None = None


class _Feed(BaseModel):
    """The payload; a wrong shape raises ValidationError."""
    model_config = ConfigDict(frozen=True, extra="ignore")
    jobs: list[_Entry] | None = None


async def fetch_remotive(category: str | None = None, max_jobs: int | None = None,
                         gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    """
    Pull Remotive's job feed; return relevant listings, read off the loop.

    `category`: optional slug, e.g. "software-dev". None = all categories.
    `max_jobs`: cap iteration (debugging). None = all.
    """
    url = "https://remotive.com/api/remote-jobs"
    if category:
        url = f"{url}?category={category}"

    data = await http.get_json(url, "Remotive", default={})
    return await asyncio.to_thread(_jobs, data, max_jobs, gate)


def _jobs(data: object, max_jobs: int | None, gate: Callable[..., bool] | None) -> list[FetchedJob]:
    """The feed's payload `data` as fetch_remotive's job dicts."""
    entries: list[_Entry] = (_Feed.model_validate(data).jobs or []) if isinstance(data, dict) else []
    if max_jobs is not None:
        entries = entries[:max_jobs]

    jobs: list[FetchedJob] = []
    for entry in entries:
        jid      = entry.id
        title    = entry.title or ""
        company  = entry.company_name or "Remotive"
        jurl     = entry.url or ""
        location = entry.candidate_required_location or "Remote"
        desc     = strip_html(entry.description)

        tags     = entry.tags or []
        tag_text = " ".join(str(t) for t in tags if t)
        cat      = entry.category or ""

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
