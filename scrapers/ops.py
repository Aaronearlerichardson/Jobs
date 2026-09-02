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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import config

from core import digest_md, gates, store, tags
from core.claude import score_resume_fit
from .fetchers import company as company_fetch
from core.filters import is_relevant
from core.locality import NC_RE, geo_mode
from .parallel import fetch_all
from core.resume import resume_text


def _default_track():
    from .runner import track_for_engine
    return track_for_engine("local")


def _t(t):
    return t if t is not None else _default_track()


def _ranked(conn, t, limit=None):
    """The track's ranked view — same knobs the crawl digest uses."""
    return store.ranked_jobs(
        conn, track=t["track"],
        location_re=(NC_RE if t["geo_gate"] else None),
        rank_by=t["rank_by"], allow_geo_modes={"remote"},
        min_mission=t["min_mission"],
        remote_mission_floor=t.get("remote_mission_floor"), limit=limit)


# --------------------------------------------------------------------------- #
#  Company-tag helpers (store roster semantics, shared by crawl + ops).        #
# --------------------------------------------------------------------------- #

def _is_sweep_tagged(company):
    """True if a company store row carries the 'sweep' scope tag (core/tags.py
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
        if not is_relevant(title, job.get("description", "")):
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


def _score_job(resume, company, job, track):
    company_fetch.hydrate_description(job)
    res = score_resume_fit(resume, job["title"], job.get("description", ""))
    return {
        "job_id": job["id"], "company_id": company["id"], "company_name": company["name"],
        "title": job["title"], "url": job["url"], "location": job["location"],
        "track": track,
        "geo_mode": geo_mode(job["location"], job.get("description", "")) or "onsite",
        "description": (job.get("description", "") or "")[:config.MAX_DESC_CHARS],
        "posted_at": job.get("posted_at"),
        **res.as_columns(),
    }


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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_score_job, resume, company, j, t["track"])
                for j in fresh]
        for fut in as_completed(futs):
            try:
                store.upsert_job(conn, fut.result())
                n_new += 1
            except Exception as e:
                print(f"    [!] scoring error: {e}")
    return (len(jobs), len(kept), n_new)


def self_heal_unscored(conn, resume, track, max_workers=6):
    """Self-heal: the fresh-only crawl loop never revisits an already-stored
    job, so a row that was ingested bodyless (unscorable -> NULL score) would
    stay out of the ranking forever even once its description is recovered.
    Score any NULL-score row that now carries a real body (hydrated by
    backfill_board_descriptions, or by an earlier run). Returns #scored."""
    from core.fit import MIN_DESC_CHARS
    _ph = ",".join("?" for _ in store.RANKING_EXCLUDED_DISPOSITIONS)
    pending = [dict(r) for r in conn.execute(
        "SELECT job_id, title, description FROM jobs "
        "WHERE (',' || COALESCE(track,'') || ',') LIKE ? "
        "AND resume_fit_score IS NULL "
        "AND COALESCE(status,'open') != 'closed' "
        f"AND (disposition IS NULL OR disposition NOT IN ({_ph})) "
        "AND length(COALESCE(description,'')) >= ?",
        (f"%,{track},%", *store.RANKING_EXCLUDED_DISPOSITIONS,
         MIN_DESC_CHARS)).fetchall()]
    if not pending:
        return 0
    print(f"  self-heal: scoring {len(pending)} newly-described "
          f"job(s) that were previously unscorable...")
    scored = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(score_resume_fit, resume, r["title"],
                          r.get("description", "")): r for r in pending}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"    [!] self-heal scoring error: {e}")
                continue
            if res.score is not None:
                store.update_job_scores(conn, r["job_id"], res.as_columns())
                scored += 1
    return scored


# --------------------------------------------------------------------------- #
#  Maintenance operations (webapp OPS + run_scraper flags).                     #
# --------------------------------------------------------------------------- #

def backfill_board_descriptions(max_workers=8, limit=None, min_len=200,
                                t=None, retry_days=3):
    """One-shot: fill in full JD text for stored jobs missing it (any
    company-linked row whose description is shorter than min_len chars —
    the default matches core.fit.MIN_DESC_CHARS), via each company's
    own ATS board. Batched per company so a board with several stale rows is
    fetched once. Safe to re-run: a row that failed within the last
    `retry_days` days is skipped (its desc_checked_at stamp), so reruns
    don't re-fetch every board to fail on the same vanished postings
    (retry_days=0 retries everything)."""
    t = _t(t)
    conn = store.connect(t["db_path"])
    cutoff = ((datetime.now() - timedelta(days=retry_days)).isoformat()
              if retry_days else "9999")
    rows = [dict(r) for r in conn.execute(
        "SELECT job_id, title, url, company_id, desc_checked_at FROM jobs "
        "WHERE company_id IS NOT NULL "
        "AND COALESCE(status,'open') != 'closed' "
        "AND length(COALESCE(description,'')) < ?", (min_len,)).fetchall()]
    recent = [r for r in rows if (r.get("desc_checked_at") or "") >= cutoff]
    rows = [r for r in rows if (r.get("desc_checked_at") or "") < cutoff]
    if limit:
        rows = rows[:int(limit)]
    print(f"  backfilling {len(rows)} description(s) via company board(s)..."
          + (f" ({len(recent)} skipped: failed in the last {retry_days}d)"
             if recent else ""))

    by_company = {}
    for r in rows:
        by_company.setdefault(r["company_id"], []).append(r)

    n = 0
    for cid, rs in by_company.items():
        company = store.get_company(conn, cid)
        if not company or not company.get("ats"):
            # No board to fetch IS a failed attempt — stamp these rows too,
            # or they are re-selected (and silently re-counted) every run
            # while never even printing a company line.
            for r in rs:
                conn.execute("UPDATE jobs SET desc_checked_at=? "
                             "WHERE job_id=?",
                             (datetime.now().isoformat(), r["job_id"]))
            conn.commit()
            continue
        # Batched board pull for the common case (one fetch per company);
        # boards we can't pull simply yield no title matches, and each row
        # falls through to per-job-URL hydration below.
        try:
            board = company_fetch.fetch_company(company, loc_re=None)
        except Exception as e:
            print(f"    [!] {company['name']}: {e}")
            board = []
        by_title = {(b.get("title") or "").strip().lower(): b for b in board}
        n_matched = 0
        for r in rs:
            desc = None
            match = by_title.get((r["title"] or "").strip().lower())
            if match is not None:
                company_fetch.hydrate_description(match)
                desc = match.get("description")
            if not desc and r.get("url"):
                # Board didn't cover this row — hydrate from the job's own
                # detail page (JSON-LD / career-site markup).
                stub = {"title": r["title"], "url": r["url"],
                        "ats": company.get("ats"), "description": ""}
                company_fetch.hydrate_description(stub)
                desc = stub.get("description")
            if not desc:
                conn.execute("UPDATE jobs SET desc_checked_at=? "
                             "WHERE job_id=?",
                             (datetime.now().isoformat(), r["job_id"]))
                conn.commit()
                continue
            conn.execute("UPDATE jobs SET description=? WHERE job_id=?",
                         (desc[:config.MAX_DESC_CHARS], r["job_id"]))
            conn.commit()
            n += 1
            n_matched += 1
        print(f"    {company['name']:30} {len(rs):2} stale -> {n_matched:2} matched")
    conn.close()
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
    spend on postings that can't surface anyway."""
    from core.fit import MIN_DESC_CHARS
    t = _t(t)
    resume = resume_text()
    if not resume:
        print("  [!] No resume text - cannot rescore. Set config.RESUME_PATH.")
        return 0
    conn = store.connect(t["db_path"])
    ph = ",".join("?" for _ in store.RANKING_EXCLUDED_DISPOSITIONS)
    conds = ["COALESCE(status,'open') != 'closed'",
             f"(disposition IS NULL OR disposition NOT IN ({ph}))"]
    args = list(store.RANKING_EXCLUDED_DISPOSITIONS)
    if track:
        conds.append("(',' || COALESCE(track,'') || ',') LIKE ?")
        args.append(f"%,{track},%")
    if described_only:
        conds.append("length(COALESCE(description,'')) >= ?")
        args.append(MIN_DESC_CHARS)
    q = "SELECT job_id, title, description FROM jobs"
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    print(f"  rescoring {len(rows)} job(s) against the current resume...")

    def _one(r):
        res = score_resume_fit(resume, r["title"], r.get("description", ""))
        return r["job_id"], res, r.get("description", "")

    n = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_one, r): r for r in rows}):
            try:
                jid, res, desc = fut.result()
            except Exception as e:
                print(f"    [!] rescore error: {e}")
                continue
            if res.score is None:
                # Unscorable (no real body): clear the stale score so it
                # drops from ranking. A described row that merely failed to
                # parse keeps its score.
                if len((desc or "").strip()) < MIN_DESC_CHARS:
                    store.update_job_scores(
                        conn, jid, {"fit_reason": "no description; unscored"})
                    n += 1
                continue
            store.update_job_scores(conn, jid, res.as_columns())
            n += 1
    conn.close()
    print(f"  {n} job(s) rescored.")
    return n


# Greenhouse job-page URL -> (board slug, job id), for the boards-API detail
# fetch in _live_jd (works for boards. and job-boards.greenhouse.io).
_GH_JOB_URL_RE = re.compile(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)")


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
            from .fetchers.workday import fetch_workday_description
            text = fetch_workday_description(url) or ""
        else:
            m = _GH_JOB_URL_RE.search(url)
            if m:
                import html as _html

                from bs4 import BeautifulSoup

                from .http import HEADERS, SESSION
                r = SESSION.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}"
                    f"/jobs/{m.group(2)}?content=true",
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


def verify_top(top_n=15, max_workers=4, rounds=2, conn=None, t=None):
    """Deep-verify the ranking's FINALISTS before anyone acts on them: for
    each of the current top `top_n` jobs not already verified (fit_reason
    carrying the 'deep:' marker), re-fetch the freshest full posting text
    (_live_jd), run fit.verify_fit — which extracts hard requirements before
    re-scoring all axes and gates — and write the verified scores back.
    Demotions can pull new unverified rows into the top, so the pass
    re-ranks and repeats up to `rounds` times.

    Unverifiable rows (dead URL and no stored body, API down) keep their
    first-pass score untouched. Costs at most top_n x rounds API calls per
    run, and only for rows that changed since their last verification."""
    from core.fit import verify_fit
    t = _t(t)
    own_conn = conn is None
    if own_conn:
        conn = store.connect(t["db_path"])
    n_done = 0
    for rnd in range(rounds):
        ranked = _ranked(conn, t, limit=top_n)
        todo = [r for r in ranked if "deep:" not in (r.get("fit_reason") or "")]
        if not todo:
            break
        print(f"  deep-verifying {len(todo)} of the top {len(ranked)} "
              f"(round {rnd + 1}/{rounds})...")

        def _one(r):
            text = _live_jd(r)
            return r, text, verify_fit(r["title"], text,
                                       location=r.get("location") or "")

        n_scored = n_crushed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for fut in as_completed({ex.submit(_one, r): r for r in todo}):
                try:
                    r, text, res = fut.result()
                except Exception as e:
                    print(f"    [!] verify error: {e}")
                    continue
                if res.score is None:
                    print(f"    [?] kept   {r['title'][:46]} - {res.reason}")
                    continue
                store.update_job_scores(conn, r["job_id"], res.as_columns())
                if text and len(text) > len(r.get("description") or ""):
                    conn.execute("UPDATE jobs SET description=? WHERE job_id=?",
                                 (text[:config.MAX_DESC_CHARS], r["job_id"]))
                    conn.commit()
                old = r.get("resume_fit_score")
                move = (f"{old:.2f} -> {res.score:.2f}"
                        if isinstance(old, float) else f"?    -> {res.score:.2f}")
                flag = "  [DEMOTED]" if isinstance(old, float) and \
                    res.score < old - 0.15 else ""
                print(f"    {move}  {r['title'][:44]} ({r['company_name']})"
                      f"{flag}")
                n_done += 1
                n_scored += 1
                if isinstance(old, float) and res.score < old - 0.25:
                    n_crushed += 1
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
    if own_conn:
        conn.close()
    return n_done


def verify_top_cli(top_n=15, max_workers=4, t=None):
    """Standalone verify: deep-verify the current top N in the store (no
    crawl), then rewrite the digest and print the corrected top."""
    t = _t(t)
    n = verify_top(top_n=top_n, max_workers=max_workers, t=t)
    conn = store.connect(t["db_path"])
    ranked = _ranked(conn, t)
    digest_md.write_ranked_digest(ranked, t, pipeline=store.get_pipeline(conn))
    print(f"\n  {n} job(s) deep-verified; corrected top {min(top_n, len(ranked))}:")
    for j in ranked[:top_n]:
        fit = j["resume_fit_score"]
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        print(f"  fit={fs} [{j.get('geo_mode','?')}] {(j['title'] or '')[:50]}"
              f"  -  {j['company_name']}")
    conn.close()
    return n


def sync_status_all(top_n=15, t=None):
    """Status-only reconciliation: re-fetch every active company's board
    (same scoping as the crawl — locality unless whole-board), reconcile
    open/closed via sync_job_statuses, and rewrite today's digest from the
    corrected ranking. NO scoring, no Claude API — the cheap recovery pass
    for when statuses have drifted without paying for a full crawl."""
    t = _t(t)
    conn = store.connect(t["db_path"])
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
    for c, (jobs, err) in zip(companies, fetched):
        if err is not None or not jobs or not c.get("id"):
            continue
        n_re, n_cl = store.sync_job_statuses(conn, c["id"], jobs,
                                             track=t["track"])
        n_boards += 1
        n_closed += n_cl
        n_reopened += n_re
        if n_cl or n_re:
            print(f"  {c['name'][:34]:34} {len(jobs):3} listed -> "
                  f"{n_cl:2} closed, {n_re:2} reopened")
    ranked = _ranked(conn, t)
    digest_md.write_ranked_digest(ranked, t, pipeline=store.get_pipeline(conn))
    print(f"\n  {n_boards} board(s) reconciled: {n_closed} closed, "
          f"{n_reopened} reopened; {len(ranked)} open job(s) in ranking.")
    for j in ranked[:top_n]:
        fit = j["resume_fit_score"]
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        print(f"  fit={fs} [{j.get('geo_mode','?')}] {(j['title'] or '')[:52]}"
              f"  -  {j['company_name']}")
    conn.close()
    return (n_closed, n_reopened)


def check_closed_jobs(max_workers=8, limit=None, stale_days=2, t=None):
    """Probe the detail URLs of OPEN rows that no successful board fetch has
    vouched for in `stale_days` and close the ones that are positively dead
    (HTTP 404/410, an ATS "no longer accepting" notice, a past JSON-LD
    validThrough, a Workday CXS miss). Indeterminate probes (bot-gated
    hosts, JS-only pages) leave the row untouched."""
    t = _t(t)
    conn = store.connect(t["db_path"])
    cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT job_id, title, company_name, url FROM jobs "
        "WHERE COALESCE(status,'open') != 'closed' "
        "AND COALESCE(last_seen, first_seen, '') < ? "
        "ORDER BY company_name", (cutoff,)).fetchall()]
    if limit:
        rows = rows[:int(limit)]
    print(f"  probing {len(rows)} open job(s) not board-verified in "
          f"{stale_days}+ day(s)...")

    n_closed = n_live = n_unknown = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(company_fetch.probe_job_open, r["url"]): r
                for r in rows}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                is_open, reason = fut.result()
            except Exception as e:
                is_open, reason = None, f"probe error: {e}"
            label = f"{(r['company_name'] or '?')[:24]:24} {(r['title'] or '')[:38]:38}"
            if is_open is False:
                store.set_job_status(conn, r["job_id"], "closed")
                n_closed += 1
                print(f"    [closed] {label} {reason}")
            elif is_open:
                n_live += 1
            else:
                n_unknown += 1
    conn.close()
    print(f"  {n_closed} closed, {n_live} confirmed live, "
          f"{n_unknown} unverifiable (left open) of {len(rows)} probed.")
    return n_closed


def _hydrate_missing_descriptions(conn, jobs):
    """Backfill empty descriptions on jobs linked to a company with a
    resolvable board, batched so each board is fetched once no matter how
    many of its jobs need hydrating."""
    need = [j for j in jobs if j.get("_company_id") and not (j.get("description") or "").strip()]
    if not need:
        return
    by_company = {}
    for j in need:
        by_company.setdefault(j["_company_id"], []).append(j)
    for cid, js in by_company.items():
        company = store.get_company(conn, cid)
        if not company or not company.get("ats"):
            continue
        try:
            board = company_fetch.fetch_company(company, loc_re=None)
        except Exception as e:
            print(f"    [!] board hydrate fetch failed for {company['name']}: {e}")
            continue
        by_title = {(b.get("title") or "").strip().lower(): b for b in board}
        n_hydrated = 0
        for j in js:
            match = by_title.get((j.get("title") or "").strip().lower())
            if match is None:
                continue
            company_fetch.hydrate_description(match)
            if match.get("description"):
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
    resume = resume_text()
    conn = store.connect(t["db_path"])
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
        res = score_resume_fit(resume, j["title"], j.get("description", ""))
        return {"job_id": j["id"], "company_id": j.get("_company_id"),
                "company_name": j.get("company"),
                "title": j.get("title"), "url": j.get("url"), "location": j.get("location"),
                "track": t["track"],
                "geo_mode": geo_mode(j.get("location", ""), j.get("description", "")) or "onsite",
                "description": (j.get("description", "") or "")[:config.MAX_DESC_CHARS],
                "posted_at": j.get("posted_at"),
                "status": "open",
                **res.as_columns()}

    scored = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_score, j): j for j in kept}):
            try:
                store.upsert_job(conn, fut.result())
                scored += 1
            except Exception as e:
                print(f"    [!] ingest error: {e}")
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
        Resolution goes through discovery.local_sourcing.resolve_or_miss —
        the same careers-page-sniff-first resolver every other interactive
        add path uses. It replaced a probe-first resolver that guessed a
        slug from the name before looking at the company's own site, which
        is exactly the collision this path is most exposed to: a hand-typed
        employer name lands on a same-named stranger's board.
    """
    from core.claude import is_active_mission, score_company_mission
    from discovery.local_sourcing import _sample_titles, resolve_or_miss

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
    conn = store.connect(t["db_path"])
    existing = next((c for c in store.get_companies(conn, active_only=False)
                     if (c["name"] or "").lower() == name.lower()), None)
    board, miss = None, None
    if not existing or not existing.get("ats"):
        print(f"  resolving board for {name!r}...")
        # A hit carrying a reason ("no-local-jobs") is a live, readable
        # board with nothing open here today — worth registering, exactly
        # as the probe-first resolver's nc=0 hit was.
        board, miss = resolve_or_miss(name)
    if board:
        is_wd = board["ats"] == "workday"
        slug = board["slug"]
        titles = _sample_titles(board)
        tier, score, reason = score_company_mission(
            name, " | ".join(x for x in titles if x))
        active = is_active_mission(tier, name)
        store.upsert_company(conn, {
            "name": name, "ats": board["ats"],
            "slug": None if is_wd else slug,
            "wd_tenant": slug[0] if is_wd else None,
            "wd_pod": slug[1] if is_wd else None,
            "wd_site": slug[2] if is_wd else None,
            "careers_url": board.get("careers_url"),
            "local_job_count": board["nc"], "total_job_count": board["count"],
            "mission_tier": tier, "mission_score": score, "mission_reason": reason,
            "tags": tags.LOCAL if board["nc"] else None,
            "source": "manual_add", "active": active,
        })
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
    conn.close()

    # 2) The single job — curated (skip exclude/technical), geo gate still on.
    print(f"  adding job: {title!r} @ {name} [{location}]")
    n_job = ingest_external_jobs(
        [{"title": title, "company": name, "url": url,
          "location": location or "", "description": description or ""}],
        source="manual", curated=True, t=t)

    # 3) The company's OTHER jobs — crawl its board whenever it has one
    #    (freshly resolved OR already in the roster), unless --no-board.
    n_other = 0
    conn = store.connect(t["db_path"])
    row = next((c for c in store.get_companies(conn, active_only=False)
                if (c["name"] or "").lower() == name.lower()), None)
    has_board = bool(row and row.get("ats"))
    if pull_board and has_board:
        _, _, n_other = crawl_company(conn, resume_text(), row, max_workers, t=t)
        print(f"    pulled {n_other} other in-scope job(s) from {name}'s board")
    conn.close()

    status = "active board" if has_board else "recorded (board unresolved)"
    print(f"\n  DONE: +{n_job} job, +{n_other} from board; company '{name}' - {status}.")
    return {"job_added": n_job, "other_jobs": n_other,
            "board": has_board, "company": name}


# --------------------------------------------------------------------------- #
#  Re-resolution of rows that died at resolution                               #
# --------------------------------------------------------------------------- #
#
# A name that never resolved to a board is kept as an inactive row carrying a
# miss_reason (core.store.record_miss), and the two families below are the
# ones worth another attempt: nothing was found at all ("no-board-found"), or
# coordinates were found and the live fetch came back empty ("board-dead").
# Neither is a permanent verdict — a resolver improves, a company migrates
# ATS, a careers page comes back — and that bucket is where the roster's
# best-known local employers sit. The other families are not retried here:
# "no-local-jobs" already IS a live board, "ats-unsupported" needs a fetcher
# rather than a retry, and "fetch-error" is a transient every pass re-attempts
# anyway.
RERESOLVE_FAMILIES = ("no-board-found", "board-dead")


def _reresolve_candidates(conn, days=None, names=None, limit=50):
    """The inactive rows a re-resolution pass should retry, oldest miss first.

    Only the two retryable miss families are selected, and only rows the
    crawl is not already using:

    >>> from core.store import connect, record_miss, upsert_company
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
    """
    wanted = {str(n).strip().lower() for n in (names or []) if str(n).strip()}
    cutoff = ((datetime.now() - timedelta(days=int(days))).isoformat()
              if days else None)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies WHERE COALESCE(active, 0) = 0 "
        "AND miss_reason IS NOT NULL "
        "ORDER BY COALESCE(miss_at, '') ASC, name ASC").fetchall()]
    out = []
    for r in rows:
        if store.miss_family(r["miss_reason"]) not in RERESOLVE_FAMILIES:
            continue
        if wanted and (r["name"] or "").strip().lower() not in wanted:
            continue
        # A NULL miss_at predates the column: unknown age, so old enough.
        if cutoff and (r.get("miss_at") or "") > cutoff:
            continue
        out.append(r)
    return out[:int(limit)] if limit else out


def reresolve_misses(conn=None, limit=50, max_workers=6, days=None,
                     names=None, t=None):
    """Retry the roster rows that died at resolution; queue every hit for
    human review. Returns the rows written.

    A hit is written onto the EXISTING row (same name): its board
    coordinates, its mission score, `active=0`, and the `pending-review`
    scope tag merged into whatever tags the row already carried. Writing
    the board clears the row's miss (core.store.upsert_company). A repeated
    miss just re-stamps miss_reason/miss_at, which moves the row to the back
    of the queue `_reresolve_candidates` orders by.

    Notes:
        Deliberately writes nothing else on the row — the roster review
        queue reads exactly `active=0` plus that tag, and confirming a row
        there is what makes it crawlable. Rows are never activated here: a
        re-resolved board is a claim about a company nobody has looked at
        in months, and the resolver's own collision guards are not a
        substitute for that look.

        Resolution runs through the same stall watchdog every other bulk
        resolution path uses (discovery.local_sourcing._drain_or_abandon):
        one wedged careers-page fetch must not hold the web UI's
        one-op-at-a-time slot.
    """
    from core.claude import score_company_mission
    from discovery.local_sourcing import (_board_already_tracked,
                                          _drain_or_abandon,
                                          _report_dup_board, _sample_titles,
                                          resolve_or_miss)

    t = _t(t)
    own_conn = conn is None
    if own_conn:
        conn = store.connect(t["db_path"])
    try:
        rows = _reresolve_candidates(conn, days=days, names=names, limit=limit)
        if not rows:
            print("  no re-resolvable misses "
                  f"(families: {', '.join(RERESOLVE_FAMILIES)}).")
            return []
        print(f"  re-resolving {len(rows)} miss(es) "
              f"(careers-page sniff -> slug-probe -> web search; every board "
              f"validated by a live fetch)...")
        was = {r["name"]: r["miss_reason"] for r in rows}
        written, still, dups = [], [], []

        def _stalled(name):
            store.record_miss(conn, name, "fetch-error:stalled")
            still.append((name, "fetch-error:stalled"))

        def _consume(fut, name):
            try:
                hit, reason = fut.result()
            except Exception as e:
                hit, reason = None, f"fetch-error:{type(e).__name__}"
            if not hit:
                store.record_miss(conn, name, reason)
                still.append((name, reason))
                print(f"    [miss]    {name[:30]:30} {was[name]} -> {reason}")
                return
            slug = hit.get("slug")
            is_wd = hit["ats"] == "workday"
            coords = {"name": name, "ats": hit["ats"],
                      "slug": None if is_wd else slug,
                      "wd_tenant": slug[0] if is_wd else None,
                      "wd_pod": slug[1] if is_wd else None,
                      "wd_site": slug[2] if is_wd else None,
                      "careers_url": hit.get("careers_url")}
            dup = _board_already_tracked(conn, coords)
            if dup:
                # Someone else already holds this board. Leave the row as
                # the miss it was, but re-stamp it so a bounded rerun moves
                # past it instead of paying for the same fetch every night.
                _report_dup_board(name, dup)
                store.record_miss(conn, name, was[name])
                dups.append(name)
                return
            titles = _sample_titles(hit)
            tier, score, reason = score_company_mission(
                name, " | ".join(x for x in titles if x))
            # upsert_company drops None values so it can never erase a
            # stored one — which would leave the dead board's slug beside
            # the new Workday triple. Clear the coordinate columns first.
            conn.execute("UPDATE companies SET slug=NULL, wd_tenant=NULL, "
                         "wd_pod=NULL, wd_site=NULL WHERE name=?", (name,))
            store.upsert_company(conn, {
                **coords,
                "local_job_count": hit["nc"], "total_job_count": hit["count"],
                "mission_tier": tier, "mission_score": score,
                "mission_reason": reason,
                "tags": tags.PENDING, "active": 0,
                "last_probed": datetime.now().isoformat(),
            })
            written.append(coords)
            ss = f"{score:.2f}" if isinstance(score, float) else "n/a"
            print(f"    [pending] {name[:30]:30} {hit['ats']:12} "
                  f"nc={hit['nc']:<3} tot={hit['count']:<4} "
                  f"{str(tier):18} {ss}  (was {was[name]})")

        ex = ThreadPoolExecutor(max_workers=max_workers)
        _drain_or_abandon(
            ex,
            {ex.submit(resolve_or_miss, r["name"], r.get("careers_url") or ""):
             r["name"] for r in rows},
            _consume, _stalled)
        conn.commit()
        print(f"\n  {len(written)} board(s) re-resolved and queued for review "
              f"(active=0, tagged {tags.PENDING})"
              + (f", {len(dups)} already tracked under another name" if dups else "")
              + f", {len(still)} still missing, of {len(rows)} tried.")
        if written:
            print("  confirm or reject them in the roster review queue.")
        return written
    finally:
        if own_conn:
            conn.close()
