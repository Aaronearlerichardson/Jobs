"""Track-agnostic store-maintenance operations.

These grew up inside the local track's module but were never local-specific
— they operate on whichever track's DB/config they're given: status
reconciliation, deep-verify, closed-URL probing, rescoring, description
backfills, external-job ingest, manual adds, and the crawl's per-company
gate/score helpers. Every function takes the track config `t` (a
config.UI_TRACKS entry; None = the default local-engine track) and derives
db path, jobs.track value, gates, and ranking knobs from it — so the web
UI and run_scraper.py can run any op against any configured track.
"""

import re
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta

from src import config
from src import digest
from src import store
from src import tags
from src.ats import coords
from src.ats.fetchers import company as company_fetch
from src.ats.fetchers import probe
from src.claude.fit import UNSCORED_CAUSES, score_resume_fit
from src.claude.resume import resume_text
from src.match import gates
from src.match.filters import is_relevant
from src.match.locality import NC_RE, geo_mode
from src.net.http import get_json
from src.net.parallel import drain, fan_out, fetch_all


def _default_track():
    return config.track_for_engine("local")


def _t(t):
    return t if t is not None else _default_track()


@contextmanager
def track_store(t=None, conn=None):
    """The track's store, closed on the way out however the block ends --
    or `conn` itself, left open, when the caller already holds one (the
    crawl and the harvest pass hand an op the store they have open, which
    need not be `t`'s configured file).

    Every op here opens the same connection the same way, and each one
    used to spell out its own `conn = store.connect(...)` / `conn.close()`
    pair -- ten of them, none inside a `try`. A raised exception therefore
    leaked the connection, and a leaked connection holds its read snapshot
    open, which is what stops SQLite folding the WAL back into the store.

    `t=None` means the default track (`_t`), not the default DB FILE. The
    two are the same until a profile gives a track its own `db`, and the
    roster ops resolved None the other way, so the same op run from the
    web UI and from discover.py could reach different stores.
    """
    if conn is not None:
        yield conn
        return
    conn = store.connect(_t(t)["db_path"])
    try:
        yield conn
    finally:
        conn.close()


def stale_body_rows(conn, where, columns="job_id, title, url", min_len=200,
                    retry_days=3, limit=None, label="description(s)"):
    """Stored rows with no usable body yet, minus the ones a recent attempt
    already failed on, and the header line saying so.

    `where` is the extra predicate that picks one backfill's population
    (company-linked rows, Workday URLs); the rest -- too short to score,
    not closed, not attempted inside `retry_days` -- is the same question
    every backfill asks. Both of them had written it out, and they had
    already diverged: one read `desc_checked_at` off a sqlite3.Row and the
    other off a dict, which is the sort of difference that survives
    because neither copy is ever read beside the other.

    `retry_days=0` retries everything. The skip count is reported, not
    hidden: "0 of 4 backfilled" with no explanation was undiagnosable from
    the session log.
    """
    cutoff = ((datetime.now() - timedelta(days=retry_days)).isoformat()
              if retry_days else "9999")
    rows = [dict(r) for r in conn.execute(
        f"SELECT {columns}, desc_checked_at FROM jobs "
        f"WHERE {where} "
        "AND COALESCE(status,'open') != 'closed' "
        "AND length(COALESCE(description,'')) < ?", (min_len,)).fetchall()]
    recent = [r for r in rows if (r.get("desc_checked_at") or "") >= cutoff]
    rows = [r for r in rows if (r.get("desc_checked_at") or "") < cutoff]
    if limit:
        rows = rows[:int(limit)]
    print(f"  backfilling {len(rows)} {label}..."
          + (f" ({len(recent)} skipped: failed in the last {retry_days}d)"
             if recent else ""))
    return rows


def save_body(conn, job_id, text):
    """Keep a fetched body, or stamp the failure. True when a body landed.

    The stamp is not optional bookkeeping: an unstamped failure is
    re-selected (and silently re-counted) by every later run, so a board
    that has stopped answering costs a fetch per row forever. Three of the
    four places that wrote this pair spelled the UPDATE out inline, beside
    a store that already exported both halves.
    """
    if not text:
        store.mark_desc_checked(conn, job_id)
        return False
    store.store_body(conn, job_id, text)
    return True


def group_by_company(rows, key="company_id"):
    """`rows` bucketed by their company id, in first-seen order.

    >>> group_by_company([{"company_id": 1, "t": "a"}, {"company_id": 2},
    ...                   {"company_id": 1, "t": "b"}])
    {1: [{'company_id': 1, 't': 'a'}, {'company_id': 1, 't': 'b'}], 2: [{'company_id': 2}]}

    Both backfill paths need this: a board with several stale rows must be
    fetched once, not once per row.
    """
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def board_index(company):
    """One company's whole board, indexed by normalised title.

    Empty when the board cannot be pulled -- which is the same outcome as a
    board that lists nothing matching, so callers fall through to their
    per-URL path either way.
    """
    try:
        board = company_fetch.fetch_company(company, loc_re=None)
    except Exception as e:                      # noqa: BLE001 - reported
        print(f"    [!] {company['name']}: board fetch failed: {e}")
        return {}
    return {(b.get("title") or "").strip().lower(): b for b in board}


def board_match(index, title):
    """The board row for `title`, hydrated, or None when the board does not
    cover it (or covers it with no body).

    The pair above plus this is the whole of "get a stored row's text back
    from its company's own board", which the description backfill and the
    external-ingest hydrator each used to spell out in full -- with
    different error text and, until this, no shared notion of what counts
    as a title match.
    """
    match = index.get((title or "").strip().lower())
    if match is None:
        return None
    company_fetch.hydrate_description(match)
    return match if match.get("description") else None


def _ranked(conn, t, limit=None):
    """The track's ranked view — same knobs the crawl digest uses."""
    return store.ranked_jobs(
        conn, track=t["track"],
        location_re=(NC_RE if t["geo_gate"] else None),
        rank_by=t["rank_by"], allow_geo_modes={"remote"},
        min_mission=t["min_mission"],
        remote_mission_floor=t.get("remote_mission_floor"), limit=limit)


def _write_digest(conn, t, watch_hits=None):
    """Rank the track's open jobs and rewrite its digest file, harvest
    triage funnel included. Returns (ranked, pipeline, followups,
    digest_path).

    The one digest writer behind rewrite_digest and the crawl's
    runner._report_ranked, so their rankings and sections cannot drift.
    """
    ranked = _ranked(conn, t)
    pipeline = store.get_pipeline(conn)
    followups = store.followups_due(conn)
    path = digest.write_ranked_digest(
        ranked, t, watch_hits=watch_hits, pipeline=pipeline,
        followups=followups, triage=store.triage_counts(conn, days=7))
    return ranked, pipeline, followups, path


def rewrite_digest(conn, t, top_n=15, heading=""):
    """Rewrite the track's ranked digest from the store as it stands now,
    and print the top `top_n` of it. Returns the ranked list.

    The tail of every op that changes what the ranking contains -- the
    status sync and the standalone deep verify both ended with their own
    copy, and the copies had already drifted apart in what they printed.
    """
    ranked = _write_digest(conn, t)[0]
    if heading:
        print(heading)
    for j in ranked[:top_n]:
        fit = j["resume_fit_score"]
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        print(f"  fit={fs} [{j.get('geo_mode','?')}] {(j['title'] or '')[:52]}"
              f"  -  {j['company_name']}")
    return ranked


# --------------------------------------------------------------------------- #
#  Company-tag helpers (store roster semantics, shared by crawl + ops).        #
# --------------------------------------------------------------------------- #

def _is_sweep_tagged(company):
    """True if a company store row carries the 'sweep' scope tag (src/tags.py
    — 'neural' in older stores). Its board is cheap to pull whole, so it is
    fetched unfiltered; the geo gate is then applied per posting below."""
    if not company:
        return False
    return tags.has(company.get("tags"), tags.SWEEP)


def _is_watched(company):
    """True if the company carries the 'watch' tag (run_scraper.py --watch
    NAME): the user wants to see EVERY new technical posting there. Watched
    companies get their whole board fetched and their new technical postings
    surface in a dedicated digest section regardless of rank or geography."""
    if not company:
        return False
    return tags.has(company.get("tags"), tags.WATCH)


def _mission_trusted(company, floor):
    """True if a store company row earns watch-grade remote treatment on its
    mission score alone: `floor` (the track's `remote_mission_floor`,
    None = off) or better.

    >>> _mission_trusted({"name": "Acme", "mission_score": 0.9}, 0.85)
    True
    >>> _mission_trusted({"name": "Acme", "mission_score": 0.5}, 0.85)
    False
    >>> _mission_trusted({"name": "Acme", "mission_score": 0.9}, None)
    False
    >>> _mission_trusted(None, 0.85)
    False

    Notes:
        Defers to store.remote_admitted, the rule the ranking applies, so
        the fetch side and the ranking side cannot drift into disagreeing
        about which remote rows should exist. The company row is passed as
        the job-shaped fields that rule reads; its tags are withheld
        because callers test the watch half themselves.
    """
    if not company:
        return False
    return store.remote_admitted(
        {"company_name": company.get("name"),
         "mission_score": company.get("mission_score")}, floor)


def _whole_board(company, mission_floor=None):
    """Whether a company's ENTIRE board is fetched, with no location filter.

    Either scope tag qualifies on its own — a sweep board is cheap to pull
    whole, a watched one must never miss a posting:

    >>> _whole_board({"name": "Acme", "tags": "watch"})
    True
    >>> _whole_board({"name": "Acme", "tags": "local"})
    False

    Given a `mission_floor` (the track's `remote_mission_floor`), a
    core-mission company qualifies on its score too, so its remote postings
    — which a locality-scoped fetch would never return — can reach the
    ranking that now admits them:

    >>> core = {"name": "Acme", "tags": "local", "mission_score": 0.9}
    >>> _whole_board(core), _whole_board(core, 0.85)
    (False, True)

    Everyone else gets the locality-scoped pull.

    Notes:
        Whole-board fetches are the expensive kind: at the shipped floor of
        0.85 about 112 further boards per run stop being location-scoped,
        and the count climbs fast as the floor drops. It is a knob to move
        deliberately.
    """
    return (_is_sweep_tagged(company) or _is_watched(company)
            or _mission_trusted(company, mission_floor))


# --------------------------------------------------------------------------- #
#  Crawl helpers (per-company gate + score), used by runner + single adds.     #
# --------------------------------------------------------------------------- #

def _keep_job(company, job, t):
    """Company-linked posting filter: technical-title gate, multi-division
    keyword gate, per-track excludes, and (when the track's geo_gate is on)
    the whole-board geography check."""
    title = job.get("title", "")
    if not gates.is_technical_role(title, t):
        return False
    if config.is_multi_division(company["name"]):
        # Workday/SmartRecruiters listings carry no description until the
        # detail call — but the relevance gate NEEDS the description (titles
        # like "Research Scientist" say nothing about the division). Hydrate
        # first; only locality-filtered jobs at conglomerates pay the GET.
        company_fetch.hydrate_description(job)
        # Same widening src.crawl.triage's division gate applies: a WATCHED
        # conglomerate's own engineering vocabulary ([policy]
        # watch_division_titles) counts as in-field here too. Without it the
        # crawl path and the triage path disagreed about the same posting at
        # the same company, and one silently dropped what the other kept.
        if not is_relevant(title, job.get("description", ""),
                           watch_titles=_is_watched(company)):
            return False
    if t["exclude_gate"] and gates.exclude_reason(
            title, job.get("description", ""),
            allow_defense=_is_watched(company), track_id=t["id"]):
        return False
    floor = t.get("remote_mission_floor")
    if t["geo_gate"] and _whole_board(company, floor):
        # Whole-board companies are fetched with no location restriction,
        # which lets their remote and onsite-elsewhere reqs through the
        # fetch. Gate here:
        #   watched / core-mission -> local-onsite or explicitly-remote is
        #              scored (the watch tag is human-curated, the mission
        #              floor is a judged score, and ranked_jobs admits both
        #              kinds of remote into the local list);
        #   sweep   -> local-onsite ONLY. That tag is machine-set and proved
        #              untrustworthy for an out-of-area exception (slug
        #              collisions flooded the ranking with remote junk).
        gm = geo_mode(job.get("location", ""), job.get("description", ""))
        if _is_watched(company) or _mission_trusted(company, floor):
            if gm is None:
                return False
        elif gm != "onsite":
            return False
    return True


def _scored_row(job, *, company_id, company_name, track, status=None):
    """Score one fetched posting and shape it into a jobs-table row.

    The crawl path and the external-ingest path build the same row and had
    written it out twice; they differ only in where the company link comes
    from (a roster row vs. a name already resolved to an id) and in
    `status`, which ingest stamps "open" and the crawl leaves to the
    store's own default. That difference is a parameter here rather than a
    silent divergence -- the two copies had already drifted apart on it.

    `job` is a fetcher's dict: `id` and `title` are required (nothing can
    be scored without them), the rest is read defensively.
    """
    res = score_resume_fit(job["title"], job.get("description", ""),
                           location=job.get("location") or "")
    row = {
        "job_id": job["id"], "company_id": company_id,
        "company_name": company_name,
        "title": job.get("title"), "url": job.get("url"),
        "location": job.get("location"),
        "track": track,
        "geo_mode": geo_mode(job.get("location") or "",
                             job.get("description", "")) or "onsite",
        "description": (job.get("description", "") or "")[:config.MAX_DESC_CHARS],
        "posted_at": job.get("posted_at"),
        **res.as_columns(),
    }
    if status is not None:
        row["status"] = status
    return row


def _score_job(resume, company, job, track):
    company_fetch.hydrate_description(job)
    return _scored_row(job, company_id=company["id"],
                       company_name=company["name"], track=track)


def crawl_company(conn, resume, company, max_workers=6, t=None):
    """Fetch ONE store company's locality-scoped board (whole board for
    watched/sweep-tagged companies), apply the track's filters, resume-fit-
    score the new postings, and store them. Returns (n_fetched, n_kept,
    n_new). Used by the manual-add flow to pull a company's other jobs once
    it's in the roster."""
    t = _t(t)
    loc_re = None if _whole_board(company,
                                  t.get("remote_mission_floor")) else NC_RE
    try:
        jobs = company_fetch.fetch_company(company, loc_re)
    except Exception as e:
        print(f"    [!] fetch error for {company['name']}: {e}")
        return (0, 0, 0)
    # A successful non-empty snapshot is the authority on what this company
    # currently lists: close stored rows that vanished, revive returners.
    if jobs and company.get("id"):
        store.sync_job_statuses(conn, company["id"], jobs, track=t["track"])
    kept = [j for j in jobs if _keep_job(company, j, t)]
    fresh = [j for j in kept if not store.job_exists(conn, j["id"])]
    n_new = 0
    for row in fan_out(fresh, lambda j: _score_job(resume, company, j,
                                                   t["track"]),
                       "scoring", max_workers):
        # Kept separate from the scoring failure fan_out reports: a store
        # write that fails is not a scoring problem, and lumping the two
        # together is what hid the write-lock starvation in harvest.py for
        # a day (every locked write read as an unreachable board).
        try:
            store.upsert_job(conn, row)
            n_new += 1
        except Exception as e:
            print(f"    [!] store error: {e}")
    return (len(jobs), len(kept), n_new)


# How long a REFUSED marker holds off a retry (fit.unscored_cause's
# "refused" class: the model was asked and gave nothing back). A TOO-SHORT
# marker ("short") has no day horizon at all -- see _unscored_due -- because
# the same body can only fail the same way again; only growth changes
# anything, and growth is free to detect (the row's own description length).
UNSCORED_RETRY_DAYS = 30

# fit_reason for a row self_heal_unscored (or rescore_all) could not score:
# "unscored:<cause>:<body length when marked>:<date marked>". Distinct from
# every OTHER fit_reason shape in the store -- a real score's tag always
# starts "[dom" (FitResult.summary), a deep-verified one always contains
# "deep:" (verify_top._stale, web.routes._job_json read that substring) --
# so this can never be mistaken for either by an existing reader. The cause
# alternation is fit.UNSCORED_CAUSES itself: fit.unscored_cause decides the
# vocabulary, this only parses back what _unscored_marker wrote.
_UNSCORED_MARKER_RE = re.compile(
    r"^unscored:(" + "|".join(UNSCORED_CAUSES) + r"):(\d+):(\d{4}-\d{2}-\d{2})$")


def _unscored_marker(cause, desc_len, when):
    """The fit_reason marker for `cause` (fit.unscored_cause's "short" or
    "refused"), carrying what a later pass needs to decide whether another
    attempt is due: the body's length right now (so a changed body is a
    cheap length comparison, not a new column) and the date (the REFUSED
    horizon; unused for "short").

    >>> from datetime import datetime
    >>> _unscored_marker("refused", 645, datetime(2026, 1, 1))
    'unscored:refused:645:2026-01-01'
    """
    return f"unscored:{cause}:{int(desc_len)}:{when.date().isoformat()}"


def _unscored_due(fit_reason, desc_len, now=None):
    """Whether a row already carrying an _unscored_marker is due for another
    scoring attempt. No marker at all -- NULL, a real score's tag, or a
    legacy bare "unscored"/"no description; unscored" from before this
    marker existed -- is always due: this is what lets the 2026-09 NC State
    rows (stuck exactly on that legacy bare string) get re-tried once and
    then, if they refuse again, finally marked.

    >>> _unscored_due(None, 645)
    True
    >>> _unscored_due("unscored", 645)
    True

    A REFUSED row is due once its body's length has moved at all (grown OR
    shrunk -- either means the posting changed, worth a fresh ask) or once
    UNSCORED_RETRY_DAYS have passed at the SAME length:

    >>> _unscored_due("unscored:refused:645:2020-01-01", 900)
    True
    >>> from datetime import datetime
    >>> _unscored_due("unscored:refused:645:" + datetime.now().date().isoformat(),
    ...               645)
    False
    >>> _unscored_due("unscored:refused:645:2020-01-01", 645)
    True

    A TOO-SHORT row is due only once it has grown PAST the scoring
    threshold -- shrinking, or growing but still short, is not:

    >>> _unscored_due("unscored:short:150:2020-01-01", 150)
    False
    >>> _unscored_due("unscored:short:150:2020-01-01", 199)
    False
    >>> _unscored_due("unscored:short:150:2020-01-01", 250)
    True
    """
    from src.claude.fit import MIN_DESC_CHARS
    m = _UNSCORED_MARKER_RE.match(fit_reason or "")
    if not m:
        return True
    cause, marked_len, marked_date = m.group(1), int(m.group(2)), m.group(3)
    desc_len = int(desc_len)
    if cause == "short":
        return desc_len >= MIN_DESC_CHARS
    if desc_len != marked_len:
        return True
    now = now or datetime.now()
    age_days = (now.date() - datetime.strptime(marked_date, "%Y-%m-%d").date()).days
    return age_days >= UNSCORED_RETRY_DAYS


def self_heal_unscored(conn, resume, track, max_workers=6):
    """Self-heal: the fresh-only crawl loop never revisits an already-stored
    job, so a row that was ingested bodyless (unscorable -> NULL score) would
    stay out of the ranking forever even once its description is recovered.
    Score any NULL-score row that now carries a real body (hydrated by
    backfill_board_descriptions, or by an earlier run). Returns #scored.

    A row the scorer STILL can't score is no longer left exactly as found:
    it is stamped with an _unscored_marker (cause from fit.unscored_cause)
    so it is not re-asked every single crawl. Retry policy lives in
    _unscored_due: REFUSED waits UNSCORED_RETRY_DAYS or an immediate body
    change; TOO-SHORT (unreachable through THIS query's own length filter
    today, but shared with rescore_all's sibling selection below) waits for
    growth past fit.MIN_DESC_CHARS. The length recorded in the marker, and
    compared against on the next pass, is the STRIPPED body -- the same
    text score_resume_fit itself measures against MIN_DESC_CHARS -- so a
    body whose padding whitespace changes without its real content
    changing is never mistaken for "the posting changed" by _unscored_due.

    desc_checked_at is ALSO stamped here on a refusal, but it does not
    own the retry clock: _unscored_due reads its date from INSIDE the
    fit_reason marker string, never from this column, so a later stamp by
    an unrelated op (check_closed_jobs' probe, a description backfill)
    can never push the REFUSED horizon out or pull it in. The stamp is
    written anyway only to keep desc_checked_at meaning "something last
    looked at this row and found nothing new" for a human or another op
    reading it -- self_heal_unscored's own population (body >=
    MIN_DESC_CHARS) and the description-backfill ops' (body < their own
    min_len) never overlap, so this stamp can never suppress a backfill
    retry either.

    Notes:
        2026-09 evidence (data/logs/session-*.log): the same 3-7 rows at
        one university board re-entered this query and re-refused on every
        crawl for weeks ("Claude returned no text (stop_reason=refusal)",
        17 occurrences) because a refusal wrote nothing -- NULL score,
        untouched fit_reason -- so this query could never tell a row that
        had just failed apart from one that had never been tried.
    """
    from src.claude.fit import MIN_DESC_CHARS, unscored_cause
    conds, args = store.open_in_track_clause(track)
    conds += ["resume_fit_score IS NULL",
              "length(COALESCE(description,'')) >= ?"]
    args.append(MIN_DESC_CHARS)
    pending = [dict(r) for r in conn.execute(
        "SELECT job_id, title, description, location, fit_reason FROM jobs "
        "WHERE " + " AND ".join(conds), args).fetchall()]
    if not pending:
        return 0
    now = datetime.now()
    due = [r for r in pending if _unscored_due(
        r.get("fit_reason"), len((r.get("description") or "").strip()), now)]
    held = len(pending) - len(due)
    if not due:
        print(f"  self-heal: {held} previously-unscorable job(s) not yet "
              f"due for retry.")
        return 0
    print(f"  self-heal: scoring {len(due)} newly-described "
          f"job(s) that were previously unscorable"
          + (f" ({held} not yet due for retry)" if held else "") + "...")
    scored = 0
    for r, res in fan_out(due,
                          lambda r: score_resume_fit(
                              r["title"], r.get("description", ""),
                              location=r.get("location") or ""),
                          "self-heal scoring", max_workers, with_item=True):
        if res.score is not None:
            store.update_job_scores(conn, r["job_id"], res.as_columns())
            scored += 1
            continue
        cause = unscored_cause(res.reason)
        if cause is None:
            continue    # scorer offline, or an unrecognized reason: leave as is
        desc_len = len((r.get("description") or "").strip())
        store.update_job_scores(conn, r["job_id"],
                                {"fit_reason": _unscored_marker(cause, desc_len, now)})
        store.mark_desc_checked(conn, r["job_id"], now)
        detail = (f"retry in {UNSCORED_RETRY_DAYS}d or when the body changes"
                  if cause == "refused"
                  else f"retry once the body grows past {MIN_DESC_CHARS} chars")
        print(f"    [{cause}] {(r.get('title') or '')[:50]} - {detail}")
    return scored


# --------------------------------------------------------------------------- #
#  Maintenance operations (webapp OPS + run_scraper flags).                     #
# --------------------------------------------------------------------------- #

def backfill_board_descriptions(max_workers=8, limit=None, min_len=200,
                                t=None, retry_days=3):
    """One-shot: fill in full JD text for stored jobs missing it (any
    company-linked row whose description is shorter than min_len chars —
    the default matches src.claude.fit.MIN_DESC_CHARS), via each company's
    own ATS board. Batched per company so a board with several stale rows is
    fetched once. Safe to re-run: a row that failed within the last
    `retry_days` days is skipped (its desc_checked_at stamp), so reruns
    don't re-fetch every board to fail on the same vanished postings
    (retry_days=0 retries everything).

    Companies are fetched CONCURRENTLY (`max_workers`), like the Workday
    sibling below. This function advertised max_workers=8 and then never
    used it: it walked `group_by_company` one company at a time, and every
    board pull and every per-job hydration in it was a blocking GET on the
    calling thread. 2026-09-11
    (data/logs/session-20260911-162142-webui-backfill-descriptions.log):
    "backfilling 43600 description(s) via company board(s)..." at 16:21:43,
    and by 16:40:39 — nineteen minutes — roughly twenty company lines and
    about 1,878 rows had gone through, one of them ("J&J MedTech 1063
    stale -> 1060 matched", 16:37:11) holding the op alone for some nine
    minutes. The log has no footer: the run was killed before it finished.

    Notes:
        Company lines now print in COMPLETION order, not roster order —
        `fan_out` yields as boards come back, and the sibling has always
        printed that way. The counts either line adds up to are unchanged.

        Only the fetching runs in the pool. Every jobs write stays on the
        calling thread (SQLite connections are not shareable across
        threads, and the store grants one writer at a time through
        src.store.schema._WRITE_LOCK, e814fac), grouped per company by
        `store.batch` so a 1,000-row board takes the write lock once
        instead of a thousand times.

        The per-row loop stays serial INSIDE each worker: those rows share
        one board pull and, when the board doesn't cover them, hit one
        host's detail pages, which is exactly the traffic the fetchers'
        own per-source pacing is written to bound.
    """
    t = _t(t)
    with track_store(t) as conn:
        rows = stale_body_rows(conn, "company_id IS NOT NULL",
                               columns="job_id, title, url, company_id",
                               min_len=min_len, retry_days=retry_days,
                               limit=limit,
                               label="description(s) via company board(s)")
        # Resolve the company rows HERE, on the connection's own thread,
        # and hand the workers plain dicts.
        groups, boardless = [], []
        for cid, rs in group_by_company(rows).items():
            company = store.get_company(conn, cid)
            if not company or not company.get("ats"):
                # No board to fetch IS a failed attempt — stamp these rows too,
                # or they are re-selected (and silently re-counted) every run
                # while never even printing a company line.
                boardless.extend(rs)
                continue
            groups.append((dict(company), rs))
        if boardless:
            with store.batch(conn):
                for r in boardless:
                    save_body(conn, r["job_id"], None)

        def _bodies(group):
            """One company's fetching, in a worker thread: the batched board
            pull for the common case (one fetch per company), then per-job-URL
            hydration for the rows that pull didn't cover. Boards we can't
            pull simply yield no title matches, so every row falls through to
            hydration either way. Returns [(job_id, description_or_None)] for
            the caller's thread to write."""
            company, rs = group
            index = board_index(company)
            out = []
            for r in rs:
                match = board_match(index, r["title"])
                desc = match.get("description") if match else None
                if not desc and r.get("url"):
                    # Board didn't cover this row — hydrate from the job's own
                    # detail page (JSON-LD / career-site markup).
                    stub = {"title": r["title"], "url": r["url"],
                            "ats": company.get("ats"), "description": ""}
                    company_fetch.hydrate_description(stub)
                    desc = stub.get("description")
                out.append((r["job_id"], desc))
            return out

        n = 0
        for (company, rs), bodies in fan_out(
                groups, _bodies,
                lambda g: f"{g[0]['name']} board backfill",
                max_workers, with_item=True):
            n_matched = 0
            with store.batch(conn):
                for job_id, desc in bodies:
                    if save_body(conn, job_id, desc):
                        n += 1
                        n_matched += 1
            print(f"    {company['name']:30} {len(rs):2} stale -> {n_matched:2} matched")
    print(f"  {n} of {len(rows)} description(s) backfilled.")
    return n


def backfill_workday_descriptions(max_workers=8, limit=None, min_len=200,
                                  t=None, retry_days=3):
    """The same backfill, for stored Workday rows, via the CXS per-job
    endpoint rather than a whole-board pull. Only touches rows whose URL is
    a myworkdayjobs.com board.

    Lived in src/ats/fetchers/workday.py, which made it the one backfill
    that could not see `track_store`: it opened `store.connect()` with no
    argument, so under a profile that gives a track its own `db` it
    backfilled the DEFAULT store while reporting the track's name. Same
    divergence the roster ops had, same fix.
    """
    from src.ats.fetchers.workday import fetch_workday_description

    t = _t(t)
    with track_store(t) as conn:
        rows = stale_body_rows(conn, "url LIKE '%myworkdayjobs.com%'",
                               columns="job_id, url", min_len=min_len,
                               retry_days=retry_days, limit=limit,
                               label="Workday description(s) via CXS")

        def _one(r):
            return r["job_id"], fetch_workday_description(r["url"])

        n, empty = 0, []
        for jid, text in fan_out(rows, _one, "backfill"):
            if save_body(conn, jid, text):
                n += 1
            else:
                empty.append(jid)
    # "0 of 4 backfilled" with no why was undiagnosable from the session
    # log; name the silent failures (CXS answered but returned no JD text,
    # usually a posting that closed since it was stored).
    if empty:
        print(f"    [!] {len(empty)} fetch(es) returned no JD text "
              f"(posting gone from CXS?): "
              + ", ".join(empty[:5]) + (" ..." if len(empty) > 5 else ""))
    print(f"  {n} of {len(rows)} description(s) backfilled.")
    return n


def rescore_all(max_workers=6, track=None, described_only=False, t=None):
    """Re-run resume-fit scoring over every stored job in the track's DB
    (all jobs.track values unless `track` names one). Use after changing the
    resume or the scoring prompt — the normal crawl only scores jobs it
    hasn't seen.

    described_only: only touch rows that have a real JD body. Without it, a
    bodyless row's stale score is cleared to NULL so it drops out of
    ranking; a *described* row that merely fails to parse keeps its score.

    Closed and dispositioned-out jobs are always skipped — no Claude API
    spend on postings that can't surface anyway. A row cleared for having no
    body is marked with self_heal_unscored's own _unscored_marker (cause
    "short") rather than a bare string, so the store has one vocabulary for
    "nothing to score here" everywhere it appears -- this call always runs
    the clearing itself (a rescore is an explicit, one-off ask), so unlike
    self_heal_unscored it does not consult _unscored_due first."""
    from src.claude.fit import MIN_DESC_CHARS
    t = _t(t)
    resume = resume_text()
    if not resume:
        print("  [!] No resume text - cannot rescore. Set config.RESUME_PATH.")
        return 0
    with track_store(t) as conn:
        conds, args = store.open_in_track_clause(track)
        if described_only:
            conds.append("length(COALESCE(description,'')) >= ?")
            args.append(MIN_DESC_CHARS)
        q = "SELECT job_id, title, description, location FROM jobs"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        print(f"  rescoring {len(rows)} job(s) against the current resume...")
        now = datetime.now()

        def _one(r):
            res = score_resume_fit(r["title"], r.get("description", ""),
                                   location=r.get("location") or "")
            return r["job_id"], res, r.get("description", "")

        n = 0
        for jid, res, desc in fan_out(rows, _one, "rescore", max_workers):
            if res.score is None:
                # Unscorable (no real body): clear the stale score so it
                # drops from ranking. A described row that merely failed to
                # parse keeps its score.
                if len((desc or "").strip()) < MIN_DESC_CHARS:
                    store.update_job_scores(
                        conn, jid, {"fit_reason": _unscored_marker(
                            "short", len((desc or "").strip()), now)})
                    n += 1
                continue
            store.update_job_scores(conn, jid, res.as_columns())
            n += 1
    print(f"  {n} job(s) rescored.")
    return n


def _live_jd(row):
    """Freshest full JD text for one stored job row, preferring a live
    detail fetch (Workday CXS, Greenhouse boards API, then the generic
    JSON-LD/careers-page extractor) over the stored text. Falls back to the
    stored description when the live pull is shorter or fails — the deep
    verify pass must never see LESS text than the first pass did."""
    url = row.get("url") or ""
    text = ""
    try:
        if "myworkdayjobs.com" in url:
            from src.ats.fetchers.workday import fetch_workday_description
            text = fetch_workday_description(url) or ""
        else:
            # Greenhouse job-page URL -> (board slug, job id), and the
            # boards-API root: one definition each, in the module that owns
            # the platform (works for boards. and job-boards.greenhouse.io).
            from src.ats.fetchers.api import (GREENHOUSE_API,
                                              GREENHOUSE_JOB_URL_RE)
            m = GREENHOUSE_JOB_URL_RE.search(url)
            if m:
                import html as _html

                from bs4 import BeautifulSoup

                from src.net.http import HEADERS, SESSION
                r = SESSION.get(
                    f"{GREENHOUSE_API}/{m.group(1)}/jobs/{m.group(2)}"
                    f"?content=true",
                    timeout=20, headers=HEADERS)
                if r.status_code == 200:
                    text = BeautifulSoup(
                        _html.unescape(r.json().get("content", "") or ""),
                        "lxml").get_text(" ")
        if not text and url:
            text = company_fetch._description_from_job_url(url)
    except Exception:
        text = ""
    stored = row.get("description") or ""
    return text if len(text) > len(stored) else stored


def _verify_floor_candidates(conn, t, floor, exclude_ids=()):
    """Track `t`'s open triage_status='fit' rows screened at or above
    `floor`, located locally (NC_RE) or stored remote_eligible, best screen
    score first, less `exclude_ids` (the top-N slice): the rows verify_top
    reaches past its top N.

    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_job(conn, {"job_id": "j1", "title": "T",
    ...                             "track": "local-tech", "location": "Elsewhere",
    ...                             "resume_fit_score": 0.3, "remote_eligible": 1})
    >>> store.record_triage(conn, "j1", "fit", "local-tech=ok")
    >>> _ = store.upsert_job(conn, {"job_id": "j2", "title": "T",
    ...                             "track": "local-tech", "location": "Elsewhere",
    ...                             "resume_fit_score": 0.3})
    >>> store.record_triage(conn, "j2", "fit", "local-tech=ok")
    >>> [r["job_id"] for r in _verify_floor_candidates(
    ...     conn, {"track": "local-tech"}, 0.25)]
    ['j1']

    Notes:
        A 'fit' row already carries the track label (triage's
        record_triage merges it for 'fit' and 'ok' alike), so the track
        LIKE finds it. The location test reads what triage stored, not the
        ranking's remote_admitted trust rule: this chooses where verify
        calls go, it does not admit rows to the ranking.
    """
    conds, args = store.open_in_track_clause(t["track"])
    conds += ["triage_status = 'fit'", "resume_fit_score >= ?"]
    args.append(floor)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE " + " AND ".join(conds)
        + " ORDER BY resume_fit_score DESC", args).fetchall()]
    return [r for r in rows if r["job_id"] not in exclude_ids
            and (NC_RE.search(r.get("location") or "")
                 or r.get("remote_eligible") == 1)]


def verify_top(top_n=15, max_workers=4, rounds=2, conn=None, t=None,
               force=False):
    """Deep-verify the ranking's FINALISTS before anyone acts on them: for
    each of the current top `top_n` jobs, PLUS enough triage_status='fit'
    candidates (_verify_floor_candidates: local/remote, screened at or
    above the track's `verify_floor`, ordered by screen score descending)
    to fill the SAME top_n budget when the top-N slice itself has fewer
    than top_n rows that need it — not already verified BY THE CURRENT
    verify model (fit_reason carrying the 'deep:' marker and fit_model
    naming fit.verify_model()) — re-fetch the freshest full posting text
    (_live_jd), run fit.verify_fit — which extracts hard requirements before
    re-scoring all axes and gates — and write the verified scores back.
    Demotions can pull new unverified rows into the top, so the pass
    re-ranks and repeats up to `rounds` times. A 'fit' row whose deep score
    reaches the track's digest_min_fit is relabelled 'ok'
    (store.record_triage), as triage would have surfaced it.

    Unverifiable rows (dead URL and no stored body, API down) keep their
    first-pass score untouched; once src.claude's breaker has disabled the
    API for this run the pass stops with ONE line instead of a per-row
    'unverified' (2026-09-09: 121 rows x 2 rounds x 2 runs, every live JD
    fetched for nothing). Costs at most top_n x rounds API calls per
    run, and only for rows that changed since their last verification or
    were verified by an older model (fit_model NULL counts as older).
    `force=True` re-verifies every finalist regardless (candidates are
    still capped at top_n; force does not widen the round's own budget).

    Notes:
        The floor candidates exist because digest_min_fit keeps a weak
        screen score out of the TOP of the ranking, not out of it: an
        underrated row (2026-09-09: screen 0.16, deep 0.50) can sit where
        top_n never reaches, however many rounds run. They only take the
        slots the top-N slice left unspent, so a busy run costs nothing
        extra.
    """
    from src.claude.api import api_disabled
    from src.claude.fit import (DEEP_MARKER, FitResult, is_deep_verified,
                                verify_fit, verify_model)
    t = _t(t)
    current = verify_model()
    done_ids = set()   # verified THIS run: never stale again, even under force

    def _stale(r):
        if r["job_id"] in done_ids:
            return False
        if force or not is_deep_verified(r.get("fit_reason")):
            return True
        return (r.get("fit_model") or "") != current

    with track_store(t, conn) as conn:
        n_done = 0
        for rnd in range(rounds):
            down = api_disabled()
            if down:
                # Tripped before this round (the crawl's screen pass, or an
                # earlier round here): nothing below can score, so say so once
                # instead of fetching every finalist's live JD to print
                # '[?] kept' per row.
                print(f"  [!] deep verify skipped: Claude API disabled for this "
                      f"run ({down})")
                break
            ranked = _ranked(conn, t, limit=top_n)
            stale_top = [r for r in ranked if _stale(r)]
            remaining = top_n - len(stale_top)
            candidates = []
            floor = t["verify_floor"]
            if remaining > 0:
                seen_ids = {r["job_id"] for r in ranked}
                floor_rows = _verify_floor_candidates(
                    conn, t, floor, exclude_ids=seen_ids)
                candidates = [r for r in floor_rows if _stale(r)][:remaining]
            todo = stale_top + candidates
            if not todo:
                if rnd == 0:
                    print(f"  deep-verify [{t['track']}]: nothing new in the "
                          f"top {top_n} or at/above {floor:.2f} for {current}")
                break
            print(f"  deep-verifying {len(stale_top)} of the top {len(ranked)}"
                  + (f" and {len(candidates)} floor candidate(s) at/above "
                     f"{floor:.2f}" if candidates else "")
                  + f" with {current} (round {rnd + 1}/{rounds}"
                  f"{', forced' if force else ''})...")

            def _one(r):
                # The breaker can trip mid-round (2026-09-09: the crawl's FIRST
                # verify call hit an exhausted credit balance). A row the API
                # can no longer score doesn't need its live JD fetched.
                if api_disabled():
                    return r, None, FitResult(score=None, reason="api disabled")
                text = _live_jd(r)
                return r, text, verify_fit(r["title"], text,
                                           location=r.get("location") or "")

            n_scored = n_crushed = 0
            halted = None
            for r, text, res in fan_out(todo, _one, "verify", max_workers):
                if res.score is None:
                    halted = api_disabled()
                    if halted:
                        # One line for the round, not one '[?] kept' per
                        # finalist. Breaking out cancels the rows still
                        # queued (fan_out does not join on the way out).
                        break
                    print(f"    [?] kept   {r['title'][:46]} - {res.reason}")
                    continue
                store.update_job_scores(conn, r["job_id"], res.as_columns())
                done_ids.add(r["job_id"])
                if text and len(text) > len(r.get("description") or ""):
                    conn.execute("UPDATE jobs SET description=? WHERE job_id=?",
                                 (text[:config.MAX_DESC_CHARS], r["job_id"]))
                    conn.commit()
                # A floor candidate that reaches the track's digest_min_fit on
                # the deep score surfaces exactly as triage would have surfaced
                # it first-pass; one that doesn't keeps triage_status='fit' —
                # its corrected score is still recorded above either way.
                if (r.get("triage_status") == "fit"
                        and res.score >= t["digest_min_fit"]):
                    store.record_triage(conn, r["job_id"], store.TRIAGE_OK,
                                        r.get("triage_detail") or "",
                                        tracks=[t["track"]])
                old = r.get("resume_fit_score")
                move = (f"{old:.2f} -> {res.score:.2f}"
                        if isinstance(old, float) else f"?    -> {res.score:.2f}")
                flag = "  [DEMOTED]" if isinstance(old, float) and \
                    res.score < old - 0.15 else ""
                reason = (res.reason or "").removeprefix(f"{DEEP_MARKER} ")[:90]
                print(f"    {move}, {r['company_name']}, {r['title'][:44]}, "
                      f"{reason}{flag}")
                n_done += 1
                n_scored += 1
                if isinstance(old, float) and res.score < old - 0.25:
                    n_crushed += 1
            if halted:
                print(f"  [!] deep verify halted: Claude API disabled for this run "
                      f"({halted}); {len(todo) - n_scored} finalist(s) keep their "
                      f"first-pass score")
                break
            # Tripwire: the two passes disagreeing WHOLESALE is a calibration or
            # parsing defect, not information. Stop instead of compounding.
            if n_scored >= 5 and n_crushed / n_scored >= 0.8:
                print(f"\n  [!] TRIPWIRE: {n_crushed}/{n_scored} verified rows "
                      f"dropped by >0.25 this round. The deep pass is disagreeing "
                      f"with the screen wholesale — that pattern means a prompt/"
                      f"parsing defect, not 30 bad jobs. Halting further rounds; "
                      f"inspect fit_gates on the demoted rows before trusting "
                      f"this ranking.")
                break
        return n_done


def verify_top_cli(top_n=15, max_workers=4, t=None, force=False):
    """Standalone verify: deep-verify the current top N in the store (no
    crawl), then rewrite the digest and print the corrected top. `force`
    re-verifies rows the current verify model already checked."""
    t = _t(t)
    n = verify_top(top_n=top_n, max_workers=max_workers, t=t, force=force)
    with track_store(t) as conn:
        n_open = len(_ranked(conn, t))
        rewrite_digest(conn, t, top_n,
                       f"\n  {n} job(s) deep-verified; corrected top "
                       f"{min(top_n, n_open)}:")
    return n


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

# The miss-reason family this closes on -- the one of the two
# RERESOLVE_FAMILIES (built from this name further down the module) that
# means "a board WAS here": "no-board-found" never had a working board in
# the first place, so it cannot own OPEN job rows to close.
_DEAD_BOARD_FAMILY = "board-dead"

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
    fam = probe.probe_family(url)
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
    accepting" notice, a past JSON-LD validThrough, a Workday CXS miss, an
    id absent from a non-empty board listing -- see
    fetchers.probe.probe_job_open). Indeterminate probes (bot-gated
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
                return probe.probe_job_open(r["url"])
            except Exception as e:          # noqa: BLE001 - an outcome
                return None, f"probe error: {type(e).__name__}"

        now = datetime.now()
        n_closed = n_live = n_unknown = n_parked = 0
        counts, reasons = defaultdict(Counter), defaultdict(Counter)
        for r, (is_open, reason) in fan_out(rows, _probe, "probe",
                                            max_workers, with_item=True):
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
              f"{n_unknown} unverifiable (left open) of {len(rows)} probed.")
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


def _hydrate_missing_descriptions(conn, jobs):
    """Backfill empty descriptions on jobs linked to a company with a
    resolvable board, batched so each board is fetched once no matter how
    many of its jobs need hydrating."""
    need = [j for j in jobs if j.get("_company_id") and not (j.get("description") or "").strip()]
    if not need:
        return
    for cid, js in group_by_company(need, "_company_id").items():
        company = store.get_company(conn, cid)
        if not company or not company.get("ats"):
            continue
        index = board_index(company)
        n_hydrated = 0
        for j in js:
            match = board_match(index, j.get("title"))
            if match is not None:
                j["description"] = match["description"]
                j["url"] = j.get("url") or match.get("url")
                n_hydrated += 1
        if n_hydrated:
            print(f"    hydrated {n_hydrated}/{len(js)} description(s) from "
                  f"{company['name']}'s {company['ats']} board")


def ingest_external_jobs(jobs, source="indeed", max_workers=6, curated=False,
                         t=None):
    """Ingest external job dicts into the track's jobs table with resume-fit
    scores. Each dict: {id?, title, company, url, location, description?}.
    Applies the same exclude + technical-title gate as the crawl. For
    agent-mediated sources (e.g. a LinkedIn capture) the caller supplies the
    fetched jobs.

    `curated=True` (manual --add): the caller hand-picked these jobs, so the
    exclude + technical-title guesswork is skipped — but the geo gate (when
    the track has one) still applies: a location-scoped track is
    locality-bound by definition."""
    import hashlib
    t = _t(t)
    with track_store(t) as conn:
        kept, n_nonlocal = [], 0
        for j in jobs:
            if not j.get("id"):
                key = (j.get("url") or "") + (j.get("title") or "") + (j.get("company") or "")
                j["id"] = f"{source}_{hashlib.md5(key.encode()).hexdigest()[:12]}"
            company_id = store.company_id_by_name(conn, j.get("company"))
            company_row = store.get_company(conn, company_id) if company_id else None
            if t["geo_gate"]:
                # Location-scoped track: gate ingested jobs on the same locality
                # filter the live crawl applies inside its fetchers, with one
                # relaxation — a posting from a company the ranking trusts with
                # an out-of-area exception (watched, or core-mission at the
                # track's remote_mission_floor) still passes when it's
                # explicitly remote. Enforced even for curated adds.
                loc = j.get("location", "") or ""
                is_local = bool(NC_RE.search(loc))
                trusted = (_is_watched(company_row)
                           or _mission_trusted(company_row,
                                               t.get("remote_mission_floor")))
                is_remote_trusted = (
                    trusted
                    and geo_mode(loc, j.get("description", "")) == "remote")
                if not (is_local or is_remote_trusted):
                    n_nonlocal += 1
                    continue
            if not curated:
                if t["exclude_gate"] and gates.exclude_reason(
                        j.get("title", ""), j.get("description", ""),
                        track_id=t["id"]):
                    continue
                if not gates.is_technical_role(j.get("title", ""), t):
                    continue
            if store.job_exists(conn, j["id"]):
                # Already stored — but the source just showed it live, so reopen
                # a closed row and reset its grace clock (no re-score).
                store.touch_job(conn, j["id"])
            else:
                # Resolve the company link on the MAIN thread — SQLite
                # connections can't cross into the scoring pool below.
                j["_company_id"] = company_id
                kept.append(j)

        _hydrate_missing_descriptions(conn, kept)

        def _score(j):
            return _scored_row(j, company_id=j.get("_company_id"),
                               company_name=j.get("company"),
                               track=t["track"], status="open")

        scored = 0
        for row in fan_out(kept, _score, "ingest scoring", max_workers):
            try:
                store.upsert_job(conn, row)
                scored += 1
            except Exception as e:
                print(f"    [!] ingest store error: {e}")
        print(f"  ingested {scored} new {source} job(s) ({len(kept)} kept, "
              f"{n_nonlocal} out-of-area dropped, {len(jobs)} raw)")
        return scored


def add_manual_job(url, title, company, location, description="",
                   pull_board=True, max_workers=6, t=None):
    """Add ONE hand-picked job, register/resolve its COMPANY, and — if that
    company's board resolves — pull its OTHER in-scope jobs too.

    The single job is curated (exclude/technical gates skipped, you chose
    it) but still geo-gated on location-scoped tracks. For bot-gated giants
    the board won't resolve, so only the one job lands and the company is
    recorded as a MISS carrying the reason, which reresolve_misses retries
    later. Returns a summary dict.

    Notes:
        Resolution goes through src.discovery.resolve.board.resolve_or_miss —
        the same careers-page-sniff-first resolver every other interactive
        add path uses. It replaced a probe-first resolver that guessed a
        slug from the name before looking at the company's own site, which
        is exactly the collision this path is most exposed to: a hand-typed
        employer name lands on a same-named stranger's board.
    """
    from src.claude.api import is_active_mission, score_company_mission
    from src.discovery.local_sourcing import mission_context
    from src.discovery.resolve.board import resolve_or_miss

    t = _t(t)
    name = (company or "").strip()
    title = (title or "").strip()
    if not name or not (url or title):
        print("  [!] --add needs --company plus at least --url or --title.")
        return {}
    if not title:
        # URL-only add: read the title (and, if none was given, the JD) off
        # the posting page itself, then fall back to the URL's slug. An
        # empty title is not a job — it can't be scored (SKIP-SCORE) or
        # ranked, and nine such rows sat in the 2026-09-01 store.
        page_title, page_desc = company_fetch.job_page_meta(url)
        title = page_title or company_fetch.title_from_url_slug(url)
        if not title:
            print(f"  [!] no title given and none readable from {url}; "
                  f"pass --title.")
            return {}
        print(f"    title from {'page' if page_title else 'URL slug'}: {title!r}")
        if page_desc and not (description or "").strip():
            description = page_desc

    # 1) Company: resolve a board if we don't already have one for it, so
    #    the job links to a real company row.
    with track_store(t) as conn:
        # The same indexed lookup step 2's ingest uses to LINK the job to a
        # company row. Both halves had to agree: scanning the roster in
        # Python picked the best-scored row while the ingest picked the
        # lowest id, so a store holding two case-variant rows for one name
        # could register/crawl one of them and file the job under the other.
        existing = store.get_company(conn, store.company_id_by_name(conn, name))
        board, miss = None, None
        if not existing or not existing.get("ats"):
            print(f"  resolving board for {name!r}...")
            # A hit carrying a reason ("no-local-jobs") is a live, readable
            # board with nothing open here today — worth registering, exactly
            # as the probe-first resolver's nc=0 hit was.
            board, miss = resolve_or_miss(name)
        if board:
            tier, score, reason = score_company_mission(
                name, mission_context(board))
            active = is_active_mission(tier, name)
            store.upsert_company(conn, coords.from_hit(
                board, name=name,
                local_job_count=board["nc"], total_job_count=board["count"],
                mission_tier=tier, mission_score=score, mission_reason=reason,
                tags=tags.LOCAL if board["nc"] else None,
                source="manual_add", active=active))
            print(f"    board resolved: {board['ats']} nc={board['nc']} "
                  f"mission={tier} ({score if score is not None else 'n/a'})")
        elif miss:
            # Resolution was attempted and failed. Keep WHY on the row rather
            # than a prose note: that is what reresolve_misses selects on.
            store.record_miss(conn, name, miss, source="manual_add",
                              notes=None if existing else f"manual add from {url}")
            print(f"    company recorded as a miss [{miss}] — board unresolved "
                  f"(gated / unknown ATS)")
        else:
            print(f"    company already in roster (ats={existing.get('ats')})")

    # 2) The single job — curated (skip exclude/technical), geo gate still on.
    print(f"  adding job: {title!r} @ {name} [{location}]")
    n_job = ingest_external_jobs(
        [{"title": title, "company": name, "url": url,
          "location": location or "", "description": description or ""}],
        source="manual", curated=True, t=t)

    # 3) The company's OTHER jobs — crawl its board whenever it has one
    #    (freshly resolved OR already in the roster), unless --no-board.
    n_other = 0
    with track_store(t) as conn:
        row = store.get_company(conn, store.company_id_by_name(conn, name))
        has_board = bool(row and row.get("ats"))
        if pull_board and has_board:
            _, _, n_other = crawl_company(conn, resume_text(), row, max_workers, t=t)
            print(f"    pulled {n_other} other in-scope job(s) from {name}'s board")

    status = "active board" if has_board else "recorded (board unresolved)"
    print(f"\n  DONE: +{n_job} job, +{n_other} from board; company '{name}' - {status}.")
    return {"job_added": n_job, "other_jobs": n_other,
            "board": has_board, "company": name}


def prune_dead_boards(conn, max_workers=12, deactivate_offmission=False):
    """Deactivate active companies whose JSON-API ATS board no longer resolves
    (a hard 404/error, the source of the crawl's `HTTP 404` spam), and
    optionally off-mission `other`-tier companies (excluding multi-division).
    Only ATSes whose board endpoint cleanly distinguishes "exists" (200)
    from "dead" (404) are probed. Returns (n_dead, n_offmission).

    Lived in src.store until 2026-09-10; it probes the network and applies
    roster policy, so it is an operation, and the store keeps only the
    write (store.deactivate_company).

    Prints how many boards it probes, then one line per company it
    deactivates (name, ATS, reason), so a clean run still leaves a trace.
    """
    from src.discovery.resolve.probes import (probe_greenhouse, probe_lever,
                                   probe_ashby, probe_bamboohr)

    def _ultipro_alive(slug):
        # Not src.discovery.resolve.probes.probe_ultipro: its ok flag means "has jobs",
        # which would prune a live-but-currently-empty board. Dead here
        # means the board REQUEST fails (the 404 spam three roster rows
        # produced in every 2026-08-28 crawl log); an empty listing is
        # alive.
        from src.ats.fetchers.ultipro import parse_board
        try:
            return (True, len(parse_board(slug)))
        except Exception:
            return (False, 0)

    PROBE = {"greenhouse": probe_greenhouse, "lever": probe_lever,
             "ashby": probe_ashby, "bamboohr": probe_bamboohr,
             "ultipro": _ultipro_alive}

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
                   and not _is_watched(c)]
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
#     LLC."). fetchers.api._greenhouse_row never reads it -- it builds the
#     row's title/location/description and drops the rest of the object.
#   * SmartRecruiters -- every posting carries `company.name` (confirmed
#     live: "AbbVie", "Eurofins"). fetchers.company.fetch_smartrecruiters_all
#     never reads it either.
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


def _employer_name_greenhouse(slug):
    """The employer name Greenhouse's OWN board carries for `slug` (a
    posting's `company_name`), or "" on any failure or an empty board.

    `content=false` is the same lightweight listing shape
    src.ats.fetchers.api.BOARD_URLS already uses for a metadata-only read --
    this needs one field off one posting, not every posting's full JD."""
    data = get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
        f"Greenhouse {slug} (employer-name check)", default={})
    jobs = (data or {}).get("jobs") or []
    return (jobs[0].get("company_name") or "").strip() if jobs else ""


def _employer_name_smartrecruiters(slug):
    """The employer name SmartRecruiters' OWN board carries for `slug` (a
    posting's `company.name`), or "" on any failure or an empty board.

    `limit=1` is the same shape src.discovery.resolve.probes
    .probe_smartrecruiters already uses -- one posting is enough to name
    the board."""
    data = get_json(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
        f"SmartRecruiters {slug} (employer-name check)", default={})
    content = (data or {}).get("content") or []
    return ((content[0].get("company") or {}).get("name") or "").strip() \
        if content else ""


# ats -> the reader above for it. Only an ATS with a reliable, BOARD-level
# (not per-posting) employer field in its own listing payload qualifies --
# see the module comment above for why Workday/Lever/Ashby are not here.
_EMPLOYER_NAME_READERS = {"greenhouse": _employer_name_greenhouse,
                          "smartrecruiters": _employer_name_smartrecruiters}


def _slug_named_boards(conn):
    """The active companies on a supported ATS (_EMPLOYER_NAME_READERS)
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
    ph = ",".join("?" for _ in _EMPLOYER_NAME_READERS)
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, name, ats, slug, source, total_job_count FROM companies "
        f"WHERE COALESCE(active,0)=1 AND ats IN ({ph})",
        tuple(_EMPLOYER_NAME_READERS)).fetchall()]
    rows = [r for r in rows if coords.slug_named(r)]
    rows.sort(key=lambda r: -(r.get("total_job_count") or 0))
    return rows


def rename_slug_boards(conn=None, t=None, commit=False, limit=None):
    """PREVIEW (default) or APPLY a rename of every active, dork-sourced
    Greenhouse/SmartRecruiters board whose stored name is nothing but its
    own board slug (_slug_named_boards) to the employer name the board's
    OWN listing payload carries (_EMPLOYER_NAME_READERS). One GET per
    candidate board, no detail fetch, no whole-board pull.

    Same preview/apply shape as reresolve_misses: `commit=False` (the
    default -- this NEVER renames silently) fetches every candidate's real
    name and prints one line each; nothing is written. `commit=True` is
    the only thing that writes, one line per rename actually made.

    A fetched name is rejected -- reported, never written, in EITHER mode
    -- when:
      * the board answered empty or errored (_EMPLOYER_NAME_READERS -> "");
      * src.match.names.junk_name_reason flags it -- the SAME screen a
        pasted or re-resolved name is run through, so a malformed payload
        naming a section heading rather than an employer can never
        overwrite a roster row;
      * it is BYTE-IDENTICAL to what is already stored -- nothing to fix.
        This is deliberately NOT a name_key comparison: name_key strips
        spaces, so it cannot tell "Centria Autism" (what the payload
        carries) from "Centriaautism" (the slug-derived name stored) --
        the exact improvement this op exists to make. An earlier version
        used name_is_own_slug(new_name, slug) here and, live-checked
        against the 22 real candidates on 2026-09-18, wrongly skipped 8 of
        14 genuine renames as "nothing to fix" (Axsome Therapeutics,
        Shields Health Solutions, Garner Health, Beam Therapeutics,
        Formation Bio, American Institutes for Research, MapLight
        Therapeutics, Eliot Community Human Services) because each one's
        name_key happens to equal its own slug's, spaces and all;
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
            new_name = _EMPLOYER_NAME_READERS[c["ats"]](c["slug"])
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
