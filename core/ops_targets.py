"""Composite operation targets for core/ops_registry.py.

Each function here is the glue that used to be spelled out inline in one
front end (open the track's store, call a maintenance function, close and
report) and re-spelled slightly differently in another. Anything that is
a single existing function is targeted directly by the registry; only
multi-step operations live here. Imports are lazy so that importing the
registry stays cheap.
"""


def _connect(t):
    from core import store
    return store.connect(t["db_path"] if t else None)


def dedup(t=None):
    """Merge duplicate company rows pointing at one board, then duplicate
    job rows. Returns (companies merged, jobs dropped)."""
    from core import store
    conn = _connect(t)
    try:
        n = store.dedup_companies(conn)
        n_jobs = store.dedup_jobs(conn)
    finally:
        conn.close()
    print(f"\n  merged {n} duplicate company row(s) into their canonical board; "
          f"dropped {n_jobs} duplicate job row(s).")
    return n, n_jobs


def prune(offmission=False, t=None):
    """Deactivate companies whose ATS board is dead, and optionally the
    off-mission ones. Returns (dead deactivated, off-mission deactivated)."""
    from scrapers.ops import prune_dead_boards
    conn = _connect(t)
    try:
        n_dead, n_off = prune_dead_boards(conn, deactivate_offmission=bool(offmission))
    finally:
        conn.close()
    print(f"\n  deactivated {n_dead} dead-board compan(ies)"
          + (f" + {n_off} off-mission" if offmission else "") + ".")
    return n_dead, n_off


def backfill_axes(t=None):
    """Populate the per-axis fit columns from fit_reason (offline)."""
    from core import store
    conn = _connect(t)
    try:
        return store.backfill_axis_columns(conn)
    finally:
        conn.close()


def ingest_nlx(companies, t=None):
    """Pull postings for bot-gated employers from the federal NLx feed and
    run them through the standard ingest. `companies` is a list of
    employer names. Returns the number of new jobs ingested."""
    from scrapers.fetchers.careeronestop import fetch_nlx_company
    from scrapers.ops import ingest_external_jobs
    if not companies:
        print("  [!] give a comma-separated list of employer names")
        return 0
    total = 0
    for name in companies:
        jobs = fetch_nlx_company(name)
        print(f"  {name}: {len(jobs)} NLx posting(s)")
        if jobs:
            total += ingest_external_jobs(jobs, source="nlx", t=t)
    print(f"\n  {total} new job(s) ingested from the NLx feed.")
    return total


def dork_sweep():
    """ATS dorking via DuckDuckGo: mine search-indexed board URLs for
    companies in your locality into the store. Returns (added, checked)."""
    from discovery.ats_dork import run_ddgs_dorks
    added, checked = run_ddgs_dorks()
    print(f"\n  {added} new local board(s) added to the store "
          f"({checked} extracted from dork results)")
    return added, checked


def discover_term(term, no_report=False, dry_run=False):
    """Free-text sector discovery: ask Claude for likely employers matching
    `term`, probe each against the ATS registry, and (apply-by-default)
    queue the confirmed ones unless `dry_run`. Returns the discovery
    result, or None when no term was given."""
    from discovery import apply_to_store, discover, print_summary, write_discovery_report
    term = (term or "").strip()
    if not term:
        print("  [!] give a sector/term to search for, e.g. 'medical device companies'")
        return None
    result = discover(term)
    print_summary(result)
    if not no_report:
        write_discovery_report(result)
    for line in apply_to_store(result, dry_run=bool(dry_run)):
        print(line)
    return result
