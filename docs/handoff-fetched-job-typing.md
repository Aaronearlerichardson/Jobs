# Handoff: type the fetched job, and settle five review disputes

Written 2026-09-29 for a fresh session. Branch `harvest`. The code is as of
`991b896`; the commit after it, `2fb5fe0`, only adds this file and deletes
the old scratch-key handoff. Delete this file when the work is done; the
last handoff went stale within a day.

## For whoever sends this (not for the agent)

- All code is on `origin/harvest` at `991b896`. `2fb5fe0` (this file) was NOT
  pushed when this was written: push it, or paste this text into the
  session, or the cloud session will not see it.
- `profile.toml` is gitignored, so a cloud checkout runs on
  `profile.example.toml`. The two tests that fail locally (section 1)
  should pass there; expect a fully green run and tell the agent if not.
- Some working files exist only on the local machine (section 8). Attach
  the ones you want the cloud session to have; everything else is rebuilt
  from the descriptions below.

## 1. Where things stand (verified at 991b896)

- `python -m mypy`: 0 errors, no `# type: ignore` in the typing series.
  `python -m pytest`: 1535 passed, 2 failed locally. Both failures are
  profile-dependent and PRE-EXISTING (they fail at older commits too):
  `tests/test_triage.py::test_geo_drop_before_hydration_unless_trusted`
  (verified to pass on the example profile) and
  `tests/test_harvest.py::test_the_watch_section_lists_us_postings_only`
  (believed the same; not verified).
- `dict[str, Any]` in `src/`: 377, down from about 499 at the start of the
  typing series. Largest clusters: `ats/board/engine.py` 61,
  `crawl/triage.py` 34, `crawl/runner.py` 29, `digest/render.py` 20,
  `crawl/harvest.py` 18, `ats/board/fields.py` 14, `crawl/page_capture.py`
  12, `ops/maintenance.py` 11.
- Typed already (all in `src/rows.py` unless noted): `CompanyRow` (total,
  closed; what store readers return), `CompanyIn` (closed; what
  `upsert_company` writes, its column list is derived from it), `BoardHit`
  (closed; a resolver's answer), `BoardCoords` (read-only view a hit and a
  row both fit), `HandleColumn` (a Literal so config-driven `company[col]`
  type-checks), `JobIn` and `FitColumns` (job rows handed to the store),
  `RuntimeTrack` (frozen pydantic model in `config/tracks.py`, replaced the
  old `TrackDict`), and the profile's fixed-field models.
- Typing series commits, oldest first: b88a77f (BoardHit), 9412930
  (CompanyRow), c58fb44 (docs/REVIEW.md), 491231d (derive upsert columns,
  validate imports), fb85ea9 (Track before-validator fix), 483f864
  (column storage-class audit), 1a952f4 (close BoardHit, view type),
  690bdef (keep profile models), 13c51fc (Candidate slug), 77983ff
  (RuntimeTrack), 569219b (fixes from a code review), 8e31288 (follow the
  literature review), 6ff767b (CompanyIn/CompanyRow split), 991b896
  (job scratch keys removed).
- The scratch keys `_row`, `_us_eligible`, `_tried`, `_new` are gone.
  `_tried` became `stats["tried"]` (returned by `hydrate_rows`); `_new`
  became `Collected.new_ids`. Do not reintroduce hidden keys on dicts.
- Behaviour changes already landed that you should know about: imports are
  stricter (a value a column's type rejects, such as `5.5` in an integer
  column, now fails the whole import); `pending_companies` selects every
  column, so the review-queue JSON gained a few keys; a NULL `active`
  loads as 0; `Candidate` has both `slug_guess` and `slug`.

## 2. Rules of this repo (read these first)

- `docs/REVIEW.md` (principles, red flags, "Data shapes"), `docs/DOCSTRINGS.md`
  (a docstring states only what a doctest, a named test or an invariant
  enforces; the rest goes under `Notes:`), `docs/PERFORMANCE.md`.
- The data-shape rule: validate once at the edge; keep the most precise type
  you already have; hand-write a copy of a shape only where a checker or a
  test verifies it. pydantic at trust boundaries, `TypedDict` for
  dict-shaped trusted data (SQL rows and patches, `{**row}`, SQL
  parameters, JSON), dataclass for working state, NamedTuple for handoffs.
  Speed does not decide (constructing a dict, dataclass or model took 0.1
  to 8 microseconds in a local timing; the crawler is network-bound).
- Checks before you call anything done: `python -m mypy` (needs mypy 2.3 or
  later), `python -m flake8 --select=F src tools tests *.py`,
  `python -m pytest` (pytest.ini already passes `-q`; do not add another,
  or the passed-count line disappears). CI runs Python 3.12, 3.13, 3.14, so
  `closed` and `ReadOnly` come from `typing_extensions`.
- Line endings are MIXED (many files CRLF, some LF). Never rewrite a whole
  file (no `sed -i`, no `write_text`); use an editor tool and confirm
  `git ls-files --eol -m` shows no file whose index and worktree endings
  differ and `git diff --stat` shows only the lines you changed.
- No new modules and no new single-use module-level names; fold into what
  exists (`tests/test_invariants.py` enforces several architecture rules).
- Prove behaviour identity, do not assume it. The repo's method is a
  differential oracle: export the pre-change tree (`git archive HEAD`, copy
  `profile.toml` if present), run seeded scenarios through the old and the
  new code on a temp store with stubs for the network and Claude, dump
  every observable (return values, store rows, printed output, digest
  files) to JSON, and require the two dumps to be identical. Mutation-check
  any test you add (break the code, watch it fail, restore).
- Owner preferences: few tokens (mechanical tools first; one Sonnet worker
  per phase with a short exact brief; Opus only for judgment-heavy design or
  scoped review), concise docstrings, few and short tests with doctests
  where possible, no personal names or ids in code (they belong in
  `profile.toml`). Confirm with the owner whether this session should
  commit or push; the previous cloud series committed one commit per phase.

## 3. Task A: settle five disputes (no verdicts exist yet)

An earlier `/simplify` run (four review agents) reviewed the mission memo
and the ingest pairs. The main session overrode some findings. An
independent ruling was started and stopped before it produced anything.
Rule on each: verify every fact either side relies on (the main session may
have stated something wrong), decide, and change code only where you rule
against the current code AND the change is behaviour-identical or
test-only, with oracle proof. Otherwise recommend.

**D1. Where the once-per-pass mission memo lives.** `triage.judge` runs
twice per company per pass (free gates, then body gates) on the same
company rows; a failed or empty mission attempt must not be retried within
a pass but must be retried on the next `triage.run`. Two reviewers preferred
`runstate.per_run(set)` (the repo's per-Run memo mechanism, used by
`_FOREIGN_ANNOUNCED` and `_JS_NOTICES`). Others preferred a wrapper. Current
code: `triage._once_per_pass`, a wrapper around the scorer built once in
`run`, keyed by company NAME (`companies.name` is `UNIQUE NOT NULL`). The
main session rejected `per_run` claiming it (1) widens the scope from one
`triage.run` call to one Run and (2) would key ids across stores. Those two
claims were never verified: check how many `triage.run` calls one Run can
make (harvest pass at `crawl/harvest.py` ~732, the op registry entry at
`dispatch/registry.py` ~281, tools), whether a Run ever triages two stores,
and whether name-keying is safe. Which is the right depth (principle 12)?

**D2. `group_by_company` in ingest.** `ops/ingest.py`
`_hydrate_missing_descriptions` uses a 4-line inline `setdefault` loop over
`_Admitted` (job, company_id) pairs instead of the shared
`ops/maintenance.py::group_by_company(rows, key)`, which indexes dict rows
by a string key (other callers, for example `ops/status.py`, pass strings).
One reviewer called that re-inventing a helper the red-flag list names;
another called it tolerable. Options: keep, widen the helper to accept a key
function, or reshape.

**D3. The ingest test's board stub.**
`tests/test_capture.py::test_ingest_links_jobs_to_their_company_and_hydrates_per_board`
monkeypatches the private names `ingest.board_index` and
`ingest.board_match`. A reviewer said to use `serve(fake_response(...))`
like the neighbouring tests, exercising the real functions and reading the
fetch count from the request log. The main session kept the monkeypatch
fearing coupling to fetcher response shapes. Try the `serve` version; keep
whichever is the better public-contract test.

**D4. A per-pass context object in triage.** `_body_gates` takes 10
positional parameters (`db`, `companies`, `survivors`, `tracks`,
`mission_scorer`, `decided`, `summary`, `n_free`, `waiting`, `cutoff`), and
several of these ride through the phase functions. An altitude reviewer
suggested bundling them (precedent: `crawl/runner.py` `Collected`,
a NamedTuple). The main session deferred it. Count the real lists now and
decide under "fold, do not add".

**D5. Efficiency findings called out of scope.** (a)
`_hydrate_missing_descriptions` awaits `get_company` then `board_index` (a
network fetch) per company serially, where `fan_out` would overlap them;
(b) `_judged` awaits `judge` company by company, so the mission call is
serial; (c) `_admitted` runs two SQLite queries per job
(`company_id_by_name`, `get_company`) even when the track's geo gate is off
and the row is never read. The crawler is network-bound and speed work
needs a profile, but (a) and (b) are network waits.

## 4. Task B: design how to type the fetched job (design first)

Do NOT implement before the design is written and the owner has seen it.

The fetched job is a plain `dict[str, Any]` that fetchers produce and that
then flows through many stages (about 51 signatures). Known, verify against
the code:

- Fetcher keys: `id, title, url, location, description`, optionally
  `posted_at, remote_hint`, a derived `head` (title plus department), an
  engine-internal `_free`, and for the Getro feed an `_employer` record.
  The board spec's row-field names are the Literal `RowField` /
  `ROW_FIELDS` in `ats/board/spec.py`: relate them (single source of truth?).
- `_free` is a spec-declared internal field (`engine.py` ~281 builds readers
  for spec keys starting `_`), set in the row mapper and read only by the
  engine's location rescue and local count; `board_jobs` strips underscore
  keys before rows leave the engine (`engine.py:172`). `_employer` is a real
  cross-layer record: set in `ats/feeds/getro.py`, checked in
  `crawl/runner.py`, used by `discovery/apply.py::attribute_employers`.
  A `TypedDict` can declare underscore keys, so neither blocks typing.
- Later stages add keys IN PLACE: gating (`track_tag`, `remote_eligible`,
  `remote_signal`, `anchor_signal` in `runner._gate_sweep_source`), fit
  scoring (`FitColumns`, `resume_fit_score`, `fit_reason`), triage's
  `_fetcher_shape` (a stored row viewed as a fetcher job for
  `hydrate_description` / `needs_detail`), hydration writing `description`,
  `location`, `url` back, then `ops/maintenance.py::_scored_row` shaping a
  `JobIn`, and `digest/render.py` reading job dicts.
- Fetchers come in kinds: the spec-driven board engine, the feeds
  (`ats/feeds/`: getro, hnhiring, remoteok, remotive, usajobs, websearch,
  discourse, rssfeed, careeronestop), custom and JSON-LD pages, and
  `crawl/page_capture.py`.

Questions to answer with evidence (counts, measurements, a prototype):

1. **Inventory:** every key that ever lives on a fetched-job dict, its type,
   which stage adds it, who reads it, whether it is mutated in place. Also
   bucket the 377 `dict[str, Any]` sites (fetched jobs vs spec or config
   data, JSON payloads, stats, HTTP responses, already-typed store rows,
   summaries); the non-job buckets are out of scope, but say which deserve
   a type.
2. **Representation:** compare (A) one closed `total=False` `TypedDict`
   with every stage's keys, (B) stage-specific `TypedDict`s with
   `ReadOnly` views like `BoardCoords`, (C) a dataclass or frozen model per
   stage, (D) type only the boundaries. Cost each: sites and signatures
   changed, call-site conversion, what mypy can then check, the in-place
   mutation problem (`job["track_tag"] = ...` needs the key declared),
   interaction with `{**job}`, `.get`, JSON and upsert paths, hot-path cost.
3. **Prototype and measure** in a scratch copy of `src/`: define the leading
   candidate in `rows.py`, retype the obvious producers and consumers, and
   record the mypy error count and categories. The company-row precedent: a
   naive first pass gave 46 errors and converged once dynamic key access
   was Literal-typed and hit-or-row readers used a read-only view.
4. **Odd keys:** `_free`, `_employer`, `head`, `department`: declared field,
   internal-only key, or remove? Decide from the code.
5. **Single source of truth:** can the fetcher keys derive from `RowField`
   (or the reverse)? What test verifies a copy that must stay?
6. **Plan:** phases one Sonnet worker can do, each with scope, its
   behaviour-identity check (which oracle, any new one), risks and
   rollback, ordered by risk and payoff.
7. **Recommendation:** one design and the first phase, what evidence would
   flip it, and the strongest alternative steelmanned.

Also report, without fixing, any latent inconsistency you see (for example a
producer omitting a key some consumer indexes with `[]`), with file and line.

## 5. Other loose typing ends (lower priority)

- The "detection" shape `{ats, slug or triple, careers_url}` (from
  `signatures.pack`, the sniffer, `websearch_board`, `add_board`): still
  `dict[str, Any]`; a small `TypedDict` would cover it.
- `coords.board_slug` returns `Any` (a tuple for a hit, a string for a
  row); `populate_companies` and `_miss_row` payloads are plain dicts.
- SQLite does not enforce declared column types; a typeof audit test covers
  the writers (483f864). STRICT tables were considered and not done.

## 6. What the main session got wrong or could not verify

- The D1 scope claims (section 3) are unverified.
- It first called the runtime `create_model` in `import_companies` a
  hand-kept duplicate of the column list; it was derived from
  `PRAGMA table_info`, so the gain of validating imports through the
  `TypedDict` is value validation, not de-duplication.
- The two profile-dependent test failures were verified against the
  example profile for only one of them.
- The timing figures (0.1 to 8 microseconds per object) come from a scratch
  benchmark in a literature review, not from a test in the repo.
- The design pass and the dispute ruling were both stopped before they
  produced output: there is no draft to build on.

## 7. Decisions the owner already made (do not relitigate)

`TrackDict` is removed in favour of `RuntimeTrack`; stricter import
validation is accepted; the old scratch-key handoff was deleted; the
disputes are to be ruled on independently; the fetched-job work starts with
a design pass.

## 8. Local-only files (not in the repo; attach if wanted)

All under `C:\Users\Jakda\AppData\Local\Temp\claude\C--Users-Jakda-git-Jobs\5a376302-ec5c-4449-a0cc-537632492390\scratchpad\`:

- `typing-literature-review.md`: the 62-source review behind the data-shape
  rule (sections 3 and 5 matter most).
- `test_zz_oracle_new.py`, `test_zz_oracle_partial_new.py`: seeded oracles
  for `triage.run` and `ingest_external_jobs` on the current tree.
- `test_zz_runner_oracle.py`: oracle for the crawl and report path
  (`run_track`), 90 scenarios.
- `old3\`: a copy of the working tree taken before the dispute review.
