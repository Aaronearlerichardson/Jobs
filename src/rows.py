"""Typed shapes for the rows the store is HANDED (jobs) and hands back (companies),
and for the resolver hit that becomes a company row.

A misspelled key in a row headed for upsert_job used to store nothing and
say nothing (`j.get("resume_fit_scor")` reads as None). These TypedDicts let
mypy check every place that builds one, and tests/test_store.py checks the
field names and column types against the tables themselves.

A leaf module, like src/tags.py: the store, the scorer and the crawl all
import it, so it imports nothing of theirs.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field
from typing_extensions import ReadOnly, TypedDict


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


# Open, unlike BoardHit: a stored CompanyRow flows into upsert_company, and a
# closed CompanyIn would refuse the row's extra keys.
class CompanyIn(TypedDict, total=False):
    """The companies columns upsert_company writes; a missing key leaves what
    is stored."""
    name: Annotated[str, Field(min_length=1)]
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
    active: int | None
    last_probed: str | None
    notes: str | None
    created_at: str | None
    miss_reason: str | None
    miss_at: str | None


class CompanyRow(CompanyIn, total=False):
    """A stored companies row, as the store's readers return it: what is
    written, plus the id and the crawl schedule."""
    id: int
    crawl_state: str | None
    empty_streak: int | None
    last_crawled_at: str | None
    last_nonempty_at: str | None
    next_crawl_at: str | None
    last_harvested_at: str | None


# Closed, so `"wd_tenant" in x` narrows a `BoardHit | CompanyRow` to the row.
class BoardHit(TypedDict, total=False, closed=True):
    """A resolver's answer for one board: its coordinates and what reading it found."""
    name: str
    ats: str
    slug: str | tuple[Any, ...] | None      # a tuple where the handle spans columns
    careers_url: str | None
    count: int                              # postings on the board
    nc: int                                 # of them, in your [locality]
    via: str                                # how it was found (sniff, probe, ...)
    reason: str                             # the miss code, when it is a miss
    source_url: str                         # the page that named it
    validated: bool
    confirmed: bool
    elapsed: float


class BoardCoords(TypedDict, total=False):
    """The board coordinates a hit and a store row both carry, for a function
    that reads only these and takes either."""
    ats: ReadOnly[str | None]
    slug: ReadOnly[str | tuple[Any, ...] | None]
    wd_tenant: ReadOnly[str | None]
    wd_pod: ReadOnly[int | None]
    wd_site: ReadOnly[str | None]
    careers_url: ReadOnly[str | None]


#: The companies columns a board's handle can be spelled in (`handle.columns`
#: of a config.BOARDS spec).
HandleColumn = Literal["slug", "wd_tenant", "wd_pod", "wd_site", "careers_url"]
