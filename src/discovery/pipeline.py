"""Company discovery pipeline — Claude → candidates → board resolution → report.

The SOURCING half of a `discover-term` / `--from-bciwiki` run: ask Claude (or
a directory) for employer names, hand each one to the shared resolver
(src.discovery.resolve.board), and turn what comes back into a report and a
roster write. The resolution itself used to be a second, probe-first
implementation living here; see validate_candidate for why it isn't any more.
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from src.claude.api import DISCOVER_SYSTEM, call_claude_json
from src.config import REPORT_DIR
from src.match.names import strip_suffixes
from src.net.parallel import drain
from src.net.util import worker_count
from .resolve.board import resolve_board_sniff_first
from .resolve.probes import PROBES, WorkdayJsProbePool
from .resolve.sniffer import sniff_careers_ats
from .seeds import seed_candidates_for

# Parallel worker count for validate_candidate. Each worker is almost
# entirely blocked on network I/O (slug probes + careers-page fetches
# against different hosts), so this is a network-concurrency knob, not a
# CPU one — defaults to n_cpus-1, raise DISCOVERY_WORKERS (e.g. 32) to push
# more concurrent requests. Tune down if you see 429s from a probe provider.
_DISCOVERY_WORKERS = worker_count("DISCOVERY_WORKERS")

# Headless browsers for the parallel Workday JS fallback. Each is ~200-300MB
# of RAM, so keep this modest; raise JS_BROWSERS to scrape more SPA careers
# pages at once. Capped at the worker count (no point having idle browsers).
_JS_BROWSERS = min(max(1, int(os.environ.get("JS_BROWSERS", "4"))),
                   _DISCOVERY_WORKERS)


@dataclass
class Candidate:
    name: str
    ats: str
    # In: the model's guessed handle, which nothing probes any more -- the
    # resolver derives its own from the name (match.names.slug_guesses) and
    # prefers what the company's careers page actually says. Out: the
    # RESOLVED handle, '|'-joined for Workday (see _slug_str), None for a
    # self-hosted board keyed on its URL.
    slug_guess: str | None
    careers_url: str
    notes: str
    confirmed: bool = False
    job_count: int = 0
    # Postings on the board that are in your [locality] — the resolver counts
    # them while validating, and the roster write tags the row LOCAL on it.
    nc: int = 0
    # How the board was found: "sniff" (read off the company's own careers
    # page), "probe" (a name-guessed slug), "websearch", or "js" (the
    # headless Workday scrape). Empty while unconfirmed.
    via: str = ""
    tried_slugs: list[str] = field(default_factory=list)
    # Set when the candidate is unconfirmed but its careers page links to a
    # known-but-not-auto-fetchable ATS (e.g. "eightfold @ acme.eightfold.ai").
    ats_lead: str = ""


def candidate_from_dict(d):
    return Candidate(
        name        = d.get("name", "").strip(),
        ats         = (d.get("ats") or "unknown").lower(),
        slug_guess  = (d.get("slug_guess") or None),
        careers_url = d.get("careers_url", "").strip(),
        notes       = d.get("notes", "").strip(),
    )


def _slug_str(ats, slug):
    """A resolver hit's handle as the single string `Candidate.slug_guess`,
    the report and `apply_to_store` all carry: Workday's (tenant, pod, site)
    triple joined with '|', the plain slug for everything else, None for a
    self-hosted board whose only coordinate is its URL.

    >>> _slug_str("workday", ("acme", 5, "External"))
    'acme|5|External'
    >>> _slug_str("greenhouse", "acmebio")
    'acmebio'
    >>> _slug_str("custom", None) is None
    True
    """
    if ats == "workday" and isinstance(slug, (tuple, list)) and len(slug) == 3:
        return "|".join(str(p) for p in slug)
    return slug or None


#: Why a confirmed board still deserves a human glance, keyed by HOW it was
#: found. A sniff read the coordinates off the company's OWN careers page,
#: which is the one provenance that cannot collide with a same-named
#: stranger; the other three guessed a handle and then validated it, which is
#: weaker evidence of WHOSE board it is.
_VIA_NOTES = {
    "sniff":     "sniffed from the careers page",
    "probe":     "name-guessed slug, not read off the company's own site "
                 "- confirm identity",
    "websearch": "found by web search, not on the company's own site",
    "js":        "headless Workday scrape - confirm identity",
}


def _flag_for_verification(c, claimed_ats):
    """Tag a confirmed hit whose identity deserves a human look: the ATS
    disagreeing with Claude's guess, and how the board was found at all
    (see _VIA_NOTES)."""
    flags = []
    if claimed_ats in PROBES and c.ats != claimed_ats:
        flags.append(f"found on {c.ats}, not Claude's guess ({claimed_ats})")
    if _VIA_NOTES.get(c.via):
        flags.append(_VIA_NOTES[c.via])
    if flags:
        note = "[VERIFY: " + "; ".join(flags) + "]"
        c.notes = f"{c.notes} {note}".strip() if c.notes else note


def verify_note(c) -> str:
    """Extract the VERIFY text from a candidate's notes, or ''."""
    m = re.search(r"\[VERIFY: ([^\]]+)\]", c.notes or "")
    return m.group(1) if m else ""


def validate_candidate(c, delay=0.3, js_probe=None, log=print, websearch=True):
    """
    Resolve one candidate to a crawlable board and record what happened.

    The resolution is resolve.board.resolve_board_sniff_first: the company's
    OWN careers page first, a name-guessed slug probe second, a web search
    third, every hit validated by a live fetch. Only the bookkeeping the
    discovery REPORT needs stays here -- the job counts, a provenance line in
    `tried_slugs`, and the [VERIFY] flag.

    Notes:
        This was a SECOND resolver, written out longhand and probe-FIRST: up
        to eight slug variants of the name tried against every ATS before
        anything looked at the company's own site, with an LLM identity check
        bolted on for the collision-shaped slugs that kept the wrong board.
        That check is advisory by design (a None verdict keeps the hit), so
        with no API key -- the offline default -- the order alone decided,
        and the order is what mapped "Ripple Neuro" onto the payments
        company's 127-job board (2026-08-28) and a stranger's Paylocity board
        onto two roster rows (2026-09-18 Direct Supply, 2026-09-21 Bright
        Vision). resolve.board was written to fix exactly this and its
        docstring says so; there is no reason for two answers to one
        question.

        Two things the shared resolver does not do are kept here as
        fallbacks: a detection-only LEAD (an ATS we can recognize but not
        fetch -- Eightfold, Dayforce, iCIMS), worth reporting so the user can
        add the board by hand; and the headless-browser Workday scrape, for
        SPA careers pages whose myworkdayjobs link only exists once JS has
        run.

        `websearch=False` skips the resolver's search step for a bulk sweep
        (see resolve_board_sniff_first).
    """
    claimed_ats = c.ats
    hit = resolve_board_sniff_first(c.name, c.careers_url, websearch=websearch)
    time.sleep(delay)
    if hit:
        c.confirmed   = True
        c.ats         = hit["ats"]
        c.slug_guess  = _slug_str(hit["ats"], hit.get("slug"))
        c.job_count   = hit.get("count") or 0
        c.nc          = hit.get("nc") or 0
        c.via         = hit.get("via") or ""
        c.careers_url = hit.get("careers_url") or c.careers_url
        c.tried_slugs.append(
            f"[{c.via}:{c.ats} {c.slug_guess or c.careers_url or '?'}]")
        _flag_for_verification(c, claimed_ats)
        return c
    c.tried_slugs.append(
        "[no board: careers sniff, slug probe"
        + (" and web search" if websearch else "") + " all missed]")

    # Detection-only lead: a real but not-auto-fetchable ATS
    # (Eightfold/Dayforce/iCIMS/...). Record it so the unconfirmed report row
    # points the user straight at the board to add by hand, rather than
    # reading as a dead miss. Cheap here -- the careers pages this re-reads
    # are already in the resolver's per-run memo (resolve.fetchpool).
    sniff = sniff_careers_ats(c.name, c.careers_url)
    time.sleep(delay)
    if sniff:
        c.ats_lead = f"{sniff['ats']} @ {sniff['slug']}"
        c.tried_slugs.append(f"[lead:{sniff['ats']} <- {sniff['source_url']}]")

    # Workday fallback for SPA careers pages: the myworkdayjobs link is only
    # inserted into the DOM after JS runs, so nothing the resolver fetches can
    # see it. Skipped when a lead already identified the platform.
    if js_probe is not None and c.ats in ("unknown", "workday") and not c.ats_lead:
        # Noisy hint to the user — browser launches are slow, and they'll
        # otherwise wonder why discover() is suddenly pausing.
        marker = "[js]" if js_probe.launched else "[js init]"
        meta = js_probe.probe(c.name, c.careers_url)
        log(f"    {marker} {c.name}: headless scrape... "
            f"{'hit' if meta else 'miss'}")
        time.sleep(delay)
        if meta:
            c.confirmed  = True
            c.ats        = "workday"
            c.slug_guess = f"{meta['tenant']}|{meta['wd_pod']}|{meta['site']}"
            c.job_count  = meta["count"]
            c.via        = "js"
            c.tried_slugs.append(
                f"[workday:{meta['tenant']}.wd{meta['wd_pod']}/{meta['site']}"
                + ("" if meta["validated"] else " ~unvalidated")
                + "]"
            )
            _flag_for_verification(c, claimed_ats)
    return c


def _merge_seeds(claude_raw: list[dict], seeds: list[dict]) -> list[dict]:
    """
    Append seed candidates to Claude's output, deduping by normalized
    name. Claude's entry wins when both sources have the same company
    (its ats/slug_guess may be more accurate than the seed's 'unknown').
    """
    seen = {strip_suffixes(c.get("name") or "").lower() for c in claude_raw}
    return list(claude_raw) + [
        s for s in seeds
        if strip_suffixes(s["name"]).lower() not in seen
    ]


def discover(term):
    print(f"  > Asking Claude for companies in: {term!r}")
    payload = call_claude_json(DISCOVER_SYSTEM, term, max_tokens=2000)
    raw_companies = (payload or {}).get("companies", [])
    seeds = seed_candidates_for(term)

    if not payload and not seeds:
        return {"term": term, "companies": [], "gated_sites": []}

    merged = _merge_seeds(raw_companies, seeds)
    added = len(merged) - len(raw_companies)

    if not payload:
        print(f"  > Claude returned no response; probing {added} seed(s)")
    elif added:
        print(f"  > Claude returned {len(raw_companies)} suggestion(s) "
              f"+ {added} seed(s) merged")
    else:
        print(f"  > Claude returned {len(raw_companies)} company suggestion(s)")

    validated = _validate_all(merged)
    return {
        "term":        term,
        "companies":   validated,
        "gated_sites": (payload or {}).get("gated_sites", []),
    }


def _validate_all(candidate_dicts, use_js=True, websearch=True):
    """Validate candidate dicts in parallel; return Candidate objects in
    input order. Shared by Claude-driven discover() and name-list-driven
    discover_companies().

    use_js gates the headless-browser Workday fallback. A single browser is
    single-threaded (Playwright greenlet affinity), so the fallback runs as
    a POOL of _JS_BROWSERS browsers — candidates that need it borrow a free
    one and only block when all are busy, instead of all queuing on one.

    websearch gates the resolver's third step for the same reason the JS
    fallback is gated: it is the slowest thing a miss can pay for, and a
    bulk sweep pays it once per boardless name.
    """
    # Each worker drops log lines into its own list and flushes them
    # as a single atomic block when the candidate finishes — so the
    # [N/total] progress line + any "[js] headless scrape..." messages
    # for one candidate always appear contiguously, even with 8
    # workers logging concurrently.
    total     = len(candidate_dicts)
    validated = [None] * total
    out_lock  = threading.Lock()
    done      = [0]                           # list-as-box for closure mutation

    def _worker(idx, rc, js_probe):
        cand = candidate_from_dict(rc)
        buf: list[str] = []
        # Small inter-step delay: each resolution step hits a different host,
        # and workers already run concurrently, so politeness sleeps add up to
        # dead time per candidate. 0.05 keeps a light touch without the tax.
        validate_candidate(
            cand, delay=0.05, js_probe=js_probe, log=buf.append,
            websearch=websearch,
        )
        # Flush under lock so concurrent candidates never interleave.
        with out_lock:
            done[0] += 1
            if cand.confirmed:
                status, detail = "OK  ", f"  slug={cand.slug_guess!r}  ({cand.job_count} jobs)"
            elif cand.ats_lead:
                status, detail = "lead", f"  {cand.ats_lead}"
            else:
                status, detail = "miss", ""
            print(f"  [{done[0]:>3}/{total}] {status}  {cand.name} "
                  f"({cand.ats}){detail}")
            for line in buf:
                print(line)
        return idx, cand

    # A pool of browsers for the JS scrapes, each lazy-launched on first
    # use, so concurrent candidates scrape in parallel (up to _JS_BROWSERS)
    # instead of serializing on one. Skipped entirely when use_js is off,
    # so bulk sweeps never pay the browser cost.
    def _done(fut, _name):
        idx, cand = fut.result()
        validated[idx] = cand

    js_probe = WorkdayJsProbePool(_JS_BROWSERS) if use_js else None
    try:
        drain(list(enumerate(candidate_dicts)),
              lambda irc: _worker(irc[0], irc[1], js_probe),
              _done, lambda _name: None,
              label=lambda irc: (irc[1].get("name") or "").strip(),
              max_workers=_DISCOVERY_WORKERS)
    finally:
        if js_probe is not None:
            js_probe.close()
    # A candidate the watchdog abandoned is reported unconfirmed, not
    # dropped: the report and --apply walk every slot.
    for i, rc in enumerate(candidate_dicts):
        if validated[i] is None:
            cand = candidate_from_dict(rc)
            cand.tried_slugs.append("[stalled: abandoned by the watchdog]")
            validated[i] = cand
    return validated


def discover_companies(candidate_dicts, term, use_js=False):
    """Resolve an explicit list of candidate dicts (e.g. harvested from the
    BCIWiki directory) to crawlable boards — no Claude call. Returns the
    same result shape as discover().

    use_js defaults False: bulk directory sweeps are dominated by the
    single-threaded browser fallback, and few entries are Workday SPAs.
    Pass use_js=True for a smaller, thorough pass. It also picks the
    resolver's web-search step, for the same reason — a directory sweep of
    several hundred names would spend most of its wall clock inside the
    search backend's rate-limit backoff (see resolve_board_sniff_first)."""
    print(f"  > Resolving {len(candidate_dicts)} candidate(s) for {term!r} "
          f"(workers={_DISCOVERY_WORKERS}, js={'on' if use_js else 'off'})")
    if not candidate_dicts:
        return {"term": term, "companies": [], "gated_sites": []}
    validated = _validate_all(candidate_dicts, use_js=use_js,
                              websearch=use_js)
    return {"term": term, "companies": validated, "gated_sites": []}


# ─── Report ──────────────────────────────────────────────────────────────

def write_discovery_report(result):
    REPORT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = "".join(c if c.isalnum() else "_" for c in result["term"].lower())[:40]
    path = REPORT_DIR / f"discover_{date_str}_{slug}.md"

    companies = result["companies"]
    confirmed = [c for c in companies if c.confirmed]
    unconfirmed = [c for c in companies if not c.confirmed]

    by_ats = {}
    for c in confirmed:
        by_ats.setdefault(c.ats, []).append(c)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Company Discovery - {result['term']}\n\n")
        f.write(f"_Generated {date_str}_\n\n")
        f.write(f"**{len(confirmed)}** confirmed / {len(companies)} suggested\n\n")

        if confirmed:
            f.write("## Confirmed - written to the company store with `--apply`\n\n")
            f.write("| Company | ATS | Slug / coordinates | Jobs live | Verify |\n")
            f.write("|---|---|---|---:|---|\n")
            for ats_name, cands in by_ats.items():
                for c in cands:
                    # A self-hosted board has no handle; its URL IS its
                    # coordinate (src.ats.coords).
                    f.write(f"| {c.name} | {ats_name} "
                            f"| `{c.slug_guess or c.careers_url or '-'}` "
                            f"| {c.job_count} | {verify_note(c)} |\n")
            f.write("\n")

        if unconfirmed:
            # Surface companies whose careers page links to a known but
            # not-auto-fetchable ATS (bot-protected/JS-only) at the top —
            # these are actionable: add the platform manually.
            leads = [c for c in unconfirmed if c.ats_lead]
            if leads:
                f.write("### Detected ATS (manual add - not auto-fetchable)\n\n")
                f.write("| Company | Platform | Found at |\n|---|---|---|\n")
                for c in leads:
                    plat, _, host = c.ats_lead.partition(" @ ")
                    f.write(f"| {c.name} | {plat} | `{host}` |\n")
                f.write("\n")

            f.write("## Unconfirmed - manual investigation needed\n\n")
            f.write("| Company | ATS guess | ATS lead | Resolution steps | Careers URL | Notes |\n")
            f.write("|---|---|---|---|---|---|\n")
            for c in unconfirmed:
                tried = ", ".join(f"`{s}`" for s in c.tried_slugs) or "-"
                f.write(f"| {c.name} | {c.ats} | {c.ats_lead or '-'} | {tried} | "
                        f"{c.careers_url or '-'} | {c.notes} |\n")
            f.write("\n")

        gated = result.get("gated_sites", [])
        if gated:
            f.write("## Gated sites (require auth)\n\n")
            f.write("Login-only boards Claude thinks are worth searching. "
                    "Browse them logged-in and capture result pages with "
                    "`python capture.py` (see README.md).\n\n")
            f.write("| Site | Suggested query | Notes |\n|---|---|---|\n")
            for g in gated:
                f.write(f"| {g.get('site','?')} | `{g.get('query','')}` | "
                        f"{g.get('notes','')} |\n")

    print(f"\n  Report -> {path}\n")
    return path


def print_summary(result):
    companies = result["companies"]
    confirmed = [c for c in companies if c.confirmed]
    w = 62
    print(f"\n{'='*w}")
    print(f"  Discovery: '{result['term']}'")
    print(f"{'='*w}")
    print(f"  Confirmed: {len(confirmed)} / Suggested: {len(companies)}\n")
    for c in confirmed:
        note = verify_note(c)
        tail = f"  [VERIFY: {note}]" if note else ""
        print(f"    + {c.name:<30} {c.ats:<10} "
              f"slug='{c.slug_guess or c.careers_url or '-'}'  "
              f"({c.job_count} jobs){tail}")
    unconfirmed = [c for c in companies if not c.confirmed]
    if unconfirmed:
        leads = [c for c in unconfirmed if c.ats_lead]
        if leads:
            print("\n  Detected ATS (manual add, not auto-fetchable):")
            for c in leads:
                print(f"    > {c.name:<30} {c.ats_lead}")
        print(f"\n  Unconfirmed ({len(unconfirmed)}):")
        for c in unconfirmed:
            lead = f"  lead={c.ats_lead}" if c.ats_lead else ""
            print(f"    ? {c.name:<30} {c.ats:<10} tried={c.tried_slugs}{lead}")
    print(f"{'='*w}\n")
