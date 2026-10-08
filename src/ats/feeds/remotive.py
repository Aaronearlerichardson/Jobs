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

from src.net import http
from src.rows import FetchedJob
from .remoteok import remote_jobs


async def fetch_remotive(category: str | None = None, max_jobs: int | None = None,
                         gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    """Remotive's listings (of the `category` slug, e.g. "software-dev",
    when given; the first `max_jobs` when given) that pass `gate`, built
    off the loop."""
    url = "https://remotive.com/api/remote-jobs"
    if category:
        url = f"{url}?category={category}"
    data = await http.get_json(url, "Remotive", default={})
    jobs = data.get("jobs") if isinstance(data, dict) else None
    entries = jobs if isinstance(jobs, list) else []
    return await asyncio.to_thread(remote_jobs, entries[:max_jobs], "Remotive", gate)
