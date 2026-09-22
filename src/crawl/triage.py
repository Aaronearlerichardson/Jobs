"""Harvest triage: weed the harvester's unscored rows down, cheapest gate
first, before anything expensive is spent on them.

The harvester (src/crawl/harvest.py) pulls every board whole and stores
every posting unscored. That is the cheap half. This module is what
happens next: every stored row no crawl has adopted (no track label) and
triage has not judged (triage_status NULL) is run through the same gates
the crawl applies, in cost order, and the first gate that drops a row is
recorded on it. Only rows that survive every free gate pay for a detail
GET (description hydration), and only hydrated survivors pay for a Claude
fit score.

Gates, per configured track, in order:

  1. mission   company-level, once per COMPANY: the roster row's cached
               mission tier (src.claude.is_active_mission) and the
               track's min_mission. A company never scored gets ONE
               score_company_mission call, with the harvested titles as
               context, and the verdict is written back to the roster.
               Multi-division employers (profile [policy]) fall through:
               a conglomerate's corporate score says nothing about the
               division that is hiring.
  2. title     the track's technical-title regex (src.match.gates), title only.
  3. anchor    the track's require_core_anchor (runner.core_anchor) -- on
               the title plus whatever body is stored; a bodiless row
               whose title does not anchor is deferred, not dropped.
  4. geo       the track's geo_gate, judged on the location FIELD: local
               passes; explicitly remote passes at a watched or mission-
               trusted company (the ranking's remote_admitted rule). Body
               text is not consulted while the field names a place -- it
               is too loose both ways ("Nonconformance (NC)", "Garner is
               on a mission" read local; "fully remote" in a Cambridge MA
               posting read remote, and 15 such rows then paid for a fit
               score only to trip the model's own geo gate, 2026-09-10).
               A listing that names no place ("N Locations", "See
               posting") defers to after hydration, then passes on a
               strict "<place>, ST" in the body, or a remote phrase at a
               trusted company.
  5. exclude   the track's [exclude.<id>] tables (src.match.gates).
  6. division  multi-division companies only: src.match.filters.is_relevant on
               the body (needs one). A WATCHED conglomerate also passes on a
               [policy] watch_division_titles TITLE (is_relevant's
               watch_titles tier): the watch tag already says "I want this
               employer's technical roles", and its aligned division is a
               plain engineering org whose postings never carry the
               profile's health/bio vocabulary. The [exclude] gate inside
               is_relevant still applies, so this widens the division
               vocabulary rather than lifting the gate.
  --- hydrate survivors (the harvester's per-host caps and pauses) ---
  gates 3-6 again, now with a body
  7. fit       Claude fit score (src.claude.fit); the verdict is 'fit' when the
               score is under every surfaced track's digest_min_fit, 'ok'
               otherwise. Either way the row is stamped with its track
               labels and score, so it enters the ranking like a crawled
               row and the crawl never re-scores it.

A row is judged against EVERY track that reads the roster; it surfaces
into the union of the tracks it passed (jobs.track is a set). The per-
track record ("local-tech=geo;remote-neural=title") is kept in
triage_detail so a false drop is debuggable; triage_status carries the
one row verdict the funnel counts.

Keyword focus (runner.apply_keyword_focus) mutates config's shared lists,
so the gate phases run on the calling thread, one track at a time, with
the lists restored afterwards. Only hydration and scoring fan out to
threads, and neither reads those lists.

Those seven steps are seven functions -- _free_gates, _hydrate,
_body_gates, _score, _write_verdicts, over _by_company and _judged -- and
`run` is the order they go in. They were numbered comments inside one
167-line function; the two gate phases ask the identical question and had
written the asking out twice.
"""

import logging
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta

from src import config
from src import store
from src import tags
from src.ats import coords
from src.ats.fetchers.company import needs_detail
from src.claude.api import is_active_mission, score_company_mission
from src.claude.fit import MIN_DESC_CHARS, score_resume_fit
from src.crawl import harvest
from src.crawl.harvest import MISS_BACKOFF_S, _hydrate_rows, hydrate_delay
from src.crawl.runner import apply_keyword_focus, core_anchor
from src.match import gates
from src.match.filters import is_relevant
from src.match.locality import (NC_HQ_RE, geo_mode, is_nc, location_unknown,
                                remote_signal, remote_signal_for)
from src.net.parallel import fan_out
from src.net.util import clean_field
from src.ops import maintenance as ops

_log = logging.getLogger(__name__)

OK = store.TRIAGE_OK
# Verdict rank: a row's status is the verdict of the track it got FURTHEST
# through, so a posting dropped for geography on one track and title on
# another reads as a geo drop (the more interesting fact).
_RANK = {g: i for i, g in enumerate(store.TRIAGE_GATES)}
_RANK[OK] = len(_RANK)
# Internal "not yet decidable" marker: the row needs a body before this
# track can rule on it. Never stored.
DEFER = "defer"

# Claude fit calls per pass. The first pass over a fresh harvest can leave
# thousands of survivors; the rest wait (their bodies are stored, so the
# next pass costs no GETs to reach them). Same order of magnitude as the
# sweep engine's cost_guard.
SCORE_CAP = 300
# A detail fetch (body, or -- Workday -- a location-only lookup) that
# failed this recently is not retried (the backfill ops' convention); the
# row stays pending until then. _hydrate stamps desc_checked_at for either
# kind of failure.
RETRY_DAYS = 3
# How many "still waiting on a body" rows _print_waiting names before it
# collapses the rest into a count -- a whole-board pass can leave hundreds
# pending and the point is a spot check, not a full dump.
_WAITING_CAP = 20
# The harvester's pool size, shared: triage runs as the harvest pass's
# second half and must not disagree with it about HARVEST_WORKERS.
DEFAULT_WORKERS = harvest.DEFAULT_WORKERS


# --------------------------------------------------------------------------- #
#  Which tracks read the roster                                                #
# --------------------------------------------------------------------------- #

def roster_tracks(tracks=None):
    """The configured tracks whose sources include the company store --
    the ones a harvested row can surface into."""
    return [t for t in (tracks or config.UI_TRACKS.values())
            if t["sources"].get("store")]


def _track_applies(t, company):
    """A tag-scoped track (store_tag) only reads companies carrying it."""
    tag = t.get("store_tag")
    return not tag or tags.has(company.get("tags"), tag)


@contextmanager
def _keyword_focus(t):
    """apply_keyword_focus for the duration of a block, then put the shared
    lists back (config.keyword_snapshot / restore_keywords)."""
    saved = config.keyword_snapshot()
    apply_keyword_focus(config, t)
    try:
        yield
    finally:
        config.restore_keywords(saved)


# --------------------------------------------------------------------------- #
#  Gate 1: mission, once per company                                          #
# --------------------------------------------------------------------------- #

def ensure_mission(conn, company, titles=(), scorer=score_company_mission):
    """The company's (tier, score), scoring it ONCE via Claude when the
    roster row has neither, with the harvested titles as context, and
    caching the verdict on the row. Never touches `active`: activation
    policy belongs to src.discovery.local_sourcing.score_missions. Returns
    (None, None) when scoring is unavailable -- which reads as unknown,
    not off-mission, everywhere downstream."""
    tier, score = company.get("mission_tier"), company.get("mission_score")
    if tier or score is not None:
        return tier, score
    if config.is_multi_division(company.get("name")):
        return None, None                       # the gate ignores it anyway
    if company.get("_mission_tried"):
        return None, None                       # one attempt per pass
    company["_mission_tried"] = True
    try:
        context = (" | ".join(t for t in titles if t)
                   or coords.board_context(company))
        tier, score, reason = scorer(company.get("name") or "", context[:1500])
    except Exception as e:                      # noqa: BLE001 - reported
        print(f"    [!] mission score failed for {company.get('name')}: {e}")
        return None, None
    if tier is None and score is None:
        return None, None
    store.upsert_company(conn, {"name": company["name"], "mission_tier": tier,
                                "mission_score": score, "mission_reason": reason})
    company["mission_tier"], company["mission_score"] = tier, score
    _log.debug("mission %s -> %s %.2f", company.get("name"), tier, score or 0)
    return tier, score


def mission_verdict(company, t):
    """OK, or 'mission' when the whole company is out for track `t`:
    an inactive mission tier (multi-division exempt), or a known effective
    mission under the track's min_mission. Unknown never drops.

    >>> t = {"min_mission": 0.2}
    >>> mission_verdict({"name": "A", "mission_tier": "other",
    ...                  "mission_score": 0.05}, t)
    'mission'
    >>> mission_verdict({"name": "A", "mission_tier": None,
    ...                  "mission_score": None}, t)
    'ok'
    """
    name = company.get("name")
    tier, score = company.get("mission_tier"), company.get("mission_score")
    if not is_active_mission(tier, name):
        return "mission"
    floor = t.get("min_mission")
    if floor is None or score is None:
        return OK
    if config.is_multi_division(name):
        score = max(score, config.MULTI_DIVISION_MISSION_FLOOR)
    return OK if score >= floor else "mission"


# --------------------------------------------------------------------------- #
#  Gates 2-6: per row, per track                                               #
# --------------------------------------------------------------------------- #

def _detail_stale(row, cutoff):
    """True once a detail fetch on this row (a body, or -- Workday -- a
    location-only lookup) already failed at least RETRY_DAYS ago, per its
    desc_checked_at. No stamp yet reads as fresh, never stale: a row gets
    one try before the geo gate stops waiting on it (see _geo_verdict)."""
    checked = row.get("desc_checked_at") or ""
    return bool(checked) and checked < cutoff


def _geo_verdict(company, job, t, has_body, cutoff):
    """Geography on the location FIELD (see the module docstring, gate 4):
    local passes anywhere; remote passes at a watched or mission-trusted
    company. An UNKNOWN location (location_unknown) defers while a detail
    fetch could still name the place (company_fetch.needs_detail): always
    for a bodiless row, and for a bodied one until that fetch has failed
    RETRY_DAYS ago (_detail_stale). With nothing left to fetch, the strict
    "<place>, ST" body rule decides.

    Notes:
        Stricter than the crawl's whole-board path (ops._keep_job, which
        lets geo_mode read the body): in the 2026-09-10 dry run the loose
        body match called 83 of 130 survivors local ("Garner", "apex",
        "NC" as nonconformance, HQ boilerplate) and 15 non-local rows
        remote.
    """
    loc = job.get("location") or ""
    desc = job.get("description") or ""
    floor = t.get("remote_mission_floor")
    trusted = tags.has(company, tags.WATCH) or ops._mission_trusted(company, floor)
    if is_nc(loc):
        return OK
    if trusted and (remote_signal(loc) or job.get("remote_hint")):
        return OK
    if not location_unknown(loc):
        return "geo"
    if not has_body or (needs_detail(_fetcher_shape(job, company))
                        and not _detail_stale(job, cutoff)):
        return DEFER                    # a detail fetch may name the place
    # Nothing left that could name the place: the body is all there is.
    if NC_HQ_RE.search(desc) or (trusted and remote_signal("", desc)):
        return OK
    return "geo"


def row_verdict(company, job, t, cutoff):
    """Gates 2-6 for one row on one track, on the text the row has NOW.
    Returns OK, a gate name, or DEFER (undecidable without a body). The
    caller has already applied the track's keyword focus. `cutoff` is the
    RETRY_DAYS boundary (an isoformat string) the geo gate uses to tell a
    fresh unknown-location row from one whose detail fetch has already
    given up -- see _geo_verdict."""
    title = job.get("title") or ""
    desc = job.get("description") or ""
    has_body = bool(desc.strip())
    if not gates.is_technical_role(title, t):
        return "title"
    deferred = False
    if t["require_core_anchor"] and not core_anchor(title, desc):
        if has_body:
            return "anchor"
        deferred = True
    if t["geo_gate"]:
        v = _geo_verdict(company, job, t, has_body, cutoff)
        if v == "geo":
            return "geo"
        deferred = deferred or v == DEFER
    if t["exclude_gate"] and gates.exclude_reason(
            title, desc, allow_defense=tags.has(company, tags.WATCH),
            track_id=t["id"]):
        return "exclude"
    if config.is_multi_division(company.get("name")):
        if not has_body:
            deferred = True
        elif not is_relevant(title, desc,
                             watch_titles=tags.has(company, tags.WATCH)):
            return "division"
    return DEFER if deferred else OK


def judge(conn, company, jobs, tracks, mission_scorer=score_company_mission,
          *, cutoff):
    """Gates 1-6 for one company's rows against every applicable track.
    Returns {job_id: {track_label: verdict}} (verdict OK / gate / DEFER).
    Mission is decided once here; the per-track keyword focus is applied
    around each track's pass over the rows."""
    applicable = [t for t in tracks if _track_applies(t, company)]
    out = {j["job_id"]: {} for j in jobs}
    if not applicable:
        return out
    ensure_mission(conn, company, [j.get("title") for j in jobs[:8]],
                   scorer=mission_scorer)
    for t in applicable:
        mv = mission_verdict(company, t)
        if mv != OK:
            for j in jobs:
                out[j["job_id"]][t["track"]] = mv
            continue
        with _keyword_focus(t):
            for j in jobs:
                out[j["job_id"]][t["track"]] = row_verdict(company, j, t, cutoff)
    return out


def summarize(verdicts):
    """(row status, detail string, surfaced track labels) from one row's
    {track: verdict}. Any DEFER left means the row is still undecided.

    >>> summarize({"local-tech": "geo", "remote-neural": "title"})
    ('geo', 'local-tech=geo;remote-neural=title', [])
    >>> summarize({"local-tech": "ok", "remote-neural": "anchor"})
    ('ok', 'local-tech=ok;remote-neural=anchor', ['local-tech'])
    >>> summarize({"local-tech": "defer", "remote-neural": "title"})[0]
    'defer'
    >>> summarize({})
    ('mission', '', [])
    """
    if not verdicts:
        return "mission", "", []          # no track reads this company
    detail = ";".join(f"{k}={v}" for k, v in verdicts.items())
    surfaced = [k for k, v in verdicts.items() if v == OK]
    if surfaced:
        return OK, detail, surfaced
    if DEFER in verdicts.values():
        return DEFER, detail, []
    status = max(verdicts.values(), key=lambda v: _RANK.get(v, -1))
    return status, detail, []


# --------------------------------------------------------------------------- #
#  Hydration (survivors only) and scoring                                     #
# --------------------------------------------------------------------------- #

def _fetcher_shape(row, company):
    """A stored row as the job dict fetchers.company.hydrate_description
    expects, with the Workday handle rebuilt from the roster row (coords.
    wd_handle) so the CXS detail endpoint is used rather than the slow
    page fallback."""
    job = {"id": row["job_id"], "job_id": row["job_id"],
           "title": row.get("title") or "", "url": row.get("url") or "",
           "location": row.get("location") or "",
           "description": row.get("description") or "",
           "ats": company.get("ats"), "_row": row}
    job["_wd"] = coords.wd_handle(company, job["url"])
    return job


def hydrate_company(company, jobs, delay=None, backoff_s=MISS_BACKOFF_S,
                    progress=lambda: None):
    """Fetch bodies for one company's survivors, serially, within the
    harvester's per-host tolerances. Returns the harvest-style stats."""
    stats = {"hydrated": 0, "unhydrated": 0}
    if delay is None:
        delay = hydrate_delay(company.get("ats"))
    _hydrate_rows(jobs, company, stats, progress, delay, backoff_s)
    return stats


def _score_one(job):
    res = score_resume_fit(job.get("title") or "", job.get("description") or "",
                           location=job.get("location") or "")
    return job, res


# --------------------------------------------------------------------------- #
#  The pass                                                                    #
# --------------------------------------------------------------------------- #

def _by_company(conn, rows):
    """Rows grouped by company, plus the roster row for each.

    Both gate phases work per company, not per row: the mission gate is a
    company-level verdict and hydration is per host. The grouping itself
    is ops.group_by_company -- this module had reimplemented it, and the
    body gates a third time inline.
    """
    groups = ops.group_by_company(rows)
    return groups, {cid: store.get_company(conn, cid) for cid in groups}


def _judged(conn, companies, groups, tracks, mission_scorer, cutoff):
    """Yield (company, row, status, detail, surfaced) for every grouped row.

    The two gate phases differ only in what they DO with a verdict -- the
    asking is identical, and was written out twice.
    """
    for cid, rs in groups.items():
        c = companies[cid]
        verdicts = judge(conn, c, rs, tracks, mission_scorer=mission_scorer,
                         cutoff=cutoff)
        for r in rs:
            status, detail, surfaced = summarize(verdicts[r["job_id"]])
            yield c, r, status, detail, surfaced


def _free_gates(conn, companies, groups, tracks, mission_scorer, cutoff):
    """Phase 1: the free gates, on the text the rows already have.

    Returns (decided, survivors). A survivor is OK on some track, or
    DEFER -- deferred meaning "cannot be ruled on without a body", which
    is what phase 2 goes and fetches. `cutoff` is the RETRY_DAYS boundary
    _geo_verdict uses to give up on an unresolvable Workday location and
    decide the row right here instead of deferring it into phase 2.
    """
    decided, survivors = {}, {}
    for c, r, status, detail, _ in _judged(conn, companies, groups, tracks,
                                           mission_scorer, cutoff):
        if status in (OK, DEFER):
            survivors[r["job_id"]] = (c, r, status)
        else:
            decided[r["job_id"]] = (status, detail, c, r)
    print(f"  free gates: {len(decided)} dropped, {len(survivors)} survive")
    return decided, survivors


def _hydrate_order(survivors):
    """Sort key for one board's hydration batch: rows some track has already
    decided OK first, then rows whose TITLE alone already reads relevant
    (match.filters.is_relevant), then the rest.

    >>> key = _hydrate_order({"a": (None, None, OK), "b": (None, None, DEFER)})
    >>> key({"id": "a", "title": "T"})[0], key({"id": "b", "title": "T"})[0]
    (False, True)

    This only decides who goes FIRST -- an off-lane row is still hydrated if
    the per-host budget reaches it, and the free exclude gate
    ([exclude.<track>] title_tokens) is what keeps it from being fetched at
    all. Relevance is judged on the GLOBAL keyword lists: hydration runs
    outside _keyword_focus, and an ordering need not agree with any one
    track. Exercised end to end by tests/test_triage.py::
    test_hydration_spends_the_board_budget_on_relevant_titles_first.

    Notes:
        The per-host detail budget (harvest._hydrate_rows' cap and
        miss-streak breaker) used to be spent in arrival order within the
        decided/undecided split. At a multi-division employer only the
        division gate can refuse a chip-design seat, and that gate needs a
        body, so the budget went on rows guaranteed to drop: NVIDIA's
        waiting list on 2026-09-18 opened with "Senior ASIC Design
        Engineer", "Mask Design Engineer", "Memory Controller Verification
        Engineer".
    """
    def key(job):
        return (survivors[job["id"]][2] != OK,
                not is_relevant(job.get("title") or "", ""))
    return key


def _hydrate(conn, companies, survivors, summary, stamp, max_workers,
             hydrate_fn, cutoff):
    """Phase 2: resolve every survivor company_fetch.needs_detail still
    flags -- a missing body, or (Workday only) a body already but a
    location the listing never named (see needs_detail/hydrate_description).

    One worker per company (hydration is per host), rows already fully
    decided on some track first and, within that, rows whose TITLE already
    reads as relevant (match.filters.is_relevant) -- so a board's per-host
    cap is spent on the surest material. A row whose detail fetch failed
    recently is not retried (`cutoff`, the same RETRY_DAYS boundary
    _geo_verdict uses).

    Returns {job_id: reason} for every survivor this pass leaves still
    needing detail (skipped, or tried and failed): _body_gates names them.

    Notes:
        A row inside the retry window is never batched, so a pass whose
        only such rows are all inside it prints no "hydrating" line
        (2026-09-13 18:42: one nav link scraped from a JS-rendered careers
        page, re-fetched and failing every RETRY_DAYS since 2026-09-10).
    """
    todo, waiting = {}, {}
    for jid, (c, r, status) in survivors.items():
        job = _fetcher_shape(r, c)
        if not needs_detail(job):
            continue
        if not (r.get("url") or "").strip():
            waiting[jid] = "no URL to fetch a body from"
            continue
        checked = r.get("desc_checked_at") or ""
        if checked and not _detail_stale(r, cutoff):
            retry_at = datetime.fromisoformat(checked) + timedelta(days=RETRY_DAYS)
            waiting[jid] = (f"fetch failed {checked[:10]}, retries after "
                            f"{retry_at:%Y-%m-%d}")
            continue
        todo.setdefault(c["id"], []).append(job)
    if not todo:
        return waiting
    order = _hydrate_order(survivors)
    for js in todo.values():
        js.sort(key=order)
    n_todo = sum(len(js) for js in todo.values())
    print(f"  hydrating {n_todo} row(s) needing detail across "
          f"{len(todo)} board(s), {max_workers} at a time...")
    for cid, st in fan_out(
            list(todo), lambda cid: hydrate_fn(companies[cid], todo[cid]),
            lambda cid: f"{companies[cid]['name']}: hydrate",
            max_workers, with_item=True):
        summary["hydrated"] += st.get("hydrated", 0)
        for j in todo[cid]:
            r = survivors[j["id"]][1]
            if j.get("description"):
                # Persist at once: a row that ends this pass still pending
                # (over the cap, or deferred) must not be fetched again
                # next pass. Also covers a Workday row that only had its
                # LOCATION resolved this pass (description is unchanged
                # but still truthy), so the resolved location is kept too.
                r["description"] = j["description"]
                r["location"] = j.get("location") or r.get("location")
                store.store_body(conn, j["id"], r["description"],
                                 r["location"])
            if not needs_detail(j):
                continue                 # resolved (body, or just location)
            if j.get("_tried"):
                store.mark_desc_checked(conn, j["id"], now=stamp)
                waiting[j["id"]] = "fetch failed this pass"
            else:
                # Never reached hydrate_description at all: the board's
                # per-host cap or miss-streak breaker (harvest._hydrate_rows)
                # cut it from this pass's batch.
                waiting[j["id"]] = "not reached this pass (board hydrate cap/pause)"
    print(f"  hydrated {summary['hydrated']} of {n_todo}")
    return waiting


def _body_gates(conn, companies, survivors, tracks, mission_scorer, decided,
                summary, n_free, waiting, cutoff):
    """Phase 3: the same gates again, now with bodies.

    Returns the rows to score. A survivor still without a body -- DEFER,
    or clear on every gate but unfetchable -- stays pending for the next
    pass rather than entering a track unscorable. `waiting` is _hydrate's
    {job_id: reason}, printed (capped) against the rows that end up here.
    """
    final, left_rows = {}, []
    # The survivor rows carry company_id themselves, so the same grouping
    # helper works here without rebuilding the (company, row) pairs.
    groups = ops.group_by_company([r for _, r, _ in survivors.values()])
    for c, r, status, detail, surfaced in _judged(conn, companies, groups,
                                                  tracks, mission_scorer,
                                                  cutoff):
        if status == OK and (r.get("description") or "").strip():
            final[r["job_id"]] = (c, r, surfaced, detail)
        elif status in (OK, DEFER):
            summary["left"] += 1
            left_rows.append((c, r))
        else:
            decided[r["job_id"]] = (status, detail, c, r)
    print(f"  body gates: {len(decided) - n_free} more dropped, "
          f"{len(final)} to score, {summary['left']} still waiting on a body")
    _print_waiting(left_rows, waiting)
    return final


def _print_waiting(rows, reasons):
    """Name every row left "still waiting on a body" and why, capped at
    _WAITING_CAP so a whole-board pass doesn't flood the log. `reasons`
    is _hydrate's {job_id: reason}; a row missing from it was never even
    considered this pass (hydrate=False)."""
    for c, r in rows[:_WAITING_CAP]:
        reason = reasons.get(r["job_id"], "hydration skipped this pass")
        print(f"    waiting: {c.get('name')} | {r.get('title') or ''} | "
              f"{r.get('location') or ''} | {reason}")
    extra = len(rows) - _WAITING_CAP
    if extra > 0:
        print(f"    ... and {extra} more")


def _score(final, summary, score_cap, fit, max_workers):
    """Phase 4: the only paid step, best companies first, under the cap.

    Returns (scores, over_cap). Rows over the cap stay pending -- their
    bodies are stored now, so the next pass reaches them for free. A row
    the scorer refuses for a body under fit.MIN_DESC_CHARS (its SKIP-SCORE
    print) is counted in summary['skip_score'].
    """
    order = sorted(final.values(),
                   key=lambda x: -(x[0].get("mission_score") or 0.0))
    to_score = order[:score_cap] if fit else []
    over_cap = {x[1]["job_id"] for x in order[score_cap:]} if fit else set()
    scores = {}
    if to_score:
        print(f"  scoring {len(to_score)} survivor(s) against the profile"
              + (f" ({len(over_cap)} over the {score_cap}/pass cap wait "
                 f"for the next pass)" if over_cap else "") + "...")
        for r, res in fan_out([x[1] for x in to_score], _score_one,
                              "scoring", max(2, min(max_workers, 6))):
            if res.score is not None:
                scores[r["job_id"]] = res
                summary["scored"] += 1
            elif len((r.get("description") or "").strip()) < MIN_DESC_CHARS:
                summary["skip_score"] += 1
    return scores, over_cap


def _write_verdicts(conn, decided, final, scores, over_cap, tracks, summary,
                    stamp):
    """Phase 5: one batch, every verdict.

    A survivor the scorer could not reach (no key, breaker tripped) is
    still stamped into its tracks unscored: the crawl's self-heal scores
    NULL-score tracked rows that have a body.

    The one place that sees every verdict, so it is also the one that logs
    every drop (DEBUG "drop <gate> | <company> | <title> | <location> |
    <triage_detail>") and prints every score ("score <n> <surfaced|fit> |
    <company> | <title> | <location> | <fit reason>" -- "surfaced" IS the
    OK status here).

    The drop record's three free-text fields (company, title, location)
    are run through net.util.clean_field first: it is the one line in the
    run that packs five fields onto one unquoted "|"-joined line, and a
    literal newline in any of them splits it into fragments a session-log
    reader cannot tell from a second record. The fetchers clean these at
    write time too (board.board_jobs), but a row already dirty in the
    store reaches this line however it got there.

    Notes:
        124 open rows carried a newline or tab in a title or location in
        the 2026-09-18 audit ("Calibration | local-tech=title" printed on a
        line of its own). The score line used to print "ok" plus a
        redundant "[SURFACED]" tag, two names for one fact.
    """
    with store.batch(conn):
        for jid, (status, detail, c, r) in decided.items():
            store.record_triage(conn, jid, status, detail, now=stamp)
            summary[status] += 1
            _log.debug("drop %s | %s | %s | %s | %s", status,
                      clean_field(c.get("name")), clean_field(r.get("title")),
                      clean_field(r.get("location")), detail)
        for jid, (c, r, surfaced, detail) in final.items():
            if jid in over_cap:
                summary["left"] += 1
                continue
            res = scores.get(jid)
            floors = [t["digest_min_fit"] for t in tracks
                      if t["track"] in surfaced]
            status = OK
            if res is not None and floors and res.score < min(floors):
                status = "fit"
            loc, desc = r.get("location") or "", r.get("description") or ""
            if res is not None:
                label = "surfaced" if status == OK else status
                print(f"  score {res.score:.2f} {label} | {c.get('name')} | "
                      f"{r.get('title') or ''} | {loc} | {res.summary()}")
            store.record_triage(
                conn, jid, status, detail, tracks=surfaced, description=desc,
                geo_mode=geo_mode(loc, desc),
                remote_signal=remote_signal_for(
                    {"location": loc, "description": desc}),
                scores=res.as_columns() if res is not None else None,
                now=stamp)
            summary["surfaced" if status == OK else status] += 1


def _print_summary(summary, bar):
    """The funnel, as one line per pass, for whoever reads the session log.

    A row the scorer skips for a short body (fit.MIN_DESC_CHARS) is still
    surfaced (it keeps its track label unscored -- see _score/_write_verdicts),
    so it is already inside `summary['surfaced']`; skip_score names how many
    of that total were unscored rather than adding a second, overlapping
    count next to it ("3 surfaced (1 unscored: short body)", not "3
    surfaced, 1 skip-score" reading as 4 distinct rows).
    """
    gate_bits = ", ".join(f"{summary[g]} {g}" for g in store.TRIAGE_GATES
                          if summary[g])
    skipped = summary["skip_score"]
    surfaced_bit = (f"{summary['surfaced']} surfaced"
                    + (f" ({skipped} unscored: short body)" if skipped else ""))
    print(f"\n{bar}\n  TRIAGE SUMMARY")
    print(f"  pending {summary['pending']}: dropped [{gate_bits or 'none'}]; "
          f"{summary['hydrated']} hydrated, {summary['scored']} scored, "
          f"{surfaced_bit}"
          + (f", {summary['left']} left for next pass" if summary["left"] else ""))
    print(f"  time:   {summary['secs'] / 60:.1f} min\n{bar}")


def run(db_path=None, tracks=None, limit=None, max_workers=DEFAULT_WORKERS,
        score_cap=SCORE_CAP, fit=True, hydrate=True,
        mission_scorer=score_company_mission, hydrate_fn=hydrate_company,
        now=None, requeue=False, requeue_apply=False):
    """Triage every pending row in the store. Returns the summary dict
    (also printed): harvested/pending N, then a count per gate, hydrated,
    scored, surfaced, and how many rows are left pending.

    Five phases, in cost order, each one working on what the last left:

      1. _free_gates     the gates that cost nothing, on stored text
      2. _hydrate        one detail GET per surviving row needing detail
      3. _body_gates     the same gates again, now with bodies
      4. _score          the only paid step, capped per pass
      5. _write_verdicts one batch, every verdict

    `limit` caps the rows read this pass; `score_cap` the Claude fit calls;
    `fit=False` stamps survivors unscored (the crawl's self-heal scores
    them later); `hydrate=False` leaves undetailed survivors pending.
    `mission_scorer` / `hydrate_fn` exist for tests.

    `requeue=True` skips all five phases and instead runs `requeue_rows`
    (report-only unless `requeue_apply=True` too) -- see its docstring for
    the full re-queue contract. A row it resets is picked up by the NEXT
    plain call to `run` (this one does not re-judge it itself): that is
    what makes the interface a two-step, reviewable repair rather than a
    silent rewrite.
    """
    if requeue:
        return requeue_rows(db_path=db_path, apply=requeue_apply,
                            tracks=tracks)
    db_path = db_path or config.STORE_DB_PATH
    tracks = roster_tracks(tracks)
    with closing(store.connect(db_path)) as conn:
        rows = store.triage_pending(conn, limit=limit)
        bar = "=" * 70
        print(f"\n{bar}\n  [TRIAGE] harvested rows -> gates -> hydrate -> score "
              f"- {datetime.now():%Y-%m-%d %H:%M}")
        print(f"  {len(rows)} pending row(s), {len(tracks)} track(s): "
              f"{', '.join(t['id'] for t in tracks)}\n{bar}\n")
        summary = {"pending": len(rows), **{g: 0 for g in store.TRIAGE_GATES},
                   "hydrated": 0, "scored": 0, "surfaced": 0, "left": 0,
                   "skip_score": 0, "secs": 0.0}
        if not rows or not tracks:
            print("  nothing to do")
            return summary
        t0 = time.monotonic()
        stamp = now or datetime.now()
        cutoff = (stamp - timedelta(days=RETRY_DAYS)).isoformat()

        groups, companies = _by_company(conn, rows)
        decided, survivors = _free_gates(conn, companies, groups, tracks,
                                         mission_scorer, cutoff)
        n_free = len(decided)
        waiting = {}
        if hydrate:
            waiting = _hydrate(conn, companies, survivors, summary, stamp,
                               max_workers, hydrate_fn, cutoff)
        final = _body_gates(conn, companies, survivors, tracks, mission_scorer,
                            decided, summary, n_free, waiting, cutoff)
        scores, over_cap = _score(final, summary, score_cap, fit, max_workers)
        _write_verdicts(conn, decided, final, scores, over_cap, tracks, summary,
                        stamp)

    summary["secs"] = time.monotonic() - t0
    _print_summary(summary, bar)
    return summary


# --------------------------------------------------------------------------- #
#  Re-queue: rows an earlier rule judged before it could resolve them right    #
# --------------------------------------------------------------------------- #
#
# record_triage's own COALESCE columns and triage_pending's WHERE clause
# both exist so a row triage has already judged is never looked at again --
# correct for every ordinary pass, and wrong the one time the RULE itself
# was the bug being fixed. Two such bugs, both still sitting in stored
# verdicts the day this was written: 2,000 open rows carry a Workday "<N>
# Locations" placeholder, and 527 of them were dropped at the geo gate
# anyway by the strict body-regex fallback this module used to reach
# immediately (NVIDIA 338, J&J 110, IQVIA 61, Merck 57, ICON 44); and 86
# more rows carry a triage_status of 'ok'/'fit' although their stored
# location names no configured-local place at all (Stryker "Cary,
# Illinois", GSK "UK - County Durham - Barnard Castle", ICON "US, Blue
# Bell (ICON); Canada, Burlington", J&J "Raynham, Massachusetts",
# QuidelOrtho "US - CA - San Diego") -- locality.is_nc's per-segment
# tightening (see its own docstring) now catches these, but a row already
# marked 'ok' never runs through is_nc again on its own.
#
# This is the one-time repair for both: report what it would touch, and
# only touch the store when told to.

# requeue_reasons()'s two reasons, in the order it checks them.
REQUEUE_GEO_UNKNOWN = "geo:unknown-location"
REQUEUE_NON_LOCAL = "geo:non-local"


def _passed_tracks(detail):
    """The track labels a triage_detail string records as passed.

    >>> sorted(_passed_tracks("a=ok;b=geo;c=ok"))
    ['a', 'c']
    >>> _passed_tracks(None)
    set()
    """
    pairs = (p.split("=", 1) for p in (detail or "").split(";") if "=" in p)
    return {k for k, v in pairs if v == OK}


def requeue_reasons(conn, tracks=None):
    """{job_id: {"reason", "company_name", "title", "location"}} for every
    open triaged row whose verdict the current geo rules would change.
    Both reasons select on `triage_status` (ix_jobs_triage), so a row a
    crawl adopted, which has none, is never selected; a closed row is left
    as it is, since triage never re-judges one:

      REQUEUE_GEO_UNKNOWN  a 'geo' drop on a Workday board whose location
                           is still location_unknown (only a Workday row
                           has a location lookup that can now name it);
      REQUEUE_NON_LOCAL    an 'ok'/'fit' row whose location names a place
                           that fails is_nc and remote_signal, and that
                           passed only geo-gated tracks (`tracks`, default
                           roster_tracks()).

    >>> conn = store.connect(":memory:")
    >>> cid = store.upsert_company(conn, {"name": "Acme", "ats": "workday",
    ...                                   "wd_tenant": "acme"})
    >>> for jid, loc in [("a", "2 Locations"), ("b", "Ulaanbaatar, Mongolia"),
    ...                  ("c", "Ulaanbaatar, Mongolia")]:
    ...     _ = store.upsert_job(conn, {"job_id": jid, "company_id": cid,
    ...                                 "company_name": "Acme", "title": "T",
    ...                                 "location": loc})
    >>> store.record_triage(conn, "a", "geo", "t=geo")
    >>> store.record_triage(conn, "b", "ok", "t=ok", tracks=["t"])
    >>> store.record_triage(conn, "c", "ok", "t=ok;s=ok", tracks=["t", "s"])
    >>> tracks = [{"track": "t", "geo_gate": True, "sources": {"store": True}},
    ...           {"track": "s", "geo_gate": False, "sources": {"store": True}}]
    >>> {jid: v["reason"] for jid, v in requeue_reasons(conn, tracks).items()}
    {'a': 'geo:unknown-location', 'b': 'geo:non-local'}

    Notes:
        Row "c" keeps its labels: the non-geo track passed it on its own
        merits, and the geo-gated track's ranking (store.ranked_jobs with
        NC_RE) already leaves a non-local row out of that list. A row
        whose location is unknown passed on the body rule, which has not
        changed, so it is not selected either.
    """
    gated = {t["track"] for t in roster_tracks(tracks) if t["geo_gate"]}
    out = {}

    def add(r, reason):
        out[r["job_id"]] = {"reason": reason, "company_name": r["company_name"],
                            "title": r["title"], "location": r["location"]}

    for r in conn.execute(
            "SELECT j.job_id, j.company_name, j.title, j.location "
            "FROM jobs j JOIN companies c ON c.id = j.company_id "
            "WHERE j.triage_status='geo' AND c.ats='workday' "
            "AND COALESCE(j.status,'open') != 'closed'"):
        if location_unknown(r["location"]):
            add(r, REQUEUE_GEO_UNKNOWN)
    for r in conn.execute(
            "SELECT job_id, company_name, title, location, triage_detail "
            "FROM jobs WHERE triage_status IN ('ok','fit') "
            "AND COALESCE(status,'open') != 'closed'"):
        loc = r["location"]
        passed = _passed_tracks(r["triage_detail"])
        if (location_unknown(loc) or is_nc(loc) or remote_signal(loc)
                or not passed or not passed <= gated):
            continue
        add(r, REQUEUE_NON_LOCAL)
    return out


def requeue_rows(db_path=None, apply=False, sample=10, tracks=None):
    """Report (the default) or apply a re-queue of rows `requeue_reasons`
    flags -- the CLI/registry surface (run_scraper.py --triage --requeue
    [--requeue-apply], the registry `triage` op's same two params).

    Contract:
      * SELECTION -- exactly requeue_reasons(conn, tracks).
      * RESET (apply=True only) -- store.clear_triage on each row: every
        column triage wrote goes back to NULL (the body and the detail
        retry clock stay), which is the blank slate store.triage_pending
        selects.
      * A row that loses its geo pass this way therefore carries NO track
        label afterwards, so it cannot appear in any ranking or digest
        until a later `run` re-judges it and it earns one back on merit.
      * This function never re-judges a row itself -- it only clears the
        old verdict. The NEXT plain `run` (this pass, or the next
        scheduled one) is what puts a reset row back through the gates.
      * DRY-RUN BY DEFAULT: apply=False only prints counts per reason and
        a sample of up to `sample` rows ("company | title | location");
        apply=True is the only thing that writes, in one batch.
      * UNDO -- there is none built in: a reset overwrites the old
        verdict/track/score columns in place. The next `run` almost
        always leaves the row no worse off (it earns back whatever the
        gates still agree with), but if a snapshot is wanted first, copy
        the SQLite file (data/jobs.db) before passing --requeue-apply.

    Returns {"counts": {reason: n}, "requeued": n if applied else 0}.
    """
    db_path = db_path or config.STORE_DB_PATH
    with closing(store.connect(db_path)) as conn:
        found = requeue_reasons(conn, tracks)
        counts = {}
        for v in found.values():
            counts[v["reason"]] = counts.get(v["reason"], 0) + 1
        verb = "requeued" if apply else "would requeue"
        print(f"  {verb} {len(found)} row(s): "
              + (", ".join(f"{n} {reason}" for reason, n in sorted(counts.items()))
                 or "none"))
        for jid, v in list(found.items())[:sample]:
            print(f"    {v['company_name']} | {v['title']} | {v['location'] or ''}")
        if len(found) > sample:
            print(f"    ... and {len(found) - sample} more")
        if apply:
            with store.batch(conn):
                for jid in found:
                    store.clear_triage(conn, jid)
    return {"counts": counts, "requeued": len(found) if apply else 0}
