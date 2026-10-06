"""Title-word vocabulary: which words of a job title mark a company of an
active mission tier. Shared by the board directory and the registries."""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from functools import lru_cache

from src import config


@lru_cache(maxsize=1 << 16)
def words(text: str | None) -> frozenset[str]:
    """The lower-case words of `text`: runs of three or more letters.

    >>> sorted(words("Sr. Clinical Data Engineer (II)"))
    ['clinical', 'data', 'engineer']
    """
    return frozenset(re.findall(r"[a-z]{3,}", (text or "").lower()))


def title_vocab(conn: sqlite3.Connection, min_companies: int = 3) -> dict[str, float]:
    """The log-odds that a title word belongs to a company of an active
    mission tier, over the roster's stored job titles: each word of at
    least `min_companies` mission-scored companies, counted once per
    company (add-one smoothed). {} when the roster has only one side.

    >>> from src import store
    >>> conn = store.connect(":memory:")
    >>> for name, tier, titles in [("A", "core-mission", ["Clinical Scientist"]),
    ...                            ("B", "core-mission", ["Clinical Engineer"]),
    ...                            ("C", "other", ["Store Engineer"]),
    ...                            ("D", "other", ["Store Manager"])]:
    ...     cid = store.upsert_company(conn, {"name": name, "mission_tier": tier})
    ...     for n, t in enumerate(titles):
    ...         _ = store.upsert_job(conn, {"job_id": f"{name}{n}", "title": t, "company_id": cid})
    >>> {w: round(v, 1) for w, v in sorted(title_vocab(conn, 2).items())}
    {'clinical': 1.1, 'engineer': 0.0, 'store': -1.1}
    >>> title_vocab(store.connect(":memory:"))
    {}
    """
    active = set(config.ACTIVE_MISSION_TIERS)
    seen: defaultdict[int, set[str]] = defaultdict(set)
    tier: dict[int, bool] = {}
    for cid, mission, title in conn.execute(
            "SELECT DISTINCT j.company_id, c.mission_tier, j.title FROM jobs j "
            "JOIN companies_effective c ON c.id = j.company_id WHERE c.mission_tier IS NOT NULL"):
        seen[cid] |= words(title)
        tier[cid] = mission in active
    pos = sum(tier.values())
    neg = len(tier) - pos
    if not pos or not neg:
        return {}
    ins, outs = Counter[str](), Counter[str]()
    for cid, ws in seen.items():
        (ins if tier[cid] else outs).update(ws)
    return {w: math.log((ins[w] + 1) / (pos + 2)) - math.log((outs[w] + 1) / (neg + 2))
            for w in ins.keys() | outs.keys() if ins[w] + outs[w] >= min_companies}


def word_score(ws: Iterable[str], vocab: dict[str, float], shrink: int = 3) -> float:
    """The mean log-odds (`title_vocab`) of the distinct words `ws`, `shrink`
    extra words of zero keeping a short text modest.

    >>> round(word_score({"clinical", "unseen"}, {"clinical": 1.0}), 2)
    0.2
    """
    ws = set(ws)
    return sum(vocab.get(w, 0.0) for w in ws) / (len(ws) + shrink)
