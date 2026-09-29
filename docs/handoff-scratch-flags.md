# Handoff: move `_mission_tried` and `_company_id` off the row dicts

Branch: `harvest` (HEAD `39c5eed` when this was written). Nothing for this task
is started; the tree was clean.

## Why

Two internal flags are written onto plain dicts as extra keys:

| Flag | Written | Read | On what |
|---|---|---|---|
| `_mission_tried` | `src/crawl/triage.py:189` | `triage.py:187` only | a **company** row dict |
| `_company_id` | `src/ops/ingest.py:92` (`_admitted`) | `ingest.py:28`, `:31`, `:116` | each **job** dict passed in by the caller |

A dict that carries keys that are not real columns cannot be given a static
type (a `TypedDict` rejects the key). That was the blocker when I tried to type
the company row: the error count went 29 -> 61 -> 49 -> 54 and never converged,
partly because of `company["_mission_tried"] = True`. Removing scratch keys is
the prerequisite, and it is a small cleanup on its own.

Neither flag is persisted, printed, or shown to the user. The change should be
behavior-neutral. The one way to get it wrong is the **lifetime** of
`_mission_tried` (see below).

## 1. `_mission_tried` (triage.py)

`ensure_mission(db, company, titles, scorer)` scores a company's mission once
via Claude when the roster row has neither tier nor score. The flag means "one
attempt per pass":

```python
if company.get("_mission_tried"):
    return None, None                       # one attempt per pass
company["_mission_tried"] = True            # set BEFORE the try: a failed
try:                                        # attempt also counts, no retry
    ...
```

Facts to preserve:

- It is set **before** the scorer call, so a scorer exception still counts as an
  attempt. Do not move the marking after the call.
- Scope = the lifetime of the `companies` dict a pass builds in
  `_by_company` (`triage.py:396`, a `{company_id: get_company(...)}` dict), used by
  `_judged` and `judge` (`triage.py:314`, which calls `ensure_mission` at `:326`).
  A new pass rebuilds the dicts, so the flag resets. A per-pass set must reset
  the same way.
- `company["mission_tier"], company["mission_score"] = tier, score` (right after
  the upsert) is a legitimate cache write into real columns. Keep it.
- `judge` is called once per company per pass today, so in practice the flag is
  a guard against the same company being judged twice in a pass. Confirm that in
  `_judged` before choosing a design.

Suggested design: give the pass a `tried: set[int]` (company ids) and pass it
down `_judged -> judge -> ensure_mission(..., tried=...)`. Default `tried=None`
means no memo, so the existing direct call in `tests/test_triage.py:120`
(`ensure_mission(w, company, titles, scorer)`) keeps working. Key by
`company["id"]`; if a test builds an id-less company dict, key on
`company.get("id") or company.get("name")`.

Alternative: return the tried-state from the pass runner instead of threading a
set. Threading a set is smaller.

## 2. `_company_id` (ingest.py)

`_admitted` resolves each job's company and stamps `j["_company_id"]` on the
kept jobs (also mutating the **caller's** dicts, which no one reads afterwards).
Readers: `_hydrate_missing_descriptions` (filter `need`, group key) and the
`fan_out` lambda in `ingest_external_jobs` (`_scored_row(company_id=...)`).

Suggested design: have `_admitted` return `kept` as `list[tuple[dict, int | None]]`
(job, company_id) and pass the pairs through:

- `_hydrate_missing_descriptions(db, kept)`: `need` = pairs where the id is
  truthy **and** the description is blank (a `None` id is excluded today via
  `j.get("_company_id")` truthiness; keep that). Group by id with a plain dict
  (or keep `group_by_company` by grouping the pairs on the id).
- The scoring lambda: `lambda pair: _scored_row(pair[0], company_id=pair[1], ...)`.
- `len(kept)` in the final print is unchanged.

Still mutating `j["id"]` when missing is intentional (the id is used later).
Leave it.

## Verifying (behavior must not change)

There is **no direct test** of `ingest_external_jobs`'s admit/hydrate/score path
(`tests/test_reresolve.py` only monkeypatches it) and none of the once-per-pass
rule. Add before/after tests, then refactor:

1. `ensure_mission` with a shared `tried` set scores at most once per company
   per pass, including when the first attempt raised.
2. A pass that judges the same company twice calls the scorer once.
3. `_hydrate_missing_descriptions` groups by company and skips jobs whose
   company id is `None`.
4. `ingest_external_jobs` passes the right `company_id` into the stored row.
5. Existing: `tests/test_triage.py`, `tests/test_webapp.py`, `tests/test_store.py`.

Then, as everywhere in this session: run the old and new implementation on the
same inputs and compare outputs (the repo has no oracle for this path, so write
one on a temp store).

## Environment and gotchas (things that cost time this session)

- **Python 3.12+ required.** The repo uses `def f[T](...)` syntax and
  `threading.Thread(context=...)`. Use a 3.13 venv:
  `python3.13 -m venv v && v/bin/pip install -r envs/requirements.txt -r envs/requirements-dev.txt`.
  On 3.11 the suite shows unrelated failures.
- **Checks**: `python -m mypy` (CI runs it; `disallow_untyped_defs`) and
  `python -m pytest -q` (includes doctests and the architecture invariants).
- **Line endings are mixed.** Most `src/store/*`, `src/ops/*`, `src/crawl/*`
  files are CRLF; `tests/test_invariants.py`, `src/config/policy.py`,
  `src/crawl/triage.py` and new files are LF. Do not rewrite whole files.
  `git diff --stat` should show only the lines you changed.
- **Invariants that bite** (`tests/test_invariants.py`):
  - module-level names read by exactly one function must live in that function
    (or be allowlisted as declared SQL);
  - no `threading` outside the four sync wrappers;
  - `src/` imports must point down `LAYERS`;
  - a store connection must be closed on every path.
- **Packaging**: data files (like `src/store/migrations/*.sql`) must be listed
  in `build_app.py` `DATA_DIRS`; a test guards the migrations dir.

## Follow-up (not part of this task)

Other scratch keys, **not yet verified safe**:

- `_tried` (`harvest.py:482`, read `triage.py:571`; five tests set it directly:
  `test_triage.py` lines 81, 485, 734, 761, 788).
- `_free` (`ats/board/engine.py`, internal to the fetch layer).
- `_new` and `_us_eligible` (`runner.py:431-432`). `_new` drives the
  "(NEW)"/"(seen)" label in the sample-matches printout (`runner.py:676`), which
  is **user-visible**. My search for their readers was quote-sensitive; grep
  for both quote styles and check `src/digest/` before touching them. Compare the
  full digest and matches output before and after.

Once the scratch keys are gone, retry typing the stored company row
(`CompanyRow`, `total=False`, columns from `src/store/migrations/0001_baseline.sql`).
The other blockers found: the ATS layer indexes company dicts by config-driven
column names (`config.BOARDS[...]["handle"]["columns"]`, used by
`coords.columns`, `board_key`, `Board.handle`), which mypy cannot see through,
and discovery passes candidate dicts with extra keys through the same
functions. Expect casts at those boundaries.

## What was done in this session (for context)

`ranked_jobs`, `dedup_jobs`, `rekey_jobs`, `dedup_companies` and
`company_by_board` moved to SQL/window functions; numbered `.sql` migrations
with `PRAGMA user_version`; `open_jobs` and `company_open_stats` views plus
partial indexes; `JobIn`/`FitColumns` and `TrackDict` types (`TrackDict` has since been
replaced by `config.tracks.RuntimeTrack`). Each conversion was
checked against the old implementation on randomized data; that harness is the
pattern to follow here.
