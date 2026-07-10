"""
LOCAL-TECH crawl driver (company-DB → job list with résumé-fit).

Reads active companies from the SQL store, pulls their NC postings, keeps the
technical ones (cheap title pre-filter, so the LLM only scores plausible jobs),
résumé-fit-scores each NEW job in parallel, writes them to the jobs table, and
prints/writes a digest ranked by fit (company mission is the tiebreak).

    python crawler.py --local-tech
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
from . import store, local_fetch
from .resume import resume_text
from .claude import score_resume_fit
from .local_clinical import exclude_reason, geo_mode, _broaden_relevance

TAG = "[LOCAL-TECH]"

# Cheap positive title gate: keep only plausibly-technical roles so we don't
# spend an LLM résumé-fit call on nurses / sales / admin / facilities.
_TECH_TITLE = re.compile(
    r"engineer|scientist|develop|program(mer|ming)?|software|\bdata\b|analyst|"
    r"analytics|machine learning|\bml\b|\bai\b|bioinformatic|biostatist|"
    r"computational|informatics|quality|validation|verification|\bqa\b|\btest\b|"
    r"devops|infrastructure|platform|database|statistician|scientific|"
    r"automation|architect|research associate|\br&d\b|modeling|python",
    re.I,
)


def _is_technical(title):
    return bool(_TECH_TITLE.search(title or ""))


def _score_job(resume, company, job):
    local_fetch.hydrate_description(job)
    fit, reason = score_resume_fit(resume, job["title"], job.get("description", ""))
    return {
        "job_id": job["id"], "company_id": company["id"], "company_name": company["name"],
        "title": job["title"], "url": job["url"], "location": job["location"],
        "geo_mode": geo_mode(job["location"], job.get("description", "")) or "onsite",
        "description": (job.get("description", "") or "")[:2000],
        "tech_bar_score": None, "resume_fit_score": fit, "fit_reason": reason,
    }


def run(max_workers=6, top_n=15, digest_min_fit=0.0):
    resume = resume_text()
    if not resume:
        print("  [!] No résumé text — fit scores will be null. Set config.RESUME_PATH.")
    _broaden_relevance()  # so Duke/UNC keyword-gated fetchers surface health-bio jobs
    conn = store.connect()
    companies = store.get_companies(conn, active_only=True)

    bar = "=" * 66
    print(f"\n{bar}\n  {TAG} crawl - {datetime.now():%Y-%m-%d %H:%M}"
          f"\n  {len(companies)} active compan(ies)\n{bar}\n")

    to_score, n_fetched, n_tech, n_skip = [], 0, 0, 0
    for c in companies:
        jobs = local_fetch.fetch_company_nc(c)
        n_fetched += len(jobs)
        kept = []
        for j in jobs:
            if exclude_reason(j["title"], j.get("description", "")):
                continue
            if not _is_technical(j["title"]):
                continue
            kept.append(j)
        n_tech += len(kept)
        fresh = [j for j in kept if not store.job_exists(conn, j["id"])]
        n_skip += len(kept) - len(fresh)
        for j in fresh:
            to_score.append((c, j))
        print(f"  {c['name']:26} {len(jobs):3} NC -> {len(kept):2} technical "
              f"-> {len(fresh):2} new")

    print(f"\n  scoring {len(to_score)} new job(s) against resume "
          f"({n_skip} already scored)...")
    scored = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_score_job, resume, c, j): j for c, j in to_score}
        for fut in as_completed(futs):
            try:
                store.upsert_job(conn, fut.result())
                scored += 1
            except Exception as e:
                print(f"    [!] scoring error: {e}")

    ranked = store.ranked_jobs(conn)
    _write_digest(ranked)

    print(f"\n  {bar}\n  TOP {min(top_n, len(ranked))} BY RESUME FIT\n  {bar}")
    for j in ranked[:top_n]:
        fit = j["resume_fit_score"]
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        tier = j.get("mission_tier") or "?"
        print(f"  {TAG} fit={fs} [{j.get('geo_mode','?')}] {(j['title'] or '')[:52]}")
        print(f"        {j['company_name']} ({tier})  -  {j.get('fit_reason','')}")
        print(f"        {j['url']}")
    print(f"\n  {len(ranked)} job(s) in store; {scored} newly scored this run.")
    print(f"  *** NO EMAIL SENT (preview) ***\n")
    return ranked


def ingest_external_jobs(jobs, source="indeed", max_workers=6):
    """
    Ingest external job dicts into the jobs table with résumé-fit scores.
    Each dict: {id?, title, company, url, location, description?}. Applies the
    same exclude + technical-title gate as the crawl. For agent-mediated
    sources (e.g. the Indeed MCP) that the standalone crawler can't poll —
    the caller supplies the fetched jobs.
    """
    import hashlib
    resume = resume_text()
    conn = store.connect()
    kept = []
    for j in jobs:
        if not j.get("id"):
            key = (j.get("url") or "") + (j.get("title") or "") + (j.get("company") or "")
            j["id"] = f"{source}_{hashlib.md5(key.encode()).hexdigest()[:12]}"
        if exclude_reason(j.get("title", ""), j.get("description", "")):
            continue
        if not _is_technical(j.get("title", "")):
            continue
        if not store.job_exists(conn, j["id"]):
            kept.append(j)

    def _score(j):
        fit, reason = score_resume_fit(resume, j["title"], j.get("description", ""))
        return {"job_id": j["id"], "company_id": None, "company_name": j.get("company"),
                "title": j.get("title"), "url": j.get("url"), "location": j.get("location"),
                "geo_mode": geo_mode(j.get("location", ""), j.get("description", "")) or "onsite",
                "description": (j.get("description", "") or "")[:2000],
                "tech_bar_score": None, "resume_fit_score": fit, "fit_reason": reason,
                "status": "open"}

    scored = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_score, j): j for j in kept}):
            try:
                store.upsert_job(conn, fut.result())
                scored += 1
            except Exception as e:
                print(f"    [!] ingest error: {e}")
    print(f"  ingested {scored} new {source} job(s) ({len(kept)} technical, "
          f"{len(jobs)} raw)")
    return scored


def _write_digest(ranked):
    config.REPORT_DIR.mkdir(exist_ok=True)
    path = config.REPORT_DIR / f"local_tech_{datetime.now():%Y-%m-%d}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {TAG} Job Digest — {datetime.now():%Y-%m-%d}\n\n")
        f.write(f"**{len(ranked)} job(s)**, ranked by résumé-fit (company mission as tiebreak).\n\n")
        f.write("| Fit | Company | Mission | Title | Location | Why |\n")
        f.write("|----:|---------|---------|-------|----------|-----|\n")
        for j in ranked:
            fit = j["resume_fit_score"]
            fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
            f.write(f"| {fs} | {j['company_name']} | {j.get('mission_tier') or '?'} "
                    f"| [{j['title']}]({j['url']}) | {j['location']} | {j.get('fit_reason','')} |\n")
    print(f"  digest -> {path}")
    return path
