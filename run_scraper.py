#!/usr/bin/env python3
"""Job crawler — daily refresh + maintenance CLI.

    python run_scraper.py                   # crawl EVERY configured track
    python run_scraper.py --track local     # crawl one track
    python run_scraper.py --track remote --preview --no-fit
    python run_scraper.py --sync-status     # maintenance (add --track)
    python run_scraper.py --mark applied <job|url> --why "..."
    python run_scraper.py --where           # where are my profile and data?

Tracks come from your profile's [tracks.*] tables — the ids are whatever you
named them, and a track's jobs.track value works too. The old crawler.py
forwards here, so scheduled tasks keep working.

Most flags are the CLI spelling of an operation in core/registry.py —
the same table the web UI's buttons run from — so a flag and a button pass
the same parameters to the same function. The few commands below that are
not registry ops (watch, mark, pipeline, export/import, score) are store
queries and edits with their own positional arguments.
"""

import argparse
import sys

import config
from core.ops import registry

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _resolve_track(name):
    """A configured track by its id, or by its jobs.track value."""
    t = config.UI_TRACKS.get(name)
    if t:
        return t
    for t in config.UI_TRACKS.values():
        if t["track"] == name:
            return t
    raise SystemExit(f"  [!] unknown track {name!r}; configured: "
                     f"{', '.join(config.UI_TRACKS)}")


def _op(name, params):
    """A command handler that runs registry op `name` with the params
    `params(args)` draws off the parsed arguments, against the --track
    selection (None = the op's own default-track rule)."""
    def run(args, t):
        registry.invoke(name, params(args), track=t)
    return run


def _store(t):
    from core import store
    return store.connect(t["db_path"] if t else None)


def _cmd_watch(args, t):
    from core import store
    name = args.watch or args.unwatch
    conn = _store(t)
    tags = store.set_company_tag(conn, name, "watch", add=bool(args.watch))
    conn.close()
    if tags is None:
        print(f"  [!] no company named {name!r} in the store "
              f"(names are matched case-insensitively but exactly).")
    else:
        verb = "watching" if args.watch else "unwatched"
        print(f"  {verb} {name}  (tags: {tags or 'none'})")


def _cmd_mark(args, t):
    from core import store
    disp, ref = args.mark
    conn = _store(t)
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


def _cmd_pipeline(args, t):
    from core import store
    conn = _store(t)
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


def _cmd_companies_io(args, t):
    from core import store
    conn = _store(t)
    if args.export_companies:
        n = store.export_companies(conn, args.export_companies)
        print(f"  exported {n} compan(ies) -> {args.export_companies}")
    if args.import_companies:
        n = store.import_companies(conn, args.import_companies)
        print(f"  imported/refreshed {n} compan(ies) from {args.import_companies}")
    conn.close()


def _cmd_score(args, t):
    from core.claude import score_technical_bar
    score, reason, mission = score_technical_bar(args.score)
    if score is None:
        print("  [!] Scorer unavailable (set ANTHROPIC_API_KEY).")
    else:
        print(f"  technical-bar score: {score:.2f}  [{mission or 'mission?'}]  ({reason})")


# One-shot commands, in precedence order: the first whose flag is set runs
# and the process exits. `dest` is the argparse attribute that selects it.
_COMMANDS = [
    ("dedup", _op("dedup", lambda a: {})),
    ("watch", _cmd_watch),
    ("unwatch", _cmd_watch),
    ("mark", _cmd_mark),
    ("pipeline", _cmd_pipeline),
    ("prune", _op("prune", lambda a: {"offmission": a.prune_offmission})),
    ("export_companies", _cmd_companies_io),
    ("import_companies", _cmd_companies_io),
    ("score", _cmd_score),
    ("nlx", _op("nlx", lambda a: {"companies": a.nlx})),
    ("reresolve_misses", _op("reresolve", lambda a: {
        "limit": a.reresolve_misses, "workers": a.workers, "days": a.miss_days})),
    ("verify_top", _op("verify", lambda a: {
        "top": a.verify_top, "workers": a.workers, "force": a.verify_all})),
    ("sync_status", _op("sync", lambda a: {"top": a.top})),
    ("check_closed", _op("check-closed", lambda a: {
        "workers": a.workers, "limit": a.limit, "stale_days": a.stale_days})),
    ("backfill_descriptions", _op("backfill-workday", lambda a: {
        "workers": a.workers, "limit": a.limit})),
    ("backfill_board_descriptions", _op("backfill-descriptions", lambda a: {
        "workers": a.workers, "limit": a.limit})),
    ("backfill_axes", _op("backfill-axes", lambda a: {})),
    ("triage", _op("triage", lambda a: {
        "limit": a.limit, "workers": a.workers, "score_cap": a.score_cap})),
    ("rescore", _op("rescore", lambda a: {
        "workers": a.workers, "described_only": a.described_only})),
]


def _selected(args, dest):
    """True when the flag behind `dest` was given: store_true flags are
    True, valued flags are non-None (an explicit 0 still counts)."""
    v = getattr(args, dest)
    return v is not None and v is not False


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Job crawler — daily refresh + maintenance",
        epilog="No flags = crawl every configured track (the daily refresh).")
    # ── crawl scope ─────────────────────────────────────────────────────
    ap.add_argument("--track", metavar="ID",
                    help="Operate on ONE configured track (a [tracks.*] id "
                         "from your profile, or its jobs.track value)")
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
    ap.add_argument("--verify-all", action="store_true",
                    help="With --verify-top: re-verify rows the current "
                         "verify model already checked")
    ap.add_argument("--check-closed", action="store_true",
                    help="Probe stale job URLs and close the provably dead")
    ap.add_argument("--stale-days", type=int, default=2)
    ap.add_argument("--triage", action="store_true",
                    help="Gate, hydrate and score the harvester's pending "
                         "rows (scrapers/triage.py); --limit caps rows, "
                         "--score-cap caps fit calls")
    ap.add_argument("--score-cap", type=int, default=None, metavar="N",
                    help="With --triage: Claude fit calls this pass")
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
    ap.add_argument("--reresolve-misses", type=int, nargs="?", const=50,
                    metavar="N",
                    help="Retry the N oldest roster rows that never resolved "
                         "to a board (miss_reason no-board-found/board-dead); "
                         "hits are queued for review, not activated. "
                         "--miss-days D retries only misses at least D days "
                         "old")
    ap.add_argument("--miss-days", type=int, default=None, metavar="D",
                    help="With --reresolve-misses: skip misses newer than D "
                         "days")
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
    # ── scoring ─────────────────────────────────────────────────────────
    # (keyword/location expansion is report-only: tools/expand.py)
    ap.add_argument("--score", metavar="TEXT",
                    help="Score one title/description on technical bar (0..1)")
    ap.add_argument("--where", action="store_true",
                    help="Print where this install keeps your profile and "
                         "data, then exit")
    args = ap.parse_args(argv)

    from core import session_log
    session_log.start(list(argv) if argv is not None else sys.argv[1:])

    from core import bootstrap
    bootstrap.ensure_profile()
    if args.where:
        for line in bootstrap.status_lines():
            print(f"  {line}")
        return

    t = _resolve_track(args.track) if args.track else None
    if args.db:
        from pathlib import Path
        config.STORE_DB_PATH = Path(args.db)
        if t is not None:
            t = dict(t, db_path=Path(args.db))

    # ── one-shot store / roster / maintenance commands ──────────────────
    for dest, handler in _COMMANDS:
        if _selected(args, dest):
            handler(args, t)
            return

    # ── the crawl (daily refresh): one track, or every configured track ──
    params = {"no_fit": args.no_fit, "preview": args.preview, "send": args.send,
              "no_verify": args.no_verify, "no_websearch": args.no_websearch,
              "confirm_cost": args.confirm_cost, "workers": args.workers,
              "top": args.top, "samples": args.samples}
    for tcfg in ([t] if t else list(config.UI_TRACKS.values())):
        registry.invoke("crawl", params, track=tcfg)


if __name__ == "__main__":
    main()
