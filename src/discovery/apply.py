"""Write what discovery found into the company store.

Replaces the original config.py source-rewriter: discovery used to regex-edit
Python source (insert entries into GREENHOUSE_COMPANIES etc.), and a separate
--import-seeds step copied them into the store. The store IS the roster now —
candidates upsert straight into the companies table, deduped by name (upsert)
and by ats+slug (a second name for the same board is skipped).

Mission fields are left NULL — `python discover.py --score-missions` owns
those. New rows land in the REVIEW QUEUE (src.store.mark_pending): a
candidate the model suggested and a slug guess confirmed is exactly the kind
of name that used to reach the roster without ever having been an employer.
"""

from datetime import datetime

from src import store
from src import tags
from src.ats.registry import ATS_REGISTRY, seed_tag_for
from src.ats.signatures import detect, pack


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
        pending = not store.is_confirmed_company(conn, c.name)
        if pending:
            row = store.mark_pending(row)
        if not dry_run:
            store.upsert_company(conn, row)
        have_names.add((c.name or "").lower())
        have_boards.add(key)
        added += 1
        summary.append(f"    + {c.name:32} {ats:12} "
                       f"{'[review]' if pending else '(refresh)'}")

    conn.close()
    verb = "would queue/refresh" if dry_run else "queued/refreshed"
    summary.insert(0, f"  {'[DRY-RUN] ' if dry_run else ''}{verb} {added} "
                      f"compan(ies) in the store, {skipped} skipped")
    if added and not dry_run:
        summary.append("  Confirm the [review] rows in the web UI's Review "
                       "section before they are crawled")
        summary.append("  Mission scores pending -> python discover.py --score-missions")
    return summary


# Back-compat alias: discover.py historically imported apply_to_config.
apply_to_config = apply_to_store


# --------------------------------------------------------------------------- #
#  Employer attribution: aggregator postings -> roster rows                    #
# --------------------------------------------------------------------------- #
#
# An aggregator board (a VC portfolio, an industry association) carries the
# openings of many employers and names the employer on each posting. The
# fetcher that reads such a board stamps `_employer` on every job; this is
# what happens next, and it is the same act as the rest of this module --
# turn something discovery found into a roster row, and queue it for
# review rather than trusting it.
#
# It lived in src/ats/fetchers/getro.py, next to the parser that produces
# `_employer`. That put a store WRITE inside a fetcher: src/ats otherwise
# knows nothing about the roster, and this single function was the whole
# reason the package reached src/store at all. Nothing in it is
# Getro-specific except the `source` prefix, which is now an argument.


def _coords_from_urls(urls):
    """Roster-shaped board coordinates for the employer, read off its
    apply links, or None when none of them names a known ATS."""
    for url in urls:
        hit = detect("", url or "")
        if not hit or hit[0] not in ("fetchable", "semi"):
            continue
        packed = pack(hit[1], hit[2], url)
        row = {"ats": packed["ats"], "careers_url": packed.get("careers_url")}
        if packed["ats"] == "workday":
            t, pod, site = packed["triple"]
            row.update({"wd_tenant": t, "wd_pod": pod, "wd_site": site})
        else:
            row["slug"] = packed.get("slug")
        return row
    return None


def attribute_employers(conn, jobs, commit=True, source="getro"):
    """Link each board-sourced job to its employer's roster row, queueing
    employers the roster lacks for review. Returns the jobs to keep.

    Jobs without an ``_employer`` record pass through untouched. For the
    rest, per employer:

    * a roster row that owns the same board (the apply link's ATS
      coordinates — ``src.store.company_by_board``) or the same name gets
      ``company_id`` stamped on the jobs. When that row is ACTIVE and
      confirmed, its own crawl covers the board, so a posting the store
      already holds under the employer's URL is dropped here rather than
      stored twice;
    * a name the reviewer rejected (``src.store.block_name``) drops its
      jobs — that decision was "not a company", and it sticks;
    * anything else becomes a review candidate under `commit`:
      ``src.store.mark_pending`` (inactive, tagged pending-review), with
      ``source = "getro:<board host>"`` and the ATS coordinates when the
      apply link revealed them. Never an active row.

    `source` names the aggregator in the stored `source` column
    ("getro:<board host>"). Getro is the only fetcher stamping
    `_employer` today; the argument is here so the next one does not
    have to fork this.

    See tests/test_fetcher_parsers.py::TestGetroAttribution.
    """
    groups = {}
    for j in jobs:
        emp = j.get("_employer")
        if isinstance(emp, dict) and (emp.get("name") or emp.get("slug")):
            groups.setdefault(emp.get("slug") or emp["name"].lower(),
                              []).append(j)
    if not groups:
        return list(jobs)

    blocked = store.blocked_name_keys(conn)
    drop = set()
    for key, group in groups.items():
        emp = group[0]["_employer"]
        name = emp.get("name") or key
        coords = _coords_from_urls([j.get("url") for j in group])
        row = store.company_by_board(conn, coords) if coords else None
        if row is None:
            cid = store.company_id_by_name(conn, name)
            row = store.get_company(conn, cid) if cid else None
        if row is not None:
            crawled = bool(row.get("active")) and not tags.has(
                row.get("tags"), tags.PENDING)
            for j in group:
                j["company_id"] = row["id"]
                if crawled and j.get("url") and conn.execute(
                        "SELECT 1 FROM jobs WHERE url=? LIMIT 1",
                        (j["url"],)).fetchone():
                    drop.add(id(j))
            continue
        if store._name_key(name) in blocked:
            drop.update(id(j) for j in group)
            continue
        if not commit:
            continue
        via = f"{source}:{emp.get('board') or ''}"
        careers_url = ((coords or {}).get("careers_url")
                       or (f"https://{emp['domain']}" if emp.get("domain")
                           else None))
        candidate = {"name": name, "careers_url": careers_url,
                     "source": via,
                     "notes": f"employer on the {emp.get('board')} board; "
                              f"{len(group)} relevant posting(s)"}
        if coords:
            candidate.update(coords)
            candidate["careers_url"] = careers_url
            candidate["tags"] = seed_tag_for(coords["ats"])
        cid = store.upsert_company(conn, store.mark_pending(candidate))
        for j in group:
            j["company_id"] = cid
    return [j for j in jobs if id(j) not in drop]
