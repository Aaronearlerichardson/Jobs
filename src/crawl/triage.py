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
               the body (needs one).
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
from contextlib import contextmanager
from datetime import datetime, timedelta

from src import config
from src import store
from src import tags
from src.ats import coords
# Workday's "N Locations" placeholder: the real list only comes with
# the detail JSON (fetchers.company.hydrate_description fixes the
# field). The pattern is the fetcher's to own; triage had a copy.
from src.ats.fetchers.workday import N_LOCATIONS_RE
from src.claude.api import is_active_mission, score_company_mission
from src.claude.fit import score_resume_fit
from src.crawl import harvest
from src.crawl.harvest import MISS_BACKOFF_S, _hydrate_rows, hydrate_delay
from src.crawl.runner import apply_keyword_focus, core_anchor
from src.match import gates
from src.match.filters import is_relevant
from src.match.locality import (NC_HQ_RE, geo_mode, is_nc, remote_signal,
                                remote_signal_for)
from src.net.parallel import fan_out
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
# A body fetch that failed this recently is not retried (the backfill ops'
# convention); the row stays pending until then.
RETRY_DAYS = 3
# Location strings that say nothing: empty, or the custom-board scrapers'
# default when no place was found (src.match.locality.location_snippet).
_UNKNOWN_LOC = {"", "see posting"}
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
        tier, score, reason = scorer(company.get("name") or "",
                                     " | ".join(t for t in titles if t)[:1500])
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

def _location_unknown(location):
    """The listing did not really say where: empty, the custom-board
    placeholder, or Workday's "N Locations" (the detail page names them)."""
    loc = (location or "").strip().lower()
    return loc in _UNKNOWN_LOC or bool(N_LOCATIONS_RE.match(loc))


def _geo_verdict(company, job, t, has_body):
    """Geography on the location FIELD (see the module docstring, gate 4):
    local passes anywhere; remote passes at a watched or mission-trusted
    company; the body is consulted only when the listing named no place.
    Stricter than the crawl's whole-board path (ops._keep_job, which lets
    geo_mode read the body): in the 2026-09-10 dry run the loose body
    match called 83 of 130 survivors local ("Garner", "apex", "NC" as
    nonconformance, HQ boilerplate) and 15 non-local rows remote."""
    loc = job.get("location") or ""
    desc = job.get("description") or ""
    floor = t.get("remote_mission_floor")
    trusted = ops._is_watched(company) or ops._mission_trusted(company, floor)
    unknown = _location_unknown(loc)
    if is_nc(loc):
        return OK
    if trusted and (remote_signal(loc) or job.get("remote_hint")):
        return OK
    if has_body and unknown:
        # The listing named no place: the body is all there is. Onsite
        # needs the strict "<place>, ST" form; remote (trusted companies)
        # locality's workforce phrases.
        if NC_HQ_RE.search(desc) or (trusted and remote_signal("", desc)):
            return OK
    if not has_body and unknown:
        return DEFER                    # the detail page may name the place
    return "geo"


def row_verdict(company, job, t):
    """Gates 2-6 for one row on one track, on the text the row has NOW.
    Returns OK, a gate name, or DEFER (undecidable without a body). The
    caller has already applied the track's keyword focus."""
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
        v = _geo_verdict(company, job, t, has_body)
        if v == "geo":
            return "geo"
        deferred = deferred or v == DEFER
    if t["exclude_gate"] and gates.exclude_reason(
            title, desc, allow_defense=ops._is_watched(company),
            track_id=t["id"]):
        return "exclude"
    if config.is_multi_division(company.get("name")):
        if not has_body:
            deferred = True
        elif not is_relevant(title, desc):
            return "division"
    return DEFER if deferred else OK


def judge(conn, company, jobs, tracks, mission_scorer=score_company_mission):
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
                out[j["job_id"]][t["track"]] = row_verdict(company, j, t)
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
    res = score_resume_fit(job.get("title") or "", job.get("description") or "")
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


def _judged(conn, companies, groups, tracks, mission_scorer):
    """Yield (company, row, status, detail, surfaced) for every grouped row.

    The two gate phases differ only in what they DO with a verdict -- the
    asking is identical, and was written out twice.
    """
    for cid, rs in groups.items():
        c = companies[cid]
        verdicts = judge(conn, c, rs, tracks, mission_scorer=mission_scorer)
        for r in rs:
            status, detail, surfaced = summarize(verdicts[r["job_id"]])
            yield c, r, status, detail, surfaced


def _free_gates(conn, companies, groups, tracks, mission_scorer):
    """Phase 1: the free gates, on the text the rows already have.

    Returns (decided, survivors). A survivor is OK on some track, or
    DEFER -- deferred meaning "cannot be ruled on without a body", which
    is what phase 2 goes and fetches.
    """
    decided, survivors = {}, {}
    for c, r, status, detail, _ in _judged(conn, companies, groups, tracks,
                                           mission_scorer):
        if status in (OK, DEFER):
            survivors[r["job_id"]] = (c, r, status)
        else:
            decided[r["job_id"]] = (status, detail, [])
    print(f"  free gates: {len(decided)} dropped, {len(survivors)} survive")
    return decided, survivors


def _hydrate(conn, companies, survivors, summary, stamp, max_workers,
             hydrate_fn):
    """Phase 2: fetch a body for every survivor that lacks one.

    One worker per company (hydration is per host), rows already fully
    decided on some track first, so a board's per-host cap is spent on the
    surest material. A row that failed recently is not retried.
    """
    todo = {}
    cutoff = (stamp - timedelta(days=RETRY_DAYS)).isoformat()
    for jid, (c, r, status) in survivors.items():
        if (r.get("description") or "").strip():
            continue
        if (r.get("desc_checked_at") or "") >= cutoff:
            continue
        todo.setdefault(c["id"], []).append(_fetcher_shape(r, c))
    if not todo:
        return
    for cid, js in todo.items():
        js.sort(key=lambda j: survivors[j["id"]][2] != OK)
    n_todo = sum(len(js) for js in todo.values())
    print(f"  hydrating {n_todo} bodiless survivor(s) across "
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
                # next pass.
                r["description"] = j["description"]
                r["location"] = j.get("location") or r.get("location")
                store.store_body(conn, j["id"], r["description"],
                                 r["location"])
            elif j.get("_tried"):
                store.mark_desc_checked(conn, j["id"], now=stamp)
    print(f"  hydrated {summary['hydrated']} of {n_todo}")


def _body_gates(conn, companies, survivors, tracks, mission_scorer, decided,
                summary, n_free):
    """Phase 3: the same gates again, now with bodies.

    Returns the rows to score. A survivor still without a body -- DEFER,
    or clear on every gate but unfetchable -- stays pending for the next
    pass rather than entering a track unscorable.
    """
    final = {}
    # The survivor rows carry company_id themselves, so the same grouping
    # helper works here without rebuilding the (company, row) pairs.
    groups = ops.group_by_company([r for _, r, _ in survivors.values()])
    for c, r, status, detail, surfaced in _judged(conn, companies, groups,
                                                  tracks, mission_scorer):
        if status == OK and (r.get("description") or "").strip():
            final[r["job_id"]] = (c, r, surfaced, detail)
        elif status in (OK, DEFER):
            summary["left"] += 1
        else:
            decided[r["job_id"]] = (status, detail, [])
    print(f"  body gates: {len(decided) - n_free} more dropped, "
          f"{len(final)} to score, {summary['left']} still waiting on a body")
    return final


def _score(final, summary, score_cap, fit, max_workers):
    """Phase 4: the only paid step, best companies first, under the cap.

    Returns (scores, over_cap). Rows over the cap stay pending -- their
    bodies are stored now, so the next pass reaches them for free.
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
    return scores, over_cap


def _write_verdicts(conn, decided, final, scores, over_cap, tracks, summary,
                    stamp):
    """Phase 5: one batch, every verdict.

    A survivor the scorer could not reach (no key, breaker tripped) is
    still stamped into its tracks unscored: the crawl's self-heal scores
    NULL-score tracked rows that have a body.
    """
    with store.batch(conn):
        for jid, (status, detail, _) in decided.items():
            store.record_triage(conn, jid, status, detail, now=stamp)
            summary[status] += 1
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
            store.record_triage(
                conn, jid, status, detail, tracks=surfaced, description=desc,
                geo_mode=geo_mode(loc, desc),
                remote_signal=remote_signal_for(
                    {"location": loc, "description": desc}),
                scores=res.as_columns() if res is not None else None,
                now=stamp)
            summary["surfaced" if status == OK else status] += 1


def _print_summary(summary, bar):
    """The funnel, as one line per pass, for whoever reads the session log."""
    gate_bits = ", ".join(f"{summary[g]} {g}" for g in store.TRIAGE_GATES
                          if summary[g])
    print(f"\n{bar}\n  TRIAGE SUMMARY")
    print(f"  pending {summary['pending']}: dropped [{gate_bits or 'none'}]; "
          f"{summary['hydrated']} hydrated, {summary['scored']} scored, "
          f"{summary['surfaced']} surfaced"
          + (f", {summary['left']} left for next pass" if summary["left"] else ""))
    print(f"  time:   {summary['secs'] / 60:.1f} min\n{bar}")


def run(db_path=None, tracks=None, limit=None, max_workers=DEFAULT_WORKERS,
        score_cap=SCORE_CAP, fit=True, hydrate=True,
        mission_scorer=score_company_mission, hydrate_fn=hydrate_company,
        now=None):
    """Triage every pending row in the store. Returns the summary dict
    (also printed): harvested/pending N, then a count per gate, hydrated,
    scored, surfaced, and how many rows are left pending.

    Five phases, in cost order, each one working on what the last left:

      1. _free_gates     the gates that cost nothing, on stored text
      2. _hydrate        one detail GET per surviving bodiless row
      3. _body_gates     the same gates again, now with bodies
      4. _score          the only paid step, capped per pass
      5. _write_verdicts one batch, every verdict

    `limit` caps the rows read this pass; `score_cap` the Claude fit calls;
    `fit=False` stamps survivors unscored (the crawl's self-heal scores
    them later); `hydrate=False` leaves bodiless survivors pending.
    `mission_scorer` / `hydrate_fn` exist for tests.
    """
    db_path = db_path or config.STORE_DB_PATH
    tracks = roster_tracks(tracks)
    conn = store.connect(db_path)
    rows = store.triage_pending(conn, limit=limit)
    bar = "=" * 70
    print(f"\n{bar}\n  [TRIAGE] harvested rows -> gates -> hydrate -> score "
          f"- {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  {len(rows)} pending row(s), {len(tracks)} track(s): "
          f"{', '.join(t['id'] for t in tracks)}\n{bar}\n")
    summary = {"pending": len(rows), **{g: 0 for g in store.TRIAGE_GATES},
               "hydrated": 0, "scored": 0, "surfaced": 0, "left": 0,
               "secs": 0.0}
    if not rows or not tracks:
        conn.close()
        print("  nothing to do")
        return summary
    t0 = time.monotonic()
    stamp = now or datetime.now()

    groups, companies = _by_company(conn, rows)
    decided, survivors = _free_gates(conn, companies, groups, tracks,
                                     mission_scorer)
    n_free = len(decided)
    if hydrate:
        _hydrate(conn, companies, survivors, summary, stamp, max_workers,
                 hydrate_fn)
    final = _body_gates(conn, companies, survivors, tracks, mission_scorer,
                        decided, summary, n_free)
    scores, over_cap = _score(final, summary, score_cap, fit, max_workers)
    _write_verdicts(conn, decided, final, scores, over_cap, tracks, summary,
                    stamp)
    conn.close()

    summary["secs"] = time.monotonic() - t0
    _print_summary(summary, bar)
    return summary
