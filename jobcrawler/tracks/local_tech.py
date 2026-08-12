"""LOCAL-TECH track.

Surfaces LOCAL-ONLY (Research-Triangle / NC, incl. ~2.5h commute) roles with
a genuine technical bar and a health / bio / science mission — clinical is
preferred but not required, and neural signals are not required. This is
the location-relaxed-to-NC / BCI-constraint-relaxed twin of the
REMOTE-NEURAL track; the two share fetchers, discovery, the company store,
the Claude scorers, and the parallel fetch pool, and differ only in their
gates and ranking.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import config

from .. import store
from ..claude import score_resume_fit
from ..filters import is_relevant, scrub_boilerplate
from ..fetchers import company as company_fetch
from ..nc import NC_RE
from ..parallel import fetch_all
from ..remote_filter import remote_signal
from ..resume import resume_text

TAG = "[LOCAL-TECH]"
TRACK = "local-tech"

# Ranking floor on company mission (see the ranked_jobs call in run()). Tier
# "other" companies score ~0.0-0.15, health/bio/med-tech ones ~0.4-0.97, so 0.2
# cleanly separates them. Only applied to companies that HAVE been scored.
MIN_MISSION_FOR_RANKING = 0.2

# --------------------------------------------------------------------------- #
#  Domain targets — health / bio / science (NOT requiring neural signals).      #
#  Source lists live in profile.toml [keywords.local_tech] (config.py exposes  #
#  them via KEYWORDS_BY_TRACK) — NOT hardcoded here, so they're editable       #
#  without touching code. Empty dict/list -> track applies nothing extra.      #
# --------------------------------------------------------------------------- #

_TRACK_KEYWORDS = getattr(config, "KEYWORDS_BY_TRACK", {}).get("local_tech", {})

# SPECIFIC health/bio/science terms — strong enough to stand alone anywhere
# in a posting (they join CORE_KEYWORDS, full-text tier-1 matching).
DOMAIN_TARGET_KEYWORDS = list(_TRACK_KEYWORDS.get("core", []))

# GENERIC health words — real signal only in context. They fire on benefits
# boilerplate ("medical, dental, vision", "health savings account", "drug-free
# workplace") and infra prose ("service health checks"), so they join
# DOMAIN_KEYWORDS instead: a hit counts only when PAIRED with a skill term in
# the posting head (filters.is_relevant tier 2x3), never alone. This is what
# made the multi-division keyword gate a sieve — is_relevant("Accountant",
# <benefits boilerplate>) was True via CORE "health"/"medical"/"drug".
# (bare "diagnostic(s)" is deliberately absent even here: at the
# multi-division employers this gate exists for, it means GPU/system
# diagnostics and pairs with "firmware"/"validation" into a false match,
# while real health-diagnostics JDs always carry "clinical"/"laboratory"/
# "health" too. The specific forms live in DOMAIN_TARGET_KEYWORDS.)
DOMAIN_TARGET_GENERIC = list(_TRACK_KEYWORDS.get("domain", []))

# Local-track skill vocabulary added to SKILL_KEYWORDS for the pairing tier —
# the track's technical bar is broader than the neural default (any genuine
# technical/quant role), so the pair partner list needs the everyday tools.
LOCAL_SKILL_KEYWORDS = list(_TRACK_KEYWORDS.get("skill", []))

# --------------------------------------------------------------------------- #
#  Geographic gate — Triangle/NC onsite, remote via the shared remote filter.  #
# --------------------------------------------------------------------------- #
#  Body-text onsite tokens used to be a separate hardcoded GEO_ONSITE_TOKENS
#  list; that duplicated profile.toml [locality] (word_tokens/substrings),
#  which nc.NC_RE already compiles with the same word-boundary/substring
#  rules. geo_mode() below now reuses NC_RE for both the location field and
#  the body-text scan — add any missing local place name to [locality]
#  substrings instead of a second list here.

_SHORT = 3


def _tok_in(token, text):
    t = token.lower()
    if len(t) <= _SHORT:
        return re.search(rf"\b{re.escape(t)}\b", text) is not None
    return t in text


def _is_neural_tagged(company):
    """True if a company store row carries the 'neural' scope tag (the
    BCI/neural-signal target list shared with the neural track). Used to
    relax local-tech's NC-only geo gate for these companies specifically —
    an explicitly REMOTE posting from one still counts as local-tech
    material; an onsite one outside NC does not."""
    if not company:
        return False
    return "neural" in {t for t in (company.get("tags") or "").split(",") if t}


def _is_watched(company):
    """True if the company carries the 'watch' tag (set via crawler.py
    --watch NAME): the user wants to see EVERY new technical posting there —
    the "wait for the right role at this employer" list. Watched companies
    get their whole board fetched (like neural-tagged ones) and their new
    technical postings surface in a dedicated digest section regardless of
    fit rank or geography."""
    if not company:
        return False
    return "watch" in {t for t in (company.get("tags") or "").split(",") if t}


def _whole_board(company):
    """Companies whose ENTIRE board is fetched (no NC location filter):
    neural-tagged (remote postings count as local-tech material) and watched
    (never miss a new posting). Everyone else gets the NC-scoped pull."""
    return _is_neural_tagged(company) or _is_watched(company)


def geo_mode(location, description=""):
    """
    Classify a posting's geography: "onsite" (Triangle/NC), "remote", or
    None (fails the local gate). Onsite wins when a posting is both local
    and remote-friendly — a "Remote; Durham, NC" multi-location posting is
    LOCAL material, not a remote drop. Both the location FIELD and the body
    text are checked against the full configured locality regex (nc.NC_RE —
    profile [locality], the same terms the fetch filter uses) so "hybrid
    from our Durham office" still counts as onsite. Remote detection
    delegates to the shared jobcrawler.remote_filter (workforce-context
    phrases, hard negations) instead of a bare token list.
    """
    if NC_RE.search(f"{location or ''} {description or ''}"):
        return "onsite"
    if remote_signal(location, description):
        return "remote"
    return None


# --------------------------------------------------------------------------- #
#  Exclude gate — low-tech clinical-ops roles + defense/military.               #
#  Source lists live in profile.toml [exclude.local_tech] (config.py exposes   #
#  them via EXCLUDE_BY_TRACK) — NOT hardcoded here.                            #
# --------------------------------------------------------------------------- #

_TRACK_EXCLUDE = getattr(config, "EXCLUDE_BY_TRACK", {}).get("local_tech", {})

# Multiword/unambiguous phrases: matched anywhere in title+description.
EXCLUDE_ROLE_PHRASES = list(_TRACK_EXCLUDE.get("role_phrases", []))

# Title-only short/ambiguous tokens (avoid false hits in body prose).
EXCLUDE_TITLE_TOKENS = list(_TRACK_EXCLUDE.get("title_tokens", []))

# Defense/military exclusion, two tiers. STRONG terms are unambiguous — one
# hit excludes. WEAK terms are words health postings use innocently, so a
# single hit is ignored and TWO DISTINCT weak terms are required: "military"
# alone is EEO boilerplate 126 times out of 135 in the stored corpus
# ("military or veteran status" — scrubbed before matching, but belt and
# suspenders), "combat" alone is "combating antibiotic resistance", "dod" /
# "darpa" alone is a funding-source mention in an academic or health-IT JD,
# and "sdr" alone is a Sales Development Representative. A real defense JD
# (CoVar-class) names several of these at once.
DEFENSE_TERMS_STRONG = list(_TRACK_EXCLUDE.get("defense_strong", []))
DEFENSE_TERMS_WEAK = list(_TRACK_EXCLUDE.get("defense_weak", []))
# Back-compat union (external callers/tests iterate DEFENSE_TERMS).
DEFENSE_TERMS = DEFENSE_TERMS_STRONG + DEFENSE_TERMS_WEAK

# Clearly non-health technical domains that can sneak a generic domain term.
# Kept tight to avoid over-exclusion — "surveillance" is intentionally NOT
# here (disease surveillance is clinical). Matched on word boundaries so
# e.g. a bare "defi" can't substring-match "defined"/"defibrillator".
NONCLINICAL_TERMS = list(_TRACK_EXCLUDE.get("nonclinical", []))


def exclude_reason(title, description="", allow_defense=False):
    """Return a short reason string if the posting must be dropped, else None.

    `allow_defense` skips the DEFENSE_TERMS/military-radar exclusion ONLY —
    role-quality excludes (coordinator/scribe/data-entry) always apply. Set
    for WATCHED companies: `--watch Covar` means "I know this employer is
    defense-adjacent and want its technical roles anyway". The mission score
    stays abysmal by design, so these roles surface (watch section, scored
    rows) without out-ranking on-mission work."""
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

    if not allow_defense:
        # EEO/benefits boilerplate scrub first: "military or veteran status"
        # must never read as a defense signal.
        scrubbed = scrub_boilerplate(text)
        hit = next((d for d in DEFENSE_TERMS_STRONG if _tok_in(d, scrubbed)), None)
        if hit:
            return f"defense: {hit}"
        weak = [d for d in DEFENSE_TERMS_WEAK if _tok_in(d, scrubbed)]
        if len(weak) >= 2:
            return f"defense: {'+'.join(weak[:3])}"
        # Military RF-radar: only exclude "radar" in a defense/military context.
        if "radar" in scrubbed and any(_tok_in(d, scrubbed) for d in
                                       ("military", "defense", "defence", "weapon",
                                        "warfare", "missile", "rf ")):
            return "defense: military radar"

    nc = next((d for d in NONCLINICAL_TERMS
               if re.search(rf"\b{re.escape(d)}\b", text)), None)
    if nc:
        return f"non-clinical: {nc}"
    return None


# --------------------------------------------------------------------------- #
#  Keyword focus — unified in runner.apply_keyword_focus.                       #
# --------------------------------------------------------------------------- #

def apply_to_config(cfg):
    """Back-compat shim: keyword focus is now a per-track config concern
    (profile.toml [tracks.*].keyword_mode + [keywords.<id>]) applied by
    jobcrawler/tracks/runner.apply_keyword_focus — "extend" mode adds this
    track's specific terms to CORE (stand-alone, full-text), generic ones to
    DOMAIN, and the everyday-tools list to SKILL, so a generic word only
    counts paired with a skill in the posting head."""
    from . import runner
    runner.apply_keyword_focus(cfg, runner.track_for_engine("local"))


def is_domain_target(title, description=""):
    text = f"{title} {description}".lower()
    return any(k.lower() in text
               for k in DOMAIN_TARGET_KEYWORDS + DOMAIN_TARGET_GENERIC)


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

def _keep_job(company, job, geo_gate=True):
    """Local-track posting filter, shared by the full crawl and single-company
    crawls: exclude gate, technical-title gate, plus the health-keyword gate
    for multi-division conglomerates (keep only their aligned-subdivision
    roles — focused companies were already mission-vetted and skip it).
    `geo_gate=False` (a [tracks.*] methodology switch — see runner.py) skips
    the whole-board geography check, letting out-of-area postings through
    with their remote signal stamped downstream instead."""
    title = job.get("title", "")
    if not is_technical_role(title):
        return False
    if config.is_multi_division(company["name"]):
        # Workday/SmartRecruiters listings carry no description until the
        # detail call — but the relevance gate NEEDS the description (titles
        # like "Research Scientist" say nothing about the division). Hydrate
        # first; only NC-filtered jobs at conglomerates pay this extra GET.
        company_fetch.hydrate_description(job)
        if not is_relevant(title, job.get("description", "")):
            return False
    if exclude_reason(title, job.get("description", ""),
                      allow_defense=_is_watched(company)):
        return False
    if geo_gate and _whole_board(company):
        # Neural/BCI-tagged and watched companies are fetched with no
        # location restriction (see run()/crawl_company()), which lets their
        # remote and onsite-elsewhere reqs through the fetch. Gate here:
        #   watched  -> NC-onsite or explicitly-remote is scored (the watch
        #               tag is human-curated, and ranked_jobs admits watched
        #               remotes into the local list);
        #   neural   -> NC-onsite ONLY. The machine-set neural tag proved
        #               untrustworthy for an out-of-area exception — slug
        #               collisions (an EEG company's row pointing at a global
        #               AI board) flooded the ranking with remote-anywhere
        #               junk. Their remote roles are the remote-neural
        #               track's job.
        gm = geo_mode(job.get("location", ""), job.get("description", ""))
        if _is_watched(company):
            if gm is None:
                return False
        elif gm != "onsite":
            return False
    return True


def _score_job(resume, company, job, track=TRACK):
    company_fetch.hydrate_description(job)
    res = score_resume_fit(resume, job["title"], job.get("description", ""))
    return {
        "job_id": job["id"], "company_id": company["id"], "company_name": company["name"],
        "title": job["title"], "url": job["url"], "location": job["location"],
        "track": track,
        "geo_mode": geo_mode(job["location"], job.get("description", "")) or "onsite",
        "description": (job.get("description", "") or "")[:config.MAX_DESC_CHARS],
        "posted_at": job.get("posted_at"),
        **res.as_columns(),
    }


def _age_tag(row, today=None):
    """Compact posting-age tag for console/digest rows: 'NEW' the day we
    first see it, else days since posted_at ('6d', '45d!' when stale — a
    45+-day-old posting is often a ghost req). '?' when no date is known.
    Workday dates parsed from 'Posted 30+ Days Ago' are floors, so '30d!'
    there means AT LEAST 30 days."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    if (row.get("first_seen") or "")[:10] == today:
        return "NEW"
    posted = (row.get("posted_at") or "")[:10]
    if not posted:
        return "?"
    try:
        days = (datetime.strptime(today, "%Y-%m-%d")
                - datetime.strptime(posted, "%Y-%m-%d")).days
    except ValueError:
        return "?"
    return f"{days}d!" if days >= 45 else f"{days}d"


def crawl_company(conn, resume, company, max_workers=6):
    """Fetch ONE store company's NC-scoped board (or, for a neural/BCI-tagged
    company, its WHOLE board — see run()), apply the local-track filters,
    resume-fit-score the new postings, and store them. Returns
    (n_nc_fetched, n_kept, n_new). Used by the single-job --add flow to pull
    a company's other jobs once it's in the roster."""
    loc_re = None if _whole_board(company) else company_fetch.NC_RE
    try:
        jobs = company_fetch.fetch_company(company, loc_re)
    except Exception as e:
        print(f"    [!] fetch error for {company['name']}: {e}")
        return (0, 0, 0)
    # A successful non-empty snapshot is the authority on what this company
    # currently lists: close stored rows that vanished, revive returners.
    if jobs and company.get("id"):
        store.sync_job_statuses(conn, company["id"], jobs, track=TRACK)
    kept = [j for j in jobs if _keep_job(company, j)]
    fresh = [j for j in kept if not store.job_exists(conn, j["id"])]
    n_new = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_score_job, resume, company, j) for j in fresh]
        for fut in as_completed(futs):
            try:
                store.upsert_job(conn, fut.result())
                n_new += 1
            except Exception as e:
                print(f"    [!] scoring error: {e}")
    return (len(jobs), len(kept), n_new)


def self_heal_unscored(conn, resume, track=TRACK, max_workers=6):
    """Self-heal: the fresh-only crawl loop never revisits an already-stored
    job, so a row that was ingested bodyless (unscorable -> NULL score) would
    stay out of the ranking forever even once its description is recovered.
    Score any NULL-score row that now carries a real body (hydrated by
    backfill_board_descriptions, or by an earlier run). Returns #scored."""
    from ..fit import MIN_DESC_CHARS
    _ph = ",".join("?" for _ in store.RANKING_EXCLUDED_DISPOSITIONS)
    pending = [dict(r) for r in conn.execute(
        "SELECT job_id, title, description FROM jobs "
        "WHERE track = ? AND resume_fit_score IS NULL "
        "AND COALESCE(status,'open') != 'closed' "
        f"AND (disposition IS NULL OR disposition NOT IN ({_ph})) "
        "AND length(COALESCE(description,'')) >= ?",
        (track, *store.RANKING_EXCLUDED_DISPOSITIONS, MIN_DESC_CHARS)).fetchall()]
    if not pending:
        return 0
    print(f"  self-heal: scoring {len(pending)} newly-described "
          f"job(s) that were previously unscorable...")
    scored = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(score_resume_fit, resume, r["title"],
                          r.get("description", "")): r for r in pending}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"    [!] self-heal scoring error: {e}")
                continue
            if res.score is not None:
                store.update_job_scores(conn, r["job_id"], res.as_columns())
                scored += 1
    return scored


def run(max_workers=6, top_n=15, verify=True):
    """
    Live local crawl: active store companies -> locality-scoped postings ->
    exclude + technical gate -> resume-fit-score NEW jobs -> deep-verify the
    top N -> store + digest ranked by fit. NEVER emails (unless the track's
    config says otherwise).

    The pipeline itself lives in jobcrawler/tracks/runner.py (one runner for
    every track; methodology comes from profile.toml [tracks.*]) — this is
    the back-compat entry point for the default local-engine track.
    """
    from . import runner
    return runner.run_track(runner.track_for_engine("local"),
                            fit=True, commit=True, verify=verify,
                            max_workers=max_workers, top_n=top_n)


def backfill_board_descriptions(max_workers=8, limit=None, min_len=200):
    """One-shot: fill in full JD text for stored jobs missing it (any
    company-linked row whose description is shorter than min_len chars —
    the default matches jobcrawler.fit.MIN_DESC_CHARS), via each company's
    own ATS board — the non-Workday counterpart to fetchers/workday.py's
    backfill_workday_descriptions. Covers rows created with a title only
    (manually captured LinkedIn cards via capture.py) that were never
    scored because they never had a real JD body. Batched per company so a
    board with several stale rows is fetched once. Safe to re-run."""
    conn = store.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT job_id, title, url, company_id FROM jobs "
        "WHERE company_id IS NOT NULL "
        "AND COALESCE(status,'open') != 'closed' "
        "AND length(COALESCE(description,'')) < ?", (min_len,)).fetchall()]
    if limit:
        rows = rows[:int(limit)]
    print(f"  backfilling {len(rows)} description(s) via company board(s)...")

    by_company = {}
    for r in rows:
        by_company.setdefault(r["company_id"], []).append(r)

    n = 0
    for cid, rs in by_company.items():
        company = store.get_company(conn, cid)
        if not company or not company.get("ats"):
            continue
        # Batched board pull for the common case (one fetch per company);
        # boards we can't pull (e.g. SuccessFactors career sites with an
        # unknown slug — Duke) simply yield no title matches, and each row
        # falls through to per-job-URL hydration below.
        try:
            board = company_fetch.fetch_company(company, loc_re=None)
        except Exception as e:
            print(f"    [!] {company['name']}: {e}")
            board = []
        by_title = {(b.get("title") or "").strip().lower(): b for b in board}
        n_matched = 0
        for r in rs:
            desc = None
            match = by_title.get((r["title"] or "").strip().lower())
            if match is not None:
                company_fetch.hydrate_description(match)
                desc = match.get("description")
            if not desc and r.get("url"):
                # Board didn't cover this row — hydrate from the job's own
                # detail page (JSON-LD / SuccessFactors career-site markup).
                stub = {"title": r["title"], "url": r["url"],
                        "ats": company.get("ats"), "description": ""}
                company_fetch.hydrate_description(stub)
                desc = stub.get("description")
            if not desc:
                continue
            conn.execute("UPDATE jobs SET description=? WHERE job_id=?",
                         (desc[:config.MAX_DESC_CHARS], r["job_id"]))
            conn.commit()
            n += 1
            n_matched += 1
        print(f"    {company['name']:30} {len(rs):2} stale -> {n_matched:2} matched")
    conn.close()
    print(f"  {n} of {len(rows)} description(s) backfilled.")
    return n


def rescore_all(max_workers=6, track=None, described_only=False):
    """Re-run resume-fit scoring over every stored job (all tracks unless
    one is named). Use after changing the resume or the scoring prompt —
    the normal crawl only scores jobs it hasn't seen.

    described_only: only touch rows that have a real JD body (skip the rest
    entirely, leaving their scores untouched). Without it, a row with no body
    is unscorable, so its stale score is cleared to NULL so it drops out of
    ranking; a *described* row that merely fails to parse keeps its old score.

    Closed jobs are always skipped — no Claude API spend on postings that
    are already gone (they're excluded from ranking anyway).
    """
    from ..fit import MIN_DESC_CHARS
    resume = resume_text()
    if not resume:
        print("  [!] No resume text - cannot rescore. Set config.RESUME_PATH.")
        return 0
    conn = store.connect()
    # Skip closed rows AND anything the user has dispositioned out of the
    # ranking (applied/interviewing/rejected/dismissed): no fit-API spend on
    # jobs that can't surface anyway.
    ph = ",".join("?" for _ in store.RANKING_EXCLUDED_DISPOSITIONS)
    conds = ["COALESCE(status,'open') != 'closed'",
             f"(disposition IS NULL OR disposition NOT IN ({ph}))"]
    args = list(store.RANKING_EXCLUDED_DISPOSITIONS)
    if track:
        conds.append("track = ?")
        args.append(track)
    if described_only:
        conds.append("length(COALESCE(description,'')) >= ?")
        args.append(MIN_DESC_CHARS)
    q = "SELECT job_id, title, description FROM jobs"
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    print(f"  rescoring {len(rows)} job(s) against the current resume...")

    def _one(r):
        res = score_resume_fit(resume, r["title"], r.get("description", ""))
        return r["job_id"], res, r.get("description", "")

    n = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_one, r): r for r in rows}):
            try:
                jid, res, desc = fut.result()
            except Exception as e:
                print(f"    [!] rescore error: {e}")
                continue
            if res.score is None:
                # Unscorable (no real body): clear the stale score AND axis
                # columns so it drops from ranking. A described row that merely
                # failed to parse (has a body) is left alone to keep its score.
                if len((desc or "").strip()) < MIN_DESC_CHARS:
                    store.update_job_scores(
                        conn, jid, {"fit_reason": "no description; unscored"})
                    n += 1
                continue
            store.update_job_scores(conn, jid, res.as_columns())
            n += 1
    conn.close()
    print(f"  {n} job(s) rescored.")
    return n


# Greenhouse job-page URL -> (board slug, job id), for the boards-API detail
# fetch in _live_jd (works for boards.greenhouse.io and job-boards.greenhouse.io).
_GH_JOB_URL_RE = re.compile(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)")


def _live_jd(row):
    """Freshest full JD text for one stored job row, preferring a live
    detail fetch (Workday CXS, Greenhouse boards API, then the generic
    JSON-LD/careers-page extractor) over the stored text. Falls back to the
    stored description when the live pull is shorter or fails — the deep
    verify pass must never see LESS text than the first pass did."""
    url = row.get("url") or ""
    text = ""
    try:
        if "myworkdayjobs.com" in url:
            from ..fetchers.workday import fetch_workday_description
            text = fetch_workday_description(url) or ""
        else:
            m = _GH_JOB_URL_RE.search(url)
            if m:
                import html as _html

                from bs4 import BeautifulSoup

                from ..http import HEADERS, SESSION
                r = SESSION.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}"
                    f"/jobs/{m.group(2)}?content=true",
                    timeout=20, headers=HEADERS)
                if r.status_code == 200:
                    text = BeautifulSoup(
                        _html.unescape(r.json().get("content", "") or ""),
                        "lxml").get_text(" ")
        if not text and url:
            text = company_fetch._description_from_job_url(url)
    except Exception:
        text = ""
    stored = row.get("description") or ""
    return text if len(text) > len(stored) else stored


def verify_top(top_n=15, max_workers=4, rounds=2, conn=None, track=TRACK,
               min_mission=MIN_MISSION_FOR_RANKING):
    """Deep-verify the ranking's FINALISTS before anyone acts on them: for
    each of the current top `top_n` jobs not already verified (fit_reason
    carrying the 'deep:' marker), re-fetch the freshest full posting text
    (_live_jd), run fit.verify_fit — which extracts hard requirements
    (years, seat type, must-haves, candidate gaps) before re-scoring all
    axes and gates — and write the verified scores back. Demotions can pull
    new unverified rows into the top, so the pass re-ranks and repeats up
    to `rounds` times.

    This is the systemic catch for first-pass failures like the Ceribell
    Sr-Manager case: a 20k-char JD whose disqualifying requirements sat
    past every truncation cap scored 0.69 from company boilerplate; the
    full-text extraction pass grades it as the TPM seat it actually is.

    Unverifiable rows (dead URL and no stored body, API down) keep their
    first-pass score untouched. Costs at most top_n x rounds API calls per
    run, and only for rows that changed since their last verification."""
    from ..fit import verify_fit
    own_conn = conn is None
    if own_conn:
        conn = store.connect()
    n_done = 0
    for rnd in range(rounds):
        ranked = store.ranked_jobs(conn, track=track,
                                   location_re=company_fetch.NC_RE,
                                   rank_by="fit", allow_geo_modes={"remote"},
                                   min_mission=min_mission,
                                   limit=top_n)
        todo = [r for r in ranked if "deep:" not in (r.get("fit_reason") or "")]
        if not todo:
            break
        print(f"  deep-verifying {len(todo)} of the top {len(ranked)} "
              f"(round {rnd + 1}/{rounds})...")

        def _one(r):
            text = _live_jd(r)
            return r, text, verify_fit(r["title"], text)

        n_scored = n_crushed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for fut in as_completed({ex.submit(_one, r): r for r in todo}):
                try:
                    r, text, res = fut.result()
                except Exception as e:
                    print(f"    [!] verify error: {e}")
                    continue
                if res.score is None:
                    print(f"    [?] kept   {r['title'][:46]} - {res.reason}")
                    continue
                store.update_job_scores(conn, r["job_id"], res.as_columns())
                if text and len(text) > len(r.get("description") or ""):
                    conn.execute("UPDATE jobs SET description=? WHERE job_id=?",
                                 (text[:config.MAX_DESC_CHARS], r["job_id"]))
                    conn.commit()
                old = r.get("resume_fit_score")
                move = (f"{old:.2f} -> {res.score:.2f}"
                        if isinstance(old, float) else f"?    -> {res.score:.2f}")
                flag = "  [DEMOTED]" if isinstance(old, float) and \
                    res.score < old - 0.15 else ""
                print(f"    {move}  {r['title'][:44]} ({r['company_name']})"
                      f"{flag}")
                n_done += 1
                n_scored += 1
                if isinstance(old, float) and res.score < old - 0.25:
                    n_crushed += 1
        # Tripwire: the two passes disagreeing WHOLESALE is a calibration or
        # parsing defect, not information — the first --verify-top run gated
        # every finalist to ~0.1x via a gates-parsing bug and silently
        # replaced the ranking with the unverified stratum below it. Stop
        # instead of compounding across rounds.
        if n_scored >= 5 and n_crushed / n_scored >= 0.8:
            print(f"\n  [!] TRIPWIRE: {n_crushed}/{n_scored} verified rows "
                  f"dropped by >0.25 this round. The deep pass is disagreeing "
                  f"with the screen wholesale — that pattern means a prompt/"
                  f"parsing defect, not 30 bad jobs. Halting further rounds; "
                  f"inspect fit_gates on the demoted rows before trusting "
                  f"this ranking.")
            break
    if own_conn:
        conn.close()
    return n_done


def verify_top_cli(top_n=15, max_workers=4):
    """Standalone `--verify-top N`: deep-verify the current top N in the
    store (no crawl), then rewrite the digest and print the corrected top."""
    n = verify_top(top_n=top_n, max_workers=max_workers)
    conn = store.connect()
    ranked = store.ranked_jobs(conn, track=TRACK, location_re=company_fetch.NC_RE,
                               rank_by="fit", allow_geo_modes={"remote"},
                               min_mission=MIN_MISSION_FOR_RANKING)
    write_digest(ranked, pipeline=store.get_pipeline(conn))
    print(f"\n  {n} job(s) deep-verified; corrected top {min(top_n, len(ranked))}:")
    for j in ranked[:top_n]:
        fit = j["resume_fit_score"]
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        print(f"  fit={fs} [{j.get('geo_mode','?')}] {(j['title'] or '')[:50]}"
              f"  -  {j['company_name']}")
    conn.close()
    return n


def sync_status_all(top_n=15):
    """Status-only reconciliation: re-fetch every active company's board
    (same scoping as run() — NC unless neural-tagged), reconcile open/closed
    via sync_job_statuses, and rewrite today's digest from the corrected
    ranking. NO scoring, no Claude API — the cheap recovery pass for when
    statuses have drifted (e.g. after a matching-logic fix) without paying
    for a full crawl's scoring phase."""
    conn = store.connect()
    companies = store.get_companies(conn, active_only=True)
    print(f"  reconciling statuses across {len(companies)} active compan(ies)...")
    sources = [(c["name"], c["ats"] or "?",
                (lambda cc=c: company_fetch.fetch_company(
                    cc, None if _whole_board(cc) else company_fetch.NC_RE)))
               for c in companies]
    fetched = fetch_all(sources)
    n_closed = n_reopened = n_boards = 0
    for c, (jobs, err) in zip(companies, fetched):
        if err is not None or not jobs or not c.get("id"):
            continue
        n_re, n_cl = store.sync_job_statuses(conn, c["id"], jobs, track=TRACK)
        n_boards += 1
        n_closed += n_cl
        n_reopened += n_re
        if n_cl or n_re:
            print(f"  {c['name'][:34]:34} {len(jobs):3} listed -> "
                  f"{n_cl:2} closed, {n_re:2} reopened")
    ranked = store.ranked_jobs(conn, track=TRACK, location_re=company_fetch.NC_RE,
                               rank_by="fit", allow_geo_modes={"remote"},
                               min_mission=MIN_MISSION_FOR_RANKING)
    write_digest(ranked, pipeline=store.get_pipeline(conn))
    print(f"\n  {n_boards} board(s) reconciled: {n_closed} closed, "
          f"{n_reopened} reopened; {len(ranked)} open job(s) in ranking.")
    for j in ranked[:top_n]:
        fit = j["resume_fit_score"]
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        print(f"  fit={fs} [{j.get('geo_mode','?')}] {(j['title'] or '')[:52]}"
              f"  -  {j['company_name']}")
    conn.close()
    return (n_closed, n_reopened)


def check_closed_jobs(max_workers=8, limit=None, stale_days=2):
    """Probe the detail URLs of OPEN rows that no successful board fetch has
    vouched for in `stale_days` (last_seen is refreshed every time a board
    snapshot matches a row) and close the ones that are positively dead
    (HTTP 404/410, an ATS "no longer accepting" notice, a past JSON-LD
    validThrough, a Workday CXS miss). Indeterminate probes (bot-gated
    aggregator hosts like LinkedIn, JS-only pages) leave the row untouched.

    The staleness scope covers everything the crawl's board-diff can't
    settle, whatever the reason: orphan rows, inactive companies, boards
    with no fetcher, AND active companies whose board has died or moved
    (e.g. a renamed Greenhouse slug 404ing while its stored jobs live on).
    Rows a healthy board vouched for recently are skipped.
    """
    conn = store.connect()
    cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT job_id, title, company_name, url FROM jobs "
        "WHERE COALESCE(status,'open') != 'closed' "
        "AND COALESCE(last_seen, first_seen, '') < ? "
        "ORDER BY company_name", (cutoff,)).fetchall()]
    if limit:
        rows = rows[:int(limit)]
    print(f"  probing {len(rows)} open job(s) not board-verified in "
          f"{stale_days}+ day(s)...")

    n_closed = n_live = n_unknown = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(company_fetch.probe_job_open, r["url"]): r
                for r in rows}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                is_open, reason = fut.result()
            except Exception as e:
                is_open, reason = None, f"probe error: {e}"
            label = f"{(r['company_name'] or '?')[:24]:24} {(r['title'] or '')[:38]:38}"
            if is_open is False:
                store.set_job_status(conn, r["job_id"], "closed")
                n_closed += 1
                print(f"    [closed] {label} {reason}")
            elif is_open:
                n_live += 1
            else:
                n_unknown += 1
    conn.close()
    print(f"  {n_closed} closed, {n_live} confirmed live, "
          f"{n_unknown} unverifiable (left open) of {len(rows)} probed.")
    return n_closed


def _hydrate_missing_descriptions(conn, jobs):
    """Backfill empty descriptions on jobs linked to a company with a
    resolvable board, batched so each board is fetched once no matter how
    many of its jobs need hydrating (e.g. several LinkedIn-captured
    postings from the same employer in one ingest run)."""
    need = [j for j in jobs if j.get("_company_id") and not (j.get("description") or "").strip()]
    if not need:
        return
    by_company = {}
    for j in need:
        by_company.setdefault(j["_company_id"], []).append(j)
    for cid, js in by_company.items():
        company = store.get_company(conn, cid)
        if not company or not company.get("ats"):
            continue
        try:
            board = company_fetch.fetch_company(company, loc_re=None)
        except Exception as e:
            print(f"    [!] board hydrate fetch failed for {company['name']}: {e}")
            continue
        by_title = {(b.get("title") or "").strip().lower(): b for b in board}
        n_hydrated = 0
        for j in js:
            match = by_title.get((j.get("title") or "").strip().lower())
            if match is None:
                continue
            company_fetch.hydrate_description(match)
            if match.get("description"):
                j["description"] = match["description"]
                j["url"] = j.get("url") or match.get("url")
                n_hydrated += 1
        if n_hydrated:
            print(f"    hydrated {n_hydrated}/{len(js)} description(s) from "
                  f"{company['name']}'s {company['ats']} board")


def ingest_external_jobs(jobs, source="indeed", max_workers=6, curated=False):
    """
    Ingest external job dicts into the jobs table with resume-fit scores.
    Each dict: {id?, title, company, url, location, description?}. Applies the
    same exclude + technical-title gate as the crawl. For agent-mediated
    sources (e.g. the Indeed MCP) that the standalone crawler can't poll —
    the caller supplies the fetched jobs.

    `curated=True` (manual --add): the caller hand-picked these jobs, so the
    exclude + technical-title guesswork is skipped — but the NC location gate
    still applies (the local track is Triangle-scoped by definition).
    """
    import hashlib
    resume = resume_text()
    conn = store.connect()
    kept, n_nonlocal = [], 0
    for j in jobs:
        if not j.get("id"):
            key = (j.get("url") or "") + (j.get("title") or "") + (j.get("company") or "")
            j["id"] = f"{source}_{hashlib.md5(key.encode()).hexdigest()[:12]}"
        # Local-tech is a Triangle/NC track: gate ingested jobs on the same
        # NC location filter the live crawl applies inside its fetchers, with
        # one relaxation — a posting from a WATCHED company still passes when
        # it's explicitly remote (matching ranked_jobs' watch-scoped remote
        # admission; the machine-set neural tag no longer earns the
        # exception). Without the NC/remote gate at all, agent-sourced
        # boards (LinkedIn/Indeed) would inject any CA/TX posting into the
        # local top-10. Enforced even for curated adds.
        loc = j.get("location", "") or ""
        company_id = store.company_id_by_name(conn, j.get("company"))
        company_row = store.get_company(conn, company_id) if company_id else None
        is_local = bool(company_fetch.NC_RE.search(loc))
        is_remote_watched = (_is_watched(company_row)
                             and geo_mode(loc, j.get("description", "")) == "remote")
        if not (is_local or is_remote_watched):
            n_nonlocal += 1
            continue
        if not curated:
            if exclude_reason(j.get("title", ""), j.get("description", "")):
                continue
            if not is_technical_role(j.get("title", "")):
                continue
        if store.job_exists(conn, j["id"]):
            # Already stored — but the source just showed it live, so reopen
            # a closed row and reset its grace clock (no re-score).
            store.touch_job(conn, j["id"])
        else:
            # Resolve the company link on the MAIN thread — SQLite connections
            # can't cross into the scoring pool below. Link to a vetted company
            # row when the name matches, so the job inherits its mission score
            # (else it stays an orphan and sinks under the combined ranking).
            j["_company_id"] = company_id
            kept.append(j)

    _hydrate_missing_descriptions(conn, kept)

    def _score(j):
        res = score_resume_fit(resume, j["title"], j.get("description", ""))
        return {"job_id": j["id"], "company_id": j.get("_company_id"),
                "company_name": j.get("company"),
                "title": j.get("title"), "url": j.get("url"), "location": j.get("location"),
                "track": TRACK,
                "geo_mode": geo_mode(j.get("location", ""), j.get("description", "")) or "onsite",
                "description": (j.get("description", "") or "")[:config.MAX_DESC_CHARS],
                "posted_at": j.get("posted_at"),
                "status": "open",
                **res.as_columns()}

    scored = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_score, j): j for j in kept}):
            try:
                store.upsert_job(conn, fut.result())
                scored += 1
            except Exception as e:
                print(f"    [!] ingest error: {e}")
    print(f"  ingested {scored} new {source} job(s) ({len(kept)} kept, "
          f"{n_nonlocal} non-NC dropped, {len(jobs)} raw)")
    return scored


def add_manual_job(url, title, company, location, description="",
                   pull_board=True, max_workers=6):
    """Add ONE hand-picked job, register/resolve its COMPANY, and — if that
    company's board resolves — pull its OTHER NC jobs too.

    The single job is curated (exclude/technical gates skipped, you chose it)
    but still NC-gated: a non-NC posting is dropped, because the local track
    is Triangle-scoped by definition. For bot-gated giants (Meta, Google) the
    board won't resolve, so only the one job lands and the company is recorded
    for a later retry. Returns a summary dict.
    """
    from ..discovery.local_sourcing import resolve_company_board, _sample_titles
    from ..claude import score_company_mission, ACTIVE_MISSION_TIERS

    name = (company or "").strip()
    if not name or not (url or title):
        print("  [!] --add needs --company plus at least --url or --title.")
        return {}

    # 1) Company: resolve a board if we don't already have one for it, so the
    #    job links to a real company row (and its board can be pulled below).
    conn = store.connect()
    existing = next((c for c in store.get_companies(conn, active_only=False)
                     if (c["name"] or "").lower() == name.lower()), None)
    board = None
    if not existing or not existing.get("ats"):
        print(f"  resolving board for {name!r}...")
        board = resolve_company_board(name)
    if board:
        is_wd = board["ats"] == "workday"
        slug = board["slug"]
        titles = _sample_titles(board)
        tier, score, reason = score_company_mission(
            name, " | ".join(t for t in titles if t))
        active = 1 if (tier in ACTIVE_MISSION_TIERS
                       or config.is_multi_division(name) or tier is None) else 0
        store.upsert_company(conn, {
            "name": name, "ats": board["ats"],
            "slug": None if is_wd else slug,
            "wd_tenant": slug[0] if is_wd else None,
            "wd_pod": slug[1] if is_wd else None,
            "wd_site": slug[2] if is_wd else None,
            "careers_url": board.get("careers_url"),
            "nc_job_count": board["nc"], "total_job_count": board["count"],
            "mission_tier": tier, "mission_score": score, "mission_reason": reason,
            "tags": "nc_local" if board["nc"] else None,
            "source": "manual_add", "active": active,
        })
        print(f"    board resolved: {board['ats']} nc={board['nc']} "
              f"mission={tier} ({score if score is not None else 'n/a'})")
    elif not existing:
        store.upsert_company(conn, {"name": name, "active": 0, "source": "manual_add",
                                    "notes": f"manual add from {url}"})
        print("    company recorded (board unresolved — gated / unknown ATS)")
    else:
        print(f"    company already in roster (ats={existing.get('ats')})")
    conn.close()

    # 2) The single job — curated (skip exclude/technical), NC gate still on.
    print(f"  adding job: {title!r} @ {name} [{location}]")
    n_job = ingest_external_jobs(
        [{"title": title, "company": name, "url": url,
          "location": location or "", "description": description or ""}],
        source="manual", curated=True)

    # 3) The company's OTHER jobs — crawl its board whenever it has one
    #    (freshly resolved OR already in the roster), unless --no-board.
    n_other = 0
    conn = store.connect()
    row = next((c for c in store.get_companies(conn, active_only=False)
                if (c["name"] or "").lower() == name.lower()), None)
    has_board = bool(row and row.get("ats"))
    if pull_board and has_board:
        _, _, n_other = crawl_company(conn, resume_text(), row, max_workers)
        print(f"    pulled {n_other} other NC job(s) from {name}'s board")
    conn.close()

    status = "active board" if has_board else "recorded (board unresolved)"
    print(f"\n  DONE: +{n_job} job, +{n_other} from board; company '{name}' - {status}.")
    return {"job_added": n_job, "other_jobs": n_other,
            "board": has_board, "company": name}


def write_digest(ranked, watch_hits=None, pipeline=None):
    config.REPORT_DIR.mkdir(exist_ok=True)
    path = config.REPORT_DIR / f"local_tech_{datetime.now():%Y-%m-%d}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {TAG} Job Digest — {datetime.now():%Y-%m-%d}\n\n")
        if pipeline:
            f.write("## Your pipeline\n\n")
            f.write("Managed with `crawler.py --mark DISPOSITION JOB` "
                    "(saved stays in the ranking; the rest live here).\n\n")
            f.write("| Disposition | When | Company | Title | Posting | Note |\n")
            f.write("|---|---|---|---|---|---|\n")
            for p in pipeline:
                state = "CLOSED" if (p.get("status") == "closed") else "open"
                f.write(f"| {p.get('disposition')} | {(p.get('disposition_at') or '')[:10]} "
                        f"| {p.get('company_name')} | [{p.get('title')}]({p.get('url')}) "
                        f"| {state} | {p.get('disposition_note') or ''} |\n")
            f.write("\n")
        if watch_hits:
            f.write("## Watched companies — new postings this run\n\n")
            f.write("Flagged regardless of rank or geography "
                    "(`crawler.py --watch NAME` manages the list).\n\n")
            for c, j, in_pipeline in watch_hits:
                note = "scored" if in_pipeline else "listed only, outside local scope"
                f.write(f"- **{c['name']}** — [{j.get('title')}]({j.get('url')}) "
                        f"— {j.get('location') or '?'} *({note})*\n")
            f.write("\n")
        f.write(f"**{len(ranked)} open job(s)** (closed, dismissed, and in-pipeline "
                f"postings excluded), ranked by resume fit "
                f"(combined = sqrt(resume-fit x company-mission), shown for reference). "
                f"Age is days since the board's posting date "
                f"(NEW = first seen today, ! = 45d+ stale, ? = date unknown).\n\n")
        f.write("| Fit | Combined | Age | Company | Mission | Title | Location | Why |\n")
        f.write("|----:|---------:|----:|---------|---------|-------|----------|-----|\n")
        today = datetime.now().strftime("%Y-%m-%d")
        for j in ranked:
            fit = j["resume_fit_score"]
            fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
            comb = j.get("combined_score")
            cs = f"{comb:.2f}" if isinstance(comb, float) else "n/a"
            f.write(f"| {fs} | {cs} | {_age_tag(j, today)} | {j['company_name']} "
                    f"| {j.get('mission_tier') or '?'} "
                    f"| [{j['title']}]({j['url']}) | {j['location']} | {j.get('fit_reason','')} |\n")
    print(f"  digest -> {path}")
    return path
