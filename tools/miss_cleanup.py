#!/usr/bin/env python3
"""Propose a cleanup of the roster's miss rows, as SQL nobody has run.

    python tools/miss_cleanup.py --out proposals.sql
    python tools/miss_cleanup.py --trial domain_trial.json --hints hints.json
    python tools/miss_cleanup.py --offline          # reuse the Wikidata cache

Every reresolve pays again for names that will never resolve: pasted
headings, duplicates of a tracked company under another spelling, acquired
or dissolved companies. This reads the store read-only and writes a SQL
script, one reason line per proposal, in two tiers:

  apply   junk names (`match.names.junk_name_reason` plus a few heading
          shapes), spellings of one company (a name equal to a tracked
          company's, or to a parenthetical alias of it), and companies
          Wikidata calls replaced (P1366) or dissolved (P576).
  review  same shapes, weaker evidence: a subsidiary of a tracked company
          (P749), a name with no domain and no Wikidata item. Emitted as
          comments.

A merge repoints the loser's jobs, folds its tags into the survivor and
deletes the row; a prune deletes the row; both blocklist the name, as
`store.reject_company` does. Rows holding jobs or notes are never pruned,
and a name a `--trial` file found a board for is never touched.

The store is opened mode=ro. Wikidata answers are cached (`--cache`) so a
rerun is free; the API is read one request at a time, `--delay` apart.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, runstate                               # noqa: E402
from src.match.names import (                                  # noqa: E402
    junk_name_reason, name_key, name_words, strip_parentheticals, strip_suffixes)
from src.net import http                                       # noqa: E402
from tools._harness import open_ro                             # noqa: E402

MAX_TRIES = 4

# Words a legal form or a connective adds to a name: "Eli Lilly and Company"
# and "Eli Lilly" are one company.
LEGAL = frozenset({"inc", "incorporated", "corp", "corporation", "ltd", "llc", "plc",
                   "co", "company", "gmbh", "the", "and", "of"})

# Wikidata instance-of labels (or a description) that make an item an
# organisation, so a person or a place sharing the name is not mistaken for it.
ORG_RE = re.compile(
    r"compan|business|enterprise|corporation|organi[sz]ation|firm|startup|brand|"
    r"manufactur|laborator|institute|universit|hospital|non-?profit|foundation|"
    r"subsidiary|pharma|biotech|conglomerate|agency|clinic|society|association|"
    r"provider|developer|supplier|research", re.I)

# Descriptions of items that merely discuss an organisation.
NOT_ORG_RE = re.compile(r"article|disambiguation|journal|news|episode|supermarket", re.I)

# Words that alone make a pasted fragment a heading, not an employer, beyond
# what junk_name_reason knows: pronoun phrases ("Who We Are") and a heading
# word with a modifier ("Minimum Requirements").
FUNCTION_WORDS = frozenset({
    "who", "we", "you", "are", "what", "our", "your", "i", "am", "is", "this",
    "that", "they", "it", "us", "how", "why", "when", "where", "to", "be"})
HEADING_MODIFIERS = frozenset({
    "minimum", "basic", "additional", "desired", "key", "core", "general",
    "nice", "plus", "bonus", "ideal", "essential", "other", "physical",
    "working", "work", "environment", "conditions", "equal", "opportunity"})
HEADING_NOUNS = frozenset({
    "requirements", "requirement", "qualifications", "qualification",
    "responsibilities", "skills", "duties", "benefits", "perks", "conditions",
    "environment", "employer", "opportunity"})

Tier = Literal["apply", "review"]
Kind = Literal["merge", "prune"]


class Hit(BaseModel):
    """One wbsearchentities result."""
    id: str
    label: str = ""
    description: str = ""
    matched: str = ""


class Item(BaseModel):
    """The Wikidata claims this tool reads about one item."""
    id: str
    label: str = ""
    description: str = ""
    instance_of: list[str] = []
    replaced_by: list[str] = []
    parents: list[str] = []
    dissolved: str | None = None


class Cache(BaseModel):
    """Everything fetched from Wikidata, keyed so a rerun asks for nothing."""
    search: dict[str, list[Hit]] = {}
    items: dict[str, Item | None] = {}
    labels: dict[str, str] = {}


class Hints(BaseModel):
    """The user's judgement calls the evidence cannot make: name -> target,
    name -> reason, and names to leave alone."""
    merge: dict[str, str] = {}
    prune: dict[str, str] = {}
    keep: list[str] = []


class Proposal(BaseModel):
    kind: Kind
    tier: Tier
    basis: str
    name: str
    target: str | None = None
    reason: str
    block: bool = True


class Row(BaseModel):
    id: int
    name: str
    ats: str | None = None
    active: int | None = None
    miss_reason: str | None = None
    tags: str | None = None
    notes: str | None = None
    jobs: int = 0


# --------------------------------------------------------------------------- #
#  Names                                                                       #
# --------------------------------------------------------------------------- #

def canon(name: str | None) -> str:
    """The name's words minus legal forms and connectives, joined.

    >>> canon("Eli Lilly and Company"), canon("Merck & Co.")
    ('elililly', 'merck')
    >>> canon("Acme (NC office)")
    'acme'
    """
    return "".join(w for w in name_words(name) if w not in LEGAL)


def name_aliases(name: str) -> set[str]:
    """Keys one company's spellings share: the canon form and each
    parenthetical's own canon form.

    >>> sorted(name_aliases("Becton Dickinson (BD)"))
    ['bd', 'bectondickinson']
    >>> sorted(name_aliases("Plain Co"))
    ['plain']
    """
    inner = re.findall(r"[\(\[]([^)\]]*)[\)\]]?", name)
    return {k for k in (canon(name), *(canon(i) for i in inner)) if k}


def stripped_keys(name: str) -> set[str]:
    """Looser keys for matching a name to a Wikidata label: the canon form
    and the form without field words ("Therapeutics").

    >>> sorted(stripped_keys("Cempra Pharmaceuticals (United States)"))
    ['cempra', 'cemprapharmaceuticals']
    """
    return {k for k in (canon(name), canon(strip_suffixes(name))) if k}


def heading_reason(name: str) -> str:
    """Why `name` reads as a pasted heading though `junk_name_reason` lets it
    through, or ''.

    >>> heading_reason("Who We Are"), heading_reason("Minimum Requirements")
    ('heading-phrase', 'heading-phrase')
    >>> heading_reason("… more")
    'listing-chrome'
    >>> heading_reason("Who Cares Labs"), heading_reason("Acme Requirements Inc")
    ('', '')
    """
    words = name_words(name)
    if words and all(w in FUNCTION_WORDS for w in words):
        return "heading-phrase"
    if (len(words) == 2 and words[0] in HEADING_MODIFIERS
            and words[1] in HEADING_NOUNS):
        return "heading-phrase"
    if re.match(r"\s*(?:…|\.\.\.)", name):
        return "listing-chrome"
    if "�" in name:
        return "garbled"
    return ""


def junk_reason(name: str) -> str:
    """The first reason `name` is not an employer, or ''.

    The length rule is left out: it fires on real names ("The University of
    X at Y"), and a prune cannot afford that.

    >>> junk_reason("Who You Are"), junk_reason("Acme Corp")
    ('heading-phrase', '')
    >>> junk_reason("The University of North Carolina at Chapel Hill")
    ''
    """
    reason = junk_name_reason(name)
    return heading_reason(name) if reason == "too-long" else reason or heading_reason(name)


def search_term(name: str) -> str:
    """What to ask Wikidata for: the name without parentheticals or a
    trailing legal form.

    >>> search_term("Cempra Pharmaceuticals (NC), Inc.")
    'Cempra Pharmaceuticals'
    """
    s = strip_parentheticals(name).strip(" ,.")
    return re.sub(r",?\s+(?:inc|llc|ltd|corp|corporation|co|plc)\.?$", "", s,
                  flags=re.I).strip(" ,.")


# --------------------------------------------------------------------------- #
#  Wikidata                                                                    #
# --------------------------------------------------------------------------- #

def _ids(claims: dict[str, Any], prop: str) -> list[str]:
    """Item ids a property points at, skipping deprecated statements."""
    out: list[str] = []
    for c in claims.get(prop, []):
        if c.get("rank") == "deprecated":
            continue
        v = (c.get("mainsnak", {}).get("datavalue") or {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            out.append(str(v["id"]))
    return out


def parse_item(qid: str, ent: dict[str, Any]) -> Item | None:
    """An `Item` from a wbgetentities entity, None when it is missing.

    >>> ent = {"labels": {"en": {"value": "Old Co"}},
    ...        "claims": {"P1366": [{"mainsnak": {"datavalue": {"value": {"id": "Q2"}}}}],
    ...                   "P576": [{"mainsnak": {"datavalue": {"value": {"time": "+2017-03-01T00:00:00Z"}}}}]}}
    >>> it = parse_item("Q1", ent)
    >>> it.replaced_by, it.dissolved
    (['Q2'], '2017')
    >>> parse_item("Q9", {"missing": ""}) is None
    True
    """
    if "missing" in ent:
        return None
    claims = ent.get("claims", {})
    when = None
    for c in claims.get("P576", []):
        t = ((c.get("mainsnak", {}).get("datavalue") or {}).get("value") or {}).get("time")
        if t:
            when = str(t).lstrip("+")[:4]
            break
    return Item(
        id=qid,
        label=ent.get("labels", {}).get("en", {}).get("value", ""),
        description=ent.get("descriptions", {}).get("en", {}).get("value", ""),
        instance_of=_ids(claims, "P31"), replaced_by=_ids(claims, "P1366"),
        parents=_ids(claims, "P749"), dissolved=when)


class Wikidata:
    """Cached, paced reads of the Wikidata API."""

    def __init__(self, cache_path: Path, delay: float, offline: bool) -> None:
        self.path, self.delay, self.offline = cache_path, delay, offline
        self.cache = (Cache.model_validate_json(cache_path.read_text(encoding="utf-8"))
                      if cache_path.exists() else Cache())
        self.calls = 0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.cache.model_dump_json(indent=1), encoding="utf-8")

    async def _call(self, params: dict[str, str]) -> dict[str, Any] | None:
        """One API read, or None when offline or it failed (not cached, so
        a rerun retries it). A 429 waits out its Retry-After and retries."""
        if self.offline:
            return None
        for attempt in range(1, MAX_TRIES + 1):
            await asyncio.sleep(self.delay)
            self.calls += 1
            # The API's own etiquette (one request at a time, backing off
            # on 429) stands in for robots.txt, which disallows /w/ to
            # every crawler, the API included.
            status, r, err = await http.request(
                "GET", config.WIKIDATA_API, "wikidata" if attempt == MAX_TRIES else None,
                params={**params, "format": "json"}, polite=False)
            if status == 429 and r is not None:
                await asyncio.sleep(float(r.headers.get("Retry-After") or 20) + attempt)
                continue
            if err or r is None:
                return None
            try:
                data = r.json()
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
        return None

    async def search(self, term: str) -> list[Hit]:
        if term in self.cache.search:
            return self.cache.search[term]
        data = await self._call({"action": "wbsearchentities", "search": term,
                                 "language": "en", "type": "item", "limit": "5"})
        if data is None:
            return []
        hits = [Hit(id=h["id"], label=h.get("label", ""),
                    description=h.get("description", ""),
                    matched=h.get("match", {}).get("text", ""))
                for h in data.get("search", [])]
        self.cache.search[term] = hits
        return hits

    async def _entities(self, want: Sequence[str], props: str
                        ) -> AsyncIterator[tuple[str, dict[str, Any] | None]]:
        """(qid, entity or None) for each of `want`, read 50 at a time; a
        failed batch yields nothing."""
        for i in range(0, len(want), 50):
            batch = want[i:i + 50]
            data = await self._call({"action": "wbgetentities", "ids": "|".join(batch),
                                     "props": props, "languages": "en"})
            if data is None:
                continue
            for q in batch:
                yield q, data.get("entities", {}).get(q)

    async def items(self, qids: Iterable[str]) -> dict[str, Item | None]:
        """The items for `qids`."""
        qids = list(dict.fromkeys(qids))
        async for q, ent in self._entities([q for q in qids if q not in self.cache.items],
                                           "claims|labels|descriptions"):
            if ent is not None:
                self.cache.items[q] = parse_item(q, ent)
        return {q: self.cache.items.get(q) for q in qids}

    async def label_of(self, qids: Iterable[str]) -> dict[str, str]:
        """English labels for `qids`."""
        qids = list(dict.fromkeys(qids))
        async for q, ent in self._entities([q for q in qids if q not in self.cache.labels],
                                           "labels"):
            self.cache.labels[q] = (ent or {}).get("labels", {}).get("en", {}).get("value", "")
        return {q: self.cache.labels.get(q, "") for q in qids}


async def wikidata_items(wd: Wikidata, names: Sequence[str]
                         ) -> dict[str, list[Item]]:
    """For each name the organisation items whose label or matched alias is
    the name (field words aside); empty when Wikidata knows no such company.
    """
    hits_by_name: dict[str, list[Hit]] = {}
    for n in names:
        term = search_term(n)
        hits_by_name[n] = [h for h in await wd.search(term) if
                           stripped_keys(n) & (stripped_keys(h.label) | stripped_keys(h.matched))]
    items = await wd.items(h.id for hs in hits_by_name.values() for h in hs)
    for _ in range(4):    # successor and parent items, a hop at a time
        await wd.items(q for it in wd.cache.items.values() if it
                       for q in it.replaced_by + it.parents)
    labels = await wd.label_of(q for it in items.values() if it for q in it.instance_of)
    out: dict[str, list[Item]] = {}
    for n, hs in hits_by_name.items():
        found = []
        for h in hs:
            it = items.get(h.id)
            if it and _is_org(it, [labels.get(q, "") for q in it.instance_of]):
                found.append(it)
        # "Biogen (Netherlands)" is a regional entity of "Biogen": the
        # unparenthesised label, when there is one, is the company.
        plain = [it for it in found if "(" not in it.label]
        out[n] = plain or found
    return out


def _is_org(item: Item, instance_labels: Sequence[str]) -> bool:
    """True when the item reads as an organisation (not a person or place).

    >>> _is_org(Item(id="Q1", description="company in Chapel Hill"), [])
    True
    >>> _is_org(Item(id="Q2", description="American actor"), ["human"])
    False
    >>> _is_org(Item(id="Q3", description="encyclopedic article about a company"), [])
    False
    """
    if NOT_ORG_RE.search(item.description):
        return False
    return bool(ORG_RE.search(item.description)
                or any(ORG_RE.search(label) for label in instance_labels))


# --------------------------------------------------------------------------- #
#  Decisions                                                                   #
# --------------------------------------------------------------------------- #

def merge_groups(rows: Sequence[Row]) -> list[tuple[Row, list[Row]]]:
    """(survivor, losers) for each set of rows that share a spelling key and
    include a miss. A tracked row (no miss_reason) survives, a boarded and
    active one first; among misses the shortest name does.

    >>> rows = [Row(id=1, name="BD", ats="workday", active=1),
    ...         Row(id=2, name="Becton Dickinson (BD)", miss_reason="no-board-found"),
    ...         Row(id=3, name="Becton Dickinson", miss_reason="no-board-found"),
    ...         Row(id=4, name="Other", miss_reason="no-board-found")]
    >>> [(s.name, [l.name for l in ls]) for s, ls in merge_groups(rows)]
    [('BD', ['Becton Dickinson', 'Becton Dickinson (BD)'])]
    """
    parent = {r.id: r.id for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first: dict[str, int] = {}
    for r in rows:
        for k in name_aliases(r.name):
            if k in first:
                parent[find(r.id)] = find(first[k])
            else:
                first[k] = r.id
    groups: dict[int, list[Row]] = defaultdict(list)
    for r in rows:
        groups[find(r.id)].append(r)
    out = []
    for g in groups.values():
        if len(g) < 2 or not any(r.miss_reason for r in g):
            continue
        g.sort(key=lambda r: (bool(r.miss_reason), not r.ats, not r.active,
                              len(r.name), r.id))
        out.append((g[0], [r for r in g[1:] if r.miss_reason]))
    return [(s, ls) for s, ls in out if ls]


class Tracked:
    """The tracked companies by spelling key, a boarded active one winning a
    shared key."""

    def __init__(self, rows: Sequence[Row]) -> None:
        self.index: dict[str, Row] = {}
        for r in sorted(rows, key=lambda r: (not r.ats, not r.active, r.id), reverse=True):
            for k in name_aliases(r.name):
                self.index[k] = r

    def named(self, label: str) -> Row | None:
        """The tracked company a Wikidata label names, if any.

        >>> t = Tracked([Row(id=1, name="Biogen", ats="workday", active=1)])
        >>> t.named("Biogen Inc.").name, t.named("Other")
        ('Biogen', None)
        """
        for k in stripped_keys(label):
            if k in self.index:
                return self.index[k]
        return None


def successor(wd: Wikidata, item: Item) -> Item:
    """The last item of a P1366 chain, at most four hops, cached items only."""
    seen = {item.id}
    for _ in range(4):
        nxt = next((wd.cache.items[q] for q in item.replaced_by
                    if q not in seen and wd.cache.items.get(q)), None)
        if nxt is None:
            break
        seen.add(nxt.id)
        item = nxt
    return item


def judge_item(d: Tracked, wd: Wikidata, row: Row, found: list[Item]) -> Proposal | None:
    """The proposal Wikidata supports for `row`, if any. Several candidate
    items must all lead to the same one, else the name is ambiguous.
    """
    props = [judge_one(d, wd, row, it) for it in found]
    first = props[0] if props else None
    if first is None or any(p is None or (p.kind, p.target) != (first.kind, first.target)
                            for p in props):
        return None
    return first


def judge_one(d: Tracked, wd: Wikidata, row: Row, item: Item) -> Proposal | None:
    """The proposal one Wikidata item supports for `row`.

    A replaced company (P1366) merges into the tracked successor or is pruned
    naming it; one that is only dissolved (P576) is a review-tier prune,
    since the item may be a namesake; a unit of a tracked company (P749) is a
    review-tier merge.

    >>> t = Tracked([Row(id=1, name="Biogen", ats="workday", active=1)])
    >>> p = judge_one(t, Wikidata(Path("x.json"), 0, True),
    ...               Row(id=2, name="Biogen Idec", miss_reason="no-board-found"),
    ...               Item(id="Q1", label="Biogen"))
    >>> (p.kind, p.target, p.tier)
    ('merge', 'Biogen', 'apply')
    """
    link = f"Wikidata {item.id}"
    if item.replaced_by:
        final = successor(wd, item)
        mine = d.named(final.label) if final.label else None
        if mine:
            return Proposal(kind="merge", tier="apply", basis="wikidata-successor",
                            name=row.name, target=mine.name,
                            reason=f"{link} replaced by '{final.label}' (P1366)")
        return Proposal(kind="prune", tier="apply", basis="wikidata-successor",
                        name=row.name,
                        reason=f"{link} replaced by '{final.label or final.id}' (P1366), not tracked")
    own = d.named(item.label)
    if own:
        return Proposal(kind="merge", tier="apply", basis="wikidata-label",
                        name=row.name, target=own.name,
                        reason=f"{link} is '{item.label}', a tracked company")
    if item.dissolved:
        return Proposal(kind="prune", tier="review", basis="wikidata-dissolved",
                        name=row.name,
                        reason=f"{link} dissolved {item.dissolved} (P576); "
                               "check it is not a namesake")
    for q in item.parents:
        parent = wd.cache.items.get(q)
        mine = d.named(parent.label) if parent and parent.label else None
        if mine:
            return Proposal(kind="merge", tier="review", basis="wikidata-parent",
                            name=row.name, target=mine.name,
                            reason=f"{link} is a unit of '{mine.name}' (P749); "
                                   "its jobs may list on the parent's board")
    return None


# --------------------------------------------------------------------------- #
#  SQL                                                                         #
# --------------------------------------------------------------------------- #

def q(s: str) -> str:
    """SQL string literal.

    >>> q("O'Brien")
    "'O''Brien'"
    """
    return "'" + s.replace("'", "''") + "'"


def statements(p: Proposal, rows: dict[str, Row]) -> list[str]:
    """The SQL lines applying one proposal.

    >>> r = {"Gone": Row(id=1, name="Gone", miss_reason="no-board-found")}
    >>> print("\\n".join(statements(Proposal(kind="prune", tier="apply", basis="x",
    ...     name="Gone", reason="dead"), r)))   # doctest: +ELLIPSIS
    DELETE FROM companies WHERE name = 'Gone' AND ats IS NULL AND miss_reason IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.company_id = companies.id);
    INSERT INTO name_blocklist (key, name, reason, added_at) VALUES ('gone', 'Gone', 'dead', strftime('%Y-%m-%dT%H:%M:%f', 'now'))
      ON CONFLICT(key) DO UPDATE SET name = excluded.name, reason = excluded.reason, added_at = excluded.added_at;
    """
    out: list[str] = []
    name = q(p.name)
    if p.kind == "merge" and p.target:
        tgt = q(p.target)
        out.append(f"UPDATE jobs SET company_id = (SELECT id FROM companies WHERE name = {tgt}),"
                   f" company_name = {tgt}\n  WHERE company_id = (SELECT id FROM companies WHERE name = {name});")
        loser, keep = rows.get(p.name), rows.get(p.target)
        have = {t for t in ((keep.tags if keep else None) or "").split(",") if t}
        merged = have | {t for t in ((loser.tags if loser else None) or "").split(",") if t}
        if merged != have:
            out.append(f"UPDATE companies SET tags = {q(','.join(sorted(merged)))} WHERE name = {tgt};")
        out.append(f"DELETE FROM companies WHERE name = {name} AND ats IS NULL AND miss_reason IS NOT NULL;")
        block = p.block and name_key(p.name) != name_key(p.target)
    else:
        out.append(f"DELETE FROM companies WHERE name = {name} AND ats IS NULL AND miss_reason IS NOT NULL\n"
                   "  AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.company_id = companies.id);")
        block = p.block
    if block:
        out.append("INSERT INTO name_blocklist (key, name, reason, added_at) VALUES "
                   f"({q(name_key(p.name))}, {name}, {q(p.reason)}, strftime('%Y-%m-%dT%H:%M:%f', 'now'))\n"
                   "  ON CONFLICT(key) DO UPDATE SET name = excluded.name, reason = excluded.reason,"
                   " added_at = excluded.added_at;")
    return out


def render(props: Sequence[Proposal], keeps: Sequence[str], rows: dict[str, Row],
           summary: Sequence[str]) -> str:
    """The SQL file: a counts header, the apply tier in one transaction, the
    review tier commented out, then the rows deliberately left alone."""
    lines = ["-- Miss-store cleanup proposals. Nothing here has been applied.",
             "-- Generated by tools/miss_cleanup.py from a read-only copy of the store.",
             "-- Statements are guarded (ats IS NULL, miss_reason IS NOT NULL, no jobs on a prune),",
             "-- so a row that changed since the read is skipped.", "--",
             *(f"-- {s}" for s in summary), ""]
    for tier, title in (("apply", "APPLY TIER"), ("review", "REVIEW TIER (uncomment to apply)")):
        mine = sorted((p for p in props if p.tier == tier),
                      key=lambda p: (p.kind, p.basis, p.name.lower()))
        lines += [f"-- {'=' * 70}", f"-- {title}: {len(mine)} proposal(s)", f"-- {'=' * 70}"]
        if tier == "apply":
            lines.append("BEGIN;")
        for p in mine:
            lines.append("")
            head = (f"-- {p.kind} {p.name!r}" + (f" -> {p.target!r}" if p.target else "")
                    + f" [{p.basis}]: {p.reason}")
            lines.append(head)
            for s in statements(p, rows):
                lines += [s] if tier == "apply" else ["-- " + ln for ln in s.splitlines()]
        if tier == "apply":
            lines += ["", "COMMIT;"]
        lines.append("")
    lines += [f"-- {'=' * 70}", f"-- LEFT ALONE: {len(keeps)} row(s) the guards protect", f"-- {'=' * 70}"]
    lines += [f"-- {k}" for k in keeps]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  Run                                                                         #
# --------------------------------------------------------------------------- #

def load_rows(db: Path) -> tuple[list[Row], list[Row]]:
    """(misses, tracked) read mode=ro. A miss is a boardless row carrying a
    miss_reason; tracked is every row without one."""
    conn = open_ro(db)
    try:
        found = [Row(**dict(r)) for r in conn.execute(
            "SELECT c.id, c.name, c.ats, c.active, c.miss_reason, c.tags, c.notes, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.company_id = c.id) AS jobs "
            "FROM companies c ORDER BY c.id")]
    finally:
        conn.close()
    misses = [r for r in found if r.miss_reason and not r.ats]
    return misses, [r for r in found if not r.miss_reason]


def load_trial(paths: Sequence[Path]) -> tuple[set[str], set[str]]:
    """(names a board was found for, names with no domain) from trial JSON."""
    hits: set[str] = set()
    domains: dict[str, bool] = {}
    for p in paths:
        for e in json.loads(p.read_text(encoding="utf-8")):
            n = e["name"]
            domains[n] = domains.get(n, False) or bool(e.get("domain"))
            if any(isinstance(e.get(k), dict) and e[k].get("ats") for k in ("hit", "hit2")):
                hits.add(n)
    return hits, {n for n, has in domains.items() if not has}


async def build(args: argparse.Namespace) -> tuple[list[Proposal], list[str], dict[str, Row], list[str]]:
    misses, tracked = load_rows(Path(args.db))
    hints = Hints.model_validate_json(Path(args.hints).read_text(encoding="utf-8")) if args.hints else Hints()
    hits, no_domain = load_trial([Path(p) for p in args.trial or []])
    d = Tracked(tracked)
    props: dict[str, Proposal] = {}
    kept = set(hints.keep)
    wd = Wikidata(Path(args.cache), args.delay, args.offline)

    def guarded(r: Row) -> str:
        if r.name in kept:
            return "hint: keep"
        if r.name in hits:
            return "a trial found it a board (the resolver work owns it)"
        if r.jobs:
            return f"holds {r.jobs} job row(s)"
        return ""

    # 1. hints, junk, and spellings of one company. A spelling merge is safe
    # for a row with jobs or a trial hit too (jobs move, the board is one
    # the survivor already carries); a prune is not.
    live = [r for r in misses if not guarded(r)]
    live_names = {r.name for r in live}
    for r in misses:
        if r.name in kept:
            continue
        if r.name in live_names and r.name in hints.prune:
            props[r.name] = Proposal(kind="prune", tier="apply", basis="hint", name=r.name,
                                     reason=hints.prune[r.name])
        elif r.name in hints.merge:
            props[r.name] = Proposal(kind="merge", tier="apply", basis="hint", name=r.name,
                                     target=hints.merge[r.name], reason="judgement call (hints file)")
        elif r.name in live_names and (j := junk_reason(r.name)):
            props[r.name] = Proposal(
                kind="prune", tier="apply", basis="junk", name=r.name,
                reason=f"junk name: {j}")
    for keep, losers in merge_groups([*misses, *tracked]):
        for loser in losers:
            if loser.name not in kept and loser.name not in props:
                basis = "tracked-alias" if not keep.miss_reason else "duplicate-name"
                props[loser.name] = Proposal(
                    kind="merge", tier="review" if loser.notes else "apply", basis=basis,
                    name=loser.name, target=keep.name,
                    reason=f"same company as '{keep.name}' under another spelling")
    # 2. Wikidata for the rest.
    todo = [r for r in live if r.name not in props]
    found = await wikidata_items(wd, [r.name for r in todo])
    wd.save()
    unknown: list[Row] = []
    for r in todo:
        p = judge_item(d, wd, r, found.get(r.name, []))
        if p:
            if r.notes:
                p = p.model_copy(update={"tier": "review"})
            props[r.name] = p
        elif not found.get(r.name):
            unknown.append(r)
    # 3. no domain, no Wikidata item: review-tier prune.
    for r in unknown:
        if r.name in no_domain and not r.notes and (r.miss_reason or "").startswith("no-board-found"):
            props[r.name] = Proposal(kind="prune", tier="review", basis="stale-no-signal", name=r.name,
                                     reason="no domain found for the name and no Wikidata organisation of it",
                                     block=False)
    keeps = [f"{r.name!r} [{r.miss_reason}]: {guarded(r)}"
             for r in misses if guarded(r) and r.name not in props]
    keeps += [f"{r.name!r} [{r.miss_reason}] has notes: {r.notes[:60]!r}"
              for r in misses if r.notes and r.name in props and props[r.name].tier == "review"]
    rows = {r.name: r for r in [*misses, *tracked]}
    tally = Counter((p.tier, p.kind, p.basis) for p in props.values())
    summary = [f"{len(misses)} miss rows read; {len(props)} proposals; {len(keeps)} left alone; "
               f"{wd.calls} Wikidata call(s) this run."]
    summary += [f"{tier:6} {kind:5} {basis:20} {n:4}" for (tier, kind, basis), n in sorted(tally.items())]
    return list(props.values()), keeps, rows, summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Propose cleanup SQL for roster miss rows")
    ap.add_argument("--db", default=str(config.STORE_DB_PATH))
    ap.add_argument("--out", default="proposals.sql")
    ap.add_argument("--cache", default="wikidata_cache.json")
    ap.add_argument("--hints", help="JSON {merge:{name:target}, prune:{name:reason}, keep:[name]}")
    ap.add_argument("--trial", action="append", help="domain-lookup trial JSON (repeatable)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between Wikidata requests")
    ap.add_argument("--offline", action="store_true", help="use the Wikidata cache only")
    args = ap.parse_args(argv)
    props, keeps, rows, summary = runstate.run(build(args))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(render(props, keeps, rows, summary), encoding="utf-8")
    print("\n".join(summary))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
