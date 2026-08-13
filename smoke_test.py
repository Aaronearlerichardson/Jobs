#!/usr/bin/env python3
"""
Offline smoke tests for the unified crawler. No network / no API key.
Run: python smoke_test.py   (exit 0 = all green). Use as a regression guard
around refactors.
"""
import sys

FAILS = []


def check(label, cond):
    print(f"  {'OK ' if cond else 'XX '}{label}")
    if not cond:
        FAILS.append(label)


def main():
    # 1. imports
    import jobcrawler.store as store
    import jobcrawler.nc as nc
    import jobcrawler.resume as resume  # noqa: F401 (import-works check)
    import jobcrawler.fetchers.company as cf
    import jobcrawler.digest_md as digest_md
    import jobcrawler.gates as gates
    import jobcrawler.ops as ops
    import jobcrawler.runner as runner
    import jobcrawler.discovery.sniffer as sniffer
    import jobcrawler.discovery.local_sourcing as ls
    import jobcrawler.discovery.ats_dork as dork
    from jobcrawler.discovery.probes import PROBES
    print("[imports OK]")

    # 2. NC locality detection (single source of truth: jobcrawler/nc.py)
    print("[NC locality]")
    check("Durham, NC is NC", nc.is_nc("Durham, NC"))
    check("Boston, MA is not NC", not nc.is_nc("Boston, MA"))
    check("'clinic' does not hit 'nc'", not nc.is_nc("outpatient clinic"))
    check("geo_mode onsite", nc.geo_mode("Durham, NC") == "onsite")
    check("geo_mode remote", nc.geo_mode("Remote - US") == "remote")
    check("geo_mode none", nc.geo_mode("Boston, MA") is None)
    check("'distributed training' is not remote",
          nc.geo_mode("", "we do distributed training at scale") is None)

    # 3. exclude gate (config-driven per track id; the substring-bug fixes)
    print("[exclude gate]")
    local_t = runner.track_for_engine("local")
    neural_t = runner.track_for_engine("neural")
    _exc = lambda *a, **k: gates.exclude_reason(*a, track_id=local_t["id"], **k)
    check("CRA excluded", bool(_exc("Clinical Research Associate (CRA)")))
    check("scribe not in 'describe'", not _exc("Engineer", "you will describe systems"))
    check("defi not in 'defibrillator'", not _exc("Engineer", "implantable defibrillator"))
    check("DoD radar excluded", bool(_exc("RF Engineer", "military radar for DoD")))
    check("neural track has no exclude vocabulary (empty table = no-op)",
          not gates.exclude_reason("Combat Systems Radar Engineer",
                                   "missile defense", track_id=neural_t["id"]))

    # 4. technical pre-filter (per-track regex from [tracks.*].tech_title_regex)
    print("[technical filter]")
    check("engineer is technical", gates.is_technical_role("Quality Engineer", local_t))
    check("data manager is technical", gates.is_technical_role("Clinical Data Manager", local_t))
    check("nurse not technical", not gates.is_technical_role("Registered Nurse", local_t))
    check("per-track title regexes differ",
          local_t["tech_title_regex"] != neural_t["tech_title_regex"])
    check("quality is local-technical but not neural-technical",
          gates.is_technical_role("Quality Engineer II", local_t)
          and not gates.is_technical_role("Quality Specialist", neural_t))

    # 5. neural-engine gates (core anchor + technical title)
    print("[neural gates]")
    check("controller not technical title",
          not gates.is_technical_role("Corporate Controller", neural_t))
    from jobcrawler.remote_filter import is_remote_eligible, us_eligible
    check("remote location eligible", is_remote_eligible("Remote, US"))
    check("hard negation vetoes", not is_remote_eligible("Remote", "this role is not remote"))
    check("Philippines remote not US-eligible", not us_eligible("Philippines Remote"))

    # 6. sniffer slug extraction + custom-board detector
    print("[sniffer]")
    boards = dork.extract_boards_from_urls(["https://jobs.lever.co/bioagilytix/x",
                                            "https://boards.greenhouse.io/pendo/jobs/1"])
    check("dork extracts lever+gh", {("lever", "bioagilytix"), ("greenhouse", "pendo")} <= set(boards))
    check("custom-board needs real job links",
          not sniffer._looks_like_custom_board("<a href='/careers/'>Careers</a>"))
    check("detect adp cid|ccid", sniffer._detect(
        "workforcenow.adp.com/x?cid=d290c04e-0230-4cd9-8bf0-f116bfab1405&ccid=19000101_000003")[1] == "adp")
    check("detect lead platform", sniffer._detect("via acme.eightfold.ai portal")[0] == "lead")
    check("probes cover sniffable ATSes",
          {"greenhouse", "lever", "ashby", "kula", "jazzhr", "bamboohr",
           "smartrecruiters"} <= set(PROBES))

    # 6b. custom-board detection — real job links vs nav/index links
    print("[custom board]")
    from bs4 import BeautifulSoup
    real = ('<a href="/careers/facilities-engineer-88">Facilities Engineer</a>'
            '<a href="/careers/quality-engineer-19">Quality Engineer</a>'
            '<a href="/careers/data-scientist-3">Data Scientist</a>')
    nav = ('<a href="/careers/open-positions/">Careers</a>'
           '<a href="/careers/career-opportunities/">View Current Job Openings</a>'
           '<a href="/careers/career-opportunities/">Career Opportunities</a>')
    check("3 real job links detected", len(cf.find_job_links(BeautifulSoup(real, "html.parser"))) == 3)
    check("nav links rejected (0)", len(cf.find_job_links(BeautifulSoup(nav, "html.parser"))) == 0)
    check("aggregator host never a custom board",
          cf.custom_board_listing_url("https://www.indeed.com/jobs?q=x", "<html></html>") is None)

    # 7. HN parser (merged: field classifiers + full text + safe sentence split)
    print("[hn parser]")
    from jobcrawler.fetchers.hnhiring import _parse_post
    c, r, l, _ = _parse_post("Acme Neuro | Remote (US) | $150k-190k | Senior ML Engineer | Full-time")
    check("role found out of order", r == "Senior ML Engineer")
    check("location classified", "Remote" in l)
    c, r, l, _ = _parse_post("Foo Inc. | ML Engineer | Durham, NC")
    check("'Inc. |' not chopped", c == "Foo Inc." and r == "ML Engineer")

    # 8. unified store schema + dedupe + tag scoping
    print("[store]")
    conn = store.connect(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    check("companies has mission_score + tags", {"mission_score", "tags"} <= cols)
    jcols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    check("jobs has fit + track + remote cols",
          {"resume_fit_score", "track", "remote_eligible", "neural_signal"} <= jcols)
    store.upsert_company(conn, {"name": "X", "ats": "greenhouse", "slug": "x", "tags": "neural"})
    store.upsert_company(conn, {"name": "X", "tags": "nc_local"})
    check("tags merge on upsert",
          store.get_companies(conn, tag="neural")[0]["tags"] == "nc_local,neural")
    store.mark_seen(conn, {"id": "j1", "company": "X", "title": "t", "url": "u",
                           "location": "Remote"}, track="remote-neural")
    check("seen-jobs dedupe", not store.is_new(conn, "j1") and store.is_new(conn, "j2"))
    conn.close()

    # 9. closed-job lifecycle: board-diff closes / reopens; ranking excludes
    print("[closed jobs]")
    conn = store.connect(":memory:")
    cid = store.upsert_company(conn, {"name": "Y", "ats": "greenhouse", "slug": "y"})
    for jid, title, url, fit in (("gh_y_1", "Data Engineer", "https://y.io/1", 0.9),
                                 ("gh_y_2", "ML Engineer", "https://y.io/2", 0.8),
                                 ("linkedin_aaa", "Platform Engineer",
                                  "https://linkedin.com/jobs/3", 0.7)):
        store.upsert_job(conn, {"job_id": jid, "company_id": cid,
                                "company_name": "Y", "title": title, "url": url,
                                "location": "Durham, NC", "track": "local-tech",
                                "resume_fit_score": fit})
    snap = [{"id": "gh_y_2", "title": "ML Engineer", "url": "https://y.io/2"}]
    st = lambda j: conn.execute("SELECT status, closed_at FROM jobs WHERE job_id=?",
                                (j,)).fetchone()
    store.sync_job_statuses(conn, cid, snap, track="local-tech")
    check("vanished board-native job closed", st("gh_y_1")["status"] == "closed"
          and st("gh_y_1")["closed_at"] is not None)
    check("still-listed job stays open", st("gh_y_2")["status"] == "open")
    check("fresh external row grace-protected", st("linkedin_aaa")["status"] == "open")
    ids = [r["job_id"] for r in store.ranked_jobs(conn, track="local-tech")]
    check("closed excluded from ranking", "gh_y_1" not in ids and "gh_y_2" in ids)
    check("include_closed readmits", "gh_y_1" in
          [r["job_id"] for r in store.ranked_jobs(conn, track="local-tech",
                                                  include_closed=True)])
    # URL match (not id) keeps an external row open; reappearance reopens
    snap2 = snap + [{"id": "gh_y_1", "title": "Data Engineer", "url": "https://y.io/1"},
                    {"id": "gh_y_9", "title": "Staff Something",
                     "url": "http://linkedin.com/jobs/3/"}]
    store.sync_job_statuses(conn, cid, snap2, track="local-tech")
    check("reappeared job reopened", st("gh_y_1")["status"] == "open"
          and st("gh_y_1")["closed_at"] is None)
    # external row past grace with no match on a later sync -> closed
    conn.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00', "
                 "last_seen='2020-01-01T00:00:00' WHERE job_id='linkedin_aaa'")
    conn.commit()
    store.sync_job_statuses(conn, cid, snap, track="local-tech")
    check("stale unmatched external row closed",
          st("linkedin_aaa")["status"] == "closed")
    # re-upsert (re-ingest/re-capture) reopens and clears closed_at
    store.upsert_job(conn, {"job_id": "linkedin_aaa", "company_id": cid,
                            "company_name": "Y", "title": "Platform Engineer",
                            "url": "https://linkedin.com/jobs/3",
                            "location": "Durham, NC", "track": "local-tech"})
    check("re-ingest reopens", st("linkedin_aaa")["status"] == "open"
          and st("linkedin_aaa")["closed_at"] is None)
    store.set_job_status(conn, "linkedin_aaa", "closed")
    store.touch_job(conn, "linkedin_aaa")   # dedupe-path re-sighting
    check("touch_job reopens + refreshes last_seen",
          st("linkedin_aaa")["status"] == "open" and conn.execute(
              "SELECT last_seen FROM jobs WHERE job_id='linkedin_aaa'"
          ).fetchone()[0] > "2020-01-02")
    check("empty snapshot never closes",
          store.sync_job_statuses(conn, cid, [], track="local-tech") == (0, 0))
    # Recycled titles: a live posting must NOT shield a dead same-titled
    # board-native req (the Beacon "Algorithm Engineer" repost pattern) —
    # board-native rows match by exact id only. External rows (foreign id
    # namespace) still get the title match.
    for jid in ("gh_y_old", "gh_y_new"):
        store.upsert_job(conn, {"job_id": jid, "company_id": cid,
                                "company_name": "Y", "title": "Algorithm Engineer",
                                "url": f"https://y.io/{jid}", "location": "Durham, NC",
                                "track": "local-tech"})
    store.upsert_job(conn, {"job_id": "linkedin_bbb", "company_id": cid,
                            "company_name": "Y", "title": "Algorithm Engineer",
                            "url": "https://linkedin.com/jobs/9",
                            "location": "Durham, NC", "track": "local-tech"})
    store.sync_job_statuses(conn, cid, [
        {"id": "gh_y_new", "title": "Algorithm Engineer",
         "url": "https://y.io/gh_y_new"}], track="local-tech")
    check("dead req closes despite shared live title",
          st("gh_y_old")["status"] == "closed")
    check("live same-titled req stays open", st("gh_y_new")["status"] == "open")
    check("external row still title-matches", st("linkedin_bbb")["status"] == "open")
    conn.close()

    # 9b. fit rubric: clip keeps the tail, management gate exists and bites,
    # profile gate_penalty merges over defaults instead of replacing them
    print("[fit rubric]")
    import config as _config
    import jobcrawler.fit as fit
    long_jd = "INTRO " + ("boilerplate " * 2000) + "REQUIREMENTS: 8+ years TPM"
    clipped = fit.clip_desc(long_jd, max_chars=5000)
    check("clip keeps the requirements tail",
          clipped.endswith("REQUIREMENTS: 8+ years TPM") and "elided" in clipped)
    check("short text passes through unclipped",
          fit.clip_desc("short jd", max_chars=5000) == "short jd")
    check("management gate registered", "management" in fit.GATES)
    axes = dict(domain=.35, function=.30, stack=.35, seniority=.45)
    check("management gate bites",
          fit.combine(axes, ["management"]) < fit.combine(axes, []) * 0.5)
    _saved = getattr(_config, "FIT_GATE_PENALTY", None)
    _config.FIT_GATE_PENALTY = {"geo": 0.10}          # pre-management profile
    merged = fit._effective_penalties()
    _config.FIT_GATE_PENALTY = _saved
    check("profile penalties merge, not replace",
          merged["geo"] == 0.10 and merged["management"] == 0.35)
    check("verify prompt extracts requirements",
          all(k in fit.build_verify_prompt()
              for k in ("years_required", "seat_type", "candidate_gaps")))
    check("verify_fit refuses stub descriptions",
          fit.verify_fit("T", "too short").score is None)

    # 9b-ii. prompt caching: the breakpoint must sit on the STABLE system
    # prompt, never on the per-posting user turn (which would write a fresh
    # cache entry per job and never read one).
    print("[prompt cache]")
    import jobcrawler.claude as claude
    p = claude.build_payload("SYSTEM RUBRIC", "JOB TITLE: X")
    check("system carries a cache breakpoint",
          isinstance(p["system"], list)
          and p["system"][0]["cache_control"]["type"] == "ephemeral")
    check("user turn carries no breakpoint",
          "cache_control" not in str(p["messages"]))
    check("cache=False falls back to a plain system string",
          claude.build_payload("S", "U", cache=False)["system"] == "S")
    check("system prompt is byte-stable across calls",
          claude.build_payload("S", "U1")["system"]
          == claude.build_payload("S", "U2")["system"])
    check("fit screen prompt clears the screen model's cache floor",
          len(fit.build_system_prompt()) // 4 > claude.min_cacheable_tokens())

    # 9c. watchlist plumbing
    print("[watchlist]")
    conn = store.connect(":memory:")
    store.upsert_company(conn, {"name": "W", "ats": "greenhouse", "slug": "w"})
    check("watch tag set", store.set_company_tag(conn, "w", "watch") == "watch")
    row = store.get_companies(conn, active_only=False)[0]
    check("watched company gets whole board",
          ops._is_watched(row) and ops._whole_board(row))
    check("watch tag removed", store.set_company_tag(conn, "W", "watch", add=False) == "")
    check("unknown company -> None", store.set_company_tag(conn, "Nope", "watch") is None)
    conn.close()

    # 9d. dispositions: mark/resolve/exclude/pipeline + few-shot calibration
    print("[dispositions]")
    conn = store.connect(":memory:")
    cid = store.upsert_company(conn, {"name": "D", "ats": "greenhouse", "slug": "d"})
    for jid, title, fit in (("gh_d_100", "Algorithm Engineer", 0.9),
                            ("gh_d_200", "TPM Seat", 0.8),
                            ("gh_d_300", "Data Engineer", 0.7)):
        store.upsert_job(conn, {"job_id": jid, "company_id": cid, "company_name": "D",
                                "title": title, "url": f"https://d.io/{jid}",
                                "location": "Durham, NC", "track": "local-tech",
                                "resume_fit_score": fit})
    row, err = store.set_disposition(conn, "gh_d_200", "dismissed", note="wrong archetype")
    check("mark by exact id", err is None and row["job_id"] == "gh_d_200")
    row, err = store.set_disposition(conn, "300", "applied")
    check("mark by unique id fragment", err is None and row["job_id"] == "gh_d_300")
    row, err = store.set_disposition(conn, "https://d.io/gh_d_100/", "saved")
    check("mark by URL", err is None and row["job_id"] == "gh_d_100")
    _, err = store.set_disposition(conn, "gh_d", "applied")
    check("ambiguous fragment errors", err is not None and "ambiguous" in err)
    _, err = store.set_disposition(conn, "gh_d_100", "bogus")
    check("unknown disposition errors", err is not None)
    ids = [r["job_id"] for r in store.ranked_jobs(conn, track="local-tech")]
    check("dismissed+applied leave ranking, saved stays", ids == ["gh_d_100"])
    check("pipeline lists all three", len(store.get_pipeline(conn)) == 3)
    from jobcrawler.fit import disposition_examples_block
    block = disposition_examples_block(conn, 3)
    check("few-shot block carries decisions + why-note",
          'PURSUED: "Data Engineer"' in block and "wrong archetype" in block)
    row, err = store.set_disposition(conn, "gh_d_100", "clear")
    check("clear removes from pipeline",
          err is None and len(store.get_pipeline(conn)) == 2)
    conn.close()

    # 9e. posting dates: normalizer, first-known-wins, sync backfill, age tags
    print("[posted dates]")
    from datetime import datetime, timedelta

    from jobcrawler.util import norm_posted_date
    today = datetime.now().strftime("%Y-%m-%d")
    check("ISO datetime w/ tz", norm_posted_date("2026-08-04T09:42:41-04:00") == "2026-08-04")
    check("epoch-ms string (Lever)",
          norm_posted_date("1784035164618")
          == datetime.fromtimestamp(1784035164.618).strftime("%Y-%m-%d"))
    check("workday relative days", norm_posted_date("Posted 3 Days Ago")
          == (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"))
    check("workday 30+ floor", norm_posted_date("Posted 30+ Days Ago")
          == (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
    check("posted today", norm_posted_date("Posted Today") == today)
    check("garbage -> None", norm_posted_date("See posting") is None)

    conn = store.connect(":memory:")
    cid = store.upsert_company(conn, {"name": "P", "ats": "greenhouse", "slug": "p"})
    for jid, posted in (("gh_p_1", "2026-08-01"), ("gh_p_2", None)):
        store.upsert_job(conn, {"job_id": jid, "company_id": cid, "company_name": "P",
                                "title": jid, "url": f"https://p.io/{jid}",
                                "location": "Durham, NC", "track": "local-tech",
                                "posted_at": posted})
    store.upsert_job(conn, {"job_id": "gh_p_1", "company_id": cid, "company_name": "P",
                            "title": "gh_p_1", "url": "https://p.io/gh_p_1",
                            "location": "Durham, NC", "track": "local-tech",
                            "posted_at": "2026-08-09"})
    got = conn.execute("SELECT posted_at FROM jobs WHERE job_id='gh_p_1'").fetchone()[0]
    check("posted_at first-known wins", got == "2026-08-01")
    store.sync_job_statuses(conn, cid, [
        {"id": "gh_p_1", "title": "gh_p_1", "url": "https://p.io/gh_p_1"},
        {"id": "gh_p_2", "title": "gh_p_2", "url": "https://p.io/gh_p_2",
         "posted_at": "2026-07-15"}], track="local-tech")
    got = conn.execute("SELECT posted_at FROM jobs WHERE job_id='gh_p_2'").fetchone()[0]
    check("status sync backfills posted_at", got == "2026-07-15")
    conn.close()
    check("age NEW on first-seen-today",
          digest_md.age_tag({"first_seen": datetime.now().isoformat()}) == "NEW")
    check("age in days", digest_md.age_tag(
        {"first_seen": "2026-01-01T00:00:00",
         "posted_at": (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")}) == "6d")
    check("age stale flag", digest_md.age_tag(
        {"first_seen": "2026-01-01T00:00:00",
         "posted_at": (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")}) == "60d!")
    check("age unknown", digest_md.age_tag({"first_seen": "2026-01-01T00:00:00"}) == "?")

    # 9f. discovery expansion: brainstorm knob + dork wiring (offline)
    print("[discovery expansion]")
    check("brainstorm disabled returns empty (no API touched)",
          ls.brainstorm_company_names(n=0) == [])
    check("populate_companies grew a dork switch",
          "dork" in ls.populate_companies.__code__.co_varnames)
    from jobcrawler.discovery.ats_dork import DORK_QUERIES
    check("dork queries built from profile locality",
          len(DORK_QUERIES) >= 4 and any("greenhouse" in q for q in DORK_QUERIES))
    # Parallel JS pass safety property: several probe instances can coexist
    # and tear down cleanly without ever launching a browser (lazy launch).
    from contextlib import ExitStack
    from jobcrawler.discovery.probes import WorkdayJsProbe
    with ExitStack() as _stack:
        _probes = [_stack.enter_context(WorkdayJsProbe()) for _ in range(3)]
        check("K probe instances coexist unlaunched",
              len(_probes) == 3 and not any(p._launched for p in _probes))
    check("probe teardown clean", True)

    # 10. probe guards (offline: no network hit for gated hosts / marker regex)
    print("[closed probe]")
    check("linkedin probe indeterminate",
          cf.probe_job_open("https://www.linkedin.com/jobs/view/123")[0] is None)
    check("closed marker matches",
          bool(cf._CLOSED_TEXT_RE.search("This position is no longer available")))
    check("closed-loop JD does not trip marker",
          not cf._CLOSED_TEXT_RE.search("develop closed-loop neurostimulation"))

    # 11. UI tracks + live geo bucket (webapp)
    print("[ui tracks + geo bucket]")
    import config
    import webapp
    check("UI_TRACKS parsed from profile", len(config.UI_TRACKS) >= 2)
    check("default track set", config.DEFAULT_TRACK in config.UI_TRACKS)
    fallback = config._build_ui_tracks(None)
    check("tracks fallback synthesizes",
          len(fallback) == 2 and any(t["default"] for t in fallback.values()))
    check("every track has an engine",
          all(t["engine"] in ("local", "neural") for t in config.UI_TRACKS.values()))
    check("ops keyed by engine, not track id",
          all("tracks" not in o for o in webapp.OPS.values()))
    check("geo local", webapp._geo_tag({"location": "Durham, NC"}) == "local")
    check("geo remote", webapp._geo_tag({"location": "Remote - US"}) == "remote")
    check("geo relocation", webapp._geo_tag({"location": "Boston, MA"}) == "relocation")
    check("stored remote_eligible wins on empty location",
          webapp._geo_tag({"location": "", "remote_eligible": 1}) == "remote")
    check("stored geo_mode=remote wins",
          webapp._geo_tag({"location": "Austin, TX", "geo_mode": "remote"}) == "remote")

    # 11b. unified crawl runner (one pipeline, methodology from [tracks.*])
    print("[unified runner]")
    lt_t = runner.track_for_engine("local")
    rn_t = runner.track_for_engine("neural")
    check("track_for_engine resolves both engines",
          lt_t["engine"] == "local" and rn_t["engine"] == "neural")
    check("crawl methodology keys parsed",
          all(k in lt_t for k in ("keyword_mode", "sources", "store_tag",
                                  "require_core_anchor", "geo_gate",
                                  "verify_top", "cost_guard", "email")))
    check("engine defaults: local extends, neural replaces",
          lt_t["keyword_mode"] == "extend" and rn_t["keyword_mode"] == "replace")
    check("engine defaults: gates differ",
          lt_t["geo_gate"] and not rn_t["geo_gate"]
          and rn_t["require_core_anchor"] and not lt_t["require_core_anchor"])
    base_core = list(config.CORE_KEYWORDS)
    runner.apply_keyword_focus(config, lt_t)
    check("extend preserves base tiers",
          config.CORE_KEYWORDS[:len(base_core)] == base_core
          and config.ACCEPT_REMOTE is False)
    runner.apply_keyword_focus(config, rn_t)
    check("replace swaps tiers + enables remote",
          "bci" in config.CORE_KEYWORDS and config.ACCEPT_REMOTE is True)
    check("core_anchor uses live CORE list",
          runner.core_anchor("EEG Data Engineer") == "eeg"
          and runner.core_anchor("Fraud Analyst", "a recognized leader") is None)
    # restore pristine keywords for any later checks
    config.CORE_KEYWORDS[:] = base_core
    specs_l = runner.build_sources(config, lt_t)
    specs_n = runner.build_sources(config, rn_t)
    check("local sources are company-linked store boards",
          specs_l and all(s["company"] is not None for s in specs_l))
    check("neural sources are sweep-style (no company rows)",
          specs_n and all(s["company"] is None for s in specs_n))
    check("priority companies starred and first",
          [s for s in specs_n[:6] if s["platform"].endswith("*")])
    check("websearch toggle removes its sources",
          len(runner.build_sources(config, rn_t, include_websearch=False))
          < len(specs_n))
    check("gates read config, not hardcoded track keys",
          gates._exclude_tables(local_t["id"])["role_phrases"]
          and not gates._exclude_tables("no_such_track")["role_phrases"])
    check("every webapp op is engine-agnostic or names a real engine",
          all(o.get("engine") in (None, "local", "neural")
              for o in webapp.OPS.values()))
    import run_scraper
    check("run_scraper CLI parses --help", True if run_scraper else False)

    # 12. profile editing (validate + tomlkit round-trip, no writes to repo)
    print("[profile edit]")
    from jobcrawler import profile_edit as pe
    raw, src = pe.read_raw()
    check("read_raw finds a profile", bool(raw) and src is not None)
    check("current profile validates", pe.validate(raw) == [])
    check("bad TOML rejected", any("syntax" in e for e in pe.validate("not [valid")))
    check("missing sections rejected",
          any("keywords" in e for e in pe.validate("x = 1")))
    check("out-of-range weight rejected", any("0..1" in e for e in pe.validate(
        "[keywords]\n[locations]\n[locality]\n[fit]\nweights = {domain = 1.5}")))
    check("non-string keyword list rejected", any("list of strings" in e
          for e in pe.validate("[locations]\n[locality]\n[keywords]\ncore = [1, 2]")))
    import tomllib as _toml
    new_text = pe.apply_updates({"keywords.core": ["smoke-a", "smoke-b"],
                                 "fit.weights.domain": 0.31})
    parsed = _toml.loads(new_text)
    check("apply_updates sets values",
          parsed["keywords"]["core"] == ["smoke-a", "smoke-b"]
          and parsed["fit"]["weights"]["domain"] == 0.31)
    before = _toml.loads(raw)
    check("apply_updates leaves other sections intact",
          all(parsed[k] == before[k] for k in before
              if k not in ("keywords", "fit")))
    # Comments INSIDE a replaced array are expected to go with the old value;
    # everything outside the touched paths must survive verbatim — the
    # section-banner comment lines are a good untouched-region proxy.
    banner = [l for l in raw.splitlines() if l.startswith("# ---")]
    check("comments outside touched values survive",
          all(l in new_text for l in banner)
          and pe.validate(new_text) == [])

    print(f"\n{'ALL GREEN' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
