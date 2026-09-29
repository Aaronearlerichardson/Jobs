"""Typed shapes for the rows the store is HANDED (jobs) and hands back (companies).

A misspelled key in a row headed for upsert_job used to store nothing and
say nothing (`j.get("resume_fit_scor")` reads as None). These TypedDicts let
mypy check every place that builds one, and tests/test_store.py checks the
field names against the tables themselves.

A leaf module, like src/tags.py: the store, the scorer and the crawl all
import it, so it imports nothing of theirs.
"""

from __future__ import annotations

from typing import Literal, TypedDict


class FitColumns(TypedDict, total=False):
    """The jobs columns one fit score writes (FitResult.as_columns)."""
    resume_fit_score: float | None
    fit_reason: str | None
    fit_gates: str | None
    fit_model: str | None
    fit_domain: float | None
    fit_function: float | None
    fit_stack: float | None
    fit_seniority: float | None


class JobIn(FitColumns, total=False):
    """A job row as upsert_job accepts it; `job_id` is the only required key
    in practice, and a missing key means "leave what is stored"."""
    job_id: str
    company_id: int | None
    company_name: str | None
    title: str | None
    url: str | None
    location: str | None
    track: str | None
    geo_mode: str | None
    remote_eligible: bool | int | None
    remote_signal: str | None
    anchor_signal: str | None
    description: str | None
    posted_at: str | None
    status: str
    harvested_at: str | None


class CompanyRow(TypedDict, total=False):
    """A stored companies row, as the store's readers return it."""
    id: int
    name: str
    ats: str | None
    slug: str | None
    wd_tenant: str | None
    wd_pod: int | None
    wd_site: str | None
    careers_url: str | None
    local_job_count: int | None
    total_job_count: int | None
    mission_tier: str | None
    mission_score: float | None
    mission_reason: str | None
    tags: str | None
    source: str | None
    active: int
    last_probed: str | None
    notes: str | None
    created_at: str | None
    miss_reason: str | None
    miss_at: str | None
    crawl_state: str | None
    empty_streak: int | None
    last_crawled_at: str | None
    last_nonempty_at: str | None
    next_crawl_at: str | None
    last_harvested_at: str | None


#: The companies columns a board's handle can be spelled in (`handle.columns`
#: of a config.BOARDS spec).
HandleColumn = Literal["slug", "wd_tenant", "wd_pod", "wd_site", "careers_url"]
