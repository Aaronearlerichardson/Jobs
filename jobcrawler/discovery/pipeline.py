"""Company discovery pipeline — Claude → candidates → ATS probe → report."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import REPORT_DIR
from ..claude import DISCOVER_SYSTEM, call_claude_json
from .probes import PROBES


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


def candidate_from_dict(d):
    return Candidate(
        name        = d.get("name", "").strip(),
        ats         = (d.get("ats") or "unknown").lower(),
        slug_guess  = (d.get("slug_guess") or None),
        careers_url = d.get("careers_url", "").strip(),
        notes       = d.get("notes", "").strip(),
    )


def slug_variants(name, first_guess):
    base = (name or "").lower().strip()
    variants = []
    if first_guess:
        variants.append(first_guess.lower())
    variants += [
        base.replace(" ", "-"),
        base.replace(" ", ""),
        base.replace(",", "").replace(".", "").replace(" ", "-"),
        base.split()[0] if base else "",
    ]
    seen, out = set(), []
    for v in variants:
        v = v.strip("-")
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out[:4]


def validate_candidate(c, delay=0.3):
    probe = PROBES.get(c.ats)
    if not probe:
        return c
    for slug in slug_variants(c.name, c.slug_guess):
        c.tried_slugs.append(slug)
        ok, count = probe(slug)
        time.sleep(delay)
        if ok:
            c.confirmed = True
            c.slug_guess = slug
            c.job_count = count
            return c
    return c


def discover(term):
    print(f"  > Asking Claude for companies in: {term!r}")
    payload = call_claude_json(DISCOVER_SYSTEM, term, max_tokens=2000)
    if not payload:
        return {"term": term, "companies": [], "gated_sites": []}

    raw_companies = payload.get("companies", [])
    print(f"  > Claude returned {len(raw_companies)} company suggestion(s)")
    validated = []
    for i, rc in enumerate(raw_companies, 1):
        cand = candidate_from_dict(rc)
        print(f"  [{i}/{len(raw_companies)}] {cand.name} ({cand.ats})")
        validated.append(validate_candidate(cand))

    return {
        "term":        term,
        "companies":   validated,
        "gated_sites": payload.get("gated_sites", []),
    }


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
                }.get(ats_name, f"{ats_name.upper()}_COMPANIES")
                f.write(f"### `{dict_name}`\n\n```python\n")
                for c in cands:
                    if ats_name == "kula":
                        f.write(f'    ("{c.name}", "{c.slug_guess}"),\n')
                    else:
                        f.write(f'    "{c.slug_guess}": "{c.name}",  '
                                f'# {c.job_count} job(s) live\n')
                f.write("```\n\n")

        if unconfirmed:
            f.write("## Unconfirmed - manual investigation needed\n\n")
            f.write("| Company | ATS guess | Slugs tried | Careers URL | Notes |\n")
            f.write("|---|---|---|---|---|\n")
            for c in unconfirmed:
                tried = ", ".join(f"`{s}`" for s in c.tried_slugs) or "-"
                f.write(f"| {c.name} | {c.ats} | {tried} | "
                        f"{c.careers_url or '-'} | {c.notes} |\n")
            f.write("\n")

        gated = result.get("gated_sites", [])
        if gated:
            f.write("## Gated sites (require auth)\n\n")
            f.write("Login-only boards Claude thinks are worth searching. "
                    "Use `python discover.py --capture-session <site>` to "
                    "save an authenticated session for these.\n\n")
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
        print(f"    + {c.name:<30} {c.ats:<10} slug='{c.slug_guess}'  "
              f"({c.job_count} jobs)")
    unconfirmed = [c for c in companies if not c.confirmed]
    if unconfirmed:
        print(f"\n  Unconfirmed ({len(unconfirmed)}):")
        for c in unconfirmed:
            print(f"    ? {c.name:<30} {c.ats:<10} tried={c.tried_slugs}")
    print(f"{'='*w}\n")
