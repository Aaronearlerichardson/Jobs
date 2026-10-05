"""Stored `geo_mode` stamps, recomputed after the rule that sets them changes."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

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
    db_backups/restamp-geo-<time>.json under the data dir, then updates
    them in one transaction. `undo=<that file>` puts the old stamps back
    (again a preview until `commit`).

    >>> conn = store.connect(":memory:")
    >>> for jid, loc in [("a", "Alameda"), ("b", "United States"), ("c", "Durham, NC")]:
    ...     _ = store.upsert_job(conn, {"job_id": jid, "title": "T", "location": loc,
    ...                                 "company_name": "Co",
    ...                                 "description": "We are a distributed team."})
    >>> _ = conn.execute("UPDATE jobs SET geo_mode='remote' WHERE job_id='a'")
    >>> restamp_geo(conn=conn)
      3 open row(s) read, 3 would change
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
        if undo:
            return _undo(conn, Path(undo), commit)
        changes: dict[str, list[tuple[str, str | None, str | None, str, str, str]]] = defaultdict(list)
        n = 0
        for r in conn.execute(
                "SELECT job_id, company_name, title, location, description, geo_mode "
                "FROM jobs WHERE closed_at IS NULL"):
            n += 1
            new = geo_mode(r["location"], r["description"])
            if new != r["geo_mode"]:
                changes[f"{r['geo_mode']} -> {new}"].append(
                    (r["job_id"], r["geo_mode"], new, r["title"] or "", r["location"] or "",
                     r["company_name"] or ""))
        counts = {k: len(v) for k, v in changes.items()}
        print(f"  {n} open row(s) read, {sum(counts.values())} would change")
        for kind, rows in sorted(changes.items(), key=lambda kv: -len(kv[1])):
            print(f"  {kind}  {len(rows)}")
            for _, _, _, title, loc, company in rows[:5]:
                print(f"    {company} | {title[:50]} | {loc[:50]}")
        if commit and changes:
            moved = [(j, old, new) for rows in changes.values() for j, old, new, *_ in rows]
            backup = _backup([(j, old) for j, old, _ in moved])
            with store.batch(conn):
                conn.executemany("UPDATE jobs SET geo_mode=? WHERE job_id=?",
                                 [(new, j) for j, _, new in moved])
            print(f"  applied. old stamps saved to {backup}; restore with undo={backup}")
        else:
            print("  applied." if commit else "  preview: nothing written.")
    return counts


def _backup(old: list[tuple[str, str | None]]) -> Path:
    """Write [job_id, old geo_mode] pairs to a timestamped file."""
    path = config.DATA_DIR / "db_backups" / f"restamp-geo-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old), encoding="utf-8")
    return path


def _undo(conn: sqlite3.Connection, path: Path, commit: bool) -> dict[str, int]:
    """Put back the stamps a restamp_geo backup recorded."""
    old: list[tuple[str, str | None]] = json.loads(path.read_text(encoding="utf-8"))
    print(f"  {path.name}: {len(old)} stamp(s) to restore")
    if commit:
        with store.batch(conn):
            conn.executemany("UPDATE jobs SET geo_mode=? WHERE job_id=?",
                             [(v, j) for j, v in old])
    print("  applied." if commit else "  preview: nothing written.")
    return {"restore": len(old)}
