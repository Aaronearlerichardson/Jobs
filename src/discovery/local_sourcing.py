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

import time
from contextlib import ExitStack
from datetime import datetime

from src import config
from src import tags as company_tags
from src.ats import coords
from src.match.names import name_key
from src.net.http import HEADERS, SESSION
from src.net.parallel import drain, fan_out
from .name_sources import MAJORS_WORKDAY, NAME_BLOCKLIST, _MAJORS_KEYS, gather_names
from .resolve.board import resolve_or_miss
from .resolve.probes import _nc_count_workday, _wd_search_text, probe_company
from .resolve.websearch_board import _websearch_board

# --------------------------------------------------------------------------- #
#  discover_local: the bulk pass over gathered names                          #
# --------------------------------------------------------------------------- #


_DEFAULT_WEBSEARCH_CAP = 20

def _boardless(names, hits):
    """The names with no LOCAL board yet.

    Only an nc>0 hit counts as found: a junk 0-NC slug collision must not
    stop a later pass from looking for the real employer. Asked before
    each of the three fallback passes.
    """
    have = {name_key(h["name"]) for h in hits if h["nc"] > 0}
    return [n for n in names if name_key(n) not in have]


def _hit_from_detection(name, det):
    """A sniff/websearch DETECTION turned into a hit, by asking the board
    how many LOCAL jobs it actually holds.

    The two fallback passes each wrote this out, and had drifted: only the
    websearch copy special-cased a `custom` board. Coordinates go through
    src.ats.coords now (the rule for every other board write in the repo),
    which handles Workday's triple, a plain slug and a careers-URL-only
    custom board without a branch per caller.

    The `reason` rides along and is popped by the caller when nc>0:
    coordinates that read empty are a DEAD board, which is a different
    miss from a name nothing could be found for.
    """
    from src.ats.fetchers import company as company_fetch
    ats = det["ats"]
    slug = det["triple"] if ats == "workday" else det.get("slug")
    try:
        jobs = company_fetch.fetch_company_nc(
            coords.columns(ats, slug, det.get("careers_url")))
    except Exception:
        jobs = []
    nc = len(jobs)
    return {"name": name, "ats": ats, "slug": slug, "count": nc, "nc": nc,
            "careers_url": det.get("careers_url"),
            "reason": "board-dead:" + ats}


def _resolve_pass(todo, resolve_one, tag, hits, misses, max_workers):
    """Run one fallback resolver over `todo`, appending to `hits` (nc>0) or
    `misses` (anything else), under the stall watchdog.

    drain_or_abandon, not a plain pool: one wedged resolution used to hold
    the web UI's single op slot until the app was restarted.
    """
    def _done(fut, n):
        h = fut.result()
        if h and h.get("nc"):
            h.pop("reason", None)
            hits.append(h)
            # Widths chosen so both tags print one aligned table.
            print(f"    {tag} {h['name']:{35 - len(tag)}} {h['ats']:14} "
                  f"{h['slug']!s:26} nc={h['nc']}")
        elif h:
            misses.append(h)

    drain(todo, resolve_one, _done,
          lambda n: misses.append({"name": n, "reason": "fetch-error:stalled"}),
          max_workers=max_workers)


def _probe_pass(names, max_workers):
    """Name-guessed slug probes over every candidate. The cheap first pass:
    no page fetched, just the platforms' own APIs."""
    n_wd = sum(1 for n in names if name_key(n) in _MAJORS_KEYS)
    print(f"  probing {len(names)} candidate compan(ies) for live ATS boards "
          f"({n_wd} with Workday fallback)...")
    # A probe that raises is now reported and skipped rather than ending
    # the pass -- this was the last pool in src/ with a bare fut.result(),
    # the same shape as the prune_dead_boards bug.
    return [h for h in fan_out(names,
                               lambda n: probe_company(n, name_key(n) in _MAJORS_KEYS),
                               "probe", max_workers) if h]


def _js_workday_pass(hits, max_workers):
    """Re-probe the MAJORS that got no board, with a headless browser.

    Big employers often have React/SPA careers pages whose
    myworkdayjobs.com link only appears after JS runs, so the static probe
    misses them entirely.
    """
    missed = _boardless(MAJORS_WORKDAY, hits)
    import importlib.util
    if importlib.util.find_spec("playwright.sync_api") is None:
        if missed:
            print(f"    [js] playwright not installed; skipping JS probe "
                  f"of {len(missed)} major(s)")
        return
    if not missed:
        return
    from .resolve.probes import WorkdayJsProbe
    # Parallel across DIFFERENT sites is safe: each target still sees
    # exactly one page load; the serial design existed for sync-
    # Playwright's thread affinity, not politeness. Each probe instance
    # already pins its browser to its own dedicated thread, so K instances
    # + K caller threads = K-way parallelism with the thread-safety model
    # untouched. K is memory-bound (one headless Chromium each), so it is
    # capped low and separate from the HTTP worker count.
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

        # `i` picks the browser, so the items are (index, name) pairs.
        # on_error keeps this pass's own wording rather than fan_out's.
        for h in fan_out(list(enumerate(missed)), lambda im: _js_one(*im),
                         "JS probe", k,
                         on_error=lambda im, e: print(
                             f"    [!] JS probe failed for {im[1]!r}: {e}")):
            if h:
                hits.append(h)
                t, p, s = h["slug"]
                print(f"    [JS-OK] {h['name']:30} {t}/{p}/{s}  "
                      f"nc={h['nc']}/{h['count']}")


def _sniff_pass(names, hits, misses, max_workers):
    """Fetch each still-boardless name's careers page and read the ATS +
    exact slug off it. The main recall lever over the directory: it covers
    every hosted platform and finds slugs the name-guesser cannot."""
    from .resolve.sniffer import sniff_ats

    todo = _boardless(names, hits)
    print(f"  sniffing careers pages for {len(todo)} name(s) without a board...")

    def _sniff_one(n):
        s = sniff_ats(n)
        return _hit_from_detection(n, s) if s else {
            "name": n, "reason": "no-board-found"}

    _resolve_pass(todo, _sniff_one, "[SNIFF]", hits, misses, max_workers)


def _websearch_pass(names, hits, misses, max_workers, cap, retry_days):
    """Search the web for a careers page, for names probe+sniff could not
    board. A measured 60-company gap study found 5 of 6 eventual
    resolutions came through here -- names on gov/acronym/product-named
    domains the slug-guesser and the careers-page sniff cannot reach.

    Capped, and recent misses skipped, because DDG rate-limits hard: an
    earlier uncapped profile blocked ~1271s of a 1726s run inside DDG's
    own retry/backoff (see src.net.ddg).
    """
    cap = (config.DISCOVERY_WEBSEARCH_CAP if cap is None else cap)
    cap = _DEFAULT_WEBSEARCH_CAP if cap is None else int(cap)
    todo = _boardless(names, hits)
    if todo and cap > 0:
        from src.store import connect as _connect, recent_miss_names
        conn = _connect()
        try:
            recent = recent_miss_names(conn, days=retry_days)
        finally:
            conn.close()
        todo = [n for n in todo if n not in recent][:cap]
    else:
        todo = []
    print(f"  websearch-resolving {len(todo)} name(s) without a board "
          f"(cap={cap})...")
    if not todo:
        return
    t0 = time.time()

    def _websearch_one(n):
        w = _websearch_board(n)
        return _hit_from_detection(n, w) if w else {
            "name": n, "reason": "no-board-found"}

    _resolve_pass(todo, _websearch_one, "[WEBSEARCH]", hits, misses,
                  max_workers)
    print(f"  websearch pass: {time.time() - t0:.1f}s for "
          f"{len(todo)} name(s)")


def _reduce_hits(names, hits, pass_misses):
    """Everything the passes found, accounted for exactly once.

    Returns (hits, confirmed, dropped, misses). Every candidate that is
    not confirmed is a MISS with a reason: a live board with no local
    openings, a board whose coordinates read empty, or a name nothing
    could be found for.
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
    by_board = {}
    for h in hits:
        key = (h["ats"], str(h["slug"]))
        if key not in by_board or len(h["name"]) < len(by_board[key]["name"]):
            by_board[key] = h
    hits = list(by_board.values())

    # Split on the locality check: nc>0 is confirmed-local; nc==0 is either
    # a false-positive slug collision or a non-local employer -- dropped,
    # but shown.
    confirmed = sorted([h for h in hits if h["nc"] > 0],
                       key=lambda h: h["nc"], reverse=True)
    dropped = sorted([h for h in hits if h["nc"] == 0],
                     key=lambda h: h["name"].lower())
    misses = {}
    for h in dropped:
        misses[h["name"]] = {**h, "reason": "no-local-jobs"}
    for h in pass_misses:
        misses.setdefault(h["name"], h)
    for n in names:
        if n not in boarded:
            misses.setdefault(n, {"name": n, "reason": "no-board-found"})
    for h in confirmed:
        misses.pop(h["name"], None)
    return hits, confirmed, dropped, sorted(misses.values(),
                                            key=lambda m: m["name"].lower())


def _report_discovery(hits, confirmed, dropped, misses):
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


def discover_local(extra_names=None, max_workers=12, js_majors=True, sniff=True,
                   websearch=True, websearch_cap=None, websearch_retry_days=14):
    """
    Gather names + probe each. Returns (confirmed, checked, misses) where
    confirmed is a list of NC-local hit dicts and misses is one dict per
    candidate that did NOT become one, carrying a ``reason`` code from
    src.store.MISS_REASONS.

    Four passes, cheapest first, each one only looking at the names the
    ones before it could not board (_boardless):

      1. _probe_pass       name-guessed slugs against the platform APIs
      2. _js_workday_pass  headless browser, MAJORS only (``js_majors``)
      3. _sniff_pass       fetch the careers page and read the ATS off it
      4. _websearch_pass   search the web for one, capped (``websearch``)

    then _reduce_hits accounts for every candidate exactly once and
    _report_discovery prints the scoreboard.

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
    names = gather_names(extra_names)
    misses = []
    hits = _probe_pass(names, max_workers)
    if js_majors:
        _js_workday_pass(hits, max_workers)
    if sniff:
        _sniff_pass(names, hits, misses, max_workers)
    if websearch:
        _websearch_pass(names, hits, misses, max_workers, websearch_cap,
                        websearch_retry_days)

    hits, confirmed, dropped, misses = _reduce_hits(names, hits, misses)
    _report_discovery(hits, confirmed, dropped, misses)
    return confirmed, names, misses

# --------------------------------------------------------------------------- #
#  Sampling and store writes (populate / add_board / resolve_leads)           #


def _sample_titles(hit, n=6):
    """Fetch a few job titles from a confirmed board for mission context."""
    ats, slug = hit["ats"], hit["slug"]
    try:
        if ats == "greenhouse":
            r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
                             timeout=config.PROBE_TIMEOUT, headers=HEADERS)
            return [j.get("title", "") for j in r.json().get("jobs", [])[:n]]
        if ats == "lever":
            r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                             timeout=config.PROBE_TIMEOUT, headers=HEADERS)
            return [j.get("text", "") for j in r.json()[:n]]
        if ats == "ashby":
            r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                             timeout=config.PROBE_TIMEOUT, headers=HEADERS)
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
                              timeout=config.PROBE_TIMEOUT, headers={**HEADERS, "Content-Type": "application/json"})
            return [j.get("title", "") for j in r.json().get("jobPostings", [])[:n]]
    except Exception:
        return []
    return []


def _board_already_tracked(conn, row):
    """The roster company that already owns `row`'s board under ANOTHER
    name, or None. Every discovery path checks names before resolving, but
    a name the roster spells differently ("SAS" for "SAS Institute", "Veeva
    Systems" for "Veeva", "NVIDIA AI" for "NVIDIA" — all re-added on
    2026-09-01) passes that check and then resolves to a board that is
    already on file; until the next dedup the crawl fetched the board twice
    and the ranking showed two companies. Same-name matches are NOT dups —
    that is the ordinary re-probe/update path — so the caller may upsert."""
    from src.store import company_by_board
    existing = company_by_board(conn, row)
    if not existing:
        return None
    if name_key(existing.get("name")) == name_key(row.get("name")):
        return None
    return existing


def _report_dup_board(name, existing):
    print(f"    [dup]  {name[:30]:30} same {existing.get('ats') or '?'} board "
          f"as '{existing.get('name')}' - already tracked, not added")


def _score_hit(hit):
    """(tier, score, reason) for a resolved board: a few live job titles
    (_sample_titles) as domain context for src.claude.score_company_mission.
    Pure network I/O, safe to run off the main thread."""
    from src.claude.api import score_company_mission
    titles = _sample_titles(hit)
    return score_company_mission(hit["name"], " | ".join(t for t in titles if t))


def score_and_upsert(conn, hit, source, include_missions=None, tags=None,
                     scored=None):
    """Mission-score a resolved board and write it to the store as a review
    candidate -- the one write path behind every automated add surface.

    `hit` is a resolver result: {name, ats, slug, nc, count} plus an optional
    careers_url, `slug` being the (tenant, pod, site) triple for Workday and
    None for a custom board. Returns (row, active, pending) -- the row as
    written, whether a reviewer's confirmation would activate it
    (src.claude.is_active_mission), and whether it went to the review queue
    -- or None when the board is already on the roster under ANOTHER name
    (_board_already_tracked). The dedup runs before the mission call, so a
    duplicate costs no LLM request; a caller that scored in a worker pool
    first (populate_companies) passes the result as `scored`.

    The row is inactive and tagged pending-review unless the store has
    already confirmed the name (src.store.is_confirmed_company). `tags`
    defaults to the local scope tag when the board has local jobs; a caller
    with another reason to call the company local (ats_dork's HQ signal)
    passes it explicitly.

    Notes:
        This sequence was spelled out at four sites (populate_companies,
        resolve_leads, paste_ingest.add_names, ats_dork.harvest_urls), each
        with small drift: two checked duplicates only after paying for the
        score, one never checked, two stamped last_probed and two left it to
        the store (which stamps it on insert anyway). add_board is not a
        fifth: a board the user registered by URL is written active
        regardless of mission tier, and carries no total count. The two
        copies in src/ops/maintenance.py (add_job's manual add, which writes
        straight to the roster, and reresolve_misses, which clears the old
        board coordinates first) still differ in ways this helper does not
        cover.
    """
    from src.claude.api import is_active_mission
    from src.store import is_confirmed_company, mark_pending, upsert_company

    name = hit["name"]
    row = coords.from_hit(hit, name=name)
    dup = _board_already_tracked(conn, row)
    if dup:
        _report_dup_board(name, dup)
        return None
    tier, score, reason = scored if scored is not None else _score_hit(hit)
    # Shared activation rule (src.claude.is_active_mission): active tiers,
    # an UNAVAILABLE (None) score, or a multi-division conglomerate whose
    # subdivisions are filtered at crawl time.
    active = is_active_mission(tier, name, include_missions)
    nc = hit.get("nc") or 0
    row.update({
        "local_job_count": nc, "total_job_count": hit.get("count"),
        "mission_tier": tier, "mission_score": score, "mission_reason": reason,
        "tags": (company_tags.LOCAL if nc else None) if tags is None else tags,
        "source": source, "active": active,
        "last_probed": datetime.now().isoformat(),
    })
    # Nothing an automated pass finds joins the roster by itself: a name
    # the store has never confirmed lands in the review queue
    # (src.store.mark_pending) for a person to accept or reject.
    pending = not is_confirmed_company(conn, name)
    if pending:
        row = mark_pending(row)
    upsert_company(conn, row)
    return row, active, pending


def populate_companies(extra_names=None, include_missions=None, dork=True):
    """
    Full sourcing pass → SQL store: discover NC-local boards, score each
    company's MISSION once (cached), and upsert into the `companies` table.
    Every company new to the store lands in the REVIEW QUEUE — inactive,
    tagged pending-review (src.store.mark_pending) — so a bulk pass cannot
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
    from src.store import connect, miss_counts, record_miss

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
        return h, _score_hit(h)

    def _score_done(fut, name):
        try:
            h, scored = fut.result()
        except Exception as e:
            print(f"    [!] mission scoring failed for {name!r}: {e}")
            return
        result = score_and_upsert(conn, h, source="local_sourcing",
                                  include_missions=include_missions,
                                  scored=scored)
        if not result:
            return
        row, active, pending = result
        written.append(dict(row))
        tier, score, reason = scored
        flag = ("PENDING REVIEW" if pending
                else "active" if active else "INACTIVE(other)")
        ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
        print(f"    {h['name']:30} {str(tier):20} {ss}  [{flag}]  ({reason})")
    drain(confirmed, _score_one, _score_done, lambda name: None,
          label=lambda h: h["name"], max_workers=8)
    conn.close()

    if dork:
        print("\n  ATS-dork sweep (search-indexed board URLs)...")
        try:
            # Still deferred, but for a different reason than before: dork
            # imports this module's write path (score_and_upsert), so the
            # two are peers in the sourcing layer with an orchestration
            # edge back. That is a file-level cycle inside one package, not
            # a package-level one -- what the deferred import used to hide
            # was src/ats depending on src/discovery.
            from .dork import run_ddgs_dorks
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
    from src.claude.api import score_company_mission
    from src.ats.fetchers import company as company_fetch
    from src.store import (CAPTURE_ATS, connect, is_confirmed_company,
                            mark_pending, upsert_company)
    from src.ats.signatures import detect, pack
    from .resolve.sniffer import sniff_ats

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

    hit = detect("", url)
    if hit and hit[0] in ("fetchable", "semi"):
        found = pack(hit[1], hit[2], url)
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
    # `slug` above is the Workday triple, or the URL as a fallback label for
    # the mission sample; the COORDINATE for every other platform is the
    # sniffed one.
    board = coords.columns(
        ats, slug if ats == "workday" else found.get("slug"),
        found.get("careers_url") or url, name=name)
    dup = _board_already_tracked(conn, board)
    if dup:
        _report_dup_board(name, dup)
        conn.close()
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
    pending = not is_confirmed_company(conn, name)
    if pending:
        row = mark_pending(row)
    upsert_company(conn, row)
    conn.close()
    ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
    print(f"  [OK] {name}: {ats} {slug!s}  nc={nc}  mission={tier} ({ss})  "
          f"{'PENDING REVIEW' if pending else 'ACTIVE'}")
    return found


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
    from src.claude.api import ACTIVE_MISSION_TIERS, score_company_mission
    from src.store import connect, get_companies, upsert_company

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
    def _scored(fut, name):
        nonlocal n
        try:
            c, (tier, score, reason) = fut.result()
        except Exception as e:
            print(f"    [!] {name}: {e}")
            return
        if tier is None and score is None:
            return            # scoring unavailable - leave the row alone
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
        # NOT src.claude.is_active_mission: this is the REACTIVATION
        # half, and it deliberately does not revive on `tier is None`.
        # A None tier with a non-None score means the model answered with
        # a mission name outside the profile's taxonomy (score_company_
        # mission nulls the tier but keeps the score), so the `return`
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
    drain(cos, _one, _scored, lambda name: None,
          label=lambda c: c["name"], max_workers=max_workers)
    conn.close()
    print(f"\n  {n} compan(ies) scored.")
    return n


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
    from src.store import (connect, get_companies as _store_companies,
                            record_miss, recent_miss_names)

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
    by_name = {c["name"]: c for c in leads}

    def _stalled(name):
        # A lead whose domains blackhole becomes a recorded miss, not a
        # hung command (src.net.parallel.drain_or_abandon).
        record_miss(conn, name, "fetch-error:stalled",
                    source=by_name[name].get("source"))

    def _consume(fut, name):
        c = by_name[name]
        hit, reason = resolved(fut, name)
        if not hit:
            # Was printed and forgotten; now the lead row keeps WHY, so
            # the next run can skip it and the user can see the tally.
            record_miss(conn, c["name"], reason, source=c.get("source"))
            print(f"    [miss] {c['name'][:34]:34} {reason}")
            return
        # A lead is a name somebody's page mentioned, not an employer
        # anyone vouched for: resolving it produces a review candidate,
        # written under the lead's own name.
        result = score_and_upsert(conn, {**hit, "name": c["name"]},
                                  source=c.get("source") or "resolve_leads")
        if not result:
            return
        row, active, pending = result
        resolved.append(row)
        if hit.get("via") == "probe":
            probe_only.append(c["name"])
        tier, score = row["mission_tier"], row["mission_score"]
        ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
        flag = "  [probe-only: verify]" if hit.get("via") == "probe" else ""
        mark = "queue" if pending else ("OK  " if active else "off ")
        print(f"    [{mark}] {c['name'][:30]:30} "
              f"{hit['ats']:12} nc={hit['nc']:<3} tot={hit['count']:<4} "
              f"{str(tier):18} {ss}{flag}")
    drain(leads,
          lambda c: resolve_or_miss(c["name"], c.get("careers_url") or ""),
          _consume, _stalled, label=lambda c: c["name"],
          max_workers=max_workers)
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
