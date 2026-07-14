"""REMOTE-NEURAL track runner.

Surfaces REMOTE-eligible roles that keep all three of: neural signals, a
high technical bar, and a clinical/health mission. Prioritizes Beacon
Biosignals, Precision Neuroscience, and Paradromics, then sweeps the
company store (tag: neural) + lightweight config sources for general
neural-ML roles. Only postings that pass remote-eligibility detection are
surfaced, and every digest entry is tagged [REMOTE-NEURAL].

Run it via the single entry point:
"""

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

# Windows consoles default to cp1252; job blurbs carry em-dashes, curly
# quotes, and the odd emoji. Print defensively rather than crash mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ..db import init_db, is_new, mark_seen
from ..parallel import fetch_all
from ..remote_filter import remote_signal_for, us_eligible
from . import remote_neural as track


def _short(text, n):
    # Some feeds (e.g. Greenhouse) hand back entity-encoded HTML that survives
    # as literal "<p>" text; strip tags for a readable blurb.
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "..."


def _diversify(matches, n):
    """Pick up to n samples spread across companies (round-robin) so the
    precision sanity-check isn't dominated by one prolific employer."""
    by_company = {}
    for j in matches:
        by_company.setdefault(j["company"], []).append(j)
    picked = []
    while len(picked) < n and any(by_company.values()):
        for comp in list(by_company):
            if by_company[comp]:
                picked.append(by_company[comp].pop(0))
                if len(picked) >= n:
                    break
    return picked


def _score_fits(matches, max_workers=6):
    """Resume-fit-score matches in parallel (local-tech's scorer, borrowed).
    No-ops with a warning when the resume or API key is missing."""
    from ..claude import score_resume_fit
    from ..resume import resume_text

    resume = resume_text()
    if not resume:
        print("  [!] --fit: no resume text (set config.RESUME_PATH) — skipping.")
        return
    print(f"  scoring {len(matches)} match(es) against resume...")

    def _one(j):
        res = score_resume_fit(resume, j["title"], j.get("description", ""))
        j.update(res.as_columns())

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_one, matches))
    matches.sort(key=lambda j: (j.get("resume_fit_score") is not None,
                                j.get("resume_fit_score") or 0.0), reverse=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remote-Neural job track")
    ap.add_argument("--commit", action="store_true",
                    help="Persist remote-eligible matches to the unified store")
    ap.add_argument("--send", action="store_true",
                    help="Email the [REMOTE-NEURAL]-tagged digest")
    ap.add_argument("--fit", action="store_true",
                    help="Resume-fit-score matches and rank the digest by fit")
    ap.add_argument("--no-websearch", action="store_true",
                    help="Skip the DuckDuckGo web-search sources")
    ap.add_argument("--samples", type=int, default=5,
                    help="Number of sample matches to print (default 5)")
    args = ap.parse_args(argv)

    # Point the shared keyword filter at the neural-ML focus + allow remote.
    track.apply_to_config(config)

    sources = track.build_sources(config, include_websearch=not args.no_websearch)

    bar = "=" * 70
    print(f"\n{bar}")
    print(f"  {track.TAG} Remote-Neural Crawler  -  "
          f"{time.strftime('%Y-%m-%d %H:%M')}")
    print(f"{bar}")
    mode = "PREVIEW (no DB writes, no email)"
    if args.commit:
        mode = "COMMIT (DB writes)" + (" + EMAIL" if args.send else "")
    elif args.send:
        mode = "EMAIL (no DB writes)"
    print(f"  Mode: {mode}")
    print(f"  Sources: {len(sources)}  |  remote filter: ON  |  "
          f"keyword focus: neural-ML\n")

    # Read-only DB connection for new/seen accounting; writes only on --commit.
    conn = init_db()

    # ─── Phase 1: fetch every source in parallel ──────────────────────────
    done_count = [0]

    def _progress(name, platform, jobs, err):
        done_count[0] += 1
        status = f"fetch error: {err}" if err else f"{len(jobs)} relevant"
        print(f"  [{done_count[0]:>2}/{len(sources)}] {name} ({platform}): "
              f"{status}")

    fetched = fetch_all(sources, on_done=_progress)

    # ─── Phase 2: gate + dedupe + persist, in source order ───────────────
    # Processing follows the configured source order (priority companies
    # first) so cross-source duplicates resolve deterministically no
    # matter which fetch finished first.
    matches = []                 # surfaced jobs, in source order
    seen_ids = set()             # de-dupe within this run
    rows = []                    # per-source summary rows
    total_relevant = total_neural = total_tech = total_surfaced = total_new = 0

    for (name, platform, _), (jobs, err) in zip(sources, fetched):
        label = f"{name} ({platform})"
        if err is not None:
            rows.append((label, 0, 0, 0, 0, 0, "ERR"))
            continue

        relevant = len(jobs)
        neural_here = tech_here = surfaced_here = new_here = 0
        for job in jobs:
            # Funnel: neural-anchored AND high-technical-bar AND remote.
            nsig = track.neural_signal(job.get("title", ""),
                                       job.get("description", ""))
            if not nsig:
                continue
            neural_here += 1
            if not track.is_technical_role(job.get("title", "")):
                continue
            tech_here += 1
            # Remote AND eligible for a US applicant ("Philippines Remote"
            # is remote, just not for you).
            sig = remote_signal_for(job)
            if not sig or not us_eligible(job.get("location", "")):
                continue
            surfaced_here += 1
            jid = job["id"]
            new = is_new(conn, jid)
            if new:
                new_here += 1
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            track.tag_job(job, signal=sig)
            job["neural_signal"] = nsig
            job["_new"] = new
            matches.append(job)

        total_relevant += relevant
        total_neural += neural_here
        total_tech += tech_here
        total_surfaced += surfaced_here
        total_new += new_here
        rows.append((label, relevant, neural_here, tech_here, surfaced_here,
                     new_here, "priority" if platform.endswith("*") else ""))

    # Optional cross-pollinated scorer: resume fit, ranked.
    if args.fit and matches:
        _score_fits(matches)

    if args.commit:
        for job in matches:
            mark_seen(conn, job, track=track.TRACK)
    conn.close()

    # ─── Per-source table + totals ────────────────────────────────────────
    print(f"\n{bar}")
    print("  PER-SOURCE FUNNEL  (RELV=keyword-relevant -> NEUR=neural-anchored")
    print("   -> TECH=+technical role -> REMOTE=+remote-eligible = surfaced;")
    print("   NEW=unseen vs DB)")
    print(f"{bar}")
    print(f"  {'SOURCE':<46} {'RELV':>4} {'NEUR':>4} {'TECH':>4} "
          f"{'REMOTE':>6} {'NEW':>4}")
    print(f"  {'-'*46} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*4}")
    for label, relevant, neural_here, tech_here, surfaced_here, new_here, note in rows:
        tail = f"  [{note}]" if note else ""
        print(f"  {label:<46} {relevant:>4} {neural_here:>4} {tech_here:>4} "
              f"{surfaced_here:>6} {new_here:>4}{tail}")
    print(f"  {'-'*46} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*4}")
    print(f"  {'TOTAL':<46} {total_relevant:>4} {total_neural:>4} "
          f"{total_tech:>4} {total_surfaced:>6} {total_new:>4}")
    print(f"\n  Surfaced (neural & technical & remote) this run: {len(matches)} "
          f"(unique)  |  new vs DB: {total_new}")

    # ─── Sample matches for precision sanity-check ────────────────────────
    n = max(0, args.samples)
    print(f"\n{bar}")
    print(f"  {n} SAMPLE MATCHES (sanity-check precision before emailing)")
    print(f"{bar}")
    if not matches:
        print("  (no remote-eligible matches)")
    for i, j in enumerate(_diversify(matches, n), 1):
        print(f"\n  {i}. {track.TAG} {j['title']}")
        print(f"     company : {j['company']}")
        print(f"     location: {j['location']}")
        print(f"     neural  : {j.get('neural_signal','')}")
        if j.get("resume_fit_score") is not None:
            print(f"     fit     : {j['resume_fit_score']:.2f}  ({j.get('fit_reason','')})")
        print(f"     remote  : {j.get('remote_signal','')}"
              f"{'   (NEW)' if j.get('_new') else '   (seen)'}")
        print(f"     url     : {j['url']}")
        if j.get("description"):
            print(f"     blurb   : {_short(j['description'], 160)}")

    # ─── Digest file + optional email ─────────────────────────────────────
    digest_path = track.write_digest(matches, config.REPORT_DIR)
    print(f"\n  Digest -> {digest_path}")

    if args.send:
        track.send_digest(matches, config)
    else:
        print("  (email suppressed — rerun with --send to email this digest)")
    print()


if __name__ == "__main__":
    main()
