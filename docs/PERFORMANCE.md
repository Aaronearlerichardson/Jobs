# Performance rules

The crawler is network-bound, so speed work goes only where a profile says
the CPU is. The rules come in three kinds: **mechanical** ones a test
enforces everywhere, **judgment** ones the reviewer applies to profiled hot
paths, and **build** flags.

## Mechanical (enforced by tests)

`tests/test_invariants.py` section 6 checks these over `src/`, `tools/` and
the root scripts with an AST scan. Each check matches one narrow shape, so a
hit is a real break, not a style opinion.

| Rule id | Never | Instead |
|---|---|---|
| `shadow` | bind a builtin's name (`list = ...`, a parameter named `type`, `id`, `input`, `max`...) at any scope. Class attributes and methods are exempt: they hide nothing outside the class. | a descriptive name |
| `eval` | `eval` / `exec` | real code |
| `scope-write` | write through `globals()` / `locals()` | a dict you own |
| `pop0` | `x.pop(0)` | `collections.deque` and `popleft()` |
| `str-concat` | grow a string with `+=` across loop passes | collect parts in a list, `"".join` them |
| `list-in` | test membership against a list inside a loop or comprehension | a set or dict built once, before the loop |
| `append-loop` | `for x in y: out.append(e)` (optionally under one `if`) | a comprehension, or `out += [...]` |
| `module-loop` | run a loop at module or class-body scope | put it in a function |

`python -O` is safe for the build because src/ and the root scripts hold no
`assert` and no `__debug__`; a test keeps it that way. Raise an exception
where you would have asserted.

## Judgment (hot paths only)

Apply these where a profile shows the time goes, and prove the output did
not change (run the same inputs through the old and new code and compare).

1. **Let C run the loop.** Comprehensions over append loops; `any`, `all`,
   `sum`, `min`, `max`, `sorted(key=)`, `str.join`, `str.startswith(tuple)`
   over hand-rolled loops. A plain `in` on a string is a C scan: use it to
   rule a term out before a regex runs.
2. **Right container.** Sets and dicts for membership, `deque` for queues,
   `Counter` / `defaultdict` over check-then-insert.
3. **Hot code lives in functions.** Locals are faster than globals.
4. **Stable types.** A hot variable keeps one type; a function returns one
   type.
5. **try/except for rare failure, an explicit check for common failure**
   (roughly 30% or more).
6. **Cache pure functions** with repeated inputs (`functools.cache` /
   `lru_cache`). Key on contents, not identity, when the input is a list
   something else mutates (see `filters._untiered`).
7. **`__slots__`** on classes with many small instances.
8. **Nuitka:** no monkeypatching and no computed-name `getattr` / `setattr`
   in hot code; write loops whose types can be inferred.

Drop old micro-tricks (a bound `.append`, globals cached as default
arguments) unless a profile says otherwise.

## Build

`build_app.py` compiles the whole program (Nuitka follows imports) with:

- `--lto=yes`: link-time optimisation of the generated C. The C build takes
  noticeably longer.
- `--python-flag=-O`: drops asserts and `__debug__` blocks (see above).

Python is 3.14; there is nothing to gain from a version flag.

## Profiling

Profile offline: copy `data/jobs.db` into a scratch directory, open the copy
read-only (`file:...?mode=ro`), and run the pure functions over its rows
under `cProfile`. Block the network and never call the Claude API or write
the real store. Time each stage best-of-3 and pickle the outputs, then run
the old tree beside the new one and compare.

## Notes

Unverified history. First profile, 2026-09-24, over 20,000 stored rows. Hot:
the place scan in `locality.geo_mode` (a re.I alternation, run on every
harvested row) and `filters.token_in`, whose boundary regex ran on every
bounded term. `token_in` now tests the substring first and caches each
term's rule; `geo_mode` scans the lowercased text case-sensitively where
that is exact. Harvest row building went from 13.2s to 4.1s, triage gates
from 4.7s to 1.7s, per-track excludes from 39.2s to 12.0s, with identical
outputs. Still slow per call but low in volume: `filters.scrub_boilerplate`
(a re.I alternation, about 0.3 ms a body) and `locality.location_snippet`.
