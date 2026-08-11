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
    import jobcrawler.db as db
    import jobcrawler.nc as nc
    import jobcrawler.resume as resume
    import jobcrawler.fetchers.company as cf
    import jobcrawler.tracks.local_tech as lt
    import jobcrawler.tracks.remote_neural as rn
    import jobcrawler.tracks.remote_neural_run as rnr
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
    check("geo_mode onsite", lt.geo_mode("Durham, NC") == "onsite")
    check("geo_mode remote", lt.geo_mode("Remote - US") == "remote")
    check("geo_mode none", lt.geo_mode("Boston, MA") is None)
    check("'distributed training' is not remote",
          lt.geo_mode("", "we do distributed training at scale") is None)

    # 3. exclude gate (the substring-bug fixes)
    print("[exclude gate]")
    check("CRA excluded", bool(lt.exclude_reason("Clinical Research Associate (CRA)")))
    check("scribe not in 'describe'", not lt.exclude_reason("Engineer", "you will describe systems"))
    check("defi not in 'defibrillator'", not lt.exclude_reason("Engineer", "implantable defibrillator"))
    check("DoD radar excluded", bool(lt.exclude_reason("RF Engineer", "military radar for DoD")))

    # 4. technical pre-filter
    print("[technical filter]")
    check("engineer is technical", lt.is_technical_role("Quality Engineer"))
    check("data manager is technical", lt.is_technical_role("Clinical Data Manager"))
    check("nurse not technical", not lt.is_technical_role("Registered Nurse"))

    # 5. remote-neural gates
    print("[remote-neural gates]")
    check("EEG anchors", rn.is_neural_role("EEG Data Engineer"))
    check("'recognized' doesn't anchor ecog", not rn.is_neural_role("Fraud Analyst",
                                                                    "a recognized leader"))
    check("subcortical anchors cortical", rn.is_neural_role("Scientist", "subcortical recordings"))
    check("controller not technical title", not rn.is_technical_role("Corporate Controller"))
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

    # 9c. watchlist plumbing
    print("[watchlist]")
    conn = store.connect(":memory:")
    store.upsert_company(conn, {"name": "W", "ats": "greenhouse", "slug": "w"})
    check("watch tag set", store.set_company_tag(conn, "w", "watch") == "watch")
    row = store.get_companies(conn, active_only=False)[0]
    check("watched company gets whole board",
          lt._is_watched(row) and lt._whole_board(row))
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
          lt._age_tag({"first_seen": datetime.now().isoformat()}) == "NEW")
    check("age in days", lt._age_tag(
        {"first_seen": "2026-01-01T00:00:00",
         "posted_at": (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")}) == "6d")
    check("age stale flag", lt._age_tag(
        {"first_seen": "2026-01-01T00:00:00",
         "posted_at": (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")}) == "60d!")
    check("age unknown", lt._age_tag({"first_seen": "2026-01-01T00:00:00"}) == "?")

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

    print(f"\n{'ALL GREEN' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
