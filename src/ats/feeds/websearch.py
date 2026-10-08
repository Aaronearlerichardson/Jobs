"""
DuckDuckGo-powered web search for jobs (free, no API key).

Why DDG: Google blocks automated search without an API key; DDG allows
modest programmatic access via the `ddgs` package. Coverage is smaller than Google for Jobs but
meaningfully broadens the crawler's reach over just hitting hard-coded
ATS tenants.

Pipeline per query:
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from src.ats.board.jsonld import fetch_jsonld_page
from src.net import ddg
from src.rows import FetchedJob


async def fetch_websearch(label: str, query: str, max_results: int = 15,
                          per_result_delay: float = 0.5,
                          gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    """
    Run one DDG query; for each result URL, scan for JSON-LD JobPosting.
    `label` is used as the company name when we can't infer one.
    """
    print(f"    -> Query: {query!r}")
    results = await ddg.search(query, max_results=max_results)
    if not results:
        return []

    jobs: list[FetchedJob] = []
    seen_urls: set[str] = set()
    for r in results:
        url = r.get("href") or r.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        jobs.extend(await fetch_jsonld_page(label, url, gate=gate))
        await asyncio.sleep(per_result_delay)
    return jobs
