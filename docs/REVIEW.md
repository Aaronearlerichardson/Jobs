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
