"""What the store-maintenance ops share (src/ops/__init__.py lists the
ops), and the per-company gate and score the manual add uses.

These grew up inside the local track's module but were never
local-specific: every helper takes the track config `t` (None = the default
local-engine track) and derives the store, jobs.track value, gates and
ranking knobs from it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator, Callable, Generator, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Literal, cast, overload

from src import config
from src import digest
from src.digest.render import score_text
from src import store
from src.ats.board import company as company_fetch
from src.claude.fit import score_resume_fit
from src.config import RuntimeTrack
from src.match import gates
from src.match.locality import (NC_RE, geo_label, geo_mode, is_nc, location_unknown,
                                remote_signal, us_eligible)
from src.net.http import fetch_failed
from src.rows import CompanyRow, FetchedJob, JobIn, RankedJob, is_watched

if TYPE_CHECKING:
    from sqlite3 import Connection


def _t(t: RuntimeTrack | None) -> RuntimeTrack:
    return t if t is not None else config.track_for_engine("local")


@contextmanager
def track_store(t: RuntimeTrack | None = None, conn: sqlite3.Connection | None = None
                ) -> Generator[sqlite3.Connection]:
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
    conn = store.connect(_t(t).db_path)
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def track_writer(t: RuntimeTrack | None = None, db: store.Writer | Connection | None = None
                       ) -> AsyncGenerator[store.Writer]:
    """`track_store` for async code: the track's store on a store.Writer
    for the block -- or `db` itself when the caller already holds a Writer
    (the crawl, the harvest pass), or a Writer over `db` when it is an open
    connection, which stays open.

    Notes:
        Its annotations name sqlite3's Connection unqualified: the
        blocking-name check (tests/test_invariants.py) reads an async
        def's signature too, and a type is not a call.
    """
    if isinstance(db, store.Writer):
        yield db
        return
    async with store.Writer(_t(t).db_path if db is None else db) as w:
        yield w


@overload
def group_by_company[T: Mapping[str, object]](rows: Iterable[T], key: Literal["company_id"] = "company_id",
                                              alone: str | None = None) -> dict[int, list[T]]: ...
@overload
def group_by_company[T: Mapping[str, object]](rows: Iterable[T], key: str,
                                              alone: str | None = None) -> dict[str, list[T]]: ...
def group_by_company[T: Mapping[str, object]](rows: Iterable[T], key: str = "company_id",
                                              alone: str | None = None
                                              ) -> dict[int, list[T]] | dict[str, list[T]]:
    """`rows` bucketed by `key` (their company id by default), in
    first-seen order. With `alone` naming an integer field, a row whose key
    is falsy (a feed's, with no company) stands alone under that field negated.

    >>> group_by_company([{"company_id": 1, "t": "a"}, {"company_id": 2},
    ...                   {"company_id": 1, "t": "b"}])
    {1: [{'company_id': 1, 't': 'a'}, {'company_id': 1, 't': 'b'}], 2: [{'company_id': 2}]}
    >>> list(group_by_company([{"company_id": None, "id": 7}, {"company_id": 3, "id": 8}], alone="id"))
    [-7, 3]

    Both backfill paths need this: a board with several stale rows must be
    fetched once, not once per row.
    """
    out: dict[object, list[T]] = {}
    for r in rows:
        k = r[key] or (-cast(int, r[alone]) if alone else r[key])
        out.setdefault(k, []).append(r)
    return cast(dict[int, list[T]], out)


async def board_index(company: CompanyRow) -> dict[str, FetchedJob]:
    """One company's whole board, indexed by normalised title.

    Empty when the board cannot be pulled -- which is the same outcome as a
    board that lists nothing matching, so callers fall through to their
    per-URL path either way.
    """
    try:
        board = await company_fetch.fetch_company(company, loc_re=None, employer=company["name"])
    except Exception as e:                      # noqa: BLE001 - reported
        fetch_failed(f"{company['name']}: board fetch failed", e)
        return {}
    return {(b.get("title") or "").strip().lower(): b for b in board}


async def board_match(index: dict[str, FetchedJob], title: str | None,
                      tried: str | None = None) -> FetchedJob | None:
    """The board row for `title`, hydrated, or None when the board does not
    cover it (or covers it with no body). A bodiless row at `tried`, a URL
    the caller just failed to hydrate, is not fetched again.

    >>> import asyncio
    >>> idx = {"t": {"title": "T", "url": "https://x/1", "description": ""}}
    >>> asyncio.run(board_match(idx, "T", tried="https://x/1")) is None
    True

    The pair above plus this is the whole of "get a stored row's text back
    from its company's own board", which the description backfill and the
    external-ingest hydrator each used to spell out in full -- with
    different error text and, until this, no shared notion of what counts
    as a title match.
    """
    match = index.get((title or "").strip().lower())
    if match is None or (tried and match.get("url") == tried
                         and not match.get("description")):
        return None
    await company_fetch.hydrate_description(match)
    return match if match.get("description") else None


def _ranked(conn: sqlite3.Connection, t: RuntimeTrack, limit: int | None = None,
            with_description: bool = False) -> list[RankedJob]:
    """The track's ranked view — same knobs the crawl digest uses.
    `with_description` is for deep verify, which falls back on the stored
    body when the live page cannot be fetched.

    >>> from pathlib import Path
    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_job(conn, {"job_id": "j1", "title": "T", "track": "local-tech",
    ...                             "location": "Durham, NC", "description": "the body", "resume_fit_score": 0.5})
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), track="local-tech", geo_gate=False)
    >>> "description" in _ranked(conn, t)[0]
    False
    >>> _ranked(conn, t, with_description=True)[0]["description"]
    'the body'
    """
    return store.ranked_jobs(
        conn, track=t.track,
        location_re=(NC_RE if t.geo_gate else None),
        rank_by=t.rank_by, allow_geo_modes={"remote"},
        min_mission=t.min_mission,
        remote_mission_floor=t.remote_mission_floor, limit=limit,
        with_description=with_description)


def new_watch_hits(conn: sqlite3.Connection, t: RuntimeTrack, since: str
                   ) -> list[tuple[CompanyRow, FetchedJob, bool]]:
    """The digest's watch section: open postings at watched companies first
    seen since `since` (an ISO time; callers pass the start of
    config.WATCH_WINDOW_DAYS) that clear the title gate, the track's
    exclude gate and `us_eligible`, as (company, job, in_ranking) -- the flag
    says the row carries `t`'s label; the job carries its `first_seen`.

    >>> from pathlib import Path
    >>> conn = store.connect(":memory:")
    >>> cid = store.upsert_company(conn, {"name": "Acme", "ats": "lever",
    ...                                   "slug": "a", "watch": 1})
    >>> for jid, loc in [("a", "Durham, NC"), ("b", "Tel Aviv, Israel")]:
    ...     _ = store.upsert_job(conn, {"job_id": jid, "company_id": cid,
    ...                                 "company_name": "Acme", "title": "Engineer",
    ...                                 "location": loc})
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), track="t", exclude_gate=False,
    ...                  tech_title_regex="engineer")
    >>> [(c["name"], j["location"], seen) for c, j, seen in
    ...  new_watch_hits(conn, t, "2000-01-01")]
    [('Acme', 'Durham, NC', False)]
    >>> new_watch_hits(conn, t, "2999-01-01")
    []
    """
    hits: list[tuple[CompanyRow, FetchedJob, bool]] = []
    companies: dict[int, CompanyRow | None] = {}
    for r in conn.execute(
            "SELECT j.job_id, j.title, j.url, j.location, j.description, j.track, "
            "j.company_id, j.first_seen FROM open_jobs j JOIN companies_effective c "
            "ON c.id = j.company_id WHERE c.watch = 1 AND j.first_seen >= ? "
            "ORDER BY j.company_name, j.first_seen", (since,)):
        title, desc = r["title"] or "", r["description"] or ""
        if (not us_eligible(r["location"] or "")
                or not gates.is_technical_role(title, t)
                or (t.exclude_gate and gates.exclude_reason(
                    title, desc, allow_defense=True, track_id=t.id))):
            continue
        if r["company_id"] not in companies:
            companies[r["company_id"]] = store.get_company(conn, r["company_id"])
        company = companies[r["company_id"]]
        if company is None:
            continue
        hits.append((company, {"id": r["job_id"], "title": title, "url": r["url"] or "",
                               "location": r["location"] or "",
                               "first_seen": r["first_seen"] or ""},
                     t.track in store.track_set(r["track"])))
    return hits


def rewrite_digest(conn: sqlite3.Connection, t: RuntimeTrack, top_n: int = 15,
                   heading: str | Callable[[int], str] = "",
                   watch_hits: list[tuple[CompanyRow, FetchedJob, bool]] | None = None
                   ) -> list[RankedJob]:
    """Rewrite the track's ranked digest from the store as it stands now
    (harvest triage funnel and `watch_hits` included), and print `heading`
    (a callable is given the ranked count) and the top `top_n` of it.
    Returns the ranked list.

    The tail of every op that changes what the ranking contains -- the
    status sync and the standalone deep verify both ended with their own
    copy, and the copies had already drifted apart in what they printed.
    """
    ranked = _ranked(conn, t)
    digest.write_ranked_digest(
        ranked, t, watch_hits=watch_hits, pipeline=store.get_pipeline(conn),
        followups=store.followups_due(conn), triage=store.triage_counts(conn, days=config.WATCH_WINDOW_DAYS))
    if callable(heading):
        heading = heading(len(ranked))
    if heading:
        print(heading)
    for j in ranked[:top_n]:
        print(f"  fit={score_text(j['resume_fit_score'])} [{geo_label(j)}] {(j['title'] or '')[:52]}"
              f"  -  {j['company_name']}")
    return ranked


# --------------------------------------------------------------------------- #
#  Company-row helpers (store roster semantics, shared by crawl + ops).        #
#  The watch flag is rows.is_watched(company); scope tags are tags.has(...).   #
# --------------------------------------------------------------------------- #

def remote_trusted(company: CompanyRow | None, floor: float | None) -> bool:
    """True if the geo gate admits a company's remote postings: it is on
    the watch list, or `_mission_trusted` at `floor`."""
    return is_watched(company) or _mission_trusted(company, floor)


def remote_us_geo_drops(conn: sqlite3.Connection, floors: Iterable[float | None], *,
                        trusted: bool) -> Iterator[tuple[sqlite3.Row, CompanyRow | None]]:
    """(row, company) for every open row dropped at the geo gate whose
    location reads remote and US-eligible, at the companies the gate trusts
    for remote at any of `floors` (`remote_trusted`) when `trusted`, else at
    the ones it does not. The company is checked first, once per company:
    it rules out most of the table before any location regex runs."""
    owners: dict[int | None, tuple[CompanyRow | None, bool]] = {}
    for r in conn.execute("SELECT job_id, company_id, company_name, title, location "
                          "FROM open_jobs WHERE triage_status='geo' AND disposition IS NULL"):
        if r["company_id"] not in owners:
            co = store.get_company(conn, r["company_id"])
            owners[r["company_id"]] = (co, co is not None
                                       and any(remote_trusted(co, f) for f in floors))
        co, ok = owners[r["company_id"]]
        loc = r["location"]
        if (ok == trusted and not location_unknown(loc) and not is_nc(loc)
                and remote_signal(loc) and us_eligible(loc)):
            yield r, co


def _mission_trusted(company: CompanyRow | None, floor: float | None) -> bool:
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
        the job-shaped fields that rule reads; its watch flag is withheld
        because callers test the watch half themselves.
    """
    if not company:
        return False
    return store.remote_admitted(
        {"company_name": company.get("name"),
         "mission_score": company.get("mission_score")}, floor)


async def _scored_row(job: FetchedJob, *, company_id: int | None, company_name: str | None,
                      track: str, status: str | None = None) -> JobIn:
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
    res = await score_resume_fit(job["title"], job.get("description", ""),
                                 location=job.get("location") or "")
    row: JobIn = {
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


# The miss-reason family status.check_closed_jobs closes on -- the one of
# the two RERESOLVE_FAMILIES (repair.py builds them from this name) that
# means "a board WAS here": "no-board-found" never had a working board in
# the first place, so it cannot own OPEN job rows to close.
_DEAD_BOARD_FAMILY = "board-dead"
