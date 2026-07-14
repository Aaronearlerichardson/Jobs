"""
One-off script for the site-evaluate harness: pulls REAL, live job
descriptions for Aaron's actual target companies by calling this repo's own
fetchers directly (no invented text). Writes JSON to
tools/evaluate/out/target_jds.json in the portfolio-site repo.

Run from the Jobs repo root: python fetch_target_jds.py
"""
import json
import sys
from pathlib import Path

from jobcrawler.fetchers.ats_api import fetch_greenhouse
from jobcrawler.fetchers.bamboohr import fetch_bamboohr
from jobcrawler.fetchers.html_scrape import fetch_kula
from jobcrawler.fetchers.jazzhr import fetch_jazzhr

# (company_name, ats, slug/subdomain) — Aaron's three named targets plus a
# handful of other real, recognizable neurotech/BCI employers pulled from
# the crawler's own active company roster (local_tech.db), so the JD corpus
# isn't just three postings.
TARGETS = [
    ("Beacon Biosignals", "greenhouse", "beaconbiosignals"),
    ("Precision Neuroscience", "kula", "precision-neuroscience"),
    ("Paradromics", "jazzhr", "paradromicsinc"),
    ("Blackrock Neurotech", "bamboohr", "blackrock"),
    ("Ceribell", "greenhouse", "ceribell"),
    ("Corcept Therapeutics", "greenhouse", "corcepttherapeutics"),
]

OUT_PATH = Path(r"C:\Users\Jakda\git\Aaronearlerichardson.github.io\tools\evaluate\out\target_jds.json")


def fetch(company_name, ats, slug):
    if ats == "greenhouse":
        return fetch_greenhouse(slug, company_name)
    if ats == "kula":
        return fetch_kula(company_name, slug)
    if ats == "jazzhr":
        return fetch_jazzhr(company_name, slug)
    if ats == "bamboohr":
        return fetch_bamboohr(slug, company_name)
    return None


def main():
    all_jobs = []
    for company_name, ats, slug in TARGETS:
        print(f"[fetch] {company_name} ({ats}:{slug}) ...", file=sys.stderr)
        try:
            jobs = fetch(company_name, ats, slug)
        except Exception as e:
            print(f"  [!] {company_name}: {e}", file=sys.stderr)
            continue
        if jobs is None:
            print(f"  [!] {company_name}: fetcher returned None", file=sys.stderr)
            continue
        print(f"  -> {len(jobs)} jobs", file=sys.stderr)
        for j in jobs:
            all_jobs.append({
                "company": company_name,
                "ats": ats,
                "title": j.get("title"),
                "url": j.get("url") or j.get("apply_url") or j.get("id"),
                "location": j.get("location"),
                "description": j.get("description") or j.get("description_text") or "",
            })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_jobs, indent=2), encoding="utf-8")
    print(f"\nWrote {len(all_jobs)} jobs to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
