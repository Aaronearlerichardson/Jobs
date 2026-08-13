"""ONE crawl pipeline for every track.

Historically each track shipped its own runner module (local_tech.run(),
remote_neural_run.main()) whose methodology differences — keyword handling,
source families, gates, scoring budget, digest/email — were code. They are
now CONFIGURATION: every knob lives in profile.toml [tracks.<id>] (parsed by
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
                         rows mark_seen; ranked digest and/or match digest;
                         optional email

The legacy entry points delegate here unchanged (local_tech.run, the
remote_neural_run.main CLI with its --commit/--send/--fit/... flags), as
does the web UI's single "crawl" op. The two ENGINE values still select the
code-level bits that are not data — the technical-title regex, the exclude
gate, digest rendering — but never the methodology.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config

from . import store
from .parallel import fetch_all
from .remote_filter import remote_signal_for, us_eligible
from .resume import resume_text
from .sources import ATS_REGISTRY, iter_store_sources

# Rough per-posting cost for the cost_guard message: ~700 input tokens
# (cached system prompt) + ~120 output at a blended per-token rate.
# Order-of-magnitude only — for deciding whether to stop and ask.
_EST_TOKENS_PER_POSTING = 820
_EST_USD_PER_MTOK = 4.0


def track_for_engine(engine):
    """The configured track to use when a legacy engine-level entry point
    (local_tech.run / remote_neural_run.main) is invoked without naming a
    track: the default-flagged track with that engine, else the first."""
    cands = [t for t in config.UI_TRACKS.values() if t["engine"] == engine]
    if not cands:
        raise SystemExit(f"no [tracks.*] entry with engine={engine!r} "
                         "in profile.toml")
    return next((t for t in cands if t["default"]), cands[0])


def apply_keyword_focus(cfg, t):
    """Point the shared keyword filter at this track's focus. Mutates the
    live list objects in place so filters.is_relevant (which imported them
    at load time) sees the change without a re-import. "extend" adds the
    track's [keywords.<id>] terms to the global tiers (deduped); "replace"
    swaps them in wholesale (and rebuilds the flat INCLUDE view, matching
    the legacy replace semantics). Empty track lists never blank a tier."""
    kw = getattr(cfg, "KEYWORDS_BY_TRACK", {}).get(t["id"], {})
    core = list(kw.get("core", []))
    dom = list(kw.get("domain", []))
    skill = list(kw.get("skill", []))
    if t["keyword_mode"] == "replace":
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
    cfg.ACCEPT_REMOTE = t["accept_remote"]


def core_anchor(title, description=""):
    """The require_core_anchor gate: the CORE keyword that anchors this
    posting, or None. Short single-token acronyms (eeg, bci, ecog...) match
    on word boundaries — "ecog" fires inside "recognized" — while longer
    terms stay substring so "subcortical" still matches "cortical". Reads
    the LIVE config lists, so after apply_keyword_focus this is exactly the
    track's own anchor vocabulary (the legacy NEURAL_ANCHORS gate)."""
    text = f"{title} {description}".lower()
    for a in config.CORE_KEYWORDS:
        k = a.lower()
        if k.isalpha() and len(k) <= 5:
            if re.search(rf"\b{re.escape(k)}\b", text):
                return a
        elif k in text:
            return a
    return None


def build_sources(cfg, t, include_websearch=None):
    """Assemble the track's source specs from its `sources` config table.
    Returns a list of dicts {name, platform, thunk, company}: `company` is
    the store row for location-scoped store boards (their jobs sync/upsert
    against that company) and None for sweep sources (priority companies,
    lightweight ATS sweep, aggregators, web search — persisted via
    mark_seen). Priority companies come first so cross-source duplicates
    resolve deterministically."""
    from . import ops
    from .fetchers import company as company_fetch

    src = t["sources"]
    use_ws = src["websearch"] if include_websearch is None else include_websearch
    specs, used = [], set()

    def add(name, platform, thunk, company=None, key=None):
        k = key or (platform, name.lower())
        if k in used:
            return
        used.add(k)
        specs.append({"name": name, "platform": platform,
                      "thunk": thunk, "company": company})

    # 1) Priority targets ([discovery] priority_companies), starred.
    if src["priority_companies"]:
        for name, ats, slug in getattr(cfg, "DISCOVERY_PRIORITY_COMPANIES", []):
            entry = ATS_REGISTRY.get(ats)
            if not entry:
                print(f"  [!] priority company {name}: unknown ATS {ats!r}")
                continue
            add(name, ats + "*", entry[0](name, slug), key=(ats, str(slug)))

    # 2) Company store (this track's own DB, optionally tag-scoped).
    if src["store"]:
        try:
            conn = store.connect(t["db_path"])
            rows = store.get_companies(conn, active_only=True,
                                       tag=t["store_tag"])
            conn.close()
        except Exception as e:
            print(f"  [!] company store unavailable ({e})")
            rows = []
        if src["location_scoped"]:
            # Full per-company board fetch through the locality filter —
            # whole-board (no filter) for watched/neural-tagged companies,
            # whose out-of-area rows the geo gate handles downstream.
            for c in rows:
                add(c["name"], c.get("ats") or "?",
                    (lambda cc=c: company_fetch.fetch_company(
                        cc, None if ops._whole_board(cc)
                        else company_fetch.NC_RE)),
                    company=c, key=("store", (c["name"] or "").lower()))
        else:
            # Location-agnostic lightweight ATS sweep (JSON-API boards only;
            # the heavyweight onsite ATSes are only worth fetching scoped).
            for ats, name, slug, thunk in iter_store_sources(rows):
                add(name, ats, thunk, key=(ats, str(slug)))

    # 3) Forums + aggregator feeds (remote-native boards).
    if src["aggregators"]:
        from .fetchers import (fetch_discourse, fetch_hnhiring,
                                fetch_remoteok, fetch_remotive, fetch_rss)
        for name, base, cat in cfg.DISCOURSE_BOARDS:
            add(name, "discourse",
                lambda n=name, b=base, c=cat: fetch_discourse(n, b, c))
        if getattr(cfg, "REMOTEOK_ENABLED", True):
            add("RemoteOK", "remoteok", fetch_remoteok)
        if getattr(cfg, "REMOTIVE_ENABLED", True):
            add("Remotive", "remotive",
                lambda: fetch_remotive(category=cfg.REMOTIVE_CATEGORY))
        if getattr(cfg, "HNHIRING_ENABLED", True):
            add("HN Who-is-hiring", "hn",
                lambda: fetch_hnhiring(max_threads=cfg.HNHIRING_MAX_THREADS))
        for label, url, default_loc in cfg.RSS_FEEDS:
            is_remote_board = default_loc.strip().lower() == "remote"
            add(label, "rss",
                lambda l=label, u=url, d=default_loc, rb=is_remote_board:
                    fetch_rss(l, u, default_location=d, remote_board=rb))

    # 4) Web searches (DDG -> JSON-LD).
    if use_ws:
        from .fetchers import fetch_websearch
        for label, query, n in getattr(cfg, "REMOTE_NEURAL_WEBSEARCH_QUERIES", []):
            add(label, "websearch",
                lambda l=label, q=query, m=n: fetch_websearch(l, q, max_results=m))

    return specs


def _short(text, n):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "..."


def _diversify(matches, n):
    """Up to n samples spread round-robin across companies so the precision
    sanity-check isn't dominated by one prolific employer."""
    by_company = {}
    for j in matches:
        by_company.setdefault(j.get("company") or j.get("company_name"),
                              []).append(j)
    picked = []
    while len(picked) < n and any(by_company.values()):
        for comp in list(by_company):
            if by_company[comp]:
                picked.append(by_company[comp].pop(0))
                if len(picked) >= n:
                    break
    return picked


def _cost_guard_trips(t, n_to_score, confirm_cost):
    """True (and prints the budget banner) when scoring n_to_score postings
    would blow the track's cost_guard without an explicit confirmation."""
    guard = t["cost_guard"]
    if not guard or n_to_score <= guard or confirm_cost:
        return False
    est_tokens = n_to_score * _EST_TOKENS_PER_POSTING
    est_usd = est_tokens / 1_000_000 * _EST_USD_PER_MTOK
    bar = "=" * 70
    print(f"\n{bar}")
    print(f"  [!] BUDGET GUARD: {n_to_score} posting(s) would be scored via "
          f"the Claude API (> {guard}).")
    print(f"      Rough estimate: ~{est_tokens:,} tokens, ~${est_usd:.2f} "
          f"(order-of-magnitude, not a quote).")
    print("      Re-run with confirm-cost to proceed. Scoring skipped this run.")
    print(f"{bar}")
    return True


def run_track(t, *, fit=True, commit=True, send=None, verify=None,
              websearch=None, confirm_cost=False, max_workers=6, top_n=15,
              samples=5):
    """Run one crawl of track `t` (a config.UI_TRACKS entry).

    Every methodology switch reads the track config; the keyword args only
    OVERRIDE it for this run (None = use the config): `send` overrides
    t["email"], `verify` (bool) overrides t["verify_top"] (True -> top_n,
    False -> skip), `websearch` overrides sources.websearch. `fit=False`
    skips resume scoring; `commit=False` is the legacy neural preview (no
    DB writes). Returns the ranked list (company-linked crawls) or the
    surfaced match list."""
    from . import digest_md, gates, ops
    from .nc import NC_RE, geo_mode

    engine = t["engine"]
    send = t["email"] if send is None else send
    verify_n = (t["verify_top"] if verify is None
                else (top_n if verify else 0))

    resume = resume_text() if fit else None
    if fit and not resume:
        print("  [!] No resume text — fit scores will be null. "
              "Set config.RESUME_PATH.")

    apply_keyword_focus(config, t)
    specs = build_sources(config, t, include_websearch=websearch)
    sources = [(s["name"], s["platform"], s["thunk"]) for s in specs]
    conn = store.connect(t["db_path"])

    bar = "=" * 70
    gates_desc = []
    if t["require_core_anchor"]:
        gates_desc.append("core-anchor")
    gates_desc.append("technical-title")
    gates_desc.append("geo" if t["geo_gate"] else "remote-stamped")
    print(f"\n{bar}\n  [{t['label'].upper()}] crawl - "
          f"{datetime.now():%Y-%m-%d %H:%M}")
    print(f"  engine={engine} keywords={t['keyword_mode']} "
          f"sources={sum(1 for v in t['sources'].values() if v)} families "
          f"({len(sources)} feeds) gates={'+'.join(gates_desc)}")
    mode = "COMMIT (DB writes)" if commit else "PREVIEW (no DB writes)"
    print(f"  Mode: {mode}" + (" + EMAIL" if send else "") + f"\n{bar}\n")
    if not sources:
        print("  [!] No sources — check [tracks.*].sources and the company "
              "store (discover.py --local / --import-companies).")

    done_count = [0]

    def _progress(name, platform, jobs, err):
        done_count[0] += 1
        status = f"fetch error: {err}" if err else f"{len(jobs)} relevant"
        print(f"  [{done_count[0]:>3}/{len(sources)}] {name} ({platform}): "
              f"{status}")

    fetched = fetch_all(sources, on_done=_progress)

    # ─── Gate + collect, in source order ─────────────────────────────────
    to_score = []      # (company, job): fresh company-linked rows to score
    matches = []       # sweep rows surfaced (fetcher dict shape)
    watch_hits = []    # (company, job, in_pipeline) at watched companies
    seen_ids = set()
    funnel = []        # per-source summary rows
    n_closed = n_reopened = n_seen = 0

    for spec, (jobs, err) in zip(specs, fetched):
        c = spec["company"]
        label = f"{spec['name']} ({spec['platform']})"
        if err is not None:
            funnel.append((label, 0, 0, 0, 0, "ERR"))
            continue

        if c is not None:
            # Company-linked store board: sync statuses (a DB write — only
            # under commit), gate, queue fresh rows for the score+upsert path.
            if jobs and c.get("id") and commit:
                n_re, n_cl = store.sync_job_statuses(conn, c["id"], jobs,
                                                     track=t["track"])
                n_reopened += n_re
                n_closed += n_cl
            kept = []
            for j in jobs:
                if t["require_core_anchor"] and not core_anchor(
                        j.get("title", ""), j.get("description", "")):
                    continue
                if not ops._keep_job(c, j, t):
                    continue
                kept.append(j)
            fresh = [j for j in kept if not store.job_exists(conn, j["id"])]
            n_seen += len(kept) - len(fresh)
            for j in fresh:
                to_score.append((c, j))
            if ops._is_watched(c):
                # Watch section: EVERY new technical, non-excluded posting
                # at a watched company, any geography — out-of-scope ones
                # stored unscored so they aren't re-flagged next run.
                fresh_ids = {f["id"] for f in fresh}
                for j in jobs:
                    if (store.job_exists(conn, j["id"])
                            and j["id"] not in fresh_ids) \
                            or not gates.is_technical_role(j.get("title", ""), t) \
                            or (t["exclude_gate"] and gates.exclude_reason(
                                j.get("title", ""), j.get("description", ""),
                                allow_defense=True, track_id=t["id"])):
                        continue
                    in_pipeline = j["id"] in fresh_ids
                    watch_hits.append((c, j, in_pipeline))
                    if not in_pipeline and commit:
                        store.upsert_job(conn, {
                            "job_id": j["id"], "company_id": c["id"],
                            "company_name": c["name"], "title": j.get("title"),
                            "url": j.get("url"), "location": j.get("location"),
                            "track": t["track"],
                            "geo_mode": geo_mode(j.get("location", ""),
                                                 j.get("description", "")),
                            "posted_at": j.get("posted_at"),
                            "description": (j.get("description") or "")
                                           [:config.MAX_DESC_CHARS]})
            funnel.append((label, len(jobs), len(kept), len(fresh),
                           len(fresh), ""))
        else:
            # Sweep source: anchor + title (+ engine excludes) gates, remote
            # signal stamped (or geo-gated when configured), deduped across
            # sources, persisted via mark_seen.
            neural_here = tech_here = surfaced = new_here = 0
            for job in jobs:
                title = job.get("title", "")
                nsig = None
                if t["require_core_anchor"]:
                    nsig = core_anchor(title, job.get("description", ""))
                    if not nsig:
                        continue
                neural_here += 1
                if not gates.is_technical_role(title, t):
                    continue
                tech_here += 1
                if t["exclude_gate"] and gates.exclude_reason(
                        title, job.get("description", ""), track_id=t["id"]):
                    continue
                if t["geo_gate"] and geo_mode(
                        job.get("location", ""),
                        job.get("description", "")) is None:
                    continue
                sig = remote_signal_for(job)
                surfaced += 1
                jid = job["id"]
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                new = store.is_new(conn, jid)
                if new:
                    new_here += 1
                job["track_tag"] = f"[{t['label'].upper()}]"
                job["remote_eligible"] = bool(sig)
                if sig is not None:
                    job["remote_signal"] = sig
                if nsig:
                    job["neural_signal"] = nsig
                job["_new"] = new
                job["_us_eligible"] = us_eligible(job.get("location", ""))
                matches.append(job)
            funnel.append((label, len(jobs), neural_here, tech_here,
                           surfaced,
                           "priority" if spec["platform"].endswith("*") else ""))

    # ─── Scoring ──────────────────────────────────────────────────────────
    scored = 0
    n_would_score = (len(to_score) + len(matches)) if fit else 0
    guard_tripped = fit and _cost_guard_trips(t, n_would_score, confirm_cost)
    score_linked = fit and not guard_tripped

    if to_score and score_linked:
        print(f"\n  scoring {len(to_score)} new job(s) against resume "
              f"({n_seen} already scored)...")
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(ops._score_job, resume, c, j,
                              t["track"]): j for c, j in to_score}
            for fut in as_completed(futs):
                try:
                    row = fut.result()
                    if commit:
                        store.upsert_job(conn, row)
                    scored += 1
                except Exception as e:
                    print(f"    [!] scoring error: {e}")
    elif to_score:
        # Scoring skipped (fit off, or budget guard) — store the fresh rows
        # UNSCORED so they aren't re-flagged as new next run; the self-heal
        # pass scores NULL-score rows once a later run has budget again.
        why = "budget guard" if guard_tripped else "fit scoring off"
        print(f"\n  storing {len(to_score)} new job(s) unscored ({why})...")
        for c, j in to_score:
            if not commit:
                break
            store.upsert_job(conn, {
                "job_id": j["id"], "company_id": c["id"],
                "company_name": c["name"], "title": j.get("title"),
                "url": j.get("url"), "location": j.get("location"),
                "track": t["track"],
                "geo_mode": geo_mode(j.get("location", ""),
                                     j.get("description", "")) or "onsite",
                "posted_at": j.get("posted_at"),
                "description": (j.get("description") or "")
                               [:config.MAX_DESC_CHARS]})

    if fit and matches and resume and not guard_tripped:
        from .claude import score_resume_fit
        print(f"  scoring {len(matches)} match(es) against resume...")

        def _one(j):
            res = score_resume_fit(resume, j["title"],
                                   j.get("description", ""))
            j.update(res.as_columns())

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(_one, matches))
        matches.sort(key=lambda j: (j.get("resume_fit_score") is not None,
                                    j.get("resume_fit_score") or 0.0),
                     reverse=True)

    if commit:
        for job in matches:
            store.mark_seen(conn, job, track=t["track"])

    # ─── Self-heal + deep-verify (company-linked crawls) ──────────────────
    if resume and commit and t["sources"]["store"] \
            and t["sources"]["location_scoped"] and not guard_tripped:
        scored += ops.self_heal_unscored(conn, resume, track=t["track"],
                                         max_workers=max_workers)
    if verify_n and resume and commit and not guard_tripped:
        ops.verify_top(top_n=verify_n, max_workers=max(2, max_workers // 2),
                       conn=conn, t=t)

    # ─── Funnel summary ───────────────────────────────────────────────────
    print(f"\n{bar}")
    print("  PER-SOURCE FUNNEL  (FETCH -> anchor/gates -> KEPT; NEW=unseen)")
    print(f"  {'SOURCE':<46} {'FETCH':>5} {'GATE1':>5} {'KEPT':>5} {'NEW':>5}")
    for label, n_f, g1, kept_n, new_n, note in funnel:
        tail = f"  [{note}]" if note else ""
        print(f"  {label:<46} {n_f:>5} {g1:>5} {kept_n:>5} {new_n:>5}{tail}")

    # ─── Digest(s) ────────────────────────────────────────────────────────
    ranked = None
    if t["sources"]["store"] and t["sources"]["location_scoped"]:
        ranked = store.ranked_jobs(
            conn, track=t["track"],
            location_re=(NC_RE if t["geo_gate"] else None),
            rank_by=t["rank_by"], allow_geo_modes={"remote"},
            min_mission=t["min_mission"])
        digest_md.write_ranked_digest(ranked, t, watch_hits=watch_hits,
                                      pipeline=store.get_pipeline(conn))
        if watch_hits:
            print(f"\n  {bar}\n  WATCHED COMPANIES - NEW POSTINGS THIS RUN\n  {bar}")
            for c, j, in_pipeline in watch_hits:
                note = ("scored" if in_pipeline
                        else "listed only (outside local scope)")
                print(f"  [WATCH] {c['name']}: {(j.get('title') or '')[:56]}")
                print(f"          [{j.get('location') or '?'}]  ({note})")
                print(f"          {j.get('url')}")
        print(f"\n  {bar}\n  TOP {min(top_n, len(ranked))} BY RESUME FIT\n  {bar}")
        for j in ranked[:top_n]:
            fs = (f"{j['resume_fit_score']:.2f}"
                  if isinstance(j.get("resume_fit_score"), float) else "n/a")
            print(f"  fit={fs} [{j.get('geo_mode', '?')}] "
                  f"[{digest_md.age_tag(j)}] {(j['title'] or '')[:48]}")
            print(f"        {j['company_name']} "
                  f"({j.get('mission_tier') or '?'})  -  "
                  f"{j.get('fit_reason', '')}")
            print(f"        {j['url']}")
        print(f"\n  {len(ranked)} open job(s) in ranking; {scored} newly "
              f"scored, {n_closed} marked closed, {n_reopened} reopened "
              f"this run.")

    if matches or not (t["sources"]["store"] and t["sources"]["location_scoped"]):
        n = max(0, samples)
        print(f"\n{bar}\n  {min(n, len(matches))} SAMPLE MATCHES "
              f"(precision sanity-check)\n{bar}")
        if not matches:
            print("  (no matches)")
        for i, j in enumerate(_diversify(matches, n), 1):
            print(f"\n  {i}. {j.get('track_tag', '')} {j['title']}")
            print(f"     company : {j.get('company') or j.get('company_name')}")
            print(f"     location: {j.get('location')}")
            if j.get("neural_signal"):
                print(f"     anchor  : {j['neural_signal']}")
            if j.get("resume_fit_score") is not None:
                print(f"     fit     : {j['resume_fit_score']:.2f}  "
                      f"({j.get('fit_reason', '')})")
            print(f"     remote  : {j.get('remote_signal', '')}"
                  f"{'   (NEW)' if j.get('_new') else '   (seen)'}")
            print(f"     url     : {j['url']}")
            if j.get("description"):
                print(f"     blurb   : {_short(j['description'], 160)}")
        digest_path = digest_md.write_matches_digest(matches,
                                                     config.REPORT_DIR, t)
        print(f"\n  Digest -> {digest_path}")
        if send:
            digest_md.send_matches_digest(matches, t, config)
        elif matches:
            print("  (email suppressed — enable [tracks.*].email or --send)")

    if not send:
        print("  *** NO EMAIL SENT ***\n")
    conn.close()
    return ranked if ranked is not None else matches
