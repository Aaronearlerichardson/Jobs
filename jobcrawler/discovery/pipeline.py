"""Company discovery pipeline — Claude → candidates → ATS probe → report."""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import os

from config import REPORT_DIR
from ..claude import DISCOVER_SYSTEM, call_claude_json
from ..util import worker_count
from .probes import (
    PROBES,
    WorkdayJsProbePool,
    probe_workday,
)
from .sniffer import sniff_careers_ats
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
    slug_guess: str | None
    careers_url: str
    notes: str
    confirmed: bool = False
    job_count: int = 0
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


# Corporate suffixes to strip before generating slug variants. Stored
# lowercased; match is word-boundary so "biosciences" doesn't eat "bio".
_CORP_SUFFIXES = (
    "incorporated", "technologies", "biosciences", "pharmaceuticals",
    "therapeutics", "corporation", "systems", "holdings", "sciences",
    "pharma", "health", "bio", "labs", "group", "inc", "corp", "ltd",
    "llc", "co", "company",
)
_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in _CORP_SUFFIXES) + r")\b\.?",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def _normalize_name(name: str) -> str:
    """Strip parentheticals and trailing corporate suffixes."""
    s = _PAREN_RE.sub("", name or "")
    s = _SUFFIX_RE.sub("", s)
    # Clean up punctuation left behind by suffix stripping.
    s = re.sub(r"[,\.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _variants_for(name: str) -> list[str]:
    """Generate plausible ATS slug strings for a single cleaned name."""
    base = (name or "").lower().strip()
    if not base:
        return []
    # Strip slug-hostile punctuation upfront — ATS slugs are [a-z0-9-].
    cleaned = re.sub(r"[^a-z0-9\s-]", "", base)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []
    words = cleaned.split()
    out = [
        cleaned.replace(" ", "-"),
        cleaned.replace(" ", ""),
        words[0],
    ]
    # Hyphenated first two words ("united-therapeutics") for short heads.
    if len(words) >= 2:
        out.append(f"{words[0]}-{words[1]}")
        out.append(f"{words[0]}{words[1]}")
    return out


def slug_variants(name, first_guess):
    """
    Produce a deduped list of slug candidates from a company name.

    Handles:
      * parenthetical annotations ("Corcept Therapeutics (NC office)")
      * slash-aliased names ("Cree / Wolfspeed")
      * corporate suffixes (Inc, Corp, Ltd, LLC, Therapeutics, Biosciences)
      * first-word truncation
    """
    variants: list[str] = []
    if first_guess:
        variants.append(first_guess.lower().strip())

    raw = (name or "").strip()
    # Slash-aliases: try each half as an independent name.
    halves = [h.strip() for h in re.split(r"\s*/\s*", raw) if h.strip()] or [raw]

    for half in halves:
        # Full form first (keeps Inc/Corp suffix in play if ATS expects it).
        cleaned_full = _PAREN_RE.sub("", half).strip()
        variants.extend(_variants_for(cleaned_full))
        # Then the suffix-stripped form.
        cleaned_short = _normalize_name(half)
        if cleaned_short and cleaned_short.lower() != cleaned_full.lower():
            variants.extend(_variants_for(cleaned_short))

    seen, out = set(), []
    for v in variants:
        v = (v or "").strip("- ").lower()
        # Drop 1-2 char junk slugs ("g" from "G.tec ...") — they match
        # unrelated boards and never reach the intended employer.
        if v and len(v) >= 3 and v not in seen:
            seen.add(v)
            out.append(v)
    return out[:8]


# Generic single-word slugs that collide with unrelated boards when
# probed from a multi-word company name ("Bio-Signal Technologies" -> the
# bare slug "signal" hits some unrelated Lever board). Flagged for review.
_GENERIC_SLUGS = {
    "signal", "neuro", "neural", "brain", "medical", "health", "data",
    "bio", "tech", "labs", "lab", "systems", "smart", "micro", "nano",
    "bci", "ai", "research", "digital", "care", "vision", "sense",
}


def _flag_for_verification(c, claimed_ats, slug):
    """Tag a confirmed hit whose identity deserves a human look.

    Slug probes confirm "a board with this slug exists", not "this is the
    company Claude meant" — "seer" (proteomics) confirms for Seer Medical
    (epilepsy), "nuro" (autonomous vehicles) for a BCI division. Flag the
    two collision-prone patterns so reports/--apply carry a VERIFY note.
    """
    flags = []
    if claimed_ats in PROBES and c.ats != claimed_ats:
        flags.append(f"found on {c.ats}, not Claude's guess ({claimed_ats})")
    name_words = re.findall(r"[a-z0-9]+", c.name.lower())
    single_token = "-" not in slug
    if len(name_words) >= 2 and slug == name_words[0]:
        flags.append("first-word slug - confirm it's the same company")
    elif len(name_words) >= 2 and single_token and slug in _GENERIC_SLUGS:
        # A generic word fragment ("signal", "neuro") that matched some
        # unrelated board — high collision risk from a multi-word name.
        flags.append(f"generic slug '{slug}' - likely a different company")
    elif len(name_words) == 1 and single_token and (slug in _GENERIC_SLUGS
                                                    or len(slug) <= 5):
        # A one-word company name ("Inter", "Spark", "TCT") slugifies to a
        # short common token that collides with a large unrelated board
        # ("inter" -> 158 jobs at a fintech). These slip past the checks
        # above, which require a multi-word name.
        flags.append(f"single-word name slug '{slug}' - confirm identity")
    if flags:
        note = "[VERIFY: " + "; ".join(flags) + "]"
        c.notes = f"{c.notes} {note}".strip() if c.notes else note


def verify_note(c) -> str:
    """Extract the VERIFY text from a candidate's notes, or ''."""
    m = re.search(r"\[VERIFY: ([^\]]+)\]", c.notes or "")
    return m.group(1) if m else ""


def validate_candidate(c, delay=0.3, js_probe=None, log=print):
    """
    Try each slug variant against the claimed ATS. If `c.ats` isn't one
    we know how to probe (e.g. "unknown", "workday"), sweep ALL probes —
    many Claude responses mislabel the ATS or leave it blank. On a hit,
    update c.ats so downstream apply_to_config() routes it correctly.

    Workday gets two fallbacks when all slug probes miss:
      1. static probe_workday — requests.get on candidate careers URLs
    """
    variants = slug_variants(c.name, c.slug_guess)
    claimed_ats = c.ats
    probe = PROBES.get(c.ats)

    if probe:
        # Probe the guessed ATS FIRST (fast path, preserves Claude's
        # signal), then fall back to the other three. Claude's ATS guess
        # is frequently wrong — it tags Neuralink "lever" when it's on
        # greenhouse, Precision Neuroscience "greenhouse" when it's on
        # kula — so a miss on the guessed ATS must not end the search,
        # or real boards get reported as misses.
        candidates = [(c.ats, probe)] + [
            (name, fn) for name, fn in PROBES.items() if name != c.ats
        ]
    else:
        # Unknown/unsupported ATS — try all known probes.
        candidates = list(PROBES.items())

    for slug in variants:
        c.tried_slugs.append(slug)
        for ats_name, probe_fn in candidates:
            ok, count = probe_fn(slug)
            time.sleep(delay)
            if ok:
                c.confirmed  = True
                c.slug_guess = slug
                c.job_count  = count
                c.ats        = ats_name
                _flag_for_verification(c, claimed_ats, slug)
                return c

    # Careers-page ATS sniff. Boards on platforms keyed by an opaque
    # subdomain/GUID (ADP, JazzHR, BambooHR) can't be reached by guessing
    # a slug — but the company's careers page links straight to them.
    # Read the coordinates out of that link. This is cheaper than the
    # Workday browser fallback, so try it first.
    sniff = sniff_careers_ats(c.name, c.careers_url)
    time.sleep(delay)
    if sniff and sniff.get("confirmed"):
        c.confirmed  = True
        c.ats        = sniff["ats"]
        c.slug_guess = sniff["slug"]
        c.job_count  = sniff["count"]
        c.tried_slugs.append(f"[sniff:{sniff['ats']} <- {sniff['source_url']}]")
        note = f"sniffed from careers page ({sniff['ats']})"
        c.notes = f"{c.notes} [VERIFY: {note}]".strip() if c.notes else f"[VERIFY: {note}]"
        return c
    if sniff:
        # Detection-only lead: a real but not-auto-fetchable ATS
        # (Eightfold/Dayforce/iCIMS/...). Record it so the unconfirmed
        # report row points the user straight at the board to add by hand,
        # rather than reading as a dead miss.
        c.ats_lead = f"{sniff['ats']} @ {sniff['slug']}"
        c.tried_slugs.append(f"[lead:{sniff['ats']} <- {sniff['source_url']}]")

    # Workday fallback (page scrape for tenant/pod/site — no slug probe
    # exists). Only for ats in (unknown, workday), and skipped when a
    # non-workday lead already identified the platform.
    if c.ats in ("unknown", "workday") and not c.ats_lead:
        meta = probe_workday(c.name, c.careers_url)
        time.sleep(delay)
        # Static scrape missed? Try the JS-rendered version for SPAs.
        # Noisy hint to the user — browser launches are slow, and
        # they'll otherwise wonder why discover() is suddenly pausing.
        if not meta and js_probe is not None:
            marker = "[js]" if js_probe.launched else "[js init]"
            meta = js_probe.probe(c.name, c.careers_url)
            log(f"    {marker} {c.name}: headless scrape... "
                f"{'hit' if meta else 'miss'}")
        if meta:
            c.confirmed  = True
            c.ats        = "workday"
            # Encode the triple in slug_guess so apply_to_config and
            # the report can parse it back out. wd_pod stays numeric.
            c.slug_guess = f"{meta['tenant']}|{meta['wd_pod']}|{meta['site']}"
            c.job_count  = meta["count"]
            c.tried_slugs.append(
                f"[workday:{meta['tenant']}.wd{meta['wd_pod']}/{meta['site']}"
                + ("" if meta["validated"] else " ~unvalidated")
                + "]"
            )
    return c


def _merge_seeds(claude_raw: list[dict], seeds: list[dict]) -> list[dict]:
    """
    Append seed candidates to Claude's output, deduping by normalized
    name. Claude's entry wins when both sources have the same company
    (its ats/slug_guess may be more accurate than the seed's 'unknown').
    """
    seen = {_normalize_name(c.get("name") or "").lower() for c in claude_raw}
    return list(claude_raw) + [
        s for s in seeds
        if _normalize_name(s["name"]).lower() not in seen
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


def _validate_all(candidate_dicts, use_js=True):
    """Validate candidate dicts in parallel; return Candidate objects in
    input order. Shared by Claude-driven discover() and name-list-driven
    discover_companies().

    use_js gates the headless-browser Workday fallback. A single browser is
    single-threaded (Playwright greenlet affinity), so the fallback runs as
    a POOL of _JS_BROWSERS browsers — candidates that need it borrow a free
    one and only block when all are busy, instead of all queuing on one.
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
        # Small inter-probe delay: each probe hits a different ATS host, and
        # workers already run concurrently, so politeness sleeps add up to
        # dead time per candidate. 0.05 keeps a light touch without the tax.
        validate_candidate(
            cand, delay=0.05, js_probe=js_probe, log=buf.append,
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
    js_probe = WorkdayJsProbePool(_JS_BROWSERS) if use_js else None
    try:
        with ThreadPoolExecutor(max_workers=_DISCOVERY_WORKERS) as pool:
            futures = [pool.submit(_worker, i, rc, js_probe)
                       for i, rc in enumerate(candidate_dicts)]
            for fut in as_completed(futures):
                idx, cand = fut.result()
                validated[idx] = cand
    finally:
        if js_probe is not None:
            js_probe.close()
    return validated


def discover_companies(candidate_dicts, term, use_js=False):
    """Resolve an explicit list of candidate dicts (e.g. harvested from the
    BCIWiki directory) to crawlable boards — no Claude call. Returns the
    same result shape as discover().

    use_js defaults False: bulk directory sweeps are dominated by the
    single-threaded browser fallback, and few entries are Workday SPAs.
    Pass use_js=True for a smaller, thorough pass."""
    print(f"  > Resolving {len(candidate_dicts)} candidate(s) for {term!r} "
          f"(workers={_DISCOVERY_WORKERS}, js={'on' if use_js else 'off'})")
    if not candidate_dicts:
        return {"term": term, "companies": [], "gated_sites": []}
    validated = _validate_all(candidate_dicts, use_js=use_js)
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
            f.write("## Confirmed - ready to add to config.py\n\n")
            for ats_name, cands in by_ats.items():
                dict_name = {
                    "greenhouse": "GREENHOUSE_COMPANIES",
                    "lever":      "LEVER_COMPANIES",
                    "ashby":      "ASHBY_COMPANIES",
                    "kula":       "KULA_COMPANIES",
                    "jazzhr":     "JAZZHR_COMPANIES",
                    "bamboohr":   "BAMBOOHR_COMPANIES",
                    "adp":        "ADP_COMPANIES",
                    "workday":    "WORKDAY_COMPANIES",
                }.get(ats_name, f"{ats_name.upper()}_COMPANIES")
                f.write(f"### `{dict_name}`\n\n```python\n")
                for c in cands:
                    note = verify_note(c)
                    suffix = f"  # VERIFY: {note}" if note else ""
                    if ats_name in ("jazzhr", "bamboohr"):
                        # {subdomain: "Company"} dicts.
                        f.write(f'    "{c.slug_guess}": "{c.name}",  '
                                f'# {c.job_count} job(s) live{suffix}\n')
                    elif ats_name == "adp":
                        cid, _, ccid = (c.slug_guess or "").partition("|")
                        f.write(f'    ("{c.name}", "{cid}", "{ccid}"),  '
                                f'# {c.job_count} job(s) live{suffix}\n')
                    elif ats_name == "kula":
                        f.write(f'    ("{c.name}", "{c.slug_guess}"),{suffix}\n')
                    elif ats_name == "workday":
                        parts = (c.slug_guess or "").split("|")
                        if len(parts) == 3 and parts[1].isdigit():
                            t, p, s = parts
                            f.write(f'    ("{t}", {int(p)}, "{s}", "{c.name}"),  '
                                    f'# {c.job_count} job(s) live{suffix}\n')
                        else:
                            f.write(f'    # malformed workday slug for {c.name}: '
                                    f'{c.slug_guess!r}\n')
                    else:
                        f.write(f'    "{c.slug_guess}": "{c.name}",  '
                                f'# {c.job_count} job(s) live{suffix}\n')
                f.write("```\n\n")

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
            f.write("| Company | ATS guess | ATS lead | Slugs tried | Careers URL | Notes |\n")
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
        print(f"    + {c.name:<30} {c.ats:<10} slug='{c.slug_guess}'  "
              f"({c.job_count} jobs){tail}")
    unconfirmed = [c for c in companies if not c.confirmed]
    if unconfirmed:
        leads = [c for c in unconfirmed if c.ats_lead]
        if leads:
            print(f"\n  Detected ATS (manual add, not auto-fetchable):")
            for c in leads:
                print(f"    > {c.name:<30} {c.ats_lead}")
        print(f"\n  Unconfirmed ({len(unconfirmed)}):")
        for c in unconfirmed:
            lead = f"  lead={c.ats_lead}" if c.ats_lead else ""
            print(f"    ? {c.name:<30} {c.ats:<10} tried={c.tried_slugs}{lead}")
    print(f"{'='*w}\n")
