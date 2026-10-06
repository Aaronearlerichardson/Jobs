"""The board directory: boards that hire in your locality, from a public dataset.

jobhive (kalil0321/ats-scrapers, MIT, regenerated daily) lists the live
postings of some 80,000 boards, one parquet file per platform plus a
`companies.parquet` of board names. This module reads it for the boards with
postings in your [locality] that the roster lacks, ranks them by how much
their posting titles read like the roster's mission-aligned employers (a
free pre-screen, `prescreen`), and hands the best to `dork.intake_boards`,
which validates, scores and queues them for review.

    directory_boards()   every local board in the directory, grouped
    title_vocab(conn)    the roster's title words, by mission log-odds
    prescreen(board, v)  a board's title words' mean log-odds
    import_boards()      the op behind `discover.py --import-boards`

Configured by [sources.board_directory]. `base_url` may be a local directory
of the same layout. The resolver's name index is `resolve.directory`.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from src import config, store
from src.ats import coords
from src.ats.board import BOARDS
from src.discovery.name_sources import blocked_keys
from src.discovery.resolve import directory
from src.discovery.resolve.directory import Detected, board_of, cache_path, is_url, locate, query
from src.match.gates import exclude_reason, is_technical_role
from src.match.locality import is_nc
from src.match.names import name_key
from src.net import http
from src.rows import BoardHit
from src.runstate import per_run

from .vocab import title_vocab, word_score, words

#: The roster statuses `classify` sorts a directory board into.
STATUSES = ("new", "alternate", "tracked", "blocked")


class DirectoryBoard(TypedDict):
    """One board with postings in the locality."""
    name: str
    ats: str
    handle: Any                 # a slug, or Workday's (tenant, pod, site)
    careers_url: str | None     # only for a platform whose handle is the URL
    nc_postings: int
    gate_passes: int            # of them, titles the local engine's gate keeps
    sample_titles: list[str]    # five, the gate's first
    sample_url: str
    title_words: set[str]       # the words of every local posting title (`words`)


def _key(found: Detected) -> tuple[Any, ...] | None:
    """The store's identity of a detected board (`store.board_key`)."""
    return store.board_key(coords.columns(found.ats, found.handle, found.careers_url))


# --------------------------------------------------------------------------- #
#  Reading the dataset                                                         #
# --------------------------------------------------------------------------- #

def _prefilter(substrings: Sequence[str], words: Sequence[str]) -> tuple[str, list[str]]:
    """SQL over a `location` column keeping the rows that may be local, and
    its parameters: a plain substring per term, and one regex for the terms
    that must stand as words. A superset of `is_nc`, which has the final say.

    >>> where, args = _prefilter(["Chapel Hill"], ["nc", "north carolina"])
    >>> where
    '(contains(lower(location), ?) OR regexp_matches(location, ?))'
    >>> args[0], args[1] == r"(?i)\\b(?:nc|north\\ carolina)\\b"
    ('chapel hill', True)
    >>> _prefilter([], [])
    ('false', [])
    """
    subs = [s.lower() for s in dict.fromkeys(substrings) if s]
    toks = [re.escape(w.lower()) for w in dict.fromkeys(words) if w]
    clauses = ["contains(lower(location), ?)"] * len(subs)
    if toks:
        clauses.append("regexp_matches(location, ?)")
    if not clauses:
        return "false", []
    args = subs + ([rf"(?i)\b(?:{'|'.join(toks)})\b"] if toks else [])
    return f"({' OR '.join(clauses)})", args


def _try_httpfs() -> bool:
    """Whether DuckDB's httpfs extension installs and loads here."""
    import duckdb
    con = duckdb.connect()
    try:
        con.install_extension("httpfs")
        con.load_extension("httpfs")
        return True
    except duckdb.Error:
        return False
    finally:
        con.close()


#: Whether httpfs loads, tried at the run's first remote read.
_HTTPFS = per_run(_try_httpfs)


def _fresh(path: Path) -> bool:
    """Whether the cached file is younger than `refresh_days`."""
    try:
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return age < timedelta(days=config.BOARD_DIRECTORY.refresh_days)


async def _cached(rel: str) -> Path | None:
    """The directory's file `rel` in the cache, downloaded when missing or
    older than `refresh_days`; a stale copy when the download fails, else
    None."""
    path = cache_path(rel)
    if _fresh(path):
        return path
    url = f"{config.BOARD_DIRECTORY.base_url.rstrip('/')}/{rel}"
    _status, r, err = await http.request("GET", url, f"board directory {rel}",
                                         timeout=(15.0, 120.0))
    if err or r is None:
        return path if path.exists() else None
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, r.content)
    return path


async def _read(rel: str, sql: str, args: Sequence[str] = ()) -> list[tuple[Any, ...]] | None:
    """`sql` over the directory's file `rel` (`?` is the file); None when it
    cannot be read. A remote file is read in place (httpfs), else through
    the cache."""
    src, remote = locate(rel), is_url(config.BOARD_DIRECTORY.base_url)
    if remote and not await asyncio.to_thread(_HTTPFS):
        cached = await _cached(rel)
        src, remote = (str(cached) if cached else ""), False
    import duckdb
    try:
        return await asyncio.to_thread(query, src, sql, args, remote) if src else None
    except duckdb.Error as e:
        print(f"    [!] board directory {rel}: {str(e).splitlines()[0][:100]}")
    if not remote:
        return None
    cached = await _cached(rel)         # the in-place read failed: try a download
    try:
        return await asyncio.to_thread(query, str(cached), sql, args, False) if cached else None
    except duckdb.Error:
        return None


# --------------------------------------------------------------------------- #
#  Grouping postings into boards                                               #
# --------------------------------------------------------------------------- #

class Scan:
    """The local postings of the directory, grouped.

    >>> scan = Scan()
    >>> track = config.track_for_engine("local")
    >>> scan.add([("acmebio", "https://boards.greenhouse.io/acmebio/jobs/1",
    ...            "Remote", "Engineer")], "greenhouse", {}, track)
    >>> scan.read, len(scan.boards)
    (1, 0)
    """

    def __init__(self) -> None:
        self.boards: dict[tuple[Any, ...], DirectoryBoard] = {}
        self.posts: Counter[str] = Counter()        # platform -> local postings
        self.leads: defaultdict[str, set[Any]] = defaultdict(set)  # unsupported platform -> boards
        self.read = 0                               # rows past the prefilter
        self.gates: dict[tuple[str, str | None], bool] = {}   # (track id, title) -> the title gate's verdict

    def add(self, rows: Iterable[tuple[Any, ...]], label: str,
            names: dict[tuple[Any, ...], str], track: config.RuntimeTrack) -> None:
        """Fold in `rows` ((company, url, location, title)) of the file
        `label`: the local ones, by board."""
        for company, url, location, title in rows:
            self.read += 1
            if not is_nc(location):
                continue
            found = board_of(url, label, company or "")
            if not found:
                # No spec for the platform: a board the crawl cannot read.
                # With one, a board the URL does not identify.
                if label in BOARDS:
                    self.posts[f"?{label}"] += 1
                else:
                    self.posts[label] += 1
                    self.leads[label].add(company)
                continue
            self.posts[found.ats] += 1
            if found.kind == "lead":
                self.leads[found.ats].add(found.handle)
                continue
            key = _key(found)
            if not key:
                continue
            board = self.boards.get(key)
            if board is None:
                # Unnamed in companies.parquet: its slug titled (as dork
                # names one), else the posting's company.
                name = names.get(key) or coords.slug_title(coords.columns(
                    found.ats, found.handle, found.careers_url)) or company or ""
                board = self.boards[key] = DirectoryBoard(
                    name=name, ats=found.ats,
                    handle=found.handle, careers_url=found.careers_url, nc_postings=0,
                    gate_passes=0, sample_titles=[], sample_url=url or "",
                    title_words=set())
            board["nc_postings"] += 1
            board["title_words"] |= words(title)
            gate = self.gates.get((track.id, title))
            if gate is None:
                gate = self.gates[track.id, title] = bool(title) and is_technical_role(
                    title, track) and not exclude_reason(title, "", track_id=track.id)
            samples = board["sample_titles"]
            if gate:
                board["gate_passes"] += 1
                if board["gate_passes"] == 1:
                    samples.clear()
            if (gate or not board["gate_passes"]) and len(samples) < 5 and title:
                samples.append(title)


async def _files() -> list[str]:
    """The platform files to read: the configured `files`, else every one
    the directory's manifest lists. [] when there is no readable manifest."""
    cfg = config.BOARD_DIRECTORY
    if cfg.files:
        return list(cfg.files)
    if is_url(cfg.base_url):
        manifest = await http.get_json(locate("manifest.json"),
                                       "board directory manifest", {})
    else:
        try:
            manifest = json.loads(await asyncio.to_thread(
                Path(locate("manifest.json")).read_text, "utf-8"))
        except (OSError, ValueError):
            manifest = {}
    return list((manifest or {}).get("by_ats", {}))


def _names() -> dict[tuple[Any, ...], str]:
    """The directory's board names by board key (`_key`), the first listed
    winning."""
    names: dict[tuple[Any, ...], str] = {}
    for found, name in directory.index().named:
        if key := _key(found):
            names.setdefault(key, name)
    return names


async def _scan() -> Scan:
    """One pass over the configured files for local postings."""
    cfg = config.BOARD_DIRECTORY
    where, args = _prefilter(config.LOCALITY_SUBSTRINGS,
                             [*config.LOCALITY_WORD_TOKENS, *config.LOCALITY_STATE_SUFFIX])
    sql = f"SELECT company, url, location, title FROM read_parquet(?) WHERE location IS NOT NULL AND {where}"
    if is_url(cfg.base_url):
        await _cached("companies.parquet")       # warm, for lookup_name
    names = await asyncio.to_thread(_names)
    track = config.track_for_engine("local")
    scan = Scan()
    for label in await _files():
        rows = await _read(f"{label}/jobs.parquet", sql, args)
        if rows is None:
            continue
        await asyncio.to_thread(scan.add, rows, label, names, track)
        print(f"  {label:16} {len(rows):7} rows past the prefilter")
    return scan


async def directory_boards() -> list[DirectoryBoard]:
    """Every board in the directory with postings in your locality, over
    the configured platforms, the most title-gate passes first, then the
    most local postings (`import_boards` adds the roster's pre-screen). A
    board on a platform with no fetcher is left out."""
    return _ranked((await _scan()).boards.values())


def prescreen(board: DirectoryBoard, vocab: dict[str, float], shrink: int = 3) -> float:
    """How much a board's posting titles read like the mission-aligned
    employers' (`title_vocab`): the `word_score` of its title words.

    >>> b = DirectoryBoard(name="A", ats="x", handle="a", careers_url=None, nc_postings=2,
    ...                    gate_passes=1, sample_titles=[], sample_url="",
    ...                    title_words={"clinical", "engineer", "unseen"})
    >>> round(prescreen(b, {"clinical": 1.2, "engineer": 0.0, "store": -1.1}), 2)
    0.2
    """
    return word_score(board["title_words"], vocab, shrink)


def _ranked(boards: Iterable[DirectoryBoard], vocab: dict[str, float] | None = None
            ) -> list[DirectoryBoard]:
    """`boards`, the highest pre-screen first when there is a `vocab`; then
    (and without one) the most gate passes, then the most postings.

    >>> a = DirectoryBoard(name="A", ats="x", handle="a", careers_url=None, nc_postings=9,
    ...                    gate_passes=1, sample_titles=[], sample_url="", title_words={"store"})
    >>> b = {**a, "name": "B", "nc_postings": 2, "gate_passes": 4, "title_words": {"clinical"}}
    >>> [r["name"] for r in _ranked([a, b])]
    ['B', 'A']
    >>> [r["name"] for r in _ranked([b, a], {"store": 1.0, "clinical": -1.0})]
    ['A', 'B']
    """
    return sorted(boards, key=lambda b: (-prescreen(b, vocab) if vocab else 0,
                                         -b["gate_passes"], -b["nc_postings"], b["name"]))


# --------------------------------------------------------------------------- #
#  Against the roster                                                          #
# --------------------------------------------------------------------------- #

def classify(conn: sqlite3.Connection, boards: Iterable[DirectoryBoard]
             ) -> dict[str, list[DirectoryBoard]]:
    """`boards` sorted by the roster (`STATUSES`).

    A board's employer is the roster row of the same name, else the one whose
    careers host is the posting's; the board takes that row's name and
    `store.plan_board` decides, as the write will:

    * tracked    the roster has this board
    * alternate  a board of an employer with a mission verdict: a sibling of
                 its live board or the replacement of its dead one
                 (import_boards writes it with that verdict, no score)
    * blocked    a name a reviewer rejected, or the profile blocks
    * new        the rest (no such employer, or none with a verdict yet, e.g.
                 a name held only as a miss): it takes a mission score

    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_company(conn, {"name": "Acme Bio", "ats": "lever", "slug": "acme",
    ...                                 "mission_tier": "core", "mission_score": 0.9})
    >>> _ = store.record_miss(conn, "Zeta Labs", "no-board-found")
    >>> _ = store.block_name(conn, "Junk Co")
    >>> def board(name, ats, handle):
    ...     return DirectoryBoard(name=name, ats=ats, handle=handle, careers_url=None,
    ...                           nc_postings=1, gate_passes=1, sample_titles=[], sample_url="",
    ...                           title_words=set())
    >>> got = classify(conn, [board("Acme Bio", "lever", "acme"),
    ...                       board("ACME BIO", "ashby", "acmebio"),
    ...                       board("Zeta Labs Inc", "ashby", "zeta"),
    ...                       board("Zeta Labs", "ashby", "zeta"),
    ...                       board("Junk Co", "lever", "junk"),
    ...                       board("Other", "lever", "other")])
    >>> {k: [b["name"] for b in v] for k, v in got.items()}
    {'new': ['Zeta Labs Inc', 'Zeta Labs', 'Other'], 'alternate': ['Acme Bio'], 'tracked': ['Acme Bio'], 'blocked': ['Junk Co']}
    """
    by_name = {name_key(c["name"]): c for c in store.get_companies(conn, active_only=False)}
    blocked = blocked_keys(conn)
    out: dict[str, list[DirectoryBoard]] = {s: [] for s in STATUSES}
    for b in boards:
        nk = name_key(b["name"])
        row = by_name.get(nk) or store.company_by_host(conn, b["sample_url"])
        if row:
            b = {**b, "name": row["name"]}
        plan = store.plan_board(conn, coords.columns(b["ats"], b["handle"], b["careers_url"],
                                                     name=b["name"]))
        if plan.action == "update":
            out["tracked"].append(b)
        elif nk in blocked:
            out["blocked"].append(b)
        else:
            out["new" if plan.needs_score else "alternate"].append(b)
    return out


# --------------------------------------------------------------------------- #
#  The op                                                                      #
# --------------------------------------------------------------------------- #

def _summary(scan: Scan, groups: dict[str, list[DirectoryBoard]], eligible: int) -> list[str]:
    """The dry-run table: per platform the local postings, boards found and
    where they fall against the roster."""
    per: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for s, bs in groups.items():
        for b in bs:
            per[b["ats"]][s] += 1
    lines = [f"  {'platform':16} {'postings':>8} {'boards':>7} {'new':>6} {'alt':>5} "
             f"{'tracked':>7} {'unsupported':>11}"]
    for ats in sorted(scan.posts, key=lambda a: -scan.posts[a]):
        c = per[ats]
        lines.append(f"  {ats:16} {scan.posts[ats]:8} {sum(c.values()):7} {c['new']:6} "
                     f"{c['alternate']:5} {c['tracked']:7} {len(scan.leads.get(ats, ())):11}")
    total = {s: len(groups[s]) for s in STATUSES}
    lines.append(f"  {scan.read} rows past the prefilter; {sum(total.values())} local boards: "
                 f"{total['new']} new ({eligible} pass the title gate), "
                 f"{total['alternate']} alternate board of a tracked employer, "
                 f"{total['tracked']} tracked, {total['blocked']} blocked; "
                 f"{sum(map(len, scan.leads.values()))} on platforms with no fetcher")
    return lines


def _write_report(groups: dict[str, list[DirectoryBoard]], vocab: dict[str, float]) -> Path:
    """job_reports/import_boards_<date>.csv: every board the roster lacks,
    best first, with its pre-screen."""
    path = config.REPORT_DIR / f"import_boards_{date.today().isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "ats", "slug", "nc_postings", "title_gate_passes",
                    "sample_titles", "sample_url", "status", "prescreen"])
        for s in ("new", "alternate"):
            for b in _ranked(groups[s], vocab):
                w.writerow([b["name"], b["ats"],
                            coords.slug_text(b["ats"], b["handle"]) or b["careers_url"] or "",
                            b["nc_postings"], b["gate_passes"],
                            " | ".join(b["sample_titles"]), b["sample_url"], s,
                            f"{prescreen(b, vocab):.3f}"])
    return path


def _hits(boards: Iterable[DirectoryBoard]) -> list[BoardHit]:
    """`boards` as the resolver-shaped hits intake_boards reads."""
    return [{"name": b["name"], "ats": b["ats"], "slug": b["handle"],
             "careers_url": b["careers_url"]} for b in boards]


async def import_boards(apply: bool = False, limit: int | None = None) -> dict[str, int]:
    """Boards in the directory with local postings that the roster lacks: a
    dry run reports them per platform and writes the report CSV; `apply`
    validates (a live local posting), mission-scores and queues the best for
    review, the highest pre-screen first, at most `limit` (and
    [sources.board_directory] `max_scored_per_run`). Returns the counts.

    Notes:
        A platform with no fetcher is only counted, since the crawl could not
        read its boards. The pre-screen (`title_vocab`) spends the capped
        scoring budget on boards that read like already-judged mission
        employers: backtested on the 2026-10-05 directory it put 75 of the
        first 100 there against 46 for gate passes then postings, and the cap
        binds before a score floor would.
    """
    cfg = config.BOARD_DIRECTORY
    scan = await _scan()
    async with store.Writer() as db:
        vocab = await db.run(title_vocab)
        groups = await db.run(classify, _ranked(scan.boards.values(), vocab))
    eligible = [b for b in groups["new"] if b["gate_passes"] >= cfg.min_gate_titles]
    print("\n".join(_summary(scan, groups, len(eligible))))
    print(f"  report: {_write_report(groups, vocab)}")
    counts = {s: len(groups[s]) for s in STATUSES} | {"eligible": len(eligible), "added": 0, "siblings": 0}
    if not apply:
        print("  dry run: nothing written (--apply to validate, score and queue)")
        return counts
    from src.discovery.dork import intake_boards      # dork imports the resolver, which may import this
    cap = min(cfg.max_scored_per_run, limit) if limit else cfg.max_scored_per_run
    counts["added"], _ = await intake_boards(_hits(eligible), "board_directory", require_live=True, limit=cap)
    print(f"  {counts['added']} board(s) queued for review")
    # An alternate board needs no verdict: once a live local posting confirms
    # it, it joins its employer or replaces its dead board (store.add_board).
    counts["siblings"], _ = await intake_boards(_hits(groups["alternate"]), "board_directory",
                                                require_live=True)
    print(f"  {counts['siblings']} alternate board(s) added to their employers")
    return counts
