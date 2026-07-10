"""
LOCAL-TECH track (formerly LOCAL-CLINICAL-ML).

A self-contained pipeline that surfaces LOCAL-ONLY (Research-Triangle / NC,
incl. ~2.5h commute) roles with a genuine technical bar and a health / bio /
science mission — clinical is preferred but not required, and neural signals
are not required. Kept in its own module so it merges cleanly alongside the
concurrent track-remote-neural work — it reuses the shared fetchers but
applies its own filter chain, scorer, db, and digest.

Filter chain (counts printed before/after each step):
    fetched candidates
      -> domain filter       (coarse health/bio/science keyword hit)
      -> geographic filter    (Triangle/NC onsite ONLY — remote dropped)
      -> exclude filter       (CRA/coordinator/scribe/data-entry + defense)
      -> technical-bar score  (0..1) + LLM mission tier
      -> mission gate         (drop "other"; keep health/bio/science)
      -> digest ranked by tech bar + healthcare-tech priority bonus,
         tagged [LOCAL-TECH]
"""

import re
import sqlite3
import time
from datetime import datetime

import config
from .claude import score_technical_bar

TAG = "[LOCAL-TECH]"

# Compact mission labels for the digest (all roles are onsite-NC now, so the
# geo flag is redundant; the mission tier is the useful per-line signal).
_MISSION_LABEL = {
    "healthcare-tech":    "HEALTHCARE-TECH",
    "health-bio-science": "HEALTH-BIO-SCI",
}

# --------------------------------------------------------------------------- #
#  1. Domain targets — clinical & health ML (NOT requiring neural signals).    #
#     A candidate must hit at least one of these to count as on-mission.       #
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
    # Medical signal processing is still captured by the qualified terms below
    # (physiological signal / biosignal / medical device / wearable sensor).
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
    # --- broadened: health / bio / science (mission need not be clinical) ---
    # The coarse keyword gate is intentionally generous here; the LLM mission
    # tier (healthcare-tech / health-bio-science / other) is the authoritative
    # filter and drops "other", so these just widen the candidate pool.
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
#  2. Geographic gate — strict Triangle/NC onsite, plus remote-eligible.       #
# --------------------------------------------------------------------------- #

# Long tokens use substring; short tokens (<=3 chars: "nc") use word-boundary.
GEO_ONSITE_TOKENS = [
    "durham", "raleigh", "chapel hill", "morrisville", "cary",
    "research triangle park", "research triangle", "the triangle",
    "rtp", "north carolina", "nc",
]

GEO_REMOTE_TOKENS = [
    "remote", "work from home", "wfh", "fully remote", "remote-first",
    "remote first", "remote-eligible", "remote eligible", "distributed",
    "anywhere", "us-remote", "remote (us",
]

_SHORT = 3


def _tok_in(token, text):
    t = token.lower()
    if len(t) <= _SHORT:
        return re.search(rf"\b{re.escape(t)}\b", text) is not None
    return t in text


def geo_mode(location, description=""):
    """
    Classify a posting's geography.

    Returns one of "onsite", "remote", or None (fails the local gate).
    Onsite (Triangle/NC) wins when a posting is both local and remote-friendly.
    """
    text = f"{location} {description}".lower()
    if any(_tok_in(t, text) for t in GEO_ONSITE_TOKENS):
        return "onsite"
    if any(_tok_in(t, text) for t in GEO_REMOTE_TOKENS):
        return "remote"
    return None


# --------------------------------------------------------------------------- #
#  3. Exclude gate — low-tech clinical-ops roles + defense/military.           #
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

# Clearly non-health technical domains that can sneak a generic domain term
# (e.g. a forensics/blockchain shop mentioning "analysis"/"detection"). Dropped
# so the digest stays clinical/health. Kept tight to avoid over-exclusion —
# "surveillance" is intentionally NOT here (disease surveillance is clinical).
NONCLINICAL_TERMS = [
    "blockchain", "cryptocurrency", "crypto wallet", "web3",
    "decentralized finance", "osint", "ad tech", "adtech", "ad-tech",
    "sportsbook", "igaming",
]
# (bare "defi" was removed — it substring-matched "defined"/"defibrillator"/
#  "deficiency". Non-clinical terms are matched on word boundaries below to
#  prevent this whole class of substring false-positive.)


def exclude_reason(title, description=""):
    """Return a short reason string if the posting must be dropped, else None."""
    title_l = (title or "").lower()
    text = f"{title} {description}".lower()

    # Word-boundary match so "scribe" doesn't fire on "describe", "data entry"
    # doesn't fire mid-word, etc. (a bare substring check wrongly dropped a
    # "Founding Engineer" role whose JD merely said "describe").
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
#  Relevance broadening — let clinical/health postings survive the fetchers.   #
#                                                                              #
#  The shared fetchers gate on filters.is_relevant(), which is neuro-tuned.    #
#  We extend the live CORE_KEYWORDS list in-process (same object filters.py    #
#  iterates) so domain-target roles pass the fetch stage. This mutates only    #
#  in-memory state in THIS process — never the config.py file on disk — so it  #
#  cannot affect the concurrent track-remote-neural run.                       #
# --------------------------------------------------------------------------- #

def _broaden_relevance():
    have = {k.lower() for k in config.CORE_KEYWORDS}
    added = [k for k in DOMAIN_TARGET_KEYWORDS if k.lower() not in have]
    config.CORE_KEYWORDS.extend(added)
    return added


def is_domain_target(title, description=""):
    text = f"{title} {description}".lower()
    return any(k.lower() in text for k in DOMAIN_TARGET_KEYWORDS)


# --------------------------------------------------------------------------- #
#  Isolated dedupe store (own schema: persists score + geo mode).              #
# --------------------------------------------------------------------------- #

def _init_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id     TEXT PRIMARY KEY,
            company    TEXT,
            title      TEXT,
            url        TEXT,
            location   TEXT,
            geo_mode   TEXT,
            tech_score REAL,
            first_seen TEXT
        )
    """)
    conn.commit()
    return conn


def _is_new(conn, job_id):
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
    ).fetchone() is None


def _mark_seen(conn, job):
    # Upsert: keep first_seen stable but refresh the score/geo on re-runs so
    # the stored score always reflects the latest scorer (heuristic -> Claude).
    conn.execute(
        """INSERT INTO seen_jobs
               (job_id, company, title, url, location, geo_mode, tech_score, first_seen)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
               company    = excluded.company,
               title      = excluded.title,
               location   = excluded.location,
               geo_mode   = excluded.geo_mode,
               tech_score = excluded.tech_score""",
        (job["id"], job["company"], job["title"], job["url"],
         job["location"], job.get("geo_mode"), job.get("tech_score"),
         datetime.now().isoformat()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
#  Source iteration — mirrors orchestrator.crawl's source list but yields      #
#  raw (location-unfiltered) candidates so we own the whole filter chain.      #
# --------------------------------------------------------------------------- #

def _iter_candidates():
    from .fetchers import (
        fetch_ashby, fetch_custom, fetch_discourse, fetch_greenhouse,
        fetch_hnhiring, fetch_jsonld_careers, fetch_kula, fetch_lever,
        fetch_peopleadmin, fetch_remoteok, fetch_remotive, fetch_rss,
        fetch_sitemap, fetch_successfactors, fetch_websearch, fetch_workday,
    )

    sources = []
    for slug, name in config.GREENHOUSE_COMPANIES.items():
        sources.append((f"{name} (Greenhouse)", lambda s=slug, n=name: fetch_greenhouse(s, n), 0.5))
    for slug, name in config.LEVER_COMPANIES.items():
        sources.append((f"{name} (Lever)", lambda s=slug, n=name: fetch_lever(s, n), 0.5))
    for slug, name in config.ASHBY_COMPANIES.items():
        sources.append((f"{name} (Ashby)", lambda s=slug, n=name: fetch_ashby(s, n), 0.5))
    for name, slug in config.KULA_COMPANIES:
        sources.append((f"{name} (Kula)", lambda n=name, s=slug: fetch_kula(n, s), 0.5))
    for name, base_url, cat_id in config.DISCOURSE_BOARDS:
        sources.append((f"{name} (Discourse)", lambda n=name, b=base_url, c=cat_id: fetch_discourse(n, b, c), 0.5))
    for name, url, sel in config.CUSTOM_COMPANIES:
        sources.append((f"{name} (HTML)", lambda n=name, u=url, s=sel: fetch_custom(n, u, s), 1.0))
    for name, base_url in config.SUCCESSFACTORS_COMPANIES:
        sources.append((f"{name} (SuccessFactors)", lambda n=name, b=base_url: fetch_successfactors(n, b), 1.0))
    for tenant, wd_pod, site, name in config.WORKDAY_COMPANIES:
        sources.append((f"{name} (Workday)", lambda t=tenant, p=wd_pod, s=site, n=name: fetch_workday(t, p, s, n), 1.0))
    for host, name in config.PEOPLEADMIN_COMPANIES:
        sources.append((f"{name} (PeopleAdmin)", lambda h=host, n=name: fetch_peopleadmin(h, n), 1.0))
    for name, url in config.JSONLD_COMPANIES:
        sources.append((f"{name} (JSON-LD)", lambda n=name, u=url: fetch_jsonld_careers(n, u), 1.0))
    for entry in config.SITEMAP_COMPANIES:
        name, sm, uf = entry if len(entry) == 3 else (*entry, None)
        sources.append((f"{name} (sitemap)", lambda n=name, s=sm, u=uf: fetch_sitemap(n, s, url_filter=u), 1.0))
    for entry in config.WEBSEARCH_QUERIES:
        label, query, max_results, *_ = entry
        sources.append((f"{label} (DDG)", lambda l=label, q=query, m=max_results: fetch_websearch(l, q, max_results=m), 2.0))
    if config.REMOTEOK_ENABLED:
        sources.append(("RemoteOK", fetch_remoteok, 1.0))
    if config.REMOTIVE_ENABLED:
        sources.append(("Remotive", lambda: fetch_remotive(category=config.REMOTIVE_CATEGORY), 1.0))
    if config.HNHIRING_ENABLED:
        sources.append(("HN Who-is-hiring", lambda: fetch_hnhiring(max_threads=config.HNHIRING_MAX_THREADS), 1.0))
    for label, url, default_location in config.RSS_FEEDS:
        sources.append((f"{label} (RSS)", lambda l=label, u=url, d=default_location: fetch_rss(l, u, default_location=d), 1.0))

    candidates = []
    for label, fn, pause in sources:
        print(f"  > {label}")
        try:
            jobs = fn() or []
        except Exception as e:
            print(f"    [!] {label} failed: {e}")
            jobs = []
        print(f"    {len(jobs)} candidate(s)")
        candidates.extend(jobs)
        time.sleep(pause)
    return candidates


# --------------------------------------------------------------------------- #
#  Digest rendering.                                                           #
# --------------------------------------------------------------------------- #

def _display_title(job):
    """
    Sanitize a title for display. Some aggregator/HN fetchers dump URLs or JD
    blobs into the title field; strip URLs, collapse whitespace, and cap the
    length so the digest stays readable. (Display only — stored data is intact.)
    """
    t = re.sub(r"https?://\S+", "", job.get("title") or "").strip()
    t = re.sub(r"\s+", " ", t).strip(" -—|")
    if len(t) > 100:
        t = t[:97].rstrip() + "..."
    return t or "(title unavailable)"


def digest_line(job):
    mission = _MISSION_LABEL.get(job.get("mission"), "MISSION?")
    score = job.get("tech_score")
    score_s = f"{score:.2f}" if isinstance(score, (int, float)) else " n/a"
    return (f"{TAG} {score_s} [{mission}] {_display_title(job)} — {job['company']} "
            f"— {job.get('location') or 'Unknown'}")


def write_digest(ranked, path):
    date_str = datetime.now().strftime("%Y-%m-%d")
    hc = sum(1 for j in ranked if j.get("mission") == "healthcare-tech")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {TAG} Job Digest — {date_str}\n\n")
        f.write(f"**{len(ranked)} local NC role(s)** "
                f"({hc} healthcare-tech, {len(ranked) - hc} broader health/bio/science), "
                f"ranked by technical bar with a healthcare-tech priority bonus.\n\n")
        f.write("| Score | Mission | Title | Company | Location |\n")
        f.write("|------:|---------|-------|---------|----------|\n")
        for j in ranked:
            score = j.get("tech_score")
            score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
            mission = _MISSION_LABEL.get(j.get("mission"), "?")
            f.write(f"| {score_s} | {mission} | [{_display_title(j)}]({j['url']}) "
                    f"| {j['company']} | {j.get('location') or 'Unknown'} |\n")
        f.write("\n---\n\n")
        for j in ranked:
            reason = j.get("tech_reason") or ""
            f.write(f"- {digest_line(j)}\n")
            if reason:
                f.write(f"    - _bar: {reason}_\n")
    return path


# --------------------------------------------------------------------------- #
#  Driver.                                                                     #
# --------------------------------------------------------------------------- #

def run(db_path=None, top_n=5):
    """
    Live local-clinical crawl. Writes dedup state + a ranked digest file but
    NEVER emails. Prints counts before/after each filter and the top-N
    by technical-bar score for spot-checking the scorer.
    """
    db_path = db_path or config.LOCAL_CLINICAL_DB_PATH
    config.DB_PATH = db_path  # so any shared db helpers also target this file
    config.REPORT_DIR.mkdir(exist_ok=True)

    bar = "=" * 64
    print(f"\n{bar}")
    print(f"  {TAG} crawl — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  db: {db_path}")
    print(f"{bar}\n")

    added = _broaden_relevance()
    print(f"  + broadened fetch relevance with {len(added)} health/bio/science "
          f"domain term(s) (in-process only)\n")

    candidates = _iter_candidates()

    # De-dup within this run by job id AND by url (sources can overlap, and
    # some boards list the same role under different ids / the same apply URL).
    seen_ids, seen_urls, deduped = set(), set(), []
    for j in candidates:
        jid = j["id"]
        url = (j.get("url") or "").strip().rstrip("/")
        if jid in seen_ids or (url and url in seen_urls):
            continue
        seen_ids.add(jid)
        if url:
            seen_urls.add(url)
        deduped.append(j)

    print(f"\n  {bar}")
    print("  FILTER CHAIN")
    print(f"  {bar}")
    n0 = len(deduped)
    print(f"  fetched candidates (deduped) ............... {n0}")

    # --- domain filter --------------------------------------------------
    domain_pass = [j for j in deduped if is_domain_target(j["title"], j.get("description", ""))]
    print(f"  after clinical/health domain filter ........ {len(domain_pass)}  "
          f"(-{n0 - len(domain_pass)})")

    # --- geographic filter (LOCAL-ONLY: Triangle/NC incl. ~2.5h) --------
    # Remote-eligible roles are intentionally dropped here — this branch is
    # local-only. (Remote lives on the track-remote-neural branch.)
    geo_pass, dropped_remote = [], 0
    for j in domain_pass:
        mode = geo_mode(j.get("location", ""), j.get("description", ""))
        if mode == "onsite":
            j["geo_mode"] = mode
            geo_pass.append(j)
        elif mode == "remote":
            dropped_remote += 1
    print(f"  after geographic filter (NC-local only) .... {len(geo_pass)}  "
          f"(-{len(domain_pass) - len(geo_pass)})  "
          f"[{dropped_remote} remote-eligible dropped]")

    # --- exclude filter -------------------------------------------------
    kept = []
    for j in geo_pass:
        reason = exclude_reason(j["title"], j.get("description", ""))
        if reason:
            print(f"      [EXCLUDE] {j['title']} — {reason}")
        else:
            kept.append(j)
    print(f"  after exclude filter (ops/defense dropped) . {len(kept)}  "
          f"(-{len(geo_pass) - len(kept)})")

    # --- technical-bar scoring (+ LLM mission tier) ---------------------
    print(f"\n  scoring {len(kept)} role(s) on technical bar (0..1)...")
    used_heuristic = 0
    for j in kept:
        score, reason, mission = score_technical_bar(j["title"], j.get("description", ""))
        if score is None:
            score = _heuristic_score(j["title"], j.get("description", ""))
            reason, mission = "heuristic", None
            used_heuristic += 1
        j["tech_score"] = round(score, 2)
        j["tech_reason"] = reason
        j["mission"] = mission
    if used_heuristic:
        print(f"  [!] Claude scorer unavailable for {used_heuristic} role(s); "
              f"used keyword heuristic fallback (set ANTHROPIC_API_KEY for the "
              f"real scorer).")

    # LLM mission gate: broadened to health/bio/science — drop only roles the
    # scorer judged "other" (generic SaaS/fintech/defense/etc.). Unknown
    # (heuristic fallback, None) is kept.
    before = len(kept)
    for j in kept:
        if j.get("mission") == "other":
            print(f"      [OFF-MISSION] {_display_title(j)} — {j['company']}")
    kept = [j for j in kept if j.get("mission") != "other"]
    print(f"  after mission gate (health/bio/science) .... {len(kept)}  "
          f"(-{before - len(kept)})")

    # Rank by technical bar, but PRIORITIZE healthcare/tech mission with a
    # gentle bonus (healthcare-tech > health-bio-science > unknown), so a
    # strong health-product role edges out an equal generic-science one.
    def _rank_key(j):
        bonus = {"healthcare-tech": 0.15, "health-bio-science": 0.05}.get(j.get("mission"), 0.0)
        return round(j.get("tech_score", 0.0) + bonus, 4)
    for j in kept:
        j["rank_score"] = _rank_key(j)
    ranked = sorted(kept, key=lambda j: j["rank_score"], reverse=True)

    # --- persist dedup state + digest -----------------------------------
    conn = _init_db(db_path)
    new_count = 0
    for j in ranked:
        if _is_new(conn, j["id"]):
            new_count += 1
        _mark_seen(conn, j)
    conn.close()

    date_str = datetime.now().strftime("%Y-%m-%d")
    digest_path = config.REPORT_DIR / f"local_clinical_{date_str}.md"
    write_digest(ranked, digest_path)

    # --- preview ---------------------------------------------------------
    print(f"\n  {bar}")
    print(f"  TOP {min(top_n, len(ranked))} (tech bar + healthcare-tech priority)")
    print(f"  {bar}")
    if not ranked:
        print("  (no roles survived the filter chain)")
    for j in ranked[:top_n]:
        print(f"  {digest_line(j)}")
        if j.get("tech_reason"):
            print(f"        bar: {j['tech_reason']}")
        print(f"        {j['url']}")

    print(f"\n  {len(ranked)} role(s) kept, {new_count} new since last run.")
    print(f"  digest -> {digest_path}")
    print(f"  *** NO EMAIL SENT (preview mode) ***\n")
    return ranked


# --------------------------------------------------------------------------- #
#  Heuristic fallback scorer (used only when the Claude API is unavailable).   #
# --------------------------------------------------------------------------- #

# Broad technical signal — ANY hands-on technical/quantitative work counts,
# not just ML (data management, quality/test/validation eng, analysis, infra).
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

# Non-technical role titles: regardless of how many ML buzzwords the company
# boilerplate sprinkles into the JD, these roles have a low technical bar.
# Capped hard so a "Business Development Manager" at an ML company can't rank
# above an actual algorithms role. (The real Claude scorer judges this from
# responsibilities; this is the offline stand-in.)
_NONTECH_TITLE = [
    "business development", "sales", "account executive", "account manager",
    "recruiter", "recruiting", "talent", "marketing", "customer success",
    "partnerships", "program manager", "project manager", "operations manager",
    "office manager", "people operations", "communications",
    "support specialist", "community manager", "executive assistant",
]

# Title tokens that signal a genuinely technical role (build / analyze / test /
# manage-data / engineer) — broadened beyond ML per the candidate's intent.
_TECH_TITLE = [
    "engineer", "scientist", "machine learning", " ml ", "algorithm",
    "research", "developer", "modeling", "computational", "biostatist",
    "bioinformatic", "data scien", "analyst", "analytics", "data manager",
    "data management", "quality", "test ", "validation", "database",
    "systems", "devops", "sre", "reliability", "software", "informatics",
    "statistician", "programmer",
]


def _heuristic_score(title, description=""):
    title_l = (title or "").lower()
    text = f"{title} {title} {description}".lower()  # title weighted x2
    hi = sum(text.count(k) for k in _HIGH_BAR)
    lo = sum(text.count(k) for k in _LOW_BAR)
    base = 0.4 if (hi == 0 and lo == 0) else 0.5 + 0.12 * (hi - 2 * lo)
    base = max(0.0, min(1.0, base))
    # A genuinely technical title floors the score up.
    if any(t in title_l for t in _TECH_TITLE):
        base = max(base, 0.5)
    # A non-technical role title caps it hard, overriding JD buzzwords.
    if any(t in title_l for t in _NONTECH_TITLE):
        base = min(base, 0.25)
    return round(base, 2)
