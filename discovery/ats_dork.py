"""
ATS "dorking" — find crawlable NC companies by mining search-engine-indexed
ATS board URLs, instead of guessing a board from a company name.

A search like `site:jobs.lever.co "Durham"` returns board URLs whose slug we
can read directly (e.g. jobs.lever.co/<slug>). We extract the ATS + slug/triple
from each URL, NC-verify the board, mission-score it, and add it.

Two entry points:
  * run_ddgs_dorks()  — fully automated via the ddgs package (DuckDuckGo).
"""

import json
import time

import config

from .sniffer import _SIGS
from .probes import _extract_workday_triple
from core import store, tags as company_tags
from scrapers.fetchers import company as company_fetch
from core.claude import is_active_mission, score_company_mission
from .local_sourcing import _sample_titles, nc_hq_signal


def _or_group(terms, n=8):
    """A `("a" OR b OR "c d")` search clause from profile terms (multi-word
    terms quoted). Empty string when there are no terms."""
    picked = [t for t in terms[:n] if t]
    if not picked:
        return ""
    return "(" + " OR ".join(f'"{t}"' if " " in t else t for t in picked) + ")"


_LOCALITY_TERMS = config.LOCALITY_SUBSTRINGS or config.LOCALITY_WORD_TOKENS
_DOMAIN = _or_group(config.DOMAIN_KEYWORDS, n=6)
_CORE = _or_group(config.CORE_KEYWORDS, n=6)


def _rotate_terms(terms, group_size, index):
    """A deterministic, cyclically-rotating slice of `terms`, `group_size`
    long, for rotation `index` (0, 1, 2, ...). Successive indices advance
    through the whole list instead of always returning the same head, so
    repeated dork sweeps cover different locality vocabulary — reproducibly:
    the same (terms, group_size, index) always returns the same slice.

    >>> _rotate_terms(["a", "b", "c", "d", "e", "f"], 2, 0)
    ['a', 'b']
    >>> _rotate_terms(["a", "b", "c", "d", "e", "f"], 2, 1)
    ['c', 'd']
    >>> _rotate_terms(["a", "b", "c", "d", "e", "f"], 2, 2)
    ['e', 'f']

    The index wraps around once every term has had a turn:

    >>> _rotate_terms(["a", "b", "c", "d", "e", "f"], 2, 3)
    ['a', 'b']

    A `group_size` larger than the list is clamped, and an empty list of
    terms is a no-op:

    >>> _rotate_terms(["a", "b"], 5, 0)
    ['a', 'b']
    >>> _rotate_terms([], 4, 7)
    []
    """
    if not terms:
        return []
    n = len(terms)
    group_size = min(group_size, n)
    start = (index * group_size) % n
    return [terms[(start + i) % n] for i in range(group_size)]


def build_dork_queries(rotation=0):
    """The dork query set for rotation index `rotation`. DDG chokes on long
    `site:` + big OR-group queries (returns nothing), so the site-scoped
    dorks use a SHORT locality clause (top few terms of that rotation's
    slice); the free-text sweep can afford more. `rotation=0` is the
    original fixed top-4/top-8 selection; each further index rotates onto
    the next slice of the profile's locality vocabulary (see
    `_rotate_terms`), so successive sweeps explore beyond the same 25
    top-ranked results DDG would otherwise return for an unchanging query.

    >>> qs = build_dork_queries(0)
    >>> any("greenhouse" in q for q in qs)
    True
    >>> any("icims" in q for q in qs)
    True

    Rotating changes which locality terms the site-scoped dorks carry
    (assuming the profile has more than 4 locality terms configured):

    >>> build_dork_queries(0) != build_dork_queries(1)
    True
    """
    loc_site = _or_group(_rotate_terms(_LOCALITY_TERMS, 4, rotation), n=4)
    loc_wide = _or_group(_rotate_terms(_LOCALITY_TERMS, 8, rotation), n=8)
    queries = [
        f'site:boards.greenhouse.io {loc_site}',
        f'site:job-boards.greenhouse.io {loc_site}',
        f'site:jobs.lever.co {loc_site}',
        f'site:jobs.ashbyhq.com {loc_site}',
        f'site:jobs.smartrecruiters.com {loc_site}',
        f'site:jobs.jobvite.com {loc_site}',
        f'site:*.icims.com {loc_site}',
        f'site:*.bamboohr.com/careers {loc_site}',
        f'"myworkdayjobs.com" {loc_site}' + (f" {_DOMAIN}" if _DOMAIN else ""),
    ]
    if _CORE:
        # Bullseye sweep — target companies are often on custom boards /
        # non-.com domains that name-guessing misses.
        queries.append(f'{_CORE} {loc_wide} (careers OR jobs OR hiring)')
    return [q for q in queries if loc_site and loc_site in q or _CORE and _CORE in q]


# Module-level default (rotation 0) — unchanged shape from before rotation was
# added, so existing callers that just want "the dork queries" keep working.
DORK_QUERIES = build_dork_queries(0)

# Non-slug path fragments the greenhouse/embed URL forms expose — never a real
# board (boards.greenhouse.io/embed/job_board?for=<realslug>).
_SLUG_STOP = {"embed", "job_board", "jobs", "js", "boards", "job-boards",
              "www", "careers", "search", "api"}


def extract_boards_from_urls(urls):
    """From a list of URLs, return de-duped [(ats, slug|triple)] board handles.

    List in, list out: one handle per distinct board, in first-seen order.

    >>> extract_boards_from_urls(["https://boards.greenhouse.io/acmebio/jobs/1",
    ...                           "https://jobs.lever.co/acmebio/abc-def"])
    [('greenhouse', 'acmebio'), ('lever', 'acmebio')]

    The same board reached by several URLs collapses to one handle — a dork
    sweep returns dozens of job links per board:

    >>> extract_boards_from_urls(["https://boards.greenhouse.io/acmebio/jobs/1",
    ...                           "https://boards.greenhouse.io/acmebio/jobs/2",
    ...                           "https://boards.greenhouse.io/acmebio"])
    [('greenhouse', 'acmebio')]

    Workday handles are the (tenant, pod, site) triple, not a slug:

    >>> extract_boards_from_urls(["https://acme.wd5.myworkdayjobs.com/en-US/External"])
    [('workday', ('acme', 5, 'External'))]

    URLs that are not boards contribute nothing, so an all-noise input is
    an empty list rather than a list of bad handles:

    >>> extract_boards_from_urls(["https://example.com/careers",
    ...                           "https://www.linkedin.com/jobs/view/123"])
    []
    >>> extract_boards_from_urls([])
    []
    """
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
    queue them for review. Returns (added, checked).
    Company name is provisionally the slug (real name can be refined later);
    mission scoring uses the board's live job titles for domain context.

    A dorked board is the weakest-sourced candidate in the codebase -- a
    search engine indexed a URL and the company NAME is a de-hyphenated slug
    -- so every row lands in the review queue (core.store.mark_pending)
    rather than on the roster.
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
        # Shared activation rule (core.claude.is_active_mission) — the same
        # call every other add path makes. An inactive row is near-
        # unrecoverable here: harvest_urls skips boards already in the store,
        # so the company is never re-probed.
        active = is_active_mission(tier, name)
        row = dict(
            name=name, ats=ats, slug=slug if ats != "workday" else None,
            wd_tenant=slug[0] if ats == "workday" else None,
            wd_pod=slug[1] if ats == "workday" else None,
            wd_site=slug[2] if ats == "workday" else None,
            local_job_count=nc, total_job_count=nc, mission_tier=tier,
            mission_score=score, mission_reason=reason, tags=company_tags.LOCAL,
            source="ats_dork", active=active)
        pending = not store.is_confirmed_company(conn, name)
        if pending:
            row = store.mark_pending(row)
        store.upsert_company(conn, row)
        added += 1
        if verbose:
            state = ("PENDING" if pending
                     else "ACTIVE" if active else "inactive")
            print(f"  {name[:26]:26} {ats:12} nc={nc:2} {str(tier):19} "
                  f"{score if score else 0:.2f} {state}")
    return added, len(boards)


# ddgs builds its engine registry by WALKING ITS OWN PACKAGE DIRECTORY
# (pkgutil.iter_modules in ddgs/engines/__init__.py). A compiled build has no
# directory to walk, so the registry comes up empty and every search dies on
# `ENGINES["text"]` — a bare KeyError('text'), raised before ddgs's own backend
# error handling can run, so the whole sweep silently returns 0 results.
# Fallback list only — a new ddgs release can add an engine this misses, which
# costs that one backend rather than the whole sweep.
_DDGS_ENGINE_MODULES = (
    "annasarchive", "bing", "bing_images", "bing_news", "brave", "duckduckgo",
    "duckduckgo_images", "duckduckgo_news", "duckduckgo_videos", "google",
    "grokipedia", "mojeek", "startpage", "wikipedia", "yahoo", "yahoo_news",
    "yandex",
)


def _ensure_ddgs_engines():
    """Re-register ddgs's search engines when its own discovery came up empty.
    No-op on a normal source run."""
    import importlib
    import inspect
    try:
        from ddgs.base import BaseSearchEngine
        from ddgs.engines import ENGINES
    except Exception:
        return
    if ENGINES.get("text"):
        return
    for modname in _DDGS_ENGINE_MODULES:
        try:
            module = importlib.import_module(f"ddgs.engines.{modname}")
        except Exception:
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (not issubclass(cls, BaseSearchEngine) or cls is BaseSearchEngine
                    or cls.__name__.startswith("Base")
                    or getattr(cls, "disabled", True)):
                continue
            name, category = getattr(cls, "name", None), getattr(cls, "category", None)
            if isinstance(name, str) and isinstance(category, str):
                ENGINES.setdefault(category, {})[name] = cls


def _ddg_text(query, max_results, retries=2, pause=2.5, page=1):
    """One DDG query with retry/backoff. DDG rate-limits aggressively and
    surfaces it as an exception ("No results found."/"Ratelimit"), so a fresh
    session + a pause between attempts recovers far more than a single try.
    `page` (1-based) asks the backend for a later results page — the lever
    for getting past DDG's default top-`max_results` ceiling — and is passed
    straight through to ddgs, which forwards unrecognised kwargs to the
    underlying engine. Returns a list of result URLs (possibly empty)."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        return []
    _ensure_ddgs_engines()
    for attempt in range(retries + 1):
        try:
            with DDGS() as ddg:
                return [u for r in ddg.text(query, max_results=max_results, page=page)
                        if (u := (r.get("href") or r.get("url")))]
        except Exception as e:
            if attempt < retries:
                time.sleep(pause * (attempt + 1))
                continue
            # "No results found." is DDG's way of returning an empty page
            # (and sometimes a disguised throttle — hence the retries above);
            # once the retries are spent it's an expected empty, and the
            # "[dork] 0 result(s)" line that follows already reports it.
            if "no results" in str(e).lower():
                return []
            # Type included: a bare KeyError prints as just its key ('text'),
            # which reads like a parsing quirk rather than a dead registry.
            print(f"  [!] dork {query[:48]}...: {type(e).__name__}: {e}")
    return []


# Persisted rotation counter, so successive runs advance through the locality
# vocabulary instead of repeating the same slice (and a re-read reproduces
# exactly which slice a past run covered) — deterministic, not `random`-based.
_ROTATION_STATE_PATH = config.DATA_DIR / ".cache" / "dork_rotation.json"


def _next_rotation_index():
    """Read-then-increment the persisted rotation counter. Best-effort: a
    read/write failure just falls back to index 0 (the original fixed
    top-4/top-8 query set) rather than crashing the sweep."""
    try:
        idx = int(json.loads(_ROTATION_STATE_PATH.read_text("utf-8")).get("index", 0))
    except Exception:
        idx = 0
    try:
        _ROTATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ROTATION_STATE_PATH.write_text(json.dumps({"index": idx + 1}), encoding="utf-8")
    except Exception:
        pass
    return idx


def run_ddgs_dorks(max_results=25, pause=2.5, pages=2, rotation=None):
    """Automated dorking via ddgs (best-effort; DDG's ATS index is patchy).
    Queries are spaced out — hammering DDG back-to-back is what makes it start
    returning 'No results found' mid-run.

    `rotation` selects which slice of the profile's locality vocabulary this
    sweep's site-scoped dorks use (see `build_dork_queries`); left at its
    default (None), it advances the persisted counter so each run covers a
    different slice than the last one, and prints which slice it used so a
    run's coverage is inspectable after the fact. Pass an explicit int for a
    reproducible, non-advancing sweep (tests, or a deliberate re-run of a
    specific slice).

    `pages`>1 asks DDG for additional results pages on any query whose first
    page came back full (a strong sign more results exist), past the
    `max_results`-per-page ceiling — capped, and only pursued for a query
    that used its whole first page, so an already-exhausted or rate-limited
    query doesn't spend extra requests chasing nothing.
    """
    idx = _next_rotation_index() if rotation is None else rotation
    queries = build_dork_queries(idx)
    loc_slice = _rotate_terms(_LOCALITY_TERMS, 4, idx)
    print(f"  [dork] rotation slice {idx} (locality terms: "
          f"{', '.join(loc_slice) or '(none configured)'})")

    urls = []
    first = True
    for q in queries:
        if not first:
            time.sleep(pause)          # be gentle between queries
        first = False
        found = _ddg_text(q, max_results, page=1)
        print(f"  [dork] {len(found):2} result(s)  page=1  {q[:60]}")
        urls += found
        # A full first page suggests DDG has more to give; an empty or
        # partial one means it doesn't (or it's already rate-limited), so
        # don't burn extra requests paginating a query that came up short.
        page = 2
        while len(found) >= max_results and page <= pages:
            time.sleep(pause)
            found = _ddg_text(q, max_results, page=page)
            print(f"  [dork] {len(found):2} result(s)  page={page}  {q[:60]}")
            urls += found
            page += 1
    return harvest_urls(urls)
