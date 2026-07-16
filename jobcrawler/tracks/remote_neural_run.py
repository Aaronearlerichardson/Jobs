"""REMOTE-NEURAL track runner.

LOCATION-AGNOSTIC (the "remote" in the name is now vestigial — kept so the
branch/module name doesn't shift mid-flight; see jobcrawler/tracks/
remote_neural.py's module docstring). Surfaces neural/BCI-company roles
ANYWHERE that keep both: neural signals and a high technical bar.
Prioritizes Beacon Biosignals, Neuralink, Precision Neuroscience,
Paradromics, Synchron, and Merge Labs, then sweeps the company store
(tag: neural, own DB — see config.NEURAL_DB_PATH) + lightweight config
sources for general neural-ML roles. remote_eligible is computed and stored
per posting but no longer gates it — an onsite BCI-company posting surfaces
too. Every digest entry is tagged [REMOTE-NEURAL].

A keyword relevance pre-filter runs before any of this: each source's own
fetcher (jobcrawler/fetchers/ats_api.py, bamboohr.py, adp_wfn.py, ...)
already applies is_relevant() against the neural-ML keyword tiers this
track sets via apply_to_config(), so `jobs` arriving here is already the
keyword-relevant set, not a raw board dump — cost control for the "same
companies, anywhere" volume this track can now see. --fit additionally
gates on jobcrawler.tracks.remote_neural_run.COST_GUARD_THRESHOLD before
spending any Claude API calls; see --confirm-cost.

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

from .. import store
from ..parallel import fetch_all
from ..remote_filter import remote_signal_for, us_eligible
from . import remote_neural as track


# Aaron's hard budget guard: never send more than this many postings to the
# Claude API in one --fit run without an explicit --confirm-cost. The track
# went location-agnostic (remove the remote-only gate below) specifically to
# admit "same BCI companies, anywhere" volume, which is exactly the scenario
# this exists to catch before it becomes an API bill.
COST_GUARD_THRESHOLD = 300
# Rough per-posting cost: ~700 input tokens (system prompt + JD, cached
# system prompt after the first call) + ~120 output tokens, at Claude
# Sonnet's blended per-token rate. Order-of-magnitude only — for deciding
# whether to stop and ask, not for a real invoice.
_EST_TOKENS_PER_POSTING = 820
_EST_USD_PER_MTOK = 4.0


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
    ap.add_argument("--confirm-cost", action="store_true",
                    help="With --fit: allow scoring more than the "
                         f"{COST_GUARD_THRESHOLD}-posting safety threshold "
                         "via the Claude API")
    args = ap.parse_args(argv)

    # Point the shared keyword filter at the neural-ML focus + allow remote.
    track.apply_to_config(config)

    sources = track.build_sources(config, include_websearch=not args.no_websearch,
                                  db_path=config.NEURAL_DB_PATH)

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
    print(f"  Sources: {len(sources)}  |  location-agnostic (remote_eligible "
          f"stored, not gated)  |  keyword focus: neural-ML\n")

    # Own store, isolated from local-tech's local_tech.db (see
    # config.NEURAL_DB_PATH) — read-only for new/seen accounting unless
    # --commit. Companies are seeded from a one-time copy of the local-tech
    # roster (see the migration note in config.py); this track's own
    # discovery/--add-board work should target this DB going forward.
    conn = store.connect(config.NEURAL_DB_PATH)

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
            # Funnel: neural-anchored AND high-technical-bar. Cheap, title-
            # level checks first (each source's own fetcher already applied
            # a title/description keyword gate before this — see
            # jobcrawler/fetchers/ats_api.py etc — so `jobs` here is already
            # the keyword-relevant set; this re-applies the track's tighter
            # neural-anchor + technical-title tiers on top of it).
            nsig = track.neural_signal(job.get("title", ""),
                                       job.get("description", ""))
            if not nsig:
                continue
            neural_here += 1
            if not track.is_technical_role(job.get("title", "")):
                continue
            tech_here += 1
            # Location-agnostic (task 2b): remote eligibility is recorded,
            # not a gate — an onsite BCI-company posting surfaces here too.
            # "Philippines Remote" is remote, just not for a US applicant,
            # so that distinction still lives in the stored signal/flag.
            sig = remote_signal_for(job)
            surfaced_here += 1
            jid = job["id"]
            new = store.is_new(conn, jid)
            if new:
                new_here += 1
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            track.tag_job(job, signal=sig)
            job["neural_signal"] = nsig
            job["_new"] = new
            job["_us_eligible"] = us_eligible(job.get("location", ""))
            matches.append(job)

        total_relevant += relevant
        total_neural += neural_here
        total_tech += tech_here
        total_surfaced += surfaced_here
        total_new += new_here
        rows.append((label, relevant, neural_here, tech_here, surfaced_here,
                     new_here, "priority" if platform.endswith("*") else ""))

    # Optional cross-pollinated scorer: resume fit, ranked. Hard budget
    # guard first — the location-agnostic sweep can surface far more
    # postings than the old remote-only gate did, and each one is a Claude
    # API call.
    if args.fit and matches:
        if len(matches) > COST_GUARD_THRESHOLD and not args.confirm_cost:
            est_tokens = len(matches) * _EST_TOKENS_PER_POSTING
            est_usd = est_tokens / 1_000_000 * _EST_USD_PER_MTOK
            print(f"\n{bar}")
            print(f"  [!] BUDGET GUARD: {len(matches)} posting(s) would be scored "
                  f"via the Claude API (> {COST_GUARD_THRESHOLD}).")
            print(f"      Rough estimate: ~{est_tokens:,} tokens, ~${est_usd:.2f} "
                  f"(order-of-magnitude, not a quote).")
            print(f"      Re-run with --fit --confirm-cost to proceed. Scoring skipped this run.")
            print(f"{bar}")
        else:
            _score_fits(matches)

    if args.commit:
        for job in matches:
            store.mark_seen(conn, job, track=track.TRACK)
    conn.close()

    # ─── Per-source table + totals ────────────────────────────────────────
    # Location-agnostic (task 2b): SURF (surfaced) is everything that cleared
    # the neural+technical gates, onsite or remote alike -- would-be-scored
    # count for --fit. REMOTE is informational (remote_eligible), not a gate.
    print(f"\n{bar}")
    print("  PER-SOURCE FUNNEL  (RELV=keyword-relevant -> NEUR=neural-anchored")
    print("   -> TECH=+technical role = SURFaced (onsite+remote, would-be-scored);")
    print("   NEW=unseen vs DB)")
    print(f"{bar}")
    print(f"  {'SOURCE':<46} {'RELV':>4} {'NEUR':>4} {'TECH':>4} "
          f"{'SURF':>6} {'NEW':>4}")
    print(f"  {'-'*46} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*4}")
    for label, relevant, neural_here, tech_here, surfaced_here, new_here, note in rows:
        tail = f"  [{note}]" if note else ""
        print(f"  {label:<46} {relevant:>4} {neural_here:>4} {tech_here:>4} "
              f"{surfaced_here:>6} {new_here:>4}{tail}")
    print(f"  {'-'*46} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*4}")
    print(f"  {'TOTAL':<46} {total_relevant:>4} {total_neural:>4} "
          f"{total_tech:>4} {total_surfaced:>6} {total_new:>4}")
    n_remote = sum(1 for j in matches if j.get("remote_eligible"))
    print(f"\n  Surfaced (neural & technical, any location) this run: {len(matches)} "
          f"(unique)  |  remote-eligible: {n_remote}  |  new vs DB: {total_new}")
    print(f"  Would be sent to the Claude API with --fit: {len(matches)} posting(s)"
          + (f"  [!] over the {COST_GUARD_THRESHOLD}-posting budget guard"
             if len(matches) > COST_GUARD_THRESHOLD else ""))

    # ─── Sample matches for precision sanity-check ────────────────────────
    n = max(0, args.samples)
    print(f"\n{bar}")
    print(f"  {n} SAMPLE MATCHES (sanity-check precision before emailing)")
    print(f"{bar}")
    if not matches:
        print("  (no matches)")
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
