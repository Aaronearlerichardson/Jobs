"""ONE crawl pipeline for every track.

Historically each track shipped its own runner module whose methodology
differences — keyword handling, source families, gates, scoring budget,
digest/email — were code. They are now CONFIGURATION: every knob lives in
your profile's [tracks.<id>] table (parsed by
config._build_ui_tracks, engine-derived defaults) and this module runs the
same pipeline for any track:

    1. keyword focus     keyword_mode "extend"/"replace" + accept_remote
    2. sources           store companies (location-scoped boards or a
                         lightweight ATS sweep), priority companies,
                         aggregator feeds, web search — each toggleable
    3. gates             require_core_anchor, engine title gate, engine
                         excludes, geo_gate (or remote-stamping when off)
    4. scoring           resume-fit on new postings, cost_guard budget,
                         self-heal of newly-described rows, verify_top
    5. persist + digest  company-linked rows upsert with fit columns; sweep
                         rows upsert without them; ranked digest and/or match digest;
                         optional email

Those phases are `run_track` and the functions above it -- `_gate_sources`
(over `_gate_company_board` / `_gate_sweep_source`), `_score_and_persist`,
`_print_funnel`, `_report_ranked`, `_report_matches`. They were five
comment banners inside one 365-line function, the longest in the repo;
what kept them from being functions was a dozen shared locals, which the
`Collected` tuple now names once.

The legacy entry points delegate here unchanged, as does the web UI's
single "crawl" op. The ENGINE value still selects the code-level bits that
are not data — the technical-title regex, the exclude gate, digest
rendering — but never the methodology.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import ModuleType
from typing import Any, NamedTuple, cast

from src import config
from src import store
from src.ats.registry import iter_store_sources, sweep
from src.claude.resume import resume_text
from src.config import RuntimeTrack
from src.config.profile_schema import TrackKeywords
from src.match.filters import SHORT_KEYWORD, first_hit, is_relevant
from src.match.locality import NC_RE, geo_label, remote_signal_for, us_eligible
from src.net.parallel import fan_out, fetch_all
from src.net.util import strip_html
from src.rows import CompanyRow, FetchedJob, RankedJob, is_watched

#: Re-exported, not defined here: it moved to src/config/tracks.py, beside
#: the two tables it reads. Keeping the name importable from the runner is
#: not a compatibility shim -- "the track this engine runs" is a question
#: the crawl asks constantly, and `runner.track_for_engine` is where a
#: reader looks for it.
track_for_engine = config.track_for_engine


def apply_keyword_focus(cfg: Any, t: RuntimeTrack) -> None:
    """Point the shared keyword filter at this track's focus. Mutates the
    live list objects in place so filters.is_relevant (which imported them
    at load time) sees the change without a re-import. "extend" adds the
    track's [keywords.<id>] terms to the global tiers (deduped); "replace"
    swaps them in wholesale (and rebuilds the flat INCLUDE view, matching
    the legacy replace semantics). Empty track lists never blank a tier."""
    kw = getattr(cfg, "KEYWORDS_BY_TRACK", {}).get(t.id) or TrackKeywords()
    core, dom, skill = list(kw.core), list(kw.domain), list(kw.skill)
    if t.keyword_mode == "replace":
        if core:
            cfg.CORE_KEYWORDS[:] = core
        if dom:
            cfg.DOMAIN_KEYWORDS[:] = dom
        if skill:
            cfg.SKILL_KEYWORDS[:] = skill
        cfg.INCLUDE_KEYWORDS[:] = (cfg.CORE_KEYWORDS + cfg.DOMAIN_KEYWORDS
                                   + cfg.SKILL_KEYWORDS)
    else:
        for dst, add in ((cfg.CORE_KEYWORDS, core),
                         (cfg.DOMAIN_KEYWORDS, dom),
                         (cfg.SKILL_KEYWORDS, skill)):
            have = {k.lower() for k in dst}
            dst.extend(k for k in add if k.lower() not in have)
    cfg.ACCEPT_REMOTE = t.accept_remote


def core_anchor(title: str, description: str = "") -> str | None:
    """The require_core_anchor gate: the CORE keyword that anchors this
    posting, or None. Short single-token acronyms (eeg, ecg, rf...) match
    on word boundaries — "ecog" fires inside "recognized" — while longer
    terms stay substring so "subcortical" still matches "cortical". Reads
    the LIVE config lists, so after apply_keyword_focus this is exactly the
    track's own CORE keyword vocabulary."""
    return first_hit(config.CORE_KEYWORDS, f"{title} {description}".lower(),
                     SHORT_KEYWORD)


async def build_sources(cfg: ModuleType, t: RuntimeTrack,
                        include_websearch: bool | None = None) -> list[dict[str, Any]]:
    """Assemble the track's source specs from its `sources` config table.
    Returns a list of dicts {name, platform, thunk, company}: `thunk()` is
    the source's fetch coroutine; `company` is
    the store row for location-scoped store boards (their jobs sync/upsert
    against that company) and None for sweep sources (priority companies,
    lightweight ATS sweep, aggregators, USAJOBS, Getro boards, web search — persisted by a plain
    upsert_job). Priority companies come first so cross-source duplicates
    resolve deterministically."""
    from src.ops import maintenance as ops
    from src.ats.board import company as company_fetch

    src = t.sources
    use_ws = src.websearch if include_websearch is None else include_websearch
    specs: list[dict[str, Any]] = []
    used: set[tuple[str, str]] = set()

    # A thunk may carry what it closes over as a defaulted parameter.
    def add(name: str, platform: str, thunk: Callable[..., Awaitable[Any]],
            company: CompanyRow | None = None, key: tuple[str, str] | None = None) -> None:
        k = key or (platform, name.lower())
        if k in used:
            return
        used.add(k)
        specs.append({"name": name, "platform": platform,
                      "thunk": thunk, "company": company})

    # 1) Priority targets ([discovery] priority_companies), starred.
    if src.priority_companies:
        for name, ats, slug in getattr(cfg, "DISCOVERY_PRIORITY_COMPANIES", []):
            thunk = sweep(ats, name, slug)
            if not thunk:
                print(f"  [!] priority company {name}: unknown ATS {ats!r}")
                continue
            add(name, ats + "*", thunk, key=(ats, str(slug)))

    # 2) Company store (this track's own DB, optionally tag-scoped).
    if src.store:
        try:
            async with store.Writer(t.db_path) as db:
                # Not every active row: dormant companies (never-productive,
                # or high-volume off-mission boards) only come round again on
                # their weekly slot — see store.record_crawl_outcome.
                rows = await db.run(store.crawlable_companies, tag=t.store_tag)
        except Exception as e:
            print(f"  [!] company store unavailable ({e})")
            rows = []
        if src.location_scoped:
            # Full per-company board fetch through the locality filter —
            # whole-board (no filter) for watched/sweep-tagged companies and
            # for core-mission ones at the track's remote_mission_floor,
            # whose out-of-area rows the geo gate handles downstream.
            floor = t.remote_mission_floor
            for c in rows:
                add(c["name"], c.get("ats") or "?",
                    (lambda cc=c: company_fetch.fetch_company(
                        cc, None if ops._whole_board(cc, floor) else NC_RE)),
                    company=c, key=("store", (c["name"] or "").lower()))
        else:
            # Location-agnostic lightweight ATS sweep (JSON-API boards only;
            # the heavyweight onsite ATSes are only worth fetching scoped).
            for ats, name, slug, thunk in iter_store_sources(rows):
                add(name, ats, cast(Callable[..., Any], thunk), key=(ats, str(slug)))

    # 3) Forums + aggregator feeds (remote-native boards). Like the ATS
    # registry, the crawl injects the keyword gate here; the fetchers are
    # ungated on their own.
    if src.aggregators:
        from src.ats.feeds.discourse import fetch_discourse
        from src.ats.feeds.hnhiring import fetch_hnhiring
        from src.ats.feeds.remoteok import fetch_remoteok
        from src.ats.feeds.remotive import fetch_remotive
        from src.ats.feeds.rssfeed import fetch_rss
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
        from src.ats.feeds.usajobs import fetch_usajobs
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
        from src.ats.feeds.getro import board_host, fetch_getro_all
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
        from src.ats.feeds.websearch import fetch_websearch
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
    by_company: defaultdict[Any, deque[FetchedJob]] = defaultdict(deque)
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


def _cost_guard_trips(t: RuntimeTrack, n_to_score: int, confirm_cost: bool) -> bool:
    """True (and prints the budget banner) when scoring n_to_score postings
    would blow the track's cost_guard without an explicit confirmation."""
    guard = t.cost_guard
    if not guard or n_to_score <= guard or confirm_cost:
        return False
    # Rough per-posting cost: ~700 input tokens (cached system prompt) +
    # ~120 output, at a blended $4 per million tokens. Order-of-magnitude
    # only -- for deciding whether to stop and ask.
    est_tokens = n_to_score * 820
    est_usd = est_tokens / 1_000_000 * 4.0
    bar = "=" * 70
    print(f"\n{bar}")
    print(f"  [!] BUDGET GUARD: {n_to_score} posting(s) would be scored via "
          f"the Claude API (> {guard}).")
    print(f"      Rough estimate: ~{est_tokens:,} tokens, ~${est_usd:.2f} "
          f"(order-of-magnitude, not a quote).")
    print("      Re-run with confirm-cost to proceed. Scoring skipped this run.")
    print(f"{bar}")
    return True

class Collected(NamedTuple):
    """What one pass over the fetched sources produced.

    The crawl's five phases used to be five comment banners inside one
    365-line function, sharing a dozen locals. This is the state the first
    phase hands the rest; naming it is what let the others become
    functions.
    """
    to_score: list[tuple[CompanyRow, FetchedJob]]     # (company, job) -- fresh company-linked rows to score
    matches: list[FetchedJob]      # sweep rows surfaced (fetcher dict shape)
    watch_hits: list[tuple[CompanyRow, FetchedJob, bool]]   # (company, job, in_pipeline) at watched companies
    funnel: list[tuple[str, int, int, int, int, str]]   # per-source summary rows, in source order
    n_closed: int
    n_reopened: int
    n_seen: int
    new_ids: set[str]                  # sweep match ids the store did not hold when gated: the "(NEW)" ones


async def _gate_company_board(
        db: store.Writer, t: RuntimeTrack, c: CompanyRow, jobs: list[FetchedJob], commit: bool,
        snapshot: dict[str, Any] | None = None,
) -> tuple[list[FetchedJob], list[FetchedJob], list[tuple[CompanyRow, FetchedJob, bool]], int, int]:
    """One store company's board through the gates, on `db` (the crawl's
    store.Writer).

    Returns (kept, fresh, watch_hits, n_reopened, n_closed). `fresh` is the
    subset no crawl has handled yet -- a row the harvester stored (no
    track, no score) counts as fresh: it was fetched, never gated.

    `snapshot` is the fetch's net.http.snapshot_info() (see fetch_all): an
    incomplete fetch closes nothing, and neither does a capped one --
    store.sync_job_statuses's `capped` never closes a board-native row, on
    any miss, because a page-capped pull is an unstable window of the
    board rather than the board itself. The rows are gated either way.
    """
    from src.ops import maintenance as ops

    n_reopened = n_closed = 0
    snapshot = snapshot or {}
    if jobs and c.get("id") and commit and not snapshot.get("incomplete"):
        n_reopened, n_closed = await db.run(
            store.sync_job_statuses, c["id"], jobs, track=t.track,
            capped=snapshot.get("capped", False))
    # Reuse bodies the background harvester already fetched, so the gates
    # and the scorer below do not pay a detail GET for a posting whose
    # description is sitting in the store.
    if jobs and c.get("id"):
        stored = await db.run(store.descriptions_for_company, c["id"])
        for j in jobs:
            if not j.get("description") and j["id"] in stored:
                j["description"] = stored[j["id"]]

    kept: list[FetchedJob] = []
    for j in jobs:
        if t.require_core_anchor and not core_anchor(
                j.get("title", ""), j.get("description", "")):
            continue
        if not await ops._keep_job(c, j, t):
            continue
        kept.append(j)
    fresh, watch_hits = await db.run(_fresh_and_watched, t, c, jobs, kept, commit)
    return kept, fresh, watch_hits, n_reopened, n_closed


def _fresh_and_watched(conn: sqlite3.Connection, t: RuntimeTrack, c: CompanyRow, jobs: list[FetchedJob],
                       kept: list[FetchedJob], commit: bool
                       ) -> tuple[list[FetchedJob], list[tuple[CompanyRow, FetchedJob, bool]]]:
    """(fresh, watch_hits) for _gate_company_board: the `kept` rows no crawl
    has handled, and the watch section's hits among `jobs`."""
    from src.match import gates
    from src.match.locality import geo_mode

    fresh = [j for j in kept if not store.crawl_seen(conn, j["id"])]
    watch_hits: list[tuple[CompanyRow, FetchedJob, bool]] = []
    if is_watched(c):
        # Watch section: EVERY new technical, non-excluded posting at a
        # watched company in the US (us_eligible; 2026-09-29: 20 of 29 hits
        # were NVIDIA seats in Israel, India and Europe) -- out-of-scope
        # ones stored unscored so they aren't re-flagged next run.
        fresh_ids = {f["id"] for f in fresh}
        for j in jobs:
            if (store.crawl_seen(conn, j["id"]) and j["id"] not in fresh_ids) \
                    or not us_eligible(j.get("location") or "") \
                    or not gates.is_technical_role(j.get("title", ""), t) \
                    or (t.exclude_gate and gates.exclude_reason(
                        j.get("title", ""), j.get("description", ""),
                        allow_defense=True, track_id=t.id)):
                continue
            in_pipeline = j["id"] in fresh_ids
            watch_hits.append((c, j, in_pipeline))
            if not in_pipeline and commit:
                store.upsert_job(conn, {
                    "job_id": j["id"], "company_id": c["id"],
                    "company_name": c["name"], "title": j.get("title"),
                    "url": j.get("url"), "location": j.get("location"),
                    "track": t.track,
                    "geo_mode": geo_mode(j.get("location", ""),
                                         j.get("description", "")),
                    "posted_at": j.get("posted_at"),
                    "description": (j.get("description") or "")
                                   [:config.MAX_DESC_CHARS]})
    return fresh, watch_hits


def _gate_sweep_source(conn: sqlite3.Connection, t: RuntimeTrack, jobs: list[FetchedJob],
                      seen_ids: set[str], new_ids: set[str]) -> tuple[list[FetchedJob], int, int, int]:
    """One sweep source's jobs through the gates: anchor + title (+ engine
    excludes), remote signal stamped (or geo-gated when configured),
    deduped across sources via `seen_ids` (mutated). `new_ids` (mutated)
    gets the surfaced ids the store did not hold yet: they are read here,
    before the run writes any of them (tests/test_harvest.py::
    test_sample_matches_label_a_job_new_only_if_the_store_lacked_it).

    Returns (surfaced_jobs, anchor_n, tech_n, surfaced_n). The jobs are
    stamped in place with the display/persist fields the digest reads.
    """
    from src.match import gates
    from src.match.locality import geo_mode

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


async def _gate_sources(db: store.Writer, t: RuntimeTrack, specs: list[dict[str, Any]],
                        fetched: list[tuple[Any, Any, Any]], commit: bool) -> Collected:
    """Every fetched source through its gates, in SOURCE order, on `db`
    (the crawl's store.Writer).

    Source order, not completion order, because cross-source dedup has to
    be deterministic: the first source to surface a posting keeps it, and
    build_sources puts priority companies first on purpose.
    """
    from src.crawl.harvest import bury_404_board

    to_score: list[tuple[CompanyRow, FetchedJob]] = []
    matches: list[FetchedJob] = []
    watch_hits: list[tuple[CompanyRow, FetchedJob, bool]] = []
    funnel: list[tuple[str, int, int, int, int, str]] = []
    seen_ids: set[str] = set()
    new_ids: set[str] = set()
    n_closed = n_reopened = n_seen = 0

    for spec, (jobs, err, snapshot) in zip(specs, fetched):
        c = spec["company"]
        label = f"{spec['name']} ({spec['platform']})"
        if c is not None and c.get("id") and commit:
            # Judged on what the BOARD returned, before any of our gating:
            # a company that keeps serving jobs is alive even when none of
            # them survive the filters.
            await db.run(store.record_crawl_outcome, c["id"], len(jobs or []), err,
                         dormant_after=t.dormant_after,
                         dormant_days=t.dormant_days)
            # A fetcher reports a 404 and returns [] rather than raising, so
            # the error is usually the snapshot's, not `err`. `c` is the
            # pre-crawl row: a streak already on it is the earlier empty.
            if not jobs and (c.get("empty_streak") or 0) >= 1:
                await db.run(bury_404_board, c, str(err) if err is not None
                             else (snapshot or {}).get("last_error"))
        if err is not None:
            funnel.append((label, 0, 0, 0, 0, "ERR"))
            continue

        if c is not None:
            kept, fresh, watched, n_re, n_cl = await _gate_company_board(
                db, t, c, jobs, commit, snapshot)
            n_reopened += n_re
            n_closed += n_cl
            n_seen += len(kept) - len(fresh)
            to_score += [(c, j) for j in fresh]
            watch_hits += watched
            funnel.append((label, len(jobs), len(kept), len(fresh),
                           len(fresh), ""))
        else:
            surfaced_jobs, anchor_n, tech_n, surfaced_n = await db.run(
                _gate_sweep_source, t, jobs, seen_ids, new_ids)
            matches += surfaced_jobs
            funnel.append((label, len(jobs), anchor_n, tech_n, surfaced_n,
                           "priority" if spec["platform"].endswith("*") else ""))

    # Board-sourced jobs (Getro) name their employer: link each to its
    # roster row, queue employers the roster lacks for review, and drop the
    # copies an active roster company's own crawl already stored.
    if any(j.get("_employer") for j in matches):
        from src.discovery.apply import attribute_employers
        matches = await db.run(attribute_employers, matches, commit=commit)

    return Collected(to_score, matches, watch_hits, funnel,
                     n_closed, n_reopened, n_seen, new_ids)


async def _score_and_persist(db: store.Writer, t: RuntimeTrack, got: Collected, resume: str | None,
                             *, fit: bool, commit: bool, guard_tripped: bool,
                             max_workers: int) -> int:
    """Score what the gates kept and write it. Returns the number scored.

    Two populations with two shapes: company-linked rows go through
    ops._score_job (which builds the full store row), sweep rows are
    scored IN PLACE so the fit columns ride along to the upsert below.
    """
    from src.match.locality import geo_mode
    from src.ops import maintenance as ops

    scored = 0
    if got.to_score and fit and not guard_tripped:
        print(f"\n  scoring {len(got.to_score)} new job(s) against resume "
              f"({got.n_seen} already scored)...")
        async for row in fan_out(got.to_score,
                                 lambda cj: ops._score_job(cj[0], cj[1], t.track),
                                 "scoring", max_workers):
            # Kept separate from the scoring failure fan_out reports: a
            # store write that fails is not a scoring problem.
            try:
                if commit:
                    await db.run(store.upsert_job, row)
                scored += 1
            except Exception as e:
                print(f"    [!] store error: {e}")
    elif got.to_score:
        # Scoring skipped (fit off, or budget guard) — store the fresh rows
        # UNSCORED so they aren't re-flagged as new next run; the self-heal
        # pass scores NULL-score rows once a later run has budget again.
        why = "budget guard" if guard_tripped else "fit scoring off"
        print(f"\n  storing {len(got.to_score)} new job(s) unscored ({why})...")
        for c, j in got.to_score:
            if not commit:
                break
            await db.run(store.upsert_job, {
                "job_id": j["id"], "company_id": c["id"],
                "company_name": c["name"], "title": j.get("title"),
                "url": j.get("url"), "location": j.get("location"),
                "track": t.track,
                "geo_mode": geo_mode(j.get("location", ""),
                                     j.get("description", "")) or "onsite",
                "posted_at": j.get("posted_at"),
                "description": (j.get("description") or "")
                               [:config.MAX_DESC_CHARS]})

    if fit and got.matches and resume and not guard_tripped:
        from src.claude.fit import score_resume_fit
        print(f"  scoring {len(got.matches)} match(es) against resume...")

        async def _one(j: FetchedJob) -> None:
            res = await score_resume_fit(j["title"], j.get("description", ""),
                                         location=j.get("location") or "")
            # FitColumns is open (JobIn extends it); a closed job takes no open update.
            j.update(cast(Any, res.as_columns()))

        # `ex.map` re-raised the first failure, so one unscorable posting
        # abandoned the scoring of every other match in the sweep.
        async for _ in fan_out(got.matches, _one, "match scoring", max_workers):
            pass
        got.matches.sort(key=lambda j: (j.get("resume_fit_score") is not None,
                                        j.get("resume_fit_score") or 0.0),
                         reverse=True)

    if commit:
        # Sweep rows: the fetched dict's id/company become job_id/company_name.
        # The fit columns ride along when --fit scored the job in place
        # (j.update(FitResult.as_columns())) and are None otherwise, which
        # upsert_job's COALESCE reads as "keep the stored score" -- a
        # --fit --commit run once computed scores, printed them in the
        # digest, then dropped every one on this write.
        for job in got.matches:
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
    return scored


def _print_funnel(funnel: list[tuple[str, int, int, int, int, str]], bar: str) -> None:
    """Per-source: what the board served, what each gate left, what was new."""
    print(f"\n{bar}")
    print("  PER-SOURCE FUNNEL  (FETCH -> anchor/gates -> KEPT; NEW=unseen)")
    print(f"  {'SOURCE':<46} {'FETCH':>5} {'GATE1':>5} {'KEPT':>5} {'NEW':>5}")
    for label, n_f, g1, kept_n, new_n, note in funnel:
        tail = f"  [{note}]" if note else ""
        print(f"  {label:<46} {n_f:>5} {g1:>5} {kept_n:>5} {new_n:>5}{tail}")


async def _report_ranked(db: store.Writer, t: RuntimeTrack, got: Collected, scored: int, *,
                         send: bool, top_n: int, bar: str) -> list[RankedJob]:
    """Write (and maybe email) the ranked digest for a company-linked crawl,
    print the watch section and the top N, and return the ranked list.

    Notes:
        The ranking and the digest file are maintenance._write_digest, the
        writer every digest-writing op shares, so this crawl's watch hits
        and triage funnel cannot drift from theirs; only the email, the
        printed watch section and the richer top-N live here.
    """
    from src import digest
    from src.ops import maintenance as ops

    ranked, pipeline, followups, digest_path = await db.run(
        ops._write_digest, t, watch_hits=got.watch_hits)
    if send:
        if await asyncio.to_thread(digest.send_ranked_digest, ranked, t,
                                   watch_hits=got.watch_hits, pipeline=pipeline,
                                   followups=followups):
            await asyncio.to_thread(digest.toast, t,
                                    len(digest.new_ranked_rows(ranked, t)),
                                    digest_path)
    else:
        print(f"  (email suppressed — enable [tracks.{t.id}].email "
              f"or pass --send)")
    if got.watch_hits:
        print(f"\n  {bar}\n  WATCHED COMPANIES - NEW POSTINGS THIS RUN\n  {bar}")
        for c, j, in_pipeline in got.watch_hits:
            note = ("scored" if in_pipeline
                    else "listed only (outside local scope)")
            print(f"  [WATCH] {c['name']}: {(j.get('title') or '')[:56]}")
            print(f"          [{j.get('location') or '?'}]  ({note})")
            print(f"          {j.get('url')}")
    print(f"\n  {bar}\n  TOP {min(top_n, len(ranked))} BY RESUME FIT\n  {bar}")
    for rj in ranked[:top_n]:
        fs = (f"{rj['resume_fit_score']:.2f}"
              if isinstance(rj.get("resume_fit_score"), float) else "n/a")
        print(f"  fit={fs} [{geo_label(rj)}] "
              f"[{digest.age_tag(rj)}] {(rj['title'] or '')[:48]}")
        print(f"        {rj['company_name']} "
              f"({rj.get('mission_tier') or '?'})  -  "
              f"{rj.get('fit_reason', '')}")
        print(f"        {rj['url']}")
    print(f"\n  {len(ranked)} open job(s) in ranking; {scored} newly "
          f"scored, {got.n_closed} marked closed, {got.n_reopened} reopened "
          f"this run.")
    return ranked


async def _report_matches(matches: list[FetchedJob], t: RuntimeTrack, *, new_ids: set[str],
                          send: bool, samples: int, bar: str) -> None:
    """The sweep side: a diversified sample for a precision eyeball (each
    marked "(NEW)" when its id is in `new_ids`, else "(seen)"; see
    tests/test_harvest.py::test_sample_matches_label_a_job_new_only_if_the_store_lacked_it),
    then the matches digest."""
    from src import digest

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
        if j.get("resume_fit_score") is not None:
            print(f"     fit     : {j['resume_fit_score']:.2f}  "
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
                    websearch: bool | None = None, confirm_cost: bool = False,
                    max_workers: int = 6, top_n: int = 15, samples: int = 5) -> list[RankedJob] | list[FetchedJob]:
    """Run one crawl of track `t` (a config.UI_TRACKS entry).

    Every methodology switch reads the track config; the keyword args only
    OVERRIDE it for this run (None = use the config): `send` overrides
    t.email, `verify` (bool) overrides t.verify_top (True -> top_n,
    False -> skip), `websearch` overrides sources.websearch. `fit=False`
    skips resume scoring; `commit=False` is the legacy sweep preview (no
    DB writes). Returns the ranked list (company-linked crawls) or the
    surfaced match list.

    The five phases are the five functions above, in order. They were five
    comment banners inside this function when it ran to 365 lines -- the
    longest in the repo -- and what kept them from being functions was a
    dozen shared locals, now named once in `Collected`.
    """
    from src.ops import scoring

    engine = t.engine
    send = t.email if send is None else send
    verify_n = (t.verify_top if verify is None
                else (top_n if verify else 0))

    resume = await asyncio.to_thread(resume_text) if fit else None
    if fit and not resume:
        print("  [!] No resume text — fit scores will be null. "
              "Set config.RESUME_PATH.")

    apply_keyword_focus(config, t)
    specs = await build_sources(config, t, include_websearch=websearch)
    sources = [(s["name"], s["platform"], s["thunk"]) for s in specs]
    async with store.Writer(t.db_path) as db:

        bar = "=" * 70
        gates_desc: list[str] = []
        if t.require_core_anchor:
            gates_desc.append("core-anchor")
        gates_desc.append("technical-title")
        gates_desc.append("geo" if t.geo_gate else "remote-stamped")
        print(f"\n{bar}\n  [{t.label.upper()}] crawl - "
              f"{datetime.now():%Y-%m-%d %H:%M}")
        print(f"  engine={engine} keywords={t.keyword_mode} "
              f"sources={sum(1 for _, v in t.sources if v)} families "
              f"({len(sources)} feeds) gates={'+'.join(gates_desc)}")
        mode = "COMMIT (DB writes)" if commit else "PREVIEW (no DB writes)"
        print(f"  Mode: {mode}" + (" + EMAIL" if send else "") + f"\n{bar}\n")
        if not sources:
            print("  [!] No sources — check [tracks.*].sources and the company "
                  "store (discover.py --local / --import-companies).")

        done_count = [0]

        def _progress(name: str, platform: str, jobs: list[FetchedJob],
                      err: object) -> None:
            done_count[0] += 1
            status = f"fetch error: {err}" if err else f"{len(jobs)} relevant"
            print(f"  [{done_count[0]:>3}/{len(sources)}] {name} ({platform}): "
                  f"{status}")

        fetched = await fetch_all(sources, on_done=_progress)

        got = await _gate_sources(db, t, specs, fetched, commit)

        n_would_score = (len(got.to_score) + len(got.matches)) if fit else 0
        guard_tripped = fit and _cost_guard_trips(t, n_would_score, confirm_cost)
        scored = await _score_and_persist(db, t, got, resume, fit=fit, commit=commit,
                                          guard_tripped=guard_tripped,
                                          max_workers=max_workers)

        linked = t.sources.store and t.sources.location_scoped
        if resume and commit and linked and not guard_tripped:
            scored += await scoring.self_heal_unscored(
                db, resume, track=t.track, max_workers=max_workers)
        if verify_n and resume and commit and not guard_tripped:
            await scoring.verify_top(top_n=verify_n,
                                     max_workers=max(2, max_workers // 2),
                                     db=db, t=t)

        _print_funnel(got.funnel, bar)

        ranked: list[RankedJob] | None = None
        if linked:
            ranked = await _report_ranked(db, t, got, scored, send=send,
                                          top_n=top_n, bar=bar)
        if got.matches or not linked:
            await _report_matches(got.matches, t, new_ids=got.new_ids, send=send,
                                  samples=samples, bar=bar)

        print("")
    return ranked if ranked is not None else got.matches
