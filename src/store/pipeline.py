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

from datetime import datetime


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

# The user-editable application-tracking columns. update_pipeline_fields
# writes these and nothing else, so an API caller cannot reach `disposition`
# (which has its own validated path) or any scorer-owned column through it.
PIPELINE_FIELDS = ("followup_at", "contact", "referral", "outcome_reason")

# How an application ENDED, as a closed vocabulary rather than free text: the
# free-text note already exists for nuance, and a fixed set is what lets
# conversion_report tell "never answered" from "interviewed and lost".
OUTCOME_REASONS = ("no-response", "rejected-screen", "rejected-interview",
                   "withdrew", "closed", "other")

# The resume_fit_score bands conversion_report groups by: (name, low, high),
# half-open on the high side, ordered low to high.
FIT_BANDS = (("low", 0.0, 0.4), ("mid", 0.4, 0.6), ("high", 0.6, 1.01))

def set_job_status(conn, job_id, status):
    """Mark one job 'open' or 'closed' directly (closed_at maintained)."""
    conn.execute(
        "UPDATE jobs SET status=?, closed_at=? WHERE job_id=?",
        (status, datetime.now().isoformat() if status == "closed" else None,
         job_id))
    conn.commit()


def _resolve_job(conn, ref):
    """Resolve a user-supplied job reference to rows: exact job_id first,
    then any job_id substring, then normalized URL. Returns a list of
    matching rows (ideally one; several = ambiguous; empty = no match) so
    set_disposition, its only caller, can report ambiguity instead of
    guessing."""
    from .jobs import _norm_url  # not at module level: see module doc
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (ref,)).fetchone()
    if row:
        return [dict(row)]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE job_id LIKE ?", (f"%{ref}%",)).fetchall()]
    if rows:
        return rows
    want = _norm_url(ref)
    if want:
        return [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()
                if _norm_url(r["url"]) == want]
    return []


def set_disposition(conn, ref, disposition, note=None):
    """Record the user's decision on one job. `ref` is a job_id, a unique
    job_id fragment, or the posting URL; `disposition` is one of
    DISPOSITIONS, or 'none'/'clear' to erase. Returns (row, error) — row is
    the matched job on success, error a printable message otherwise.

    Marking a row 'applied' also stamps `applied_at`, once: a later
    interviewing/rejected leaves the original apply date alone. See
    tests/test_store.py::TestPipelineTracking."""
    d = (disposition or "").strip().lower()
    clearing = d in ("none", "clear")
    if not clearing and d not in DISPOSITIONS:
        return None, (f"unknown disposition {disposition!r} — use one of "
                      f"{', '.join(DISPOSITIONS)} (or 'clear')")
    matches = _resolve_job(conn, ref)
    if not matches:
        return None, f"no job matches {ref!r} (job_id, id fragment, or URL)"
    if len(matches) > 1:
        opts = "\n".join(f"    {m['job_id']}  {(m['title'] or '')[:50]}"
                         for m in matches[:8])
        return None, f"{ref!r} is ambiguous ({len(matches)} matches):\n{opts}"
    row = matches[0]
    now = datetime.now().isoformat()
    sets = ["disposition=?", "disposition_note=?", "disposition_at=?"]
    args = [None if clearing else d, None if clearing else note,
            None if clearing else now]
    if d == "applied":
        # COALESCE, not an assignment: the FIRST apply owns the date. Without
        # it, re-marking a row that came back 'rejected' and then 'applied'
        # again — or any later edit — would silently reset the clock every
        # elapsed-time question is measured against.
        sets.append("applied_at=COALESCE(applied_at, ?)")
        args.append(now)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?",
                 [*args, row["job_id"]])
    conn.commit()
    return row, None


def get_pipeline(conn):
    """Every job the user has dispositioned, newest decision first — the
    digest's pipeline section and the --pipeline CLI. Includes closed rows
    on purpose: 'posting closed after you applied' is a signal."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE disposition IS NOT NULL "
        "ORDER BY disposition_at DESC").fetchall()]


def update_pipeline_fields(conn, job_id, **fields):
    """Write the application-tracking columns (PIPELINE_FIELDS) on one job.

    Any other column name is refused rather than written, empty strings
    normalize to NULL, `referral` to 0/1, and `outcome_reason` must be one of
    OUTCOME_REASONS. Returns (row, error) like set_disposition — the updated
    job on success, a printable message otherwise. See
    tests/test_store.py::TestPipelineTracking.

    Notes:
        The whitelist is the point: this is reachable from the browser, and a
        blanket "UPDATE jobs SET <whatever the JSON body named>" would let a
        typo — or a crafted request — overwrite a scorer-owned column such as
        resume_fit_score or the validated `disposition` itself.
    """
    unknown = sorted(set(fields) - set(PIPELINE_FIELDS))
    if unknown:
        return None, (f"unknown pipeline field(s) {', '.join(unknown)} — "
                      f"writable: {', '.join(PIPELINE_FIELDS)}")
    if conn.execute("SELECT 1 FROM jobs WHERE job_id=?",
                    (job_id,)).fetchone() is None:
        return None, f"no job matches {job_id!r}"
    sets = {}
    for k, v in fields.items():
        if k == "referral":
            sets[k] = None if v is None else int(bool(v))
            continue
        v = (str(v).strip() if v is not None else "") or None
        if k == "outcome_reason" and v is not None and v not in OUTCOME_REASONS:
            return None, (f"unknown outcome_reason {v!r} — use one of "
                          f"{', '.join(OUTCOME_REASONS)}")
        sets[k] = v
    if sets:
        conn.execute(
            f"UPDATE jobs SET {', '.join(f'{k}=?' for k in sets)} "
            f"WHERE job_id=?", [*sets.values(), job_id])
        conn.commit()
    return dict(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                             (job_id,)).fetchone()), None


def _fit_band(score):
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


def conversion_report(conn):
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
    buckets = {}
    for r in rows:
        key = (_fit_band(r["resume_fit_score"]), r["geo_mode"] or "unknown")
        bucket = buckets.setdefault(key, {
            "band": key[0], "geo_mode": key[1], "applications": 0,
            "applied": 0, "interviewing": 0, "rejected": 0, "interviews": 0})
        bucket["applications"] += 1
        bucket[r["disposition"]] += 1
        if (r["disposition"] == "interviewing"
                or r["outcome_reason"] == "rejected-interview"):
            bucket["interviews"] += 1
    out = sorted(buckets.values(),
                 key=lambda x: (order.index(x["band"]), x["geo_mode"]))
    for bucket in out:
        bucket["interview_rate"] = round(
            bucket["interviews"] / bucket["applications"], 3)
    return out


def followups_due(conn, today=None):
    """Live applications whose follow-up date has arrived, oldest first.

    A row qualifies when `followup_at` is set and not in the future and the
    application is still live (LIVE_DISPOSITIONS) — nudging a rejected row
    is noise. `today` is a 'YYYY-MM-DD' string and defaults to today. See
    tests/test_store.py::TestPipelineTracking.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    ph = ",".join("?" for _ in LIVE_DISPOSITIONS)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM jobs WHERE COALESCE(followup_at,'') != '' "
        f"AND followup_at <= ? AND disposition IN ({ph}) "
        f"ORDER BY followup_at", (today, *LIVE_DISPOSITIONS)).fetchall()]
