"""
The application pipeline: the user's recorded decision on a job
(disposition), the tracking columns behind it, follow-up reminders and the
conversion report. Split out of src.store on 2026-09-10; src.store
re-exports every name here, so callers keep saying ``store.set_disposition``.

This module must not import src.store at module level: store imports it
at load time to re-export it, and a module-level import back would make
whichever side loads first fail. The one helper a body needs is imported
inside the function.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from operator import itemgetter
from typing import Annotated, NamedTuple, TypedDict

from pydantic import (BaseModel, BeforeValidator, ConfigDict, PlainSerializer,
                      ValidationError)

from src.match.names import name_key
from src.rows import JobRow
from src.validation import OneOf, Text, blank_is_none, error_lines
from .schema import apply_update, as_job, sql

# The user's recorded decision on a job. `saved` = shortlisted, still shown
# in ranking; the rest leave the ranking: applied/interviewing move to the
# digest's pipeline section, rejected/dismissed disappear (and dismissed
# rows become negative few-shot examples for the fit scorer — 
# fit.py reads them, so a --note saying WHY is worth writing).
DISPOSITIONS = ("saved", "applied", "interviewing", "rejected", "dismissed")
RANKING_EXCLUDED_DISPOSITIONS = ("applied", "interviewing", "rejected", "dismissed")

# A live application: one that went out and has not come back. followups_due
# only chases these, and conversion_report counts everything else as closed.
LIVE_DISPOSITIONS = ("applied", "interviewing")

# An application that actually went out, however it ended. The denominator
# conversion_report divides by; `saved` and `dismissed` never applied.
APPLIED_DISPOSITIONS = ("applied", "interviewing", "rejected")

# How an application ENDED, as a closed vocabulary rather than free text: the
# free-text note already exists for nuance, and a fixed set is what lets
# conversion_report tell "never answered" from "interviewed and lost".
OUTCOME_REASONS = ("no-response", "rejected-screen", "rejected-interview",
                   "withdrew", "closed", "other")

# Why a job was DISMISSED, in the same `outcome_reason` column. Only the
# last three say something about FIT; the first two are bookkeeping and must
# not teach the scorer that the role was a poor match: a posting that was
# already dead ("closed", which also closes the row) or a second opening at
# a company you had applied to ("sibling").
NOT_A_FIT_SIGNAL = ("closed", "sibling")
DISMISS_REASONS = (*NOT_A_FIT_SIGNAL, "location", "function", "seniority", "other")

# The resume_fit_score bands conversion_report groups by: (name, low, high),
# half-open on the high side, ordered low to high.
FIT_BANDS = (("low", 0.0, 0.4), ("mid", 0.4, 0.6), ("high", 0.6, 1.01))


class PipelineFields(BaseModel):
    """The user-editable application-tracking columns, as stored: text
    stripped with a blank as NULL, `referral` as 0/1, `outcome_reason` one
    of OUTCOME_REASONS. Any other key is refused, so a caller cannot reach
    `disposition` (which has its own validated path) or a scorer-owned
    column through update_pipeline_fields."""
    model_config = ConfigDict(extra="forbid")

    followup_at: Text = None
    contact: Text = None
    referral: Annotated[bool | None,
                        PlainSerializer(int, when_used="unless-none")] = None
    outcome_reason: Annotated[Annotated[str, OneOf(OUTCOME_REASONS + DISMISS_REASONS)] | None,
                              BeforeValidator(blank_is_none)] = None


def set_job_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """Mark one job 'open' or 'closed' directly (closed_at maintained)."""
    apply_update(conn, "jobs", "job_id", job_id, {
        "status": status,
        "closed_at": datetime.now().isoformat() if status == "closed" else None,
    })


def _resolve_job(conn: sqlite3.Connection, ref: str) -> list[JobRow]:
    """Resolve a user-supplied job reference to rows: exact job_id first,
    then any job_id substring, then normalized URL. Returns a list of
    matching rows (ideally one; several = ambiguous; empty = no match) so
    set_disposition, its only caller, can report ambiguity instead of
    guessing."""
    from .jobs import _norm_url  # not at module level: see module doc
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (ref,)).fetchone()
    if row:
        return [as_job(row)]
    rows = [as_job(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE job_id LIKE ?", (f"%{ref}%",)).fetchall()]
    if rows:
        return rows
    want = _norm_url(ref)
    if want:
        return [as_job(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE norm_url(url) = ?", (want,)).fetchall()]
    return []


def set_disposition(conn: sqlite3.Connection, ref: str, disposition: str | None,
                    note: str | None = None, reason: str | None = None
                    ) -> tuple[JobRow | None, str | None]:
    """Record the user's decision on one job. `ref` is a job_id, a unique
    job_id fragment, or the posting URL; `disposition` is one of
    DISPOSITIONS, or 'none'/'clear' to erase. Returns (row, error) — row is
    the matched job on success, error a printable message otherwise.

    Marking a row 'applied' also stamps `applied_at`, once: a later
    interviewing/rejected leaves the original apply date alone. See
    tests/test_store.py::TestPipelineTracking.

    `reason` (one of DISMISS_REASONS) goes with a 'dismissed' decision and
    is kept in `outcome_reason`; 'closed' also closes the row. Clearing a
    dismissed row clears its reason.

    >>> from src import store
    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_job(conn, {"job_id": "j", "title": "T"})
    >>> set_disposition(conn, "j", "applied", reason="closed")[1]
    "a reason goes with 'dismissed' only"
    >>> set_disposition(conn, "j", "dismissed", reason="bogus")[1]
    "unknown dismissal reason 'bogus' - use one of closed, sibling, location, function, seniority, other"
    >>> _ = set_disposition(conn, "j", "dismissed", reason="closed")
    >>> tuple(conn.execute("SELECT outcome_reason, status FROM jobs").fetchone())
    ('closed', 'closed')
    >>> _ = set_disposition(conn, "j", "clear")
    >>> tuple(conn.execute("SELECT outcome_reason FROM jobs").fetchone())
    (None,)
    """
    d = (disposition or "").strip().lower()
    clearing = d in ("none", "clear")
    if not clearing and d not in DISPOSITIONS:
        return None, (f"unknown disposition {disposition!r} — use one of "
                      f"{', '.join(DISPOSITIONS)} (or 'clear')")
    reason = (reason or "").strip().lower() or None
    if reason and d != "dismissed":
        return None, "a reason goes with 'dismissed' only"
    if reason and reason not in DISMISS_REASONS:
        return None, (f"unknown dismissal reason {reason!r} - use one of "
                      f"{', '.join(DISMISS_REASONS)}")
    matches = _resolve_job(conn, ref)
    if not matches:
        return None, f"no job matches {ref!r} (job_id, id fragment, or URL)"
    if len(matches) > 1:
        opts = "\n".join(f"    {m['job_id']}  {(m['title'] or '')[:50]}"
                         for m in matches[:8])
        return None, f"{ref!r} is ambiguous ({len(matches)} matches):\n{opts}"
    row = matches[0]
    now = datetime.now().isoformat()
    sets: dict[str, object] = {"disposition": None if clearing else d,
                            "disposition_note": None if clearing else note,
                            "disposition_at": None if clearing else now}
    if reason:
        sets["outcome_reason"] = reason
    elif clearing and row.get("disposition") == "dismissed":
        sets["outcome_reason"] = None
    if reason == "closed":
        sets.update(status="closed", closed_at=now)
    if d == "applied":
        # COALESCE, not an assignment: the FIRST apply owns the date. Without
        # it, re-marking a row that came back 'rejected' and then 'applied'
        # again — or any later edit — would silently reset the clock every
        # elapsed-time question is measured against.
        sets["applied_at"] = sql("COALESCE(applied_at, ?)", now)
    apply_update(conn, "jobs", "job_id", row["job_id"], sets)
    return row, None


class Prior(NamedTuple):
    """An earlier application that a posting repeats: `kind` is "repost"
    (the same title at the same company) or "sibling" (the same role at
    another level), `title` the one you applied to, `disposition` where it
    stands, `when` the day (YYYY-MM-DD)."""
    kind: str
    title: str
    disposition: str
    when: str

    @property
    def label(self) -> str:
        """How the badge says it.

        >>> Prior("repost", "Data Engineer", "applied", "2026-08-21").label
        'applied to this title'
        >>> Prior("sibling", "Data Engineer", "applied", "2026-08-21").label
        'sibling of Data Engineer'
        """
        return "applied to this title" if self.kind == "repost" else f"sibling of {self.title}"


_LEVEL_WORDS = frozenset({"senior", "sr", "staff", "principal", "lead", "junior", "jr",
                          "associate", "i", "ii", "iii", "iv", "v", "level"})


def title_keys(title: str | None) -> tuple[str, str]:
    """(exact, role) keys of a job title. `exact` drops punctuation,
    requisition numbers and level digits; `role` also drops the level words,
    so a title and its other levels share it.

    >>> title_keys("Senior Machine Learning Engineer II")
    ('senior machine learning engineer ii', 'machine learning engineer')
    >>> title_keys("Network Engineer - #4532")
    ('network engineer', 'network engineer')
    >>> title_keys("Data Engineer (R-10234)") == title_keys("data engineer")
    True
    >>> title_keys("Senior")
    ('senior', 'senior')
    """
    text = re.sub(r"#\s*\d+|\br-?\d+\b", " ", (title or "").lower())
    words = [w for w in re.findall(r"[a-z0-9+]+", text) if not w.isdigit()]
    role = [w for w in words if w not in _LEVEL_WORDS]
    return " ".join(words), " ".join(role or words)


def _str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def prior_lookup(pipeline: Iterable[Mapping[str, object]]
                 ) -> Callable[[Mapping[str, object]], Prior | None]:
    """A function from a job row to the application it repeats, or None,
    over `pipeline` (get_pipeline's rows; only the ones that went out count:
    APPLIED_DISPOSITIONS). A job repeats an application at the same company
    (name_key of company_name) whose title has the same `exact` key (a
    "repost") or the same `role` key (a "sibling"); a repost wins, then the
    newest application. A row never repeats itself.

    >>> look = prior_lookup([{"job_id": "a", "company_name": "Acme, Inc.",
    ...                       "title": "Algorithm Engineer", "disposition": "applied",
    ...                       "applied_at": "2026-08-21T10:00"}])
    >>> look({"job_id": "b", "company_name": "ACME inc", "title": "Algorithm Engineer"})
    Prior(kind='repost', title='Algorithm Engineer', disposition='applied', when='2026-08-21')
    >>> look({"job_id": "c", "company_name": "Acme Inc", "title": "Senior Algorithm Engineer II"}).kind
    'sibling'
    >>> look({"job_id": "d", "company_name": "Other", "title": "Algorithm Engineer"}) is None
    True
    >>> look({"job_id": "a", "company_name": "Acme Inc", "title": "Algorithm Engineer"}) is None
    True
    """
    by_company: dict[str, list[tuple[str, str, Mapping[str, object]]]] = {}
    for p in pipeline:
        if p.get("disposition") in APPLIED_DISPOSITIONS:
            exact, role = title_keys(_str(p.get("title")))
            by_company.setdefault(name_key(_str(p.get("company_name"))), []).append((exact, role, p))

    def when(p: Mapping[str, object]) -> str:
        return (_str(p.get("applied_at")) or _str(p.get("disposition_at")) or "")[:10]

    def look(job: Mapping[str, object]) -> Prior | None:
        rivals = by_company.get(name_key(_str(job.get("company_name"))))
        if not rivals:
            return None
        exact, role = title_keys(_str(job.get("title")))
        best = max(((1 if e == exact else 0, when(p), p) for e, r, p in rivals
                    if p.get("job_id") != job.get("job_id") and r == role),
                   key=itemgetter(0, 1), default=None)
        if best is None:
            return None
        return Prior("repost" if best[0] else "sibling", _str(best[2].get("title")) or "",
                     _str(best[2].get("disposition")) or "", best[1])
    return look


def get_pipeline(conn: sqlite3.Connection) -> list[JobRow]:
    """Every job the user has dispositioned, newest decision first — the
    digest's pipeline section and the --pipeline CLI. Includes closed rows
    on purpose: 'posting closed after you applied' is a signal."""
    return [as_job(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE disposition IS NOT NULL "
        "ORDER BY disposition_at DESC").fetchall()]


def update_pipeline_fields(conn: sqlite3.Connection, job_id: str,
                           **fields: object) -> tuple[JobRow | None, str | None]:
    """Write the given application-tracking columns (PipelineFields) on one
    job. Returns (row, error) like set_disposition: the updated job on
    success, otherwise a printable message with one 'field: problem' per
    bad key. See tests/test_store.py::TestPipelineTracking.

    Notes:
        The whitelist is the point: this is reachable from the browser, and a
        blanket "UPDATE jobs SET <whatever the JSON body named>" would let a
        typo — or a crafted request — overwrite a scorer-owned column such as
        resume_fit_score or the validated `disposition` itself.
    """
    try:
        sets = PipelineFields.model_validate(fields).model_dump(
            exclude_unset=True)
    except ValidationError as e:
        return None, "; ".join(error_lines(e))
    if conn.execute("SELECT 1 FROM jobs WHERE job_id=?",
                    (job_id,)).fetchone() is None:
        return None, f"no job matches {job_id!r}"
    apply_update(conn, "jobs", "job_id", job_id, sets)
    return as_job(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)).fetchone()), None


def _fit_band(score: float | None) -> str:
    """The conversion_report bucket one resume_fit_score falls in.

    >>> _fit_band(0.2), _fit_band(0.45), _fit_band(0.9)
    ('low', 'mid', 'high')

    Each edge belongs to the band above it, and a perfect score still lands
    in the top band rather than off the end:

    >>> _fit_band(0.4), _fit_band(0.6), _fit_band(1.0)
    ('mid', 'high', 'high')

    A row the scorer never reached is reported separately, not counted as a
    weak one:

    >>> _fit_band(None)
    'unscored'

    A score outside 0..1 is a scorer bug, not a band of its own; it is
    clamped to the nearest end instead of raising or reading as 'low':

    >>> _fit_band(-0.1), _fit_band(1.5)
    ('low', 'high')
    """
    if score is None:
        return "unscored"
    for name, lo, hi in FIT_BANDS:
        if lo <= score < hi:
            return name
    # Off the table entirely (a negative score, or one above the top band's
    # deliberately open 1.01 ceiling): clamp to the nearest end.
    return FIT_BANDS[0][0] if score < FIT_BANDS[0][1] else FIT_BANDS[-1][0]


class ConversionRow(TypedDict):
    """One conversion_report row."""
    band: str
    geo_mode: str
    applications: int
    applied: int
    interviewing: int
    rejected: int
    interviews: int
    interview_rate: float


def conversion_report(conn: sqlite3.Connection) -> list[ConversionRow]:
    """Where applications actually convert, sliced by fit band and geo_mode.

    One dict per (band, geo_mode) that has at least one application, ordered
    by band (FIT_BANDS low to high, then 'unscored') then geo_mode. Each
    carries `applications` (every row that went out), the live `applied` and
    `interviewing` counts, `rejected`, `interviews`, and `interview_rate` =
    interviews / applications, rounded to three places.

    `interviews` counts a row that REACHED an interview, which is not the
    same as one sitting in 'interviewing': a rejected row whose
    outcome_reason is 'rejected-interview' got there too, and without it
    every conversion number would decay as applications resolve. See
    tests/test_store.py::TestPipelineTracking.
    """
    ph = ",".join("?" for _ in APPLIED_DISPOSITIONS)
    rows = conn.execute(
        f"SELECT resume_fit_score, geo_mode, disposition, outcome_reason "
        f"FROM jobs WHERE disposition IN ({ph})",
        APPLIED_DISPOSITIONS).fetchall()
    order = [name for name, _, _ in FIT_BANDS] + ["unscored"]
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        key = (_fit_band(r["resume_fit_score"]), r["geo_mode"] or "unknown")
        n = counts.setdefault(key, dict.fromkeys(
            ("applications", "applied", "interviewing", "rejected", "interviews"), 0))
        n["applications"] += 1
        n[r["disposition"]] += 1
        if (r["disposition"] == "interviewing"
                or r["outcome_reason"] == "rejected-interview"):
            n["interviews"] += 1
    return [ConversionRow(
        band=band, geo_mode=geo, applications=n["applications"],
        applied=n["applied"], interviewing=n["interviewing"],
        rejected=n["rejected"], interviews=n["interviews"],
        interview_rate=round(n["interviews"] / n["applications"], 3))
        for (band, geo), n in sorted(
            counts.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1]))]


def followups_due(conn: sqlite3.Connection, today: str | None = None) -> list[JobRow]:
    """Live applications whose follow-up date has arrived, oldest first.

    A row qualifies when `followup_at` is set and not in the future and the
    application is still live (LIVE_DISPOSITIONS) — nudging a rejected row
    is noise. `today` is a 'YYYY-MM-DD' string and defaults to today. See
    tests/test_store.py::TestPipelineTracking.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    ph = ",".join("?" for _ in LIVE_DISPOSITIONS)
    return [as_job(r) for r in conn.execute(
        f"SELECT * FROM jobs WHERE COALESCE(followup_at,'') != '' "
        f"AND followup_at <= ? AND disposition IN ({ph}) "
        f"ORDER BY followup_at", (today, *LIVE_DISPOSITIONS)).fetchall()]
