"""Discourse forum job-category feed."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from src.net import http
from src.net.http import JSON_HEADERS
from src.net.util import JSON
from src.rows import FetchedJob


async def fetch_discourse(display_name: str, base_url: str, category_id: int,
                          gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    url = f"{base_url}/c/job-opportunities/{category_id}.json"
    data = await http.get_json(url, f"Discourse {display_name}", default={},
                               headers=JSON_HEADERS)
    # TODO(any-zero): HEAD trusts the payload shape (a wrong one raises
    # AttributeError/TypeError); parse it through a typed model at the edge.
    d = cast("dict[str, dict[str, list[dict[str, JSON]]]]", data)
    topics = (d.get("topic_list") or {}).get("topics", []) if d else []
    jobs: list[FetchedJob] = []
    for t in topics:
        if t.get("posts_count", 0) == 1 and t.get("reply_count", 0) == 0:
            continue
        title = cast(str, t.get("title", ""))
        slug  = t.get("slug", "")
        tid   = t.get("id", "")
        jurl  = f"{base_url}/t/{slug}/{tid}"
        loc   = cast(str, t["last_posted_at"])[:10] if t.get("last_posted_at") else "See post"
        if gate is None or gate(title):
            jobs.append({
                "id":          f"discourse_{base_url.split('.')[0].split('//')[1]}_{tid}",
                "company":     display_name,
                "title":       title,
                "url":         jurl,
                "location":    f"Posted {loc}",
                "description": "",
            })
    return jobs
