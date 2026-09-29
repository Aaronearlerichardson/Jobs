"""Typed shapes for the dicts the store is HANDED to write.

A misspelled key in a row headed for upsert_job used to store nothing and
say nothing (`j.get("resume_fit_scor")` reads as None). These TypedDicts let
mypy check every place that builds one, and tests/test_store.py checks the
field names against the jobs table itself.

A leaf module, like src/tags.py: the store, the scorer and the crawl all
import it, so it imports nothing of theirs.
"""

from __future__ import annotations

from typing import TypedDict


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
