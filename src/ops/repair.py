"""Roster repair: deactivate dead boards, retry the rows that never
resolved, rename boards still named after their own slug."""

from datetime import datetime, timedelta

from src import config
from src import store
from src import tags
from src.ats import coords
from src.ats.board import BOARDS, board_for
from src.net.parallel import drain, fan_out
from src.ops.maintenance import _DEAD_BOARD_FAMILY, _t, track_store


def prune_dead_boards(conn, max_workers=12, deactivate_offmission=False):
    """Deactivate active companies whose JSON-API ATS board no longer resolves
    (a hard 404/error, the source of the crawl's `HTTP 404` spam), and
    optionally off-mission `other`-tier companies (excluding multi-division).
    Only ATSes whose board endpoint cleanly distinguishes "exists" (200)
    from "dead" (404) are probed. Returns (n_dead, n_offmission).

    Prints how many boards it probes, then one line per company it
    deactivates (name, ATS, reason), so a clean run still leaves a trace.

    Notes:
        Lived in src.store until 2026-09-10; it probes the network and
        applies roster policy, so it is an operation, and the store keeps
        only the write (store.deactivate_company).
    """
    # Board.alive, not the slug probe: an empty board is alive; dead means
    # the board REQUEST fails.
    PROBE = {b.name: b.alive for b in BOARDS.values() if b.spec.prunable}

    rows = [c for c in store.get_companies(conn, active_only=True)
            if c.get("ats") in PROBE and c.get("slug")]
    print(f"  probing {len(rows)} board(s) for a dead ATS endpoint...")

    def _check(c):
        ok, _ = PROBE[c["ats"]](c["slug"])
        return c, ok

    # A probe that raises is now reported and skipped rather than killing
    # the whole prune -- this was the one pool here with no try at all, so
    # a single unreachable host aborted the pass over every other board.
    dead = [c for c, ok in fan_out(rows, _check, "board probe", max_workers)
            if not ok]
    with store.batch(conn):
        for c in dead:
            store.deactivate_company(
                conn, c["id"],
                note=f"deactivated: dead {c['ats']} board '{c['slug']}'")
            print(f"    [dead]  {c['name'][:30]:30} {c['ats']:10} "
                  f"board '{c['slug']}' no longer resolves")

        n_off = 0
        if deactivate_offmission:
            # Watched companies are exempt: a watch tag is the user
            # deliberately keeping an off-mission employer crawled.
            off = [c for c in store.get_companies(conn, active_only=True)
                   if c.get("mission_tier") == "other"
                   and not config.is_multi_division(c.get("name"))
                   and not tags.has(c, tags.WATCH)]
            for c in off:
                store.deactivate_company(conn, c["id"])
                print(f"    [other] {c['name'][:30]:30} {c['ats'] or '?':10} "
                      f"off-mission (score={c.get('mission_score')})")
            n_off = len(off)
    return len(dead), n_off


# --------------------------------------------------------------------------- #
#  Re-resolution of rows that died at resolution                               #
# --------------------------------------------------------------------------- #
#
# A name that never resolved to a board is kept as an inactive row carrying a
# miss_reason (src.store.record_miss), and the two families below are the
# ones worth another attempt: nothing was found at all ("no-board-found"), or
# coordinates were found and the live fetch came back empty ("board-dead").
# Neither is a permanent verdict — a resolver improves, a company migrates
# ATS, a careers page comes back — and that bucket is where the roster's
# best-known local employers sit. The other families are not retried here:
# "no-local-jobs" already IS a live board, "ats-unsupported" needs a fetcher
# rather than a retry, and "fetch-error" is a transient every pass re-attempts
# anyway.
RERESOLVE_FAMILIES = ("no-board-found", _DEAD_BOARD_FAMILY)

# A board that is not a resolution failure at all -- ats/slug are set, the
# harvester keeps fetching it without error -- but that has LISTED nothing
# in a week or more. total_job_count=0 forever most often means the slug
# resolves to nothing real (a 200 OK with an empty body, not a 404: Lever
# and some others answer this way for a retired or mistyped tenant), which
# a live re-sniff can catch the same way it catches a dead board. Not one
# of RERESOLVE_FAMILIES because it carries no miss_reason of its own -- see
# _silent_board_candidates -- so a pass opts into it through
# reresolve_misses's `families` rather than getting it by default.
SILENT_FAMILY = "silent-board"
# How long a board must have listed nothing before it counts as silent
# (last_nonempty_at, or created_at when it never had one, at least this
# old), and how recently it must still have been harvested to count as
# "still being tracked" rather than abandoned.
SILENT_DAYS = 7
SILENT_HARVESTED_WITHIN_DAYS = 3


def _silent_board_candidates(conn, now=None):
    """Harvested boards that have listed nothing in >= SILENT_DAYS days --
    a resolution that stopped being true, not a resolution failure (those
    are RERESOLVE_FAMILIES's job). Carries no miss_reason of its own, so
    this is a separate query rather than another WHERE clause on one: a
    miss-family row and a silent-board row have almost nothing in common
    to select on.

    A row qualifies only when ALL of:
      * it has a real, fetchable board (an `ats`, not the capture-only one)
      * `miss_reason` is NULL -- one already on a miss-remediation path
        (including a promoted 'board-dead:<ats>', mark_harvested's own
        cycle) is that path's to retry, not this one's to re-select;
      * `total_job_count` is 0;
      * `last_harvested_at` is within SILENT_HARVESTED_WITHIN_DAYS days --
        still being actively harvested, not simply a board the run has
        stopped visiting;
      * `last_nonempty_at` is NULL or >= SILENT_DAYS days old;
      * `created_at` is also >= SILENT_DAYS days old, or NULL (the row
        predates the column) -- so a board harvested for the first time
        this morning (last_nonempty_at NULL, same as a chronically silent
        one) is not selected before it has actually had a week.

    Oldest evidence first (last_nonempty_at, falling back to created_at for
    a board that never had one):

    >>> from src.store import connect, upsert_company
    >>> from datetime import datetime, timedelta
    >>> conn = connect(":memory:")
    >>> old = (datetime.now() - timedelta(days=30)).isoformat()
    >>> recent = datetime.now().isoformat()

    (last_harvested_at/last_nonempty_at are crawl-scheduling columns
    upsert_company does not accept -- like mark_harvested, this sets them
    with a raw UPDATE.)

    >>> cid = upsert_company(conn, {"name": "Stale", "ats": "lever",
    ...                             "slug": "stale", "total_job_count": 0})
    >>> _ = conn.execute("UPDATE companies SET created_at=?, "
    ...                  "last_harvested_at=? WHERE id=?", (old, recent, cid))
    >>> [c["name"] for c in _silent_board_candidates(conn)]
    ['Stale']

    A board harvested for the first time this week is not silent yet --
    even though it too has never had a nonempty pass:

    >>> cid_new = upsert_company(conn, {"name": "New", "ats": "lever",
    ...                                 "slug": "n", "total_job_count": 0})
    >>> _ = conn.execute("UPDATE companies SET last_harvested_at=? "
    ...                  "WHERE id=?", (recent, cid_new))
    >>> [c["name"] for c in _silent_board_candidates(conn)]
    ['Stale']

    Neither is one that DID list something recently, one no longer being
    harvested at all, or one already on a miss-remediation path of its
    own:

    >>> cid2 = upsert_company(conn, {"name": "Fine", "ats": "lever",
    ...                              "slug": "f", "total_job_count": 0})
    >>> _ = conn.execute("UPDATE companies SET created_at=?, "
    ...                  "last_harvested_at=?, last_nonempty_at=? "
    ...                  "WHERE id=?", (old, recent, recent, cid2))
    >>> cid3 = upsert_company(conn, {"name": "Abandoned", "ats": "lever",
    ...                              "slug": "ab", "total_job_count": 0})
    >>> _ = conn.execute("UPDATE companies SET created_at=? WHERE id=?",
    ...                  (old, cid3))
    >>> cid4 = upsert_company(conn, {"name": "Erroring", "ats": "lever",
    ...                              "slug": "e", "total_job_count": 0})
    >>> _ = conn.execute("UPDATE companies SET created_at=?, "
    ...                  "last_harvested_at=?, "
    ...                  "miss_reason='fetch-error:harvest' WHERE id=?",
    ...                  (old, recent, cid4))
    >>> [c["name"] for c in _silent_board_candidates(conn)]
    ['Stale']
    """
    from src.store.companies import CAPTURE_ATS

    now = now or datetime.now()
    silent_cut = (now - timedelta(days=SILENT_DAYS)).isoformat()
    harvested_cut = (now - timedelta(days=SILENT_HARVESTED_WITHIN_DAYS)
                    ).isoformat()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM companies WHERE ats IS NOT NULL AND ats != ? "
        "AND miss_reason IS NULL "
        "AND COALESCE(total_job_count, 0) = 0 "
        "AND last_harvested_at IS NOT NULL AND last_harvested_at >= ? "
        "AND COALESCE(created_at, '') <= ? "
        "AND (last_nonempty_at IS NULL OR last_nonempty_at <= ?) "
        "ORDER BY COALESCE(last_nonempty_at, created_at) ASC, name ASC",
        (CAPTURE_ATS, harvested_cut, silent_cut, silent_cut)).fetchall()]


def _reresolve_candidates(conn, days=None, names=None, limit=50,
                          families=RERESOLVE_FAMILIES):
    """The rows a re-resolution pass should retry, oldest evidence first.

    Only the requested miss families are selected among rows the crawl is
    not already using (`active = 0`, same as every miss — see
    src.store.record_miss):

    >>> from src.store import connect, record_miss, upsert_company
    >>> conn = connect(":memory:")
    >>> for n, r in [("Emmes", "no-board-found:wrong-domain"),
    ...              ("Advarra", "board-dead:icims"),
    ...              ("Chiesi", "no-local-jobs"),
    ...              ("Locus", "ats-unsupported:ukg")]:
    ...     _ = record_miss(conn, n, r)
    >>> _ = upsert_company(conn, {"name": "Guardant", "ats": "lever",
    ...                           "active": 1})
    >>> _ = conn.execute("UPDATE companies SET miss_at='2026-01-01' "
    ...                  "WHERE name='Advarra'")
    >>> [c["name"] for c in _reresolve_candidates(conn)]
    ['Advarra', 'Emmes']

    `limit` bounds the pass, and the oldest miss goes first — a hit clears
    the row's miss and a repeated miss re-stamps `miss_at`, so successive
    bounded runs work through the backlog instead of re-probing the same
    head of it:

    >>> [c["name"] for c in _reresolve_candidates(conn, limit=1)]
    ['Advarra']

    `days` keeps only rows whose miss is at least that old, so a nightly
    run does not re-probe what this morning already failed:

    >>> [c["name"] for c in _reresolve_candidates(conn, days=30)]
    ['Advarra']

    `names` restricts the pass to specific companies, matched
    case-insensitively; it narrows the same selection rather than widening
    it, so a name that is not a retryable miss is still not selected:

    >>> [c["name"] for c in _reresolve_candidates(conn, names=["emmes"])]
    ['Emmes']
    >>> _reresolve_candidates(conn, names=["Chiesi", "Guardant"])
    []

    Passing SILENT_FAMILY alongside the miss families ALSO selects
    harvested boards that have listed nothing in SILENT_DAYS+ days (see
    _silent_board_candidates) -- these carry no miss_reason of their own,
    so they are appended after the miss-backlog rows (a bounded run works
    the classic miss backlog first) rather than interleaved by `miss_at`:

    >>> cid = upsert_company(conn, {"name": "Quiet", "ats": "lever",
    ...                             "slug": "quiet", "total_job_count": 0})
    >>> old = (datetime.now() - timedelta(days=30)).isoformat()
    >>> _ = conn.execute("UPDATE companies SET created_at=?, "
    ...                  "last_harvested_at=? WHERE name='Quiet'",
    ...                  (old, datetime.now().isoformat()))
    >>> [c["name"] for c in _reresolve_candidates(
    ...     conn, families=RERESOLVE_FAMILIES + (SILENT_FAMILY,))]
    ['Advarra', 'Emmes', 'Quiet']
    >>> [c["name"] for c in _reresolve_candidates(conn)]
    ['Advarra', 'Emmes']
    """
    wanted = {str(n).strip().lower() for n in (names or []) if str(n).strip()}
    cutoff = ((datetime.now() - timedelta(days=int(days))).isoformat()
              if days else None)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies WHERE COALESCE(active, 0) = 0 "
        "AND miss_reason IS NOT NULL "
        "ORDER BY COALESCE(miss_at, '') ASC, name ASC").fetchall()]
    rows = [r for r in rows if store.miss_family(r["miss_reason"]) in families]
    if SILENT_FAMILY in families:
        rows += _silent_board_candidates(conn)

    def since(r):
        # A NULL miss_at predates the column: unknown age, so old enough.
        if r["miss_reason"]:
            return r.get("miss_at") or ""
        return r.get("last_nonempty_at") or r.get("created_at") or ""

    out = [r for r in rows
           if (not wanted or (r["name"] or "").strip().lower() in wanted)
           and not (cutoff and since(r) > cutoff)]
    return out[:int(limit)] if limit else out


def reresolve_misses(conn=None, limit=50, max_workers=6, days=None,
                     names=None, t=None, families=RERESOLVE_FAMILIES,
                     commit=True):
    """Retry the roster rows that died at resolution; queue every hit for
    human review. Returns the rows written.

    A hit is written onto the EXISTING row (same name): its board
    coordinates, its mission score, `active=0`, and the `pending-review`
    scope tag merged into whatever tags the row already carried. Writing
    the board clears the row's miss (src.store.upsert_company). A repeated
    miss just re-stamps miss_reason/miss_at, which moves the row to the back
    of the queue `_reresolve_candidates` orders by.

    `families` picks what is retried (default RERESOLVE_FAMILIES; an empty
    value means the default). SILENT_FAMILY adds harvested boards that
    have listed nothing in SILENT_DAYS+ days (no miss_reason of their own
    -- see _silent_board_candidates); they are tried after every
    miss-family row, so `families=(SILENT_FAMILY,)` is how a bounded pass
    reaches them. A hit on one is written exactly like any other family's.
    resolve_or_miss only calls a board a hit when a live fetch lists jobs,
    so the silent coordinates themselves never come back as one; a miss on
    an inactive row is recorded as usual (record_miss declines on an
    active row, which then stays a candidate).

    `commit=False` previews the pass: every sniff runs and every line is
    printed, but nothing is written and no mission score is requested;
    the would-be retargets are returned.

    Notes:
        Deliberately writes nothing else on the row — the roster review
        queue reads exactly `active=0` plus that tag, and confirming a row
        there is what makes it crawlable. Rows are never activated here: a
        re-resolved board is a claim about a company nobody has looked at
        in months, and the resolver's own collision guards are not a
        substitute for that look.

        Resolution runs through the same stall watchdog every other bulk
        resolution path uses (src.net.parallel.drain_or_abandon):
        one wedged careers-page fetch must not hold the web UI's
        one-op-at-a-time slot.
    """
    from src.claude.api import score_company_mission
    from src.discovery.local_sourcing import (_board_already_tracked,
                                              _report_dup_board,
                                              mission_context)
    from src.discovery.resolve.board import resolve_or_miss, resolved
    from src.match.names import junk_name_reason

    families = tuple(families or RERESOLVE_FAMILIES)
    unknown = set(families) - {*RERESOLVE_FAMILIES, SILENT_FAMILY}
    if unknown:
        raise ValueError(f"unknown reresolve families: {sorted(unknown)}")
    t = _t(t)
    with track_store(t, conn) as conn:
        rows = _reresolve_candidates(conn, days=days, names=names, limit=limit,
                                     families=families)
        # Misses recorded before the paste screen existed include section
        # headings and category nouns ("Required Qualifications",
        # "Proficiency in SQL.", "Oncology"). Re-stamp them into the
        # 'junk-name' family, which no pass retries, instead of paying a
        # sniff, two web searches and a stall slot for each again.
        junk = [(r, junk_name_reason(r["name"])) for r in rows]
        miss = store.record_miss if commit else (lambda *a, **k: False)
        for r, why in junk:
            if why:
                miss(conn, r["name"], f"junk-name:{why}")
                print(f"    [junk]    {r['name'][:30]:30} {why} - "
                      f"{'retired' if commit else 'would be retired'} "
                      f"from the retry queue")
        rows = [r for r, why in junk if not why]
        if not rows:
            print("  no re-resolvable misses "
                  f"(families: {', '.join(families)}).")
            return []
        print(f"  re-resolving {len(rows)} miss(es) "
              f"(careers-page sniff -> slug-probe -> web search; every board "
              f"validated by a live fetch)...")
        # A silent-board candidate carries no miss_reason of its own (that
        # is the point of the family), so `was` falls back to naming the
        # family for the [miss]/[pending] print lines below.
        was = {r["name"]: (r["miss_reason"] or SILENT_FAMILY) for r in rows}
        written, still, dups = [], [], []

        def _stalled(name):
            miss(conn, name, "fetch-error:stalled")
            still.append((name, "fetch-error:stalled"))

        def _consume(fut, name):
            hit, reason = resolved(fut, name)
            if not hit:
                miss(conn, name, reason)
                still.append((name, reason))
                print(f"    [miss]    {name[:30]:30} {was[name]} -> {reason}")
                return
            board = coords.from_hit(hit, name=name)
            dup = _board_already_tracked(conn, board)
            if dup:
                # Someone else already holds this board. Leave the row as
                # the miss it was, but re-stamp it so a bounded rerun moves
                # past it instead of paying for the same fetch every night.
                _report_dup_board(name, dup)
                miss(conn, name, was[name])
                dups.append(name)
                return
            if not commit:
                written.append(board)
                print(f"    [preview] {name[:30]:30} {hit['ats']:12} "
                      f"{coords.board_slug(board) or board.get('careers_url') or ''} "
                      f"nc={hit['nc']:<3} "
                      f"tot={hit['count']:<4} (was {was[name]})")
                return
            tier, score, reason = score_company_mission(
                name, mission_context(hit))
            # upsert_company drops None values so it can never erase a
            # stored one — which would leave the dead board's slug beside
            # the new Workday triple. Clear the coordinate columns first.
            conn.execute("UPDATE companies SET slug=NULL, wd_tenant=NULL, "
                         "wd_pod=NULL, wd_site=NULL WHERE name=?", (name,))
            store.upsert_company(conn, {
                **board,
                "local_job_count": hit["nc"], "total_job_count": hit["count"],
                "mission_tier": tier, "mission_score": score,
                "mission_reason": reason,
                "tags": tags.PENDING, "active": 0,
                "last_probed": datetime.now().isoformat(),
            })
            written.append(board)
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            print(f"    [pending] {name[:30]:30} {hit['ats']:12} "
                  f"nc={hit['nc']:<3} tot={hit['count']:<4} "
                  f"{str(tier):18} {ss}  (was {was[name]})")

        drain(rows,
              lambda r: resolve_or_miss(r["name"], r.get("careers_url") or ""),
              _consume, _stalled, label=lambda r: r["name"],
              max_workers=max_workers)
        conn.commit()
        print(f"\n  {len(written)} board(s) "
              + ("re-resolved and queued for review "
                 f"(active=0, tagged {tags.PENDING})" if commit
                 else "would be re-resolved (preview: nothing written)")
              + (f", {len(dups)} already tracked under another name" if dups else "")
              + f", {len(still)} still missing, of {len(rows)} tried.")
        if written and commit:
            print("  confirm or reject them in the roster review queue.")
        return written


# --------------------------------------------------------------------------- #
#  Employer-name repair: a board still named after its own slug/tenant        #
# --------------------------------------------------------------------------- #
#
# 51 boards read the digest and the logs under their bare slug/tenant
# ("Lifestance", "Centriaautism", "Abbvie", "Akumincorp", ...) rather than
# the employer's real name (2026-09-18 audit). Two ATSes carry that real
# name in the SAME listing call every ordinary board pull already makes,
# with no per-posting detail fetch and no per-row drift:
#
#   * Greenhouse -- every job object in the boards-api listing carries
#     `company_name` (confirmed live, 2026-09-18: tenant "centriaautism"
#     answers "Centria Autism", "medelitellc" answers "MedElite Group,
#     LLC.").
#   * SmartRecruiters -- every posting carries `company.name` (confirmed
#     live: "AbbVie", "Eurofins").
#
# config.BOARDS names that field as each spec's `employer`; the engine
# reads it off one listing request (`Board.employer_name`).
#
# Workday, Lever and Ashby were checked the same way and do NOT qualify:
#   * Workday's CXS job-DETAIL JSON (not the listing) carries a top-level
#     `hiringOrganization.name` -- but live-checked against the "aah"
#     tenant (Advocate Aurora Health) it answered "136 Aurora Medical
#     Center Grafton LLC" for one req: the POSTING's legal entity, not the
#     board's brand, and it varies row to row. Using it would rename the
#     board WRONG, not just fail to rename it -- and reading it needs a
#     per-posting detail GET the other two do not, since a board-native
#     name has to be the same on every row to be worth writing once.
#   * Lever's and Ashby's public postings APIs carry no employer field at
#     all, structured or otherwise (confirmed live against "kitware" and
#     "brainco"/"alpacahealth" -- the name appears only inside description
#     HTML). There is no existing parsing of it to reuse, so it is not
#     read at all rather than screen-scraped freshly for this one op.


def _employer_atses():
    return sorted(b.name for b in BOARDS.values() if b.spec.employer)


def _employer_name(ats, slug):
    return board_for(ats).employer_name(slug)


def _slug_named_boards(conn):
    """The active companies on a supported ATS (_employer_atses)
    that src.ats.coords.slug_named calls slug-named -- the same rule, and
    the same one definition of it, the HARVEST SUMMARY's own tally
    applies. Biggest board first -- the boards a wrong name embarrasses
    most in the digest and the logs.

    Notes:
        slug_named's SLUG_NAME_SOURCE half is what keeps this op off rows
        a human (or local_sourcing) named for real, and it live-caught a
        THIRD failure mode past both of its screens: company id 70, stored
        as "NeU" (source "discovery:bciwiki:companies", a real neurotech
        employer, not slug-derived) with slug "neu" -- but the "neu"
        Greenhouse tenant today answers a totally unrelated company's
        postings ("Fora"), i.e. the stored slug no longer names NeU's own
        board at all. A source-restricted candidate list never reaches
        that row; that stale coordinate is a separate, pre-existing roster
        problem (a dead/reassigned Greenhouse tenant) for reresolve_misses
        or a human to catch, not this op to paper over by renaming NeU to
        Fora.
    """
    atses = _employer_atses()
    ph = ",".join("?" for _ in atses)
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, name, ats, slug, source, total_job_count FROM companies "
        f"WHERE COALESCE(active,0)=1 AND ats IN ({ph})",
        tuple(atses)).fetchall()]
    rows = [r for r in rows if coords.slug_named(r)]
    rows.sort(key=lambda r: -(r.get("total_job_count") or 0))
    return rows


def rename_slug_boards(conn=None, t=None, commit=False, limit=None):
    """PREVIEW (default) or APPLY a rename of every active, dork-sourced
    Greenhouse/SmartRecruiters board whose stored name is nothing but its
    own board slug (_slug_named_boards) to the employer name the board's
    OWN listing payload carries (`Board.employer_name`). One GET per
    candidate board, no detail fetch, no whole-board pull.

    Same preview/apply shape as reresolve_misses: `commit=False` (the
    default -- this NEVER renames silently) fetches every candidate's real
    name and prints one line each; nothing is written. `commit=True` is
    the only thing that writes, one line per rename actually made.

    A fetched name is rejected -- reported, never written, in EITHER mode
    -- when:
      * the board answered empty or errored (`Board.employer_name` -> "");
      * src.match.names.junk_name_reason flags it -- the SAME screen a
        pasted or re-resolved name is run through, so a malformed payload
        naming a section heading rather than an employer can never
        overwrite a roster row;
      * it is BYTE-IDENTICAL to what is already stored -- nothing to fix.
        This is deliberately NOT a name_key comparison: name_key strips
        spaces, so it cannot tell "Centria Autism" (what the payload
        carries) from "Centriaautism" (the slug-derived name stored) --
        the exact improvement this op exists to make;
      * it collides (src.match.names.name_key) with a DIFFERENT company
        already on the roster -- `companies.name` is UNIQUE, and this op
        renames one row, it does not merge two.

    `limit` caps how many candidates this ONE pass checks (biggest board
    first -- see _slug_named_boards).

    Returns [(company_id, old_name, new_name)]: renamed rows (commit=True)
    or the rows that WOULD be renamed (commit=False).

    Notes:
        Workday, Lever and Ashby boards named after their own slug are not
        covered and still need a human rename -- see the module comment
        above (Workday's own per-posting hiringOrganization field is the
        wrong grain and was confirmed live to produce a WRONG name, not
        merely a missing one; Lever and Ashby carry no employer field in
        their public postings API at all).

        An earlier version compared with name_is_own_slug(new_name, slug)
        and, live-checked against the 22 real candidates on 2026-09-18,
        wrongly skipped 8 of 14 genuine renames (Axsome Therapeutics,
        Garner Health, Beam Therapeutics, ...) because each one's name_key
        equals its own slug's.
    """
    from src.match.names import junk_name_reason, name_key

    t = _t(t)
    with track_store(t, conn) as conn:
        rows = _slug_named_boards(conn)
        if limit:
            rows = rows[:int(limit)]
        if not rows:
            print("  no active Greenhouse/SmartRecruiters board is still "
                  "named after its own slug.")
            return []
        print(f"  checking {len(rows)} slug-named board's own payload for "
              f"its employer name...")
        existing = {name_key(r["name"]): r["name"]
                   for r in conn.execute("SELECT name FROM companies")}
        out = []
        for c in rows:
            new_name = _employer_name(c["ats"], c["slug"])
            label = f"{c['name'][:30]:30} {c['ats']:15}"
            if not new_name:
                print(f"    [skip]      {label} board answered no employer name")
                continue
            why = junk_name_reason(new_name)
            if why:
                print(f"    [skip]      {label} payload name {new_name!r} "
                      f"rejected ({why})")
                continue
            if new_name == c["name"]:
                print(f"    [skip]      {label} payload's name matches what "
                      f"is already stored -- nothing to fix")
                continue
            key = name_key(new_name)
            if key in existing and existing[key] != c["name"]:
                print(f"    [skip]      {label} {new_name!r} collides with "
                      f"existing company {existing[key]!r}")
                continue
            print(f"    [{'renamed' if commit else 'preview'}]    {label} "
                  f"-> {new_name!r}")
            out.append((c["id"], c["name"], new_name))
            if commit:
                conn.execute("UPDATE companies SET name=? WHERE id=?",
                            (new_name, c["id"]))
                existing[key] = new_name
        if commit and out:
            conn.commit()
        print(f"\n  {len(out)} board(s) "
              + ("renamed" if commit
                 else "would be renamed (preview: nothing written)")
              + f" of {len(rows)} checked.")
        return out
