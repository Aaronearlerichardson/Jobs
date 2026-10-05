"""Stored stamps recomputed after the rule that sets them changes: a job's
`geo_mode`, a company's `mission_tier`."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from src import config, store
from src.config import RuntimeTrack
from src.match.locality import geo_mode
from src.ops.maintenance import track_store


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
        def fresh() -> Iterable[tuple[Any, Any, Any, str]]:
            for r in conn.execute(
                    "SELECT job_id, company_name, title, location, description, geo_mode "
                    "FROM jobs WHERE closed_at IS NULL"):
                yield (r["job_id"], r["geo_mode"], geo_mode(r["location"], r["description"]),
                       f"{r['company_name'] or ''} | {(r['title'] or '')[:50]} | "
                       f"{(r['location'] or '')[:50]}")
        return _restamp(conn, commit, undo, ("jobs", "job_id", "geo_mode"), fresh())


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
        `active` is left alone: it comes from the tier at sourcing time and
        is a human-editable switch, so a re-tier never parks or wakes a
        board. New scores are reconciled when written
        (api.score_company_mission); this brings the stored ones in line.
    """
    with track_store(t, conn) as conn:
        def fresh() -> Iterable[tuple[Any, Any, Any, str]]:
            for r in conn.execute(
                    "SELECT id, name, mission_tier, mission_score FROM companies "
                    "WHERE mission_score IS NOT NULL"):
                yield (r["id"], r["mission_tier"],
                       config.tier_for_score(r["mission_score"], r["mission_tier"]),
                       f"{r['name']} | score {r['mission_score']:.2f}")
        return _restamp(conn, commit, undo, ("companies", "id", "mission_tier"), fresh())


def _restamp(conn: sqlite3.Connection, commit: bool, undo: str, column: tuple[str, str, str],
             fresh: Iterable[tuple[Any, Any, Any, str]]) -> dict[str, int]:
    """The shared preview/apply/undo over `column` = (table, key, column):
    `fresh` yields (key, stored value, recomputed value, sample line)."""
    if undo:
        return _undo(conn, Path(undo), commit)
    table, key, col = column
    changes: dict[str, list[tuple[Any, Any, Any, str]]] = defaultdict(list)
    n = 0
    for k, old, new, sample in fresh:
        n += 1
        if new != old:
            changes[f"{old} -> {new}"].append((k, old, new, sample))
    counts = {kind: len(rows) for kind, rows in changes.items()}
    print(f"  {n} row(s) read, {sum(counts.values())} would change")
    for kind, rows in sorted(changes.items(), key=lambda kv: -len(kv[1])):
        print(f"  {kind}  {len(rows)}")
        for *_, sample in rows[:5]:
            print(f"    {sample}")
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


def _backup(column: tuple[str, str, str], old: list[tuple[Any, Any]]) -> Path:
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
