# Docstring standard

## The rule

> **A docstring may only state a claim that a doctest or an invariant test
> enforces. Everything else — rationale, history, design war stories — goes
> under an explicit `Notes:` heading, which is understood to be unverified
> prose.**

That is the whole standard. The rest of this file is how to apply it
mechanically.

### Why

Several docstrings in this repo described behaviour the code did not have.
One of them — the comment above `discovery/ats_dork.py`'s activation line,
which claimed the "same activation rule as every other add path" while the
code beneath it hard-coded two tier names — sent a real investigation down
the wrong path for hours. Prose rots because nothing checks it. Executable
prose does not.

So: everything above a `Notes:` heading is a **contract**, and CI breaks if
it stops being true. Everything under `Notes:` is a **story**, and the reader
knows to distrust it. The heading is the entire signal.

## The skeleton

```python
def canonical(tag):
    """<One line: what it returns, in the present tense.>

    <Optional: a sentence or two of behaviour, each one demonstrated below.>

    >>> canonical("watch")
    'watch'
    >>> canonical("  NC_Local ")
    'local'

    <Prose introducing the next group of examples — edge cases, failure
    modes, the surprising bit.>

    >>> canonical(None), canonical("")
    ('', '')

    Notes:
        Unverified prose: why it is this way, what it used to be, what
        broke. Never a behavioural claim — if it is worth claiming, it is
        worth a doctest.
    """
```

Rules for the skeleton:

- **Summary line first**, present tense, no trailing rationale.
- **Examples are interleaved with prose**, not dumped in one block at the
  end. Each paragraph makes one claim; the examples under it prove that
  claim. A reader should be able to delete the prose and still have a test,
  and delete the tests and still have a readable paragraph.
- **`Notes:` is last**, indented, and appears only when there is genuinely
  unverifiable context worth keeping.
- No `Args:`/`Returns:` boilerplate that restates the signature. Document a
  parameter only where its meaning is not obvious from its name.

## Good example (from this codebase)

`core/store.py:combined_score` — every sentence above `Notes:` is backed by
the example under it, including the two edge cases that used to be
undocumented (`None` inputs, negative inputs):

```python
    """Geometric mean sqrt(fit * mission) of the resume-fit and company
    mission scores (both 0..1).

    >>> combined_score(0.25, 0.64)
    0.4

    Floats are compared at a stated precision, never by their full repr:

    >>> round(combined_score(0.9, 0.2), 4)
    0.4243
    >>> round(combined_score(0.5, 0.5), 4)
    0.5

    Those two lines are the point of the geometric mean: it punishes
    imbalance, so a strong fit at a weak-mission company (0.42) ranks below
    a job that is merely solid on both axes (0.50).

    A missing factor is unranked, NOT zero:

    >>> combined_score(None, 0.9) is None
    True
    """
```

Note what happened to the original prose "the geometric mean punishes
imbalance: (0.9*0.2 -> 0.42 < balanced 0.5*0.5 -> 0.50)". Those numbers were
a *claim*. They are now two doctest lines with a sentence that reads the
result. Same words, now enforced.

## Bad example (from this codebase, before the fix)

```python
        # Same activation rule as every other add path (populate_companies,
        # add_names, resolve_leads, add_manual_job): the profile's active
        # tiers, multi-division conglomerates, and — critically — `tier is
        # None` ...
        active = 1 if (tier in ("healthcare-tech", "health-bio-science")
                       or config.is_multi_division(name)) else 0
```

Three failures at once:

1. It asserts a **cross-module** fact ("same rule as every other add path")
   that no doctest could ever check — that is an invariant test's job.
2. It asserts a **behavioural** fact (`tier is None` keeps the row active)
   that the code beneath it does not implement.
3. It is confident and specific, which is what made it load-bearing to a
   reader.

The fix was not to correct the comment. It was to delete the comment, extract
the rule into `core.claude.is_active_mission` with a doctest per arm, and add
`tests/test_invariants.py::test_activation_rule_is_not_re_implemented` so the
cross-module claim is enforced by an AST scan instead of asserted by prose.

## Choosing: doctest, invariant test, or `Notes:`

Work down this list and stop at the first match.

| The claim is… | Put it in… |
| --- | --- |
| about the return value of ONE pure function, for inputs you can write literally | a **doctest** in that function |
| about a function that needs a DB, network, LLM, thread, or filesystem | a **pytest test** in `tests/`, and reference it from the docstring by name |
| about how several modules relate ("every add path does X", "only this module may Y", "these two lists stay in sync") | an **invariant test** in `tests/test_invariants.py` |
| unfalsifiable, historical, or motivational ("this used to be serial", "the model can't see client-rendered pages") | **`Notes:`** |

Two sharper cuts:

- **"Pure" means** no `requests`/`SESSION`, no `sqlite3`, no thread pool, no
  `open()`, no `core.claude` API call, and no dependence on wall-clock time or
  randomness. Roughly 207 of the 534 functions here qualify. If you need a
  fixture, it is not a doctest.
- **If a doctest would need a mock, it is the wrong tool.** Move it to
  `tests/` rather than monkeypatching inside a docstring — the docstring is
  documentation first, and a reader should be able to paste the example into
  a REPL and see that output.

### Profile-dependence — the trap specific to this repo

Almost everything here reads `profile.toml`, falling back to
`profile.example.toml`. CI runs on the **example** profile; you are probably
developing against a real one. So:

- **Never** hard-code a value that comes from the profile — a tier name, a
  city, a keyword, a company name. Those doctests pass for you and fail in CI.
- Pass an explicit override where the function supports one:
  `is_active_mission("green", "Nowhere Robotics", ("green", "blue"))`.
- Or derive the input from config in the example itself:
  `is_active_mission(ACTIVE_MISSION_TIERS[0], "Nowhere Robotics")`.
- Anything that cannot be made profile-agnostic belongs in `tests/`, where a
  fixture can `monkeypatch` config or `pytest.skip` when the loaded profile
  has nothing to test against.

To check yourself: `JOBS_PROFILE=profile.example.toml pytest`.

## House style for awkward outputs

**Unordered output — sort it.** Set and dict iteration order is not a
contract, so do not assert one:

```python
    >>> sorted(parse("local,watch"))
    ['local', 'watch']
    >>> parse(None) == set()
    True
```

**Order-sensitive output — say so, then assert it literally.** In
`slug_variants` the order *is* the probe order, so it is part of the
contract and the docstring says as much before pinning it.

**Long output — one item per line.** Never let a repr wrap; a wrapped line is
unreadable and doctest will not match it anyway:

```python
    >>> for slug in slug_variants("Bio-Signal Technologies, Inc.", None):
    ...     print(slug)
    bio-signal-technologies-inc
    bio-signaltechnologiesinc
    ...
```

For output that is long *and* uninteresting, assert a property instead of the
value: `len(...) <= 8`, `"g" in ... `, `... is None`.

**Floats — `round()` to a stated precision.** Never paste a full repr;
`0.4242640687119285` is both unreadable and platform-flaky.

**Exceptions —** use the standard doctest form, and only for exceptions that
are part of the contract:

```python
    >>> parse_track("nope")
    Traceback (most recent call last):
        ...
    KeyError: 'nope'
```

**Multi-line input** continues with `...` and stays under 79 columns:

```python
    >>> extract_boards_from_urls(["https://boards.greenhouse.io/acmebio/jobs/1",
    ...                           "https://jobs.lever.co/acmebio/abc-def"])
    [('greenhouse', 'acmebio'), ('lever', 'acmebio')]
```

Avoid `# doctest: +ELLIPSIS` and friends. If an example needs a directive to
pass, it is usually asserting the wrong thing.

## Invariant tests

`tests/test_invariants.py` is for claims that span modules — the class of bug
a doctest structurally cannot catch, because the copy the doctest is attached
to is correct and the drift lives in the copy nobody looked at.

Current invariants:

- **The company-activation rule** lives in exactly one place. The full truth
  table (every configured tier × `None` × multi-division) is asserted, then an
  AST scan asserts that no module re-implements it inline. `score_missions` is
  on an explicit allowlist with a written reason.
- **`pytest.ini` `testpaths` covers every root module**, so a new file's
  doctests cannot be silently skipped.
- **At least five modules contain doctests**, so a doctest-only run can never
  go green on an empty collection (pytest exits 5 on "no tests collected", and
  a stray `|| true` would turn that into a tick).

When you add one, write the failure message as an instruction — the reader is
someone who just broke it and does not know why.

## Running the checks

```bash
pytest                                    # everything: tests/ + all doctests
pytest --doctest-modules core/tags.py     # doctests in one module
pytest tests/test_invariants.py           # the cross-module claims
pytest --doctest-modules core -v          # see each doctest by name

JOBS_PROFILE=profile.example.toml pytest  # what CI actually runs

python -m pyflakes core scrapers discovery webapp tests *.py
python -m compileall -q core scrapers discovery webapp *.py
```

`pytest.ini` points `testpaths` at `tests/` **and** the source trees, and puts
`--doctest-modules` in `addopts`. That is deliberate: `--doctest-modules` only
collects from paths pytest is given, so the default `testpaths = tests` would
have run zero doctests while looking perfectly green.

CI runs the same thing (`.github/workflows/ci.yml`, the `test` job) on Python
3.12/3.13/3.14 against `profile.example.toml` with no API key, plus a
doctest-only step whose collected count is printed so an empty run is visible.
