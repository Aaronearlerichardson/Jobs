"""
ATS "dorking" — find crawlable NC companies by mining search-engine-indexed
ATS board URLs, instead of guessing a board from a company name.

A search like `site:jobs.lever.co "Durham"` returns board URLs whose slug we
can read directly (e.g. jobs.lever.co/<slug>). We extract the ATS + slug/triple
from each URL, NC-verify the board, mission-score it, and add it.

Two entry points:
  * run_ddgs_dorks()  — fully automated via src.net.ddg (DuckDuckGo).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from functools import partial
from typing import Any

from src import config
from src import store
from src import tags as company_tags
from src.ats import coords
from src.ats.board import BOARDS
from src.ats.board import company as company_fetch
from src.ats.signatures import detect
from src.discovery.local_sourcing import score_and_upsert
from src.discovery.resolve.board import validate_board
from src.discovery.resolve.identity import nc_hq_signal
from src.discovery.resolve.probes import slug_keyed
from src.match.locality import NC_RE
from src.match.names import SLUG_NAME_SOURCE
from src.net import ddg
from src.net.parallel import fan_out
from src.rows import BoardHit


def _or_group(terms: Sequence[str], n: int = 8) -> str:
    """A `("a" OR b OR "c d")` search clause from profile terms (multi-word
    terms quoted). Empty string when there are no terms."""
    picked = [t for t in terms[:n] if t]
    if not picked:
        return ""
    return "(" + " OR ".join(f'"{t}"' if " " in t else t for t in picked) + ")"


_LOCALITY_TERMS = config.LOCALITY_SUBSTRINGS or config.LOCALITY_WORD_TOKENS
_DOMAIN = _or_group(config.DOMAIN_KEYWORDS, n=6)
_CORE = _or_group(config.CORE_KEYWORDS, n=6)


def _rotate_terms(terms: Sequence[str], group_size: int, index: int) -> list[str]:
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


def build_dork_queries(rotation: int = 0) -> list[str]:
    """The dork query set for rotation index `rotation`: one `site:` dork
    per host form a spec's `discovery.search` names, in position order (a
    `discovery.narrow` spec's searched by name with the profile's domain
    keywords instead), then a free-text sweep. DDG chokes on long `site:` +
    big OR-group queries (returns nothing), so the site-scoped dorks use a
    SHORT locality clause (top few terms of that rotation's slice); the
    free-text sweep can afford more. `rotation=0` is the
    original fixed top-4/top-8 selection; each further index rotates onto
    the next slice of the profile's locality vocabulary (see
    `_rotate_terms`), so successive sweeps explore beyond the same 25
    top-ranked results DDG would otherwise return for an unchanging query.

    >>> qs = build_dork_queries(0)
    >>> len(qs) >= 4, any("greenhouse" in q for q in qs)
    (True, True)
    >>> any("icims" in q for q in qs)
    True

    Rotating changes which locality terms the site-scoped dorks carry
    (assuming the profile has more than 4 locality terms configured):

    >>> build_dork_queries(0) != build_dork_queries(1)
    True
    """
    # (position, host form, narrow) for every spec's `discovery.search` form.
    sites = sorted((at, host, b.spec.discovery.narrow)
                   for b in BOARDS.values() for at, host in b.spec.discovery.search)
    loc_site = _or_group(_rotate_terms(_LOCALITY_TERMS, 4, rotation), n=4)
    loc_wide = _or_group(_rotate_terms(_LOCALITY_TERMS, 8, rotation), n=8)
    queries = [f'"{host}" {loc_site}' + (f" {_DOMAIN}" if _DOMAIN else "") if narrow
               else f'site:{host} {loc_site}' for _, host, narrow in sites]
    if _CORE:
        # Bullseye sweep — target companies are often on custom boards /
        # non-.com domains that name-guessing misses.
        queries.append(f'{_CORE} {loc_wide} (careers OR jobs OR hiring)')
    return [q for q in queries if loc_site and loc_site in q or _CORE and _CORE in q]


def extract_boards_from_urls(urls: Iterable[str]) -> list[tuple[str, Any]]:
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

    Nor is a vendor's own site or an embed/asset path of a board URL form
    (src.ats.signatures.BAD_SLUGS), nor a board keyed on its careers URL,
    which a slug does not name:

    >>> extract_boards_from_urls(["https://www.bamboohr.com/",
    ...                           "https://boards.greenhouse.io/embed/job_board/js?for=x",
    ...                           "https://unc.peopleadmin.com/postings/123"])
    []
    """
    out: list[tuple[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for u in urls:
        hit = detect("", u, leads=False)
        # A lead (Taleo, Eightfold, ...) has no fetchable coordinates, and a
        # board keyed on its careers URL (src.store.board_key) has no slug:
        # an (ats, slug) handle for one would mint a row with no board
        # identity.
        if not hit or not slug_keyed(BOARDS[hit[1]]):
            continue
        _, ats, slug = hit
        key = (ats, str(slug))
        if key not in seen:
            seen.add(key)
            out.append((ats, slug))
    return out


async def _live_board(cand: BoardHit, require_live: bool) -> BoardHit | None:
    """`cand` with the counts of a live read of its board, or None when it
    is not worth scoring: no live local posting (`require_live`), else no
    local posting and no confirmed local HQ either."""
    comp = coords.from_hit(cand)
    if require_live:
        counts = await validate_board(comp)
        if not counts or counts[1] < 1:
            return None
        total, nc = counts
    else:
        try:
            jobs = await company_fetch.fetch_company(comp, NC_RE, validate=True)
        except Exception:
            jobs = []
        total = nc = len(jobs)
        # Add even with 0 current NC openings IF we can confirm an NC HQ/office
        # (so a daily run catches their next NC posting) -- but not otherwise,
        # else non-NC companies that merely mention NC would pollute the roster.
        if nc == 0 and not await nc_hq_signal(cand["name"]):
            return None
    return {**cand, "nc": nc, "count": total}


async def intake_boards(candidates: Iterable[BoardHit], source: str, *,
                        tags: str | None = company_tags.LOCAL,
                        require_live: bool = False, limit: int | None = None,
                        verbose: bool = True) -> tuple[int, int]:
    """
    Read each candidate board the roster lacks (a `BoardHit`: ats, slug and
    optionally name and careers_url), mission-score the ones with local
    jobs, and queue them for review under `source`. Returns (added, checked).
    A nameless candidate is named after its slug; mission scoring reads the
    board's live job titles for domain context.

    Every row lands in the review queue (src.store.mark_pending), never on
    the roster: a candidate named by a search engine or a dataset is the
    weakest-sourced one in the codebase.

    `require_live` admits only a board with a live local posting, read the
    way the resolver reads one; without it a board with none is admitted too
    when the employer has a confirmed local HQ (dork's rule). `limit` caps
    the rows written, taking candidates in the order given.

    What a board becomes is store.plan_board's: one the roster has is
    skipped; a board of an employer the roster holds on another is written
    as its sibling, or replaces the employer's dead one, with the
    employer's mission verdict and no score. A candidate left out is
    counted, with its reason, in one "skipped" line (`verbose`).

    Notes:
        harvest_urls' body, made the one intake of every board-first source
        (2026-10-05). Reads run concurrently across hosts for a directory
        pass; dork's HQ lookups stay one at a time.
    """
    named: list[BoardHit] = [
        {**c, "name": c.get("name") or coords.slug_title(coords.from_hit(c))}
        for c in candidates]
    async with store.Writer() as db:
        todo = [c for c in named
                if (await db.run(store.plan_board,
                                 coords.from_hit(c, name=c["name"]))).action != "update"]
        skipped = {"already tracked": len(named) - len(todo), "no live local posting": 0,
                   "same board under another name": 0, "over the limit": 0}
        origin = ((lambda c: company_fetch.board_origin(coords.from_hit(c)))
                  if require_live else None)
        added = 0
        while todo and (limit is None or added < limit):
            size = 30 if limit is None else min(30, 2 * (limit - added))
            chunk, todo = todo[:size], todo[size:]
            live: dict[int, BoardHit] = {
                id(cand): hit async for cand, hit in fan_out(
                    chunk, partial(_live_board, require_live=require_live),
                    "board", with_item=True, max_workers=1, key=origin) if hit}
            for cand in chunk:
                hit = live.get(id(cand))
                if not hit:
                    skipped["no live local posting"] += 1
                    continue
                if limit is not None and added >= limit:
                    skipped["over the limit"] += 1
                    continue
                # Scoring, activation and the review queue are the shared write
                # path (local_sourcing.score_and_upsert). The row is tagged local
                # even at nc == 0: the HQ signal is what admitted it. An inactive
                # row is near-unrecoverable here -- a board already in the store
                # is never re-probed -- which is why the activation rule must be
                # the shared one.
                result = await score_and_upsert(db, hit, source=source, tags=tags)
                if not result:
                    skipped["same board under another name"] += 1
                    continue
                row, active, pending = result
                added += 1
                if verbose:
                    state = ("PENDING" if pending
                             else "ACTIVE" if active else "inactive")
                    print(f"  {hit['name'][:26]:26} {hit['ats'] or '':12} nc={hit['nc']:2} "
                          f"{str(row['mission_tier']):19} "
                          f"{row['mission_score'] or 0:.2f} {state}")
    skipped["over the limit"] += len(todo)
    if verbose and any(skipped.values()):
        print("  skipped: " + ", ".join(f"{n} {why}" for why, n in skipped.items() if n))
    return added, len(named)


async def harvest_urls(urls: Iterable[str], verbose: bool = True) -> tuple[int, int]:
    """Extract boards from `urls` and intake the new ones (`intake_boards`),
    each named after its slug. Returns (added, checked)."""
    return await intake_boards(
        ({"ats": ats, "slug": slug} for ats, slug in extract_boards_from_urls(urls)),
        SLUG_NAME_SOURCE, verbose=verbose)


def _next_rotation_index() -> int:
    """Read-then-increment the persisted rotation counter. Best-effort: a
    read/write failure just falls back to index 0 (the original fixed
    top-4/top-8 query set) rather than crashing the sweep."""
    # Persisted, so successive runs advance through the locality vocabulary
    # instead of repeating the same slice (and a re-read reproduces exactly
    # which slice a past run covered) — deterministic, not `random`-based.
    state = config.DATA_DIR / ".cache" / "dork_rotation.json"
    try:
        idx = int(json.loads(state.read_text("utf-8")).get("index", 0))
    except Exception:
        idx = 0
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"index": idx + 1}), encoding="utf-8")
    except Exception:
        pass
    return idx


async def run_ddgs_dorks(max_results: int = 25, pause: float = 2.5, pages: int = 2,
                         rotation: int | None = None) -> tuple[int, int]:
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
    loc_slice = _rotate_terms(_LOCALITY_TERMS, 4, idx)
    print(f"  [dork] rotation slice {idx} (locality terms: "
          f"{', '.join(loc_slice) or '(none configured)'})")
    return await harvest_urls(await dork_urls(build_dork_queries(idx), max_results,
                                              pause, pages))


async def dork_urls(queries: Iterable[str], max_results: int, pause: float,
                    pages: int) -> list[str]:
    """The result URLs of each dork query in turn, `pause` seconds apart,
    with up to `pages` pages for a query whose first page came back full
    (see run_ddgs_dorks)."""
    urls: list[str] = []
    first = True
    for q in queries:
        if not first:
            await asyncio.sleep(pause)          # be gentle between queries
        first = False
        found = await ddg.search_urls(q, max_results)
        print(f"  [dork] {len(found):2} result(s)  page=1  {q[:60]}")
        urls += found
        # A full first page suggests DDG has more to give; an empty or
        # partial one means it doesn't (or it's already rate-limited), so
        # don't burn extra requests paginating a query that came up short.
        page = 2
        while len(found) >= max_results and page <= pages:
            await asyncio.sleep(pause)
            found = await ddg.search_urls(q, max_results, page=page)
            print(f"  [dork] {len(found):2} result(s)  page={page}  {q[:60]}")
            urls += found
            page += 1
    return urls
