"""
Local company sourcing: candidate employer NAMES -> confirmed, crawlable,
locality-verified boards in the companies table.

The stages, and where each lives:

  1. Gather candidate names -- profile seeds, directory scrapes, web-search
     harvesting, an LLM brainstorm (src.discovery.name_sources) -- or take them
     from a pasted page (src.discovery.paste_ingest) or a banked lead
     (resolve_leads below).
  2. Resolve each name to a board. That whole step is src.discovery.resolve
     now -- sniff the careers page, probe guessed slugs, search the web,
     validate every hit with a live fetch, and say WHY when none of it
     worked (resolve.board.resolve_or_miss). It used to live here, which
     put ~300 lines of store-free resolution in the middle of the module
     that decides what to store.
  3. Verify the board has jobs in your [locality], or the company an office
     there (resolve.identity.nc_hq_signal); mission-score it; write it to
     the store as a review candidate (score_and_upsert), or record the miss.

This module is the SOURCING half: which names to try, and what to do with
what comes back. Everything here that writes, writes to the store.

Entry points: populate_companies (the bulk pass), add_board (a URL the user
already knows), resolve_leads (leads banked by capture.py), score_missions
(mission backfill).
"""

from __future__ import annotations

import importlib.util
import logging
import json
import sqlite3
import time
from datetime import datetime, timedelta
from contextlib import AsyncExitStack
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from typing import cast

from src import config
from src import store
from src import tags as company_tags
from src.ats import coords
from src.ats.board import company as company_fetch
from src.ats.registry import seed_tag_for
from src.ats.signatures import detect, pack
from src.claude import api as claude_api
from src.claude.api import ACTIVE_MISSION_TIERS
from src.digest.render import score_text
from src.match.locality import NC_RE
from src.match.names import name_key
from src.net.parallel import RESOLVE_STALL_S, fan_out
from src.rows import BoardHit, CompanyIn, CompanyRow, is_watched
from .name_sources import MAJORS, NAME_BLOCKLIST, _MAJORS_KEYS, gather_names
from .resolve import board as resolve_board
from .resolve.board import read_local
from .resolve.probes import JsScanProbePool, probe_company
from .resolve import sniffer
from src.ats.signatures import Detection
from .resolve.websearch_board import websearch_board
from .dork import run_ddgs_dorks
from .write import (_print_scored, _productive_keys, _score_hit, _settled_board,
                    board_already_tracked, mission_context, report_dup_board,
                    score_and_upsert)

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  discover_local: the bulk pass over gathered names                          #
# --------------------------------------------------------------------------- #


def _boardless(names: list[str], hits: Iterable[BoardHit],
               skip: Collection[str] = frozenset()) -> list[str]:
    """The names with no LOCAL board yet, less the name keys in `skip`
    (the roster's working boards, _productive_keys).

    Only an nc>0 hit counts as found: a junk 0-NC slug collision must not
    stop a later pass from looking for the real employer. Asked before
    each of the three fallback passes.
    """
    have = {name_key(h["name"]) for h in hits if h["nc"] > 0} | set(skip)
    return [n for n in names if name_key(n) not in have]


async def _hit_from_detection(name: str, det: Detection) -> BoardHit:
    """A sniff/websearch DETECTION turned into a hit, by asking the board
    how many LOCAL jobs it actually holds.

    The two fallback passes each wrote this out, and had drifted: only the
    websearch copy special-cased a `custom` board. Coordinates go through
    src.ats.coords now (the rule for every other board write in the repo),
    which handles a multi-part handle, a plain slug and a careers-URL-only
    custom board without a branch per caller.

    A `reason` rides along only when nc == 0: a live board with nothing
    local, a DEAD board and an unreadable one (resolve.board.read_local)
    are each a different miss from a name nothing could be found for.
    """
    ats = det["ats"]
    slug = det.get("triple", det.get("slug"))
    jobs, total, reason = await read_local(coords.columns(ats, slug, det.get("careers_url")))
    hit: BoardHit = {"name": name, "ats": ats, "slug": slug, "count": len(jobs) or total,
                     "nc": len(jobs), "careers_url": det.get("careers_url")}
    if reason:
        hit["reason"] = reason
    return hit


async def _resolve_pass(todo: list[str],
                        resolve_one: Callable[[str], Awaitable[BoardHit | None]],
                        tag: str, hits: list[BoardHit],
                        misses: list[BoardHit], max_workers: int) -> None:
    """Run one fallback resolver over `todo`, appending to `hits` (nc>0) or
    `misses` (anything else), under the stall watchdog. A name whose
    resolution raises is reported and becomes a `fetch-error:<Exception>`
    miss, as a stalled one does, so the retry schedule still applies.

    The watchdog, not a plain fan-out: one wedged resolution used to hold
    the web UI's single op slot until the app was restarted.
    """
    def raised(n: str, e: Exception) -> None:
        print(f"    [!] {n} error: {e}")
        misses.append({"name": n, "reason": f"fetch-error:{type(e).__name__}"})

    async for h in fan_out(
            todo, resolve_one, str, max_workers, on_error=raised,
            stall_s=RESOLVE_STALL_S,
            on_abandon=lambda n: misses.append({"name": n,
                                                "reason": "fetch-error:stalled"})):
        if h and h.get("nc"):
            hits.append(h)
            # Widths chosen so both tags print one aligned table.
            print(f"    {tag} {h['name']:{35 - len(tag)}} {h['ats']:14} "
                  f"{h['slug']!s:26} nc={h['nc']}")
        elif h:
            misses.append(h)


async def _probe_pass(names: list[str], max_workers: int) -> list[BoardHit]:
    """Name-guessed slug probes over every candidate. The cheap first pass:
    no page fetched, just the platforms' own APIs."""
    n_scan = sum(1 for n in names if name_key(n) in _MAJORS_KEYS)
    print(f"  probing {len(names)} candidate compan(ies) for live ATS boards "
          f"({n_scan} with the careers-page scan)...")
    # A probe that raises is now reported and skipped rather than ending
    # the pass -- this was the last pool in src/ with a bare fut.result(),
    # the same shape as the prune_dead_boards bug.
    return [h async for h in fan_out(
        names, lambda n: probe_company(n, name_key(n) in _MAJORS_KEYS),
        "probe", max_workers) if h]


async def _js_scan_pass(hits: list[BoardHit], max_workers: int,
                        skip: Collection[str] = frozenset()) -> None:
    """Re-probe the MAJORS that got no board, with a headless browser
    (probes.JsScanProbePool).

    Big employers often have React/SPA careers pages whose board link only
    appears after JS runs, so the static probe misses them entirely.
    """
    missed = _boardless(MAJORS, hits, skip)
    if importlib.util.find_spec("playwright.async_api") is None:
        if missed:
            print(f"    [js] playwright not installed; skipping JS probe "
                  f"of {len(missed)} major(s)")
        return
    if not missed:
        return
    # Parallel across DIFFERENT sites is safe: each target still sees
    # exactly one page load. K is memory-bound (a page in the one headless
    # browser each), so JS_PAGES caps it, as `max_workers` does.
    #
    # A pool rather than a fixed `i % k` page per name: the fixed split
    # queued two names on one page while another sat idle, and the
    # per-probe budget would then have counted that queue wait.
    k = min(config.SETTINGS.js_pages, max_workers, len(missed))
    print(f"  JS-probing {len(missed)} major(s) with no static board "
          f"({k} parallel page(s))...")
    async with JsScanProbePool(k) as pool:

        async def _js_one(name: str) -> BoardHit:
            t0 = time.monotonic()
            meta, outcome = await pool.probe(name)
            if outcome == "hit" and meta is not None:
                return await _hit_from_detection(name, {
                    "ats": meta["ats"], "slug": meta["slug"],
                    "careers_url": meta.get("careers_url") or ""})
            return {"name": name, "reason": outcome,
                    "elapsed": time.monotonic() - t0}

        # Every name prints a line: 2026-09-22 printed 4 [JS-OK] for 38
        # majors, and the other 34 (and a 338s stall) left no trace.
        # on_error keeps this pass's own wording rather than fan_out's.
        async for h in fan_out(missed, _js_one, "JS probe", k,
                               on_error=lambda n, e: print(
                                   f"    [!] JS probe failed for {n!r}: {e}")):
            if "nc" in h:
                hits.append(h)
                slug = h["slug"]
                shown = "/".join(map(str, slug)) if isinstance(slug, tuple) else slug
                print(f"    [JS-OK] {h['name']:30} {shown}  "
                      f"nc={h['nc']}/{h['count']}")
            else:
                print(f"    [JS-MISS] {h['name']:28} {h['reason']}  "
                      f"{h['elapsed']:.0f}s")


async def _sniff_pass(names: list[str], hits: list[BoardHit],
                      misses: list[BoardHit], max_workers: int) -> None:
    """Fetch each still-boardless name's careers page and read the ATS +
    exact slug off it. The main recall lever over the directory: it covers
    every hosted platform and finds slugs the name-guesser cannot."""
    todo = _boardless(names, hits)
    print(f"  sniffing careers pages for {len(todo)} name(s) without a board...")

    async def _sniff_one(n: str) -> BoardHit:
        s = await sniffer.sniff_ats(n)
        return await _hit_from_detection(n, s) if s else {
            "name": n, "reason": "no-board-found"}

    await _resolve_pass(todo, _sniff_one, "[SNIFF]", hits, misses, max_workers)


async def _websearch_pass(names: list[str], hits: list[BoardHit],
                          misses: list[BoardHit], max_workers: int,
                          cap: int | None) -> None:
    """Search the web for a careers page, for names probe+sniff could not
    board. A measured 60-company gap study found 5 of 6 eventual
    resolutions came through here -- names on gov/acronym/product-named
    domains the slug-guesser and the careers-page sniff cannot reach.

    Capped, and recent misses skipped, because DDG rate-limits hard: an
    earlier uncapped profile blocked ~1271s of a 1726s run inside DDG's
    own retry/backoff (see src.net.ddg).
    """
    cap = config.DISCOVERY_WEBSEARCH_CAP if cap is None else cap
    todo = _boardless(names, hits)
    if todo and cap > 0:
        async with store.Writer() as db:
            recent = await db.run(store.recent_miss_names)
        todo = [n for n in todo if n not in recent][:cap]
    else:
        todo = list[str]()
    print(f"  websearch-resolving {len(todo)} name(s) without a board "
          f"(cap={cap})...")
    if not todo:
        return
    t0 = time.time()

    async def _websearch_one(n: str) -> BoardHit:
        w = await websearch_board(n)
        return await _hit_from_detection(n, w) if w else {
            "name": n, "reason": "no-board-found"}

    await _resolve_pass(todo, _websearch_one, "[WEBSEARCH]", hits, misses,
                        max_workers)
    print(f"  websearch pass: {time.time() - t0:.1f}s for "
          f"{len(todo)} name(s)")


def _reduce_hits(names: list[str], hits: list[BoardHit], pass_misses: list[BoardHit]
                 ) -> tuple[list[BoardHit], list[BoardHit],
                            list[BoardHit], list[BoardHit]]:
    """Everything the passes found, accounted for exactly once.

    Returns (hits, confirmed, dropped, misses). Every candidate that is
    not confirmed is a MISS with a reason: a live board with no local
    openings, a board whose coordinates read empty, or a name nothing
    could be found for. Two names on one board keep the shorter; the other
    is a `duplicate`:

    >>> hit = {"ats": "phenom", "slug": "jobs.x.com", "nc": 5, "count": 9}
    >>> h, ok, _, miss = _reduce_hits(["Patheon", "Thermo X"], [
    ...     {**hit, "name": "Thermo X"}, {**hit, "name": "Patheon"}], [])
    >>> [c["name"] for c in ok], [(m["name"], m["reason"]) for m in miss]
    (['Patheon'], [('Thermo X', 'duplicate')])
    """
    # Names that reached a live board under SOME spelling, before the
    # blocklist and the by-board dedup collapse them: they are accounted
    # for by their surviving row and must not ALSO be filed as
    # no-board-found.
    boarded = {h["name"] for h in hits}

    # Drop known bad name->board matches.
    hits = [h for h in hits if name_key(h["name"]) not in NAME_BLOCKLIST]

    # De-dup by resolved board (same slug/triple reached via different
    # names, e.g. "BioAgilytix" vs "BioAgilytix Labs"); keep the shorter.
    # The others are `duplicate` misses, aliases of whichever row ends up
    # tracking the board (populate_companies).
    by_board: dict[tuple[str, str], BoardHit] = {}
    losers: list[BoardHit] = []
    for h in hits:
        key = (h["ats"], str(h["slug"]))
        kept = by_board.get(key)
        if kept is None or len(h["name"]) < len(kept["name"]):
            if kept is not None:
                losers.append(kept)
            by_board[key] = h
        else:
            losers.append(h)
    hits = list(by_board.values())

    # Split on the locality check: nc>0 is confirmed-local; nc==0 is either
    # a false-positive slug collision or a non-local employer -- dropped,
    # but shown.
    confirmed = sorted([h for h in hits if h["nc"] > 0],
                       key=lambda h: h["nc"], reverse=True)
    dropped = sorted([h for h in hits if h["nc"] == 0],
                     key=lambda h: h["name"].lower())
    misses: dict[str, BoardHit] = {}
    for h in dropped:
        misses[h["name"]] = {**h, "reason": "no-local-jobs"}
    for h in losers:
        misses.setdefault(h["name"], {**h, "reason": "duplicate"})
    for h in pass_misses:
        misses.setdefault(h["name"], h)
    for n in names:
        if n not in boarded:
            misses.setdefault(n, {"name": n, "reason": "no-board-found"})
    for h in confirmed:
        misses.pop(h["name"], None)
    return hits, confirmed, dropped, sorted(misses.values(),
                                            key=lambda m: m["name"].lower())


def _report_discovery(hits: list[BoardHit], confirmed: list[BoardHit],
                      dropped: list[BoardHit], misses: list[BoardHit]) -> None:
    """The pass's own scoreboard, printed for a human reading the log."""
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


async def discover_local(extra_names: list[str] | None = None, max_workers: int = 12,
                         js_majors: bool = True, sniff: bool = True,
                         websearch: bool = True, websearch_cap: int | None = None,
                         tracked: Collection[str] = frozenset()
                         ) -> tuple[list[BoardHit], list[str], list[BoardHit]]:
    """
    Gather names + probe each. Returns (confirmed, checked, misses) where
    confirmed is a list of NC-local hit dicts and misses is one dict per
    candidate that did NOT become one, carrying a ``reason`` code from
    src.store.MISS_REASONS.

    Four passes, cheapest first, each one only looking at the names the
    ones before it could not board (_boardless):

      1. _probe_pass       name-guessed slugs against the platform APIs
      2. _js_scan_pass     headless browser, MAJORS only (``js_majors``)
      3. _sniff_pass       fetch the careers page and read the ATS off it
      4. _websearch_pass   search the web for one, capped (``websearch``)

    then _reduce_hits accounts for every candidate exactly once and
    _report_discovery prints the scoreboard. A name whose key is in
    `tracked` (the roster's working boards) gets the cheap probe only:
    no fallback pass re-resolves it and a miss is never filed for it.

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
    """
    names = await gather_names(extra_names)
    misses: list[BoardHit] = []
    hits = await _probe_pass(names, max_workers)
    todo = [n for n in names if name_key(n) not in tracked]
    if len(todo) < len(names):
        print(f"  {len(names) - len(todo)} candidate(s) already on the roster with a "
              f"working board: probed only, not re-resolved")
    if js_majors:
        await _js_scan_pass(hits, max_workers, tracked)
    if sniff:
        await _sniff_pass(todo, hits, misses, max_workers)
    if websearch:
        await _websearch_pass(todo, hits, misses, max_workers, websearch_cap)

    hits, confirmed, dropped, misses = _reduce_hits(todo, hits, misses)
    misses = [m for m in misses if name_key(m["name"]) not in tracked]
    _report_discovery(hits, confirmed, dropped, misses)
    return confirmed, names, misses


async def populate_companies(extra_names: list[str] | None = None,
                             include_missions: list[str] | None = None,
                             dork: bool = True) -> list[dict[str, object]]:
    """
    Full sourcing pass → SQL store: discover NC-local boards, score each
    company's MISSION once (cached), and upsert into the `companies` table.
    Every company new to the store lands in the REVIEW QUEUE — inactive,
    review-pending (src.store.mark_pending) — so a bulk pass cannot
    put a name nobody vetted on the roster. Mission scoring still runs, so
    the reviewer sees the tier; `include_missions` only decides what
    confirming such a row activates.

    Finishes with the ATS-dork sweep (search-indexed board URLs) unless
    `dork=False` — run LAST on purpose: it consults the store and skips
    boards the name-based pass just added, so the two passes don't create
    duplicate rows for the same board. Dork adds are upserted directly and
    are NOT included in the returned list.

    Every candidate that did NOT become a company is written too, as an
    inactive row carrying a `miss_reason` (src.store.record_miss), so the
    failures are a queryable worklist instead of terminal scrollback.

    Returns the list of company dicts written by the name-based pass.
    """
    async with store.Writer() as db:
        tracked = await db.run(_productive_keys)
    confirmed, _, misses = await discover_local(extra_names, tracked=tracked)
    async with store.Writer() as db:
        written: list[dict[str, object]] = []

        # Misses first: they are pure local writes, so the roster's failure
        # record survives even if the mission-scoring pass below is interrupted.
        # A `duplicate` waits for the board it lost to (below).
        aliases = [m for m in misses if m["reason"] == "duplicate"]
        misses = [m for m in misses if m["reason"] != "duplicate"]
        n_miss = await db.run(lambda conn: sum(
            _record_miss(conn, m["name"], m["reason"], m) for m in misses))
        if misses:
            counts = await db.run(store.miss_counts)
            print(f"\n  recorded {n_miss} miss(es) (of {len(misses)} not "
                  f"confirmed); store now holds: "
                  + ", ".join(f"{fam}={n}" for fam, n in counts))
        # A board the roster already settles (a duplicate, a working row
        # kept) costs no mission call: until 2026-09-29 every confirmed hit
        # was scored first, 45 of 49 calls on boards already tracked.
        todo = []
        held = {}       # name -> the verdict a replaced board keeps
        for h in confirmed:
            settled, result, verdict = await db.run(_settled_board, h, "local_sourcing", None, None)
            if not settled:
                todo.append(h)
                if verdict:
                    held[h["name"]] = verdict
            elif result:
                written.append(dict(result[0]))
                _print_scored(h["name"], result[0], "tracked")
            else:
                # A duplicate: remembered as the tracked employer's alias,
                # so the next pass does not resolve the name again.
                await db.run(_record_alias, h)
        print(f"\n  scoring mission for {len(todo)} NC-local compan(ies) "
              f"not already on the roster...")

        # The title fetch (1 GET) + mission call (1 LLM request) per company are
        # pure network I/O -- the historical serial tail of the pass. Run them
        # concurrently, under the stall watchdog; output is completion-ordered.
        async def verdict_of(h: BoardHit) -> tuple[str | None, float | None, str]:
            return held.get(h["name"]) or await _score_hit(h)

        async for h, scored in fan_out(
                todo, verdict_of, lambda h: h["name"], 8, with_item=True,
                on_error=lambda h, e: print(
                    f"    [!] mission scoring failed for {h['name']!r}: {e}"),
                stall_s=RESOLVE_STALL_S):
            result = await score_and_upsert(db, h, source="local_sourcing",
                                            include_missions=include_missions,
                                            scored=scored)
            if not result:
                continue
            row, active, pending = result
            written.append(dict(row))
            _print_scored(h["name"], row, "PENDING REVIEW" if pending
                          else "active" if active else "INACTIVE(other)")
        for m in aliases:
            await db.run(_record_alias, m)

    if dork:
        print("\n  ATS-dork sweep (search-indexed board URLs)...")
        try:
            added, checked = await run_ddgs_dorks()
            print(f"  dork: {added} new board(s) added "
                  f"({checked} extracted from search results)")
        except Exception as e:
            print(f"  [!] dork sweep failed (name-based results unaffected): {e}")
    return written


async def queue_names(db: store.Writer, names: Mapping[str, str],
                      careers_urls: Mapping[str, str] | None = None,
                      max_workers: int = 6, *, local_only: bool = True,
                      include_missions: list[str] | None = None,
                      report: Callable[[str, BoardHit, tuple[CompanyRow | CompanyIn, int, bool]],
                                       None] | None = None,
                      seed_tags: bool = False, js_pages: int = 0, dry_run: bool = False
                      ) -> tuple[list[BoardHit], list[tuple[str, str]]]:
    """Resolve each name in `names` (name -> the `source` it is filed under;
    its `careers_urls` entry, when it has one, seeds the sniff) and queue
    every board with local jobs for review; every other outcome is recorded
    as a miss.

    Returns (queued hits, [(name, reason)] of the misses). A live board with
    no local jobs is a miss (`no-local-jobs`), as in discover_local: a name
    from a regional list is not proof the board is local. Without
    `local_only` it is queued too. `report(name, hit, (row, active,
    pending))` prints each queued board (default: `_print_scored`).
    `seed_tags` tags each board as its platform seeds (`seed_tag_for`)
    instead of by its local jobs; `js_pages` > 0 adds a headless scan of
    that many pages for a name nothing else resolves (resolve_or_miss's
    `js`). `dry_run` resolves and returns without writing anything.
    """
    urls = careers_urls or {}
    queued: list[BoardHit] = []
    missed: list[tuple[str, str]] = []
    stalled: list[str] = []

    async def miss(name: str, reason: str, hit: BoardHit | None = None) -> None:
        if not dry_run:
            await db.run(_record_miss, name, reason, hit, names[name])
        missed.append((name, reason))

    async with AsyncExitStack() as stack:
        js = {"js": await stack.enter_async_context(JsScanProbePool(js_pages))
              } if js_pages else {}
        async for name, (hit, reason) in fan_out(
                list(names), lambda n: resolve_board.resolve_or_miss(n, urls.get(n, ""), **js),
                str, max_workers, with_item=True, stall_s=RESOLVE_STALL_S,
                on_abandon=stalled.append):
            if not hit or (reason and local_only):
                await miss(name, reason or "no-board-found", hit)
                continue
            if dry_run:
                queued.append(hit)
                continue
            result = await score_and_upsert(
                db, hit, source=names[name], include_missions=include_missions,
                tags=seed_tag_for(hit["ats"]) if seed_tags else None)
            if not result:
                continue
            queued.append(hit)
            if report:
                report(name, hit, result)
            else:
                _print_scored(name, result[0], "PENDING REVIEW" if result[2] else "tracked")
    for name in stalled:
        await miss(name, "fetch-error:stalled")
    return queued, missed


async def add_board(name: str, url: str, capture: bool = False) -> Detection | None:
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
    if capture:
        async with store.Writer() as db:
            row: CompanyIn = {
                "name": name, "ats": store.CAPTURE_ATS, "careers_url": url,
                "source": "manual", "active": 1,
                "notes": "capture-only board: browse it yourself and save "
                         "pages with capture.py --watch"}
            dup = await db.run(board_already_tracked, row)
            if dup:
                report_dup_board(name, dup)
                return None
            await db.run(store.upsert_company, row)
        print(f"  [OK] {name}: capture-only, {url}  -- save its pages with "
              f"capture.py --watch")
        return {"ats": store.CAPTURE_ATS, "careers_url": url}

    hit = detect("", url, leads=False)
    found: Detection | None
    if hit:
        found = pack(hit[1], hit[2], url)
    else:
        found = await sniffer.sniff_ats(name, careers_url=url)
    if not found:
        print(f"  [!] No ATS coordinates found at/near {url}")
        return None

    ats = found["ats"]
    # The sniffed handle (a tuple where it has several parts); the URL
    # labels the printout below when there is none.
    handle = found.get("triple", found.get("slug"))
    slug = handle or url
    board = coords.columns(ats, handle, found.get("careers_url") or url, name=name)
    try:
        nc = len(await company_fetch.fetch_company(board, NC_RE, validate=True))
    except Exception:   # the engine fan-out has many failure kinds
        _log.warning("add board %s: fetch failed", url, exc_info=True)
        nc = 0

    tier, score, reason = await claude_api.score_company_mission(name, await mission_context(board))

    async with store.Writer() as db:
        dup = await db.run(board_already_tracked, board)
        if dup:
            report_dup_board(name, dup)
            return None
        row = {
            **board,
            "local_job_count": nc, "mission_tier": tier, "mission_score": score,
            "mission_reason": reason, "tags": company_tags.LOCAL if nc else None,
            "source": "manual", "active": 1,
        }
        # The URL is the user's, but the ATS coordinates under it were sniffed:
        # a careers page that links a shared/parent tenant resolves to somebody
        # else's board. One confirmation click covers both.
        pending = not await db.run(store.is_confirmed_company, name)
        if pending:
            row = store.mark_pending(row)
        await db.run(store.upsert_company, row)
    print(f"  [OK] {name}: {ats} {slug!s}  nc={nc}  mission={tier} ({score_text(score)})  "
          f"{'PENDING REVIEW' if pending else 'ACTIVE'}")
    return found


def _to_score(conn: sqlite3.Connection, rescore_all: bool) -> list[tuple[CompanyRow, bool]]:
    """score_missions' rows, each with whether it holds a board verdict of
    its own: every board row with no mission tier (every active one with
    `rescore_all`), keeping of one employer's verdict-less boards only the
    largest (total_job_count, then lowest id).

    >>> conn = store.connect(":memory:")
    >>> for n, jobs in (("A", 5), ("A (lever)", 9), ("B", 1)):
    ...     _ = store.upsert_company(conn, {"name": n, "ats": "lever", "slug": n,
    ...                                     "total_job_count": jobs, "active": 1})
    >>> _ = conn.execute("UPDATE companies SET employer_id = 1 WHERE name LIKE 'A%'")
    >>> [(c["name"], own) for c, own in _to_score(conn, False)]
    [('A (lever)', False), ('B', False)]
    """
    rows = conn.execute(
        "SELECT e.*, k.own FROM companies_effective e JOIN ("
        " SELECT e.id, o.own, ROW_NUMBER() OVER ("
        "  PARTITION BY COALESCE(e.employer_id, -e.id), o.own"
        "  ORDER BY COALESCE(e.total_job_count, 0) DESC, e.id) AS rn"
        " FROM companies_effective e JOIN (SELECT id, (mission_tier IS NOT NULL"
        "  OR mission_score IS NOT NULL) AS own FROM companies) o ON o.id = e.id"
        " WHERE COALESCE(e.ats, '') != '' AND CASE WHEN ? THEN e.active = 1"
        "  ELSE COALESCE(e.mission_tier, '') = '' END) k ON k.id = e.id"
        " WHERE k.own OR k.rn = 1"
        " ORDER BY e.mission_score DESC, e.local_job_count DESC", (rescore_all,)).fetchall()
    return [(store.as_company(r), bool(r["own"])) for r in rows]


async def score_missions(max_workers: int = 6, rescore_all: bool = False) -> int:
    """Backfill company mission scores: every company with a board and no
    mission_tier (every ACTIVE one, with rescore_all) gets sampled titles and
    one score_company_mission call. Heals stores imported without scoring or
    left by keyless/failed scoring passes.

    The unscored pass includes INACTIVE rows: a row whose mission call failed
    may have been written active=0, and the sourcing passes skip boards
    already in the store, so nothing else would re-score it. Scoring one to an
    active tier reactivates it. `rescore_all` stays active-only: it re-judges
    the live roster, and widening it would resurrect off-mission rows.

    The verdict is the EMPLOYER's, so an employer's boards are scored once,
    through its largest board; only a board holding a verdict of its own
    (set_board_mission) is scored on its own and keeps it."""
    async with store.Writer() as db:
        picked = await db.run(_to_score, rescore_all)
        cos = [c for c, _ in picked]
        own = {c["id"] for c, o in picked if o}
        if not cos:
            print("  Nothing to score - every active company has a mission tier.")
            return 0
        print(f"  mission-scoring {len(cos)} compan(ies)...")

        async def _one(c: CompanyRow) -> tuple[str | None, float | None, str]:
            return await claude_api.score_company_mission(c["name"], await mission_context(c))

        n = 0
        async for c, (tier, score, reason) in fan_out(
                cos, _one, lambda c: c["name"], max_workers, with_item=True,
                on_error=lambda c, e: print(f"    [!] {c['name']}: {e}"),
                stall_s=RESOLVE_STALL_S):
            if tier is None and score is None:
                continue          # scoring unavailable - leave the row alone
            # Off-mission companies are deactivated so the crawl skips them,
            # as in the new-company path; watched ones are exempt (the user
            # keeps them crawled on purpose).
            update: CompanyIn = {"name": c["name"]}
            if c["id"] in own:
                await db.run(store.set_board_mission, c["id"], tier, score, reason)
            else:
                update |= {"mission_tier": tier, "mission_score": score, "mission_reason": reason}
            revived = False
            if (tier is not None and tier not in ACTIVE_MISSION_TIERS
                    and not config.is_multi_division(c["name"])
                    and not is_watched(c)):
                update["active"] = 0
            # NOT src.claude.is_active_mission: this is the REACTIVATION half
            # and must not revive on `tier is None`. A None tier with a score
            # is an answer outside the profile's taxonomy; the helper would
            # call it "unavailable" and revive, but it must leave an inactive
            # company alone. See tests/test_invariants.py.
            elif not c.get("active") and (tier in ACTIVE_MISSION_TIERS
                                          or config.is_multi_division(c["name"])):
                # Recovery: an on-mission tier on an inactive, unscored row
                # means its mission call failed. Dead boards stay off
                # (prune_dead_boards: the endpoint 404s), and so do rows in
                # the review queue (reviving them would skip it).
                if (not (c.get("notes") or "").startswith("deactivated: dead")
                        and c["review"] != "pending"):
                    update["active"] = 1
                    revived = True
            await db.run(store.upsert_company, update)
            n += 1
            flag = ("  -> deactivated (off-mission)"
                    if (tier is not None and tier not in ACTIVE_MISSION_TIERS)
                    else "  -> REACTIVATED (was unscored + inactive)" if revived
                    else "")
            print(f"    {c['name']:32} {str(tier):20} {score_text(score)}  ({reason}){flag}")
    print(f"\n  {n} compan(ies) scored.")
    return n


def _record_alias(conn: sqlite3.Connection, hit: BoardHit) -> bool:
    """store.record_alias for `hit`'s name, of the row tracking its board."""
    dup = board_already_tracked(conn, coords.from_hit(hit, name=hit["name"]))
    return bool(dup) and store.record_alias(conn, hit["name"], dup)


def _record_miss(conn: sqlite3.Connection, name: str, reason: str,
                 hit: BoardHit | None = None, source: str = "local_sourcing") -> bool:
    """record_miss for `name` with whatever board `hit` established, unless
    that board is already tracked under another name: then `name` becomes
    an alias of that row's employer (store.record_alias), not a second row
    that would harvest the board twice ("Paradromics" beside "Paradromics
    Inc.", 2026-10-08).

    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_company(conn, {"name": "P Inc.", "ats": "jazzhr", "slug": "p"})
    >>> _record_miss(conn, "P", "no-local-jobs", {"name": "P", "ats": "jazzhr", "slug": "p"})
        [dup]  P                              same jazzhr board as 'P Inc.' - already tracked, not added
    True
    >>> conn.execute("SELECT ats, miss_reason FROM companies WHERE name='P'").fetchone()[:]
    (None, 'duplicate')
    """
    row: CompanyIn = {**_miss_row(hit), "source": source} if hit else {"source": source}
    dup = row.get("ats") and board_already_tracked(conn, {**row, "name": name})
    if dup:
        report_dup_board(name, dup)
        return store.record_alias(conn, name, dup)
    return store.record_miss(conn, name, reason, **row)


def _miss_row(m: BoardHit) -> CompanyIn:
    """The record_miss(**fields) payload for a discover_local miss dict:
    whatever board coordinates the attempt DID establish, so a retry starts
    from them instead of re-deriving them.

    >>> _miss_row({"name": "X", "reason": "no-board-found"})
    {'source': 'local_sourcing'}
    >>> _miss_row({"name": "X", "ats": "greenhouse", "slug": "x",
    ...            "nc": 0, "count": 4, "reason": "no-local-jobs"})["ats"]
    'greenhouse'
    >>> _miss_row({"name": "X", "ats": "workday", "slug": ("t", 5, "s"),
    ...            "reason": "no-local-jobs"})["handle"]
    't|5|s'

    Only what was established is carried, never a NULL that could overwrite
    a stored coordinate:

    >>> None in _miss_row({"name": "X", "ats": "workday", "slug": ("t", 5, "s"),
    ...                    "reason": "no-local-jobs"}).values()
    False
    """
    row: CompanyIn = {"source": "local_sourcing"}
    ats = m.get("ats")
    if not ats:
        return row
    row["ats"] = ats
    if m.get("slug"):
        row.update(cast(CompanyIn, {k: v for k, v in coords.columns(ats, m["slug"]).items()
                                    if v is not None}))
    if m.get("careers_url"):
        row["careers_url"] = m["careers_url"]
    if m.get("count"):
        row["total_job_count"] = m["count"]
    return row


def _leads(conn: sqlite3.Connection, sources: Collection[str] | None, days: int,
           limit: int | None) -> tuple[list[CompanyRow], int]:
    """(resolve_leads' rows, how many were skipped): inactive boardless rows
    whose `source` is in `sources` (None: any), less those that missed
    within `days` (0: none skipped), best mission score first, at most `limit`.

    >>> conn = store.connect(":memory:")
    >>> for n in ("Fresh", "Missed", "Other"):
    ...     _ = store.upsert_company(conn, {"name": n, "active": 0,
    ...                                     "source": "x" if n != "Other" else "y"})
    >>> _ = store.record_miss(conn, "Missed", "no-board-found")
    >>> [c["name"] for c in _leads(conn, ["x"], 14, None)[0]], _leads(conn, ["x"], 14, None)[1]
    (['Fresh'], 1)
    >>> sorted(c["name"] for c in _leads(conn, None, 0, None)[0])
    ['Fresh', 'Missed', 'Other']
    """
    where = ("COALESCE(c.ats, '') = '' AND NOT COALESCE(c.active, 0)"
             " AND (? IS NULL OR c.source IN (SELECT value FROM json_each(?)))")
    recent = ("EXISTS (SELECT 1 FROM companies m WHERE m.name = c.name"
              " AND m.miss_reason IS NOT NULL AND m.miss_at >= ?)")
    src = None if sources is None else json.dumps(list(sources))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat() if days else "9999"
    args = (src, src, cutoff)
    skipped = conn.execute(f"SELECT COUNT(*) FROM companies_effective c WHERE {where}"
                           f" AND {recent}", args).fetchone()[0]
    rows = conn.execute(f"SELECT c.* FROM companies_effective c WHERE {where} AND NOT {recent}"
                        " ORDER BY c.mission_score DESC, c.local_job_count DESC LIMIT ?",
                        (*args, limit or -1)).fetchall()
    return [store.as_company(r) for r in rows], skipped


async def resolve_leads(max_workers: int = 8,
                        sources: tuple[str, ...] = ("page_capture", "linkedin_search",
                                                     "linkedin_company_search"),
                        all_leads: bool = False, limit: int | None = None
                        ) -> list[CompanyRow | CompanyIn]:
    """Resolve boardless company leads (banked by capture.py from browsed
    LinkedIn/Indeed pages, or by manual adds) into crawlable boards and queue
    the hits for review. Careers-page SNIFF first (collision-safe), slug-probe
    fallback, every board VALIDATED by a live fetch, then mission-scored.
    The capture -> resolve-leads -> review -> crawl loop is how manually
    browsed postings grow the roster.

    sources: resolve only leads carrying one of these ``source`` values
    (default: capture.py's 'page_capture'). all_leads=True ignores the source
    filter and takes every inactive boardless lead. Idempotent — rerunning
    retries only the still-unresolved leads; a lead that missed within
    [discovery] miss_retry_days is skipped unless `all_leads`."""
    days = 0 if all_leads else config.DISCOVERY_MISS_RETRY_DAYS
    async with store.Writer() as db:
        leads, skipped = await db.run(_leads, None if all_leads else sources, days, limit)
        if skipped:
            print(f"  skipping {skipped} lead(s) that missed in "
                  f"the last {days}d (--all-leads to retry them)")
        if not leads:
            print("  No unresolved leads to resolve"
                  + ("." if all_leads else f" (source in {sources}; --all-leads to widen)."))
            return []
        print(f"  resolving {len(leads)} lead(s) (careers-page sniff -> slug-probe "
              f"fallback; every board validated by a live fetch)...")

        resolved_rows: list[CompanyRow | CompanyIn] = []
        probe_only: list[str] = []

        def report(name: str, hit: BoardHit,
                   result: tuple[CompanyRow | CompanyIn, int, bool]) -> None:
            row, active, pending = result
            resolved_rows.append(row)
            if hit.get("via") == "probe":
                probe_only.append(name)
            flag = "  [probe-only: verify]" if hit.get("via") == "probe" else ""
            mark = "queue" if pending else ("OK  " if active else "off ")
            print(f"    [{mark}] {name[:30]:30} "
                  f"{hit['ats']:12} nc={hit['nc']:<3} tot={hit['count']:<4} "
                  f"{str(row['mission_tier']):18} {score_text(row['mission_score'])}{flag}")

        # A lead is a name somebody's page mentioned, not an employer anyone
        # vouched for: resolving it produces a review candidate under the
        # lead's own name, and the lead row keeps WHY a miss missed (a
        # stalled one included) so the next run can skip it.
        _, missed = await queue_names(
            db, {c["name"]: c.get("source") or "resolve_leads" for c in leads},
            {c["name"]: u for c in leads if (u := c.get("careers_url"))},
            max_workers, local_only=False, report=report)
        for name, reason in missed:
            print(f"    [miss] {name[:34]:34} {reason}")
    queued = sum(1 for r in resolved_rows
                 if r.get("review") == "pending")
    print(f"\n  {len(resolved_rows)} board(s) resolved, "
          f"{queued} awaiting review, "
          f"{sum(r['active'] or 0 for r in resolved_rows)} activated, "
          f"{len(leads) - len(resolved_rows)} miss(es).")
    if probe_only:
        print(f"  [verify] {len(probe_only)} resolved by name-guess, not the "
              f"company's own site — sanity-check for collisions: "
              f"{', '.join(probe_only[:6])}{'...' if len(probe_only) > 6 else ''}")
    return resolved_rows
