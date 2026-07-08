"""
DuckDuckGo-powered web search for jobs (free, no API key).

Why DDG: Google blocks automated search without an API key; DDG allows
modest programmatic access via the `ddgs` package (formerly
`duckduckgo-search`). Coverage is smaller than Google for Jobs but
meaningfully broadens the crawler's reach over just hitting hard-coded
ATS tenants.

Pipeline per query:
  1. Run DDG text search with your `site:` / operator-flavored query.
  2. For each result URL, fetch the page and extract JSON-LD JobPosting
     records (via fetchers/jsonld.py).
  3. Filters + dedupe happen in the orchestrator like any other source.

Rate limiting: DDG tolerates a few queries per minute comfortably. We
sleep between queries and between per-result page fetches.
"""

import time

from .jsonld import fetch_jsonld_page


def _ddg_search(query, max_results=15):
    """Return a list of {title, href, body} dicts. Handles both package names."""
    try:
        from ddgs import DDGS              # new name (2024+)
    except ImportError:
        try:
            from duckduckgo_search import DDGS   # legacy name
        except ImportError:
            print("    [!] ddgs not installed. Run: pip install ddgs")
            return []

    try:
        with DDGS() as ddg:
            return list(ddg.text(query, max_results=max_results))
    except Exception as e:
        print(f"    [!] DuckDuckGo search failed: {e}")
        return []


def fetch_websearch(label, query, max_results=15, per_result_delay=0.5):
    """
    Run one DDG query; for each result URL, scan for JSON-LD JobPosting.
    `label` is used as the company name when we can't infer one.
    """
    print(f"    -> Query: {query!r}")
    results = _ddg_search(query, max_results=max_results)
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
