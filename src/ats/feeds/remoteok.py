"""
RemoteOK public job feed, and the job builder it shares with Remotive.

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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.net import http
from src.net.util import JSON, strip_html
from src.rows import FetchedJob


class RemoteEntry(BaseModel):
    """One remote-only feed listing, RemoteOK's or Remotive's names;
    `object` fields pass through uncoerced."""
    model_config = ConfigDict(frozen=True, extra="ignore")
    id: object = None
    title: str | None = Field(None, validation_alias=AliasChoices("position", "title"))
    company: str | None = Field(None, validation_alias=AliasChoices("company", "company_name"))
    url: str | None = None
    apply_url: str | None = None
    location: str | None = Field(None, validation_alias=AliasChoices(
        "location", "candidate_required_location"))
    description: object = ""
    tags: list[object] | None = None
    category: str | None = None


def remote_jobs(entries: list[JSON], source: str, gate: Callable[..., bool] | None
                ) -> list[FetchedJob]:
    """A remote-only feed's dict `entries` as job dicts, ids `<source>_<id>`:
    an id-less entry (a metadata stub) is skipped, a missing company is
    the feed's `source` name, a missing location "Remote"; `gate(title,
    text)` reads the body, tags and category.

    >>> [(j["id"], j["company"], j["location"]) for j in remote_jobs(
    ...     [{"legal": "x"}, {"id": 7, "position": "Dev", "company_name": "Acme"}], "RemoteOK", None)]
    [('remoteok_7', 'Acme', 'Remote')]
    """
    jobs: list[FetchedJob] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        e = RemoteEntry.model_validate(raw)
        if not e.id:
            continue
        title, desc = e.title or "", strip_html(e.description)
        tag_text = " ".join(str(t) for t in e.tags or [] if t)
        if gate is not None and not gate(title, f"{desc} {tag_text} {e.category or ''}".rstrip()):
            continue
        jobs.append({
            "id":          f"{source.lower()}_{e.id}",
            "company":     e.company or source,
            "title":       title,
            "url":         e.url or e.apply_url or "",
            "location":    e.location or "Remote",
            "description": desc,
            # A remote-only board; `location` is the candidate region
            # requirement (e.g. "USA Only", "Worldwide"), not an office.
            "remote_hint": f"board:{source.lower()}",
        })
    return jobs


async def fetch_remoteok(max_jobs: int = 500, gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    """RemoteOK's first `max_jobs` listings that pass `gate`, built off the loop."""
    data = await http.get_json("https://remoteok.com/api", "RemoteOK", default=[])
    if not isinstance(data, list):
        raise TypeError("RemoteOK payload is a list")
    return await asyncio.to_thread(remote_jobs, data[:max_jobs + 1], "RemoteOK", gate)
