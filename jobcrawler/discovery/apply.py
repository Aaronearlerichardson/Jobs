"""Write confirmed discovery candidates into the company store.

Replaces the original config.py source-rewriter: discovery used to regex-edit
Python source (insert entries into GREENHOUSE_COMPANIES etc.), and a separate
--import-seeds step copied them into the store. The store IS the roster now —
candidates upsert straight into the companies table, deduped by name (upsert)
and by ats+slug (a second name for the same board is skipped).

Mission fields are left NULL — `python discover.py --score-missions` owns
those. Rows land active; the mission pass deactivates off-mission companies.
"""

from datetime import datetime

from .. import store
from ..sources import ATS_REGISTRY, seed_tag_for


def _slug_fields(ats, slug):
    """Map a candidate slug to store columns. Workday slugs are 't|p|s'."""
    if ats == "workday":
        parts = (slug or "").split("|")
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        return {"wd_tenant": parts[0], "wd_pod": int(parts[1]), "wd_site": parts[2]}
    return {"slug": slug}


def _board_key(row):
    """Identity of a board for cross-name dedup: (ats, normalized slug)."""
    if row.get("ats") == "workday":
        return ("workday", f"{row.get('wd_tenant')}|{row.get('wd_pod')}|{row.get('wd_site')}")
    return (row.get("ats"), row.get("slug"))


def apply_to_store(result, dry_run: bool = False) -> list[str]:
    """Upsert confirmed candidates into the companies table; return summary
    lines. `dry_run=True` reports without writing."""
    term = result["term"]
    confirmed = [c for c in result["companies"] if c.confirmed]
    if not confirmed:
        return [f"  (no confirmed candidates for '{term}')"]

    conn = store.connect()
    existing = store.get_companies(conn, active_only=False)
    have_names = {(c["name"] or "").lower() for c in existing}
    have_boards = {_board_key(c) for c in existing if c.get("ats")}

    added, skipped, summary = 0, 0, []
    for c in confirmed:
        ats = c.ats
        if ats not in ATS_REGISTRY:
            summary.append(f"    [skip] {c.name}: no fetcher for ATS '{ats}'")
            skipped += 1
            continue
        fields = _slug_fields(ats, (c.slug_guess or "").strip())
        if not fields or not any(fields.values()):
            summary.append(f"    [skip] {c.name}: malformed slug {c.slug_guess!r}")
            skipped += 1
            continue
        row = {"name": c.name, "ats": ats, **fields,
               "careers_url": c.careers_url or None,
               "total_job_count": c.job_count,
               "tags": seed_tag_for(ats), "source": f"discovery:{term[:60]}",
               "notes": (c.notes or None), "active": 1,
               "last_probed": datetime.now().isoformat()}
        key = _board_key(row)
        is_new_name = (c.name or "").lower() not in have_names
        if is_new_name and key in have_boards:
            summary.append(f"    [dup ] {c.name}: board {key[0]}:{key[1]} "
                           f"already registered under another name")
            skipped += 1
            continue
        if not dry_run:
            store.upsert_company(conn, row)
        have_names.add((c.name or "").lower())
        have_boards.add(key)
        added += 1
        summary.append(f"    + {c.name:32} {ats:12} "
                       f"{'(refresh)' if not is_new_name else ''}")

    conn.close()
    verb = "would add/refresh" if dry_run else "added/refreshed"
    summary.insert(0, f"  {'[DRY-RUN] ' if dry_run else ''}{verb} {added} "
                      f"compan(ies) in the store, {skipped} skipped")
    if added and not dry_run:
        summary.append("  Mission scores pending -> python discover.py --score-missions")
    return summary


# Back-compat alias: discover.py historically imported apply_to_config.
apply_to_config = apply_to_store
