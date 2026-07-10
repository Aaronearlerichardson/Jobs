"""Import the config.py company lists into the unified store.

config.py keeps the hand-curated / discovery-applied company lists as
SEEDS — human-reviewable, diffable, and the zero-setup fallback. The store
is the operational roster both tracks crawl. This importer maps each list
to store rows with scope tags:

    neural    — the BCI/neurotech-focused boards (Greenhouse/Lever/Ashby/
                Kula/JazzHR/BambooHR/ADP lists): swept by the remote-neural
                track.
    nc_local  — the heavyweight onsite RTP employers (Workday/
                SuccessFactors/PeopleAdmin lists): crawled by the local
                track. Deliberately excluded from remote-neural (thousands
                of onsite reqs, almost entirely culled by the remote
                filter).

Import is idempotent: companies upsert by name and tags MERGE, so re-running
after a discovery pass never loses scopes. Mission fields are left alone —
the local-sourcing / dorking passes own those.

    python crawler.py --import-seeds
"""

import config

from . import store


def _rows(cfg):
    for slug, name in getattr(cfg, "GREENHOUSE_COMPANIES", {}).items():
        yield {"name": name, "ats": "greenhouse", "slug": slug, "tags": "neural"}
    for slug, name in getattr(cfg, "LEVER_COMPANIES", {}).items():
        yield {"name": name, "ats": "lever", "slug": slug, "tags": "neural"}
    for slug, name in getattr(cfg, "ASHBY_COMPANIES", {}).items():
        yield {"name": name, "ats": "ashby", "slug": slug, "tags": "neural"}
    for name, slug in getattr(cfg, "KULA_COMPANIES", []):
        yield {"name": name, "ats": "kula", "slug": slug, "tags": "neural"}
    for sub, name in getattr(cfg, "JAZZHR_COMPANIES", {}).items():
        yield {"name": name, "ats": "jazzhr", "slug": sub, "tags": "neural"}
    for sub, name in getattr(cfg, "BAMBOOHR_COMPANIES", {}).items():
        yield {"name": name, "ats": "bamboohr", "slug": sub, "tags": "neural"}
    for name, cid, ccid in getattr(cfg, "ADP_COMPANIES", []):
        yield {"name": name, "ats": "adp", "slug": f"{cid}|{ccid}", "tags": "neural"}
    for tenant, pod, site, name in getattr(cfg, "WORKDAY_COMPANIES", []):
        yield {"name": name, "ats": "workday", "wd_tenant": tenant,
               "wd_pod": pod, "wd_site": site, "tags": "nc_local"}
    for name, base_url in getattr(cfg, "SUCCESSFACTORS_COMPANIES", []):
        yield {"name": name, "ats": "successfactors", "careers_url": base_url,
               "tags": "nc_local"}
    for host, name in getattr(cfg, "PEOPLEADMIN_COMPANIES", []):
        yield {"name": name, "ats": "peopleadmin", "careers_url": host,
               "tags": "nc_local"}


def import_config_seeds(cfg=config, verbose=True):
    """Upsert every config company list into the store. Returns count."""
    conn = store.connect()
    n = 0
    for row in _rows(cfg):
        row.setdefault("source", "config_seed")
        row.setdefault("active", 1)
        store.upsert_company(conn, row)
        n += 1
        if verbose:
            print(f"  + {row['name']:36} {row['ats']:14} [{row['tags']}]")
    conn.close()
    if verbose:
        print(f"\n  {n} compan(ies) imported/refreshed into the store.")
    return n
