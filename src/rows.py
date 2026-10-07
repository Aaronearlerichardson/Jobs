"""Typed shapes for the rows the store is HANDED (jobs) and hands back (companies),
for the fetched job that becomes a jobs row, for the ranked and stored
job rows read back, and for the resolver hit that becomes a company row.

A misspelled key in a row headed for upsert_job used to store nothing and
say nothing (`j.get("resume_fit_scor")` reads as None). These TypedDicts let
the type checker check every place that builds one, and tests/test_store.py checks the
field names and column types against the tables themselves.

A leaf module, like src/tags.py: the store, the scorer and the crawl all
import it, so it imports nothing of theirs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from annotated_types import MinLen
from typing_extensions import NotRequired, ReadOnly, TypedDict


#: What `json.loads` returns, once narrowed: a payload's value.
type JSON = Mapping[str, JSON] | Sequence[JSON] | str | int | float | bool | None


def dig(v: JSON, *keys: str) -> JSON:
    """`v` at the nested dict `keys`; None where the shape breaks.

    >>> dig({"a": {"b": [1]}}, "a", "b"), dig({"a": 1}, "a", "b"), dig([1], "a")
    ([1], None, None)
    """
    for k in keys:
        v = v.get(k) if isinstance(v, dict) else None
    return v


def str_or_none(v: object) -> str | None:
    """`v` if it is a string, else None: a payload or Mapping value read as text.

    >>> str_or_none("a"), str_or_none(1), str_or_none(None)
    ('a', None, None)
    """
    return v if isinstance(v, str) else None


class _FitFields(TypedDict, total=False):
    resume_fit_score: float | None
    fit_reason: str | None
    fit_gates: str | None
    fit_model: str | None
    fit_domain: float | None
    fit_function: float | None
    fit_stack: float | None
    fit_seniority: float | None


class FitColumns(_FitFields, total=False, closed=True):
    """The jobs columns one fit score writes (FitResult.as_columns). Closed,
    so it unpacks into JobIn and FetchedJob; JobIn extends the open base."""


class JobIn(_FitFields, total=False):
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


class Employer(TypedDict, closed=True):
    """The employer an aggregator board's posting names: what
    `attribute_employers` needs to find, or queue, its roster row."""
    name: str
    domain: str
    slug: str
    board: str
    page_url: str


class FetchedJob(TypedDict, total=False, closed=True):
    """A posting as a fetcher returns it, then as each later stage stamps it
    in place. Its names are checked against ROW_FIELDS and FitColumns by
    tests/test_boards_spec.py::test_the_fetched_job_names_the_row_fields_and_fit_columns.

    Notes:
        Nothing is required, because the shape differs by stage: a fetcher
        sets `id`, `title`, `url`, `location` and `description` (a feed or
        `board_jobs` adds `company`, `adapt` swaps it for `ats`), then
        `attribute_employers` stamps `company_id`, the sweep gates
        `track_tag` to `anchor_signal`, and a fit score the eight fit
        columns. The board engine's own row, before `board_jobs`, is
        `spec.EngineRow`.
    """
    id: str
    title: str
    url: str
    location: str
    description: str
    posted_at: str | None
    remote_hint: str
    company: str
    ats: str | None
    company_url: str
    _employer: Employer
    company_id: int | None
    track_tag: str
    remote_eligible: bool
    remote_signal: str
    anchor_signal: str
    resume_fit_score: float | None
    fit_reason: str | None
    fit_gates: str | None
    fit_model: str | None
    fit_domain: float | None
    fit_function: float | None
    fit_stack: float | None
    fit_seniority: float | None


class JobRow(TypedDict, closed=True):
    """A stored jobs row, as `SELECT *` returns it: every column, so a
    subscript is checked. It is declared apart from `JobIn` (what
    upsert_job takes) and `RankedJob`; tests/test_store.py checks all three
    against the table.

    Notes:
        `remote_hint` is the one key that is not a column: triage's
        hydration stamps the hint a detail gave onto the row for the rest of
        its pass, and the geo gate reads it.
    """
    id: int
    job_id: str
    company_id: int | None
    company_name: str | None
    title: str | None
    url: str | None
    location: str | None
    track: str | None
    geo_mode: str | None
    remote_eligible: int | None
    remote_signal: str | None
    anchor_signal: str | None
    description: str | None
    desc_checked_at: str | None
    resume_fit_score: float | None
    fit_reason: str | None
    first_seen: str | None
    last_seen: str | None
    status: str | None
    harvested_at: str | None
    triage_status: str | None
    triage_detail: str | None
    triaged_at: str | None
    fit_domain: float | None
    fit_function: float | None
    fit_stack: float | None
    fit_seniority: float | None
    fit_gates: str | None
    fit_model: str | None
    closed_at: str | None
    posted_at: str | None
    disposition: str | None
    disposition_note: str | None
    disposition_at: str | None
    probe_streak: int | None
    applied_at: str | None
    followup_at: str | None
    contact: str | None
    referral: int | None
    outcome_reason: str | None
    dup_of: int | None
    remote_hint: NotRequired[str]


class RankedJob(TypedDict, closed=True):
    """A row of `ranked_jobs`: the jobs columns (`description` only with
    `with_description`), the company's mission and tags, the combined score
    and, once collapsed, the survivor's `dup_*` fields. Its keys are checked
    by tests/test_store.py::TestJobReaders.test_ranked_jobs_returns_the_ranked_job_keys.
    """
    id: int
    job_id: str
    company_id: int | None
    company_name: str | None
    title: str | None
    url: str | None
    location: str | None
    track: str | None
    geo_mode: str | None
    remote_eligible: int | None
    remote_signal: str | None
    anchor_signal: str | None
    description: NotRequired[str | None]
    desc_checked_at: str | None
    resume_fit_score: float | None
    fit_reason: str | None
    first_seen: str | None
    last_seen: str | None
    status: str | None
    harvested_at: str | None
    triage_status: str | None
    triage_detail: str | None
    triaged_at: str | None
    fit_domain: float | None
    fit_function: float | None
    fit_stack: float | None
    fit_seniority: float | None
    fit_gates: str | None
    fit_model: str | None
    closed_at: str | None
    posted_at: str | None
    disposition: str | None
    disposition_note: str | None
    disposition_at: str | None
    probe_streak: int | None
    applied_at: str | None
    followup_at: str | None
    contact: str | None
    referral: int | None
    outcome_reason: str | None
    dup_of: int | None
    mission_tier: str | None
    mission_score: float | None
    company_watch: int | None
    combined_score: float | None
    dup_count: NotRequired[int]
    dup_job_ids: NotRequired[tuple[str, ...]]
    dup_urls: NotRequired[tuple[str | None, ...]]


class CompanyIn(TypedDict, total=False, closed=True):
    """The companies columns upsert_company writes, and the shape a builder
    of a partial row (coords.columns) returns: a missing key leaves what is
    stored."""
    name: Annotated[str, MinLen(1)]
    ats: str | None
    slug: str | None
    handle: str | None          # a multi-part handle, its parts joined by the spec's `sep`
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
    #: Facts of the employer: only "pending" and a truthy `watch` act (a write
    #: never clears them); a legacy `watch`/`pending-review` token in `tags`
    #: is read as the same. See store.upsert_company.
    review: str | None
    watch: int | None


class CompanyRow(TypedDict, closed=True):
    """A company as `SELECT *` over the companies_effective view returns it:
    every column, with the mission, active, review and watch the EFFECTIVE
    ones (the board's own, else its employer's; migration 0005) and `tags` the
    board's own scope tags (migration 0006), so
    a subscript is checked. Its columns are declared a second time, apart
    from CompanyIn (a TypedDict cannot make an inherited optional key
    required); tests/test_store.py checks both against the table and
    against each other."""
    id: int
    name: str
    ats: str | None
    slug: str | None
    handle: str | None
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
    crawl_state: str | None
    empty_streak: int | None
    last_crawled_at: str | None
    last_nonempty_at: str | None
    next_crawl_at: str | None
    last_harvested_at: str | None
    harvest_attempted_at: str | None
    employer_id: int | None
    review: str | None
    watch: int | None


def is_watched(company: Mapping[str, object] | None) -> bool:
    """True when `company` (a CompanyRow, or None) is on the watch list."""
    return bool(company and company.get("watch"))


# Closed, so `"handle" in x` narrows a `BoardHit | CompanyRow` to the row.
#: A board's handle: a slug, or Workday's (tenant, pod, site).
type Slug = str | tuple[str | int, ...] | None


class BoardHit(TypedDict, total=False, closed=True):
    """A resolver's answer for one board: its coordinates and what reading it found."""
    name: str
    ats: str
    slug: Slug                              # a tuple where the handle spans columns
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
    slug: ReadOnly[Slug]
    handle: ReadOnly[str | None]
    careers_url: ReadOnly[str | None]


#: The companies columns a board's handle can be spelled in (`handle.columns`
#: of a config.BOARDS spec); `handle` holds a handle of several parts, `sep`-joined.
HandleColumn = Literal["slug", "handle", "careers_url"]

