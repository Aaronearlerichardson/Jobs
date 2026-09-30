# Code-health review

What a reviewer (a person or an agent) checks when a phase of work is
finished, so every review asks the same questions. The goal: **a change must
not be overly additive.** New code folds into the module that already owns
the concept; it does not open a parallel path beside it.

## How a review runs

- **When:** after a phase (one coherent change) is integrated and green,
  before it is committed.
- **Snapshot first:** `git diff --binary`, plus a copy of any new files, so
  the review's own edits can be diffed against what it was given.
- **Scope:** review what added code or changed behaviour. Annotation-only
  hunks need no line-by-line read.
- **Output:** findings ranked by additivity (new modules, parallel code
  paths, duplicated helpers first).
- **Edits:** apply only behaviour-identical folds and renames. Anything else
  is reported, not applied. Keep `python -m pytest`, `python -m mypy` and
  `python -m flake8 --select=F` green. Never commit.
- **Proof:** "behaviour-identical" is shown by running the old and the new
  code on the same inputs and comparing outputs, not by a green suite alone
  (ca360f6).

## Principles

The shas are the commits on `harvest` that set each one.

1. **Fold duplication into the module that owns the concept**, never into a
   new `utils` (4cc7495).
2. **Two helpers for one job need a stated decision rule, enforced by a
   test** (e1e8723 set the pattern for `fan_out` and `drain`; the async
   migration retired `drain`, and `test_threads_live_in_the_four_sync_wrappers`
   is the pattern's current form).
3. **Prefer deleting** what nothing calls (9f3a971, ddc0272, 25a85f2).
4. **Split long functions along their existing banners**, and prove identity
   with a differential probe (ca360f6).
5. **Patch and import at the definition site** (63798a6).
6. **One writer, one definition** (5525f88 `fetch_failed`, e814fac
   `_WRITE_LOCK`).
7. **Docstrings state contracts; evidence goes under `Notes:`**, never a
   TODO. See [DOCSTRINGS.md](DOCSTRINGS.md).
8. **Derived lists, not hand-kept ones** (ccbc9fc, cc86d84). A list that
   restates a table, a spec or a type is a second definition to drift.
9. **Test scaffolding is defined once** (`conftest.fake_response` 7b57261,
   `keep_store_open` fbdf0f0, the parametrised dead-source contract in
   `tests/test_fetcher_parsers.py` 245269e).
10. **Record near-misses.** A merge that was considered and refused gets a
    `NEAR-MISS, DELIBERATE` comment saying why (aa78816), so it is not
    proposed again. Look for one before proposing a merge.
11. **Match each file's line endings.** Many files are CRLF; `git diff
    --stat` should show only the lines you changed.
12. **Fix at the right depth.** A parameter added to five signatures, a
    special case on shared infrastructure, or a flag stamped on a data dict
    is a symptom patch when the state has a natural owner or a general
    mechanism exists. Prefer the per-run state in `src/runstate.py`, a
    wrapper at the boundary the state belongs to, or a value returned by the
    phase that owns it. The once-per-pass mission memo was first a `tried`
    set threaded through five functions; it is now `triage._once_per_pass`,
    one wrapper of the scorer in `triage.run`, and no signature changed.

## Red flags

A new module where one of these already owns the concept:
`net/http`, `net/parallel`, `ats/board` (the engine), `ops/maintenance`
(shared helpers; the op families live in `ops/status`, `scoring`,
`backfill`, `ingest`, `repair`, `rekey`), `dispatch/registry`, `store/jobs`.

A helper rebuilt beside one that exists: `fan_out`, `fetch_all`,
`get_json`, `fetch_failed`, `board_jobs`, `loc_ok`, `group_by_company`,
`store.batch`, `coords`, `widen_keywords`, `stable_id`,
`net.util.host_of`, `net.util.origin_of` (with `urljoin`, for any href
join), `net.http.HostBreaker`, `signatures.detect(leads=False)`.

Also:

- a fetcher that opens its own `requests.Session` (it bypasses robots,
  logging and `serve`);
- a hand-kept test list where a registry could be parametrised over;
- `conn = store.connect()` without `with closing(...)`, `track_store` or
  try/finally;
- a parallel code path where a parameter would do;
- a new module-global memo, cache or thread-local (per-run state lives in
  `src/runstate.py`; 5124ace moved the discovery and scoring memos there);
- a `tools/` script that should be an op in `dispatch/registry` (ops ship in
  the exe), or the reverse;
- test-local `Response` stubs or fake sessions (use `conftest.fake_response`
  and `conftest.serve`; per-run memos are reset by the autouse
  `_fresh_run` fixture);
- docstring boilerplate that restates the signature;
- a `model_dump()` of a validated model that is then cast to, or re-described
  as, a hand-written dict type (see Data shapes);
- a dict-shaped copy of a table or a model with no test that checks its
  names and types;
- a dead re-export in `store/__init__` or `config/__init__`;
- an import that points up the layers;
- a user-specific id, keyword or name in code (it belongs in `profile.toml`).

## Where things belong

- A new board platform: a spec in `src/config/boards.py` `BOARDS`. No
  per-platform module.
- A fetch-level signal: `src/net/http.py`, beside `fetch_failed`.
- Closure rules: arguments on `store.jobs.sync_job_statuses`.
- A scorer or hydrator parameter: threaded like triage's `mission_scorer`
  and `hydrate_fn`.
- A repeatable op: an op-family module in `src/ops` plus an entry in
  `src/dispatch/registry.py` (a real function reference and an `OpParams`
  model).
- Schedule math: `harvest.py::run_forever`, with a doctest.

## Data shapes

The rule: **validate once, at the edge; after that keep the most precise
type you already have; write out a copy of a shape by hand only where a
checker or a test verifies the copy.** Choose by where the data comes from,
not by habit.

| The data is | Use | Here |
|---|---|---|
| input from outside: a file, the environment, an HTTP body, a Claude reply, board-spec config | a pydantic model, validated once at the edge | `config/profile_schema.py`, `config/secrets.py` (`Settings`), `ats/board/spec.py`, `claude/reply.py`, `web/routes.py` (`_Body`), `dispatch/registry.py` (`OpParams`) |
| a validated model the program keeps using | that model, frozen, read by attribute | `RuntimeTrack` |
| a SQL row or patch, or anything that must stay a dict (SQL parameters, `{**row}`, JSON, `**kwargs`) | a `TypedDict`; a missing key means "leave what is stored" | `src/rows.py`: `CompanyRow` (read), `CompanyIn` (write), `JobRow` and `RankedJob` (read), `JobIn` (write), `FitColumns`, `BoardHit`, `FetchedJob` and `Employer` (a fetcher's job); `ats/board/spec.py`: `EngineRow` (the board engine's row, before `board_jobs`) |
| working state with behaviour, or a mutable pipeline object | a dataclass | `FitResult`, `Candidate` |
| a pair or triple handed between two functions | a `NamedTuple` | `Collected`, `_Admitted` |

What a reviewer holds a new shape to:

- **No dump-and-copy.** `model_dump()` followed by a cast to a hand-written
  `TypedDict` copies the model, and only a names test can keep the copy
  honest. Keep the model. (`TrackDict` was this; `RuntimeTrack` replaced it.)
- **Describe a column set once.** Derive lists from the type:
  `upsert_company` writes exactly `CompanyIn`'s keys. A hand-kept list that
  restates a type or a table is a second definition (principle 8).
- **A hand-written mirror of a table needs a test on names and types**
  (`test_the_company_row_model_is_exactly_the_companies_columns` and
  `test_a_row_model_types_each_column_as_the_table_declares_it`, in
  `tests/test_store.py`). `CompanyRow` is the accepted example of a
  deliberate second copy: it repeats `CompanyIn`'s columns because a
  `TypedDict` cannot make an inherited optional key required, and those
  tests check both against the table and against each other. `JobRow` (the
  table) and `RankedJob` (what `ranked_jobs` returns) are the same kind of
  deliberate copy, since a closed type cannot be extended:
  `test_the_job_row_models_are_the_jobs_columns` and `TestJobReaders` check
  their names and each reader's keys. The fetched
  job and the engine row mirror the spec's row fields and `FitColumns`
  instead of a table:
  `test_the_fetched_job_names_the_row_fields_and_fit_columns` and
  `test_the_engine_row_names_the_spec_row_fields`, in
  `tests/test_boards_spec.py`, check their names.
- **SQLite does not enforce declared column types**, so the one cast where a
  row leaves sqlite is honest only with an audit:
  `test_the_company_writers_store_each_column_as_declared`, and its jobs
  twin, check what the writers actually store.
- **A projection is not a row.** A query that selects some columns
  (`stale_body_rows`, the closure probe's rows, `dedup_jobs`' groups) returns
  a plain dict; a function that takes a row or such a projection reads a
  `Mapping` (`same_posting`, `age_tag`). `JobRow` promises every column, so
  typing a projection as one makes a subscript that raises look checked.
- **A read-only parameter takes a read-only view, not `dict`.** A
  `TypedDict` is not assignable to `dict[str, Any]`, only to a `Mapping`. A
  function that takes either a hit or a row reads `BoardCoords`.
- **Close record types.** mypy checks subscripts (`row["typo"]`) but not
  `.get("typo")` on an open `TypedDict`: the typing spec allows it and it
  comes back as `object`. On a `closed=True` one it comes back as `None`, so
  the typo surfaces where the value is used. `closed` and `ReadOnly` come
  from `typing_extensions`, since CI runs Python 3.12 to 3.14, and need mypy
  2.3 or later. A closed `TypedDict` cannot be extended with new keys, and an open one
  is not assignable to it, so `FitColumns` stays open (`JobIn` extends it) and the
  one place a fit score updates a `FetchedJob` takes a `cast`.
- **Convert a flow whole.** A `TypedDict` is not assignable to `dict[str,
  Any]`, so a producer and every function that receives its dicts change in
  one commit; a half-typed flow does not pass mypy. Such a phase is
  annotation-only, and shown to be: parse the old and the new tree, strip
  annotations, `cast(T, x)` and imports, and compare the ASTs (what still
  differs is the edits meant), and compare the suite's `-s` output as a
  multiset of lines (concurrent tasks print in a varying order).
- **NEAR-MISS, DELIBERATE: the fetched job is a `TypedDict`, not a
  dataclass**, though four stages stamp it in place. It is barely used as a
  dict (no `{**job}`, no copy, no JSON dump, three `.items()` loops), so a
  dataclass would work; but it means about 450 key accesses, a dozen
  producers and 48 test literals rewritten, each a runtime change the AST
  comparison above cannot prove identical, against none for the `TypedDict`.
  Revisit if a bug appears that a required constructor argument would have
  caught (docs/fetched-job-typing-design.md, 2.7).
- **Speed does not decide.** Building a dict, a dataclass or a pydantic
  model took 0.1 to 8 microseconds an object in a local timing
  (2026-09-29), and the crawler is network-bound. Choose by the table, and
  by a profile only if one shows a construction hot.
- **No msgspec, attrs or code generator for this.** Each is a third
  modelling library or a build step that costs more than a type-aware test
  for two tables.

## Performance

The mechanical rules are enforced by `tests/test_invariants.py`; the
judgment rules and the build flags are in [PERFORMANCE.md](PERFORMANCE.md).
Two things a reviewer holds to:

- The crawler is network-bound. Change code for speed only where a profile
  shows it hot, and prove the output did not change.
- Memory is cheap here. Do not flag retained memory (a closure, a cache, a
  bigger structure) as a cost, and do not justify a design by saving it.
  Work done once at startup is acceptable when it buys better organisation.

## Field-grammar creep

Every platform is on the engine. Flag a new operator in the field grammar
(`src/ats/board/fields.py`) unless at least two platforms use it.
Conditionals in config are behaviour in config: a one-platform oddity is a
named mechanism or a `kind`, and the non-uniform sources (`hnhiring`,
`websearch`, the feeds) stay code.

## Docstrings

A docstring may only state a claim that a doctest, a named test or an
invariant enforces; everything else goes under `Notes:`. The full standard,
including how to choose, is [DOCSTRINGS.md](DOCSTRINGS.md). A new docstring
is part of the change under review.

## What a test already enforces

A reviewer need not check these by eye; `tests/test_invariants.py` fails
first:

- imports point down the layers;
- threads live only in the four sync wrappers;
- store connections close on every path;
- a module-level name has more than one reader, and a single-use private
  helper is inlined;
- async code never blocks and nothing swallows cancellation or drops a task;
- the one client session is made in `net/http`, with a timeout;
- the environment is read only through `config.SETTINGS`;
- board specs stay JSON;
- the mechanical performance rules, and no `assert` or `__debug__` in
  compiled code.

Data shapes have their own tests in `tests/test_store.py`: the row
`TypedDict`s name exactly their tables' columns with the declared types, and
the writers store each column as declared.
