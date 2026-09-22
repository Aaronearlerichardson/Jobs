"""Write what discovery found into the company store.

Replaces the original config.py source-rewriter: discovery used to regex-edit
Python source (insert entries into GREENHOUSE_COMPANIES etc.), and a separate
--import-seeds step copied them into the store. The store IS the roster now —
candidates upsert straight into the companies table through the same
mission-scoring write path every other automated add uses
(src.discovery.local_sourcing.score_and_upsert).

New rows land in the REVIEW QUEUE (src.store.mark_pending): a candidate the
model suggested and a resolver confirmed is exactly the kind of name that
used to reach the roster without ever having been an employer.
"""

from contextlib import closing

from src import store
from src import tags
from src.ats import coords
from src.ats.registry import seed_tag_for
from src.ats.signatures import detect, pack


def _candidate_hit(c):
    """A confirmed Candidate as the resolver-shaped hit dict the store write
    path takes, or None when its coordinates are malformed. Workday's
    't|p|s' string goes back to the (tenant, pod, site) triple src.ats.coords
    spells out as columns.

    What counts as coordinates is ``src.store.board_key``, the store's own
    rule for which column identifies a board -- the slug for most families,
    the triple for Workday, and the careers URL for the ones keyed on it
    (custom, successfactors, peopleadmin, wpjson). Requiring a slug here
    instead rejected a self-hosted `custom` board, whose only coordinate IS
    its URL, as a malformed one (src.discovery.resolve.board returns exactly
    that for a real careers page on no known platform).
    """
    slug = (c.slug_guess or "").strip() or None
    if c.ats == "workday":
        parts = (slug or "").split("|")
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        slug = (parts[0], int(parts[1]), parts[2])
    hit = {"name": c.name, "ats": c.ats, "slug": slug,
           "careers_url": c.careers_url or None,
           "count": c.job_count, "nc": c.nc}
    # The same coordinates score_and_upsert will write, asked of the same
    # function company_by_board dedups on: no board, no row.
    return hit if store.board_key(coords.from_hit(hit)) else None


def apply_to_store(result, dry_run: bool = False) -> list[str]:
    """Mission-score confirmed candidates and write them to the companies
    table; return summary lines. `dry_run=True` reports without writing —
    and without paying for a mission call.

    The write is local_sourcing.score_and_upsert, the one path behind every
    automated add: it mission-scores the board, activates it only if the tier
    says so (src.claude.is_active_mission), refuses a board the roster
    already holds under another name, and queues anything the store has not
    confirmed for review.

    Notes:
        This used to build the row here instead, with ``active=1``
        unconditionally and the mission columns left NULL for a later
        ``--score-missions`` pass. So a `discover-term` / `--from-bciwiki`
        candidate was the one discovery product that skipped the mission
        gate: confirming its review row put an unscored company on the
        roster, ACTIVE, and it stayed crawled until somebody remembered to
        run the backfill.
    """
    # Deferred: the write path pulls in the fetchers and the mission scorer,
    # and src.discovery.__init__ imports this module on every `import
    # src.discovery` -- including the ones that only want the report.
    from src.ats.fetchers import company as company_fetch
    from .local_sourcing import score_and_upsert

    term = result["term"]
    confirmed = [c for c in result["companies"] if c.confirmed]
    if not confirmed:
        return [f"  (no confirmed candidates for '{term}')"]

    with closing(store.connect()) as conn:
        added, skipped, summary = 0, 0, []
        for c in confirmed:
            # "Can this row be fetched" is fetchers.company.FETCHERS, the table
            # fetch_company dispatches on and the one every other caller of the
            # write path trusts (local_sourcing._hit_from_detection stores a
            # custom board through it without asking anything else). NOT
            # src.ats.registry.ATS_REGISTRY: that table schedules ONE crawl loop
            # -- iter_store_sources' lightweight sweep -- and deliberately omits
            # the families it does not schedule, `custom` among them. Gating
            # here on it reported a confirmed self-hosted careers page and then
            # threw it away.
            if c.ats not in company_fetch.FETCHERS:
                summary.append(f"    [skip] {c.name}: no fetcher for ATS '{c.ats}'")
                skipped += 1
                continue
            hit = _candidate_hit(c)
            if hit is None:
                summary.append(f"    [skip] {c.name}: malformed slug {c.slug_guess!r}")
                skipped += 1
                continue
            if dry_run:
                added += 1
                summary.append(f"    + {c.name:32} {c.ats:12} (unscored preview)")
                continue
            written = score_and_upsert(
                conn, hit, source=f"discovery:{term[:60]}",
                tags=seed_tag_for(c.ats), extra={"notes": c.notes or None})
            if not written:
                # Already on the roster under another name -- score_and_upsert
                # printed which one.
                skipped += 1
                continue
            row, active, pending = written
            added += 1
            state = "[review]" if pending else ("active" if active else "off-mission")
            summary.append(f"    + {c.name:32} {c.ats:12} "
                           f"{str(row.get('mission_tier')):16} {state}")

    verb = "would queue/refresh" if dry_run else "queued/refreshed"
    summary.insert(0, f"  {'[DRY-RUN] ' if dry_run else ''}{verb} {added} "
                      f"compan(ies) in the store, {skipped} skipped")
    if added and not dry_run:
        summary.append("  Confirm the [review] rows in the web UI's Review "
                       "section before they are crawled")
    return summary


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
        hit = detect("", url or "", leads=False)
        if not hit:
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
