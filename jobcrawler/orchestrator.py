"""Top-level crawl driver — loops every source and collects new jobs."""

import time
from datetime import datetime

import config
from config import (
    ACCEPT_REMOTE,
    CUSTOM_COMPANIES,
    DISCOURSE_BOARDS,
    HNHIRING_ENABLED,
    HNHIRING_MAX_THREADS,
    JSONLD_COMPANIES,
    REMOTEOK_ENABLED,
    REMOTIVE_CATEGORY,
    REMOTIVE_ENABLED,
    REPORT_DIR,
    RSS_FEEDS,
    SITEMAP_COMPANIES,
    WEBSEARCH_QUERIES,
)
from .db import init_db, is_new, mark_seen
from .fetchers import (
    fetch_custom,
    fetch_discourse,
    fetch_hnhiring,
    fetch_jsonld_careers,
    fetch_remoteok,
    fetch_remotive,
    fetch_rss,
    fetch_sitemap,
    fetch_websearch,
)
from .sources import iter_config_sources
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

    # ─── Per-ATS sources (declarative registry — see sources.py) ─────────
    _LABEL = {"adp": "ADP WFN", "jazzhr": "JazzHR", "bamboohr": "BambooHR",
              "successfactors": "SuccessFactors", "peopleadmin": "PeopleAdmin"}
    for ats, name, _slug, thunk, pause in iter_config_sources(config):
        print(f"  > {name} ({_LABEL.get(ats, ats.title())})")
        process(thunk())
        time.sleep(pause)

    for name, base_url, cat_id in DISCOURSE_BOARDS:
        print(f"  > {name} (Discourse)")
        process(fetch_discourse(name, base_url, cat_id))
        time.sleep(0.5)

    for name, url, sel in CUSTOM_COMPANIES:
        print(f"  > {name} (HTML scrape)")
        process(fetch_custom(name, url, sel))
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

    for entry in WEBSEARCH_QUERIES:
        # Optional 4th element flags remote-only boards; skip those when
        # ACCEPT_REMOTE is off so a pure-local crawl doesn't waste DDG
        # quota on WeWorkRemotely/Himalayas/Remote.co etc.
        label, query, max_results, *rest = entry
        remote_only = bool(rest[0]) if rest else False
        if remote_only and not ACCEPT_REMOTE:
            print(f"  . {label} (DuckDuckGo) — skipped, remote-only "
                  f"and ACCEPT_REMOTE=False")
            continue
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
