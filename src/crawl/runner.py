"""ONE crawl pipeline for every track.

A crawl of track `t` is the harvest pipeline (src/crawl/harvest.py `run`:
plan, pull whole boards, triage, verify, closed-URL probe, digest) scoped to
`t`, then the source families that are not on the company roster. Every knob
lives in the profile's [tracks.<id>] table (config.tracks, engine-derived
defaults):

    1. roster boards     harvest.run, tag-scoped to `store_tag`, reporting `t`
    2. sources           priority companies, aggregator feeds, USAJOBS, Getro,
                         web search -- each toggleable
    3. gates             require_core_anchor, engine title gate, engine
                         excludes, geo_gate (or remote-stamping when off)
    4. scoring           resume-fit on the surfaced matches, within what is
                         left of the run's one triage.SCORE_CAP budget
    5. persist + digest  matches upsert with fit columns; matches digest;
                         optional email

The ENGINE value still selects the code-level bits that are not data -- the
technical-title regex, the exclude gate, digest rendering -- but never the
methodology.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import ModuleType
from typing import NamedTuple, TypedDict

from src import config
from src import digest
from src import store
from src.ats.feeds.discourse import fetch_discourse
from src.ats.feeds.getro import board_host, fetch_getro_all
from src.ats.feeds.hnhiring import fetch_hnhiring
from src.ats.feeds.remoteok import fetch_remoteok
from src.ats.feeds.remotive import fetch_remotive
from src.ats.feeds.rssfeed import fetch_rss
from src.ats.feeds.usajobs import fetch_usajobs
from src.ats.feeds.websearch import fetch_websearch
from src.ats.registry import sweep
from src.claude.fit import score_resume_fit
from src.claude.resume import resume_text
from src.config import RuntimeTrack
from src.crawl import harvest, triage
from src.discovery.apply import attribute_employers
from src.match import gates
from src.match.filters import core_anchor, is_relevant
from src.match.locality import geo_mode, remote_signal_for
from src.net.http import Snapshot
from src.net.parallel import fan_out, fetch_all
from src.net.util import strip_html
from src.rows import FetchedJob

#: Re-exported, not defined here: it moved to src/config/tracks.py, beside
#: the two tables it reads. Keeping the name importable from the runner is
#: not a compatibility shim -- "the track this engine runs" is a question
#: the crawl asks constantly, and `runner.track_for_engine` is where a
#: reader looks for it.
track_for_engine = config.track_for_engine


type SourceThunk = Callable[[], Awaitable[list[FetchedJob] | None]]


class SourceSpec(TypedDict):
    """One source of a crawl: `thunk()` fetches it."""
    name: str
    platform: str
    thunk: SourceThunk


async def build_sources(cfg: ModuleType, t: RuntimeTrack,
                        include_websearch: bool | None = None) -> list[SourceSpec]:
    """The track's non-roster source specs ({name, platform, thunk}) from
    its `sources` config table: priority companies, aggregator feeds,
    USAJOBS, Getro boards, web search. `thunk()` is the source's fetch
    coroutine; every one is persisted by a plain upsert_job. Priority
    companies come first so cross-source duplicates resolve
    deterministically. The roster is harvest.run's.

    >>> import types
    >>> from pathlib import Path
    >>> from src.config.profile_schema import TrackSources
    >>> cfg = types.SimpleNamespace(
    ...     DISCOVERY_PRIORITY_COMPANIES=[("Acme", "greenhouse", "acme")],
    ...     WEBSEARCH_QUERIES=[("WS", "neural engineer jobs", 5)])
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), sources=TrackSources(
    ...     priority_companies=True, websearch=True))
    >>> [(s["name"], s["platform"]) for s in asyncio.run(build_sources(cfg, t))]
    [('Acme', 'greenhouse*'), ('WS', 'websearch')]
    >>> [s["name"] for s in asyncio.run(build_sources(cfg, t, include_websearch=False))]
    ['Acme']
    """
    src = t.sources
    use_ws = src.websearch if include_websearch is None else include_websearch
    specs: list[SourceSpec] = []
    used: set[tuple[str, str]] = set()

    # A thunk may carry what it closes over as a defaulted parameter.
    def add(name: str, platform: str, thunk: SourceThunk,
            key: tuple[str, str] | None = None) -> None:
        k = key or (platform, name.lower())
        if k in used:
            return
        used.add(k)
        specs.append({"name": name, "platform": platform, "thunk": thunk})

    # 1) Priority targets ([discovery] priority_companies), starred.
    if src.priority_companies:
        for name, ats, slug in getattr(cfg, "DISCOVERY_PRIORITY_COMPANIES", []):
            thunk = sweep(ats, name, slug)
            if not thunk:
                print(f"  [!] priority company {name}: unknown ATS {ats!r}")
                continue
            add(name, ats + "*", thunk, key=(ats, str(slug)))

    # 3) Forums + aggregator feeds (remote-native boards). Like the ATS
    # registry, the crawl injects the keyword gate here; the fetchers are
    # ungated on their own.
    if src.aggregators:
        for name, base, cat in cfg.DISCOURSE_BOARDS:
            add(name, "discourse",
                lambda n=name, b=base, c=cat: fetch_discourse(n, b, c, gate=is_relevant))
        if getattr(cfg, "REMOTEOK_ENABLED", True):
            add("RemoteOK", "remoteok", lambda: fetch_remoteok(gate=is_relevant))
        if getattr(cfg, "REMOTIVE_ENABLED", True):
            add("Remotive", "remotive",
                lambda: fetch_remotive(category=cfg.REMOTIVE_CATEGORY,
                                       gate=is_relevant))
        if getattr(cfg, "HNHIRING_ENABLED", True):
            add("HN Who-is-hiring", "hn",
                lambda: fetch_hnhiring(max_threads=cfg.HNHIRING_MAX_THREADS,
                                       gate=is_relevant))
        for label, url, default_loc in cfg.RSS_FEEDS:
            is_remote_board = default_loc.strip().lower() == "remote"
            add(label, "rss",
                lambda l=label, u=url, d=default_loc, rb=is_remote_board:
                    fetch_rss(l, u, default_location=d, remote_board=rb,
                              gate=is_relevant))

    # 4) USAJOBS (federal openings). Deliberately NOT under `aggregators`:
    # that family is remote-native boards and is off for location-scoped
    # tracks, whereas this source is location-scoped by construction (its
    # profile table names a place and a radius) and is exactly what a local
    # track wants — a federal campus has no ATS to put in the roster. Safe
    # to leave outside the gate because it ships OFF and needs credentials.
    if getattr(cfg, "USAJOBS_ENABLED", False):
        add("USAJOBS", "usajobs",
            lambda: fetch_usajobs(
                keyword=cfg.USAJOBS_KEYWORD, location=cfg.USAJOBS_LOCATION,
                radius=cfg.USAJOBS_RADIUS, series=cfg.USAJOBS_SERIES,
                results_per_page=cfg.USAJOBS_RESULTS_PER_PAGE,
                gate=is_relevant))

    # 4b) Getro network boards (a VC portfolio, an association). Outside the
    # `aggregators` gate for the same reason as USAJOBS: a board is a place,
    # and the local track's geo gate decides which of its postings apply.
    # Ships OFF. Their employers are attributed after gating (run_track).
    if getattr(cfg, "GETRO_ENABLED", False):
        for board in getattr(cfg, "GETRO_BOARDS", []):
            host = board_host(board)
            if not host:
                continue
            add(f"Getro {host}", "getro",
                lambda b=board: fetch_getro_all(
                    b, max_details=getattr(cfg, "GETRO_MAX_DETAILS", 150),
                    gate=is_relevant))

    # 5) Web searches (DDG -> JSON-LD).
    if use_ws:
        for label, query, n in getattr(cfg, "WEBSEARCH_QUERIES", []):
            add(label, "websearch",
                lambda l=label, q=query, m=n: fetch_websearch(
                    l, q, max_results=m, gate=is_relevant))

    return specs


def _short(text: str, n: int) -> str:
    """`text` as one line of readable prose, truncated to `n` characters —
    the console blurb under a sampled match. The markup half is
    net.util.strip_html, which also unescapes entities (a JD blurb reading
    "R&amp;D" was the reason to stop rolling this by hand)."""
    text = strip_html(text)
    return text if len(text) <= n else text[: n - 1] + "..."


def _diversify(matches: list[FetchedJob], n: int) -> list[FetchedJob]:
    """Up to n samples spread round-robin across companies so the precision
    sanity-check isn't dominated by one prolific employer."""
    by_company: defaultdict[object, deque[FetchedJob]] = defaultdict(deque)
    for j in matches:
        by_company[j.get("company") or j.get("company_name")].append(j)
    picked: list[FetchedJob] = []
    while len(picked) < n and any(by_company.values()):
        for waiting in by_company.values():
            if waiting:
                picked.append(waiting.popleft())
                if len(picked) >= n:
                    break
    return picked


class Collected(NamedTuple):
    """What one pass over the fetched sources produced."""
    matches: list[FetchedJob]      # sweep rows surfaced (fetcher dict shape)
    funnel: list[tuple[str, int, int, int, int, str]]   # per-source summary rows, in source order
    new_ids: set[str]                  # match ids the store did not hold when gated: the "(NEW)" ones


def _gate_sweep_source(conn: sqlite3.Connection, t: RuntimeTrack, jobs: list[FetchedJob],
                      seen_ids: set[str], new_ids: set[str]) -> tuple[list[FetchedJob], int, int, int]:
    """One sweep source's jobs through the gates: anchor + title (+ engine
    excludes), remote signal stamped (or geo-gated when configured),
    deduped across sources via `seen_ids` (mutated). `new_ids` (mutated)
    gets the surfaced ids the store did not hold yet: they are read here,
    before the run writes any of them, so the sample label "(NEW)" is the
    store's answer at gating time.

    Returns (surfaced_jobs, anchor_n, tech_n, surfaced_n). The jobs are
    stamped in place with the display/persist fields the digest reads.

    >>> from pathlib import Path
    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_job(conn, {"job_id": "feed_old", "title": "Old Role"})
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), require_core_anchor=False,
    ...                  exclude_gate=False, geo_gate=False, tech_title_regex="role")
    >>> feed = [{"id": f"feed_{n}", "title": f"{n.title()} Role", "location": "Remote"}
    ...         for n in ("old", "new")]
    >>> new_ids = set()
    >>> out, *_ = _gate_sweep_source(conn, t, feed, set(), new_ids)
    >>> [j["id"] for j in out], sorted(new_ids)
    (['feed_old', 'feed_new'], ['feed_new'])
    """
    out: list[FetchedJob] = []
    anchor_here = tech_here = surfaced = 0
    for job in jobs:
        title = job.get("title", "")
        nsig = None
        if t.require_core_anchor:
            nsig = core_anchor(title, job.get("description", ""))
            if not nsig:
                continue
        anchor_here += 1
        if not gates.is_technical_role(title, t):
            continue
        tech_here += 1
        if t.exclude_gate and gates.exclude_reason(
                title, job.get("description", ""), track_id=t.id):
            continue
        if t.geo_gate and geo_mode(
                job.get("location", ""), job.get("description", "")) is None:
            continue
        sig = remote_signal_for(job)
        surfaced += 1
        jid = job["id"]
        if jid in seen_ids:
            continue
        seen_ids.add(jid)
        job["track_tag"] = f"[{t.label.upper()}]"
        job["remote_eligible"] = bool(sig)
        if sig is not None:
            job["remote_signal"] = sig
        if nsig:
            job["anchor_signal"] = nsig
        if not store.job_exists(conn, jid):
            new_ids.add(jid)
        out.append(job)
    return out, anchor_here, tech_here, surfaced


async def _gate_sources(db: store.Writer, t: RuntimeTrack, specs: list[SourceSpec],
                        fetched: list[tuple[list[FetchedJob], BaseException | None, Snapshot | None]]
                        ) -> Collected:
    """Every fetched source through its gates, in SOURCE order, on `db`
    (the crawl's store.Writer).

    Source order, not completion order, because cross-source dedup has to
    be deterministic: the first source to surface a posting keeps it, and
    build_sources puts priority companies first on purpose.
    """
    matches: list[FetchedJob] = []
    funnel: list[tuple[str, int, int, int, int, str]] = []
    seen_ids: set[str] = set()
    new_ids: set[str] = set()

    for spec, (jobs, err, _) in zip(specs, fetched):
        label = f"{spec['name']} ({spec['platform']})"
        if err is not None:
            funnel.append((label, 0, 0, 0, 0, "ERR"))
            continue
        surfaced_jobs, anchor_n, tech_n, surfaced_n = await db.run(
            _gate_sweep_source, t, jobs, seen_ids, new_ids)
        matches += surfaced_jobs
        funnel.append((label, len(jobs), anchor_n, tech_n, surfaced_n,
                       "priority" if spec["platform"].endswith("*") else ""))

    # Board-sourced jobs (Getro) name their employer: link each to its
    # roster row, queue employers the roster lacks for review, and drop the
    # copies an active roster company's own crawl already stored.
    if any(j.get("_employer") for j in matches):
        matches = await db.run(attribute_employers, matches, commit=True)

    return Collected(matches, funnel, new_ids)


async def _score_and_persist(db: store.Writer, t: RuntimeTrack, matches: list[FetchedJob],
                             resume: str | None, *, fit: bool, max_workers: int,
                             budget: int) -> None:
    """Score the surfaced matches IN PLACE (the first `budget` fit calls,
    what the run's harvest.run left; the rest are stored unscored) and
    write them, fit columns riding along."""
    if fit and matches and resume:
        to_fit = matches[:max(0, budget)]
        print(f"  scoring {len(to_fit)} match(es) against resume"
              + (f" ({len(matches) - len(to_fit)} over the budget stay unscored)"
                 if len(matches) > len(to_fit) else "") + "...")

        async def _one(j: FetchedJob) -> None:
            res = await score_resume_fit(j["title"], j.get("description", ""),
                                         location=j.get("location") or "")
            j.update(res.as_columns())

        # `ex.map` re-raised the first failure, so one unscorable posting
        # abandoned the scoring of every other match in the sweep.
        async for _ in fan_out(to_fit, _one, "match scoring", max_workers):
            pass
        matches.sort(key=lambda j: (j.get("resume_fit_score") is not None,
                                    j.get("resume_fit_score") or 0.0),
                     reverse=True)

    # The fit columns are None unless scored, which upsert_job's COALESCE
    # reads as "keep the stored score" -- a --fit run once computed scores,
    # printed them in the digest, then dropped every one on this write.
    for job in matches:
        await db.run(store.upsert_job, {
            "job_id": job["id"], "company_id": job.get("company_id"),
            "company_name": job.get("company"), "title": job.get("title"),
            "url": job.get("url"), "location": job.get("location"),
            "track": t.track,
            "remote_eligible": job.get("remote_eligible"),
            "remote_signal": job.get("remote_signal"),
            "anchor_signal": job.get("anchor_signal"),
            "posted_at": job.get("posted_at"),
            "description": (job.get("description") or "")
                           [:config.MAX_DESC_CHARS],
            "resume_fit_score": job.get("resume_fit_score"),
            "fit_reason": job.get("fit_reason"), "fit_gates": job.get("fit_gates"),
            "fit_model": job.get("fit_model"), "fit_domain": job.get("fit_domain"),
            "fit_function": job.get("fit_function"), "fit_stack": job.get("fit_stack"),
            "fit_seniority": job.get("fit_seniority"),
        })


def _print_funnel(funnel: list[tuple[str, int, int, int, int, str]], bar: str) -> None:
    """Per-source: what the board served, what each gate left, what was new."""
    print(f"\n{bar}")
    print("  PER-SOURCE FUNNEL  (FETCH -> anchor/gates -> KEPT; NEW=unseen)")
    print(f"  {'SOURCE':<46} {'FETCH':>5} {'GATE1':>5} {'KEPT':>5} {'NEW':>5}")
    for label, n_f, g1, kept_n, new_n, note in funnel:
        tail = f"  [{note}]" if note else ""
        print(f"  {label:<46} {n_f:>5} {g1:>5} {kept_n:>5} {new_n:>5}{tail}")


async def _report_matches(matches: list[FetchedJob], t: RuntimeTrack, *, new_ids: set[str],
                          send: bool, samples: int, bar: str) -> None:
    """The sweep side: a diversified sample for a precision eyeball (each
    marked "(NEW)" when its id is in `new_ids`, else "(seen)"; see
    _gate_sweep_source's doctest), then the matches digest."""
    n = max(0, samples)
    print(f"\n{bar}\n  {min(n, len(matches))} SAMPLE MATCHES "
          f"(precision sanity-check)\n{bar}")
    if not matches:
        print("  (no matches)")
    for i, j in enumerate(_diversify(matches, n), 1):
        print(f"\n  {i}. {j.get('track_tag', '')} {j['title']}")
        print(f"     company : {j.get('company') or j.get('company_name')}")
        print(f"     location: {j.get('location')}")
        if j.get("anchor_signal"):
            print(f"     anchor  : {j['anchor_signal']}")
        if (fit := j.get("resume_fit_score")) is not None:
            print(f"     fit     : {fit:.2f}  "
                  f"({j.get('fit_reason', '')})")
        print(f"     remote  : {j.get('remote_signal', '')}"
              f"{'   (NEW)' if j['id'] in new_ids else '   (seen)'}")
        print(f"     url     : {j['url']}")
        if j.get("description"):
            print(f"     blurb   : {_short(j['description'], 160)}")
    digest_path = digest.write_matches_digest(matches, config.REPORT_DIR, t)
    print(f"\n  Digest -> {digest_path}")
    if send:
        await asyncio.to_thread(digest.send_matches_digest, matches, t, config)
    elif matches:
        print("  (email suppressed — enable [tracks.*].email or --send)")


async def run_track(t: RuntimeTrack, *, fit: bool = True, commit: bool = True,
                    send: bool | None = None, verify: bool | None = None,
                    websearch: bool | None = None, refresh: bool = False,
                    max_workers: int = 6, top_n: int = 15, samples: int = 5) -> list[FetchedJob]:
    """Run one crawl of track `t` (a config.UI_TRACKS entry); returns the
    surfaced non-roster matches.

    The roster is harvest.run scoped to `t` (boards carrying its store_tag,
    `t` reported); the other source families follow. Every switch reads the
    track config; the keyword args only OVERRIDE it for this run (None =
    use the config): `send` overrides t.email, `verify` (bool) overrides
    t.verify_top (True -> top_n, False -> skip), `websearch` overrides
    sources.websearch. `refresh` pulls every board, not only the ones not
    pulled in the last few hours. `fit=False` skips resume scoring;
    `commit=False` prints the boards the crawl would pull and stops.
    """
    send = t.email if send is None else send
    if not commit:
        if t.sources.store:
            harvest.list_plan(t.db_path, min_age_hours=0 if refresh else None,
                              tag=t.store_tag)
        return []
    budget = triage.SCORE_CAP
    if t.sources.store:
        n = t.verify_top if verify is None else (top_n if verify else 0)
        summary = await harvest.run(
            db_path=t.db_path, scope=t.model_copy(update={"email": send, "verify_top": n}),
            min_age_hours=0 if refresh else None, fit=fit,
            top_n=top_n, max_workers=max_workers)
        left = summary.get("score_left")
        if isinstance(left, (int, float)):
            budget = int(left)

    resume = await asyncio.to_thread(resume_text) if fit else None
    if fit and not resume:
        print("  [!] No resume text -- fit scores will be null. "
              "Set config.RESUME_PATH.")

    with config.keyword_focus(t):
        specs = await build_sources(config, t, include_websearch=websearch)
        sources = [(s["name"], s["platform"], s["thunk"]) for s in specs]
        if not sources:
            return []
        async with store.Writer(t.db_path) as db:
            bar = "=" * 70
            print(f"\n{bar}\n  [{t.label.upper()}] other sources - "
                  f"{datetime.now():%Y-%m-%d %H:%M}\n  engine={t.engine} "
                  f"keywords={t.keyword_mode} ({len(sources)} feeds)\n{bar}\n")
            done_count = [0]

            def _progress(name: str, platform: str, jobs: list[FetchedJob],
                          err: object) -> None:
                done_count[0] += 1
                status = f"fetch error: {err}" if err else f"{len(jobs)} relevant"
                print(f"  [{done_count[0]:>3}/{len(sources)}] {name} ({platform}): "
                      f"{status}")

            fetched = await fetch_all(sources, on_done=_progress)
            got = await _gate_sources(db, t, specs, fetched)
            await _score_and_persist(db, t, got.matches, resume, fit=fit,
                                     max_workers=max_workers, budget=budget)
            _print_funnel(got.funnel, bar)
            if got.matches or not t.sources.store:
                await _report_matches(got.matches, t, new_ids=got.new_ids,
                                      send=send, samples=samples, bar=bar)
            print("")
    return got.matches
