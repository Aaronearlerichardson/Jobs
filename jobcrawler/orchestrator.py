"""Top-level crawl driver — loops every source and collects new jobs."""

import time
from datetime import datetime

from config import (
    ASHBY_COMPANIES,
    CUSTOM_COMPANIES,
    DISCOURSE_BOARDS,
    GREENHOUSE_COMPANIES,
    HNHIRING_ENABLED,
    HNHIRING_MAX_THREADS,
    JSONLD_COMPANIES,
    KULA_COMPANIES,
    LEVER_COMPANIES,
    PEOPLEADMIN_COMPANIES,
    REMOTEOK_ENABLED,
    REMOTIVE_CATEGORY,
    REMOTIVE_ENABLED,
    REPORT_DIR,
    RSS_FEEDS,
    SITEMAP_COMPANIES,
    SUCCESSFACTORS_COMPANIES,
    WEBSEARCH_QUERIES,
    WORKDAY_COMPANIES,
)
from .db import init_db, is_new, mark_seen
from .fetchers import (
    fetch_ashby,
    fetch_custom,
    fetch_discourse,
    fetch_greenhouse,
    fetch_hnhiring,
    fetch_jsonld_careers,
    fetch_kula,
    fetch_lever,
    fetch_peopleadmin,
    fetch_remoteok,
    fetch_remotive,
    fetch_rss,
    fetch_sitemap,
    fetch_successfactors,
    fetch_websearch,
    fetch_workday,
)
from .filters import is_location_allowed


def crawl(dry_run=False):
    conn = None if dry_run else init_db()
    REPORT_DIR.mkdir(exist_ok=True)
    all_new, total_relevant = [], 0

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  BCI Job Crawler  -  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{bar}")
    if dry_run:
        print("  *** DRY-RUN: no DB writes, no email ***")
    print()

    def process(jobs):
        nonlocal total_relevant
        filtered = []
        for job in jobs:
            if is_location_allowed(job.get("location", "")):
                filtered.append(job)
            else:
                print(f"    [LOC-SKIP] {job['title']} - {job.get('location','?')}")
        total_relevant += len(filtered)
        for job in filtered:
            new = dry_run or is_new(conn, job["id"])
            if new:
                all_new.append(job)
                if not dry_run:
                    mark_seen(conn, job)
                print(f"    {'[DRY]' if dry_run else '[NEW]'} {job['title']}")

    # ─── Per-ATS sources ──────────────────────────────────────────────────

    for slug, name in GREENHOUSE_COMPANIES.items():
        print(f"  > {name} (Greenhouse)")
        process(fetch_greenhouse(slug, name))
        time.sleep(0.5)

    for slug, name in LEVER_COMPANIES.items():
        print(f"  > {name} (Lever)")
        process(fetch_lever(slug, name))
        time.sleep(0.5)

    for slug, name in ASHBY_COMPANIES.items():
        print(f"  > {name} (Ashby)")
        process(fetch_ashby(slug, name))
        time.sleep(0.5)

    for name, slug in KULA_COMPANIES:
        print(f"  > {name} (Kula)")
        process(fetch_kula(name, slug))
        time.sleep(0.5)

    for name, base_url, cat_id in DISCOURSE_BOARDS:
        print(f"  > {name} (Discourse)")
        process(fetch_discourse(name, base_url, cat_id))
        time.sleep(0.5)

    for name, url, sel in CUSTOM_COMPANIES:
        print(f"  > {name} (HTML scrape)")
        process(fetch_custom(name, url, sel))
        time.sleep(1.0)

    for name, base_url in SUCCESSFACTORS_COMPANIES:
        print(f"  > {name} (SuccessFactors)")
        process(fetch_successfactors(name, base_url))
        time.sleep(1.0)

    for tenant, wd_pod, site, name in WORKDAY_COMPANIES:
        print(f"  > {name} (Workday)")
        process(fetch_workday(tenant, wd_pod, site, name))
        time.sleep(1.0)

    for host, name in PEOPLEADMIN_COMPANIES:
        print(f"  > {name} (PeopleAdmin)")
        process(fetch_peopleadmin(host, name))
        time.sleep(1.0)

    # ─── Generic sources (JSON-LD / sitemap / web search) ─────────────────

    for name, url in JSONLD_COMPANIES:
        print(f"  > {name} (JSON-LD careers)")
        process(fetch_jsonld_careers(name, url))
        time.sleep(1.0)

    for entry in SITEMAP_COMPANIES:
        name, sitemap_url, url_filter = entry if len(entry) == 3 else (*entry, None)
        print(f"  > {name} (sitemap)")
        process(fetch_sitemap(name, sitemap_url, url_filter=url_filter))
        time.sleep(1.0)

    for label, query, max_results in WEBSEARCH_QUERIES:
        print(f"  > {label} (DuckDuckGo)")
        process(fetch_websearch(label, query, max_results=max_results))
        time.sleep(2.0)

    # ─── Aggregator JSON / RSS feeds (non-company-owned boards) ───────────

    if REMOTEOK_ENABLED:
        print("  > RemoteOK (public JSON)")
        process(fetch_remoteok())
        time.sleep(1.0)

    if REMOTIVE_ENABLED:
        print("  > Remotive (public JSON)")
        process(fetch_remotive(category=REMOTIVE_CATEGORY))
        time.sleep(1.0)

    if HNHIRING_ENABLED:
        print("  > HN 'Who is hiring?' (Firebase API)")
        process(fetch_hnhiring(max_threads=HNHIRING_MAX_THREADS))
        time.sleep(1.0)

    for label, url, default_location in RSS_FEEDS:
        print(f"  > {label} (RSS)")
        process(fetch_rss(label, url, default_location=default_location))
        time.sleep(1.0)

    print(f"\n  Done - {total_relevant} relevant listing(s), {len(all_new)} new.\n")
    if conn:
        conn.close()
    return all_new
