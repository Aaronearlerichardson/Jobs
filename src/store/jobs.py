"""The `jobs` table: postings, their track membership, and their scores.

Track membership (jobs.track holds a comma-separated SET, not one name),
the upsert and dedup paths, the harvest triage columns src/crawl/triage.py
writes, and the status/score/ranking reads the digest and the web UI make.

Split out of store/__init__.py alongside companies.py. The two share
nothing: this module never reads the companies table and the roster half
never reads this one. `combined_score` and the track-set helpers are here
because ranking and upsert are their only callers.

Never imports store/__init__ at load time (that module imports this one).
"""

import math
import re
from datetime import datetime, timedelta

from src import config
from src import tags
from .schema import _commit, batch, connect  # noqa: F401  (doctests connect)


def combined_score(fit, mission):
    """Geometric mean sqrt(fit * mission) of the resume-fit and company
    mission scores (both 0..1).

    >>> combined_score(0.25, 0.64)
    0.4
    >>> combined_score(1.0, 1.0)
    1.0

    Floats are compared at a stated precision, never by their full repr —
    the house rule for any numeric doctest:

    >>> round(combined_score(0.9, 0.2), 4)
    0.4243
    >>> round(combined_score(0.5, 0.5), 4)
    0.5

    Those two lines are the point of the geometric mean: it punishes
    imbalance, so a strong fit at a weak-mission company (0.42) ranks below
    a job that is merely solid on both axes (0.50).

    A missing factor is unranked, NOT zero — a job is only scored once both
    axes are known:

    >>> combined_score(None, 0.9) is None
    True
    >>> combined_score(0.9, None) is None
    True

    Negative input is out of domain and yields None rather than a
    ``ValueError`` from ``sqrt`` or a bogus positive from sqrt(-a * -b):

    >>> combined_score(-0.5, 0.5) is None
    True
    >>> combined_score(-0.5, -0.5) is None
    True

    Zero is a legitimate score and stays zero:

    >>> combined_score(0.0, 0.9)
    0.0
    """
    if fit is None or mission is None:
        return None
    if fit < 0 or mission < 0:
        return None
    return math.sqrt(fit * mission)


# --------------------------------------------------------------------------- #
#  Track membership                                                            #
# --------------------------------------------------------------------------- #
#
# jobs.track holds a comma-separated SET of track names, not one name: the
# same posting can belong to several tracks (a neural-company job in your
# area is both local and neural material), and one store now serves every
# track. Same shape as companies.tags, and matched the same way in SQL.

def track_set(value):
    """The set of track names in a stored `track` value ('' / None -> set())."""
    return {t.strip() for t in (value or "").split(",") if t.strip()}


def join_tracks(tracks):
    """Canonical stored form for a set of track names (sorted, comma-joined)."""
    return ",".join(sorted(t for t in tracks if t)) or None


# SQL fragment + arg for "this row belongs to track ?" — the comma-delimited
# LIKE that get_companies already uses for tags.
_TRACK_MATCH_SQL = "(',' || COALESCE(j.track,'') || ',') LIKE ?"


def _track_match_arg(track):
    return f"%,{track},%"


# --------------------------------------------------------------------------- #
#  Jobs                                                                        #
# --------------------------------------------------------------------------- #

def job_exists(conn, job_id):
    return conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is not None


def crawl_seen(conn, job_id):
    """Has a CRAWL already handled this posting? True for a row that
    carries a track label -- the crawl stamps one whether it scored the row
    or stored it unscored under a budget guard. A row the harvester stored
    (no track yet) reads as unseen, so the crawl still gates and scores it:

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "h1", "title": "T",
    ...                       "harvested_at": "2026-09-10T01:00:00"})
    >>> job_exists(conn, "h1"), crawl_seen(conn, "h1")
    (True, False)
    >>> _ = upsert_job(conn, {"job_id": "h1", "title": "T", "track": "local"})
    >>> crawl_seen(conn, "h1")
    True
    """
    row = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                       (job_id,)).fetchone()
    return bool(row and (row["track"] or "").strip())


def descriptions_for_company(conn, company_id):
    """{job_id: description} for a company's stored rows that have a body,
    so a crawl can reuse what the harvester already hydrated instead of
    re-fetching every detail page."""
    if not company_id:
        return {}
    return {r["job_id"]: r["description"] for r in conn.execute(
        "SELECT job_id, description FROM jobs WHERE company_id=? "
        "AND length(COALESCE(description,'')) > 0", (company_id,))}


# --------------------------------------------------------------------------- #
#  Harvest triage (src/crawl/triage.py)                                        #
# --------------------------------------------------------------------------- #

# Row verdicts, cheapest gate first. The digest and the pass summary count
# rows by these; `ok` is the only one that puts a row into a track set.
TRIAGE_GATES = ("mission", "title", "anchor", "geo", "exclude", "division",
                "fit")
TRIAGE_OK = "ok"


def triage_pending(conn, company_id=None, limit=None):
    """Open, company-linked rows no crawl has adopted (no track label) and
    triage has not judged yet -- the harvester's unscored material. A row
    triage looked at but could not hydrate stays NULL, so it comes back
    here next pass.

    >>> from src.store import upsert_company
    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> for jid, extra in [("h1", {}), ("h2", {"track": "local"}),
    ...                    ("h3", {"status": "closed"}),
    ...                    ("h4", {"triage_status": "title"})]:
    ...     _ = upsert_job(conn, {"job_id": jid, "title": "T",
    ...                           "company_id": cid, **extra})
    >>> record_triage(conn, "h4", "title", "local=title")
    >>> [r["job_id"] for r in triage_pending(conn)]
    ['h1']
    """
    q = ("SELECT j.*, c.name AS company_name_row FROM jobs j "
         "JOIN companies c ON j.company_id = c.id "
         "WHERE j.triage_status IS NULL "
         "AND COALESCE(j.track,'') = '' "
         "AND COALESCE(j.status,'open') != 'closed'")
    args = []
    if company_id is not None:
        q += " AND j.company_id = ?"
        args.append(company_id)
    q += " ORDER BY j.company_id, j.first_seen"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def record_triage(conn, job_id, status, detail, *, tracks=(), description=None,
                  geo_mode=None, remote_signal=None, scores=None, now=None):
    """Write one row's triage verdict. `tracks` (the track labels the row
    surfaced into) MERGE into the stored set exactly as a crawl's label
    would, so crawl_seen reads the row as handled; `description` fills an
    empty body only; `scores` is a FitResult.as_columns() dict.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T", "track": "x"})
    >>> record_triage(conn, "j", "ok", "y=ok", tracks=["y"],
    ...               description="body", scores={"resume_fit_score": 0.5})
    >>> r = conn.execute("SELECT * FROM jobs").fetchone()
    >>> (r["triage_status"], r["triage_detail"], sorted(track_set(r["track"])),
    ...  r["description"], r["resume_fit_score"])
    ('ok', 'y=ok', ['x', 'y'], 'body', 0.5)
    """
    sets = ["triage_status=?", "triage_detail=?", "triaged_at=?"]
    args = [status, detail, (now or datetime.now()).isoformat()]
    if tracks:
        prev = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                            (job_id,)).fetchone()
        merged = track_set(prev["track"] if prev else None) | set(tracks)
        sets.append("track=?")
        args.append(join_tracks(merged))
    if description:
        sets.append("description=COALESCE(NULLIF(description,''), ?)")
        args.append(description[:config.MAX_DESC_CHARS])
    if geo_mode:
        sets.append("geo_mode=COALESCE(geo_mode, ?)")
        args.append(geo_mode)
    if remote_signal:
        sets.append("remote_eligible=1")
        sets.append("remote_signal=COALESCE(remote_signal, ?)")
        args.append(remote_signal)
    if scores:
        for c in _SCORE_COLS:
            sets.append(f"{c}=?")
            args.append(scores.get(c))
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?",
                 [*args, job_id])
    _commit(conn)


def store_body(conn, job_id, description, location=None):
    """Keep a freshly fetched body (and, when the detail page named one,
    the real location) on a row whose verdict is still open, so the next
    pass does not fetch it again. An empty body never blanks a stored one.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T",
    ...                       "location": "2 Locations"})
    >>> store_body(conn, "j", "body", "Durham, NC; Remote")
    >>> r = conn.execute("SELECT description, location FROM jobs").fetchone()
    >>> (r["description"], r["location"])
    ('body', 'Durham, NC; Remote')
    """
    conn.execute(
        "UPDATE jobs SET description=COALESCE(NULLIF(?,''), description), "
        "location=COALESCE(NULLIF(?,''), location) WHERE job_id=?",
        ((description or "")[:config.MAX_DESC_CHARS], location or "", job_id))
    _commit(conn)


def mark_desc_checked(conn, job_id, now=None):
    """Stamp a failed body fetch so the next pass does not retry it at once
    (the same desc_checked_at the backfill ops honour)."""
    conn.execute("UPDATE jobs SET desc_checked_at=? WHERE job_id=?",
                 ((now or datetime.now()).isoformat(), job_id))
    _commit(conn)


def triage_counts(conn, days=None):
    """{verdict: n} over triaged rows, optionally only those judged in the
    last `days` days -- the per-gate funnel the digest shows.

    >>> conn = connect(":memory:")
    >>> for jid, st in [("a", "ok"), ("b", "title"), ("c", "title")]:
    ...     _ = upsert_job(conn, {"job_id": jid, "title": "T"})
    ...     record_triage(conn, jid, st, "")
    >>> triage_counts(conn)
    {'ok': 1, 'title': 2}
    """
    q = ("SELECT triage_status AS s, COUNT(*) AS n FROM jobs "
         "WHERE triage_status IS NOT NULL")
    args = []
    if days:
        q += " AND triaged_at >= ?"
        args.append((datetime.now() - timedelta(days=days)).isoformat())
    q += " GROUP BY triage_status"
    rows = conn.execute(q, args).fetchall()
    order = (TRIAGE_OK, *TRIAGE_GATES)
    return {r["s"]: r["n"] for r in sorted(
        rows, key=lambda r: order.index(r["s"]) if r["s"] in order
        else len(order))}


def upsert_job(conn, j):
    """Insert or refresh a job. Returns True if it was new.

    `first_seen` stays stable across re-runs; scores refresh so the stored
    values always reflect the latest scorer.
    """
    now = datetime.now().isoformat()
    new = not job_exists(conn, j["job_id"])
    if new and j.get("url"):
        # Same posting arriving under a NEW id scheme — a company's ats/
        # tenant changed (Keebler custom_* -> rippling_*) or a fetcher's id
        # format did (Duke sf__<slug> -> sf_<tenant>_<num>). Re-key the
        # existing row instead of inserting a duplicate: dupes double-rank
        # and double-spend deep-verify (17 such URL pairs in the 2026-08-28
        # store). Title must match too — some custom boards give several
        # DISTINCT postings one landing URL, and those must stay separate
        # rows.
        prev = conn.execute(
            "SELECT job_id, title FROM jobs WHERE url=?",
            (j["url"],)).fetchone()
        if (prev is not None
                and (prev["title"] or "").strip().lower()
                == (j.get("title") or "").strip().lower()):
            conn.execute("UPDATE jobs SET job_id=? WHERE job_id=?",
                         (j["job_id"], prev["job_id"]))
            new = False
    remote = j.get("remote_eligible")
    if remote is not None:
        remote = int(bool(remote))
    # `track` is a SET (see track_set): a posting can legitimately belong to
    # several tracks at once — a neural-company job in your area is both
    # local and neural material — so a second track's crawl ADDS its label
    # instead of stealing the row. Merged here in Python; the ON CONFLICT
    # clause below just writes the union.
    track = j.get("track")
    if track and not new:
        prev = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                            (j["job_id"],)).fetchone()
        track = join_tracks(track_set(prev["track"] if prev else None)
                            | track_set(track))
    conn.execute(
        """INSERT INTO jobs
            (job_id, company_id, company_name, title, url, location, track,
             geo_mode, remote_eligible, remote_signal, anchor_signal,
             description, resume_fit_score, fit_reason,
             fit_domain, fit_function, fit_stack, fit_seniority, fit_gates,
             fit_model, posted_at, first_seen, last_seen, status,
             harvested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, location=excluded.location,
             track=COALESCE(excluded.track, track),
             geo_mode=COALESCE(excluded.geo_mode, geo_mode),
             remote_eligible=COALESCE(excluded.remote_eligible, remote_eligible),
             remote_signal=COALESCE(excluded.remote_signal, remote_signal),
             anchor_signal=COALESCE(excluded.anchor_signal, anchor_signal),
             description=COALESCE(NULLIF(excluded.description,''), description),
             resume_fit_score=COALESCE(excluded.resume_fit_score, resume_fit_score),
             fit_reason=COALESCE(NULLIF(excluded.fit_reason,''), fit_reason),
             fit_domain=COALESCE(excluded.fit_domain, fit_domain),
             fit_function=COALESCE(excluded.fit_function, fit_function),
             fit_stack=COALESCE(excluded.fit_stack, fit_stack),
             fit_seniority=COALESCE(excluded.fit_seniority, fit_seniority),
             fit_gates=COALESCE(excluded.fit_gates, fit_gates),
             fit_model=COALESCE(excluded.fit_model, fit_model),
             posted_at=COALESCE(posted_at, excluded.posted_at),
             last_seen=excluded.last_seen,
             status=excluded.status,
             closed_at=CASE WHEN excluded.status='closed'
                            THEN closed_at ELSE NULL END,
             harvested_at=COALESCE(excluded.harvested_at, harvested_at)""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), track, j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("anchor_signal"),
         j.get("description"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         j.get("fit_domain"), j.get("fit_function"), j.get("fit_stack"),
         j.get("fit_seniority"), j.get("fit_gates"), j.get("fit_model"),
         j.get("posted_at"), now, now, j.get("status", "open"),
         j.get("harvested_at")),
    )
    _commit(conn)
    return new


# --------------------------------------------------------------------------- #
#  Job status sync, score columns, ranking                                     #
# --------------------------------------------------------------------------- #

def _norm_title(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _norm_url(u):
    """Scheme/query/fragment/trailing-slash-insensitive URL key."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")



def touch_job(conn, job_id):
    """Record that a job was just observed live at its source — reopen it and
    refresh last_seen, touching nothing else. For dedupe paths that skip the
    full upsert (e.g. a re-captured LinkedIn card already in the store): the
    sighting must still reset the closed flag and the external-row grace
    clock (see sync_job_statuses), or the next board sync could re-close a
    posting the user just saw live."""
    conn.execute(
        "UPDATE jobs SET status='open', closed_at=NULL, last_seen=? "
        "WHERE job_id=?", (datetime.now().isoformat(), job_id))
    conn.commit()


def sync_job_statuses(conn, company_id, fetched_jobs, track=None,
                      external_grace_days=3):
    """Reconcile ONE company's stored jobs against a live board snapshot
    (`fetched_jobs`: dicts with id/title/url, as returned by
    fetchers.company.fetch_company). Rows matched by job_id, URL, or
    normalized title are (re)marked open and their last_seen touched; rows
    that have vanished from the snapshot are marked closed. Returns
    (n_reopened, n_closed).

    Caller contract: only pass a snapshot from a SUCCESSFUL, non-empty fetch
    — fetchers soft-fail to [] (HTTP 404, non-JSON), which is
    indistinguishable from a genuinely emptied board, so an empty snapshot
    must never close anything (this function no-ops on one).

    Matching depends on where the row's job_id came from:
      * BOARD-NATIVE rows — job_id in the snapshot's own id namespace (same
        "<ats>_<key>_" prefix as some snapshot id) — match by EXACT id only.
        The board is authoritative for its own ids, and boards recycle
        titles across requisitions (Beacon reposts "Algorithm Engineer"
        under a fresh Greenhouse id every cycle), so a title/URL fallback
        would let one live posting shield every dead same-titled req from
        ever closing. Absent id -> closed immediately. A partial fetch
        (e.g. Workday pagination dying mid-board) can close rows
        spuriously, but the next full fetch reopens them (matched rows
        always flip back).
      * EXTERNAL rows (LinkedIn captures, NLx, manual --add, legacy ids
        from a retired fetcher) can never id-match, so they match by
        normalized URL or title instead, and are closed only after
        `external_grace_days` without being seen — a manual --add isn't
        insta-closed just because its title doesn't exactly match a board
        row.
      * When `track` is given, only rows of that track are ever CLOSED
        (matched rows are reopened regardless — they're live on the board).
    """
    if not company_id or not fetched_jobs:
        return (0, 0)
    ids = {j.get("id") for j in fetched_jobs if j.get("id")}
    urls = {u for u in (_norm_url(j.get("url")) for j in fetched_jobs) if u}
    titles = {t for t in (_norm_title(j.get("title")) for j in fetched_jobs) if t}
    # Posting dates piggyback on the sync: every matched row gets its NULL
    # posted_at backfilled from the live snapshot, so the whole store gains
    # real posting dates over normal crawls with zero extra HTTP.
    posted = {}
    for j in fetched_jobs:
        p = j.get("posted_at")
        if not p:
            continue
        for key in (j.get("id"), _norm_url(j.get("url")), _norm_title(j.get("title"))):
            if key:
                posted.setdefault(key, p)
    # "gh_<slug>_123" -> "gh_<slug>_": the id namespace(s) this snapshot
    # covers. First TWO tokens, not rsplit — the per-job tail may itself
    # carry underscores ("wd_amgen_<Title-Slug>_R-250290"). Single-token-tail
    # ids ("custom_<blob>") degrade to a full-id prefix, i.e. those rows only
    # ever close via the grace path — right for the flakiest scraped boards.
    prefixes = {"_".join(i.split("_", 2)[:2]) + "_" for i in ids if "_" in i}
    now = datetime.now().isoformat()
    grace_cutoff = (datetime.now()
                    - timedelta(days=external_grace_days)).isoformat()
    n_reopened = n_closed = 0
    rows = conn.execute(
        "SELECT job_id, url, title, track, status, first_seen, last_seen "
        "FROM jobs WHERE company_id=?", (company_id,)).fetchall()
    for r in rows:
        board_native = any(r["job_id"].startswith(p) for p in prefixes)
        present = (r["job_id"] in ids
                   or (not board_native
                       and (_norm_url(r["url"]) in urls
                            or _norm_title(r["title"]) in titles)))
        if present:
            if (r["status"] or "open") != "open":
                n_reopened += 1
            p = (posted.get(r["job_id"]) or posted.get(_norm_url(r["url"]))
                 or posted.get(_norm_title(r["title"])))
            conn.execute(
                "UPDATE jobs SET status='open', closed_at=NULL, last_seen=?, "
                "posted_at=COALESCE(posted_at, ?) WHERE job_id=?",
                (now, p, r["job_id"]))
            continue
        if track is not None and track not in track_set(r["track"]):
            continue
        if (r["status"] or "open") == "closed":
            continue
        seen = r["last_seen"] or r["first_seen"] or ""
        if board_native or seen < grace_cutoff:   # ISO strings sort by time
            conn.execute(
                "UPDATE jobs SET status='closed', closed_at=? WHERE job_id=?",
                (now, r["job_id"]))
            n_closed += 1
    _commit(conn)
    return (n_reopened, n_closed)


# Fit columns written together by the rescore path (see update_job_scores).
_SCORE_COLS = ("resume_fit_score", "fit_reason", "fit_gates", "fit_model",
               "fit_domain", "fit_function", "fit_stack", "fit_seniority")


def update_job_scores(conn, job_id, cols):
    """Overwrite only the fit columns for one job (used by rescore). `cols` is a
    FitResult.as_columns() dict; any missing key is written NULL, so passing an
    empty/partial dict clears a stale score (an unscorable row drops out of
    ranking)."""
    sets = ", ".join(f"{c}=?" for c in _SCORE_COLS)
    conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                 [cols.get(c) for c in _SCORE_COLS] + [job_id])
    conn.commit()


# Matches the fit_reason tag summary() writes: "[dom0.45 fun0.72 sta0.55
# sen0.80 gate:geo+embedded] reason". Gates are '+'-joined in the tag.
_AXIS_TAG = re.compile(
    r"\[dom([\d.]+) fun([\d.]+) sta([\d.]+) sen([\d.]+)(?: gate:([^\]]+))?\]")


def backfill_axis_columns(conn):
    """Populate the per-axis columns (fit_domain/function/stack/seniority,
    fit_gates) from the tag already embedded in fit_reason. Offline, no API.
    Only touches rows that have the tag and a NULL fit_domain, and leaves
    resume_fit_score / fit_reason untouched. Rows with no tag ('no
    description; unscored', or old single-scalar reasons) are skipped."""
    rows = conn.execute(
        "SELECT job_id, fit_reason FROM jobs "
        "WHERE fit_domain IS NULL AND fit_reason LIKE '[dom%'"
    ).fetchall()
    n = 0
    for r in rows:
        m = _AXIS_TAG.match(r["fit_reason"] or "")
        if not m:
            continue
        dom, fun, sta, sen, gates = m.groups()
        conn.execute(
            "UPDATE jobs SET fit_domain=?, fit_function=?, fit_stack=?, "
            "fit_seniority=?, fit_gates=? WHERE job_id=?",
            (float(dom), float(fun), float(sta), float(sen),
             (gates.replace("+", ",") if gates else None), r["job_id"]),
        )
        n += 1
    conn.commit()
    print(f"  {n} of {len(rows)} row(s) backfilled from fit_reason tags.")
    return n


def remote_admitted(row, remote_mission_floor):
    """Whether an out-of-area REMOTE `row` (a ranked_jobs row) is still
    worth showing in a location-scoped view.

    A watched company qualifies whatever it scores — watch is the one
    human-curated tag, "show me everything at this employer":

    >>> remote_admitted({"company_tags": "local,watch",
    ...                  "mission_score": 0.05}, 0.85)
    True

    Any other company has to reach `remote_mission_floor` on its own
    judged mission score:

    >>> remote_admitted({"mission_score": 0.9}, 0.85)
    True
    >>> remote_admitted({"mission_score": 0.5}, 0.85)
    False

    A company nobody has scored is not admitted (unknown is not a verdict
    in its favour), and a floor of None turns the score arm off entirely,
    leaving watch as the only way in:

    >>> remote_admitted({"mission_score": None}, 0.85)
    False
    >>> remote_admitted({"mission_score": 0.99}, None)
    False

    A multi-division conglomerate never qualifies on score — see
    tests/test_store.py::TestRemoteAdmission, which patches the profile
    policy the check reads.

    Notes:
        The watch list is hand-maintained and lags the data: 8 starred
        companies produced a third of all good-fit rows while 20 unstarred
        ones had produced at least one, and a remote research-engineer
        posting at fit 0.94 fell out of a location-scoped ranking purely
        for want of a star. ranked_jobs applies this server-side; the web
        UI re-applies it per row, because /api/jobs deliberately ships
        every row and gates on the client.
    """
    if tags.has(row.get("company_tags"), tags.WATCH):
        return True
    if remote_mission_floor is None:
        return False
    if config.is_multi_division(row.get("company_name")):
        return False
    mission = row.get("mission_score")
    return mission is not None and mission >= remote_mission_floor


def ranked_jobs(conn, track=None, limit=None, location_re=None, rank_by="combined",
                allow_geo_modes=None, min_mission=None,
                remote_mission_floor=None, include_closed=False,
                include_dispositioned=False):
    """Jobs joined to company mission. `rank_by="combined"` (default) sorts by
    sqrt(resume_fit * company_mission); `rank_by="fit"` sorts by the résumé-fit
    score alone. Use "fit" for a market where every company shares one mission
    tier (e.g. the local health-tech track), so the near-constant mission
    factor doesn't inflate and compress the ranking. `combined_score` is still
    computed either way, so callers can display it. Jobs missing the ranking
    factor fall to the bottom, ordered among themselves by whatever they have.

    `location_re` (a compiled regex) enforces geography at query time,
    independent of the `track` label: a job whose stored location doesn't
    match is excluded from this search but stays in the shared table. This
    is how the local track keeps out-of-area postings out of its results no
    matter which ingest path stamped them `local-tech`.

    `allow_geo_modes` (an iterable of stored `geo_mode` values, e.g.
    {"remote"}) admits rows that fail `location_re` but whose own geo_mode
    already qualifies them — ONLY at companies `remote_admitted` (above)
    trusts with the exception: the watch list, or a mission score at or
    above `remote_mission_floor`. The machine-set sweep tag never earns it —
    auto-probed boards include slug collisions (an EEG company's row that
    actually points at a global AI board), and an unscoped geo_mode
    exception let 87 remote-anywhere rows into a 534-row local ranking.

    `min_mission` drops jobs at companies we positively know are off-mission
    (effective mission below the floor). Needed when ranking by "fit", which
    ignores the mission factor entirely: an off-mission employer's senior ML
    role can otherwise out-rank on-mission work on function/seniority alone
    (a games studio's rec-sys job at fit 0.40 / mission 0.03). Rows with NO
    mission score — unlinked or unscored companies — are KEPT, so the floor
    only removes what has been judged, never what is merely unknown. The
    multi-division floor is applied first, so a conglomerate's keyword-vetted
    job isn't dropped for its parent's low corporate score.

    Jobs marked closed (status='closed' — vanished from their company's
    board, or probed dead; see sync_job_statuses) are excluded unless
    `include_closed=True`. Jobs the user has dispositioned also leave the
    ranking — applied/interviewing live in the digest's pipeline section,
    rejected/dismissed disappear — except 'saved' (shortlisted), which
    stays visible."""
    q = """
      SELECT j.*, c.mission_tier, c.mission_score, c.tags AS company_tags
      FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
    """
    conds, args = [], []
    if track:
        conds.append(_TRACK_MATCH_SQL)
        args.append(_track_match_arg(track))
    if not include_closed:
        conds.append("COALESCE(j.status,'open') != 'closed'")
    if not include_dispositioned:
        # Deferred, like pipeline.py's one reach back here, so neither
        # module depends on the other at load time (same rule as
        # companies/review). Ranking hides what a person ruled on.
        from .pipeline import RANKING_EXCLUDED_DISPOSITIONS
        ph = ",".join("?" for _ in RANKING_EXCLUDED_DISPOSITIONS)
        conds.append(f"(j.disposition IS NULL OR j.disposition NOT IN ({ph}))")
        args += list(RANKING_EXCLUDED_DISPOSITIONS)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    if location_re is not None:
        rows = [r for r in rows
                if location_re.search(r.get("location") or "")
                or (allow_geo_modes and r.get("geo_mode") in allow_geo_modes
                    and remote_admitted(r, remote_mission_floor))]
    def _effective_mission(r):
        # A conglomerate's own mission score is ~0.05 (off-mission overall),
        # but a job here already passed the health keyword filter at crawl
        # time — so rank it at the keyword-vetted floor, not the company's
        # score, or its combined rank would be sunk unfairly.
        mission = r.get("mission_score")
        if config.is_multi_division(r.get("company_name")):
            mission = max(mission or 0.0, config.MULTI_DIVISION_MISSION_FLOOR)
        return mission

    for r in rows:
        r["combined_score"] = combined_score(r.get("resume_fit_score"),
                                             _effective_mission(r))
    if min_mission is not None:
        rows = [r for r in rows
                if (m := _effective_mission(r)) is None or m >= min_mission]
    # Primary sort key per rank_by, then the other factors as tiebreaks; None
    # sorts last via the -1 sentinel (all real scores are >= 0).
    primary = "resume_fit_score" if rank_by == "fit" else "combined_score"
    def _k(r):
        vals = (r.get(primary), r.get("combined_score"),
                r.get("resume_fit_score"), r.get("mission_score"))
        return tuple(v if v is not None else -1.0 for v in vals)
    rows.sort(key=_k, reverse=True)
    if limit:
        rows = rows[:int(limit)]
    return rows


def dedup_jobs(conn):
    """Collapse job rows that are the SAME posting under different ids: same
    company, same URL modulo scheme/query/fragment (_norm_url), same
    normalized title. upsert_job's re-key only catches an EXACT URL match,
    so a fetcher that emitted the same posting with a different query string
    (iCIMS `?in_iframe=1` vs `?hub=9&in_iframe=1`, 12 SAS pairs in the
    2026-09-01 store) under a second id namespace slipped past it, and the
    pair then double-ranked and double-spent deep-verify.

    Two guards keep this from eating distinct postings. Title must match —
    some custom boards give several DISTINCT postings one landing URL (see
    upsert_job). And the ids' per-posting tail (the board's own requisition
    number, "..._42453") must match too: Greenhouse companies whose stored
    URL is a shared careers landing page (butterflynetwork.com/careers?
    gh_jid=N) reduce to one URL for every job, and a title reposted under a
    fresh requisition (a second office, a re-opened req) is a separate
    posting, not a duplicate — the dry run without this guard would have
    merged three such Butterfly Network pairs.

    Keeps, per group: a dispositioned row over an undispositioned one, then
    an open row over a closed one, then the earliest first_seen (the row
    whose history is longest). Returns the number of rows deleted."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in conn.execute(
            "SELECT job_id, company_id, url, title, disposition, status, "
            "first_seen FROM jobs WHERE company_id IS NOT NULL "
            "AND url IS NOT NULL AND url != ''"):
        key = (r["company_id"], _norm_url(r["url"]), _norm_title(r["title"]),
               r["job_id"].rsplit("_", 1)[-1])
        if key[1] and key[2]:
            groups[key].append(dict(r))

    def keep_rank(r):
        return (r.get("disposition") is not None,
                (r.get("status") or "open") == "open",
                # earliest first: ISO strings sort by time, so negate via
                # tuple ordering by sorting descending on the inverse
                "" if not r.get("first_seen") else r["first_seen"])

    deleted = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Highest disposition/open rank wins; among equals the EARLIEST
        # first_seen (min) wins, so sort ascending on first_seen and
        # descending on the two flags.
        members.sort(key=lambda r: (not keep_rank(r)[0], not keep_rank(r)[1],
                                    keep_rank(r)[2]))
        keep, losers = members[0], members[1:]
        for l in losers:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (l["job_id"],))
        deleted += len(losers)
        print(f"    {(keep['title'] or '')[:40]:40} kept {keep['job_id'][:28]}"
              f" <- dropped {', '.join(l['job_id'][:28] for l in losers)}")
    conn.commit()
    return deleted
