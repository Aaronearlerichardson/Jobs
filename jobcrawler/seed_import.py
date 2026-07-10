"""Import the config.py company lists into the unified store.

config.py keeps the hand-curated / discovery-applied company lists as
SEEDS — human-reviewable, diffable, and the zero-setup fallback. The store
is the operational roster both tracks crawl. Import is idempotent:
companies upsert by name and tags MERGE, so re-running after a discovery
pass never loses scopes. Mission fields are left alone — the local
sourcing / dorking passes own those.

    python crawler.py --import-seeds
"""

import config

from . import store
from .sources import seed_rows


def import_config_seeds(cfg=config, verbose=True):
    """Upsert every config company list into the store. Returns count."""
    conn = store.connect()
    n = 0
    for row in seed_rows(cfg):
        store.upsert_company(conn, row)
        n += 1
        if verbose:
            print(f"  + {row['name']:36} {row['ats']:14} [{row['tags']}]")
    conn.close()
    if verbose:
        print(f"\n  {n} compan(ies) imported/refreshed into the store.")
    return n
