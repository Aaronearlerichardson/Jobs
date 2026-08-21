"""
Local (Triangle / NC) company sourcing for the LOCAL-TECH crawler.

Replaces the BCI-heavy hand-picked company list with a discovery pass over
health / bio / science / tech employers with a Triangle-NC presence:

  1. Gather candidate company NAMES from several free sources:
       - a curated seed of established Triangle/NC health-bio-science + tech
         employers,
       - the static entries on the RTP.org directory,
"""

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime

import config
from core import tags as company_tags

from scrapers.http import HEADERS, SESSION
from .probes import (probe_greenhouse, probe_lever, probe_ashby, probe_workday,
                     _DOMAIN_STOPWORDS, _name_domain_tokens)


# --------------------------------------------------------------------------- #
#  Bounded + cached DuckDuckGo text search                                     #
# --------------------------------------------------------------------------- #
#
# DDG is the crawl's single biggest time sink: a plain `DDGS().text(q)` has no
# wall-clock bound, so when DDG rate-limits (frequent), the library's internal
# retry/backoff blocks for many minutes yielding nothing — profiled at ~1271s
# of a 1726s --local run. Two guards, capability preserved:
#   * a disk cache (7-day TTL) so repeat runs — and repeat queries within a
#     run — return instantly instead of re-hitting DDG;
#   * a hard per-query wall-clock budget via a worker thread + join(timeout),
#     so one throttled query abandons after ~budget seconds instead of stalling
#     the whole crawl. A timed-out/empty result is NOT cached, so it retries
#     next run (we only cache genuine non-empty hits).
_DDG_CACHE_DIR = config.DATA_DIR / ".cache" / "ddg"
_DDG_CACHE_TTL = 7 * 24 * 3600      # seconds
_DDG_WALL_BUDGET = 25.0             # hard per-query wall-clock cap (seconds)


def _ddg_cache_path(key):
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return _DDG_CACHE_DIR / f"{h}.json"


def _ddg_cache_get(key):
    p = _ddg_cache_path(key)
    try:
        if time.time() - p.stat().st_mtime > _DDG_CACHE_TTL:
            return None
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _ddg_cache_put(key, value):
    try:
        _DDG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _ddg_cache_path(key).write_text(json.dumps(value), encoding="utf-8")
    except Exception:
        pass


def ddg_text(query, max_results=10, budget=_DDG_WALL_BUDGET):
    """Bounded, disk-cached DDG text search. Returns a list of result dicts
    (each with 'href'/'title'/...), or [] on miss/timeout/missing-package —
    every caller already tolerates an empty list."""
    key = f"{query}||{max_results}"
    cached = _ddg_cache_get(key)
    if cached is not None:
        return cached
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return []
    box = {"v": None}

    def _run():
        try:
            with DDGS(timeout=min(10, int(budget))) as ddg:
                box["v"] = list(ddg.text(query, max_results=max_results))
        except Exception:
            box["v"] = None

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(budget)
    out = box["v"] or []
    if out:                              # cache only genuine hits
        _ddg_cache_put(key, out)
    return out


# --------------------------------------------------------------------------- #
#  Candidate NAMES                                                             #
# --------------------------------------------------------------------------- #

# Seed employers + Workday-fallback majors + drop-list all come from the
# active profile ([discovery]) so sourcing generalizes to any region/domain.
# Name -> comparison key: strip everything but [a-z0-9]. Called on every
# candidate name in several dedup/lookup loops, so compile it once.
_NONALNUM_RE = re.compile(r"[^a-z0-9]")

SEED_COMPANIES = config.DISCOVERY_SEED_NAMES   # names only; seeds.py keeps notes
MAJORS_WORKDAY = config.DISCOVERY_WORKDAY_MAJORS
_MAJORS_KEYS = {_NONALNUM_RE.sub("", m.lower()) for m in MAJORS_WORKDAY}
NAME_BLOCKLIST = config.DISCOVERY_NAME_BLOCKLIST


def _wd_search_text():
    """Free-text location term for Workday's CXS search, from [locality] —
    the same derivation the crawl fetcher uses, so a probe's count and the
    later crawl agree on what "in your area" means."""
    from scrapers.fetchers.company import _default_search_text
    return _default_search_text()


# Non-company noise seen in `/company/<slug>/` harvesting: image placeholders,
# nav/facet labels, listicle fragments — dropped before probing.
_NAME_NOISE_RE = re.compile(
    r"^(fallback[\s-]?image|compan(y|ies)|directory|home|built in|search|menu|"
    r"about|contact|careers?|jobs?|privacy|terms|cookie|login|register|"
    r"company[\s_-]?types?|facility[\s_-]?types?|availability|operator|opt)\b",
    re.I)


def _looks_like_company(name):
    n = (name or "").strip()
    if not (2 < len(n) < 45) or not re.search(r"[A-Za-z]", n):
        return False
    if _NAME_NOISE_RE.match(n) or re.fullmatch(r"[a-z0-9_]+", n):
        return False   # snake_case slug / facet name, never a display name
    if re.search(r"\b(jobs|startups?|startup week|ecosystem|degrees?)\b", n, re.I):
        return False   # listicle/region phrases, not employers
    return True


# On a directory/listicle page, an employer is a `/company/<slug>/` link.
_COMPANY_SLUG_RE = re.compile(r"/company/([a-z0-9][a-z0-9\-]{2,58})/?", re.I)
_STOP_SLUGS = {"research-triangle-park"}


def _names_from_html(html):
    out = set()
    for slug in _COMPANY_SLUG_RE.findall(html or ""):
        s = slug.lower()
        if s in _STOP_SLUGS or "fallback-image" in s:
            continue
        out.add(slug.replace("-", " ").title())
    return {n for n in out if _looks_like_company(n)}


def scrape_directory_names(url, timeout=20):
    """Employer names from a directory page's `/company/<slug>/` links — works
    for any site with that shape (RTP.org, Built In, chamber directories).
    Server-rendered only; JS-loaded facets are out of scope."""
    try:
        r = SESSION.get(url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] directory scrape failed ({url}): {e}")
        return []
    return sorted(_names_from_html(r.text))


def harvest_search_names(queries, per_query=12, fetch_dirs=10):
    """The main recall lever: web-search each query, then scrape the directory/
    listicle results (Built In, Growjo, Crunchbase, ...) for `/company/<slug>/`
    employer links. Every name is probed downstream, so residual noise just
    fails to resolve. Returns a de-duped list."""
    if not queries:
        return []

    _DIR_HOSTS = ("builtin.com", "growjo.com", "rtp.org", "ncbiotech",
                  "crunchbase", "themuse", "vault.com", "clutch.co", "wellfound",
                  "getlatka", "tracxn", "f6s.com")
    dir_urls = []
    for q in queries:
        for r in ddg_text(q, max_results=per_query):
            u = r.get("href") or r.get("url") or ""
            if u and any(h in u.lower() for h in _DIR_HOSTS):
                dir_urls.append(u)

    names = set()
    for u in list(dict.fromkeys(dir_urls))[:fetch_dirs]:
        try:
            html = SESSION.get(u, timeout=12, headers=HEADERS).text
        except Exception:
            continue
        names |= _names_from_html(html)
    return sorted(names)


def brainstorm_company_names(n=None):
    """One LLM call listing REAL employers matching the profile's region +
    domain — a stage-1 name source reaching companies that directory sites
    never list (private CROs, hospital-system tech arms, spinouts).

    Hallucination-safe by construction: every name still has to survive the
    probe -> NC-count -> sniffer verification chain downstream, so an
    invented company simply fails to resolve — same contract as web-harvest
    noise. Disk-cached on the DDG cache's 7-day TTL so repeat runs are free;
    profile.toml [discovery] brainstorm_names tunes the count (0 disables).
    Without an API key it quietly contributes nothing."""
    if n is None:
        cfg = getattr(config, "DISCOVERY_BRAINSTORM_NAMES", None)
        n = 50 if cfg is None else int(cfg)   # explicit 0 means "off"
    if n <= 0:
        return []
    region = ", ".join((config.LOCALITY_SUBSTRINGS or [])[:6]) or "the target region"
    domain = ", ".join((config.DOMAIN_KEYWORDS or [])[:10]) or "the target domain"
    key = f"brainstorm||{n}||{region}||{domain}"
    cached = _ddg_cache_get(key)
    if cached is not None:
        return cached
    from core.claude import call_claude_json
    system = ("You help maintain a job-search company roster. "
              "Return ONLY valid JSON. No markdown, no commentary.")
    user = (
        f"List up to {n} REAL employers likely to have offices, labs, or "
        f"significant operations in or near: {region}.\n"
        f"Focus on organizations whose work involves: {domain}.\n"
        "Mix sizes and kinds: large employers, mid-size companies, startups, "
        "CROs, diagnostics and device makers, health-system technology arms, "
        "university spinouts. Use official company names only — no "
        "descriptions, no locations, no commentary.\n"
        'Return ONLY: {"companies": ["Name", "Name", ...]}')
    r = call_claude_json(system, user, max_tokens=1600)
    names = [str(x).strip() for x in (r.get("companies") or []) if str(x).strip()]
    names = [x for x in names if 2 < len(x) < 60][:n]
    if names:
        _ddg_cache_put(key, names)
    return names


def gather_names(extra=None):
    """Union of all name sources, de-duplicated case-insensitively:
    profile seeds + Workday majors + configured directory scrapes + web-search
    harvesting + an LLM region/domain brainstorm + any explicit `extra`."""
    sources = [SEED_COMPANIES, MAJORS_WORKDAY]
    for url in config.DISCOVERY_DIRECTORY_URLS:
        sources.append(scrape_directory_names(url))
    harvested = harvest_search_names(config.DISCOVERY_NAME_SEARCH_QUERIES)
    if harvested:
        print(f"    web-search harvested {len(harvested)} candidate name(s)")
    sources.append(harvested)
    brainstormed = brainstorm_company_names()
    if brainstormed:
        print(f"    LLM brainstorm contributed {len(brainstormed)} candidate name(s)")
    sources.append(brainstormed)
    sources.append(extra or [])

    names, seen = [], set()
    for src in sources:
        for n in src:
            k = _NONALNUM_RE.sub("", (n or "").lower())
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


from core.locality import is_nc as _has_nc  # single source of truth for NC locality


def _nc_count_greenhouse(slug):
    try:
        r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
                         timeout=15, headers=HEADERS)
        return sum(1 for j in r.json().get("jobs", [])
                   if _has_nc(j.get("location", {}).get("name", "")))
    except Exception:
        return 0


def _nc_count_lever(slug):
    try:
        r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         timeout=15, headers=HEADERS)
        return sum(1 for j in r.json()
                   if _has_nc(j.get("categories", {}).get("location", "")))
    except Exception:
        return 0


def _nc_count_ashby(slug):
    try:
        r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         timeout=15, headers=HEADERS)
        data = r.json()
        return sum(1 for j in data.get("jobs", data.get("jobPostings", []))
                   if _has_nc(j.get("location", "")))
    except Exception:
        return 0


def _nc_count_workday(tenant, pod, site):
    """Count Workday postings in your [locality] (searchText hits location)."""
    api = f"https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = SESSION.post(api, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                     "searchText": _wd_search_text()},
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
    n_wd = sum(1 for n in names if _NONALNUM_RE.sub("", n.lower()) in _MAJORS_KEYS)
    print(f"  probing {len(names)} candidate compan(ies) for live ATS boards "
          f"({n_wd} with Workday fallback)...")
    hits = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(probe_company, n,
                          _NONALNUM_RE.sub("", n.lower()) in _MAJORS_KEYS): n
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
        found = {_NONALNUM_RE.sub("", h["name"].lower())
                 for h in hits if h["nc"] > 0}
        missed = [m for m in MAJORS_WORKDAY
                  if _NONALNUM_RE.sub("", m.lower()) not in found]
        import importlib.util
        if importlib.util.find_spec("playwright.sync_api") is None:
            if missed:
                print(f"    [js] playwright not installed; skipping JS probe "
                      f"of {len(missed)} major(s)")
            missed = []
        if missed:
            from .probes import WorkdayJsProbe
            # Parallel across DIFFERENT sites is safe: each target still sees
            # exactly one page load; the serial design existed for sync-
            # Playwright's thread affinity, not politeness. Each probe
            # instance already pins its browser to its own dedicated thread,
            # so K instances + K caller threads = K-way parallelism with the
            # thread-safety model untouched. K is memory-bound (one headless
            # Chromium each), so it's capped low and separate from the HTTP
            # worker count.
            k = min(4, len(missed))
            print(f"  JS-probing {len(missed)} major(s) with no static board "
                  f"({k} parallel browser(s))...")
            with ExitStack() as stack:
                probes = [stack.enter_context(WorkdayJsProbe()) for _ in range(k)]

                def _js_one(i, name):
                    wd = probes[i % k].probe(name)
                    if not (wd and wd.get("validated")):
                        return None
                    nc = _nc_count_workday(wd["tenant"], wd["wd_pod"], wd["site"])
                    return {"name": name, "ats": "workday",
                            "slug": (wd["tenant"], wd["wd_pod"], wd["site"]),
                            "count": wd["count"], "nc": nc}

                with ThreadPoolExecutor(max_workers=k) as ex:
                    futs = {ex.submit(_js_one, i, m): m
                            for i, m in enumerate(missed)}
                    for fut in as_completed(futs):
                        try:
                            h = fut.result()
                        except Exception as e:
                            print(f"    [!] JS probe failed for "
                                  f"{futs[fut]!r}: {e}")
                            continue
                        if h:
                            hits.append(h)
                            t, p, s = h["slug"]
                            print(f"    [JS-OK] {h['name']:30} {t}/{p}/{s}  "
                                  f"nc={h['nc']}/{h['count']}")

    # Sniffer pass: for names still without a real (NC>0) board, fetch their
    # careers page and detect the embedded ATS + exact slug (covers Greenhouse/
    # Lever/Ashby/Workday/SmartRecruiters/iCIMS/SuccessFactors and finds slugs
    # the name-guesser can't). This is the main recall lever over the directory.
    if sniff:
        from .sniffer import sniff_ats
        from scrapers.fetchers import company as company_fetch
        have = {_NONALNUM_RE.sub("", h["name"].lower()) for h in hits if h["nc"] > 0}
        todo = [n for n in names if _NONALNUM_RE.sub("", n.lower()) not in have]
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
            if _NONALNUM_RE.sub("", h["name"].lower()) not in NAME_BLOCKLIST]

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

from core.locality import NC_HQ_RE as _NC_HQ_RE  # "<Triangle city>, NC" HQ/office signal


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
            r = SESSION.get(u, timeout=12, headers=HEADERS, allow_redirects=True)
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
            r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
                             timeout=15, headers=HEADERS)
            return [j.get("title", "") for j in r.json().get("jobs", [])[:n]]
        if ats == "lever":
            r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                             timeout=15, headers=HEADERS)
            return [j.get("text", "") for j in r.json()[:n]]
        if ats == "ashby":
            r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                             timeout=15, headers=HEADERS)
            # Ashby's posting API says "jobs"; only Workday (below) says
            # "jobPostings". Reading the wrong one here handed the mission
            # scorer an empty title list, so every Ashby company was scored
            # on its name alone.
            data = r.json()
            return [j.get("title", "") for j in
                    data.get("jobs", data.get("jobPostings", []))[:n]]
        if ats == "workday":
            t, p, s = slug
            api = f"https://{t}.wd{p}.myworkdayjobs.com/wday/cxs/{t}/{s}/jobs"
            r = SESSION.post(api, json={"appliedFacets": {}, "limit": n, "offset": 0,
                                         "searchText": _wd_search_text()},
                              timeout=15, headers={**HEADERS, "Content-Type": "application/json"})
            return [j.get("title", "") for j in r.json().get("jobPostings", [])[:n]]
    except Exception:
        return []
    return []


def populate_companies(extra_names=None, include_missions=None, dork=True):
    """
    Full sourcing pass → SQL store: discover NC-local boards, score each
    company's MISSION once (cached), and upsert into the `companies` table.
    Companies whose mission is `other` are stored but marked inactive (so
    they aren't crawled) unless include_missions says otherwise.

    Finishes with the ATS-dork sweep (search-indexed board URLs) unless
    `dork=False` — run LAST on purpose: it consults the store and skips
    boards the name-based pass just added, so the two passes don't create
    duplicate rows for the same board. Dork adds are upserted directly and
    are NOT included in the returned list.

    Returns the list of company dicts written by the name-based pass.
    """
    from core.store import connect, upsert_company
    from core.claude import score_company_mission, is_active_mission

    confirmed, _ = discover_local(extra_names)
    conn = connect()
    written = []
    print(f"\n  scoring mission for {len(confirmed)} NC-local compan(ies)...")

    # The title fetch (1 GET) + mission call (1 LLM request) per company are
    # pure network I/O — the historical serial tail of the pass. Run them in
    # a pool; SQLite upserts stay on this thread (connections don't cross
    # threads). Output is completion-ordered.
    def _score_one(h):
        titles = _sample_titles(h)
        return h, score_company_mission(h["name"],
                                        " | ".join(t for t in titles if t))

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(confirmed)))) as ex:
        futs = {ex.submit(_score_one, h): h for h in confirmed}
        for fut in as_completed(futs):
            try:
                h, (tier, score, reason) = fut.result()
            except Exception as e:
                print(f"    [!] mission scoring failed for "
                      f"{futs[fut]['name']!r}: {e}")
                continue
            # Shared activation rule (core.claude.is_active_mission):
            # active tiers, an UNAVAILABLE (None) score, or a multi-division
            # conglomerate whose subdivisions are filtered at crawl time.
            active = is_active_mission(tier, h["name"], include_missions)
            row = {
                "name": h["name"], "ats": h["ats"],
                "slug": h["slug"] if h["ats"] != "workday" else None,
                "wd_tenant": h["slug"][0] if h["ats"] == "workday" else None,
                "wd_pod":    h["slug"][1] if h["ats"] == "workday" else None,
                "wd_site":   h["slug"][2] if h["ats"] == "workday" else None,
                "careers_url": h.get("careers_url"),
                "local_job_count": h["nc"], "total_job_count": h["count"],
                "mission_tier": tier, "mission_score": score, "mission_reason": reason,
                "tags": company_tags.LOCAL, "source": "local_sourcing", "active": active,
                "last_probed": datetime.now().isoformat(),
            }
            upsert_company(conn, row)
            written.append({**row, "active": active})
            flag = "active" if active else "INACTIVE(other)"
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            print(f"    {h['name']:30} {str(tier):20} {ss}  [{flag}]  ({reason})")
    conn.close()

    if dork:
        print("\n  ATS-dork sweep (search-indexed board URLs)...")
        try:
            from .ats_dork import run_ddgs_dorks
            added, checked = run_ddgs_dorks()
            print(f"  dork: {added} new board(s) added "
                  f"({checked} extracted from search results)")
        except Exception as e:
            print(f"  [!] dork sweep failed (name-based results unaffected): {e}")
    return written


def add_board(name, url):
    """Register a board the user already knows — no guessing. `url` may be
    the ATS board itself (myworkdayjobs / greenhouse / lever / ...) or the
    company's careers page; coordinates are detected, the board NC-counted,
    mission-scored, and activated.

        python discover.py --add-board "NC DHHS" https://nc.wd108.myworkdayjobs.com/NC_Careers
    """
    from core.claude import score_company_mission
    from scrapers.fetchers import company as company_fetch
    from core.store import connect, upsert_company
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
        "local_job_count": nc, "mission_tier": tier, "mission_score": score,
        "mission_reason": reason, "tags": company_tags.LOCAL if nc else None,
        "source": "manual", "active": 1,
    })
    conn.close()
    ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
    print(f"  [OK] {name}: {ats} {slug!s}  nc={nc}  mission={tier} ({ss})  ACTIVE")
    return found


# Job aggregators / company-directory sites: they rank highly for
# '"<name>" careers' but are never the employer's own ATS board, so sniffing
# them wastes fetch slots. Skipped when picking result URLs to resolve.
# Source: config.DISCOVERY_AGGREGATOR_HOSTS (profile.toml [discovery]
# aggregator_hosts); falls back to these defaults when unconfigured.
_DEFAULT_AGGREGATOR_HOSTS = (
    "linkedin.com", "indeed.", "glassdoor.", "ziprecruiter.com", "simplyhired.com",
    "builtin.com", "rocketreach.co", "careerjet.", "monster.com", "dice.com",
    "lensa.com", "jobcase.com", "themuse.com", "wellfound.com", "levels.fyi",
    "trueup.io", "salary.com", "comparably.com", "talent.com", "unifygtm.com",
    "getro.com", "jooble.org", "adzuna.", "snagajob.com", "careers.tufts.edu",
    "google.com/search", "bing.com", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "crunchbase.com", "pitchbook.com", "zippia.com",
)
_AGGREGATOR_HOSTS = tuple(
    getattr(config, "DISCOVERY_AGGREGATOR_HOSTS", None) or _DEFAULT_AGGREGATOR_HOSTS
)


def _is_aggregator(url):
    return any(h in url.lower() for h in _AGGREGATOR_HOSTS)


# Generic words that don't distinguish a company's domain — excluded when
# matching a result host to a name, so "medicaljobs.com" doesn't match
# "Sampson Regional Medical Center" on the word "medical". Source:
# config.DISCOVERY_GENERIC_NAME_WORDS (profile.toml [discovery]
# generic_name_words); falls back to these defaults when unconfigured.
_DEFAULT_GENERIC_NAME_WORDS = {
    "medical", "center", "centre", "health", "healthcare", "regional",
    "group", "services", "systems", "system", "technology", "technologies",
    "imaging", "solutions", "associates", "partners", "care", "clinic",
    "hospital", "labs", "laboratories", "company", "corporation", "global",
    "national", "american", "international", "the", "and", "inc", "llc",
}
_GENERIC_NAME_WORDS = getattr(config, "DISCOVERY_GENERIC_NAME_WORDS", None) or _DEFAULT_GENERIC_NAME_WORDS


def _host_matches_name(url, name):
    """True if the result's host plausibly belongs to the company itself
    (a distinctive name token appears in the host) — the guard that keeps a
    self-hosted 'custom' board from resolving to a third-party jobs site."""
    host = re.sub(r"^https?://", "", url.lower()).split("/", 1)[0].replace("www.", "")
    hostslug = _NONALNUM_RE.sub("", host)
    joined = _NONALNUM_RE.sub("", name.lower())
    tokens = {joined} | {w for w in re.findall(r"[a-z0-9]+", name.lower())
                         if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}
    return any(len(t) >= 4 and t in hostslug for t in tokens)


def _slug_matches_name(slug, name):
    """True if a web-searched ATS slug/tenant plausibly belongs to the
    company — guards against the dork surfacing an unrelated board (e.g.
    'Novamed' -> the 'nc' NC-government Workday tenant)."""
    s = slug[0] if isinstance(slug, tuple) else slug   # workday tenant, else slug
    s = _NONALNUM_RE.sub("", str(s or "").lower())
    if len(s) < 3:
        return False
    tokens = {_NONALNUM_RE.sub("", name.lower())}
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
    from scrapers.http import HEADERS as _H
    from scrapers.fetchers.company import custom_board_listing_url
    from .sniffer import _detect, _pack

    def _search(query):
        out = []
        for r in ddg_text(query, max_results=max_results):
            u = r.get("href") or r.get("url")
            if u and not _is_aggregator(u):
                out.append(u)
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
                r = SESSION.get(u, timeout=8, headers=_H, allow_redirects=True)
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
    """Backfill company mission scores: every company with a board and no
    mission_tier (or every ACTIVE one, with rescore_all) gets sampled titles
    + one score_company_mission call. Heals stores populated by
    --import-companies / older seed imports (no scoring) or by
    keyless/failed scoring passes.

    The unscored pass deliberately includes INACTIVE rows. A company whose
    mission call failed can have been written active=0 by the add path that
    created it, and that state is otherwise terminal: the sourcing passes all
    skip boards already present in the store, so the row is never re-probed
    and never re-scored. Reading only active rows made this healer blind to
    exactly the rows it exists to heal. Scoring one of them to an active tier
    reactivates it below. `rescore_all` stays active-only — it is a
    re-judgement of the live roster, not a recovery pass, and widening it
    would resurrect everything ever deactivated for being off-mission."""
    from core.claude import ACTIVE_MISSION_TIERS, score_company_mission
    from core.store import connect, get_companies, upsert_company

    conn = connect()
    cos = [c for c in get_companies(conn, active_only=rescore_all)
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
            # Watched companies are exempt: the watch tag is the user
            # deliberately keeping an off-mission employer crawled (Covar).
            update = {"name": c["name"], "mission_tier": tier,
                      "mission_score": score, "mission_reason": reason}
            revived = False
            if (tier == "other" and not config.is_multi_division(c["name"])
                    and "watch" not in (c.get("tags") or "").split(",")):
                update["active"] = 0
            # NOT core.claude.is_active_mission: this is the REACTIVATION
            # half, and it deliberately does not revive on `tier is None`.
            # A None tier with a non-None score means the model answered with
            # a mission name outside the profile's taxonomy (score_company_
            # mission nulls the tier but keeps the score), so the `continue`
            # above did not fire. The helper would call that "unavailable" and
            # revive the row; here an unrecognised answer must leave an
            # already-inactive company alone. See tests/test_invariants.py.
            elif not c.get("active") and (tier in ACTIVE_MISSION_TIERS
                                          or config.is_multi_division(c["name"])):
                # The recovery half: this row reached an on-mission tier but
                # is sitting inactive, which for an unscored row means its
                # original mission call failed rather than judged it. Revive
                # it. Dead boards are excluded — prune_dead_boards turns those
                # off because the endpoint 404s, and a good mission score says
                # nothing about whether the board still resolves.
                if not str(c.get("notes") or "").startswith("deactivated: dead"):
                    update["active"] = 1
                    revived = True
            upsert_company(conn, update)
            n += 1
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            flag = ("  -> deactivated (off-mission)" if tier == "other"
                    else "  -> REACTIVATED (was unscored + inactive)" if revived
                    else "")
            print(f"    {c['name']:32} {str(tier):20} {ss}  ({reason}){flag}")
    conn.close()
    print(f"\n  {n} compan(ies) scored.")
    return n


def resolve_company_board(name):
    """Resolve a company NAME to a crawlable board: slug-probe (Greenhouse/
    Lever/Ashby/Workday) -> careers-page sniff -> web search. Returns a hit
    dict {name, ats, slug, count, nc, careers_url} with an NC job count, or
    None. Shared by --resolve-leads and the manual --add flow."""
    from scrapers.fetchers import company as company_fetch
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


def _validate_board(comp):
    """Fetch a resolved board and return (total, nc) live job counts. A board
    that returns zero jobs is treated as dead/wrong by the caller — this is
    what rejects a slug-guess that resolves to an empty or nonexistent board."""
    from scrapers.fetchers import company as company_fetch
    try:
        allj = company_fetch.fetch_company(comp, None)
    except Exception:
        return 0, 0
    if not allj:
        return 0, 0
    try:
        nc = sum(1 for j in allj
                 if company_fetch.NC_RE.search(j.get("location", "") or ""))
    except Exception:
        nc = 0
    return len(allj), nc


def resolve_board_sniff_first(name, careers_url=""):
    """Resolve a company NAME -> crawlable board, careers-page SNIFF FIRST,
    slug-probe only as a fallback, and VALIDATE every hit with a live fetch.

    The collision-hardened counterpart to resolve_company_board(): that one
    probes name-guessed slugs first, which false-positives onto same-named but
    unrelated boards ('Oxford Biomedica' -> a different Oxford Workday tenant;
    'Raya Health' -> the Raya dating app on Lever). Sniffing the company's OWN
    careers page can't collide that way, so it goes first; a probe-only hit is
    tagged ``via='probe'`` so the caller can flag it for a human sanity-check.

    Returns {name, ats, slug, careers_url, count, nc, via} or None. ``slug`` is
    a (tenant, pod, site) triple for Workday, the GUID/slug otherwise, None for
    a custom self-hosted board."""
    from .sniffer import sniff_ats

    def _mk(ats, slug, curl, via):
        if ats == "workday":
            comp = {"ats": "workday", "wd_tenant": slug[0],
                    "wd_pod": slug[1], "wd_site": slug[2]}
        elif ats == "custom":
            comp = {"ats": "custom", "careers_url": curl}
        else:
            comp = {"ats": ats, "slug": slug, "careers_url": curl}
        total, nc = _validate_board(comp)
        if total <= 0:
            return None
        return {"name": name, "ats": ats, "slug": slug, "careers_url": curl,
                "count": total, "nc": nc, "via": via}

    # 1) Authoritative: detect the ATS embedded on the company's own careers page.
    s = sniff_ats(name, careers_url or "")
    if s:
        if s["ats"] == "workday":
            hit = _mk("workday", s["triple"], s.get("careers_url"), "sniff")
        elif s["ats"] == "custom":
            hit = _mk("custom", None, s.get("careers_url"), "sniff")
        else:
            hit = _mk(s["ats"], s.get("slug"), s.get("careers_url"), "sniff")
        if hit:
            return hit

    # 2) Fallback: name-guessed slug/Workday probe (collision risk -> validated).
    p = probe_company(name, try_workday=True)
    if p:
        hit = _mk(p["ats"], p["slug"], p.get("careers_url"), "probe")
        if hit:
            return hit

    # 3) Web-search fallback: find the careers page for names whose domain the
    #    sniffer can't guess (acronyms, hyphenated or product-named domains --
    #    'OXB' -> oxb.com, 'United Imaging - North America' -> united-imaging.com,
    #    'Core Sound Imaging' -> studycast). _websearch_board already validates
    #    slug/own-domain against the name, so it's not collision-flagged.
    #    Best-effort: degrades to a miss when the search backend is rate-limited.
    w = _websearch_board(name)
    if w:
        if w["ats"] == "workday":
            hit = _mk("workday", w["triple"], w.get("careers_url"), "websearch")
        elif w["ats"] == "custom":
            hit = _mk("custom", None, w.get("careers_url"), "websearch")
        else:
            hit = _mk(w["ats"], w.get("slug"), w.get("careers_url"), "websearch")
        if hit:
            return hit
    return None


def resolve_leads(max_workers=8,
                  sources=("page_capture", "linkedin_search", "linkedin_company_search"),
                  all_leads=False, limit=None):
    """Resolve boardless company leads (banked by capture.py from browsed
    LinkedIn/Indeed pages, or by manual adds) into crawlable boards and
    activate the hits. Careers-page SNIFF first (collision-safe), slug-probe
    fallback, every board VALIDATED by a live fetch, then mission-scored.
    The capture -> resolve-leads -> crawl loop is how manually browsed postings
    grow the roster.

    sources: resolve only leads carrying one of these ``source`` values
    (default: capture.py's 'page_capture'). all_leads=True ignores the source
    filter and takes every inactive boardless lead. Idempotent — rerunning
    retries only the still-unresolved leads."""
    from core.claude import score_company_mission, is_active_mission
    from core.store import connect, get_companies as _store_companies, upsert_company

    conn = connect()
    leads = [c for c in _store_companies(conn, active_only=False)
             if not c.get("ats") and not c.get("active")]
    if not all_leads:
        leads = [c for c in leads if c.get("source") in sources]
    if limit:
        leads = leads[:int(limit)]
    if not leads:
        print("  No unresolved leads to resolve"
              + ("." if all_leads else f" (source in {sources}; --all-leads to widen)."))
        conn.close()
        return []
    print(f"  resolving {len(leads)} lead(s) (careers-page sniff -> slug-probe "
          f"fallback; every board validated by a live fetch)...")

    resolved, probe_only = [], []
    # Hard cap on the pass: a lead whose domains blackhole must become a
    # reported miss, not a hung command.
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(resolve_board_sniff_first, c["name"], c.get("careers_url") or ""): c
            for c in leads}
    try:
        for fut in as_completed(futs, timeout=300):
            c, hit = futs[fut], fut.result()
            if not hit:
                print(f"    [miss] {c['name'][:34]:34} no live board found "
                      f"(LinkedIn/Indeed-only or JS-gated employer?)")
                continue
            titles = _sample_titles(hit)
            tier, score, reason = score_company_mission(
                c["name"], " | ".join(t for t in titles if t))
            active = is_active_mission(tier, c["name"])
            is_wd = hit["ats"] == "workday"
            row = {"name": c["name"], "ats": hit["ats"],
                   "slug": None if is_wd else hit["slug"],
                   "wd_tenant": hit["slug"][0] if is_wd else None,
                   "wd_pod":    hit["slug"][1] if is_wd else None,
                   "wd_site":   hit["slug"][2] if is_wd else None,
                   "careers_url": hit.get("careers_url"),
                   "local_job_count": hit["nc"], "total_job_count": hit["count"],
                   "mission_tier": tier, "mission_score": score,
                   "mission_reason": reason,
                   "tags": company_tags.LOCAL if hit["nc"] else None,
                   "source": c.get("source") or "resolve_leads", "active": active}
            upsert_company(conn, row)
            resolved.append(row)
            if hit.get("via") == "probe":
                probe_only.append(c["name"])
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            flag = "  [probe-only: verify]" if hit.get("via") == "probe" else ""
            print(f"    [{'OK  ' if active else 'off '}] {c['name'][:30]:30} "
                  f"{hit['ats']:12} nc={hit['nc']:<3} tot={hit['count']:<4} "
                  f"{str(tier):18} {ss}{flag}")
    except TimeoutError:
        stuck = [c["name"] for f, c in futs.items() if not f.done()]
        print(f"    [!] timed out on {len(stuck)} lead(s): "
              f"{', '.join(stuck[:6])}{'...' if len(stuck) > 6 else ''} "
              f"(rerun --resolve-leads to retry — idempotent)")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    conn.close()
    print(f"\n  {len(resolved)} board(s) resolved, "
          f"{sum(r['active'] for r in resolved)} activated, "
          f"{len(leads) - len(resolved)} miss(es).")
    if probe_only:
        print(f"  [verify] {len(probe_only)} resolved by name-guess, not the "
              f"company's own site — sanity-check for collisions: "
              f"{', '.join(probe_only[:6])}{'...' if len(probe_only) > 6 else ''}")
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


# --------------------------------------------------------------------------- #
#  Paste-a-page ingest                                                         #
# --------------------------------------------------------------------------- #
#
# Getting employers out of a site you are already browsing — a LinkedIn search,
# a Built In list, a news article — comes down to harvesting NAMES. The crawler
# never wants the source's job data: it resolves each name to the employer's
# OWN board, which is fresher, complete, and carries a real apply URL. So this
# surface can be dumb — take whatever text was on the page and let the existing
# probe -> validate -> score chain decide what was real. Same contract
# brainstorm_company_names() runs under: a name that isn't an employer simply
# fails to resolve.

# Lines that are never a company name in a pasted results page.
_PASTE_NOISE_RE = re.compile(
    r"^(?:"
    r"promoted|easy apply|actively recruiting|be an early applicant|viewed|"
    r"applied|saved|save|dismiss|see all|show more|load more|next|previous|"
    r"remote|hybrid|on-?site|full-?time|part-?time|contract|internship|"
    r"\d*\s*(?:day|week|month|hour|minute)s?\s*ago|reposted.*|"
    r"[\d,]*\s*[km]?\s*(?:followers?|employees?|connections?|applicants?)|"
    r"over \d+ applicants|(?:page\s*)?\d* *of *\d+|see all.*|show all.*"
    r")\W*$", re.I)

# A line that reads as a JOB TITLE rather than an employer. Results pages
# interleave the two, and a title resolves to nothing, so this saves the probe.
_TITLE_WORD_RE = re.compile(
    r"\b(?:engineer|scientist|developer|analyst|manager|director|specialist|"
    r"coordinator|associate|assistant|technician|architect|consultant|intern|"
    r"lead|head of|vp|president|officer|administrator|nurse|physician|"
    r"recruiter|designer|researcher|postdoc|fellow|programmer|statistician)\b",
    re.I)

# "Durham, NC" / "Durham, NC (Hybrid)" / "Raleigh-Durham-Chapel Hill Area"
_LOCATION_LINE_RE = re.compile(
    r"^[A-Z][\w.'-]+(?:[ \-][\w.'-]+)*,\s*(?:[A-Z]{2}|[A-Z][a-z]+)"
    r"(?:\s*\([^)]*\))?$|.*\bArea$|.*\bMetropolitan\b", re.I)


# A results row whose job title is rendered twice, optionally with a badge
# wedged between the halves: "Signal Processing Engineer (Verified job)Signal
# Processing Engineer". LinkedIn emits this for every hit, and the employer is
# always the next line — so the doubled line is an unambiguous "company below"
# marker. `.{4,}?` is lazy so the shortest repeating half wins.
_DOUBLED_TITLE_RE = re.compile(r"^(.{4,}?)(?:\s*\([^)]{,24}\))?\1$")


def _clean_candidate(raw, drop_titles=True):
    """One line -> a usable company name, or None.

    `drop_titles=False` for structurally-located names: the doubled-title
    marker already proved the line is an employer, and rejecting it for
    containing a word like "Science" or "Research" would lose real ones
    (Headwater Science, Vadum). The keyword screen is only needed when we
    are guessing from unstructured lines.
    """
    stripped = raw.strip()
    # Test noise BEFORE removing list markers: "2 days ago" and "1K followers"
    # only read as noise while they still carry their leading digits.
    if _PASTE_NOISE_RE.match(stripped):
        return None
    # Markdown export turns nav into "[Help Center](https://...)".
    if re.match(r"^\[[^\]]*\]\(", stripped):
        return None
    # Results pages render "Company · Location" and "Company • 1K followers".
    name = re.split(r"\s+[·•|]\s+", stripped)[0].strip()
    name = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", name).strip()
    if not (2 < len(name) <= 60):
        return None
    if _PASTE_NOISE_RE.match(name):
        return None
    if drop_titles and _TITLE_WORD_RE.search(name):
        return None
    if _LOCATION_LINE_RE.match(name):
        return None
    if not re.search(r"[A-Za-z]{2}", name):          # numbers / punctuation only
        return None
    if name.startswith(("http://", "https://", "www.")):
        return None
    return name


def _names_from_doubled_titles(lines):
    """Employers located by the repeated-title marker, in page order."""
    out = []
    for i, line in enumerate(lines):
        if not _DOUBLED_TITLE_RE.match(line.strip()):
            continue
        for nxt in lines[i + 1:i + 3]:               # skip a blank if present
            if nxt.strip():
                name = _clean_candidate(nxt, drop_titles=False)
                if name:
                    out.append(name)
                break
    return out


def parse_company_names(blob, limit=300):
    """Plausible employer names out of a pasted block of page text.

    Two passes. If the page repeats each job title — the shape every
    LinkedIn results row has — the employer is pinned by position and the
    surrounding chrome is never even considered. A real search page yields
    15 employers and no junk that way, against 117 lines for the filter.

    Otherwise fall back to filtering lines, which is deliberately
    permissive: precision is cheap to get wrong and expensive to tune, a
    junk name costs one failed resolve and is dropped, and a dropped real
    name is invisible.
    """
    if isinstance(blob, (list, tuple)):
        lines = [str(x) for x in blob]
    else:
        lines = re.split(r"[\r\n]+", str(blob or ""))

    structured = _names_from_doubled_titles(lines)
    # Two hits mean the marker is really this page's shape, not a coincidental
    # repeat in prose.
    candidates = structured if len(structured) >= 2 else [
        n for n in (_clean_candidate(ln) for ln in lines) if n]

    out, seen = [], set()
    for name in candidates:
        key = _NONALNUM_RE.sub("", name.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(name)
        if len(out) >= limit:
            break
    return out


def extract_names_llm(blob, limit=60):
    """Ask the model which employers a pasted page mentions.

    The regex path cannot tell "Fennec Pharmaceuticals" from a job title whose
    words it has never seen. One call fixes that for a messy paste. Returns []
    without an API key, so the caller falls back to the regex.
    """
    from core.claude import call_claude_json
    system = ("You extract EMPLOYER NAMES from text copied off a job-search or "
              "company-directory page. Return only organisations that could "
              "employ someone. Never return job titles, locations, dates, "
              "recruiter names, or UI labels. Return each company's plain name "
              "without taglines.")
    user = ('Return JSON {"companies": ["name", ...]} with at most '
            f'{limit} entries, in the order they appear.\n\n'
            f"---\n{str(blob or '')[:20000]}\n---")
    try:
        data = call_claude_json(system, user, max_tokens=2000)
    except Exception as e:
        print(f"    [!] name extraction failed ({type(e).__name__}: {e}); "
              f"falling back to the text parser")
        return []
    names = [str(x).strip() for x in (data or {}).get("companies", [])
             if str(x).strip()]
    return [n for n in names if 2 < len(n) <= 60][:limit]


def add_names(blob, use_llm=False, max_workers=6, include_missions=None):
    """Resolve pasted company names to boards and store the ones that verify.

    Uses resolve_board_sniff_first(), not the slug-probe-first resolver: a name
    a person pasted is exactly where a slug collision does the most damage
    (guessing 'sas' lands on an unrelated 5-job board while the real SAS
    Institute sits on iCIMS). Sniffing the company's own careers page cannot
    collide that way; a probe-only hit is reported for a human glance.
    """
    from core.claude import score_company_mission, is_active_mission
    from core.store import connect, upsert_company

    names = extract_names_llm(blob) if use_llm else []
    if not names:
        names = parse_company_names(blob)
    if not names:
        print("  no company names found in that text.")
        return []

    conn = connect()
    existing = {_NONALNUM_RE.sub("", (r["name"] or "").lower())
                for r in conn.execute("SELECT name FROM companies").fetchall()}
    fresh = [n for n in names if _NONALNUM_RE.sub("", n.lower()) not in existing]
    skipped = len(names) - len(fresh)
    print(f"  {len(names)} name(s) parsed"
          + (f", {skipped} already tracked" if skipped else "")
          + f" -> resolving {len(fresh)}...")

    written, unresolved = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(resolve_board_sniff_first, n): n for n in fresh}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                hit = fut.result()
            except Exception as e:
                print(f"    [!] {name}: {type(e).__name__}: {e}")
                hit = None
            if not hit:
                unresolved.append(name)
                continue
            titles = _sample_titles(hit)
            tier, score, reason = score_company_mission(
                hit["name"], " | ".join(t for t in titles if t))
            active = is_active_mission(tier, hit["name"], include_missions)
            slug = hit.get("slug")
            is_wd = hit["ats"] == "workday"
            upsert_company(conn, {
                "name": hit["name"], "ats": hit["ats"],
                "slug": None if is_wd else slug,
                "wd_tenant": slug[0] if is_wd else None,
                "wd_pod":    slug[1] if is_wd else None,
                "wd_site":   slug[2] if is_wd else None,
                "careers_url": hit.get("careers_url"),
                "local_job_count": hit["nc"], "total_job_count": hit["count"],
                "mission_tier": tier, "mission_score": score,
                "mission_reason": reason, "tags": company_tags.LOCAL,
                "source": "paste", "active": active,
                "last_probed": datetime.now().isoformat(),
            })
            written.append(hit)
            # via='probe' means the board came from a name-guess rather than
            # the company's own careers page — worth a human glance.
            flag = "  [verify: slug-guess]" if hit.get("via") == "probe" else ""
            print(f"    [OK]  {hit['name'][:30]:30} {hit['ats']:12} "
                  f"{hit['nc']}/{hit['count']:<5} {str(tier):20} "
                  f"{'active' if active else 'inactive'}{flag}")
    conn.commit()
    conn.close()
    if unresolved:
        print(f"\n  {len(unresolved)} name(s) did not resolve to a live board "
              f"(not employers, or no board we can read):")
        print("    " + ", ".join(unresolved[:25])
              + (" ..." if len(unresolved) > 25 else ""))
    print(f"\n  {len(written)} compan(ies) added.")
    return written
