"""Stored stamps recomputed after the rule that sets them changes: a job's
`geo_mode`, a company's `mission_tier`, and the `employer_id` that joins
two boards of one company."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from src import config, store
from src.config import RuntimeTrack
from src.match.locality import geo_mode
from src.ops.maintenance import track_store
from src.store.schema import SqlScalar


def restamp_geo(commit: bool = False, undo: str = "", t: RuntimeTrack | None = None,
                conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """PREVIEW (default) or APPLY recomputing `geo_mode` on every open job
    from its stored location and description. Prints each old -> new
    transition's count with five samples; returns {"old -> new": count}.

    The store keeps the FIRST stamp a row gets (store.record_triage and
    upsert_job both COALESCE), so a row stamped before the rule changed
    keeps its old answer until this runs: the ranking admits a row at a
    trusted company on `geo_mode == 'remote'` alone.

    `commit=True` first writes every changed row's old stamp to
    db_backups/restamp-<column>-<time>.json under the data dir, then
    updates them in one transaction. `undo=<that file>` puts the old stamps
    back (again a preview until `commit`).

    >>> conn = store.connect(":memory:")
    >>> for jid, loc in [("a", "Alameda"), ("b", "United States"), ("c", "Durham, NC")]:
    ...     _ = store.upsert_job(conn, {"job_id": jid, "title": "T", "location": loc,
    ...                                 "company_name": "Co",
    ...                                 "description": "We are a distributed team."})
    >>> _ = conn.execute("UPDATE jobs SET geo_mode='remote' WHERE job_id='a'")
    >>> restamp_geo(conn=conn)
      3 row(s) read, 3 would change
      remote -> None  1
        Co | T | Alameda
      None -> remote  1
        Co | T | United States
      None -> onsite  1
        Co | T | Durham, NC
      preview: nothing written.
    {'remote -> None': 1, 'None -> remote': 1, 'None -> onsite': 1}

    Notes:
        Open rows only: a closed row is out of every ranking. `geo_mode`
        is the only column written; `remote_eligible` and `remote_signal`
        keep what triage stamped.
    """
    with track_store(t, conn) as conn:
        def fresh() -> Iterable[tuple[SqlScalar, SqlScalar, SqlScalar, sqlite3.Row]]:
            for r in conn.execute(
                    "SELECT job_id, company_name, title, location, description, geo_mode "
                    "FROM jobs WHERE closed_at IS NULL"):
                yield r["job_id"], r["geo_mode"], geo_mode(r["location"], r["description"]), r
        return _restamp(conn, commit, undo, ("jobs", "job_id", "geo_mode"), fresh(),
                        lambda r: f"{r['company_name'] or ''} | {(r['title'] or '')[:50]} | "
                                  f"{(r['location'] or '')[:50]}")


def restamp_tiers(commit: bool = False, undo: str = "", t: RuntimeTrack | None = None,
                  conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """PREVIEW (default) or APPLY setting every scored company's
    `mission_tier` to the tier whose band holds its `mission_score`
    (config.tier_for_score). Same preview, backup and undo as restamp_geo.

    >>> conn = store.connect(":memory:")
    >>> top, bottom = config.MISSION_TIERS[0].name, config.MISSION_TIERS[-1].name
    >>> _ = store.upsert_company(conn, {"name": "Acme", "mission_tier": top,
    ...                                 "mission_score": 0.0})
    >>> restamp_tiers(conn=conn) == {f"{top} -> {bottom}": 1}  # doctest: +ELLIPSIS
      1 row(s) read, 1 would change
      ... -> ...  1
        Acme | score 0.00
      preview: nothing written.
    True

    Notes:
        `active` is left alone (a human-editable switch set at sourcing), so
        a re-tier never parks or wakes a board. Verdicts are the employers'
        and any board's own override. New scores are reconciled when written
        (api.score_company_mission); this brings stored ones in line.
    """
    with track_store(t, conn) as conn:
        def fresh(table: str) -> Iterable[tuple[SqlScalar, SqlScalar, SqlScalar, sqlite3.Row]]:
            for r in conn.execute(
                    f"SELECT id, name, mission_tier, mission_score FROM {table} "
                    "WHERE mission_score IS NOT NULL"):
                yield (r["id"], r["mission_tier"],
                       config.tier_for_score(r["mission_score"], r["mission_tier"]), r)
        def describe(r: sqlite3.Row) -> str:
            return f"{r['name']} | score {r['mission_score']:.2f}"
        # The verdicts live on employers; a board's own (override) is the rare second pass.
        counts: Counter[str] = Counter()
        for table in ("employers", "companies"):
            if table == "companies" and (undo or not conn.execute(
                    "SELECT 1 FROM companies WHERE mission_score IS NOT NULL LIMIT 1").fetchone()):
                break
            counts.update(_restamp(conn, commit, undo, (table, "id", "mission_tier"),
                                   fresh(table), describe))
        return dict(counts)


def _employer_clusters(pairs: Iterable[tuple[int, int, int]], size: dict[int, int],
                       min_share: float) -> list[list[int]]:
    """Groups of company ids joined by (a, b, shared postings) `pairs` whose
    shared count is at least `min_share` of the smaller board's `size`. Chains join.

    >>> _employer_clusters([(1, 2, 30), (2, 3, 30), (4, 5, 1)], dict.fromkeys(range(1, 6), 40), 0.25)
    [[1, 2, 3]]
    """
    parent: dict[int, int] = {}

    def root(i: int) -> int:
        while parent.setdefault(i, i) != i:
            i = parent[i]
        return i
    for a, b, n in pairs:
        if n >= min_share * min(size[a], size[b]):
            parent[root(a)] = root(b)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in sorted(parent):
        groups[root(i)].append(i)
    return list(groups.values())


def link_employers(commit: bool = False, undo: str = "", min_shared: int = 25,
                   min_share: float = 0.25, t: RuntimeTrack | None = None,
                   conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """PREVIEW (default) or APPLY joining companies that are boards of one
    employer under one `employers` row (companies.employer_id), so the
    ranking shows a posting listed on both once. Same preview, backup and
    undo as restamp_geo.

    Two companies are one employer when at least `min_shared` of their open
    postings share a title and city, and those are at least `min_share` of
    the smaller board's. The employer is the cluster's largest board's.

    >>> conn = store.connect(":memory:")
    >>> for name in ("Acme", "Acme Labs"):
    ...     cid = store.upsert_company(conn, {"name": name, "ats": "lever", "slug": name[:9]})
    ...     for i in range(3):
    ...         _ = store.upsert_job(conn, {"job_id": f"{cid}-{i}", "company_id": cid,
    ...                                     "company_name": name, "title": f"T{i}",
    ...                                     "location": "Durham"})
    >>> link_employers(min_shared=2, conn=conn)
      2 row(s) read, 1 would change
      2 -> 1  1
        Acme Labs | Acme
      preview: nothing written.
    {'2 -> 1': 1}

    Notes:
        store.dedup_companies cannot do this: it merges rows of one board key,
        and these are different boards (say a Phenom site and a Workday
        tenant).
    """
    with track_store(t, conn) as conn:
        pairs = conn.execute("""
            WITH k AS (SELECT DISTINCT company_id, norm_title(title) AS t,
                              city_key(location) AS l
                       FROM open_jobs WHERE company_id IS NOT NULL
                         AND city_key(location) != '')
            SELECT a.company_id AS x, b.company_id AS y, COUNT(*) AS n
            FROM k a JOIN k b ON a.t = b.t AND a.l = b.l AND a.company_id < b.company_id
            GROUP BY 1, 2 HAVING n >= ?""", (min_shared,)).fetchall()
        size = dict(conn.execute("SELECT company_id, COUNT(*) FROM open_jobs "
                                 "WHERE company_id IS NOT NULL GROUP BY company_id"))
        clusters = _employer_clusters([(p["x"], p["y"], p["n"]) for p in pairs], size, min_share)
        rows = {r["id"]: r for r in conn.execute(
            "SELECT c.id, c.name, c.employer_id, e.name AS employer FROM companies c "
            "JOIN employers e ON e.id = c.employer_id "
            "WHERE c.id IN (SELECT value FROM json_each(?))",
            (json.dumps([i for ids in clusters for i in ids]),))}

        def fresh() -> Iterable[tuple[SqlScalar, SqlScalar, SqlScalar, tuple[str, str]]]:
            for ids in clusters:
                top = rows[max(ids, key=lambda i: (size[i], -i))]
                for i in sorted(ids, key=lambda i: (-size[i], i)):
                    yield (i, rows[i]["employer_id"], top["employer_id"],
                           (rows[i]["name"], top["employer"]))
        counts = _restamp(conn, commit, undo, ("companies", "id", "employer_id"), fresh(),
                          lambda c: f"{c[0]} | {c[1]}")
        if commit and counts:
            store.realign_job_names(conn)
        return counts


def _restamp[Ctx](conn: sqlite3.Connection, commit: bool, undo: str, column: tuple[str, str, str],
             fresh: Iterable[tuple[SqlScalar, SqlScalar, SqlScalar, Ctx]],
             describe: Callable[[Ctx], str]) -> dict[str, int]:
    """The shared preview/apply/undo over `column` = (table, key, column):
    `fresh` yields (key, stored value, recomputed value, context), and
    `describe` turns a changed row's context into its sample line."""
    if undo:
        return _undo(conn, Path(undo), commit)
    table, key, col = column
    changes: dict[str, list[tuple[SqlScalar, SqlScalar, SqlScalar, Ctx]]] = defaultdict(list)
    n = 0
    for k, old, new, ctx in fresh:
        n += 1
        if new != old:
            changes[f"{old} -> {new}"].append((k, old, new, ctx))
    counts = {kind: len(rows) for kind, rows in changes.items()}
    print(f"  {n} row(s) read, {sum(counts.values())} would change")
    for kind, rows in sorted(changes.items(), key=lambda kv: -len(kv[1])):
        print(f"  {kind}  {len(rows)}")
        for *_, ctx in rows[:5]:
            print(f"    {describe(ctx)}")
    if commit and changes:
        moved = [(k, old, new) for rows in changes.values() for k, old, new, _ in rows]
        backup = _backup(column, [(k, old) for k, old, _ in moved])
        with store.batch(conn):
            conn.executemany(f"UPDATE {table} SET {col}=? WHERE {key}=?",
                             [(new, k) for k, _, new in moved])
        print(f"  applied. old stamps saved to {backup}; restore with undo={backup}")
    else:
        print("  applied." if commit else "  preview: nothing written.")
    return counts


def _backup(column: tuple[str, str, str], old: list[tuple[SqlScalar, SqlScalar]]) -> Path:
    """Write the old (key, value) pairs and where they go to a timestamped file."""
    table, key, col = column
    path = config.DATA_DIR / "db_backups" / f"restamp-{col}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"table": table, "key": key, "column": col, "old": old}),
                    encoding="utf-8")
    return path


def _undo(conn: sqlite3.Connection, path: Path, commit: bool) -> dict[str, int]:
    """Put back the stamps a restamp backup recorded. The first geo backup
    (restamp-geo-20261005-132800.json) is a bare list of [job_id, geo_mode]
    pairs."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"table": "jobs", "key": "job_id", "column": "geo_mode", "old": data}
    old = data["old"]
    print(f"  {path.name}: {len(old)} stamp(s) to restore to {data['table']}.{data['column']}")
    if commit:
        with store.batch(conn):
            conn.executemany(f"UPDATE {data['table']} SET {data['column']}=? "
                             f"WHERE {data['key']}=?", [(v, k) for k, v in old])
    print("  applied." if commit else "  preview: nothing written.")
    return {"restore": len(old)}
