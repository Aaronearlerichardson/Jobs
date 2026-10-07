"""Discourse forum job-category feed."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from src.net import http
from src.net.http import JSON_HEADERS
from src.rows import FetchedJob


class _Topic(BaseModel):
    """One forum topic; `object` fields pass through uncoerced."""
    model_config = ConfigDict(frozen=True, extra="ignore")
    posts_count: object = 0
    reply_count: object = 0
    title: str | None = ""
    slug: object = ""
    id: object = ""
    last_posted_at: str | None = None


class _TopicList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    topics: list[_Topic] = []


class _Category(BaseModel):
    """The category payload; a wrong shape raises ValidationError.

    >>> _Category.model_validate({"topic_list": {"topics": [{"title": None}]}}
    ...                          ).topic_list.topics[0].title is None
    True
    >>> _Category.model_validate({"topic_list": []})
    Traceback (most recent call last):
    ...
    ...
    pydantic_core._pydantic_core.ValidationError: 1 validation error for _Category
    topic_list
    ...
    """
    model_config = ConfigDict(frozen=True, extra="ignore")
    topic_list: _TopicList = _TopicList()


async def fetch_discourse(display_name: str, base_url: str, category_id: int,
                          gate: Callable[..., bool] | None = None) -> list[FetchedJob]:
    url = f"{base_url}/c/job-opportunities/{category_id}.json"
    data = await http.get_json(url, f"Discourse {display_name}", default={},
                               headers=JSON_HEADERS)
    topics = _Category.model_validate(data or {}).topic_list.topics
    jobs: list[FetchedJob] = []
    for t in topics:
        if t.posts_count == 1 and t.reply_count == 0:
            continue
        title = t.title
        tid   = t.id
        jurl  = f"{base_url}/t/{t.slug}/{tid}"
        loc   = t.last_posted_at[:10] if t.last_posted_at else "See post"
        if gate is None or gate(title):
            jobs.append({
                "id":          f"discourse_{base_url.split('.')[0].split('//')[1]}_{tid}",
                "company":     display_name,
                "title":       title,  # type: ignore[typeddict-item]  # null passes through, as before
                "url":         jurl,
                "location":    f"Posted {loc}",
                "description": "",
            })
    return jobs
