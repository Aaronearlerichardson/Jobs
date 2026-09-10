"""Declarative ATS source registry.

One table describes, per ATS: how to build a fetch thunk from a store row,
the default seed tag discovery assigns, and the politeness pause for the
serial orchestrator. Every crawl path iterates STORE company rows — the
companies table is the single roster (config.py seed lists retired 2026-07;
manage the roster with discover.py, --add-board, or --import-companies).

The seed TAG (src/tags.py) is the scope a newly added company gets, and it
follows one rule: SWEEP for every ATS in LIGHTWEIGHT (a cheap JSON or
single-page board the sweep pulls whole), LOCAL for the enterprise boards
that are only worth querying per region (workday, successfactors,
peopleadmin). Paylocity and UltiPro seeded LOCAL until 2026-09 because the
first boards found on them belonged to one user's local search; the tag
has meant crawl mechanics since the names were generalized, and both
boards are pulled whole in one or two requests like the rest of
LIGHTWEIGHT. tests/test_fetcher_parsers.py pins the rule.

Deliberately absent: ``src.store.CAPTURE_ATS`` ("capture"). A capture-only
company has no fetchable board -- its pages are saved by hand through
capture.py -- and store.crawlable_companies never hands such a row to any
crawl path, so it neither needs a thunk here nor counts as "unsupported":
an ATS name this table lacks is simply skipped by iter_store_sources.
"""

from src import tags
from src.match.filters import is_relevant

from .fetchers import (
    fetch_adp,
    fetch_ashby,
    fetch_bamboohr,
    fetch_greenhouse,
    fetch_hibob,
    fetch_jazzhr,
    fetch_jobvite,
    fetch_kula,
    fetch_lever,
    fetch_paylocity,
    fetch_peopleadmin,
    fetch_rippling,
    fetch_ultipro,
    fetch_successfactors,
    fetch_workday,
)

# ats -> (thunk(name, slug) -> fetch callable, seed tag, politeness pause)
#
# Every thunk passes `gate=is_relevant`: the fetchers themselves are
# ungated (gate=None keeps every posting), and this registry is where the
# profile's keyword filter is injected for the unvetted-board sweep. The
# company-vetted path (fetchers/company.py) calls the same fetchers with
# no gate and a location regex instead.
ATS_REGISTRY = {
    "greenhouse": (lambda n, s: lambda: fetch_greenhouse(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "lever":      (lambda n, s: lambda: fetch_lever(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "ashby":      (lambda n, s: lambda: fetch_ashby(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "kula":       (lambda n, s: lambda: fetch_kula(n, s, gate=is_relevant), tags.SWEEP, 0.5),
    "jazzhr":     (lambda n, s: lambda: fetch_jazzhr(n, s, gate=is_relevant), tags.SWEEP, 0.5),
    "jobvite":    (lambda n, s: lambda: fetch_jobvite(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "bamboohr":   (lambda n, s: lambda: fetch_bamboohr(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "adp":        (lambda n, s: lambda: fetch_adp(*s.split("|", 1), n, gate=is_relevant), tags.SWEEP, 0.5),
    "paylocity":  (lambda n, s: lambda: fetch_paylocity(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "rippling":   (lambda n, s: lambda: fetch_rippling(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "ultipro":    (lambda n, s: lambda: fetch_ultipro(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "hibob":      (lambda n, s: lambda: fetch_hibob(s, n, gate=is_relevant), tags.SWEEP, 0.5),
    "workday":    (lambda n, s: (lambda t=s.split("|")[0], p=int(s.split("|")[1]),
                                        st=s.split("|")[2]:
                                 fetch_workday(t, p, st, n, gate=is_relevant)), tags.LOCAL, 1.0),
    "successfactors": (lambda n, s: lambda: fetch_successfactors(n, s, gate=is_relevant), tags.LOCAL, 1.0),
    "peopleadmin":    (lambda n, s: lambda: fetch_peopleadmin(s, n, gate=is_relevant), tags.LOCAL, 1.0),
}

# ATSes whose store rows a location-agnostic ("sweep") track pulls whole,
# and that seed the SWEEP tag: lightweight JSON APIs or single-page boards.
# The heavyweight boards stay location-scoped and seed LOCAL.
LIGHTWEIGHT = ("greenhouse", "lever", "ashby", "kula", "jazzhr", "jobvite", "bamboohr", "adp", "paylocity", "rippling", "ultipro", "hibob")


def seed_tag_for(ats):
    entry = ATS_REGISTRY.get(ats)
    return entry[1] if entry else None


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
