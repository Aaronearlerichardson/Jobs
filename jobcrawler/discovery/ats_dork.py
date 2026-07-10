"""
ATS "dorking" — find crawlable NC companies by mining search-engine-indexed
ATS board URLs, instead of guessing a board from a company name.

A search like `site:jobs.lever.co "Durham"` returns board URLs whose slug we
can read directly (e.g. jobs.lever.co/<slug>). We extract the ATS + slug/triple
from each URL, NC-verify the board, mission-score it, and add it.

Two entry points:
  * run_ddgs_dorks()  — fully automated via the ddgs package (DuckDuckGo).
                        Note: DDG's index for boards.greenhouse.io is stale, so
                        this under-delivers vs. a better search backend.
  * harvest_urls(urls)— process a list of ATS URLs gathered elsewhere (e.g. the
                        agent's WebSearch tool, which indexes these far better).
"""

import re

from .sniffer import _SIGS
from .probes import _extract_workday_triple
from .. import store
from ..fetchers import company as company_fetch
from ..claude import score_company_mission
from .local_sourcing import _sample_titles, nc_hq_signal

# Standard dork set: each ATS host x the NC location terms, plus a Workday sweep.
_NC_TERMS = '("North Carolina" OR Durham OR Raleigh OR "Research Triangle" OR Morrisville OR Cary)'
DORK_QUERIES = [
    f'site:boards.greenhouse.io {_NC_TERMS}',
    f'site:job-boards.greenhouse.io {_NC_TERMS}',
    f'site:jobs.lever.co {_NC_TERMS}',
    f'site:jobs.ashbyhq.com {_NC_TERMS}',
    f'site:jobs.smartrecruiters.com {_NC_TERMS}',
    f'"myworkdayjobs.com" ("Durham, NC" OR "Raleigh, NC" OR "Research Triangle") '
    f'(biotech OR pharma OR health OR clinical OR medical OR diagnostics)',
    # Neurotech / BCI — the candidate's bullseye. These companies (e.g. Science
    # Corp) are often on custom boards / non-.com domains, missed by name-guessing.
    f'("neurotechnology" OR "brain-computer" OR "neural interface" OR "neural implant" '
    f'OR "BCI" OR "electrophysiology" OR "neural signal") {_NC_TERMS} (careers OR jobs OR hiring)',
    f'site:jobs.ashbyhq.com ("neuro" OR "neural" OR "brain") {_NC_TERMS}',
]


def extract_boards_from_urls(urls):
    """From a list of URLs, return de-duped [(ats, slug|triple)] board handles."""
    out, seen = [], set()
    for u in urls:
        triple = _extract_workday_triple(u)
        if triple:
            key = ("workday", str(triple))
            if key not in seen:
                seen.add(key)
                out.append(("workday", triple))
            continue
        for ats, rx in _SIGS:
            m = rx.search(u)
            if not m:
                continue
            slug = m.group(1)
            if ats == "icims" and slug.lower() in ("www", "careers", "jobs"):
                continue
            key = (ats, slug)
            if key not in seen and len(slug) >= 2:
                seen.add(key)
                out.append((ats, slug))
            break
    return out


def _existing_boards(conn):
    rows = conn.execute("SELECT ats, slug, wd_tenant, wd_pod, wd_site FROM companies").fetchall()
    have = set()
    for r in rows:
        if r["ats"] == "workday" and r["wd_tenant"]:
            have.add(("workday", str((r["wd_tenant"], r["wd_pod"], r["wd_site"]))))
        elif r["slug"]:
            have.add((r["ats"], r["slug"]))
    return have


def harvest_urls(urls, verbose=True):
    """
    Extract boards from `urls`, NC-verify + mission-score the new ones, and
    upsert active health/bio/science ones. Returns (added, checked).
    Company name is provisionally the slug (real name can be refined later);
    mission scoring uses the board's live job titles for domain context.
    """
    boards = extract_boards_from_urls(urls)
    conn = store.connect()
    have = _existing_boards(conn)
    added = 0
    for ats, slug in boards:
        key = ("workday", str(slug)) if ats == "workday" else (ats, slug)
        if key in have:
            continue
        comp = ({"ats": "workday", "wd_tenant": slug[0], "wd_pod": slug[1], "wd_site": slug[2]}
                if ats == "workday" else {"ats": ats, "slug": slug})
        try:
            jobs = company_fetch.fetch_company_nc(comp)
        except Exception:
            jobs = []
        nc = len(jobs)
        name = (slug[0] if ats == "workday" else slug).replace("-", " ").title()
        # Add even with 0 current NC openings IF we can confirm an NC HQ/office
        # (so a daily run catches their next NC posting) — but not otherwise,
        # else non-NC companies that merely mention NC would pollute the roster.
        if nc == 0 and not nc_hq_signal(name):
            continue
        titles = _sample_titles({"ats": ats, "slug": slug})
        tier, score, reason = score_company_mission(name, " | ".join(t for t in titles if t))
        active = 1 if tier in ("healthcare-tech", "health-bio-science") else 0
        store.upsert_company(conn, dict(
            name=name, ats=ats, slug=slug if ats != "workday" else None,
            wd_tenant=slug[0] if ats == "workday" else None,
            wd_pod=slug[1] if ats == "workday" else None,
            wd_site=slug[2] if ats == "workday" else None,
            nc_job_count=nc, total_job_count=nc, mission_tier=tier,
            mission_score=score, mission_reason=reason, tags="nc_local",
            source="ats_dork", active=active))
        added += 1
        if verbose:
            print(f"  {name[:26]:26} {ats:12} nc={nc:2} {str(tier):19} "
                  f"{score if score else 0:.2f} {'ACTIVE' if active else 'inactive'}")
    return added, len(boards)


def run_ddgs_dorks(max_results=25):
    """Automated dorking via ddgs (best-effort; DDG's ATS index is patchy)."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    urls = []
    with DDGS() as ddg:
        for q in DORK_QUERIES:
            try:
                for r in ddg.text(q, max_results=max_results):
                    u = r.get("href") or r.get("url")
                    if u:
                        urls.append(u)
            except Exception as e:
                print(f"  [!] dork {q[:40]}...: {e}")
    return harvest_urls(urls)
