"""Where candidate employer NAMES come from, before any of them is resolved
to a board.

Profile seeds and Workday majors, `/company/<slug>/` links on directory and
listicle pages (RTP.org, Built In, chamber directories), web-search
harvesting of such pages, and an LLM brainstorm of the profile's region and
domain, unioned by gather_names. Every name still has to survive the
resolve -> validate -> score chain in local_sourcing, so noise here costs
requests rather than roster rows -- which is why the shape filters
(_looks_like_company, _is_nav_noise) run first.
"""

import re

from src import config

from src.match.names import name_key
from src.net import ddg
from src.net.http import HEADERS, SESSION


# Seed employers + Workday-fallback majors + drop-list all come from the
# active profile ([discovery]) so sourcing generalizes to any region/domain.

SEED_COMPANIES = config.DISCOVERY_SEED_NAMES   # names only; seeds.py keeps notes
MAJORS_WORKDAY = config.DISCOVERY_WORKDAY_MAJORS
_MAJORS_KEYS = {name_key(m) for m in MAJORS_WORKDAY}
NAME_BLOCKLIST = config.DISCOVERY_NAME_BLOCKLIST


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


def scrape_directory_names(url, timeout=config.FETCH_TIMEOUT):
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
        for r in ddg.search(q, max_results=per_query):
            u = r.get("href") or r.get("url") or ""
            if u and any(h in u.lower() for h in _DIR_HOSTS):
                dir_urls.append(u)

    names = set()
    for u in list(dict.fromkeys(dir_urls))[:fetch_dirs]:
        try:
            html = SESSION.get(u, timeout=config.FETCH_TIMEOUT, headers=HEADERS).text
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
    cached = ddg.cache_get(key)
    if cached is not None:
        return cached
    from src.claude.api import call_claude_json
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
        ddg.cache_put(key, names)
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
            k = name_key(n)
            if k and k not in seen:
                seen.add(k)
                names.append(n.strip())
    return names
