#!/usr/bin/env python3
"""
Offline smoke tests for the LOCAL-TECH pipeline. No network / no API key.
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
    import jobcrawler.local_fetch as lf
    import jobcrawler.local_tech as lt
    import jobcrawler.local_clinical as lc
    import jobcrawler.resume as resume
    import jobcrawler.discovery.sniffer as sniffer
    import jobcrawler.discovery.local_sourcing as ls
    import jobcrawler.discovery.ats_dork as dork
    print("[imports OK]")

    # 2. NC locality detection
    print("[NC locality]")
    check("Durham, NC is NC", lf._is_nc("Durham, NC"))
    check("Boston, MA is not NC", not lf._is_nc("Boston, MA"))
    check("geo_mode onsite", lc.geo_mode("Durham, NC") == "onsite")
    check("geo_mode remote", lc.geo_mode("Remote - US") == "remote")
    check("geo_mode none", lc.geo_mode("Boston, MA") is None)

    # 3. exclude gate (the substring-bug fixes)
    print("[exclude gate]")
    check("CRA excluded", bool(lc.exclude_reason("Clinical Research Associate (CRA)")))
    check("scribe not in 'describe'", not lc.exclude_reason("Engineer", "you will describe systems"))
    check("defi not in 'defibrillator'", not lc.exclude_reason("Engineer", "implantable defibrillator"))
    check("DoD radar excluded", bool(lc.exclude_reason("RF Engineer", "military radar for DoD")))

    # 4. technical pre-filter
    print("[technical filter]")
    check("engineer is technical", lt._is_technical("Quality Engineer"))
    check("data manager is technical", lt._is_technical("Clinical Data Manager"))
    check("nurse not technical", not lt._is_technical("Registered Nurse"))

    # 5. sniffer slug extraction + custom-board detector
    print("[sniffer]")
    boards = dork.extract_boards_from_urls(["https://jobs.lever.co/bioagilytix/x",
                                            "https://boards.greenhouse.io/pendo/jobs/1"])
    check("dork extracts lever+gh", {("lever", "bioagilytix"), ("greenhouse", "pendo")} <= set(boards))
    check("custom-board needs real job links",
          not sniffer._looks_like_custom_board("<a href='/careers/'>Careers</a>"))

    # 6. store schema
    print("[store]")
    conn = store.connect(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    check("companies has mission_score", "mission_score" in cols)
    jcols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    check("jobs has resume_fit_score", "resume_fit_score" in jcols)

    print(f"\n{'ALL GREEN' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
