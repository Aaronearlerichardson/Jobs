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
import logging
import re
import threading
import time
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                as_completed, wait as fut_wait)
from contextlib import ExitStack
from datetime import datetime

import config
from core import tags as company_tags

# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("discovery")

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

# Resolution watchdog: abandon a pass's remaining names if NO resolution
# completes for this long. Generous on purpose — a normal resolve chains a
# handful of 6-15s-bounded fetches; only a genuinely wedged one exceeds this.
RESOLVE_STALL_S = 300.0


def _drain_or_abandon(ex, futs, consume, stalled):
    """Drain `futs` ({future: name}) through consume(future, name); if no
    future completes within RESOLVE_STALL_S, report each remaining name to
    stalled(name) instead and shut the executor down WITHOUT joining its
    threads.

    Notes:
        The `with ThreadPoolExecutor(...)` form joins every worker on exit,
        so one wedged resolution (fetch_company_nc on a sprawling "custom
        board" is bounded per request, not in total) used to hold the web
        UI's one-op-at-a-time slot until the app was restarted — 2026-08-28:
        an add-names run finished 59 of 60 names in 8 minutes, then hung
        >1h on the last. Behavior is enforced by tests/test_parsers.py::
        TestResolutionStallWatchdog.
    """
    pending = set(futs)
    while pending:
        done, pending = fut_wait(pending, timeout=RESOLVE_STALL_S,
                                 return_when=FIRST_COMPLETED)
        if not done:
            for fut in pending:
                n = futs[fut]
                print(f"    [!] {n}: no progress in {RESOLVE_STALL_S:.0f}s "
                      f"- abandoned")
                stalled(n)
            break
        for fut in done:
            consume(fut, futs[fut])
    ex.shutdown(wait=False, cancel_futures=True)


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
        _log.debug("ddg cache hit (%d result(s)): %s", len(cached), query)
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
    if th.is_alive():
        _log.debug("ddg timed out after %.0fs: %s", budget, query)
    else:
        _log.debug("ddg live query, %d result(s): %s", len(out), query)
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
# nav/facet labels, listicle fragments — dropped before probing. Matches as a
# PREFIX (`\b`) because it comes from Title-Cased `/company/<slug>/` fragments
# that are never followed by more real words ("Company Types", "Careers").
_NAME_NOISE_RE = re.compile(
    r"^(fallback[\s-]?image|compan(y|ies)|directory|home|built in|search|menu|"
    r"about|contact|careers?|jobs?|privacy|terms|cookie|login|register|"
    r"company[\s_-]?types?|facility[\s_-]?types?|availability|operator|opt)\b",
    re.I)

# Site chrome seen in a PASTED LinkedIn/Indeed/Glassdoor page (nav bar items,
# sidebar CTAs, notification badges). Same idea as `_NAME_NOISE_RE` above —
# extended for the paste surface rather than a parallel filter — but matches
# the WHOLE line (`$`) instead of just a prefix: pasted text is free-running
# sentences/titles, not slug fragments, so a prefix match would also reject a
# real name that legitimately starts with one of these common words ("Company
# 0 Bio", "Learning Care Group"). A bad name that slips past this doesn't just
# get dropped — the downstream slug-guesser can resolve it to an unrelated
# real company's board (see discover_local's NAME_BLOCKLIST for confirmed
# collisions of this kind).
_NAV_CHROME_RE = re.compile(
    r"^(?:"
    # bare top-nav words, whole-line only (a real name may legitimately
    # START with one of these, e.g. "Home Depot", "Jobs.com")
    r"home|jobs?|"
    # LinkedIn top/side nav + CTAs
    r"my network|messaging|notifications?(?:\s*\d+)?|for business|"
    r"create (?:a |your )?cover letter|learning|"
    r"people you (?:can|may) (?:reach out to|know)|"
    r"people also (?:viewed|searched)|premium|my items|"
    # Indeed nav + CTAs
    r"find (?:a )?jobs?|job search|post (?:a |your )?job|employers?(?: home)?|"
    r"upload (?:your )?resum[eé]|career advice|company reviews?|"
    r"salary (?:guide|estimator|calculator)|salaries|find salaries|"
    # Glassdoor nav + CTAs
    r"for employers|explore|get hired|add a salary|add an interview|"
    r"interview questions?|write a review|browse (?:jobs|companies)|"
    r"community|reviews?|interviews?|companies|"
    # shared UI chrome
    r"saved jobs?|job alerts?|help center|sign (?:in|up)|see all|"
    r"show more|load more|unlock (?:profile|insights)|"
    # LinkedIn COMPANY/PROFILE page chrome (the 2026-08-28 add-names paste
    # was a company page, not a results page, and 45 of its 63 lines
    # reached the resolver: nav tabs, CTAs, sidebar labels)
    r"about|apply|more|overview|posts?|life|people|events|"
    r"advertising|ad choices|chart|beta|company|company-wide|competitors|"
    r"similar pages|affiliated pages|locations|verified page|"
    r"visit website|follow(?:ing)?|unfollow|connect|message|share|"
    r"show\b.*|see\b.*|navigating to\b.*|skip to\b.*|"
    r"(?:the )?latest hiring trends?.*|in my network|"
    # LinkedIn industry/sector labels (rendered as bare lines on company
    # pages; "Biotech" resolved by websearch to an unrelated real board)
    r"biotech(?:nology)?(?: research)?|pharma(?:ceuticals?)?|biology|"
    r"research|healthcare|health care|business services|"
    r"staffing (?:and|&) recruiting|artificial intelligence|"
    r"machine learning|ai/ml|software development|"
    r"information technology(?: (?:and|&) services)?|"
    # JD section headers (job-detail pastes interleave these)
    r"(?:job )?summary|(?:preferred |minimum |basic )?qualifications|"
    r"essential duties(?: (?:and|&) responsibilit\w*)?|"
    r"(?:key )?responsibilities|experience (?:and|&) qualifications|"
    r"requirements|benefits|compensation|education|"
    # footer chrome
    r"privacy(?: (?:&|and) terms)?|terms|cookie(?:s| policy)?|"
    r"accessibility|user agreement|copyright policy|brand policy|"
    r"community guidelines|language"
    r")$",
    re.I)


# The job sites people paste FROM put their own brand in the page chrome, so
# "Glassdoor"/"LinkedIn"/"Indeed" arrive looking exactly like a one-word
# Title-Cased employer and no structural rule can tell them apart. Derived
# from [discovery].aggregator_hosts rather than hardcoded, so a profile that
# adds a regional job board gets its brand filtered too: 'glassdoor.' ->
# 'glassdoor', 'linkedin.com' -> 'linkedin'.
_AGGREGATOR_BRANDS = {
    h.split(".")[0].lower()
    for h in (getattr(config, "DISCOVERY_AGGREGATOR_HOSTS", None) or ())
    if h.split(".")[0].isalpha()
}


def _is_nav_noise(name):
    """True if `name` is a pasted-page chrome line (nav item, CTA,
    notification badge) — the check behind the paste parser's
    `_clean_candidate()`. Also covers two slug/label shapes, shared with
    `_looks_like_company()` below.

    >>> _is_nav_noise("company_types")          # underscore facet slug
    True
    >>> _is_nav_noise("what"), _is_nav_noise("where")   # bare form labels
    (True, True)
    >>> _is_nav_noise("restor3d"), _is_nav_noise("nCino")
    (False, False)

    A MULTI-word run of pure-lowercase pure-alpha words is prose, not a
    name ("in the past day"); lowercase brands carry a digit or interior
    capital and are single tokens anyway:

    >>> _is_nav_noise("in the past day")
    True
    >>> _is_nav_noise("bioMerieux Clinical Diagnostics")
    False

    Notes:
        The two lowercase shapes are separated on purpose. An
        UNDERSCORE-joined fragment ("company_types") is unambiguously a
        facet slug. A bare all-lowercase run is only noise when it is also
        all-ALPHABETIC: search-form labels ("what", "where", "remote") look
        like that, while the real companies that stylize themselves
        lowercase carry a digit or an interior capital (restor3d, nCino,
        bioMerieux, 23andMe) and so survive. An earlier revision rejected
        every `[a-z0-9_]+` run, which caught the labels but also ate
        restor3d; the revision after it required an underscore, which saved
        restor3d and let "what"/"where" back through Indeed pastes.
    """
    n = (name or "").strip()
    return bool(_NAV_CHROME_RE.match(n)
                or n.lower() in _AGGREGATOR_BRANDS
                or re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)+", n)
                or re.fullmatch(r"[a-z]+(?: [a-z]+)*", n))


def _looks_like_company(name):
    n = (name or "").strip()
    if not (2 < len(n) < 45) or not re.search(r"[A-Za-z]", n):
        return False
    if _NAME_NOISE_RE.match(n) or _is_nav_noise(n):
        return False   # facet-slug noise, nav/CTA chrome, or a snake_case name
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
        # Crunchbase-style duplicate slugs carry a single-digit suffix
        # ("genomics-plc-1"), which title-cases into a bogus "Genomics Plc 1"
        # company name. Multi-digit tails stay: they are part of real names
        # (intel-471).
        s = re.sub(r"-\d$", "", slug)
        out.add(s.replace("-", " ").title())
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


_DEFAULT_WEBSEARCH_CAP = 20


def discover_local(extra_names=None, max_workers=12, js_majors=True, sniff=True,
                   websearch=True, websearch_cap=None, websearch_retry_days=14):
    """
    Gather names + probe each. Returns (confirmed, checked, misses) where
    confirmed is a list of NC-local hit dicts and misses is one dict per
    candidate that did NOT become one, carrying a ``reason`` code from
    core.store.MISS_REASONS. ``js_majors`` runs a headless-browser Workday
    probe for big employers the static probe missed.

    ``websearch`` runs resolve_board_sniff_first's third step (DDG search for
    a careers page) over names still boardless after probe+sniff, capped at
    ``websearch_cap`` names (None -> config [discovery].websearch_cap, else a
    small built-in default; 0 disables it). Names that missed within
    ``websearch_retry_days`` (core.store.recent_miss_names) are skipped.

    Notes:
        The misses used to be printed and dropped, so a name that failed
        failed identically on every subsequent run with no record of why —
        two thirds of the curated seed list was lost this way. The caller
        persists them (see populate_companies).

        A name the careers-page sniff cannot read anything off is reported
        as plain ``no-board-found``, not a refined code: classify_miss()
        would re-fetch every candidate URL for every one of the hundreds of
        boardless names in a full pass. The on-demand paths (resolve_or_miss,
        add_names, resolve_leads) work on tens of names and do classify.

        websearch defaults ON but is capped rather than run over the whole
        gather (~100+ names): DDG rate-limits hard, and an earlier uncapped
        profile blocked ~1271s of a 1726s run in DDG's own retry/backoff
        (see ddg_text above) — the on-demand resolvers (resolve_or_miss et
        al.) can afford to run it uncapped only because they work on tens of
        names, not the full candidate gather.
    """
    names = gather_names(extra_names)
    n_wd = sum(1 for n in names if _NONALNUM_RE.sub("", n.lower()) in _MAJORS_KEYS)
    print(f"  probing {len(names)} candidate compan(ies) for live ATS boards "
          f"({n_wd} with Workday fallback)...")
    hits, sniff_misses = [], []
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
                return {"name": n, "reason": "no-board-found"}
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
                    "careers_url": s.get("careers_url"),
                    # Coordinates were detected but the board yields nothing:
                    # a dead/wrong board, not an absent one. Overwritten by
                    # the nc>0 branch below when it does yield.
                    "reason": "board-dead:" + ats}

        def _sniff_done(fut, n):
            h = fut.result()
            if h and h.get("nc"):
                h.pop("reason", None)
                hits.append(h)
                print(f"    [SNIFF] {h['name']:28} {h['ats']:14} "
                      f"{h['slug']!s:26} nc={h['nc']}")
            elif h:
                sniff_misses.append(h)

        ex = ThreadPoolExecutor(max_workers=max_workers)
        _drain_or_abandon(
            ex, {ex.submit(_sniff_one, n): n for n in todo}, _sniff_done,
            lambda n: sniff_misses.append(
                {"name": n, "reason": "fetch-error:stalled"}))

    # Websearch pass: the third resolve_board_sniff_first step, bounded, for
    # names probe+sniff still could not board. This is the recovery path a
    # measured 60-company gap study found 5 of 6 eventual resolutions came
    # through (Eli Lilly, Fujifilm Diosynth Biotechnologies, Q2 Solutions,
    # ...) — names on gov/acronym/product-named domains the slug-guesser and
    # careers-page sniff can't reach on their own. Capped and skip-recent
    # because DDG rate-limits hard: an earlier uncapped profile blocked
    # ~1271s of a 1726s run in DDG's own retry/backoff (see ddg_text above).
    if websearch:
        cap = (config.DISCOVERY_WEBSEARCH_CAP if websearch_cap is None
              else websearch_cap)
        cap = _DEFAULT_WEBSEARCH_CAP if cap is None else int(cap)
        have = {_NONALNUM_RE.sub("", h["name"].lower()) for h in hits if h["nc"] > 0}
        todo = [n for n in names if _NONALNUM_RE.sub("", n.lower()) not in have]
        if todo and cap > 0:
            from core.store import connect as _connect, recent_miss_names
            conn = _connect()
            try:
                recent = recent_miss_names(conn, days=websearch_retry_days)
            finally:
                conn.close()
            todo = [n for n in todo if n not in recent][:cap]
        else:
            todo = []
        print(f"  websearch-resolving {len(todo)} name(s) without a board "
              f"(cap={cap})...")
        if todo:
            t0 = time.time()

            def _websearch_one(n):
                from scrapers.fetchers import company as company_fetch
                w = _websearch_board(n)
                if not w:
                    return {"name": n, "reason": "no-board-found"}
                ats = w["ats"]
                if ats == "workday":
                    t, p, site = w["triple"]
                    comp = {"ats": "workday", "wd_tenant": t, "wd_pod": p, "wd_site": site}
                    slug = (t, p, site)
                elif ats == "custom":
                    comp = {"ats": "custom", "careers_url": w.get("careers_url")}
                    slug = None
                else:
                    comp = {"ats": ats, "slug": w.get("slug"), "careers_url": w.get("careers_url")}
                    slug = w.get("slug")
                try:
                    jobs = company_fetch.fetch_company_nc(comp)
                except Exception:
                    jobs = []
                nc = len(jobs)
                return {"name": n, "ats": ats, "slug": slug, "count": nc, "nc": nc,
                        "careers_url": w.get("careers_url"),
                        "reason": "board-dead:" + ats}

            def _websearch_done(fut, n):
                h = fut.result()
                if h and h.get("nc"):
                    h.pop("reason", None)
                    hits.append(h)
                    print(f"    [WEBSEARCH] {h['name']:24} {h['ats']:14} "
                          f"{h['slug']!s:26} nc={h['nc']}")
                elif h:
                    sniff_misses.append(h)

            ex = ThreadPoolExecutor(max_workers=min(max_workers, len(todo)))
            _drain_or_abandon(
                ex, {ex.submit(_websearch_one, n): n for n in todo},
                _websearch_done,
                lambda n: sniff_misses.append(
                    {"name": n, "reason": "fetch-error:stalled"}))
            print(f"  websearch pass: {time.time() - t0:.1f}s for "
                  f"{len(todo)} name(s)")

    # Names that reached a live board under SOME spelling, before the
    # blocklist and the by-board dedup collapse them: they are accounted for
    # by their surviving row and must not also be filed as no-board-found.
    boarded = {h["name"] for h in hits}

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
    # Every candidate that is not confirmed is a MISS with a reason: a live
    # board with no local openings, a board whose coordinates read empty, or
    # a name nothing could be found for. Deduped by name, confirmed wins.
    misses = {}
    for h in dropped:
        misses[h["name"]] = {**h, "reason": "no-local-jobs"}
    for h in sniff_misses:
        misses.setdefault(h["name"], h)
    for n in names:
        if n not in boarded:
            misses.setdefault(n, {"name": n, "reason": "no-board-found"})
    for h in confirmed:
        misses.pop(h["name"], None)
    misses = sorted(misses.values(), key=lambda m: m["name"].lower())
    print(f"\n  live boards: {len(hits)}  |  NC-local confirmed: {len(confirmed)}  "
          f"|  dropped (no NC jobs): {len(dropped)}  "
          f"|  misses recorded: {len(misses)}")
    print("\n  --- NC-LOCAL CONFIRMED (nc jobs / total) ---")
    for h in confirmed:
        print(f"    [OK]   {h['name']:32} {h['ats']:10} {h['slug']!s:34} "
              f"{h['nc']}/{h['count']}")
    print("\n  --- DROPPED: live board but no NC jobs (likely wrong slug or non-local) ---")
    for h in dropped:
        print(f"    [drop] {h['name']:32} {h['ats']:10} {h['slug']!s:34} "
              f"0/{h['count']}")
    return confirmed, names, misses


# --------------------------------------------------------------------------- #
#  Config-ready output                                                         #
# --------------------------------------------------------------------------- #

from core.locality import NC_HQ_RE as _NC_HQ_RE  # "<Triangle city>, NC" HQ/office signal


def _hq_match_beyond_brand(text, name, hq_re=None):
    r"""True if `text` carries a "<place>, ST" match whose place is NOT just
    the company's own name.

    Garner Health is a New York company, but "Garner" is also a configured
    locality town — so every page of garnerhealth.com address-matched and
    the 2026-08-28 discover run activated it as NC-local with zero NC jobs.
    A match whose place tokens all appear in the company name is brand
    text; a match on any OTHER configured place still counts.

    `hq_re` defaults to the profile-derived locality pattern; the examples
    pass their own so they hold on any profile (the suite must pass on
    profile.example.toml, whose [locality] lists no Garner — a worktree
    without a personal profile failed this doctest on 2026-09-01):

    >>> pat = re.compile(r"\b(?:Garner|Durham),\s*NC\b")
    >>> _hq_match_beyond_brand("visit us in Garner, NC", "Garner Health", pat)
    False
    >>> _hq_match_beyond_brand("visit us in Garner, NC", "Acme Bio", pat)
    True
    >>> _hq_match_beyond_brand("HQ: Durham, NC", "Garner Health", pat)
    True
    >>> _hq_match_beyond_brand("no address here", "Acme Bio", pat)
    False
    """
    squashed = _NONALNUM_RE.sub("", (name or "").lower())
    for m in (hq_re or _NC_HQ_RE).finditer(text or ""):
        toks = re.findall(r"[a-z0-9]+", m.group(0).lower())
        place_toks = toks[:-1] or toks          # drop the state suffix token
        if all(t in squashed for t in place_toks):
            continue
        return True
    return False


def nc_hq_signal(name, careers_url="", board_jobs=None):
    """
    True if the company has a verifiable NC presence — used to TRACK local
    companies that currently have no NC openings. Checks the board's job
    locations first (cheap), then the company site/careers/contact pages.
    Page-text matches that are just the company's own brand name don't
    count (see _hq_match_beyond_brand); a JOB posted in a locality town is
    a genuine signal regardless of what the company is called.
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
            if r.status_code == 200 and _hq_match_beyond_brand(r.text, name):
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
    Every company new to the store lands in the REVIEW QUEUE — inactive,
    tagged pending-review (core.store.mark_pending) — so a bulk pass cannot
    put a name nobody vetted on the roster. Mission scoring still runs, so
    the reviewer sees the tier; `include_missions` only decides what
    confirming such a row activates.

    Finishes with the ATS-dork sweep (search-indexed board URLs) unless
    `dork=False` — run LAST on purpose: it consults the store and skips
    boards the name-based pass just added, so the two passes don't create
    duplicate rows for the same board. Dork adds are upserted directly and
    are NOT included in the returned list.

    Every candidate that did NOT become a company is written too, as an
    inactive row carrying a `miss_reason` (core.store.record_miss), so the
    failures are a queryable worklist instead of terminal scrollback.

    Returns the list of company dicts written by the name-based pass.
    """
    from core.store import (connect, is_confirmed_company, mark_pending,
                            miss_counts, record_miss, upsert_company)
    from core.claude import score_company_mission, is_active_mission

    confirmed, _, misses = discover_local(extra_names)
    conn = connect()
    written = []

    # Misses first: they are pure local writes, so the roster's failure
    # record survives even if the mission-scoring pass below is interrupted.
    n_miss = sum(record_miss(conn, m["name"], m["reason"], **_miss_row(m))
                 for m in misses)
    if misses:
        print(f"\n  recorded {n_miss} miss(es) (of {len(misses)} not "
              f"confirmed); store now holds: "
              + ", ".join(f"{fam}={n}" for fam, n in miss_counts(conn)))
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
            dup = _board_already_tracked(conn, row)
            if dup:
                _report_dup_board(h["name"], dup)
                continue
            # Nothing an automated pass finds joins the roster by itself: a
            # name the store has never confirmed lands in the review queue
            # (core.store.mark_pending) for a person to accept or reject.
            pending = not is_confirmed_company(conn, h["name"])
            if pending:
                row = mark_pending(row)
            upsert_company(conn, row)
            written.append(dict(row))
            flag = ("PENDING REVIEW" if pending
                    else "active" if active else "INACTIVE(other)")
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


def add_board(name, url, capture=False):
    """Register a board the user already knows — no guessing. `url` may be
    the ATS board itself (myworkdayjobs / greenhouse / lever / ...) or the
    company's careers page; coordinates are detected, the board NC-counted,
    mission-scored, and queued for review (the URL is the user's, but the
    coordinates under it were still sniffed).

        python discover.py --add-board "NC DHHS" https://nc.wd108.myworkdayjobs.com/NC_Careers

    `capture=True` registers a CAPTURE-ONLY company instead: nothing is
    sniffed or fetched, the row goes straight onto the roster (the URL is the
    user's own statement of where the board lives) with ats = "capture", and
    the crawl leaves it alone -- its pages are saved by hand with capture.py,
    which attributes them to this row by host. For boards that answer plain
    requests with a bot challenge or render their postings in JavaScript on a
    site with no ATS signature:

        python discover.py --add-board "Some Health System" https://jobs.example.org/ --capture

    Notes:
        A hosted PeopleAdmin tenant works here too — the university board
        URL carries the signature, and the row is keyed on the tenant
        origin whichever page of it you paste:

            python discover.py --add-board "UNC" https://unc.peopleadmin.com/postings/search

        A university serving PeopleAdmin from its OWN hostname
        (jobs.ncsu.edu) has no signature to detect, so there is nothing for
        this path to sniff; name the coordinates by hand and load them with
        --import-companies (see the PeopleAdmin bullet in README.md). Either
        way, nothing is fetched until the host is listed in the profile's
        [policy] robots_exempt_hosts.
    """
    from core.claude import score_company_mission
    from scrapers.fetchers import company as company_fetch
    from core.store import (CAPTURE_ATS, connect, is_confirmed_company,
                            mark_pending, upsert_company)
    from .sniffer import _detect, _pack, sniff_ats

    if capture:
        conn = connect()
        row = {"name": name, "ats": CAPTURE_ATS, "careers_url": url,
               "source": "manual", "active": 1,
               "notes": "capture-only board: browse it yourself and save "
                        "pages with capture.py --watch"}
        dup = _board_already_tracked(conn, row)
        if dup:
            _report_dup_board(name, dup)
            conn.close()
            return None
        upsert_company(conn, row)
        conn.close()
        print(f"  [OK] {name}: capture-only, {url}  -- save its pages with "
              f"capture.py --watch")
        return {"ats": CAPTURE_ATS, "careers_url": url}

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
    dup = _board_already_tracked(conn, {
        "name": name, "ats": ats,
        "slug": None if is_wd else (found.get("slug") or None),
        "wd_tenant": slug[0] if is_wd else None,
        "wd_pod":    slug[1] if is_wd else None,
        "wd_site":   slug[2] if is_wd else None,
        "careers_url": found.get("careers_url") or url})
    if dup:
        _report_dup_board(name, dup)
        conn.close()
        return None
    row = {
        "name": name, "ats": ats,
        "slug": None if is_wd else (found.get("slug") or None),
        "wd_tenant": slug[0] if is_wd else None,
        "wd_pod":    slug[1] if is_wd else None,
        "wd_site":   slug[2] if is_wd else None,
        "careers_url": found.get("careers_url") or url,
        "local_job_count": nc, "mission_tier": tier, "mission_score": score,
        "mission_reason": reason, "tags": company_tags.LOCAL if nc else None,
        "source": "manual", "active": 1,
    }
    # The URL is the user's, but the ATS coordinates under it were sniffed:
    # a careers page that links a shared/parent tenant resolves to somebody
    # else's board. One confirmation click covers both.
    pending = not is_confirmed_company(conn, name)
    if pending:
        row = mark_pending(row)
    upsert_company(conn, row)
    conn.close()
    ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
    print(f"  [OK] {name}: {ats} {slug!s}  nc={nc}  mission={tier} ({ss})  "
          f"{'PENDING REVIEW' if pending else 'ACTIVE'}")
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
    from .sniffer import _detect, _foreign_board, _pack

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
            # An own-page Workday embed can still be a parent conglomerate's
            # shared board (seqirus.com links to CSL's 'csl' tenant), which
            # would attribute every sibling company's jobs to this one —
            # same guard as the sniffer.
            if hit and hit[0] in ("fetchable", "semi") and (own or _slug_matches_name(hit[2], name)):
                if not (hit[1] == "workday" and _foreign_board(name, hit[2])):
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
            if (tier is not None and tier not in ACTIVE_MISSION_TIERS
                    and not config.is_multi_division(c["name"])
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
                # nothing about whether the board still resolves. Rows in the
                # review queue are excluded too: they are inactive because a
                # person has not confirmed them yet, not because a call
                # failed, and reviving them here would skip the queue (the
                # 2026-09-01 re-resolution pass queued 24 unscored rows that
                # this healer would otherwise have activated wholesale).
                if (not str(c.get("notes") or "").startswith("deactivated: dead")
                        and not company_tags.has(c.get("tags"), company_tags.PENDING)):
                    update["active"] = 1
                    revived = True
            upsert_company(conn, update)
            n += 1
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            flag = ("  -> deactivated (off-mission)"
                    if (tier is not None and tier not in ACTIVE_MISSION_TIERS)
                    else "  -> REACTIVATED (was unscored + inactive)" if revived
                    else "")
            print(f"    {c['name']:32} {str(tier):20} {ss}  ({reason}){flag}")
    conn.close()
    print(f"\n  {n} compan(ies) scored.")
    return n


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

    The only resolver the interactive paths use. It replaced a probe-first
    one that guessed slugs from the name before looking at the company's own
    site, which false-positived onto same-named but unrelated boards ('Oxford
    Biomedica' -> a different Oxford Workday tenant; 'Raya Health' -> the Raya
    dating app on Lever). Sniffing the company's OWN careers page can't
    collide that way, so it goes first; a probe-only hit is tagged
    ``via='probe'`` so the caller can flag it for a human sanity-check.

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
    # A `custom` sniff hit is held back rather than returned outright: a
    # marketing/careers page with no real ATS embedded still classifies as
    # `custom`, and a handful of scraped page fragments is enough for
    # _validate_board's total > 0 to pass (Pfizer/Sanofi/AstraZeneca/Syngenta/
    # Novozymes all resolved this way, each with a single-digit `total` that
    # was never their real Workday board). Only a `custom` hit that already
    # carries LOCAL jobs (nc > 0) is a genuine self-hosted board worth taking
    # immediately; an nc == 0 custom hit is kept as a last-resort fallback so
    # steps 2/3 get a chance to find the real ATS first.
    fallback = None
    s = sniff_ats(name, careers_url or "")
    if s:
        if s["ats"] == "workday":
            hit = _mk("workday", s["triple"], s.get("careers_url"), "sniff")
        elif s["ats"] == "custom":
            hit = _mk("custom", None, s.get("careers_url"), "sniff")
        else:
            hit = _mk(s["ats"], s.get("slug"), s.get("careers_url"), "sniff")
        if hit:
            if s["ats"] != "custom" or hit["nc"] > 0:
                return hit
            fallback = hit

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

    # Nothing better than the weak custom sniff turned up: it beats a miss.
    return fallback


def classify_miss(name, careers_url=""):
    """Second look at a name that would not resolve: which core.store
    MISS_REASONS code explains it.

    Re-sniffs the careers page for detections resolve_board_sniff_first
    discards — an ATS we can RECOGNIZE but not fetch (Taleo, Eightfold,
    Dayforce, ...) is a very different problem from a company we could find
    nothing for, and the two were previously indistinguishable.

    A bare "no-board-found" is itself four different problems (nothing
    resolves, the domain is dead, a careers page exists with no known ATS,
    or a candidate resolved to someone else's site) — sniffer.diagnose_no_board
    tells them apart, appended as the ':'-qualifier a rerun's miss_counts
    already knows how to aggregate past (see core.store.miss_family).

    Notes:
        Costs one extra careers-page sniff (plus diagnose_no_board's own,
        on the no-board-found path), so it is called only on the failure
        path and only by the on-demand resolvers — never per candidate in
        a full discover_local pass.
    """
    from .sniffer import sniff_careers_ats, ATS_LEAD_PATTERNS, diagnose_no_board
    try:
        lead = sniff_careers_ats(name, careers_url or "")
    except Exception as e:
        return f"fetch-error:{type(e).__name__}"
    if not lead:
        try:
            sub = diagnose_no_board(name, careers_url or "")
        except Exception:
            sub = ""
        return f"no-board-found:{sub}" if sub else "no-board-found"
    ats = lead.get("ats") or "?"
    if ats in {a for a, _ in ATS_LEAD_PATTERNS}:
        return f"ats-unsupported:{ats}"
    return f"board-dead:{ats}"


def resolve_or_miss(name, careers_url=""):
    """Resolve a company NAME to a crawlable board, or say why it failed.

    Returns ``(hit, reason)``. A hit with no reason is usable; a reason with
    no hit is a failed resolution (see classify_miss); a hit WITH a reason is
    a live, readable board that simply has no openings in your [locality]
    (``no-local-jobs``) — worth keeping, not worth crawling today.

    Notes:
        The single entry point for "attempt a company, and record the
        outcome either way". Callers persist the reason with
        core.store.record_miss so a rerun can skip, retry or report it.
    """
    try:
        hit = resolve_board_sniff_first(name, careers_url or "")
    except Exception as e:
        return None, f"fetch-error:{type(e).__name__}"
    if not hit:
        return None, classify_miss(name, careers_url)
    if not hit.get("nc"):
        return hit, "no-local-jobs"
    return hit, None


def _miss_row(m):
    """The record_miss(**fields) payload for a discover_local miss dict:
    whatever board coordinates the attempt DID establish, so a retry starts
    from them instead of re-deriving them.

    >>> _miss_row({"name": "X", "reason": "no-board-found"})
    {'source': 'local_sourcing'}
    >>> _miss_row({"name": "X", "ats": "greenhouse", "slug": "x",
    ...            "nc": 0, "count": 4, "reason": "no-local-jobs"})["ats"]
    'greenhouse'
    >>> _miss_row({"name": "X", "ats": "workday", "slug": ("t", 5, "s"),
    ...            "reason": "no-local-jobs"})["wd_tenant"]
    't'
    """
    row = {"source": "local_sourcing"}
    ats = m.get("ats")
    if not ats:
        return row
    row["ats"] = ats
    if ats == "workday" and isinstance(m.get("slug"), tuple):
        row["wd_tenant"], row["wd_pod"], row["wd_site"] = m["slug"]
    elif m.get("slug"):
        row["slug"] = m["slug"]
    if m.get("careers_url"):
        row["careers_url"] = m["careers_url"]
    if m.get("count"):
        row["total_job_count"] = m["count"]
    return row


def resolve_leads(max_workers=8,
                  sources=("page_capture", "linkedin_search", "linkedin_company_search"),
                  all_leads=False, limit=None, retry_days=14):
    """Resolve boardless company leads (banked by capture.py from browsed
    LinkedIn/Indeed pages, or by manual adds) into crawlable boards and queue
    the hits for review. Careers-page SNIFF first (collision-safe), slug-probe
    fallback, every board VALIDATED by a live fetch, then mission-scored.
    The capture -> resolve-leads -> review -> crawl loop is how manually
    browsed postings grow the roster.

    sources: resolve only leads carrying one of these ``source`` values
    (default: capture.py's 'page_capture'). all_leads=True ignores the source
    filter and takes every inactive boardless lead. Idempotent — rerunning
    retries only the still-unresolved leads."""
    from core.claude import score_company_mission, is_active_mission
    from core.store import (connect, get_companies as _store_companies,
                            is_confirmed_company, mark_pending, record_miss,
                            recent_miss_names, upsert_company)

    conn = connect()
    leads = [c for c in _store_companies(conn, active_only=False)
             if not c.get("ats") and not c.get("active")]
    if not all_leads:
        leads = [c for c in leads if c.get("source") in sources]
    # Skip leads that failed recently: without this every rerun re-probes
    # every permanent miss, and the pass gets slower the longer it runs.
    # retry_days=0 (or --all-leads) retries the lot.
    if retry_days and not all_leads:
        recent = recent_miss_names(conn, days=retry_days)
        skipped_recent = [c for c in leads if c["name"] in recent]
        leads = [c for c in leads if c["name"] not in recent]
        if skipped_recent:
            print(f"  skipping {len(skipped_recent)} lead(s) that missed in "
                  f"the last {retry_days}d (--all-leads to retry them)")
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
    futs = {ex.submit(resolve_or_miss, c["name"], c.get("careers_url") or ""): c
            for c in leads}
    try:
        for fut in as_completed(futs, timeout=300):
            c, (hit, reason) = futs[fut], fut.result()
            if not hit:
                # Was printed and forgotten; now the lead row keeps WHY, so
                # the next run can skip it and the user can see the tally.
                record_miss(conn, c["name"], reason, source=c.get("source"))
                print(f"    [miss] {c['name'][:34]:34} {reason}")
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
            dup = _board_already_tracked(conn, row)
            if dup:
                _report_dup_board(c["name"], dup)
                continue
            # A lead is a name somebody's page mentioned, not an employer
            # anyone vouched for: resolving it produces a review candidate.
            pending = not is_confirmed_company(conn, c["name"])
            if pending:
                row = mark_pending(row)
            upsert_company(conn, row)
            resolved.append(row)
            if hit.get("via") == "probe":
                probe_only.append(c["name"])
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            flag = "  [probe-only: verify]" if hit.get("via") == "probe" else ""
            mark = "queue" if pending else ("OK  " if active else "off ")
            print(f"    [{mark}] {c['name'][:30]:30} "
                  f"{hit['ats']:12} nc={hit['nc']:<3} tot={hit['count']:<4} "
                  f"{str(tier):18} {ss}{flag}")
    except TimeoutError:
        stuck = [c["name"] for f, c in futs.items() if not f.done()]
        for n in stuck:
            record_miss(conn, n, "fetch-error:Timeout")
        print(f"    [!] timed out on {len(stuck)} lead(s): "
              f"{', '.join(stuck[:6])}{'...' if len(stuck) > 6 else ''} "
              f"(rerun --resolve-leads to retry — idempotent)")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    conn.close()
    queued = sum(1 for r in resolved
                 if company_tags.has(r.get("tags"), company_tags.PENDING))
    print(f"\n  {len(resolved)} board(s) resolved, "
          f"{queued} awaiting review, "
          f"{sum(r['active'] for r in resolved)} activated, "
          f"{len(leads) - len(resolved)} miss(es).")
    if probe_only:
        print(f"  [verify] {len(probe_only)} resolved by name-guess, not the "
              f"company's own site — sanity-check for collisions: "
              f"{', '.join(probe_only[:6])}{'...' if len(probe_only) > 6 else ''}")
    return resolved


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
    r"over \d+ applicants|(?:page\s*)?\d* *of *\d+|see all.*|show all.*|"
    r"\$.*|"                     # any $-leading line: salary in every format
    # LinkedIn company/profile page stat lines ("11 results", "615 on
    # LinkedIn", "501-1000 employees", "2 year growth", "42Fair Match",
    # "4 connections work here", "75% have a Doctor of Philosophy")
    r"[\d,\-–]+\s*(?:results?|notifications?|on linkedin|"
    r"year growth|fair match|employees?)|"
    r"\d+\s*(?:company |school )?(?:alumni|connections?)\s+works? here.*|"
    r"\d+%.*|"
    r"401\(k\).*|"
    r"in the past \w+|"
    r".*(?:©|\(c\)|�)\s*\d{4}.*|.*\bcorporation\b\W*\d{4}"
    r")\W*$", re.I)

# A line that reads as a JOB TITLE rather than an employer. Results pages
# interleave the two, and a title resolves to nothing, so this saves the probe.
_TITLE_WORD_RE = re.compile(
    r"\b(?:engineer|scientist|developer|analyst|manager|director|specialist|"
    r"coordinator|associate|assistant|technician|architect|consultant|intern|"
    r"lead|head of|vp|president|officer|administrator|nurse|physician|"
    r"recruiter|designer|researcher|postdoc|fellow|programmer|"
    r"(?:bio)?statistician)\b",
    re.I)

# "Durham, NC" / "Durham, NC (Hybrid)" / "Raleigh-Durham-Chapel Hill Area" /
# "North Carolina, United States (Remote)" — the region after the comma may
# be several words, and a clipped paste can truncate the trailing "(Remote)"
# to "(R", so the closing paren is optional.
_LOCATION_LINE_RE = re.compile(
    r"^[A-Z][\w.'-]+(?:[ \-][\w.'-]+)*,\s*"
    r"(?:[A-Z]{2}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
    r"(?:\s*\([^)]*\)?)?$|.*\bArea$|.*\bMetropolitan\b", re.I)


# A results row whose job title is rendered twice, optionally with a badge
# wedged between the halves: "Signal Processing Engineer (Verified job)Signal
# Processing Engineer". LinkedIn emits this for every hit, and the employer is
# always the next line — so the doubled line is an unambiguous "company below"
# marker. `.{4,}?` is lazy so the shortest repeating half wins.
_DOUBLED_TITLE_RE = re.compile(r"^(.{4,}?)(?:\s*\([^)]{,24}\))?\1$")


# Words that stay lowercase in a real Title-Case name ("Bank of America",
# "University of North Carolina", "Bausch + Lomb") and so don't count as
# evidence either way when judging capitalisation.
_CASE_STOPWORDS = {
    "of", "and", "for", "the", "a", "an", "in", "at", "to", "on", "or",
    "&", "+", "de", "la", "le", "van", "von",
}


def _is_sentence_case(name):
    """True if a MULTI-WORD `name` reads as sentence-case UI prose (only the
    first significant word capitalised -- "Date posted", "Salary estimate")
    rather than a Title-Case company name (every significant word
    capitalised -- "Alpaca Health", "University of North Carolina").

    Single-token names are never flagged: a legitimately-lowercase name
    like "bioMerieux" or "nCino" has no second word to compare against, so
    there is no sentence-vs-title signal to read.

    >>> _is_sentence_case("Date posted")
    True
    >>> _is_sentence_case("Skip to main content")
    True

    A capitalised CONNECTOR first word still starts the shape — these two
    slipped through when the check skipped straight to the first
    significant word ("my" / "latest") and read its lowercase as
    "doesn't even start capitalised":

    >>> _is_sentence_case("In my network")
    True
    >>> _is_sentence_case("The latest hiring trend")
    True
    >>> _is_sentence_case("Alpaca Health")
    False
    >>> _is_sentence_case("University of North Carolina")
    False
    >>> _is_sentence_case("bioMerieux")
    False

    Notes:
        A name reduced to one significant word after stripping connector
        words ("Bank of X" with X itself lowercase, or a bare "X of") is
        left alone -- one word is not enough evidence to call sentence
        case, and a false reject here is a lost real company, not a
        dropped chrome line.
    """
    def _is_connector(w):
        # A word only counts as one of the fixed CASE_STOPWORDS after
        # stripping trailing punctuation, NOT the symbols that ARE
        # stopwords ("+", "&") -- stripping those first would zero the
        # token out and hide it from the membership check. A token with no
        # letters at all (a bare number, "+", "&") never carries a case
        # signal either way, so it's a connector too.
        core = w.lower().strip(".,’")
        return core in _CASE_STOPWORDS or not any(c.isalpha() for c in w)

    words = name.split()
    if len(words) < 2:
        return False
    significant = [w for w in words if not _is_connector(w)]
    if len(significant) < 2:
        return False

    def _starts_upper(w):
        core = w.lstrip("(\"'")
        return bool(core) and core[0].isalpha() and core[0].isupper()

    # Sentence case is defined by its shape: capitalised FIRST word, lower-
    # case rest. A phrase that doesn't even start capitalised ("bla bla")
    # isn't in that shape either way, so there's nothing to flag -- this
    # also keeps a completely-lowercase throwaway phrase from being read as
    # "sentence case" when it's really just not a name at all. The shape
    # starts at the LITERAL first word, connector or not: "In my network"
    # begins capitalised even though "In" carries no case signal itself.
    if not _starts_upper(words[0]):
        return False
    rest = significant[1:] if significant[0] == words[0] else significant
    if not rest:
        return False
    return any(not _starts_upper(w) for w in rest)


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
    if _PASTE_NOISE_RE.match(name) or _is_nav_noise(name):
        return None
    if drop_titles and _TITLE_WORD_RE.search(name):
        return None
    if drop_titles and " / " in name:
        return None        # breadcrumb/CTA pair ("Employers / Post Job"), not a name
    if drop_titles and _is_sentence_case(name):
        return None        # sentence-case UI prose ("Date posted"), not Title Case
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

    Otherwise fall back to filtering lines. Permissiveness is no longer
    cheap: a junk name that reaches the resolver costs a full sniff ->
    probe -> websearch chain, is PERSISTED as a miss row either way
    (add_names records every unresolved name), and a generic word can
    websearch-resolve to an unrelated real company's board (2026-08-28:
    "Biotech" landed on Dianthus Therapeutics' greenhouse board, ACTIVE).
    So page chrome, stat lines, sector labels and JD section headers are
    filtered out on sight; a dropped real name is still the rarer, cheaper
    mistake, so the filters key on shapes no employer name takes:

    >>> parse_company_names('''Home
    ... My Network
    ... Jobs
    ... Messaging
    ... Notifications
    ... For Business
    ... Create cover letter
    ... Learning
    ... People you can reach out to
    ... Senior Data Engineer
    ... Alpaca Health
    ... Durham, NC (Hybrid)
    ... IQVIA
    ... Durham, NC''')
    ['Alpaca Health', 'IQVIA']
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


def _board_already_tracked(conn, row):
    """The roster company that already owns `row`'s board under ANOTHER
    name, or None. Every discovery path checks names before resolving, but
    a name the roster spells differently ("SAS" for "SAS Institute", "Veeva
    Systems" for "Veeva", "NVIDIA AI" for "NVIDIA" — all re-added on
    2026-09-01) passes that check and then resolves to a board that is
    already on file; until the next dedup the crawl fetched the board twice
    and the ranking showed two companies. Same-name matches are NOT dups —
    that is the ordinary re-probe/update path — so the caller may upsert."""
    from core.store import company_by_board
    existing = company_by_board(conn, row)
    if not existing:
        return None
    if (_NONALNUM_RE.sub("", (existing.get("name") or "").lower())
            == _NONALNUM_RE.sub("", (row.get("name") or "").lower())):
        return None
    return existing


def _report_dup_board(name, existing):
    print(f"    [dup]  {name[:30]:30} same {existing.get('ats') or '?'} board "
          f"as '{existing.get('name')}' - already tracked, not added")


# A name the store already has a BOARD for is not worth resolving again.
# Miss rows are excluded on purpose: they carry an `ats` when the board was
# found but rejected (no local jobs, dead board), and a re-paste of such a
# name should be allowed to try again.
_TRACKED_NAMES_SQL = ("SELECT name FROM companies "
                      "WHERE ats IS NOT NULL AND miss_reason IS NULL")


def _blocked_keys(conn):
    """Normalized name keys no path may add: the store's rejection blocklist
    (core.store.blocked_name_keys) union the profile's [discovery]
    name_blocklist."""
    from core.store import blocked_name_keys
    return blocked_name_keys(conn) | set(NAME_BLOCKLIST)


def _name_state(key, tracked, blocked, missed):
    """Which review bucket a parsed name falls in, given the three key sets
    the store answers with.

    A name nothing on file knows about is the one worth spending requests on:

    >>> _name_state("acmebio", set(), set(), set())
    'new'

    Anything already answered for is not:

    >>> _name_state("acmebio", {"acmebio"}, set(), set())
    'tracked'
    >>> _name_state("oncology", set(), {"oncology"}, set())
    'blocked'
    >>> _name_state("acmebio", set(), set(), {"acmebio"})
    'missed'

    Blocked beats tracked beats missed, so a rejected name still reads as
    rejected when a stale row or miss stamp mentions it too:

    >>> _name_state("x", {"x"}, {"x"}, {"x"})
    'blocked'
    >>> _name_state("x", {"x"}, set(), {"x"})
    'tracked'
    """
    if key in blocked:
        return "blocked"
    if key in tracked:
        return "tracked"
    if key in missed:
        return "missed"
    return "new"


def preview_names(blob, use_llm=None):
    """A pasted page -> the list a person ticks through before anything is
    resolved: ``[{"name", "key", "state"}]``, one entry per distinct name, in
    the order they appear, with `state` from `_name_state`.

    Notes:
        Step one of the two-step paste flow, and the reason it exists: one
        pasted page produced 15 names that were never employers, and
        resolving them cost about a thousand HTTP requests before four of
        them landed on the roster with real boards. Nothing here resolves
        and nothing here writes -- add_names() takes the confirmed list.

        `use_llm=None` (the default) runs extract_names_llm whenever an API
        key is configured and falls back to parse_company_names when it
        returns nothing; True forces the model, False the regex parser.
    """
    from core.store import connect, recent_miss_names
    if use_llm is None:
        use_llm = config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY_HERE"
    names = extract_names_llm(blob) if use_llm else []
    if not names:
        names = parse_company_names(blob)
    conn = connect()
    try:
        tracked = {_NONALNUM_RE.sub("", (r["name"] or "").lower())
                   for r in conn.execute(_TRACKED_NAMES_SQL).fetchall()}
        blocked = _blocked_keys(conn)
        missed = {_NONALNUM_RE.sub("", n.lower())
                  for n in recent_miss_names(conn)}
    finally:
        conn.close()
    out, seen = [], set()
    for n in names:
        key = _NONALNUM_RE.sub("", n.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": n, "key": key,
                    "state": _name_state(key, tracked, blocked, missed)})
    return out


def add_names(names, use_llm=False, max_workers=6, include_missions=None):
    """Resolve company names to boards and queue the ones that verify.

    `names` is the list of names a person confirmed in the review step. A raw
    blob is still accepted -- preview_names() parses it and the `new` names go
    forward -- so the CLI and any single-step caller keep working.

    Uses resolve_board_sniff_first(), not the slug-probe-first resolver: a name
    a person pasted is exactly where a slug collision does the most damage
    (guessing 'sas' lands on an unrelated 5-job board while the real SAS
    Institute sits on iCIMS). Sniffing the company's own careers page cannot
    collide that way; a probe-only hit is reported for a human glance.

    Everything written lands in the review queue (core.store.mark_pending),
    never straight onto the roster.
    """
    from core.claude import score_company_mission, is_active_mission
    from core.store import (connect, is_confirmed_company, mark_pending,
                            record_miss, upsert_company)

    if isinstance(names, (str, bytes)):
        names = [n["name"] for n in preview_names(names, use_llm=use_llm)
                 if n["state"] == "new"]
    else:
        names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        print("  no company names to resolve.")
        return []

    conn = connect()
    skip = ({_NONALNUM_RE.sub("", (r["name"] or "").lower())
             for r in conn.execute(_TRACKED_NAMES_SQL).fetchall()}
            | _blocked_keys(conn))
    fresh = [n for n in names if _NONALNUM_RE.sub("", n.lower()) not in skip]
    skipped = len(names) - len(fresh)
    print(f"  {len(names)} name(s) given"
          + (f", {skipped} already tracked or blocked" if skipped else "")
          + f" -> resolving {len(fresh)}...")

    written, unresolved = [], []

    def _stalled(n):
        record_miss(conn, n, "fetch-error:stalled", source="paste")
        unresolved.append((n, "fetch-error:stalled"))

    def _consume(fut, name):
        try:
            hit, reason = fut.result()
        except Exception as e:
            print(f"    [!] {name}: {type(e).__name__}: {e}")
            hit, reason = None, f"fetch-error:{type(e).__name__}"
        if not hit:
            # A pasted name that resolves to nothing used to be printed
            # once and lost; keep it with a reason so the paste is a
            # worklist, not a one-shot.
            record_miss(conn, name, reason, source="paste")
            unresolved.append((name, reason))
            return
        slug = hit.get("slug")
        is_wd = hit["ats"] == "workday"
        row = {"name": hit["name"], "ats": hit["ats"],
               "slug": None if is_wd else slug,
               "wd_tenant": slug[0] if is_wd else None,
               "wd_pod":    slug[1] if is_wd else None,
               "wd_site":   slug[2] if is_wd else None,
               "careers_url": hit.get("careers_url")}
        dup = _board_already_tracked(conn, row)
        if dup:
            _report_dup_board(hit["name"], dup)
            return
        titles = _sample_titles(hit)
        tier, score, reason = score_company_mission(
            hit["name"], " | ".join(t for t in titles if t))
        active = is_active_mission(tier, hit["name"], include_missions)
        row.update({
            "local_job_count": hit["nc"], "total_job_count": hit["count"],
            "mission_tier": tier, "mission_score": score,
            "mission_reason": reason,
            "tags": company_tags.LOCAL if hit["nc"] else None,
            "source": "paste", "active": active,
            "last_probed": datetime.now().isoformat(),
        })
        pending = not is_confirmed_company(conn, hit["name"])
        if pending:
            row = mark_pending(row)
        upsert_company(conn, row)
        written.append(hit)
        # resolve_board_sniff_first's `via` says HOW the board was found:
        # 'sniff' read it off the company's own careers page, 'probe' guessed
        # a slug from the name, 'websearch' only means some result URL
        # matched. The weakest two used to be corroborated (or written
        # inactive) here; the review queue is that check now, and it shows
        # the reviewer which one they are looking at.
        flag = {"probe": "  [slug-guess]",
                "websearch": "  [websearch match]"}.get(hit.get("via"), "")
        state = ("pending review" if pending
                 else "active" if active else "inactive")
        print(f"    [{'queue' if pending else ' ok  '}] {hit['name'][:30]:30} "
              f"{hit['ats']:12} {hit['nc']}/{hit['count']:<5} {str(tier):20} "
              f"{state}{flag}")

    ex = ThreadPoolExecutor(max_workers=max_workers)
    _drain_or_abandon(ex, {ex.submit(resolve_or_miss, n): n for n in fresh},
                      _consume, _stalled)
    conn.commit()
    conn.close()
    if unresolved:
        print(f"\n  {len(unresolved)} name(s) did not resolve to a live board "
              f"(kept as misses — see the companies table's miss_reason):")
        print("    " + ", ".join(f"{n} [{r}]" for n, r in unresolved[:25])
              + (" ..." if len(unresolved) > 25 else ""))
    print(f"\n  {len(written)} compan(ies) queued for review.")
    return written
