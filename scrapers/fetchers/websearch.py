"""
DuckDuckGo-powered web search for jobs (free, no API key).

Why DDG: Google blocks automated search without an API key; DDG allows
modest programmatic access via the `ddgs` package (formerly
`duckduckgo-search`). Coverage is smaller than Google for Jobs but
meaningfully broadens the crawler's reach over just hitting hard-coded
ATS tenants.

Pipeline per query:
"""

import time

from .. import ddg
from .jsonld import fetch_jsonld_page


def fetch_websearch(label, query, max_results=15, per_result_delay=0.5):
    """
    Run one DDG query; for each result URL, scan for JSON-LD JobPosting.
    `label` is used as the company name when we can't infer one.
    """
    print(f"    -> Query: {query!r}")
    results = ddg.search(query, max_results=max_results)
    if not results:
        return []

    jobs, seen_urls = [], set()
    for r in results:
        url = r.get("href") or r.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        jobs.extend(fetch_jsonld_page(label, url))
        time.sleep(per_result_delay)
    return jobs
