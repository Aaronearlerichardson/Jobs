"""Phase 0 remediation: rescore jobs the missing-location bug in
src.claude.fit.score_resume_fit could have hurt (fixed 2026-09-16).

Before the fix, score_resume_fit's user turn carried title+description only
-- no JOB LOCATION line, unlike verify_fit -- so the geo gate had nothing
but JD prose to judge geography from. A local, in-region posting whose body
never names a city (most ATS postings: the location lives in a stored
FIELD, not the prose) could get gate:geo'd to ~0.20x its true score. Real
examples from the live store: a TARGAN "Data Scientist" stored at "Main
Office, Raleigh, NC, US" scored 0.07 ("no NC/remote location stated"); a
Verily "Software Engineer III" in Raleigh scored 0.04 with gate:geo.

Selection:
  - triage_status='fit' rows (Claude scored them, but they landed under
    every surfaced track's digest_min_fit) -- the bug's most direct
    victims, OR
  - crawl-scored rows (track IS NOT NULL AND triage_status IS NULL) whose
    fit_gates already contains 'geo' -- the crawl's own first-pass scorer
    (src.ops.maintenance._score_job) tripped the gate under the old,
    location-blind prompt,
  restricted to rows whose location either matches the profile's locality
  regex (src.match.locality.NC_RE -- itself derived from profile.toml
  [locality], nothing hard-coded here) or contains "remote". That is the
  ONLY population where adding the location line can change the verdict;
  rescoring anything else would just burn API budget for no possible
  change.

Both structural filters (triage_status='fit', triage_status IS NULL) hit
the ix_jobs_triage index (EXPLAIN QUERY PLAN: a MULTI-INDEX OR over it);
the locality check runs in Python against the SAME regex the live
crawl/triage gates use, so no city name is duplicated here.

Why a tools/ script and not an extension of the `rescore` op
(src.dispatch.registry / scoring.rescore_all): the op's `where` shape is a
flat SQL predicate the CLI/registry passes as a plain string;
this selection's locality half is NOT expressible as SQL without either
(a) inlining city names into a LIKE chain here (exactly what the "no
hard-coded locality" rule forbids), or (b) accepting a Python callable as
an op param (the registry's OpParams fields are primitives for a reason --
CLI + webapp both drive it). Doing the structural half in SQL and the locality
half in Python, in one purpose-built script, was the smaller mismatch. It
is also explicitly a ONE-TIME migration for this bug, not a recurring
op -- rescore_all (already fixed to pass location -- see scoring.py)
remains the right tool for a general "rescore everything" pass.

Usage (from the repo root; `python tools/phase0_geo_rescore.py ...` works
the same):
  python -m tools.phase0_geo_rescore --pilot 25            # pilot, writes DB
  python -m tools.phase0_geo_rescore --pilot 25 --dry-run  # no DB writes
  python -m tools.phase0_geo_rescore --full                # the rest, writes DB + CSV
  python -m tools.phase0_geo_rescore --full --skip ids.txt # exclude already-piloted job_ids

Safety, checked in this order before any row is touched (paths are under
config.DATA_DIR, the same directory config.STORE_DB_PATH resolves in):
  1. --full refuses to run without --go (or --dry-run): pilot first
  2. the pre-Phase0 DB backup must exist (db_backups/jobs_20260916_pre_phase0.db)
  3. ANTHROPIC_API_KEY must be set (see confirm_api_key)
  4. no logs/session-*-harvest.log modified in the last 10 minutes may be
     missing its "# ended" footer (a harvest pass mid-run)
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools._harness import console_utf8                    # noqa: E402
from src import config, store                              # noqa: E402
from src.claude.fit import score_resume_fit                # noqa: E402
from src.match.locality import NC_RE                       # noqa: E402

BACKUP_PATH = config.DATA_DIR / "db_backups" / "jobs_20260916_pre_phase0.db"
LOG_DIR = config.DATA_DIR / "logs"
CSV_PATH = config.DATA_DIR / "rescore_20260916.csv"
DIGEST_MIN_FIT_FALLBACK = 0.40   # used only if a row's track isn't in UI_TRACKS
CSV_FIELDS = ["job_id", "title", "company_name", "location", "old_score",
              "new_score", "delta", "old_gates", "new_gates", "crossed_floor"]

_SELECT_SQL = """
    SELECT job_id, title, company_name, location, track,
           triage_status, resume_fit_score, fit_gates, fit_reason,
           fit_model, description
    FROM jobs
    WHERE (triage_status = 'fit')
       OR (track IS NOT NULL AND triage_status IS NULL
           AND fit_gates LIKE '%geo%')
"""


def _is_local_or_remote(location: str) -> bool:
    loc = location or ""
    return bool(NC_RE.search(loc)) or ("remote" in loc.lower())


def select_candidates(conn, exclude_ids=frozenset()):
    """Every row the bug could have hurt, per the brief's two-population
    selection plus the locality/remote restriction. `exclude_ids` lets
    --full skip rows the pilot already rescored."""
    rows = [dict(r) for r in conn.execute(_SELECT_SQL).fetchall()]
    return [r for r in rows
           if r["job_id"] not in exclude_ids and _is_local_or_remote(r.get("location"))]


def _floor_for_row(row):
    tracks = store.track_set(row.get("track"))
    floors = [t["digest_min_fit"] for t in config.UI_TRACKS.values()
              if t["track"] in tracks]
    return min(floors) if floors else DIGEST_MIN_FIT_FALLBACK


def confirm_backup():
    if not BACKUP_PATH.exists():
        raise SystemExit(f"[!] Backup not found at {BACKUP_PATH} -- refusing to write.")
    print(f"  backup OK: {BACKUP_PATH} ({BACKUP_PATH.stat().st_size / 1e6:.0f} MB)")


def confirm_api_key():
    """Refuse to run without a real key. Without one, call_claude_json
    fails per-row and score_resume_fit's `reason` for that comes back
    identical to a legitimate malformed-JSON miss ("unscored") -- not
    distinguishable after the fact -- so update_job_scores would silently
    NULL out every candidate's real, already-good score. Fail loud instead,
    before touching a single row."""
    key = getattr(config, "ANTHROPIC_API_KEY", "") or ""
    if not key or key == "YOUR_ANTHROPIC_API_KEY_HERE":
        raise SystemExit("[!] ANTHROPIC_API_KEY is not set -- refusing to run. "
                         "A no-key call comes back indistinguishable from a "
                         "real 'unscored' miss, and writing that would NULL "
                         "out real scores on live rows.")
    print("  ANTHROPIC_API_KEY is set")


def confirm_no_harvest_running():
    cutoff = time.time() - 600   # 10 minutes
    for p in sorted(LOG_DIR.glob("session-*-harvest.log")):
        if p.stat().st_mtime < cutoff:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "# ended" not in text:
            raise SystemExit(f"[!] {p.name} was modified <10min ago with no "
                             f"'# ended' footer -- a harvest pass looks "
                             f"mid-run. Wait and retry.")
    print("  no harvest pass appears to be mid-run")


def rescore_row(row):
    """Score one candidate with the FIXED scorer (location threaded
    through)."""
    return score_resume_fit(row["title"] or "", row.get("description") or "",
                            location=row.get("location") or "")


def apply_result(conn, row, res, dry_run):
    """Write one row's rescore. Crossing the digest floor gets the full
    triage relabel (record_triage: status='ok', track merged, scores set);
    otherwise only the fit columns move (store.update_job_scores), mirroring
    rescore_all's existing below-floor behavior. Returns (crossed, now_ok)."""
    floor = _floor_for_row(row)
    old = row.get("resume_fit_score")
    was_ok = old is not None and old >= floor
    now_ok = res.score is not None and res.score >= floor
    crossed = now_ok and not was_ok
    if not dry_run:
        if now_ok:
            store.record_triage(conn, row["job_id"], "ok", "phase0_geo_rescore",
                                tracks=store.track_set(row.get("track")),
                                scores=res.as_columns())
        else:
            store.update_job_scores(conn, row["job_id"], res.as_columns())
    return crossed, now_ok


def _fmt(v, width, prec=None):
    if v is None:
        s = "None"
    elif prec is not None:
        s = f"{v:.{prec}f}"
    else:
        s = str(v)
    return f"{s[:width]:<{width}}"


def print_table(rows_and_results):
    hdr = (f"{'title':32} {'company':20} {'location':24} {'old':>5} {'new':>5}  "
          f"old_gates -> new_gates")
    print(hdr)
    print("-" * len(hdr))
    for row, res in rows_and_results:
        print(f"{_fmt(row['title'], 32)} {_fmt(row['company_name'], 20)} "
              f"{_fmt(row['location'], 24)} "
              f"{_fmt(row['resume_fit_score'], 5, 2) if row['resume_fit_score'] is not None else 'None':>5} "
              f"{_fmt(res.score, 5, 2) if res.score is not None else 'None':>5}  "
              f"{row['fit_gates'] or '-'} -> {','.join(res.gates) or '-'}")


def run_pilot(n=25, dry_run=False, seed=0):
    confirm_backup()
    confirm_api_key()
    confirm_no_harvest_running()
    conn = store.connect(config.STORE_DB_PATH)
    try:
        candidates = select_candidates(conn)
        print(f"  {len(candidates)} candidate row(s) total (fit-status + "
              f"crawl-geo-gated, local/remote); piloting "
              f"{min(n, len(candidates))}{' (dry-run, no writes)' if dry_run else ''}")
        sample = random.Random(seed).sample(candidates, min(n, len(candidates)))
        results = []
        n_crossed = n_ok = 0
        for row in sample:
            res = rescore_row(row)
            crossed, now_ok = apply_result(conn, row, res, dry_run)
            n_crossed += int(crossed)
            n_ok += int(now_ok)
            results.append((row, res))
        print_table(results)
        print(f"\n  {n_crossed}/{len(results)} crossed the digest floor on "
              f"rescore; {n_ok}/{len(results)} now score >= floor.")
        return results, [r["job_id"] for r, _ in results]
    finally:
        conn.close()


def run_full(exclude_ids=(), dry_run=False):
    confirm_backup()
    confirm_api_key()
    confirm_no_harvest_running()
    conn = store.connect(config.STORE_DB_PATH)
    try:
        candidates = select_candidates(conn, exclude_ids=frozenset(exclude_ids))
        print(f"  {len(candidates)} remaining candidate row(s) to rescore"
              f"{' (dry-run, no writes)' if dry_run else ''}")
        rows_out = []
        n_crossed = 0
        crossed_list = []
        for row in candidates:
            res = rescore_row(row)
            crossed, now_ok = apply_result(conn, row, res, dry_run)
            n_crossed += int(crossed)
            old = row.get("resume_fit_score")
            delta = (res.score - old) if (res.score is not None and old is not None) else None
            if crossed:
                crossed_list.append((row, res))
            rows_out.append({
                "job_id": row["job_id"], "title": row["title"],
                "company_name": row["company_name"], "location": row["location"],
                "old_score": old, "new_score": res.score, "delta": delta,
                "old_gates": row["fit_gates"], "new_gates": ",".join(res.gates),
                "crossed_floor": crossed,
            })
        if not dry_run:
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                w.writeheader()
                w.writerows(rows_out)
            print(f"  wrote {len(rows_out)} row(s) to {CSV_PATH}")
        deltas_list = [r["delta"] for r in rows_out if r["delta"] is not None]
        if deltas_list:
            deltas_list.sort()
            n = len(deltas_list)
            print(f"  delta distribution: min={deltas_list[0]:+.2f} "
                  f"p25={deltas_list[n // 4]:+.2f} median={deltas_list[n // 2]:+.2f} "
                  f"p75={deltas_list[3 * n // 4]:+.2f} max={deltas_list[-1]:+.2f}")
        print(f"  {n_crossed} row(s) crossed the digest floor:")
        for row, res in crossed_list:
            print(f"    {row['job_id']:20} {(row['title'] or '')[:40]:40} "
                  f"{(row['company_name'] or '')[:20]:20} "
                  f"{row['resume_fit_score']} -> {res.score:.2f}")
        return rows_out
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pilot", type=int, default=None, metavar="N",
                   help="rescore a random sample of N candidates and stop")
    p.add_argument("--full", action="store_true",
                   help="rescore every remaining candidate (requires --go)")
    p.add_argument("--go", action="store_true",
                   help="required with --full -- confirms the pilot was reviewed")
    p.add_argument("--skip", metavar="FILE",
                   help="path to a text file of job_ids (one per line) to exclude, "
                        "e.g. ones already handled by --pilot")
    p.add_argument("--dry-run", action="store_true",
                   help="score and print/write CSV but do not touch the DB")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    console_utf8()   # titles are arbitrary text; a cp1252 console dies mid-run

    if args.pilot:
        run_pilot(n=args.pilot, dry_run=args.dry_run, seed=args.seed)
    elif args.full:
        if not args.go and not args.dry_run:
            raise SystemExit("[!] --full requires --go (confirms the pilot "
                             "was reviewed) or --dry-run.")
        exclude = set()
        if args.skip:
            exclude = {ln.strip() for ln in Path(args.skip).read_text().splitlines() if ln.strip()}
        run_full(exclude_ids=exclude, dry_run=args.dry_run)
    else:
        p.error("pass --pilot N or --full")


if __name__ == "__main__":
    main()
