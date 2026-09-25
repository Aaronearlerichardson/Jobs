"""External and hand-picked postings into a track's store: the ingest
behind capture.py and the NLx feed, and the manual add."""

from src import store
from src import tags
from src.ats import coords
from src.ats.board import company as company_fetch
from src.claude.resume import resume_text
from src.match import gates
from src.match.locality import NC_RE, geo_mode
from src.net.parallel import fan_out
from src.ops.maintenance import (_mission_trusted, _scored_row, _t,
                                 board_index, board_match, crawl_company,
                                 group_by_company, track_store)


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
                trusted = (tags.has(company_row, tags.WATCH)
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
