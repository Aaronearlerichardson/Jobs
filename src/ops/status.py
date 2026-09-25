"""Open/closed reconciliation of stored job rows: the board-snapshot status
sync and the closed-URL probe."""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from src import config
from src import store
from src.ats.board import company as company_fetch
from src.ats.board import closure
from src.match.locality import NC_RE
from src.net.parallel import fan_out, fetch_all
from src.ops.maintenance import (_DEAD_BOARD_FAMILY, _ranked, _t, _whole_board,
                                 group_by_company, rewrite_digest, track_store)


# Why a fetched board can't be reconciled, in the order the footer reports
# them. Distinct on purpose: "the fetch raised", "the fetch came back with
# nothing", and "the roster row has no primary key" are three different
# faults with three different fixes, and the sync used to treat all three as
# the same silent `continue`.
_SYNC_SKIP_REASONS = ("fetch error", "empty board", "no roster id")


def _sync_skip_reason(company, jobs, err, snapshot=None):
    """Why this company's board cannot be reconciled, or None when it can.
    `snapshot` is fetch_all's net.http.snapshot_info() for the board: an
    INCOMPLETE one (a page failed partway) is a fetch error too.

    >>> _sync_skip_reason({"id": 1}, [{"id": "j1"}], None) is None
    True
    >>> _sync_skip_reason({"id": 1}, [], RuntimeError("HTTP 404"))
    'fetch error'
    >>> _sync_skip_reason({"id": 1}, [{"id": "j1"}], None, {"incomplete": True})
    'fetch error'
    >>> _sync_skip_reason({"id": 1}, [], None)
    'empty board'
    >>> _sync_skip_reason({"name": "Acme"}, [{"id": "j1"}], None)
    'no roster id'
    """
    if err is not None or (snapshot or {}).get("incomplete"):
        return "fetch error"
    if not jobs:
        return "empty board"
    if not company.get("id"):
        return "no roster id"
    return None


def _sync_skip_note(skipped):
    """The footer's skipped-board clause, or "" when nothing was skipped.

    >>> _sync_skip_note({"fetch error": 6, "empty board": 1, "no roster id": 1})
    ', 8 skipped (6 fetch error, 1 empty board, 1 no roster id)'
    >>> _sync_skip_note({"fetch error": 0, "empty board": 0})
    ''

    It sits beside the "N board(s) reconciled" count because that count on
    its own is what hid the gap: 216 attempted, 208 reconciled, and no
    arithmetic in the log to say the other eight existed.
    """
    parts = [f"{skipped[why]} {why}" for why in _SYNC_SKIP_REASONS
             if skipped.get(why)]
    if not parts:
        return ""
    return f", {sum(skipped.values())} skipped (" + ", ".join(parts) + ")"


def sync_status_all(top_n=15, t=None):
    """Status-only reconciliation: re-fetch every active company's board
    (same scoping as the crawl — locality unless whole-board), reconcile
    open/closed via sync_job_statuses, and rewrite today's digest from the
    corrected ranking. NO scoring, no Claude API — the cheap recovery pass
    for when statuses have drifted without paying for a full crawl.

    A board that cannot be reconciled is skipped (never closed on a failed
    or partial fetch) and SAID SO: one "[!]" line per board with the
    reason, and the reason counts beside the footer's reconciled count. A
    capped board IS reconciled (reopening still happens for whatever
    matches), but a board-native row missing from a capped snapshot is
    NEVER closed here, on any miss (store.sync_job_statuses's `capped`) --
    that snapshot is an unstable window, not the board, and closing what
    it drops is check_closed_jobs's job, which probes the row's own URL
    instead of trusting one page-capped pull."""
    t = _t(t)
    with track_store(t) as conn:
        companies = store.crawlable_companies(conn, tag=t["store_tag"])
        print(f"  reconciling statuses across {len(companies)} active compan(ies)...")
        loc = NC_RE if t["sources"]["location_scoped"] else None
        sources = [(c["name"], c["ats"] or "?",
                    (lambda cc=c: company_fetch.fetch_company(
                        cc, None if (_whole_board(cc, t.get("remote_mission_floor"))
                                     or loc is None) else loc)))
                   for c in companies]
        fetched = fetch_all(sources)
        n_closed = n_reopened = n_boards = 0
        skipped = {why: 0 for why in _SYNC_SKIP_REASONS}
        for c, (jobs, err, snapshot) in zip(companies, fetched):
            why = _sync_skip_reason(c, jobs, err, snapshot)
            if why:
                # The skip itself is right and stays: fetchers soft-fail to
                # [], so an error or an empty snapshot is indistinguishable
                # from a board that emptied for real, and reconciling one
                # would close every job of a company whose ATS merely
                # hiccuped. What was wrong is that it happened in silence —
                # 2026-09-11 (data/logs/session-20260911-161836-webui-sync
                # .log): 16:18:37 "reconciling statuses across 216 active
                # compan(ies)...", 16:20:51 "208 board(s) reconciled", and
                # not one line in between about the other eight. A skipped
                # board is a company whose statuses are now stale, so it is
                # a WARNING ("  [!]" lines are logged at WARNING —
                # src/session_log.py::_level_for), named and counted.
                skipped[why] += 1
                detail = f": {err}" if err is not None else ""
                print(f"    [!] {(c.get('name') or '?')[:34]:34} "
                      f"not reconciled ({why}){detail}")
                continue
            n_re, n_cl = store.sync_job_statuses(
                conn, c["id"], jobs, track=t["track"],
                capped=(snapshot or {}).get("capped", False))
            n_boards += 1
            n_closed += n_cl
            n_reopened += n_re
            if n_cl or n_re:
                print(f"  {c['name'][:34]:34} {len(jobs):3} listed -> "
                      f"{n_cl:2} closed, {n_re:2} reopened")
        n_open = len(_ranked(conn, t))
        rewrite_digest(conn, t, top_n,
                       f"\n  {n_boards} board(s) reconciled: {n_closed} closed, "
                       f"{n_reopened} reopened{_sync_skip_note(skipped)}; "
                       f"{n_open} open job(s) in ranking.")
        return (n_closed, n_reopened)


# How stale an OPEN row at a board-dead company must be before this closes
# it outright, with no URL probe at all: the company's OWN board fetch has
# already failed since (store.miss_family(miss_reason) == this family), a
# stronger signal than any one dead URL. A default kept as a module constant
# beside the query that reads it -- the precedent is harvest.py's own
# CLOSED_PROBE_STALE_DAYS/CLOSED_PROBE_LIMIT beside its one call site.
DEAD_BOARD_CLOSE_DAYS = 14

# Consecutive UNVERIFIABLE closure probes (jobs.probe_streak) after which
# check_closed_jobs stops selecting a row at all. Some rows can never be
# answered by a URL probe -- a bot-gated host, a JS-only detail page, an ATS
# with no closure signal, a row whose company has no resolved board -- and
# every pass spent re-probing one is a pass not spent on the rows a probe
# CAN answer. Ten is deliberately generous: a probe failing ten times
# running is a property of the endpoint, not a bad afternoon, and the row is
# only parked from PROBING -- it stays open, keeps its rank, and still
# closes the moment its board stops listing it (store.sync_job_statuses) or
# its board dies (_dead_board_open_rows below). Any live sighting clears the
# streak (store.record_probe_outcome, touch_job, sync_job_statuses).
CLOSED_PROBE_GIVE_UP = 10

#: A quoted page phrase is per-row detail, not a reason of its own -- one
#: tally bucket per KIND of answer.
_PROBE_DETAIL_RE = re.compile(r"'[^']*'")


def _probe_label(url):
    """The bucket one probe outcome is reported under: the ATS family the
    probe recognized, else the URL's own host (which is what distinguishes
    the bot-gated aggregators and the self-hosted boards from each other).

    >>> _probe_label("https://jobs.lever.co/acme/2e1a8d40-0f2b-4c7e-9a11-5b6c7d8e9f01")
    'lever'
    >>> _probe_label("https://www.linkedin.com/jobs/view/4435444961/")
    'www.linkedin.com'
    >>> _probe_label("")
    '?'
    """
    fam = closure.probe_family(url)
    if fam and fam != "gated":
        return fam
    return re.sub(r"^https?://", "", url or "").split("/")[0].lower() or "?"


def _probe_tally_lines(counts, reasons):
    """The per-family outcome lines for a probe pass's summary, so the next
    audit reads "icims: 17 closed [icims api HTTP 410 x17]" instead of one
    undifferentiated "36 unverifiable". Sorted by how much of the pass each
    family accounted for.

    >>> t = {"lever": Counter(live=12, closed=3),
    ...      "www.linkedin.com": Counter(unverifiable=2)}
    >>> r = {"lever": Counter({"lever api: posting live": 12}),
    ...      "www.linkedin.com": Counter({"bot-gated aggregator host": 2})}
    >>> for ln in _probe_tally_lines(t, r): print(ln)
        lever            12 live, 3 closed [lever api: posting live x12]
        www.linkedin.com 2 unverifiable [bot-gated aggregator host x2]
    """
    out = []
    for label in sorted(counts, key=lambda k: (-sum(counts[k].values()), k)):
        got = ", ".join(f"{counts[label][k]} {k}" for k in
                        ("live", "closed", "unverifiable") if counts[label][k])
        why = ", ".join(f"{r} x{n}" if n > 1 else r
                        for r, n in reasons[label].most_common(3))
        out.append(f"    {label:<16} {got}" + (f" [{why}]" if why else ""))
    return out


def _dead_board_open_rows(conn, days):
    """OPEN rows at a company whose CURRENT miss_reason is in the
    'board-dead' family (store.miss_family) and whose last board-verified
    sighting (last_seen, or first_seen for a row a board never re-confirmed)
    is older than `days`: nobody has vouched for it since the board itself
    started returning nothing, so these are dead by inference rather than
    by a URL probe. Ordered by company name, which is how its one caller
    reports them (group_by_company); nothing bounds this query, so the
    order is a reporting choice rather than a rotation one.

    A quiet-but-not-missing board (stale rows, but no miss_reason at all,
    or a miss in some OTHER family such as 'no-local-jobs') is excluded:
    only a row whose OWN company carries this exact family qualifies.

    A promoted board-dead company (store.companies.mark_harvested's
    HARVEST_DEAD_AFTER_DAYS cycle, or a manual src.ops.roster.prune) writes
    miss_reason with a raw UPDATE rather than through record_miss -- the
    whole point of the promotion is demoting a company record_miss's own
    "never demote an ACTIVE company" guard would otherwise protect -- so
    that is what these fixtures do too:

    >>> from src.store import connect, upsert_company, upsert_job
    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Judi Health", "ats": "greenhouse"})
    >>> _ = upsert_job(conn, {"job_id": "j1", "title": "T", "company_id": cid,
    ...                       "company_name": "Judi Health"})
    >>> _ = conn.execute("UPDATE jobs SET last_seen='2020-01-01' "
    ...                  "WHERE job_id='j1'")
    >>> _dead_board_open_rows(conn, 14)
    []
    >>> _ = conn.execute("UPDATE companies SET miss_reason='board-dead:greenhouse' "
    ...                  "WHERE name='Judi Health'")
    >>> [r['job_id'] for r in _dead_board_open_rows(conn, 14)]
    ['j1']

    A quiet board that never missed is never touched, however stale:

    >>> cid2 = upsert_company(conn, {"name": "Quiet Co", "ats": "lever"})
    >>> _ = upsert_job(conn, {"job_id": "j2", "title": "T", "company_id": cid2,
    ...                       "company_name": "Quiet Co"})
    >>> _ = conn.execute("UPDATE jobs SET last_seen='2020-01-01' "
    ...                  "WHERE job_id='j2'")
    >>> [r['job_id'] for r in _dead_board_open_rows(conn, 14)]
    ['j1']

    Nor is a miss in a DIFFERENT family, however dead-sounding the row's own
    situation looks otherwise:

    >>> cid3 = upsert_company(conn, {"name": "Ats Gap"})
    >>> _ = upsert_job(conn, {"job_id": "j3", "title": "T", "company_id": cid3,
    ...                       "company_name": "Ats Gap"})
    >>> _ = conn.execute("UPDATE jobs SET last_seen='2020-01-01' "
    ...                  "WHERE job_id='j3'")
    >>> _ = conn.execute("UPDATE companies SET miss_reason='ats-unsupported:ukg' "
    ...                  "WHERE name='Ats Gap'")
    >>> [r['job_id'] for r in _dead_board_open_rows(conn, 14)]
    ['j1']
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT j.job_id, j.title, j.company_id, j.company_name, "
        "c.miss_reason FROM jobs j JOIN companies c ON c.id = j.company_id "
        "WHERE COALESCE(j.status,'open') != 'closed' "
        "AND c.miss_reason IS NOT NULL "
        "AND COALESCE(j.last_seen, j.first_seen, '') < ? "
        "ORDER BY j.company_name",
        (cutoff,)).fetchall()]
    return [r for r in rows
            if store.miss_family(r["miss_reason"]) == _DEAD_BOARD_FAMILY]


def check_closed_jobs(max_workers=8, limit=None, stale_days=2, t=None,
                      conn=None):
    """Probe the detail URLs of OPEN rows that no successful board fetch has
    vouched for in `stale_days` and close the ones that are positively dead
    (HTTP 404/410 from the ATS's own endpoint or the page, an ATS "no longer
    accepting" notice, a past JSON-LD validThrough, a spec's closure
    rule, an id absent from a non-empty board listing -- see
    board.closure.probe_job_open). Indeterminate probes (bot-gated
    hosts, JS-only pages) leave the row untouched. THEN, separately, close
    every OPEN row at a DEAD_BOARD_CLOSE_DAYS+-stale company whose own board
    fetch has already failed (store.miss_family == "board-dead") -- no URL
    probe needed there, the board itself is the witness. Returns how many
    rows this call closed IN TOTAL, probed and inferred together.

    `conn=None` opens the track's own store (`t`, default track when `t` is
    also None); a caller's own `conn` is used as is (track_store). Neither
    query is scoped by track: a stale OPEN row is stale whichever track
    ranks it.

    Selection: a stale row is only worth a GET when its own board would
    have vouched for it and did not -- i.e. when the harvester walks that
    board (store.harvestable_companies) and last walked it INSIDE the same
    `stale_days` window, so the walk happened after the row's last
    sighting and no longer listed it -- or when no board snapshot has ever
    ruled on the row at all (no company, no ATS, a no-board/dead-board
    miss, or a board the harvester has yet to walk once). What that leaves
    out is the board whose next walk is simply not due: on the longer
    config.HARVEST_OFFMISSION_HOURS cadence (168h, against a 7-day
    CLOSED_PROBE_STALE_DAYS) every one of its rows goes "not
    board-verified in 7+ days" by arithmetic just before each walk, and
    probing them says nothing the imminent walk will not say better.

    Reporting: outcomes are tallied per ATS family (per host for what no
    family claims) and printed under the summary line, because "36
    unverifiable" named nothing an audit could act on and "icims: 17
    closed [icims api HTTP 410 x17]" names all of it.

    Probe rotation: the `limit` rows are the ones this op has gone longest
    without probing -- ordered by desc_checked_at, never-probed-by-it
    first -- and every live/unverifiable verdict stamps that column, so
    successive bounded passes cover the backlog instead of re-probing one
    head of the queue (tests/test_probes.py's TestClosedProbeRotation). A
    CLOSED row needs no stamp; leaving the WHERE clause is a stronger exit
    than any timestamp.

    Probe give-up: a row whose last CLOSED_PROBE_GIVE_UP probes were all
    unverifiable leaves the selection for good (jobs.probe_streak, written
    by store.record_probe_outcome) -- some URLs can never answer this
    question, and re-asking them forever is what crowded out the rows that
    can. Such a row is NOT closed and NOT deleted: it stays open, and a
    live sighting (a probe that confirms it, a board that lists it again)
    resets the streak and puts it back in the queue.
    tests/test_probes.py's TestClosedProbeGiveUp pins both halves.

    Notes:
        `ORDER BY company_name` alone never moved a row: a CONFIRMED-live
        or UNVERIFIABLE verdict, unlike a closed one, leaves the row
        exactly where the next bounded pass picks it up again.

        desc_checked_at already means "last non-productive attempt" for
        the description backfills; reusing it here shares that clock
        rather than colliding with it -- the probe's own outcome is not a
        description fetch, it just also means "don't hammer this one again
        immediately". The price is that a probed row's backfill retry
        timer restarts too. Measured on the 2026-09-17 store: of the 84
        rows this op would select, 7 carry a body under fit.MIN_DESC_CHARS
        (so only those 7 are backfill candidates at all) and NONE are in
        triage's own pending-hydration set (every one is already tracked
        and judged) -- and a row the probe just found gated or slow is
        exactly the one worth leaving alone for the backfill's grace
        period anyway.

        last_seen is NEVER written here, on any of the three verdicts: it
        means "a board vouched for this", a direct URL probe is not a
        board, and this op's own selection ("no board has vouched for it
        in stale_days") would misfire the moment a probe outcome could
        satisfy it instead.

        The dead-board half closed 48 rows on 2026-09-17: 47 at one
        greenhouse board dead since 09-11 (Judi Health), 1 at Hippocratic
        AI.
    """
    with track_store(t, conn) as conn:
        cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat()
        rows = [dict(r) for r in conn.execute(
            "SELECT job_id, title, company_name, company_id, url FROM jobs "
            "WHERE COALESCE(status,'open') != 'closed' "
            "AND COALESCE(last_seen, first_seen, '') < ? "
            "AND COALESCE(probe_streak, 0) < ? "
            "ORDER BY COALESCE(desc_checked_at, ''), company_name",
            (cutoff, CLOSED_PROBE_GIVE_UP)).fetchall()]
        # The harvester's OWN view of which boards it walks and when
        # (store.harvestable_companies, companies.last_harvested_at), not a
        # second copy of that rule.
        walked = {c["id"]: (c.get("last_harvested_at") or "")
                  for c in store.harvestable_companies(conn)}
        n_rows = len(rows)
        # "" covers both "no board the harvester walks" and "walked none
        # yet": either way no board snapshot has ever ruled on the row.
        rows = [r for r in rows if not walked.get(r["company_id"])
                or walked[r["company_id"]] > cutoff]
        n_deferred = n_rows - len(rows)
        if limit:
            rows = rows[:int(limit)]
        print(f"  probing {len(rows)} open job(s) not board-verified in "
              f"{stale_days}+ day(s)"
              + (f" ({n_deferred} skipped: board not walked since)"
                 if n_deferred else "") + "...")

        def _probe(r):
            # A probe that RAISES is not a failure to report and skip, it
            # is an unverifiable row -- the third outcome this op counts.
            # So it is caught here rather than left to fan_out, which would
            # drop the row and quietly shrink the denominator.
            try:
                return closure.probe_job_open(r["url"], r["job_id"])
            except Exception as e:          # noqa: BLE001 - an outcome
                return None, f"probe error: {type(e).__name__}"

        now = datetime.now()
        n_closed = n_live = n_unknown = n_parked = 0
        counts, reasons = defaultdict(Counter), defaultdict(Counter)
        # An abandoned probe is never yielded, so it closes nothing and
        # records no outcome: the row waits for the next pass as it was.
        abandoned = []
        for r, (is_open, reason) in fan_out(
                rows, _probe,
                lambda r: f"probe {r['company_name']}: {(r['title'] or '')[:40]}",
                max_workers, with_item=True, budget_s=config.PASS_BUDGET_S,
                on_abandon=abandoned.append):
            label = f"{(r['company_name'] or '?')[:24]:24} {(r['title'] or '')[:38]:38}"
            bucket = _probe_label(r["url"])
            reasons[bucket][_PROBE_DETAIL_RE.sub("...", reason or "?")] += 1
            if is_open is False:
                store.set_job_status(conn, r["job_id"], "closed")
                n_closed += 1
                counts[bucket]["closed"] += 1
                print(f"    [closed] {label} {reason}")
            elif is_open:
                store.record_probe_outcome(conn, r["job_id"], True, now)
                n_live += 1
                counts[bucket]["live"] += 1
            else:
                streak = store.record_probe_outcome(conn, r["job_id"], False,
                                                    now)
                n_unknown += 1
                counts[bucket]["unverifiable"] += 1
                if streak >= CLOSED_PROBE_GIVE_UP:
                    n_parked += 1
                    print(f"    [give-up] {label} {streak} unverifiable "
                          f"probes; not probing again ({reason})")
        print(f"  {n_closed} closed, {n_live} confirmed live, "
              f"{n_unknown} unverifiable (left open)"
              + (f", {len(abandoned)} abandoned (left open)" if abandoned else "")
              + f" of {len(rows)} probed.")
        for line in _probe_tally_lines(counts, reasons):
            print(line)
        if n_parked:
            print(f"  {n_parked} row(s) hit {CLOSED_PROBE_GIVE_UP} "
                  f"unverifiable probes and left the probe queue.")

        dead = group_by_company(_dead_board_open_rows(
            conn, DEAD_BOARD_CLOSE_DAYS))
        n_dead = 0
        for rs in dead.values():
            for r in rs:
                store.set_job_status(conn, r["job_id"], "closed")
            n_dead += len(rs)
            print(f"    [dead-board] {(rs[0]['company_name'] or '?')[:34]:34} "
                  f"{len(rs):3} row(s) closed ({rs[0]['miss_reason']})")
        if dead:
            print(f"  {n_dead} row(s) closed at {len(dead)} dead-board "
                  f"compan(ies) not board-verified in "
                  f"{DEAD_BOARD_CLOSE_DAYS}+ day(s).")
    return n_closed + n_dead
