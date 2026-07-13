"""
Local (Triangle / NC) company sourcing for the LOCAL-TECH crawler.

Replaces the BCI-heavy hand-picked company list with a discovery pass over
health / bio / science / tech employers with a Triangle-NC presence:

  1. Gather candidate company NAMES from several free sources:
       - a curated seed of established Triangle/NC health-bio-science + tech
         employers,
       - the static entries on the RTP.org directory,
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

import config

from ..http import HEADERS
from .probes import (probe_greenhouse, probe_lever, probe_ashby, probe_workday,
                     _DOMAIN_STOPWORDS, _name_domain_tokens)


# --------------------------------------------------------------------------- #
#  Candidate NAMES                                                             #
# --------------------------------------------------------------------------- #

# Curated seed: established Triangle/NC health-bio-science + science/health-tech
# employers likely to run a public ATS. Generous by design — the probe filters.
SEED_COMPANIES = [
    # CROs / clinical data / health analytics
    "IQVIA", "Labcorp", "Syneos Health", "Advarra", "Q2 Solutions",
    "Clinipace", "Caidya", "Emmes", "PRA Health Sciences", "Parexel",
    # pharma / biopharma / cell & gene
    "United Therapeutics", "Biogen", "Grifols", "Seqirus", "CSL",
    "Fujifilm Diosynth Biotechnologies", "KBI Biopharma", "Precision BioSciences",
    "Humacyte", "Chimerix", "G1 Therapeutics", "Fennec Pharmaceuticals",
    "Asklepios BioPharmaceutical", "AskBio", "StrideBio", "Locus Biosciences",
    "Bioventus", "Novartis Gene Therapies", "Amgen", "Pfizer",
    # diagnostics / genomics / tools
    "GRAIL", "Metabolon", "BioAgilytix", "Galaxy Diagnostics", "BioMedomics",
    "Genedata", "Sequenom", "QIAGEN", "bioMerieux", "Avazyme",
    # medical devices / health hardware
    "410 Medical", "ABK Biomedical", "Bioptimus", "Teleflex", "Nuvectra",
    # health-tech / science software / analytics
    "SAS", "Relias", "nCino", "Pendo", "Red Hat", "Willow Tree",
    "Definitive Healthcare", "Clarify Health", "Vidant", "First Health",
    # industrial bio / materials / agtech science
    "Novonesis", "Novozymes", "BASF", "Bayer Crop Science", "Syngenta",
    "Boragen", "AgBiome", "Precision BioSciences",
]

# Big NC health-bio employers known to run Workday. Only these get the slow
# Workday careers-page fallback; everyone else is probed via the fast
# Greenhouse/Lever/Ashby JSON APIs, so a 200+ name pool stays quick.
MAJORS_WORKDAY = [
    "IQVIA", "Amgen", "Biogen", "Novartis Gene Therapies", "Merck",
    "Eli Lilly", "Novo Nordisk", "Catalent", "Charles River Laboratories",
    "Certara", "Fortrea", "Labcorp", "Syneos Health", "United Therapeutics",
    "Grifols", "CSL Seqirus", "Fujifilm Diosynth Biotechnologies", "Metabolon",
    "Precision BioSciences", "Humacyte", "Chimerix", "Bioventus",
    "Thermo Fisher Scientific", "PPD", "Parexel", "ICON", "RTI International",
    "Novonesis", "Novozymes", "Bayer", "BASF", "Syngenta", "Corteva",
    "Alltech", "Almac", "Asymchem", "bioMerieux", "Antech Diagnostics",
    "Sanofi", "AskBio", "KBI Biopharma", "Pfizer", "AstraZeneca", "GSK",
]

_MAJORS_KEYS = {re.sub(r"[^a-z0-9]", "", m.lower()) for m in MAJORS_WORKDAY}

# Known bad name→board matches to drop from discovery (normalized names).
NAME_BLOCKLIST = {"q2solutions", "q2labsolutions"}  # slug q2ebanking = Q2 Holdings (fintech)


# From Built In "Biotech companies in RTP" (fetched 2026-07).
BUILTIN_RTP = [
    "Fennec Pharmaceuticals", "G1 Therapeutics", "Galaxy Diagnostics",
    "MAA Laboratories", "BioMedomics", "Avazyme", "Merakris Therapeutics",
    "GRAIL", "Asklepios Biopharmaceutical", "NALA Membranes", "Inanovate",
    "Verinetics", "Click Therapeutics", "Dignify Therapeutics",
    "Lindy Biosciences", "Ascent Bio-Nano Technologies",
]


def scrape_rtp_static(timeout=20):
    """
    Best-effort: pull company names from the server-rendered part of the
    RTP.org directory. (The full list loads via FacetWP AJAX which needs
    template config we don't replicate; the static slugs are a free bonus.)
    """
    try:
        r = requests.get("https://www.rtp.org/directory-map/",
                         timeout=timeout, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] RTP directory scrape failed: {e}")
        return []
    slugs = sorted(set(re.findall(r'/company/([a-z0-9\-]+)/', r.text)))
    names = []
    for s in slugs:
        if s in ("research-triangle-park",):
            continue
        names.append(s.replace("-", " ").title())
    return names


def gather_names(extra=None):
    """Union of all name sources, de-duplicated case-insensitively."""
    names, seen = [], set()
    for src in (SEED_COMPANIES, MAJORS_WORKDAY, BUILTIN_RTP, scrape_rtp_static(), extra or []):
        for n in src:
            k = re.sub(r"[^a-z0-9]", "", n.lower())
            if k and k not in seen:
                seen.add(k)
                names.append(n.strip())
    return names


# --------------------------------------------------------------------------- #
#  Slug candidates + probing                                                   #
# --------------------------------------------------------------------------- #

def _slug_candidates(name):
    """
    ATS-slug guesses for a company name, in priority order. Uses joined,
    hyphenated, and suffix-stripped-joined forms only — deliberately NOT the
    bare first word ("eli", "novo", "charles"), which collides with unrelated
    boards and shadows the real employer.
    """
    clean = re.sub(r"\s*\([^)]*\)", "", name).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", clean) if w]
    if not words:
        return []
    joined = "".join(words)                                  # unitedtherapeutics
    hyphen = "-".join(words)                                 # united-therapeutics
    stripped = "".join(w for w in words if w not in _DOMAIN_STOPWORDS) or joined
    out, seen = [], set()
    for c in (joined, hyphen, stripped):
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


from ..nc import is_nc as _has_nc  # single source of truth for NC locality


def _nc_count_greenhouse(slug):
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
                         timeout=15, headers=HEADERS)
        return sum(1 for j in r.json().get("jobs", [])
                   if _has_nc(j.get("location", {}).get("name", "")))
    except Exception:
        return 0


def _nc_count_lever(slug):
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         timeout=15, headers=HEADERS)
        return sum(1 for j in r.json()
                   if _has_nc(j.get("categories", {}).get("location", "")))
    except Exception:
        return 0


def _nc_count_ashby(slug):
    try:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         timeout=15, headers=HEADERS)
        return sum(1 for j in r.json().get("jobPostings", [])
                   if _has_nc(j.get("location", "")))
    except Exception:
        return 0


def _nc_count_workday(tenant, pod, site):
    """Query the Workday CXS search for NC postings (searchText hits location)."""
    api = f"https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = requests.post(api, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                     "searchText": "North Carolina"},
                          timeout=15, headers={**HEADERS, "Content-Type": "application/json"})
        return int(r.json().get("total", 0) or 0)
    except Exception:
        return 0


def probe_company(name, try_workday=True):
    """
    Probe Greenhouse/Lever/Ashby (fast) then — only if ``try_workday`` —
    Workday (slow careers-page fallback), then VERIFY the board has NC-area
    jobs (kills false-positive slug collisions and enforces local relevance).
    Returns a hit dict with an ``nc`` count, or None.
    """
    hit = None
    for slug in _slug_candidates(name):
        for ats, fn, nc_fn in (("greenhouse", probe_greenhouse, _nc_count_greenhouse),
                               ("lever", probe_lever, _nc_count_lever),
                               ("ashby", probe_ashby, _nc_count_ashby)):
            ok, count = fn(slug)
            if ok:
                hit = {"name": name, "ats": ats, "slug": slug,
                       "count": count, "nc": nc_fn(slug)}
                break
        if hit:
            break
    if not hit and try_workday:
        wd = probe_workday(name)
        if wd and wd.get("validated"):
            hit = {"name": name, "ats": "workday",
                   "slug": (wd["tenant"], wd["wd_pod"], wd["site"]),
                   "count": wd["count"],
                   "nc": _nc_count_workday(wd["tenant"], wd["wd_pod"], wd["site"])}
    return hit


def discover_local(extra_names=None, max_workers=12, js_majors=True, sniff=True):
    """
    Gather names + probe each. Returns (confirmed, checked) where confirmed
    is a list of NC-local hit dicts. ``js_majors`` runs a headless-browser
    Workday probe for big employers the static probe missed.
    """
    names = gather_names(extra_names)
    n_wd = sum(1 for n in names if re.sub(r"[^a-z0-9]", "", n.lower()) in _MAJORS_KEYS)
    print(f"  probing {len(names)} candidate compan(ies) for live ATS boards "
          f"({n_wd} with Workday fallback)...")
    hits = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(probe_company, n,
                          re.sub(r"[^a-z0-9]", "", n.lower()) in _MAJORS_KEYS): n
                for n in names}
        for fut in as_completed(futs):
            hit = fut.result()
            if hit:
                hits.append(hit)

    # JS-Workday pass: big employers often have React/SPA careers pages whose
    # myworkdayjobs.com link only appears after JS runs, so the static probe
    # misses them. Re-probe MAJORS that got no board using one headless browser.
    if js_majors:
        # Only an NC>0 board counts as "found" — a junk 0-NC slug collision
        # must not block the JS fallback for the real employer.
        found = {re.sub(r"[^a-z0-9]", "", h["name"].lower())
                 for h in hits if h["nc"] > 0}
        missed = [m for m in MAJORS_WORKDAY
                  if re.sub(r"[^a-z0-9]", "", m.lower()) not in found]
        if missed:
            from .probes import WorkdayJsProbe
            print(f"  JS-probing {len(missed)} major(s) with no static board...")
            with WorkdayJsProbe() as js:
                for m in missed:
                    wd = js.probe(m)
                    if wd and wd.get("validated"):
                        nc = _nc_count_workday(wd["tenant"], wd["wd_pod"], wd["site"])
                        hits.append({"name": m, "ats": "workday",
                                     "slug": (wd["tenant"], wd["wd_pod"], wd["site"]),
                                     "count": wd["count"], "nc": nc})
                        print(f"    [JS-OK] {m:30} {wd['tenant']}/{wd['wd_pod']}/"
                              f"{wd['site']}  nc={nc}/{wd['count']}")

    # Sniffer pass: for names still without a real (NC>0) board, fetch their
    # careers page and detect the embedded ATS + exact slug (covers Greenhouse/
    # Lever/Ashby/Workday/SmartRecruiters/iCIMS/SuccessFactors and finds slugs
    # the name-guesser can't). This is the main recall lever over the directory.
    if sniff:
        from .sniffer import sniff_ats
        from ..fetchers import company as company_fetch
        have = {re.sub(r"[^a-z0-9]", "", h["name"].lower()) for h in hits if h["nc"] > 0}
        todo = [n for n in names if re.sub(r"[^a-z0-9]", "", n.lower()) not in have]
        print(f"  sniffing careers pages for {len(todo)} name(s) without a board...")

        def _sniff_one(n):
            s = sniff_ats(n)
            if not s:
                return None
            ats = s["ats"]
            if ats == "workday":
                t, p, site = s["triple"]
                comp = {"ats": "workday", "wd_tenant": t, "wd_pod": p, "wd_site": site}
                slug = (t, p, site)
            else:
                comp = {"ats": ats, "slug": s.get("slug"), "careers_url": s.get("careers_url")}
                slug = s.get("slug")
            try:
                jobs = company_fetch.fetch_company_nc(comp)
            except Exception:
                jobs = []
            nc = len(jobs)
            return {"name": n, "ats": ats, "slug": slug, "count": nc, "nc": nc,
                    "careers_url": s.get("careers_url")}

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for fut in as_completed({ex.submit(_sniff_one, n): n for n in todo}):
                h = fut.result()
                if h and h["nc"] > 0:
                    hits.append(h)
                    print(f"    [SNIFF] {h['name']:28} {h['ats']:14} "
                          f"{h['slug']!s:26} nc={h['nc']}")

    # Drop known bad name→board matches.
    hits = [h for h in hits
            if re.sub(r"[^a-z0-9]", "", h["name"].lower()) not in NAME_BLOCKLIST]

    # De-dup by resolved board (same slug/triple reached via different names,
    # e.g. "BioAgilytix" vs "BioAgilytix Labs"); keep the shorter name.
    by_board = {}
    for h in hits:
        key = (h["ats"], str(h["slug"]))
        if key not in by_board or len(h["name"]) < len(by_board[key]["name"]):
            by_board[key] = h
    hits = list(by_board.values())

    # Split on the NC-locality check: nc>0 is confirmed-local; nc==0 is either
    # a false-positive slug collision or a non-NC employer — dropped, but shown.
    confirmed = sorted([h for h in hits if h["nc"] > 0],
                       key=lambda h: h["nc"], reverse=True)
    dropped = sorted([h for h in hits if h["nc"] == 0],
                     key=lambda h: h["name"].lower())
    print(f"\n  live boards: {len(hits)}  |  NC-local confirmed: {len(confirmed)}  "
          f"|  dropped (no NC jobs): {len(dropped)}")
    print("\n  --- NC-LOCAL CONFIRMED (nc jobs / total) ---")
    for h in confirmed:
        print(f"    [OK]   {h['name']:32} {h['ats']:10} {h['slug']!s:34} "
              f"{h['nc']}/{h['count']}")
    print("\n  --- DROPPED: live board but no NC jobs (likely wrong slug or non-local) ---")
    for h in dropped:
        print(f"    [drop] {h['name']:32} {h['ats']:10} {h['slug']!s:34} "
              f"0/{h['count']}")
    return confirmed, names


# --------------------------------------------------------------------------- #
#  Config-ready output                                                         #
# --------------------------------------------------------------------------- #

from ..nc import NC_HQ_RE as _NC_HQ_RE  # "<Triangle city>, NC" HQ/office signal


def nc_hq_signal(name, careers_url="", board_jobs=None):
    """
    True if the company has a verifiable NC presence — used to TRACK local
    companies that currently have no NC openings. Checks the board's job
    locations first (cheap), then the company site/careers/contact pages.
    """
    if board_jobs:
        for j in board_jobs:
            if _NC_HQ_RE.search(j.get("location", "") or ""):
                return True
    urls = []
    if careers_url:
        urls.append(careers_url)
    for tok in _name_domain_tokens(name):
        urls += [f"https://www.{tok}.com/contact", f"https://www.{tok}.com/about",
                 f"https://www.{tok}.com/locations", f"https://www.{tok}.com/",
                 f"https://www.{tok}.com/company"]
    seen = set()
    for u in urls[:8]:
        if u in seen:
            continue
        seen.add(u)
        try:
            r = requests.get(u, timeout=12, headers=HEADERS, allow_redirects=True)
            if r.status_code == 200 and _NC_HQ_RE.search(r.text):
                return True
        except Exception:
            continue
    return False


def _sample_titles(hit, n=6):
    """Fetch a few job titles from a confirmed board for mission context."""
    ats, slug = hit["ats"], hit["slug"]
    try:
        if ats == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
                             timeout=15, headers=HEADERS)
            return [j.get("title", "") for j in r.json().get("jobs", [])[:n]]
        if ats == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                             timeout=15, headers=HEADERS)
            return [j.get("text", "") for j in r.json()[:n]]
        if ats == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                             timeout=15, headers=HEADERS)
            return [j.get("title", "") for j in r.json().get("jobPostings", [])[:n]]
        if ats == "workday":
            t, p, s = slug
            api = f"https://{t}.wd{p}.myworkdayjobs.com/wday/cxs/{t}/{s}/jobs"
            r = requests.post(api, json={"appliedFacets": {}, "limit": n, "offset": 0,
                                         "searchText": "North Carolina"},
                              timeout=15, headers={**HEADERS, "Content-Type": "application/json"})
            return [j.get("title", "") for j in r.json().get("jobPostings", [])[:n]]
    except Exception:
        return []
    return []


def populate_companies(extra_names=None, include_missions=None):
    """
    Full sourcing pass → SQL store: discover NC-local boards, score each
    company's MISSION once (cached), and upsert into the `companies` table.
    Companies whose mission is `other` are stored but marked inactive (so
    they aren't crawled) unless include_missions says otherwise.

    Returns the list of company dicts written (active ones first).
    """
    from ..store import connect, upsert_company
    from ..claude import score_company_mission, ACTIVE_MISSION_TIERS

    if include_missions is None:
        include_missions = ACTIVE_MISSION_TIERS
    confirmed, _ = discover_local(extra_names)
    conn = connect()
    written = []
    print(f"\n  scoring mission for {len(confirmed)} NC-local compan(ies)...")
    for h in confirmed:
        titles = _sample_titles(h)
        tier, score, reason = score_company_mission(h["name"], " | ".join(t for t in titles if t))
        # Multi-division conglomerates stay active despite an "other" tier —
        # their healthcare subdivisions are filtered in at crawl time.
        active = 1 if (tier in include_missions or tier is None
                       or config.is_multi_division(h["name"])) else 0
        row = {
            "name": h["name"], "ats": h["ats"],
            "slug": h["slug"] if h["ats"] != "workday" else None,
            "wd_tenant": h["slug"][0] if h["ats"] == "workday" else None,
            "wd_pod":    h["slug"][1] if h["ats"] == "workday" else None,
            "wd_site":   h["slug"][2] if h["ats"] == "workday" else None,
            "careers_url": h.get("careers_url"),
            "nc_job_count": h["nc"], "total_job_count": h["count"],
            "mission_tier": tier, "mission_score": score, "mission_reason": reason,
            "tags": "nc_local", "source": "local_sourcing", "active": active,
            "last_probed": datetime.now().isoformat(),
        }
        upsert_company(conn, row)
        written.append({**row, "active": active})
        flag = "active" if active else "INACTIVE(other)"
        ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
        print(f"    {h['name']:30} {str(tier):20} {ss}  [{flag}]  ({reason})")
    conn.close()
    return written


def add_board(name, url):
    """Register a board the user already knows — no guessing. `url` may be
    the ATS board itself (myworkdayjobs / greenhouse / lever / ...) or the
    company's careers page; coordinates are detected, the board NC-counted,
    mission-scored, and activated.

        python discover.py --add-board "NC DHHS" https://nc.wd108.myworkdayjobs.com/NC_Careers
    """
    from ..claude import score_company_mission
    from ..fetchers import company as company_fetch
    from ..store import connect, upsert_company
    from .sniffer import _detect, _pack, sniff_ats

    hit = _detect("", url)
    if hit and hit[0] in ("fetchable", "semi"):
        found = _pack(hit[1], hit[2], url)
    else:
        found = sniff_ats(name, careers_url=url)
    if not found:
        print(f"  [!] No ATS coordinates found at/near {url}")
        return None

    ats = found["ats"]
    if ats == "workday":
        t, pd, site = found["triple"]
        comp = {"ats": "workday", "wd_tenant": t, "wd_pod": pd, "wd_site": site}
        slug = (t, pd, site)
    else:
        comp = {"ats": ats, "slug": found.get("slug"),
                "careers_url": found.get("careers_url") or url}
        slug = found.get("slug") or url
    try:
        nc = len(company_fetch.fetch_company(comp, company_fetch.NC_RE))
    except Exception:
        nc = 0

    sample_hit = {"ats": ats, "slug": slug}
    titles = _sample_titles(sample_hit)
    tier, score, reason = score_company_mission(name, " | ".join(t for t in titles if t))

    conn = connect()
    is_wd = ats == "workday"
    upsert_company(conn, {
        "name": name, "ats": ats,
        "slug": None if is_wd else (found.get("slug") or None),
        "wd_tenant": slug[0] if is_wd else None,
        "wd_pod":    slug[1] if is_wd else None,
        "wd_site":   slug[2] if is_wd else None,
        "careers_url": found.get("careers_url") or url,
        "nc_job_count": nc, "mission_tier": tier, "mission_score": score,
        "mission_reason": reason, "tags": "nc_local" if nc else None,
        "source": "manual", "active": 1,
    })
    conn.close()
    ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
    print(f"  [OK] {name}: {ats} {slug!s}  nc={nc}  mission={tier} ({ss})  ACTIVE")
    return found


# Job aggregators / company-directory sites: they rank highly for
# '"<name>" careers' but are never the employer's own ATS board, so sniffing
# them wastes fetch slots. Skipped when picking result URLs to resolve.
_AGGREGATOR_HOSTS = (
    "linkedin.com", "indeed.", "glassdoor.", "ziprecruiter.com", "simplyhired.com",
    "builtin.com", "rocketreach.co", "careerjet.", "monster.com", "dice.com",
    "lensa.com", "jobcase.com", "themuse.com", "wellfound.com", "levels.fyi",
    "trueup.io", "salary.com", "comparably.com", "talent.com", "unifygtm.com",
    "getro.com", "jooble.org", "adzuna.", "snagajob.com", "careers.tufts.edu",
    "google.com/search", "bing.com", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "crunchbase.com", "pitchbook.com", "zippia.com",
)


def _is_aggregator(url):
    return any(h in url.lower() for h in _AGGREGATOR_HOSTS)


# Generic words that don't distinguish a company's domain — excluded when
# matching a result host to a name, so "medicaljobs.com" doesn't match
# "Sampson Regional Medical Center" on the word "medical".
_GENERIC_NAME_WORDS = {
    "medical", "center", "centre", "health", "healthcare", "regional",
    "group", "services", "systems", "system", "technology", "technologies",
    "imaging", "solutions", "associates", "partners", "care", "clinic",
    "hospital", "labs", "laboratories", "company", "corporation", "global",
    "national", "american", "international", "the", "and", "inc", "llc",
}


def _host_matches_name(url, name):
    """True if the result's host plausibly belongs to the company itself
    (a distinctive name token appears in the host) — the guard that keeps a
    self-hosted 'custom' board from resolving to a third-party jobs site."""
    host = re.sub(r"^https?://", "", url.lower()).split("/", 1)[0].replace("www.", "")
    hostslug = re.sub(r"[^a-z0-9]", "", host)
    joined = re.sub(r"[^a-z0-9]", "", name.lower())
    tokens = {joined} | {w for w in re.findall(r"[a-z0-9]+", name.lower())
                         if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}
    return any(len(t) >= 4 and t in hostslug for t in tokens)


def _slug_matches_name(slug, name):
    """True if a web-searched ATS slug/tenant plausibly belongs to the
    company — guards against the dork surfacing an unrelated board (e.g.
    'Novamed' -> the 'nc' NC-government Workday tenant)."""
    s = slug[0] if isinstance(slug, tuple) else slug   # workday tenant, else slug
    s = re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    if len(s) < 3:
        return False
    tokens = {re.sub(r"[^a-z0-9]", "", name.lower())}
    tokens |= {w for w in re.findall(r"[a-z0-9]+", name.lower())
               if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}
    return any(len(t) >= 3 and (s in t or t in s) for t in tokens)


def _websearch_board(name, max_results=8):
    """Find a company's board via web search when domain-guessing fails
    (gov/org domains, acronyms, or product-named domains — e.g. 'Core Sound
    Imaging' -> corestudycast.com). Returns the sniff_ats result shape, or
    None.

    Two improvements over a plain '"<name>" careers' search, which is
    dominated by LinkedIn/Indeed and rarely surfaces the real board:
      1. an ATS-dork query first, so a direct Workday/Greenhouse/iCIMS board
         link surfaces in the results;
      2. aggregators are skipped and self-hosted *custom* boards accepted,
         not just JSON-API ATSes.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        return None
    import requests as _rq

    from ..http import HEADERS as _H
    from ..fetchers.company import custom_board_listing_url
    from .sniffer import _detect, _pack

    def _search(query):
        out = []
        try:
            with DDGS() as ddg:
                for r in ddg.text(query, max_results=max_results):
                    u = r.get("href") or r.get("url")
                    if u and not _is_aggregator(u):
                        out.append(u)
        except Exception:
            pass
        return out

    def _resolve(urls):
        # Pass 1: ATS coordinates already visible in a result URL
        # (myworkdayjobs.com / boards.greenhouse.io / *.icims.com links).
        # The slug must match the name — a bare board link from search has no
        # page context, so an unrelated board (nc.wd108 for "Novamed") is
        # otherwise indistinguishable from a real hit.
        for u in urls:
            hit = _detect("", u)
            if hit and hit[0] in ("fetchable", "semi") and _slug_matches_name(hit[2], name):
                return _pack(hit[1], hit[2], u)
        # Pass 2: fetch the top real (non-aggregator) results and sniff for
        # an embedded ATS or a self-hosted board with genuine job links.
        for u in urls[:5]:
            try:
                r = _rq.get(u, timeout=8, headers=_H, allow_redirects=True)
                if r.status_code != 200 or len(r.text) < 300:
                    continue
            except Exception:
                continue
            own = _host_matches_name(r.url, name)
            hit = _detect(r.text, r.url)
            # Trust an embedded ATS when its slug matches the name OR it was
            # embedded on the company's own careers page (own-domain link).
            if hit and hit[0] in ("fetchable", "semi") and (own or _slug_matches_name(hit[2], name)):
                return _pack(hit[1], hit[2], r.url)
            # Custom self-hosted board: only on the company's OWN domain —
            # otherwise a third-party jobs site with ≥3 listings
            # (healthecareers, dotmed, expertini, …) resolves as the board.
            if own:
                listing = custom_board_listing_url(r.url, r.text)
                if listing:
                    return {"ats": "custom", "careers_url": listing}
        return None

    # Dork for a direct ATS board first (cheap win, avoids the second query
    # when it lands); fall back to a general careers search only if it misses.
    ats_hint = ("myworkdayjobs OR greenhouse OR lever OR ashbyhq OR icims "
                "OR smartrecruiters OR bamboohr OR workday")
    seen = set()
    for query in (f'"{name}" jobs ({ats_hint})', f'"{name}" careers'):
        fresh = [u for u in _search(query) if u not in seen]
        seen.update(fresh)
        hit = _resolve(fresh)
        if hit:
            return hit
    return None


def score_missions(max_workers=6, rescore_all=False):
    """Backfill company mission scores: every active company with a board
    and no mission_tier (or all of them, with rescore_all) gets sampled
    titles + one score_company_mission call. Heals stores populated by
    --import-seeds (which deliberately imports without scoring) or by
    keyless/failed scoring passes."""
    from ..claude import score_company_mission
    from ..store import connect, get_companies, upsert_company
    from ..sources import store_slug

    conn = connect()
    cos = [c for c in get_companies(conn, active_only=True)
           if c.get("ats") and (rescore_all or not c.get("mission_tier"))]
    if not cos:
        print("  Nothing to score - every active company has a mission tier.")
        conn.close()
        return 0
    print(f"  mission-scoring {len(cos)} compan(ies)...")

    def _one(c):
        hit = {"ats": c["ats"],
               "slug": ((c.get("wd_tenant"), c.get("wd_pod"), c.get("wd_site"))
                        if c["ats"] == "workday" else c.get("slug"))}
        titles = _sample_titles(hit)
        return c, score_company_mission(c["name"], " | ".join(t for t in titles if t))

    n = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_one, c): c for c in cos}):
            try:
                c, (tier, score, reason) = fut.result()
            except Exception as e:
                print(f"    [!] {e}")
                continue
            if tier is None and score is None:
                continue          # scoring unavailable - leave the row alone
            # Off-mission companies are deactivated so the crawl skips them,
            # matching the new-company sourcing path (an `other` tier means
            # "not health/bio/science" — no reason to keep crawling it).
            update = {"name": c["name"], "mission_tier": tier,
                      "mission_score": score, "mission_reason": reason}
            if tier == "other" and not config.is_multi_division(c["name"]):
                update["active"] = 0
            upsert_company(conn, update)
            n += 1
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            flag = "  -> deactivated (off-mission)" if tier == "other" else ""
            print(f"    {c['name']:32} {str(tier):20} {ss}  ({reason}){flag}")
    conn.close()
    print(f"\n  {n} compan(ies) scored.")
    return n


def resolve_company_board(name):
    """Resolve a company NAME to a crawlable board: slug-probe (Greenhouse/
    Lever/Ashby/Workday) -> careers-page sniff -> web search. Returns a hit
    dict {name, ats, slug, count, nc, careers_url} with an NC job count, or
    None. Shared by --resolve-leads and the manual --add flow."""
    from ..fetchers import company as company_fetch
    from .sniffer import sniff_ats
    # Workday fallback ON: enterprise employers (Analog Devices, Cadence, ...)
    # overwhelmingly live on Workday, worth the careers-page scrape.
    hit = probe_company(name, try_workday=True)
    if hit:
        return hit
    s = sniff_ats(name) or _websearch_board(name)
    if not s:
        return None
    ats = s["ats"]
    if ats == "workday":
        t, p, site = s["triple"]
        comp = {"ats": "workday", "wd_tenant": t, "wd_pod": p, "wd_site": site}
        slug = (t, p, site)
    else:
        comp = {"ats": ats, "slug": s.get("slug"), "careers_url": s.get("careers_url")}
        slug = s.get("slug") or s.get("careers_url")
    try:
        nc = len(company_fetch.fetch_company_nc(comp))
    except Exception:
        nc = 0
    return {"name": name, "ats": ats, "slug": slug, "count": nc, "nc": nc,
            "careers_url": s.get("careers_url")}


def resolve_leads(max_workers=8):
    """Resolve company leads recorded by page capture (inactive rows with
    no ats) into crawlable boards: slug-probe + careers-page sniff each
    name, NC-verify, mission-score, activate the hits. The page-capture ->
    resolve-leads -> crawl loop is how manually browsed postings grow the
    company roster."""
    from ..claude import score_company_mission, ACTIVE_MISSION_TIERS
    from ..store import connect, get_companies as _store_companies, upsert_company

    conn = connect()
    leads = [c for c in _store_companies(conn, active_only=False)
             if c.get("source") == "page_capture" and not c.get("ats")]
    if not leads:
        print("  No unresolved page-capture leads in the store.")
        conn.close()
        return []
    print(f"  resolving {len(leads)} page-capture lead(s) "
          f"(slug probe -> careers-page sniff -> NC-verify)...")

    resolved = []
    # Hard cap on the whole pass: a lead whose domains blackhole (accept
    # then stall) must become a reported miss, not a hung command. The
    # command is idempotent — rerunning retries only unresolved leads.
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(resolve_company_board, c["name"]): c for c in leads}
    try:
        for fut in as_completed(futs, timeout=240):
            c, hit = futs[fut], fut.result()
            if not hit:
                print(f"    [miss] {c['name']:32} no board found "
                      f"(gated/aggregator-only employer?)")
                continue
            titles = _sample_titles(hit)
            tier, score, reason = score_company_mission(
                hit["name"], " | ".join(t for t in titles if t))
            active = 1 if (tier in ACTIVE_MISSION_TIERS
                           or config.is_multi_division(c["name"])
                           or tier is None) else 0
            is_wd = hit["ats"] == "workday"
            row = {"name": c["name"], "ats": hit["ats"],
                   "slug": None if is_wd else hit["slug"],
                   "wd_tenant": hit["slug"][0] if is_wd else None,
                   "wd_pod":    hit["slug"][1] if is_wd else None,
                   "wd_site":   hit["slug"][2] if is_wd else None,
                   "careers_url": hit.get("careers_url"),
                   "nc_job_count": hit["nc"], "total_job_count": hit["count"],
                   "mission_tier": tier, "mission_score": score,
                   "mission_reason": reason,
                   "tags": "nc_local" if hit["nc"] else None,
                   "source": "page_capture", "active": active}
            upsert_company(conn, row)
            resolved.append(row)
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            print(f"    [{'OK  ' if active else 'off-mission'}] {c['name']:32} "
                  f"{hit['ats']:12} nc={hit['nc']:<3} {str(tier):20} {ss}")
    except TimeoutError:
        stuck = [c["name"] for f, c in futs.items() if not f.done()]
        print(f"    [!] timed out waiting on {len(stuck)} lead(s): "
              f"{', '.join(stuck[:6])}{'...' if len(stuck) > 6 else ''} "
              f"(rerun --resolve-leads to retry)")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    conn.close()
    print(f"\n  {len(resolved)} board(s) resolved, "
          f"{sum(r['active'] for r in resolved)} activated, "
          f"{len(leads) - len(resolved)} miss(es).")
    return resolved


def format_config_block(confirmed):
    by = {"greenhouse": [], "lever": [], "ashby": [], "workday": []}
    for h in confirmed:
        by[h["ats"]].append(h)
    lines = ["# --- LOCAL_TECH company targets (discovered) ---", ""]
    lines.append("LOCAL_TECH_GREENHOUSE = {")
    for h in by["greenhouse"]:
        lines.append(f'    "{h["slug"]}": "{h["name"]}",  # {h["nc"]} NC / {h["count"]} total')
    lines.append("}\n")
    lines.append("LOCAL_TECH_LEVER = {")
    for h in by["lever"]:
        lines.append(f'    "{h["slug"]}": "{h["name"]}",  # {h["nc"]} NC / {h["count"]} total')
    lines.append("}\n")
    lines.append("LOCAL_TECH_ASHBY = {")
    for h in by["ashby"]:
        lines.append(f'    "{h["slug"]}": "{h["name"]}",  # {h["nc"]} NC / {h["count"]} total')
    lines.append("}\n")
    lines.append("# (tenant, wd_pod, site, name)")
    lines.append("LOCAL_TECH_WORKDAY = [")
    for h in by["workday"]:
        t, p, s = h["slug"]
        lines.append(f'    ("{t}", {p}, "{s}", "{h["name"]}"),  # {h["nc"]} NC / {h["count"]} total')
    lines.append("]")
    return "\n".join(lines)
