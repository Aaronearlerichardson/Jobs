#!/usr/bin/env python3
"""
discover.py - expand the crawler's company universe.

Asks Claude for likely employers in a sector, then probes public ATSes
(Greenhouse/Lever/Ashby/Kula) to confirm which slugs are real. Outputs
copy-paste dict entries you can drop straight into config.py.

Also provides:
  * A credentials manager for gated sites (legacy cookie-paste).
  * A Playwright-based session capture tool that opens a real browser,
    lets you log in manually, and saves cookies/localStorage so later
    fetches can reuse the authenticated session without storing a password.

Usage:
    python discover.py "neurotech startups"
    python discover.py "medical device companies hiring engineers"
    python discover.py --from-keywords          # seed from INCLUDE_KEYWORDS
    python discover.py --credentials-init       # scaffold credentials.json
    python discover.py --credentials-check      # print what's configured
    python discover.py --capture-session linkedin   # open browser, save session
    python discover.py --list-sessions          # show captured sessions + age
    python discover.py --test-session linkedin  # verify a saved session still works
"""

import argparse
import sys

from config import INCLUDE_KEYWORDS, SITE_CONFIGS
from jobcrawler.discovery import (
    apply_to_config,
    discover,
    print_summary,
    write_discovery_report,
)
from jobcrawler.sessions import (
    capture_session,
    check_credentials,
    configure,
    init_credentials_template,
    list_sessions,
    test_session,
)


def main():
    ap = argparse.ArgumentParser(description="Expand the crawler's company universe.")
    ap.add_argument("term", nargs="?",
                    help="Sector/industry/term to search for (e.g. 'neurotech startups')")
    ap.add_argument("--from-keywords", action="store_true",
                    help="Run discovery for each entry in INCLUDE_KEYWORDS")
    ap.add_argument("--no-report", action="store_true",
                    help="Print to stdout only, don't write a markdown report")
    ap.add_argument("--apply", action="store_true",
                    help="Insert confirmed candidates into config.py in place "
                         "(deduped by slug, tagged with date/term for audit)")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --apply, preview changes without writing the file")

    g = ap.add_argument_group("credentials (legacy cookie-paste flow)")
    g.add_argument("--credentials-init", action="store_true",
                   help="Create credentials.json.template and scaffold credentials.json")
    g.add_argument("--credentials-check", action="store_true",
                   help="Show which credential blocks are populated")

    s = ap.add_argument_group("session capture (preferred; Playwright)")
    s.add_argument("--capture-session", metavar="SITE",
                   help=f"Open a browser, log in manually, save session "
                        f"(sites: {', '.join(SITE_CONFIGS)})")
    s.add_argument("--test-session", metavar="SITE",
                   help="Verify a saved session is still valid")
    s.add_argument("--list-sessions", action="store_true",
                   help="Show captured sessions and their age")
    s.add_argument("--browser", choices=["chrome", "chromium", "firefox"],
                   default="chrome",
                   help="Which browser engine to use. Default 'chrome' uses your "
                        "real installed Chrome (best for Cloudflare). 'chromium' "
                        "uses Playwright's bundled build. 'firefox' is an "
                        "alternative if Chrome fingerprinting is the problem.")
    s.add_argument("--use-profile", action="store_true",
                   help="Launch Chrome with a COPY of your real user profile "
                        "(cookies, history, extensions). Strongest Cloudflare "
                        "bypass. Chrome must be CLOSED first. The copy lives "
                        "at sessions/chrome-profile/ and is reused across runs. "
                        "Implies --browser chrome.")
    s.add_argument("--refresh-profile", action="store_true",
                   help="With --use-profile, re-copy from the live Chrome "
                        "profile instead of reusing the cached copy. Use this "
                        "after logging into a site in regular Chrome.")
    s.add_argument("--user-data-dir", metavar="PATH",
                   help="Override the SOURCE Chrome user-data dir to copy from. "
                        "Defaults to the OS standard location. Only used with "
                        "--use-profile.")
    s.add_argument("--profile-directory", metavar="NAME", default="Default",
                   help="Profile subfolder name (e.g. 'Default', 'Profile 1'). "
                        "Only used with --use-profile.")

    args = ap.parse_args()

    configure(
        browser_choice    = args.browser,
        use_profile       = args.use_profile,
        user_data_dir     = args.user_data_dir,
        profile_directory = args.profile_directory,
        refresh_profile   = args.refresh_profile,
    )

    if args.credentials_init:
        init_credentials_template(); return
    if args.credentials_check:
        check_credentials(); return
    if args.list_sessions:
        list_sessions(); return
    if args.capture_session:
        capture_session(args.capture_session); return
    if args.test_session:
        ok = test_session(args.test_session)
        sys.exit(0 if ok else 1)

    if args.from_keywords:
        for kw in INCLUDE_KEYWORDS:
            result = discover(kw)
            print_summary(result)
            if not args.no_report:
                write_discovery_report(result)
            if args.apply:
                for line in apply_to_config(result, dry_run=args.dry_run):
                    print(line)
        return

    if not args.term:
        ap.print_help()
        sys.exit(1)

    result = discover(args.term)
    print_summary(result)
    if not args.no_report:
        write_discovery_report(result)
    if args.apply:
        for line in apply_to_config(result, dry_run=args.dry_run):
            print(line)


if __name__ == "__main__":
    main()
