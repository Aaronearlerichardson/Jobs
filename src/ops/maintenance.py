"""What the store-maintenance ops share (src/ops/__init__.py lists the
ops), and the crawl's per-company gate and score, which the runner, triage
and the manual add all use.

These grew up inside the local track's module but were never
local-specific: every helper takes the track config `t` (None = the default
local-engine track) and derives the store, jobs.track value, gates and
ranking knobs from it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src import digest
from src import store
from src import tags
from src.ats.board import company as company_fetch
from src.claude.fit import score_resume_fit
from src.config import RuntimeTrack
from src.match import gates
from src.match.filters import is_relevant
from src.match.locality import (NC_RE, geo_label, geo_mode, is_nc, location_unknown,
                                remote_signal, us_eligible)
from src.net.http import fetch_failed
from src.rows import CompanyRow, FetchedJob, JobIn, JobRow, RankedJob, is_watched

if TYPE_CHECKING:
    from sqlite3 import Connection


def _t(t: RuntimeTrack | None) -> RuntimeTrack:
    return t if t is not None else config.track_for_engine("local")


@contextmanager
def track_store(t: RuntimeTrack | None = None, conn: sqlite3.Connection | None = None
                ) -> Iterator[sqlite3.Connection]:
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
                       ) -> AsyncIterator[store.Writer]:
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


def group_by_company[T: Mapping[str, Any]](rows: Iterable[T], key: str = "company_id") -> dict[Any, list[T]]:
    """`rows` bucketed by `key` (their company id by default), in
    first-seen order.

    >>> group_by_company([{"company_id": 1, "t": "a"}, {"company_id": 2},
    ...                   {"company_id": 1, "t": "b"}])
    {1: [{'company_id': 1, 't': 'a'}, {'company_id': 1, 't': 'b'}], 2: [{'company_id': 2}]}

    Both backfill paths need this: a board with several stale rows must be
    fetched once, not once per row.
    """
    out: dict[Any, list[T]] = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


async def board_index(company: CompanyRow) -> dict[str, FetchedJob]:
    """One company's whole board, indexed by normalised title.

    Empty when the board cannot be pulled -- which is the same outcome as a
    board that lists nothing matching, so callers fall through to their
    per-URL path either way.
    """
    try:
        board = await company_fetch.fetch_company(company, loc_re=None)
    except Exception as e:                      # noqa: BLE001 - reported
        fetch_failed(f"{company['name']}: board fetch failed", e)
        return {}
    return {(b.get("title") or "").strip().lower(): b for b in board}


async def board_match(index: dict[str, FetchedJob], title: str | None) -> FetchedJob | None:
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
    await company_fetch.hydrate_description(match)
    return match if match.get("description") else None


def _ranked(conn: sqlite3.Connection, t: RuntimeTrack, limit: int | None = None,
            with_description: bool = False) -> list[RankedJob]:
    """The track's ranked view — same knobs the crawl digest uses.
    `with_description` is for deep verify, which falls back on the stored
    body when the live page cannot be fetched.

    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_job(conn, {"job_id": "j1", "title": "T", "track": "local-tech",
    ...                             "location": "Durham, NC", "description": "the body", "resume_fit_score": 0.5})
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), track="local-tech")
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


def _write_digest(conn: sqlite3.Connection, t: RuntimeTrack,
                  watch_hits: list[tuple[CompanyRow, FetchedJob, bool]] | None = None
                  ) -> tuple[list[RankedJob], list[JobRow], list[JobRow], Path]:
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


def rewrite_digest(conn: sqlite3.Connection, t: RuntimeTrack, top_n: int = 15,
                   heading: str = "") -> list[RankedJob]:
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
        print(f"  fit={fs} [{geo_label(j)}] {(j['title'] or '')[:52]}"
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


def _whole_board(company: CompanyRow, mission_floor: float | None = None) -> bool:
    """Whether a company's ENTIRE board is fetched, with no location filter.

    Either scope tag qualifies on its own — a sweep board is cheap to pull
    whole, a watched one must never miss a posting:

    >>> _whole_board({"name": "Acme", "watch": 1})
    True
    >>> _whole_board({"name": "Acme", "tags": "sweep"})
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
    >>> _whole_board({"name": "Acme", "mission_score": 0.5}, 0.85)
    False

    Everyone else gets the locality-scoped pull.

    Notes:
        Whole-board fetches are the expensive kind: at the shipped floor of
        0.85 about 112 further boards per run stop being location-scoped,
        and the count climbs fast as the floor drops. It is a knob to move
        deliberately.
    """
    return (tags.has(company, tags.SWEEP) or is_watched(company)
            or _mission_trusted(company, mission_floor))


# --------------------------------------------------------------------------- #
#  Crawl helpers (per-company gate + score), used by runner + single adds.     #
# --------------------------------------------------------------------------- #

async def _keep_job(company: CompanyRow, job: FetchedJob, t: RuntimeTrack) -> bool:
    """Company-linked posting filter: technical-title gate, multi-division
    keyword gate, per-track excludes, and (when the track's geo_gate is on)
    the whole-board geography check."""
    title = job.get("title", "")
    if not gates.is_technical_role(title, t):
        return False
    watched = is_watched(company)
    if config.is_multi_division(company["name"]):
        # Workday/SmartRecruiters listings carry no description until the
        # detail call — but the relevance gate NEEDS the description (titles
        # like "Research Scientist" say nothing about the division). Hydrate
        # first; only locality-filtered jobs at conglomerates pay the GET.
        await company_fetch.hydrate_description(job)
        # Same widening src.crawl.triage's division gate applies: a WATCHED
        # conglomerate's own engineering vocabulary ([policy]
        # watch_division_titles) counts as in-field here too. Without it the
        # crawl path and the triage path disagreed about the same posting at
        # the same company, and one silently dropped what the other kept.
        if not is_relevant(title, job.get("description", ""),
                           watch_titles=watched):
            return False
    if t.exclude_gate and gates.exclude_reason(
            title, job.get("description", ""),
            allow_defense=watched, track_id=t.id):
        return False
    floor = t.remote_mission_floor
    if t.geo_gate and _whole_board(company, floor):
        # Whole-board companies are fetched with no location restriction,
        # which lets their remote and onsite-elsewhere reqs through the
        # fetch. Gate here:
        #   watched / core-mission -> local-onsite or explicitly-remote (and
        #              US-eligible: "Canada, Remote" is not remote for us) is
        #              scored (the watch flag is human-curated, the mission
        #              floor is a judged score, and ranked_jobs admits both
        #              kinds of remote into the local list);
        #   sweep   -> local-onsite ONLY. That tag is machine-set and proved
        #              untrustworthy for an out-of-area exception (slug
        #              collisions flooded the ranking with remote junk).
        gm = geo_mode(job.get("location", ""), job.get("description", ""))
        if remote_trusted(company, floor):
            if gm is None or (gm == "remote" and not us_eligible(job.get("location", ""))):
                return False
        elif gm != "onsite":
            return False
    return True


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
        **res.as_columns(),  # type: ignore[typeddict-item]  # FitColumns names JobIn's fit keys
    }
    if status is not None:
        row["status"] = status
    return row


async def _score_job(company: CompanyRow, job: FetchedJob, track: str) -> JobIn:
    await company_fetch.hydrate_description(job)
    return await _scored_row(job, company_id=company["id"],
                             company_name=company["name"], track=track)


# The miss-reason family status.check_closed_jobs closes on -- the one of
# the two RERESOLVE_FAMILIES (repair.py builds them from this name) that
# means "a board WAS here": "no-board-found" never had a working board in
# the first place, so it cannot own OPEN job rows to close.
_DEAD_BOARD_FAMILY = "board-dead"
