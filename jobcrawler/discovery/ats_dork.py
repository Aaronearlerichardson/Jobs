"""
ATS "dorking" — find crawlable NC companies by mining search-engine-indexed
ATS board URLs, instead of guessing a board from a company name.

A search like `site:jobs.lever.co "Durham"` returns board URLs whose slug we
can read directly (e.g. jobs.lever.co/<slug>). We extract the ATS + slug/triple
from each URL, NC-verify the board, mission-score it, and add it.

Two entry points:
  * run_ddgs_dorks()  — fully automated via the ddgs package (DuckDuckGo).
"""

import re
import time

import config

from .sniffer import _SIGS
from .probes import _extract_workday_triple
from .. import store
from ..fetchers import company as company_fetch
from ..claude import score_company_mission
from .local_sourcing import _sample_titles, nc_hq_signal


def _or_group(terms, n=8):
    """A `("a" OR b OR "c d")` search clause from profile terms (multi-word
    terms quoted). Empty string when there are no terms."""
    picked = [t for t in terms[:n] if t]
    if not picked:
        return ""
    return "(" + " OR ".join(f'"{t}"' if " " in t else t for t in picked) + ")"


# Dork set derived from the loaded profile. DDG chokes on long `site:` + big
# OR-group queries (returns nothing), so the site-scoped dorks use a SHORT
# locality clause (top few terms); the free-text sweeps can afford more.
_LOC = _or_group(config.LOCALITY_SUBSTRINGS or config.LOCALITY_WORD_TOKENS, n=8)
_LOC_SITE = _or_group(config.LOCALITY_SUBSTRINGS or config.LOCALITY_WORD_TOKENS, n=4)
_DOMAIN = _or_group(config.DOMAIN_KEYWORDS, n=6)
_CORE = _or_group(config.CORE_KEYWORDS, n=6)

DORK_QUERIES = [
    f'site:boards.greenhouse.io {_LOC_SITE}',
    f'site:job-boards.greenhouse.io {_LOC_SITE}',
    f'site:jobs.lever.co {_LOC_SITE}',
    f'site:jobs.ashbyhq.com {_LOC_SITE}',
    f'site:jobs.smartrecruiters.com {_LOC_SITE}',
    f'"myworkdayjobs.com" {_LOC_SITE}' + (f" {_DOMAIN}" if _DOMAIN else ""),
]
if _CORE:
    # Bullseye sweep — target companies are often on custom boards / non-.com
    # domains that name-guessing misses.
    DORK_QUERIES.append(f'{_CORE} {_LOC} (careers OR jobs OR hiring)')
DORK_QUERIES = [q for q in DORK_QUERIES if _LOC_SITE and _LOC_SITE in q or _CORE and _CORE in q]

# Non-slug path fragments the greenhouse/embed URL forms expose — never a real
# board (boards.greenhouse.io/embed/job_board?for=<realslug>).
_SLUG_STOP = {"embed", "job_board", "jobs", "js", "boards", "job-boards",
              "www", "careers", "search", "api"}


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
            if slug.lower() in _SLUG_STOP:   # embed/job_board/js/... not a board
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


def _ddg_text(query, max_results, retries=2, pause=2.5):
    """One DDG query with retry/backoff. DDG rate-limits aggressively and
    surfaces it as an exception ("No results found."/"Ratelimit"), so a fresh
    session + a pause between attempts recovers far more than a single try.
    Returns a list of result URLs (possibly empty)."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        return []
    for attempt in range(retries + 1):
        try:
            with DDGS() as ddg:
                return [u for r in ddg.text(query, max_results=max_results)
                        if (u := (r.get("href") or r.get("url")))]
        except Exception as e:
            if attempt < retries:
                time.sleep(pause * (attempt + 1))
                continue
            print(f"  [!] dork {query[:48]}...: {e}")
    return []


def run_ddgs_dorks(max_results=25, pause=2.5):
    """Automated dorking via ddgs (best-effort; DDG's ATS index is patchy).
    Queries are spaced out — hammering DDG back-to-back is what makes it start
    returning 'No results found' mid-run."""
    urls = []
    for i, q in enumerate(DORK_QUERIES):
        if i:
            time.sleep(pause)          # be gentle between queries
        found = _ddg_text(q, max_results)
        print(f"  [dork] {len(found):2} result(s)  {q[:60]}")
        urls += found
    return harvest_urls(urls)
