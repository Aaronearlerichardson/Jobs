"""LOCAL-TECH track.

Surfaces LOCAL-ONLY (Research-Triangle / NC, incl. ~2.5h commute) roles with
a genuine technical bar and a health / bio / science mission — clinical is
preferred but not required, and neural signals are not required. This is
the location-relaxed-to-NC / BCI-constraint-relaxed twin of the
REMOTE-NEURAL track; the two share fetchers, discovery, the company store,
the Claude scorers, and the parallel fetch pool, and differ only in their
gates and ranking.

Pipeline (company-store driven):
    active companies from the store (populated by discovery/local_sourcing
    + ats_dork + --import-seeds)
      -> NC-scoped board pull            (fetchers/company.py, parallel)
      -> exclude filter                  (CRA/coordinator/scribe + defense)
      -> technical-title pre-filter      (cheap, so the LLM only scores
                                          plausible roles)
      -> resume-fit scoring              (claude.score_resume_fit, parallel)
      -> store + digest ranked by fit    (company mission as tiebreak),
         tagged [LOCAL-TECH]

Consolidates the former jobcrawler/local_clinical.py (filter chain, domain
keywords, excludes, heuristic scorer) and jobcrawler/local_tech.py (store-
driven crawl + resume fit) into the tracks framework. Geography now uses
the shared remote_filter for the "remote" side of the classification —
higher precision than the old bare-token list ("distributed systems" no
longer reads as a remote role).
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config

from .. import store
from ..claude import score_resume_fit
from ..fetchers import company as company_fetch
from ..parallel import fetch_all
from ..remote_filter import remote_signal
from ..resume import resume_text

TAG = "[LOCAL-TECH]"
TRACK = "local-tech"

# --------------------------------------------------------------------------- #
#  Domain targets — health / bio / science (NOT requiring neural signals).      #
# --------------------------------------------------------------------------- #

DOMAIN_TARGET_KEYWORDS = [
    # clinical & health ML
    "clinical machine learning", "clinical ml", "health ml", "healthcare ai",
    "clinical ai", "clinical data scientist", "health data scientist",
    "clinical informatics", "health informatics", "biomedical data",
    "clinical nlp", "ehr", "electronic health record", "real-world data",
    "real world evidence", "population health",
    # medical-device ML & signal processing.
    # NOTE: bare "signal processing" was removed — it is domain-agnostic and
    # leaked military RF/SDR roles (e.g. counter-drone) into a clinical search.
    "medical device", "medical-device", "physiological signal",
    "biosignal", "biosensor", "wearable sensor", "ecg", "eeg signal",
    # diagnostics
    "diagnostic", "diagnostics", "computational pathology", "digital pathology",
    "radiology ai", "medical imaging", "image analysis", "biomarker",
    # genomics & computational biology
    "genomics", "genomic", "computational biology", "computational biologist",
    "bioinformatics", "single-cell", "sequencing", "proteomics",
    "systems biology",
    # pharma computational R&D
    "drug discovery", "computational chemistry", "cheminformatics",
    "molecular modeling", "pharmacometrics", "quantitative pharmacology",
    "computational drug",
    # digital health & wearables
    "digital health", "digital biomarker", "remote patient monitoring",
    "wearable", "telehealth", "connected health", "mhealth",
    # cross-cutting health-ML signal terms
    "biostatistics", "biostatistician", "epidemiolog", "medical ai",
    "clinical trial analytics",
    # broadened: health / bio / science (mission need not be clinical); the
    # LLM mission tier is the authoritative filter and drops "other".
    "health", "healthcare", "medical", "clinical", "patient", "hospital",
    "biotech", "biotechnology", "life science", "life sciences",
    "pharma", "pharmaceutical", "therapeutics", "drug",
    "biology", "biological", "molecular", "immunolog", "oncolog", "cell ",
    "chemistry", "biochemistry", "assay", "laboratory", "reagent",
    "scientific software", "scientific computing", "research software",
    "scientific instrument", "biomedical", "biopharma", "vaccine",
    "microbiolog", "neuroscience", "cardiolog", "radiolog", "pathology",
]

# --------------------------------------------------------------------------- #
#  Geographic gate — Triangle/NC onsite, remote via the shared remote filter.  #
# --------------------------------------------------------------------------- #

GEO_ONSITE_TOKENS = [
    "durham", "raleigh", "chapel hill", "morrisville", "cary",
    "research triangle park", "research triangle", "the triangle",
    "rtp", "north carolina", "nc",
]

_SHORT = 3


def _tok_in(token, text):
    t = token.lower()
    if len(t) <= _SHORT:
        return re.search(rf"\b{re.escape(t)}\b", text) is not None
    return t in text


def geo_mode(location, description=""):
    """
    Classify a posting's geography: "onsite" (Triangle/NC), "remote", or
    None (fails the local gate). Onsite wins when a posting is both local
    and remote-friendly. Remote detection delegates to the shared
    jobcrawler.remote_filter (workforce-context phrases, hard negations)
    instead of a bare token list.
    """
    text = f"{location} {description}".lower()
    if any(_tok_in(t, text) for t in GEO_ONSITE_TOKENS):
        return "onsite"
    if remote_signal(location, description):
        return "remote"
    return None


# --------------------------------------------------------------------------- #
#  Exclude gate — low-tech clinical-ops roles + defense/military.               #
# --------------------------------------------------------------------------- #

# Multiword/unambiguous phrases: matched anywhere in title+description.
EXCLUDE_ROLE_PHRASES = [
    "clinical research associate", "study coordinator",
    "clinical research coordinator", "clinical trial coordinator",
    "research coordinator", "site monitor", "site monitoring",
    "clinical monitor", "scribe", "data entry", "data-entry",
    "patient recruiter", "study assistant",
]

# Title-only short/ambiguous tokens (avoid false hits in body prose).
EXCLUDE_TITLE_TOKENS = ["cra", "csc"]

DEFENSE_TERMS = [
    "defense", "defence", "department of defense", "dod",
    "weapon", "weapons", "weaponry", "armament", "munition", "missile",
    "warfare", "warfighter", "combat", "military", "soldier",
    "security clearance", "ts/sci", "secret clearance", "active clearance",
    "polygraph", "darpa", "raytheon", "lockheed", "northrop",
    # military RF / counter-drone / SIGINT (the SkySafe class)
    "counter-uas", "counter uas", "c-uas", "counter-drone", "counter drone",
    "software-defined radio", "software defined radio", "sdr",
    "airspace security", "electronic warfare", "sigint",
    "signals intelligence", "spectrum dominance",
]

# Clearly non-health technical domains that can sneak a generic domain term.
# Kept tight to avoid over-exclusion — "surveillance" is intentionally NOT
# here (disease surveillance is clinical). Matched on word boundaries so
# e.g. a bare "defi" can't substring-match "defined"/"defibrillator".
NONCLINICAL_TERMS = [
    "blockchain", "cryptocurrency", "crypto wallet", "web3",
    "decentralized finance", "osint", "ad tech", "adtech", "ad-tech",
    "sportsbook", "igaming",
]


def exclude_reason(title, description=""):
    """Return a short reason string if the posting must be dropped, else None."""
    title_l = (title or "").lower()
    text = f"{title} {description}".lower()

    # Word-boundary match so "scribe" doesn't fire on "describe", "data entry"
    # doesn't fire mid-word, etc.
    for phrase in EXCLUDE_ROLE_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return f"role: {phrase}"
    for tok in EXCLUDE_TITLE_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", title_l):
            return f"role-title: {tok.upper()}"

    hit = next((d for d in DEFENSE_TERMS if _tok_in(d, text)), None)
    if hit:
        return f"defense: {hit}"
    # Military RF-radar: only exclude "radar" in a defense/military context.
    if "radar" in text and any(_tok_in(d, text) for d in
                               ("military", "defense", "defence", "weapon",
                                "warfare", "missile", "rf ")):
        return "defense: military radar"

    nc = next((d for d in NONCLINICAL_TERMS
               if re.search(rf"\b{re.escape(d)}\b", text)), None)
    if nc:
        return f"non-clinical: {nc}"
    return None


# --------------------------------------------------------------------------- #
#  Keyword focus — same mechanism as the remote-neural track.                   #
# --------------------------------------------------------------------------- #

def apply_to_config(cfg):
    """Broaden the shared keyword filter with the health/bio/science domain
    terms and force a local crawl. Mutates the live list objects in place so
    ``filters.is_relevant`` (which imported them at load time) sees the
    change without a re-import — in-memory only, never config.py on disk."""
    have = {k.lower() for k in cfg.CORE_KEYWORDS}
    added = [k for k in DOMAIN_TARGET_KEYWORDS if k.lower() not in have]
    cfg.CORE_KEYWORDS.extend(added)
    cfg.ACCEPT_REMOTE = False
    return added


def is_domain_target(title, description=""):
    text = f"{title} {description}".lower()
    return any(k.lower() in text for k in DOMAIN_TARGET_KEYWORDS)


# --------------------------------------------------------------------------- #
#  Technical gates.                                                             #
# --------------------------------------------------------------------------- #

# Cheap positive title gate: keep only plausibly-technical roles so we don't
# spend an LLM resume-fit call on nurses / sales / admin / facilities.
_TECH_TITLE = re.compile(
    r"engineer|scientist|develop|program(mer|ming)?|software|\bdata\b|analyst|"
    r"analytics|machine learning|\bml\b|\bai\b|bioinformatic|biostatist|"
    r"computational|informatics|quality|validation|verification|\bqa\b|\btest\b|"
    r"devops|infrastructure|platform|database|statistician|scientific|"
    r"automation|architect|research associate|\br&d\b|modeling|python",
    re.I,
)


def is_technical_role(title):
    return bool(_TECH_TITLE.search(title or ""))


# Heuristic fallback scorer (used only when the Claude API is unavailable).
_HIGH_BAR = [
    "machine learning", "deep learning", "model", "models", "algorithm",
    "research", "statistical", "biostatistic", "bioinformatic", "genomic",
    "computational", "build", "develop", "design", "pipeline", "analysis",
    "analytics", "software", "engineer", "programming", "code", "python",
    "sql", "database", "data engineering", "data management", "etl",
    "quality engineering", "test engineer", "validation", "verification",
    "automation", "infrastructure", "devops", "systems", "reporting",
    "data analysis", "data pipeline", "qa",
]
_LOW_BAR = [
    "sop", "standard operating procedure", "coordinate", "coordination",
    "monitor site", "site monitoring", "data entry", "scribe", "schedule",
    "recruit", "irb", "consent", "case report form", "study coordinator",
    "filing", "logistics", "patient care", "nursing", "phlebotomy",
    "front desk", "scheduling",
]
_NONTECH_TITLE = [
    "business development", "sales", "account executive", "account manager",
    "recruiter", "recruiting", "talent", "marketing", "customer success",
    "partnerships", "program manager", "project manager", "operations manager",
    "office manager", "people operations", "communications",
    "support specialist", "community manager", "executive assistant",
]
_TECH_TITLE_TOKENS = [
    "engineer", "scientist", "machine learning", " ml ", "algorithm",
    "research", "developer", "modeling", "computational", "biostatist",
    "bioinformatic", "data scien", "analyst", "analytics", "data manager",
    "data management", "quality", "test ", "validation", "database",
    "systems", "devops", "sre", "reliability", "software", "informatics",
    "statistician", "programmer",
]


def heuristic_score(title, description=""):
    title_l = (title or "").lower()
    text = f"{title} {title} {description}".lower()  # title weighted x2
    hi = sum(text.count(k) for k in _HIGH_BAR)
    lo = sum(text.count(k) for k in _LOW_BAR)
    base = 0.4 if (hi == 0 and lo == 0) else 0.5 + 0.12 * (hi - 2 * lo)
    base = max(0.0, min(1.0, base))
    if any(t in title_l for t in _TECH_TITLE_TOKENS):
        base = max(base, 0.5)
    if any(t in title_l for t in _NONTECH_TITLE):
        base = min(base, 0.25)
    return round(base, 2)


# --------------------------------------------------------------------------- #
#  Runner.                                                                      #
# --------------------------------------------------------------------------- #

def _score_job(resume, company, job):
    company_fetch.hydrate_description(job)
    fit, reason = score_resume_fit(resume, job["title"], job.get("description", ""))
    return {
        "job_id": job["id"], "company_id": company["id"], "company_name": company["name"],
        "title": job["title"], "url": job["url"], "location": job["location"],
        "track": TRACK,
        "geo_mode": geo_mode(job["location"], job.get("description", "")) or "onsite",
        "description": (job.get("description", "") or "")[:2000],
        "tech_bar_score": None, "resume_fit_score": fit, "fit_reason": reason,
    }


def run(max_workers=6, top_n=15):
    """
    Live local crawl: active store companies -> NC postings (parallel) ->
    exclude + technical gate -> resume-fit-score NEW jobs (parallel) ->
    store + digest ranked by fit. NEVER emails.
    """
    resume = resume_text()
    if not resume:
        print("  [!] No resume text — fit scores will be null. Set config.RESUME_PATH.")
    apply_to_config(config)  # so Duke/UNC keyword-gated fetchers surface health-bio jobs
    conn = store.connect()
    companies = store.get_companies(conn, active_only=True)

    bar = "=" * 66
    print(f"\n{bar}\n  {TAG} crawl - {datetime.now():%Y-%m-%d %H:%M}"
          f"\n  {len(companies)} active compan(ies)\n{bar}\n")
    if not companies:
        print("  [!] Company store is empty. Populate it first:\n"
              "        python crawler.py --import-seeds      (config lists)\n"
              "        python discover.py --local             (NC sourcing pass)\n")

    # Fetch every company's NC-scoped board in parallel (remote-neural's
    # thread pool; per-company politeness lives inside each fetcher).
    sources = [(c["name"], c["ats"] or "?",
                (lambda cc=c: company_fetch.fetch_company(cc, company_fetch.NC_RE)))
               for c in companies]
    fetched = fetch_all(sources)

    to_score, n_fetched, n_tech, n_skip = [], 0, 0, 0
    for c, (jobs, err) in zip(companies, fetched):
        if err is not None:
            print(f"  {c['name']:26} [!] fetch error: {err}")
            continue
        n_fetched += len(jobs)
        kept = []
        for j in jobs:
            if exclude_reason(j["title"], j.get("description", "")):
                continue
            if not is_technical_role(j["title"]):
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

    ranked = store.ranked_jobs(conn, track=TRACK)
    write_digest(ranked)

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
    Ingest external job dicts into the jobs table with resume-fit scores.
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
        if not is_technical_role(j.get("title", "")):
            continue
        if not store.job_exists(conn, j["id"]):
            kept.append(j)

    def _score(j):
        fit, reason = score_resume_fit(resume, j["title"], j.get("description", ""))
        return {"job_id": j["id"], "company_id": None, "company_name": j.get("company"),
                "title": j.get("title"), "url": j.get("url"), "location": j.get("location"),
                "track": TRACK,
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


def write_digest(ranked):
    config.REPORT_DIR.mkdir(exist_ok=True)
    path = config.REPORT_DIR / f"local_tech_{datetime.now():%Y-%m-%d}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {TAG} Job Digest — {datetime.now():%Y-%m-%d}\n\n")
        f.write(f"**{len(ranked)} job(s)**, ranked by resume-fit (company mission as tiebreak).\n\n")
        f.write("| Fit | Company | Mission | Title | Location | Why |\n")
        f.write("|----:|---------|---------|-------|----------|-----|\n")
        for j in ranked:
            fit = j["resume_fit_score"]
            fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
            f.write(f"| {fs} | {j['company_name']} | {j.get('mission_tier') or '?'} "
                    f"| [{j['title']}]({j['url']}) | {j['location']} | {j.get('fit_reason','')} |\n")
    print(f"  digest -> {path}")
    return path
