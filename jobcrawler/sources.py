"""Declarative ATS source registry.

One table describes, per ATS: the config-list name and shape, how to build
a fetch thunk, the store columns a seed maps to, and the default seed tag.
The classic orchestrator, the remote-neural track's source assembly, and
the --import-seeds command all iterate this table instead of hand-writing
per-ATS loops (which previously existed in four slightly different copies).

Seed tags: the lightweight JSON-API boards (greenhouse/lever/.../adp) are
the BCI-focused set -> "neural"; the heavyweight onsite RTP employers
"""

from .fetchers import (
    fetch_adp,
    fetch_ashby,
    fetch_bamboohr,
    fetch_greenhouse,
    fetch_jazzhr,
    fetch_kula,
    fetch_lever,
    fetch_peopleadmin,
    fetch_successfactors,
    fetch_workday,
)


def _norm_dict(items):          # {slug: name}
    return [(name, slug) for slug, name in items.items()]


def _norm_pairs(items):         # [(name, slug)]
    return [(name, slug) for name, slug in items]


def _norm_adp(items):           # [(name, cid, ccid)] -> slug "cid|ccid"
    return [(name, f"{cid}|{ccid}") for name, cid, ccid in items]


def _norm_workday(items):       # [(tenant, pod, site, name)] -> slug "t|p|s"
    return [(name, f"{t}|{p}|{s}") for t, p, s, name in items]


def _norm_hosts(items):         # [(host, name)]
    return [(name, host) for host, name in items]


# ats -> (config list name, normalizer -> [(name, slug)], thunk(name, slug),
#         seed tag, politeness pause for the serial orchestrator)
ATS_REGISTRY = {
    "greenhouse": ("GREENHOUSE_COMPANIES", _norm_dict,
                   lambda n, s: lambda: fetch_greenhouse(s, n), "neural", 0.5),
    "lever":      ("LEVER_COMPANIES", _norm_dict,
                   lambda n, s: lambda: fetch_lever(s, n), "neural", 0.5),
    "ashby":      ("ASHBY_COMPANIES", _norm_dict,
                   lambda n, s: lambda: fetch_ashby(s, n), "neural", 0.5),
    "kula":       ("KULA_COMPANIES", _norm_pairs,
                   lambda n, s: lambda: fetch_kula(n, s), "neural", 0.5),
    "jazzhr":     ("JAZZHR_COMPANIES", _norm_dict,
                   lambda n, s: lambda: fetch_jazzhr(n, s), "neural", 0.5),
    "bamboohr":   ("BAMBOOHR_COMPANIES", _norm_dict,
                   lambda n, s: lambda: fetch_bamboohr(s, n), "neural", 0.5),
    "adp":        ("ADP_COMPANIES", _norm_adp,
                   lambda n, s: lambda: fetch_adp(*s.split("|", 1), n), "neural", 0.5),
    "workday":    ("WORKDAY_COMPANIES", _norm_workday,
                   lambda n, s: (lambda t=s.split("|")[0], p=int(s.split("|")[1]),
                                        st=s.split("|")[2]:
                                 fetch_workday(t, p, st, n)), "nc_local", 1.0),
    "successfactors": ("SUCCESSFACTORS_COMPANIES", _norm_pairs,
                       lambda n, s: lambda: fetch_successfactors(n, s), "nc_local", 1.0),
    "peopleadmin": ("PEOPLEADMIN_COMPANIES", _norm_hosts,
                    lambda n, s: lambda: fetch_peopleadmin(s, n), "nc_local", 1.0),
}

# ATSes whose store rows the remote-neural track sweeps (lightweight JSON
# APIs; the heavyweight onsite boards stay with the local track).
LIGHTWEIGHT = ("greenhouse", "lever", "ashby", "kula", "jazzhr", "bamboohr", "adp")


def iter_config_sources(cfg, only=None):
    """Yield (ats, name, slug, thunk, pause) for every config-listed board."""
    for ats, (list_name, norm, mk, _tag, pause) in ATS_REGISTRY.items():
        if only and ats not in only:
            continue
        items = getattr(cfg, list_name, None)
        if not items:
            continue
        for name, slug in norm(items):
            yield ats, name, slug, mk(name, slug), pause


def store_slug(company):
    """The registry-normalized slug for a store company row."""
    if company.get("ats") == "workday":
        return f"{company.get('wd_tenant')}|{company.get('wd_pod')}|{company.get('wd_site')}"
    return company.get("slug") or company.get("careers_url") or ""


def iter_store_sources(companies, only=LIGHTWEIGHT):
    """Yield (ats, name, slug, thunk) for store company rows."""
    for c in companies:
        ats = c.get("ats")
        if ats not in ATS_REGISTRY or (only and ats not in only):
            continue
        slug = store_slug(c)
        if not slug:
            continue
        _, _, mk, _, _ = ATS_REGISTRY[ats]
        yield ats, c["name"], slug, mk(c["name"], slug)


def seed_rows(cfg):
    """Store rows for every config-listed board (used by --import-seeds)."""
    for ats, (list_name, norm, _mk, tag, _p) in ATS_REGISTRY.items():
        items = getattr(cfg, list_name, None)
        if not items:
            continue
        for name, slug in norm(items):
            row = {"name": name, "ats": ats, "tags": tag,
                   "source": "config_seed", "active": 1}
            if ats == "workday":
                t, p, s = slug.split("|")
                row.update(wd_tenant=t, wd_pod=int(p), wd_site=s)
            elif ats in ("successfactors", "peopleadmin"):
                row["careers_url"] = slug
            else:
                row["slug"] = slug
            yield row
