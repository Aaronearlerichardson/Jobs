"""Declarative ATS source registry.

One table describes, per ATS: how to build a fetch thunk from a store row,
the default seed tag discovery assigns, and the politeness pause for the
serial orchestrator. Every crawl path iterates STORE company rows — the
companies table is the single roster (config.py seed lists retired 2026-07;
manage the roster with discover.py, --add-board, or --import-companies).

Seed tags: the lightweight JSON-API boards (greenhouse/lever/.../adp) are
the BCI-focused set -> "neural"; the heavyweight onsite RTP employers
(workday/successfactors/peopleadmin) -> "nc_local".
"""

from .fetchers import (
    fetch_adp,
    fetch_ashby,
    fetch_bamboohr,
    fetch_greenhouse,
    fetch_jazzhr,
    fetch_kula,
    fetch_lever,
    fetch_paylocity,
    fetch_peopleadmin,
    fetch_successfactors,
    fetch_workday,
)

# ats -> (thunk(name, slug) -> fetch callable, seed tag, politeness pause)
ATS_REGISTRY = {
    "greenhouse": (lambda n, s: lambda: fetch_greenhouse(s, n), "neural", 0.5),
    "lever":      (lambda n, s: lambda: fetch_lever(s, n), "neural", 0.5),
    "ashby":      (lambda n, s: lambda: fetch_ashby(s, n), "neural", 0.5),
    "kula":       (lambda n, s: lambda: fetch_kula(n, s), "neural", 0.5),
    "jazzhr":     (lambda n, s: lambda: fetch_jazzhr(n, s), "neural", 0.5),
    "bamboohr":   (lambda n, s: lambda: fetch_bamboohr(s, n), "neural", 0.5),
    "adp":        (lambda n, s: lambda: fetch_adp(*s.split("|", 1), n), "neural", 0.5),
    "paylocity":  (lambda n, s: lambda: fetch_paylocity(s, n), "nc_local", 0.5),
    "workday":    (lambda n, s: (lambda t=s.split("|")[0], p=int(s.split("|")[1]),
                                        st=s.split("|")[2]:
                                 fetch_workday(t, p, st, n)), "nc_local", 1.0),
    "successfactors": (lambda n, s: lambda: fetch_successfactors(n, s), "nc_local", 1.0),
    "peopleadmin":    (lambda n, s: lambda: fetch_peopleadmin(s, n), "nc_local", 1.0),
}

# ATSes whose store rows the remote-neural track sweeps (lightweight JSON
# APIs; the heavyweight onsite boards stay with the local track).
LIGHTWEIGHT = ("greenhouse", "lever", "ashby", "kula", "jazzhr", "bamboohr", "adp", "paylocity")


def seed_tag_for(ats):
    entry = ATS_REGISTRY.get(ats)
    return entry[1] if entry else None


def pause_for(ats):
    entry = ATS_REGISTRY.get(ats)
    return entry[2] if entry else 1.0


def store_slug(company):
    """The registry-normalized slug for a store company row."""
    if company.get("ats") == "workday":
        return f"{company.get('wd_tenant')}|{company.get('wd_pod')}|{company.get('wd_site')}"
    return company.get("slug") or company.get("careers_url") or ""


def iter_store_sources(companies, only=LIGHTWEIGHT):
    """Yield (ats, name, slug, thunk) for store company rows. `only=None`
    iterates every registered ATS (the classic orchestrator's sweep)."""
    for c in companies:
        ats = c.get("ats")
        if ats not in ATS_REGISTRY or (only and ats not in only):
            continue
        slug = store_slug(c)
        if not slug:
            continue
        if ats == "workday":
            # Guard malformed triples (e.g. a lead row with NULL tenant →
            # "None|None|None") — int(pod) at thunk-build would crash.
            parts = slug.split("|")
            if len(parts) != 3 or not parts[1].isdigit():
                continue
        mk, _tag, _pause = ATS_REGISTRY[ats]
        yield ats, c["name"], slug, mk(c["name"], slug)
