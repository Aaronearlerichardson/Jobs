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
    conn.close()

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
