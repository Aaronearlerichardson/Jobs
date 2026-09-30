# Fetched-job typing: five rulings and a design

Session of 2026-09-30, branch `harvest` at `b10ab8a` (the checkout was at
`a97c43d`; fast-forwarded first). Nothing is committed. Task A changed three
files (section 1). Task B changed no repo code: everything in section 2 was
measured on a scratch copy of `src/`.

**Status (owner decisions, same day).** Remove `via` and `job_id`: done
(phase 1, `37ffcf6`). Storing None for `url` or `location` is not a problem
(2.5). Phases 1 and 2 approved and done; phase 2 put `EngineRow` and
`FillField` in `src/ats/board/spec.py` beside `RowField`, not in `rows.py`.
Phase 3 (the fetched-job flow) is done as well; built, it needed no
`Fillable` type: `Board._apply` takes `EngineRow | FetchedJob`. Phase 4 is
done: REVIEW.md's Data shapes table and four reviewer rules from this work.
The handoff this memo answers, `docs/handoff-fetched-job-typing.md`, is
deleted, as it asked; "the handoff" below means that file. The separate
`JobRow` track (phase 5) is done too: section 4.

Checks on the tree at the first draft: `python -m mypy` 0 errors; `flake8 --select=F`
clean; `python -m pytest` 1527 passed, 10 skipped, 0 failed (example profile;
9 skips are "profile configures no silicon title tokens" and 1 is "no NC
locality", which is why the handoff's local run read 1535 passed + 2
failed). Both profile-dependent tests the handoff named pass on the example
profile.

## 1. Task A: the five disputes

### D1. Mission memo: keep `triage._once_per_pass`

Checked, not assumed:

- Every entry point starts one Run per op: the root `harvest.py:297` (a
  pass), `src/dispatch/background.py:151` (a web op), `src/web/server.py:55`
  (a request), `runstate.run` in `run_scraper.py:61,325` and `discover.py`. No
  op calls another (`registry.invoke` is only called by entry points).
  `triage.run` has two callers: `harvest._triage` (`src/crawl/harvest.py:737`,
  once per `harvest.run`) and the registry op (`src/dispatch/registry.py:281`).
  So a Run holds at most one `triage.run`, on one store.
- Claim (2) was wrong. The wrapper keys by name, and `per_run(set)` would key
  by name too; no Run triages two stores; `companies.name` is `UNIQUE NOT
  NULL` (`0001_baseline.sql:11`); `ensure_mission` has no caller outside
  triage.
- Claim (1) is true, and it matters only in tests. The test that pins the
  contract, `test_an_unanswered_mission_score_is_not_retried_within_a_pass`,
  calls `triage.run` twice in one test, which `conftest._fresh_run` makes one
  Run, and asserts the second call asks again. Trial (scratch, restored):
  swapping the wrapper's set for `runstate.per_run(set)` fails both
  parametrisations.

Ruling: in production the two depths are equal; the wrapper keeps the
contract on the call it is documented against, needs no test change, and
REVIEW.md principle 12 names it as the sanctioned depth. Changed: a
`NEAR-MISS, DELIBERATE` note in `_once_per_pass`'s docstring (docstring only).
Would flip: a second `triage.run` per Run in production (per_run then wrong),
or a caller that wants one memo across several passes (per_run then right).

### D2. `group_by_company` in ingest: keep the inline loop

`group_by_company(rows, key: str)` buckets dict rows by a string key. The
ingest loop buckets `_Admitted` pairs, filters on the way in (`company_id`
set, description empty) and collects `.job`. Widening the helper to take a key
function adds a generic and a branch for one caller; reshaping by stamping
`company_id` on the job is the hidden-key pattern 991b896 removed. There are
nine inline `setdefault(..., []).append` groupings in `src/`, including the
sibling `triage.py:552` (pairs grouped by company id). The reviewer who called
it tolerable was right. Changed: a `NEAR-MISS, DELIBERATE` comment in
`ingest._hydrate_missing_descriptions` (CRLF kept).

### D3. Ingest test stub: use `serve`

`test_ingest_links_jobs_to_their_company_and_hydrates_per_board` now serves a
four-field greenhouse payload with `serve(fake_response(...))`, like its
neighbour `test_a_row_with_a_real_board_keeps_it`, and asserts on the request
log: exactly one GET, to the `acmedx` board, none for the company not in the
roster. Mutation checks (each restored):

| mutation | new test | old test |
|---|---|---|
| fetch the board once per job | fails | fails |
| never hydrate | fails | fails |
| `board_index` stops lower-casing titles | fails | **passes** |

The stubbed version could not see the third: it replaced `board_index` and
`board_match` outright. Changed: `tests/test_capture.py`, 19 lines.

### D4. Triage phase parameters: no context object; drop `n_free`

Real threading across `_judged`, `_free_gates`, `_hydrate`, `_body_gates`,
`_score`, `_write_verdicts`: `db` 4 phases, `companies` 4, `cutoff` 4,
`tracks` 4, `summary` 4, `mission_scorer` 3, `stamp` 2. The set
{db, companies, tracks, mission_scorer, cutoff} rides through three functions
and `_free_gates` only forwards it to `_judged`. A bundle would be one new
class with one construction site; `Collected`, the cited precedent, is a
return handoff, not an input context.

The one true redundancy: `n_free` always equals `len(decided)` at
`_body_gates` entry (`_hydrate` never touches `decided`). Removed
(`_body_gates` 10 -> 9 parameters). Proof: `pytest tests/test_triage.py
tests/test_harvest.py -s` (83 tests, 720 lines of output, 31 "body gates:"
lines), normalised for timestamps and paths, is identical before and after
the change and after the docstring edit; an off-by-one `n_free` changes it
("-1 more dropped"). The handoff's oracle files were not attached, so this
transcript diff stands in for them.

### D5. Efficiency findings: leave all three

- (a) Serial board fetch in ingest: the known callers pass one company per
  call (`ops/roster.py:70` loops per employer name; `capture.py:114` is one
  page). Overlapping would also need the per-host serialisation triage's
  `_hydrate` adopted on 2026-09-26. New machinery, no known gain.
- (b) Serial `judge`: cannot simply fan out. `judge`'s `gate()` runs in a
  thread under `_keyword_focus`, which snapshots, rewrites and restores the
  process-global keyword lists in `config`; two companies at once would race.
  Only the mission call could overlap (a prefetch phase), and it costs a
  network wait only on a company's first pass (the verdict is cached on the
  row). Sizing needs a session log; none is available here.
- (c) Two SQLite lookups per job in `_admitted`: real dead work when the geo
  gate is off, measured at 12.7 us (`get_company`) and 18.3 us
  (`company_id_by_name`) per call on a 300-company store: 13 ms per 1,000
  jobs, against a Claude fit call per job. Under REVIEW.md ("only where a
  profile shows it hot") it stays.

### Files changed by Task A

| file | change |
|---|---|
| `src/crawl/triage.py` | `n_free` derived inside `_body_gates` (3 lines net); D1 note |
| `src/ops/ingest.py` | D2 comment (3 lines, CRLF) |
| `tests/test_capture.py` | D3 test rewritten |

## 2. Task B: typing the fetched job

### 2.1 Inventory

There are two dict shapes the handoff calls one thing: the **fetched job**
(network in, ~36% of the `dict[str, Any]` sites) and the **stored job row**
(SQL out, ~26%). This design covers the first; the second is its own,
larger, and better-precedented type (2.7).

Keys on a fetched-job dict, by stage:

| key | type | set by | read by | mutated in place |
|---|---|---|---|---|
| `id` | str (external ingest input has none) | every producer; `ingest.py:71` fills it | gates, `store.sync_job_statuses`, `hydrate_rows`, triage `_hydrate`, upsert | ingest only |
| `title` | str | every producer | gates, digest, upsert | cleaned by `board_jobs`, `adapt` |
| `url` | str, `""` fallback | every producer | hydration, store, digest | ingest `:54`, hydrate |
| `location` | str, `""` or `"Unknown"` | every producer | geo gates, triage, digest | rescue, hydrate |
| `description` | str | every producer | gates, fit, store | hydrate, capped |
| `company` | str | feeds, `board_jobs`, page_capture; `adapt` drops it | runner, digest, `attribute_employers` | no |
| `ats` | str, or None from `_fetcher_shape` | `adapt`, `_fetcher_shape` | `company._board_of` (picks the engine) | no |
| `posted_at` | str or None; the engine omits it when empty | engine, feeds, jsonld; hydrate | store sync, upsert | hydrate fills |
| `remote_hint` | str | engine, feeds, jsonld; hydrate | `triage.py:289`, `locality.remote_signal_for` | hydrate fills |
| `head` | str | `_row_mapper` `engine.py:302` | `board_jobs` pops it `:147` | popped |
| `_free` | str | `_row_mapper` (spec `rescue.free`) | `_rescue`, `local_count` | stripped at `board_jobs` |
| `_employer` | 5 str keys: name, domain, slug, board, page_url | `getro.py:193` | `runner.py:500`, `apply.py:200,210` | no |
| `company_url` | str | `page_capture._job` | `capture.py:91,116` | no |
| `via` | str | `getro.py:191` | **nothing in `src/`**; `test_fetcher_parsers.py:630` | no |
| `company_id` | int or None | `attribute_employers` `apply.py:223,248` | runner upsert | yes (stamp) |
| `track_tag`, `remote_eligible`, `remote_signal`, `anchor_signal` | | `runner._gate_sweep_source` | matches digest, upsert | yes (stamp) |
| 8 fit columns | | `runner.py:562` `j.update(res.as_columns())` | sort key, digest, upsert | yes (stamp) |

`department` is a `RowField` and a spec field, but it never lands on a row:
`_row_mapper` folds it into `head`. Partial views that already exist:
`_fetcher_shape` (`triage.py:399`: `id`, `job_id`, `title`, `url`, `location`,
`description`, `ats`), the backfill stub (`backfill.py:142`: `title`, `url`,
`ats`, `description`), the `remote_signal_for` probe (`triage.py:747`:
`location`, `description`), and the external-ingest input (no `id`).

In-place mutation sites: engine (`board_jobs`, `adapt`, `_rescue`,
`_rescued`, `_apply`), `hydrate_rows`, ingest, both merges in `page_capture`,
`getro.py:275`, the runner's stamps and fit update, `attribute_employers`.
Dict-ness is barely used: in the 25 files holding fetched-job sites there is no
`{**job}`, no copy and no JSON dump on a job-named variable, and three
`.items()` loops.

The 377 lines are 409 `dict[str, Any]` occurrences. Bucketed by a table in
the scratchpad (`bucket.py`; heuristic, treat as +/-10%):

| bucket | n | where | deserves a type? |
|---|---|---|---|
| fetched job / engine row | 147 (36%) | engine 32, runner 28, pager 12, page_capture 12, feeds | this design |
| stored job row read back | 108 (26%) | triage 31, digest 20, maintenance 9, scoring 8, pipeline 8, store/jobs 7 | **yes, `JobRow`** (2.7) |
| spec / grammar / JSON records | 63 (15%) | engine internals 35, fields 16, decode 7 | no: data-driven, validated by pydantic at the edge |
| company / discovery dicts | 37 | discovery/pipeline 9, store/companies 8, detection shape (`signatures.pack` etc.) | the detection shape and candidate: small `TypedDict`s |
| stats / status | 26 | harvest 13 (fixed 14-key set), background 8 | harvest stats: yes |
| payloads (API, params, profile) | 25 | claude/api, registry, ddg | no: already at a pydantic edge |
| job write payloads | 3 | store/jobs, harvest | already `JobIn` |

### 2.2 Representation: measured

Prototype method: a scratch copy of `src/`; a closed `TypedDict` appended to
`rows.py`; every `dict[str, Any]` site bucketed as fetched-job rewritten by
position from the AST; mypy; then the hand edits the errors demanded. Scripts
are in the scratchpad (`proto.py`, `run_proto.sh`, `fix_A.py`, `astdiff.py`).

mypy error path for the leading candidate (one closed type, section A below):

| pass (design A) | errors | what was left |
|---|---|---|
| naive: every fetched-job-bucketed site | 116 in 21 files | half my mis-bucketing (specs, stats, raw API records typed as jobs) |
| bucketing repaired | 64 | boundary mismatches, producers, dynamic keys |
| raw records and mixed annotations excluded | 47 | 10 producers, 24 unconverted neighbours, 3 dynamic keys, 3 `update(**kw)`, 1 fit columns, 6 real inconsistencies |
| producers annotated, casts, neighbours converted | 14 | dynamic keys, `_link`, backfill stub, fit update, `run_track` return, `job_id`, a sort key |
| tried closing `FitColumns` for the fit update | 19 | 15 are "cannot extend closed base class": `JobIn` extends `FitColumns`; reverted, one cast instead |
| final | **0** | 143 sites in 26 files, 24 scripted hand edits |

On the recommended tree (A plus the B-lite split, below): 93 functions name
a fetched-job type in their signature and 36 locals are annotated. The
identity oracle: 95 of 102 files are AST-identical to the current tree once
annotations, `cast(T, x)` and imports are stripped (`astdiff.py`). The 7 that
differ hold exactly the deliberate edits: `engine.py` (`board_jobs` builds its
job explicitly, `out, fetched = [], 0` split for the annotation, `or ""` on
`location`), `getro.py` (`via`), `page_capture.py` (`update({...})`),
`runner.py` (the loop variable rename), `triage.py` (`job_id`), `ingest.py`
(`or ""` on `url`), and `rows.py`. The suite passes except the one test that
pins the removed `via` key.

| design | cost | what mypy then checks | verdict |
|---|---|---|---|
| **A** one closed `total=False` type with every stage's keys | above | key names, value types, `.get("typo")` (returns None on a closed type) | converges; no presence check |
| **A'** as A, but `id`, `title`, `url`, `location`, `description` required, the rest `NotRequired` | on top of the B-lite tree: **4 errors, all real**: three partial dicts (the backfill stub `backfill.py:142`, the ingest input before `id` is assigned `ingest.py:267`, the `remote_signal_for` probe `triage.py:747`) and the shared mutable view `_apply(Fillable)`, which fails because a mutable item's requiredness must match | as A, plus a partial dict passed as a whole job; it found **no** missing key in any of the twelve real producers | needs ~3 more types or views; benefit is future-proofing only |
| **B-lite** A plus `EngineRow` (engine-internal: `head`, `_free`), `Employer` (the 5-key record), `Fillable` (what a detail fills, shared by a row and a job) | 4 types; `board_jobs` builds its job explicitly (one cast fewer); 0 errors | as A, plus `head`/`_free` cannot appear on a fetched job | converges; recommended |
| **B-full** a type per stage, stamped match extending the job | not built. The closed-base rule bites: closing `FitColumns` gives 15 errors ("Cannot extend closed base class") because `JobIn` extends it; a stamped type must repeat keys (a second copy plus a names test), and in-place stamping becomes a copy, which needs an oracle | stamp presence | cost exceeds the check |
| **C** dataclass or model per stage | ~453 key accesses in 25 files, 12 producers, 48 test dict literals in 15 files, plus the 143 annotation sites | attribute names, presence, no casts | strongest alternative (2.7) |
| **D** type only the boundaries | not stable: a `TypedDict` is not assignable to `dict[str, Any]`, so a half-converted flow is red. In the 47-error pass, 24 were pure neighbour mismatches | little | rejected |

Friction TypedDict adds, each seen in the prototype:

- a comprehension cannot build one (`engine.py:151` the underscore strip and
  `:209` in `adapt`): a cast, or an explicit build;
- `d.update(k=v)` is rejected (`page_capture.py:203`); `d.update({...})` is
  identical;
- dynamic keys need Literal keys: `engine.py:780` (`row[key] = v`) is solved
  by narrowing `Detail.fields` from `RowField` (8 names) to the four names any
  spec fills, `FillField = Literal["location", "description", "posted_at",
  "remote_hint"]`; the two merge loops in `page_capture.py:311,565` iterate
  `j.items()` and take a cast;
- `job.get("url")` then `job["url"]` does not narrow (`company.py:86`); it is
  guarded at runtime, so this is friction, not a bug.

### 2.3 The odd keys

| key | decision | why |
|---|---|---|
| `_free` | engine-internal; lives on `EngineRow` only | read by `_rescue` and `local_count`, both before `board_jobs` |
| `head` | engine-internal; `EngineRow` only | popped at `engine.py:147` |
| `department` | stays a spec `RowField`; not a row key | input to `head`; nothing stores it |
| `_employer` | a declared field with an `Employer` type; keep the name | real cross-layer record; renaming touches getro, runner, apply and tests for nothing |
| `company_url` | declared optional field | two readers in `capture.py` |
| `via` | **remove** (owner call) | written once, read only by the test that asserts it |
| `job_id` on the fetcher shape | **remove** | `triage.py:399` sets it; nothing in `ats/`, `net/`, `match/`, `claude/` or `hydrate_rows` reads it; removing it left the suite green |

### 2.4 Single source of truth

`RowField` (a `Literal`) and a `TypedDict` cannot derive from each other under
mypy. The copy that must stay is verified the way `CompanyRow` is, by a names
test. Done for the engine row:
`tests/test_boards_spec.py::test_the_engine_row_names_the_spec_row_fields`
asserts `set(EngineRow.__annotations__) == (ROW_FIELDS - {"department"}) |
{"head", "_free"}` and that `FillField` is a subset. Still to add in phase 4:
that `FetchedJob` contains `ROW_FIELDS - {"department"}`, and that its fit
keys equal `FitColumns`'. Narrowing `Rescue.fields` to `FillField` makes the
spec loader reject a rescue naming `id`, `title`, `url` or `department`
(pinned by two entries in `REFUSED`); neither of the two specs that set
`fields` does.

### 2.5 Latent inconsistencies (reported, not fixed)

- `getro.py:191` writes `via`; nothing in `src/` reads it (`test_fetcher_parsers.py:630`
  is the only reader).
- `triage.py:399` `_fetcher_shape` sets `job_id` as well as `id`; unread.
- `engine.py:1018` `hydrate`: `job["location"] = (... or job.get("location"))`
  stores None if the job has no `location` key (the backfill stub has none).
  `ingest.py:54` `j["url"] = j.get("url") or match.get("url")` can store None
  the same way. Neither fires today; both are what a `str` type would flag.
  **Not a problem** (checked): `jobs.url` and `jobs.location` are nullable
  (`0001_baseline.sql`), every SQL read coalesces or filters NULL and `''` alike
  (`store/jobs.py:466,974,1126`), `location_unknown(None)` is True as `""` is,
  and triage reads `r.get("url") or ""`. The upsert overwrites a stored
  location with NULL instead of `''`, which no reader tells apart. So in
  phase 3 either declare the two keys `str` and add `or ""` at those two
  writes (safe: nothing distinguishes the values), or declare `str | None`
  and narrow at `company.py:86`; the first is smaller.
- `runner.py:787` `run_track` returns stored rows on the company-linked path
  and sweep matches on the other: one signature, two shapes. `runner.py:645`
  reuses the loop variable `j` for a watch hit and then for a ranked row.
- `digest/render.py:506` `j.get("company") or j.get("company_name")`: the only
  production caller of the matches digest (`runner._report_matches`) passes
  sweep matches, which never carry `company_name`.
- `spec.py:372` lets a rescue fill `id`, `title`, `url` or `department`; no
  spec does, and `_rescued` would write them onto the row.
- `spec.py:26` `RowField` includes `department`, which never reaches a row.
- Empty-location sentinels differ: `""` from the engine, `"Unknown"` from
  jsonld (`jsonld.py:150`) and greenhouse's spec default, `"See post"` and
  `"Posted <date>"` from two feeds; `location_unknown` covers them.
- `engine.py:203` `adapt` mutates its input dicts before whitelisting;
  `ingest.py:71` writes `id` onto the caller's dicts.

### 2.6 Plan

Typing is flow-closed: the phases must follow flow boundaries, not file
lists. Every phase: `python -m mypy`, `flake8 --select=F`, `pytest`, and
`astdiff.py` old vs new. Phase 1 needs nothing else, because the AST diff is
the identity proof.

| # | phase (one worker) | scope | identity check | risk / rollback |
|---|---|---|---|---|
| 1 | dead keys (**done**) | remove `via` (getro + its assertion) and `job_id` (`_fetcher_shape`) | grep proof; AST diff shows only those two dict literals; suite; triage transcript unchanged | minimal; revert |
| 2 | engine row (**done**) | `EngineRow` and `FillField` in `spec.py`; retype the engine and pager row chain (32 sites); `Rescue.fields` to `FillField`; `_apply` takes `EngineRow \| dict[str, Any]` until phase 3; names test; two more refused specs | mypy 0, 1528 passed, 101 of 102 files AST-identical (only `spec.py`, the new types). The AST diff cannot see the one deliberate runtime change, the pydantic field `Rescue.fields`, so the two refused specs pin it; four mutations caught | low; revert |
| 3 | the fetched-job flow (**done**) | `FetchedJob` (design A) and `Employer` in `rows.py`; `_link` takes a `Mapping`; 102 sites in 24 files by script, plus hand edits in the engine, page_capture, runner, ingest and backfill; `_apply` takes `EngineRow \| FetchedJob`, which ends the phase-2 union; a names test | mypy 0, 1529 passed. 97 of 102 files AST-identical to the previous commit; the 5 that differ are exactly: `engine.py` (`out, fetched = [], 0` split for the annotation, `or ""` on `location`), `page_capture.py` (`update({...})`), `runner.py` (loop variable rename), `ingest.py` (`or ""` on `url`), `rows.py` (the types). The whole suite's `-s` transcript, compared as a multiset of lines, equals the previous commit's except for the three lines that count the new test (a run of `HEAD` against itself differs in 40 lines of concurrent print order). Three mutations of the names test caught | medium (size); one commit, revert |
| 4 | tests and docs (**done**) | names tests from 2.4 (in phases 2 and 3); REVIEW.md: the Data shapes table row, the names-test mention, the closed-type and whole-flow rules, and a NEAR-MISS note on the dataclass | the names tests were mutation-checked; docs only | low |
| 5 | (separate track) `JobRow` for stored rows (**done**, section 4) | see 2.7 | typeof audit twin to `test_the_company_writers_store_each_column_as_declared` | medium |

### 2.7 Recommendation

Adopt **design A with the B-lite split**: `EngineRow` (phase 2), then
`FetchedJob` and `Employer` (phase 3; `Fillable` proved unnecessary). Built as
recommended: phases 1 to 3 are done.

Why not A' (required core keys): on the real code it flagged nothing (no
producer omits a core key) and cost four errors, so three more types or
views and a workaround for mutable views over required keys. It is a possible
tightening later, once a new producer has gone wrong. Why not per-stage types
everywhere (B-full): the repo closes record types and a closed type cannot be
extended, so every added stage repeats keys under a names test, for a check
(stamp presence) that only the digest and the final upsert would use. Why not
D: it does not stay green between steps.

What would flip it: (1) the owner wants presence checked, for the core keys
(then A') or for the stamp keys (`track_tag`, fit columns; then B-full for the
sweep match); (2) a bug shows up that a required constructor argument would
have caught: then C.

Strongest alternative, steelmanned: **C, a slotted dataclass for the fetched
job.** REVIEW.md's own table assigns "a mutable pipeline object" to a
dataclass, and this is one: four stages stamp it in place. The measurements
help it: the code barely uses dict-ness (0 `{**job}`, 0 copies, 0 JSON dumps,
3 `.items()` loops); construction is 0.1 to 8 us, immaterial for a
network-bound crawler; required fields make "a producer omitting a key a
consumer indexes with `[]`" a constructor error, the check A' buys with three
more types; and it removes the casts and the Literal-key workaround. Against
it: ~453 accesses, 12 producers and 48 test literals to rewrite, every one a
runtime change that the AST diff cannot prove identical, against zero runtime
change for A.

Next-largest type by count, outside this task: **`JobRow`** for stored rows
(108 sites: triage, digest, maintenance, scoring, pipeline, store/jobs), with
the `CompanyRow` playbook and a joined `RankedJob`. Harvest's 14-key stats
dict is the other clear one.

## 3. Corrections to the handoff

- Local counts were 1535 passed + 2 failed; on the example profile it is
  1527 passed + 10 skipped, 0 failed. `test_the_watch_section_lists_us_postings_only`
  passes there (it was "believed the same; not verified").
- "377" is a per-line count; the AST finds 409 occurrences.
- "About 51 signatures" is 93 functions naming the type, plus 36 annotated
  locals, on the prototype.
- D1's two scope claims: one wrong, one true but test-only (1).
- The session's checkout was 39 commits behind `origin/harvest` (`a97c43d`
  against `b10ab8a`), and had neither the typing series nor `docs/REVIEW.md`.

## 4. The `JobRow` track

Same day, at the owner's request. `JobRow` (the table's 40 columns, closed,
total) and `RankedJob` (what `ranked_jobs` returns) in `src/rows.py`;
`store.as_job` is the one place a `SELECT *` jobs row leaves sqlite, as
`as_company` is for companies. 68 sites in 8 files retyped; the stored-row
bucket of 2.1 was a heuristic count, and the sites it over-counted turned
out to be projections, stats or generic helpers, which stay as they were.

What the work found:

- **Two shapes, not one.** Ranked rows carry the company's mission and tags,
  a combined score and, once collapsed, `dup_*` keys, and lack `description`
  unless asked; a closed type cannot be extended, so `RankedJob` is a tested
  copy (as `CompanyRow` is of `CompanyIn`). `verify_top` mixes both (ranked
  rows plus floor candidates), so its helpers take `JobRow | RankedJob`.
- **Projections are not rows.** `same_posting` is called with two-key dicts,
  `dedup_jobs` groups four-column rows, and the backfill, closure-probe and
  rescore selections pick columns; they stay dicts or a `Mapping`.
- **A hidden key on stored rows.** `triage._hydrate` stamps `remote_hint`, not
  a column, onto the stored row so the geo gate can read it later in the pass.
  Existing behaviour, kept: `JobRow` declares it `NotRequired`, documented as
  the one non-column key. Worth its own fix (a value returned by the phase
  that owns it, principle 12) if the row is ever handed anywhere else.
- **A dead alias, removed.** `triage_pending` joined `c.name AS
  company_name_row`; nothing read it. Triage rows are now exactly `SELECT
  j.*`.
- **A nullable title, guarded.** `verify_top` passed and sliced `r["title"]`
  in three places, nullable in the schema, so a NULL title would have raised
  there; it now says `(r["title"] or "")`, as its neighbours already did.
  `apply_band_rows`' sort key gets `or 0.0` (its guard already guarantees a
  number, so that one changes nothing).
- **`group_by_company` is generic** over its row type, so `list[JobRow]`
  keeps its type through it.

Checks: mypy 0, flake8 clean, 1533 passed (four new tests); 95 of 102 files
AST-identical to the previous commit, the 7 others being the edits above; the
whole suite's `-s` transcript equals the previous commit's as a multiset
except the test count. The storage-class audit now covers every `JobRow`
column, after every job writer. Six mutations were caught (a key added to
`JobRow`, one dropped from `RankedJob`, a column mistyped, `triage_pending`
selecting two columns, `ranked_jobs` ignoring `with_description`, and a
writer storing text in an integer column); a first attempt at the last one,
an integer into a text column, is invisible to any audit because SQLite
coerces it.
