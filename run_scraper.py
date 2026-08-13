#!/usr/bin/env python3
"""Job crawler — daily refresh + maintenance CLI.

    python run_scraper.py                        # crawl EVERY configured track
    python run_scraper.py --track local_tech     # crawl one track
    python run_scraper.py --track remote_neural --preview --no-fit
    python run_scraper.py --sync-status          # maintenance (add --track)
    python run_scraper.py --mark applied <job|url> --why "..."

Tracks come from profile.toml [tracks.*] (any id you define); legacy names
("local-tech"/"remote-neural") match a track's jobs.track value. The old
crawler.py forwards here, so scheduled tasks keep working.
"""

import argparse
import sys

import config

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _resolve_track(name):
    """A configured track by id (local_tech) or jobs.track value (local-tech)."""
    t = config.UI_TRACKS.get(name)
    if t:
        return t
    for t in config.UI_TRACKS.values():
        if t["track"] == name:
            return t
    raise SystemExit(f"  [!] unknown track {name!r}; configured: "
                     f"{', '.join(config.UI_TRACKS)}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Job crawler — daily refresh + maintenance",
        epilog="No flags = crawl every configured track (the daily refresh).")
    # ── crawl scope ─────────────────────────────────────────────────────
    ap.add_argument("--track", metavar="ID",
                    help="Operate on ONE configured track (profile.toml "
                         "[tracks.*] id, or its jobs.track value)")
    ap.add_argument("--preview", action="store_true",
                    help="Crawl without DB writes or email")
    ap.add_argument("--no-fit", action="store_true",
                    help="Crawl without resume-fit scoring (no Claude spend)")
    ap.add_argument("--send", action="store_true",
                    help="Email the digest even if the track config says not to")
    ap.add_argument("--no-websearch", action="store_true",
                    help="Skip the web-search sources this run")
    ap.add_argument("--confirm-cost", action="store_true",
                    help="Allow scoring past the track's cost_guard threshold")
    ap.add_argument("--samples", type=int, default=5,
                    help="Sample matches printed for sweep tracks (default 5)")
    ap.add_argument("--top", type=int, default=15, help="Top-N for digests")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip the deep second-pass verification of the top N")
    # ── maintenance (single track; --track selects, default = default track) ──
    ap.add_argument("--sync-status", action="store_true",
                    help="Reconcile open/closed against live boards (no API)")
    ap.add_argument("--verify-top", type=int, nargs="?", const=15,
                    default=None, metavar="N",
                    help="Deep-verify the current top N stored jobs, no crawl")
    ap.add_argument("--check-closed", action="store_true",
                    help="Probe stale job URLs and close the provably dead")
    ap.add_argument("--stale-days", type=int, default=2)
    ap.add_argument("--rescore", action="store_true",
                    help="Re-score every stored job with the current rubric")
    ap.add_argument("--described-only", action="store_true",
                    help="With --rescore: only rows with a real JD body")
    ap.add_argument("--backfill-descriptions", action="store_true",
                    help="Fetch missing Workday JD text (CXS endpoint)")
    ap.add_argument("--backfill-board-descriptions", action="store_true",
                    help="Fetch missing JD text via each company's own board")
    ap.add_argument("--backfill-axes", action="store_true",
                    help="Populate per-axis fit columns from fit_reason (offline)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap rows processed (backfills / probes)")
    # ── roster / dispositions / store ───────────────────────────────────
    ap.add_argument("--dedup", action="store_true",
                    help="Merge duplicate company rows pointing at one board")
    ap.add_argument("--watch", metavar="COMPANY",
                    help="Tag a company watched (whole board, digest-flagged)")
    ap.add_argument("--unwatch", metavar="COMPANY")
    ap.add_argument("--mark", nargs=2, metavar=("DISPOSITION", "JOB"),
                    help="saved|applied|interviewing|rejected|dismissed|clear "
                         "+ job_id/fragment/URL. Pair with --why.")
    ap.add_argument("--why", metavar="TEXT",
                    help="With --mark: one-line reason (teaches the scorer)")
    ap.add_argument("--pipeline", action="store_true",
                    help="Print every dispositioned job, then exit")
    ap.add_argument("--prune", action="store_true",
                    help="Deactivate companies whose ATS board is dead")
    ap.add_argument("--prune-offmission", action="store_true")
    ap.add_argument("--export-companies", metavar="PATH")
    ap.add_argument("--import-companies", metavar="PATH")
    ap.add_argument("--nlx", metavar="COMPANIES",
                    help="Ingest NLx feed postings for comma-separated employers")
    ap.add_argument("--db", metavar="PATH",
                    help="Override the store DB path (isolates concurrent runs)")
    # ── keyword tools ───────────────────────────────────────────────────
    ap.add_argument("--expand", metavar="TERM",
                    help="Expand a term into titles/keywords/sectors and exit")
    ap.add_argument("--expand-location", metavar="TERM")
    ap.add_argument("--keyword-report", action="store_true",
                    help="Bulk-expand every configured keyword into a report")
    ap.add_argument("--score", metavar="TEXT",
                    help="Score one title/description on technical bar (0..1)")
    args = ap.parse_args(argv)

    t = _resolve_track(args.track) if args.track else None
    if args.db:
        from pathlib import Path
        config.STORE_DB_PATH = Path(args.db)
        if t is not None:
            t = dict(t, db_path=Path(args.db))

    # ── one-shot store / roster commands ────────────────────────────────
    if args.dedup:
        from jobcrawler import store
        conn = store.connect(t["db_path"] if t else None)
        n = store.dedup_companies(conn)
        conn.close()
        print(f"\n  merged {n} duplicate company row(s) into their canonical board.")
        return

    if args.watch or args.unwatch:
        from jobcrawler import store
        name = args.watch or args.unwatch
        conn = store.connect(t["db_path"] if t else None)
        tags = store.set_company_tag(conn, name, "watch", add=bool(args.watch))
        conn.close()
        if tags is None:
            print(f"  [!] no company named {name!r} in the store "
                  f"(names are matched case-insensitively but exactly).")
        else:
            verb = "watching" if args.watch else "unwatched"
            print(f"  {verb} {name}  (tags: {tags or 'none'})")
        return

    if args.mark:
        from jobcrawler import store
        disp, ref = args.mark
        conn = store.connect(t["db_path"] if t else None)
        row, err = store.set_disposition(conn, ref, disp, note=args.why)
        conn.close()
        if err:
            print(f"  [!] {err}")
            raise SystemExit(1)
        act = ("cleared" if disp.strip().lower() in ("none", "clear")
               else f"marked {disp.strip().lower()}")
        print(f"  {act}: {row['title']} @ {row['company_name']}")
        print(f"    {row['job_id']}")
        if args.why:
            print(f"    why: {args.why}")
        return

    if args.pipeline:
        from jobcrawler import store
        conn = store.connect(t["db_path"] if t else None)
        rows = store.get_pipeline(conn)
        conn.close()
        if not rows:
            print("  pipeline empty - record decisions with: "
                  "python run_scraper.py --mark applied <job_id|url>")
        for p in rows:
            state = "CLOSED" if p.get("status") == "closed" else "open"
            note = f"  - {p['disposition_note']}" if p.get("disposition_note") else ""
            print(f"  {p['disposition']:<12} {(p.get('disposition_at') or '')[:10]}"
                  f"  [{state:<6}] {(p['title'] or '')[:44]} @ "
                  f"{p['company_name']}{note}")
        return

    if args.prune:
        from jobcrawler import store
        conn = store.connect(t["db_path"] if t else None)
        n_dead, n_off = store.prune_dead_boards(
            conn, deactivate_offmission=args.prune_offmission)
        conn.close()
        print(f"\n  deactivated {n_dead} dead-board compan(ies)"
              + (f" + {n_off} off-mission" if args.prune_offmission else "")
              + ".")
        return

    if args.export_companies or args.import_companies:
        from jobcrawler import store
        conn = store.connect(t["db_path"] if t else None)
        if args.export_companies:
            n = store.export_companies(conn, args.export_companies)
            print(f"  exported {n} compan(ies) -> {args.export_companies}")
        if args.import_companies:
            n = store.import_companies(conn, args.import_companies)
            print(f"  imported/refreshed {n} compan(ies) from {args.import_companies}")
        conn.close()
        return

    if args.score:
        from jobcrawler.claude import score_technical_bar
        score, reason, mission = score_technical_bar(args.score)
        if score is None:
            print("  [!] Scorer unavailable (set ANTHROPIC_API_KEY).")
        else:
            print(f"  technical-bar score: {score:.2f}  [{mission or 'mission?'}]  ({reason})")
        return

    if args.nlx:
        from jobcrawler.fetchers.careeronestop import fetch_nlx_company
        from jobcrawler.ops import ingest_external_jobs
        total = 0
        for name in [n.strip() for n in args.nlx.split(",") if n.strip()]:
            jobs = fetch_nlx_company(name)
            print(f"  {name}: {len(jobs)} NLx posting(s)")
            if jobs:
                total += ingest_external_jobs(jobs, source="nlx", t=t)
        print(f"\n  {total} new job(s) ingested from the NLx feed.")
        return

    # ── keyword expansion tools ─────────────────────────────────────────
    if args.expand:
        from jobcrawler.claude import expand_search
        from jobcrawler.expand import print_expansion
        expanded = expand_search(args.expand)
        if expanded:
            print_expansion(args.expand, expanded)
        return

    if args.expand_location:
        from jobcrawler.claude import expand_location
        from jobcrawler.expand import print_location_expansion
        expanded = expand_location(args.expand_location)
        if expanded:
            print_location_expansion(args.expand_location, expanded)
        return

    if args.keyword_report:
        from jobcrawler.expand import generate_keyword_report
        generate_keyword_report()
        return

    # ── maintenance ─────────────────────────────────────────────────────
    if args.verify_top is not None:
        from jobcrawler.ops import verify_top_cli
        verify_top_cli(top_n=args.verify_top, max_workers=args.workers, t=t)
        return
    if args.sync_status:
        from jobcrawler.ops import sync_status_all
        sync_status_all(top_n=args.top, t=t)
        return
    if args.check_closed:
        from jobcrawler.ops import check_closed_jobs
        check_closed_jobs(max_workers=args.workers, limit=args.limit,
                          stale_days=args.stale_days, t=t)
        return
    if args.backfill_descriptions:
        from jobcrawler.fetchers.workday import backfill_workday_descriptions
        backfill_workday_descriptions(max_workers=args.workers,
                                      limit=args.limit)
        return
    if args.backfill_board_descriptions:
        from jobcrawler.ops import backfill_board_descriptions
        backfill_board_descriptions(max_workers=args.workers,
                                    limit=args.limit, t=t)
        return
    if args.backfill_axes:
        from jobcrawler import store
        conn = store.connect(t["db_path"] if t else None)
        store.backfill_axis_columns(conn)
        conn.close()
        return
    if args.rescore:
        from jobcrawler.ops import rescore_all
        rescore_all(max_workers=args.workers,
                    described_only=args.described_only, t=t)
        return

    # ── the crawl (daily refresh): one track, or every configured track ──
    from jobcrawler import runner
    tracks = [t] if t else list(config.UI_TRACKS.values())
    for tcfg in tracks:
        runner.run_track(tcfg,
                         fit=not args.no_fit,
                         commit=not args.preview,
                         send=args.send or None,
                         verify=False if args.no_verify else None,
                         websearch=False if args.no_websearch else None,
                         confirm_cost=args.confirm_cost,
                         max_workers=args.workers, top_n=args.top,
                         samples=args.samples)


if __name__ == "__main__":
    main()
