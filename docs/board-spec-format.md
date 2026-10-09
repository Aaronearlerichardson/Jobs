# Fix the registry, keep comments inline

> **Updated 2026-10-08, after the owner's answers.** The recommendation below changed from "stay in Python" to **"move to TOML 1.0 data files, loaded at runtime with a per-user override folder"**. See [Decision](#decision-after-the-owners-answers). JSON and YAML stay rejected for the reasons in the rest of this report.

Do not convert the board specs to JSON, and do not move their comments into a README or the package docstring. Both halves of the premise fail against this repo's own numbers. Converting all 48 specs to JSON nearly doubles them, from **2,252 to 4,417 lines**. It also doubles the backslashes (**307 to 585**), stretches the longest line from 123 to 224 characters, and deletes **186 line comments and about 256 docstring lines**. None of that fixes what actually gets harder as boards are added. Each new platform costs two hand edits in `__init__.py`. Detection order is implied by the order of a dict literal. The docs already drifted one day after the last refactor and had to be fixed by hand in `6edfc7a` (measured in the repo, 2026-10-08). The specs are already JSON in the sense that matters, because `tests/test_invariants.py` pins a JSON round trip. The owner therefore already has JSON's portability without its authoring cost. The recommendation:

- **Now:** keep Python as the authoring format. Fix registration, detection precedence, fixture coverage and the drifted docs. Add a generated catalog. Remove the 11 load-bearing `None` values with two small schema changes, so any later format change is a mechanical export.
- **If the specs must change without rebuilding the binary, or a non-Python program needs to read them:** generate JSON in CI as a shipped artifact. Do not hand-author it.
- **If specs will routinely be edited by tools or written by outside contributors:** move to **TOML 1.0**, not JSON or YAML. TOML writes regexes verbatim, keeps comments, has a stdlib parser, and is already in the repo's stack.

In every scenario, comments stay next to the values they explain. The only README worth adding is one generated from the specs and checked in CI.

## Decision after the owner's answers

**The answers.**

1. **What hurts:** noisy diffs, and rebuilding the binary for every minor tweak.
2. **What "board specs stay JSON" means:** JSON-*compatible*. The rule exists to stop specs from growing their own logic. New behaviour belongs in the shared grammar (the engine plus `spec.py`), not in one spec.
3. **Detection order:** the owner believes most specs rely on it, but is not sure.

**What changes.** Both pains are ones data files solve and Python modules do not:

- A spec compiled into the binary cannot change without a rebuild. A data file can be overridden next to the binary.
- A format that cannot express code turns rule 2 from a review convention into a property of the file.

That moves this report's decision table from "Phase 1 only" to **Phase 1 plus Phase 2b (TOML 1.0)**. A per-user override folder replaces Phase 2a's generated JSON, because with TOML the file you edit is the file the binary reads, so there is one copy, not two.

**What was measured after the answers** (in the repo, 2026-10-08):

- **Where the diff noise comes from.**
  - Single-purpose spec edits already diff cleanly (`5ea653e`, `8bf525f`, `58bfecd`).
  - The noise is in sweeping commits. `e144c08` touched 42 spec files (+332/−159), and its churn was mostly prose moving between homes, plus brackets re-wrapped by hand when one key was removed (zohorecruit's `location`). For example, zohorecruit's 11-line comment block moved into its docstring.
  - The cure is one fixed home for each kind of rationale and a formatter check, so layout is never a hand choice.
  - `tombi` 1.7.3 (a PyPI wheel; a dev tool only) formatted the bamboohr pilot with data, key order and comments unchanged. A second pass changed nothing, and it has `--check` for CI.
- **TOML 1.0 is enough.** The bamboohr pilot breaks lines only inside arrays and parses with stdlib `tomllib` on Python 3.12 and 3.13, so the migration adds no runtime dependency.
- **Order is load-bearing in two known places, not everywhere.** I ran each spec's detector alone over 827 distinct URLs and 116 fixture pages from the repo. Exactly two pairs ever matched the same input:
  - **`ultipro` before `ukg`.** `ukg` is the catch-all on the same host; 6 URLs.
  - **`jibe` before `icims`.** A Jibe page also names its iCIMS tenant.

  Posting-URL matching (`board_for_url`, first match wins) found no URL, out of 864, that two specs' `job_ref` both read. Beyond the two pairs, order only decides which platform wins on a page linking several vendors: a policy choice, not a correctness constraint. The corpus is the repo's own tests and fixtures, so the result is evidence, not proof. Keep the explicit order, and pin the two pairs in a test.

### Revised plan

**Phase 1: format-neutral; land first, while specs are still Python.** Done on 2026-10-08, except the order file, which moved to Phase 2: while the specs are modules, a data-driven order would force dynamic imports and a temporary Nuitka flag. Instead, a test now pins that every module is one `BOARDS` entry.

1. **Remove the nulls** with Changes A and B below. Then all 48 specs encode as TOML.
2. **Add a precedence test.** Every fixture, canary and doctest URL must detect as its own spec, with `ultipro`>`ukg` and `jibe`>`icims` asserted by name. A new spec that steals another's input then fails CI instead of silently changing detection.
3. **Make the order data (moved to Phase 2, step 3).** Add `src/config/boards/order.toml` (`order = ["greenhouse", ...]`, the current `BOARDS` order unchanged). A test asserts it names every spec file exactly once. Adding a platform becomes one file plus one line, and `__init__.py` stops changing.
4. **Give each kind of rationale one home,** and write it into the `__init__` docstring and `docs/REVIEW.md`:
   - a value-level fact is a `#` comment on the line above the value;
   - a workaround is a `why` field;
   - platform history is the file's header comment (the docstring until Phase 2).

   This removes the prose churn seen in `e144c08`.
5. **Generate the catalog README** (`src/config/boards/README.md`) from the specs, with a regenerate-and-compare test.

**Phase 2: TOML 1.0 migration.** Done on 2026-10-09:

- **Conversion:** a one-off converter, not kept, wrote all 48 specs. Each loads (stdlib `tomllib`, Python 3.12) equal to its Python `SPEC`, with every `fields` table in its original order, and parses to the same `BoardSpec`.
- **Comments:** the 186 were placed automatically on the key they preceded (inline-expression comments sit above their field). Bamboohr's shared `REMOTE` constant is written out where it was used.
- **Overrides:** they apply only in a compiled build. From source you edit the repo file, so the tests never see a user's overrides.
- **Editor schema** (done 2026-10-09): `src/config/boards/board-spec.schema.json` is generated from `BoardSpec` (`spec.json_schema`). It adds what the before-validators accept: one value or several, a lone rule or a list, and a lone listing or fallbacks with `reset`. The generated schema flagged 42 of 48 specs; this one flags none. `tombi.toml` associates it, so the Tombi extension completes and checks spec files, and CI runs `tombi lint`. The field grammar under `fields` is still unchecked (`{}`).
- **Deferred:** TOML 1.1.

1. **Convert with a dedicated converter, not `tomli_w`** (it reordered keys in 42 of 48 specs). Use the house layout from Phase 2b below, then run `tombi format`. For all 48, verify both `tomllib.loads(out) == SPEC` and key order (by comparing `json.dumps` of both sides).
2. **Port the 186 comments by hand.** Each docstring becomes the file's header comment.
3. **Write the loader** in `src/config/boards/__init__.py`:
   - read `order.toml`, then each `<name>.toml`;
   - if `DATA_DIR/boards/<name>.toml` exists (on Windows `%LOCALAPPDATA%\JobCrawler\boards\`), validate it with `BoardSpec` and use it in place of the bundled spec;
   - if it fails validation, fall back to the bundled spec and warn;
   - if it is identical to the bundled spec, warn that it can be deleted;
   - always log which overrides are active.

   The host lists (`FETCHABLE_HOSTS`, `SHARED_HOSTS`) are derived after this, in the same module, so overrides reach them.
   - **Users can add platforms** (decided 2026-10-08). An override naming a platform that isn't bundled is added after every bundled spec in detection order, so it can't take an existing spec's input. The precedence test only covers bundled specs, so the loader logs every added platform. An added spec can use only what the engine's grammar already offers; a platform that needs new behaviour still ships in a build (rule 2).
4. **Build:** change `build_app.py`'s `src/config` `.glob("*.toml")` to `.rglob`, and add a post-build smoke test that the binary lists all 48 boards.
5. **CI:**
   - add `tombi format --check src/config/boards` (`tombi` in `envs/requirements-dev.txt`);
   - widen `test_toml_tables_are_json` to `rglob`, so the JSON-compatible invariant covers the specs;
   - keep the `BOARDS` invariant.
6. **Clean up in the same PR:**
   - delete the `.py` spec modules;
   - restate the REVIEW rule as "board specs are data: TOML, JSON-compatible; a new behaviour is a grammar change in the engine, never per-spec code";
   - update the `__init__` docstring.

**A minor tweak afterwards:**

1. Edit `src/config/boards/workday.toml` and commit it (a one-line diff).
2. Copy the file into `DATA_DIR/boards/` on the machine running the binary.
3. Restart the app.

The next build bundles the change, and the loader then reports the override as redundant.

## Both halves of the premise fail on this repo's numbers

### A format change leaves every growth cost in place

The plan treats the number of boards as a format problem, but nothing that gets harder per platform depends on syntax. Today each new platform costs a module plus **two hand edits to `src/config/boards/__init__.py`**: an import, and a `BOARDS` entry whose position silently sets detection order, because `src/ats/signatures.py` tries specs in `BOARDS` order (measured in the repo, 2026-10-08). At ten platforms a month that is twenty edits a month to the one file every change touches. A directory of JSON files would still need a loader and an order list.

The scale is modest. yt-dlp keeps **928 hand-written import statements** for about 1,750 extractor classes in a single Python registry, plus a generated lazy index for startup speed ([yt-dlp _extractors.py](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/_extractors.py); [make_lazy_extractors.py](https://github.com/yt-dlp/yt-dlp/blob/master/devscripts/make_lazy_extractors.py)). Zotero keeps 749 per-site translator files ([zotero/translators](https://github.com/zotero/translators/blob/master/arXiv.org.js)). Nuclei stores over 11,000 HTTP templates, one file each ([TEMPLATES-STATS.md](https://github.com/projectdiscovery/nuclei-templates/blob/main/TEMPLATES-STATS.md)). Forty-eight specs growing to roughly 170 within a year is well inside what Python modules already handle.

JSON also makes the specs themselves worse. A mechanical conversion, verified equal on a round trip, gives the line, backslash and line-length numbers above. The lines get longer because JSON has no raw strings and no way to split the **49 implicitly concatenated strings** (measured in the repo, 2026-10-08). The doubled backslashes are also a source of bugs, not just noise. Python's `json` module raises an error on a single `\d` or `\.`, but it silently turns a single `\b` (a regex word boundary) into a backspace character ([Python json docs](https://docs.python.org/3/library/json.html)).

webappanalyzer, the continuation of Wappalyzer and the largest regex-in-JSON corpus surveyed, shows where this ends up. It has lines up to **519 characters**, and its own README example mixes single and doubled backslashes ([webappanalyzer README](https://github.com/enthec/webappanalyzer/blob/main/README.md); [a.json](https://github.com/enthec/webappanalyzer/blob/main/src/technologies/a.json)). Even JSON's author never meant annotated config files to lose their comments. Crockford's advice was to "insert all the comments you like" and strip them before parsing ([nlohmann/json, Comments](https://json.nlohmann.me/features/comments/)).

### A README is where per-value rationale goes stale

The second half of the premise, that comments can live in a README or the package docstring, does more damage. None of the comparable projects surveyed moves per-entry rationale out of the data file:

- **Where the format allows comments**, short rationale sits next to the pattern it explains. This is the case in Linguist's heuristics, the Public Suffix List and Nuitka's package config ([Linguist heuristics.yml](https://github.com/github-linguist/linguist/blob/main/lib/linguist/heuristics.yml); [public_suffix_list.dat](https://github.com/publicsuffix/list/blob/main/public_suffix_list.dat); [Nuitka package config](https://github.com/Nuitka/Nuitka/blob/develop/nuitka/plugins/standard/standard.nuitka-package.config.yml)).
- **Where it does not**, projects either add a `description` or `notes` field, as Renovate and caniuse do, or keep no per-entry rationale at all, as Wappalyzer does ([Renovate replacements.json](https://github.com/renovatebot/renovate/blob/main/lib/data/replacements.json); [caniuse CONTRIBUTING](https://github.com/Fyrd/caniuse/blob/main/CONTRIBUTING.md)).
- **READMEs** in these projects document the field schema, never individual entries ([Renovate lib/data/readme.md](https://github.com/renovatebot/renovate/blob/main/lib/data/readme.md)).

The research on documentation drift points the same way:

- Comments kept next to code changed with it in **90% of cases in six of eight systems** studied ([Fluri et al. 2009](https://consensus.app/papers/details/848a3e9bf61e536cb67c68eac26e4b26/?utm_source=claude_desktop)).
- **68% of surveyed engineers** agreed that separate documentation "is always outdated" ([Lethbridge et al. 2003](https://dl.acm.org/doi/10.1109/MS.2003.1241364)).
- More than a quarter of the 1,000 most popular GitHub projects contain references to code that no longer exists ([Tan et al. 2023](https://consensus.app/papers/details/5cededeaf5025300a439b1ea810538e5/?utm_source=claude_desktop)).
- In a controlled experiment, changes split across separate artifacts mostly failed unless a tool flagged the inconsistency. With that feedback, correct completion was **268% more likely** ([Kanakis et al. 2019](https://consensus.app/papers/details/6192063bc6f1538990d324803993e04f/?utm_source=claude_desktop)).

This repo has already run that experiment on itself. Commit `1d92c47` split `boards.py` into the package and changed 46 files, tests included. It left six references (`docs/REVIEW.md:101`, `README.md:384,445`, two code comments and a test docstring) pointing at a file that no longer exists (measured in the repo, 2026-10-08). Commit `6edfc7a` fixed them by hand the next day; nothing had failed while they were wrong. Linguist's CONTRIBUTING.md still links to `heuristics.rb` seven years after that file's rules moved to YAML ([Linguist CONTRIBUTING.md](https://github.com/github-linguist/linguist/blob/main/CONTRIBUTING.md)).

A README holding 186 value-level notes would need a key path for each note to say which value it explains, and those paths would go stale at the next rename. The `__init__` docstring variant is worse still. It would put every platform's reasoning into the one file that every new platform already has to edit.

### Where the instinct toward data files is right

The pull toward data is reasonable; it is just aimed at the wrong target. Code-to-data is the dominant migration among comparable projects:

- Linguist moved its heuristics from Ruby to YAML in 2018 ([Linguist #4087](https://github.com/github-linguist/linguist/commit/8bf9efa3702a1a43df85dc5cd72b63f3ff36871f)).
- Home Assistant moved its metadata into `manifest.json` in 2019 ([Home Assistant 0.92](https://github.com/home-assistant/developers.home-assistant/blob/master/blog/2019-04-12-new-integration-structure.md)).
- Nuitka moved its package rules into YAML plus a JSON Schema in 2021–22 ([Nuitka commit 2e3fa23](https://github.com/Nuitka/Nuitka/commit/2e3fa23df5a4ba6da3754d6be2afb743fa87a8d2)).

In every case the target format either kept comments or held data simple enough not to need them.

Data files have three concrete advantages over modules:

1. **They can change without recompiling.** Nuitka compiles today's `.py` specs into the binary. Data files placed next to the executable can be read through `__compiled__.containing_dir` ([Nuitka User Manual](https://pypi.org/project/Nuitka/)).
2. **Tools can rewrite them safely.**
3. **Non-Python programs can read them.** This was PEP 518's main objection to Python literals ([PEP 518](https://peps.python.org/pep-0518/)).

The repo already has most of the third advantage. `tests/test_invariants.py` pins `json.loads(json.dumps(config.BOARDS)) == config.BOARDS`, so a JSON export is a few lines of script whenever someone needs one (measured in the repo, 2026-10-08).

## TOML is the best data format; Python won only until the drivers were known

The table scores each option against the repo's concrete features:

- 108 regexes containing backslashes;
- 186 line comments, plus dated `Notes:` in the docstrings;
- 11 load-bearing nulls;
- expression trees nested 10 deep, and 49 split strings;
- a Nuitka binary for Windows users, and a Python 3.12 floor.

Repo figures are measured in the repo, 2026-10-08; library behaviour is cited in the prose below.

| Option | 108 regexes | Comments and Notes | 11 nulls | Depth-10 trees, 49 split strings | Parser and packaging | Verdict |
|---|---|---|---|---|---|---|
| **Python dict literals (today)** | `r"..."`, written verbatim | `#` comments and docstrings | `None` | Native; implicit concatenation | Compiled into the binary; Nuitka follows the static imports | **Best today** |
| Strict JSON + README | Every backslash doubled (307 to 585); a single `\b` silently becomes a backspace | None; rationale is moved away from the values | `null` | 4,417 lines; longest line 224; strings cannot be split | stdlib; data files must be bundled | Reject |
| JSONC | Doubled | `//` comments in place | `null` | Strings cannot be split | `json-with-comments` (pure Python, about 17x slower than stdlib); the spec is a draft | Only if a JSON-only tool requires it |
| JSON5 / HJSON | JSON5 silently drops unknown escapes (`\d` becomes `d`); HJSON's unquoted values silently absorb trailing commas and comments | Yes | Yes | JSON5 line continuation copies the next line's indentation into the value | hjson last released 2022 | Reject |
| **TOML 1.0** | `'...'` is verbatim; 29 strings containing `'` need `"..."`; the 3 that also contain `\` need `'''...'''` | `#` comments in place | **None; needs 2 schema changes** | Inline tables must stay on one line, so breaks go only inside `[ ]`; 41 of 48 specs convert, giving 2,946 lines | stdlib `tomllib` on 3.12+; already used for `src/config/*.toml` | **Best data format** |
| TOML 1.1 | Same as 1.0 | Also allows comments inside inline tables | None | Multi-line inline tables read like the Python layout (bamboohr: 41 lines vs 43 in Python) | stdlib only from Python 3.15; `tomli>=2.4` needed on 3.12–3.14 | Later |
| YAML | Single quotes are verbatim, but unquoted strings change type (below) | `#`; anchors for shared values | `null` | Shortest: 2,895 lines; bamboohr 32 | New dependency; PyYAML implements YAML 1.1 while editors use 1.2 | Reject |
| Jsonnet / Starlark | Verbatim | Yes | Yes | Jsonnet's `+` fits the listing-fallback inheritance | Native extension; no mypy or IDE help | Only if runtime-loaded or untrusted specs appear |
| CUE, Pkl, Dhall, KDL, NestedText, HCL | Varies | Varies | Varies | Data model mismatch or no override semantics | No maintained Windows binding for Python 3.12+, or the wrong data model | Reject |

### Why TOML beats JSON and YAML as a data format

TOML's failures are loud, and its strengths match this data:

- **Regexes paste verbatim.** Literal strings `'...'` do no escape processing, exactly like `r"..."` ([TOML v1.0.0](https://toml.io/en/v1.0.0)).
- **Duplicate keys are a parse error.** stdlib `json` and PyYAML silently keep the last value instead ([Python json docs](https://docs.python.org/3/library/json.html); [PyYAML 6.0.3](https://pypi.org/project/PyYAML/6.0.3/)).
- **Every string is quoted**, so TOML has no equivalent of YAML's implicit typing.

That last point is what rules YAML out here. A scan of the specs' actual strings found that, left unquoted in YAML (measured in the repo, 2026-10-08):

- 22 values such as `"{url}"` would parse as mappings;
- `"#job-description"` would be read as a comment and become null;
- `"true"` would become a bool, and `"[class*='jobLocation']"` a list;
- `"1"` would become an int inside the loosely typed `eq` condition grammar, where pydantic's Strict types never check it.

YAML's own critics prescribe quoting every string ([Ruud van Asseldonk](https://ruudvanasseldonk.com/2023/01/11/the-yaml-document-from-hell); [arp242](https://www.arp242.net/yaml-config.html)), which gives up most of YAML's brevity. Editor and loader would also disagree: the Red Hat YAML language server parses YAML 1.2 by default, while PyYAML loads 1.1, where `no` and `on` are booleans ([yaml-language-server README](https://raw.githubusercontent.com/redhat-developer/yaml-language-server/main/README.md); [PyYAML resolver](https://github.com/yaml/pyyaml/blob/main/lib/yaml/resolver.py)).

TOML also fits the existing stack. The repo already loads four TOML tables with stdlib `tomllib` and tests that every `.toml` file is JSON-compatible. It edits `profile.toml` with tomlkit, which is the comment-preserving style of editor PEP 680 recommends ([PEP 680](https://peps.python.org/pep-0680/)).

### TOML's costs are real and specific

- **No null.** The spec says "Unspecified values are invalid", and requests to add a null have been closed since 2013 ([TOML v1.1.0](https://toml.io/en/v1.1.0); [toml-lang#30](https://github.com/toml-lang/toml/issues/30)). Seven of the 48 specs cannot be encoded until two schema changes land (Phase 1 below). The repo's "reset an inherited key" pattern is exactly the case discussed in [toml-lang#803](https://github.com/toml-lang/toml/issues/803).
- **Deep trees are awkward in 1.0.** TOML 1.0 forbids line breaks inside inline tables, so the deep `first`/`when` trees can break lines only inside `[ ]`, leaving ragged closers like `] } },`.
- **1.1 fixes that, but needs a dependency.** TOML 1.1 (released 2025-12-18) allows multi-line inline tables ([TOML CHANGELOG](https://github.com/toml-lang/toml/blob/main/CHANGELOG.md)). stdlib `tomllib` parses 1.1 only from Python 3.15, whose final release PEP 790 schedules for 2026-10-09 ([What's New in 3.15](https://github.com/python/cpython/blob/main/Doc/whatsnew/3.15.rst); [PEP 790](https://peps.python.org/pep-0790/)). On the repo's 3.12–3.14 CI, 1.1 needs `tomli>=2.4` ([tomli README](https://github.com/hukkin/tomli#readme)).
- **Deep schemas get ugly.** StrictYAML's author and pytoml's former maintainer both argue TOML degrades as schemas get deeper ([StrictYAML: What is wrong with TOML?](https://hitchdev.com/strictyaml/why-not/toml/)). The hand-written bamboohr pilot suggests that cost is manageable for these specs, but not zero.

Load time does not matter: PEP 680 notes that TOML parsing is rarely a bottleneck ([PEP 680](https://peps.python.org/pep-0680/)). Measured on the real specs, parsing all 48 takes about 20 ms against roughly 1.3 s of app imports.

### Why Python won before the owner's answers

Every TOML benefit except replacing specs at runtime is one the repo already has. Python keeps `r""` strings, `None`, comments, docstrings, implicit concatenation and the one shared constant (bamboohr's `REMOTE`), with zero migration. Nuitka follows the static imports without extra flags. The JSON invariant keeps the exit open.

The programmable config languages are not real contenders:

- CUE has no released Python binding ([cue-py](https://github.com/cue-lang/cue-py)).
- Pkl's Python library calls itself pre-release ([pkl-python](https://github.com/jw-y/pkl-python)).
- Dhall's binding has no Windows or Python 3.12+ wheels ([dhall 0.1.16](https://pypi.org/project/dhall/0.1.16/)).
- Jsonnet is the only one whose `+` override matches the listing-fallback inheritance. It would add a native extension and lose type-checker support, for a need the Python loader already meets ([jsonnet 0.22.0](https://pypi.org/project/jsonnet/0.22.0/)).

## Rationale stays beside its value; the README becomes a generated view

Each kind of rationale has a home, and none of them is a hand-written README:

| Kind of rationale | Today | Where it lives in Python (now) | Where it lives if TOML | What keeps it honest |
|---|---|---|---|---|
| Value-level fact (for example "$skip counts from 1") | 186 line comments | A `#` comment next to the value | A `#` comment next to the value | Code review, because it sits in the diff |
| A workaround that should be re-checked (fallback, rescue, odd transform) | 28 `why` fields, `"reason, YYYY-MM"` | `why` | `why` | The existing regex in `spec.py`, plus a new staleness test |
| Platform narrative and dated history | About 256 docstring lines under `Notes:` | The module docstring | A `notes = '''...'''` field declared on `BoardSpec` | The catalog generator reads it |
| Overview of all platforms | None | Generated `src/config/boards/README.md` | Same | A test that regenerates it and compares |
| Conventions (adding a platform, what each key means) | `__init__` docstring and `spec.py` descriptions | A short hand-written guide plus the schema's 161 descriptions | Same | A test that every path mentioned in the docs exists |

**Value-level facts must stay inline.** Diátaxis classes a spec as reference material, whose structure should "follow that of the machinery" it describes ([Diátaxis](https://diataxis.fr/_/downloads/en/latest/pdf/)). Ousterhout's rule is to keep comments next to the code, and in the code rather than in side documents ([Philosophy of Software Design notes](https://github.com/alysivji/notes/blob/main/software-engineering/philosophy_of_software_design.md)). A stale high-level overview can still be useful, but a stale low-level fact is simply wrong.

**Rationale a machine should check becomes a structured field.** The repo's `why` fields already follow the strongest precedent:

- SigmaHQ requires `description`, `references` and `falsepositives` on every rule ([SigmaHQ rule convention](https://github.com/SigmaHQ/sigma-specification/blob/main/sigmahq/sigmahq-rule-convention.md)).
- Chromium pairs each flag with an expiry milestone that forces a re-review ([Chromium flag ownership](https://chromium.googlesource.com/chromium/src/+/main/docs/flag_ownership.md)).

A schema guarantees that rationale is present and attached to its value, not that it is still true. The `YYYY-MM` date makes a cheap staleness test possible, for example one that flags any `why` older than twelve months for re-verification.

**What not to do with comments in a data format:**

- Do not use `"//"` or `"$comment"` keys. JSON Schema defines `$comment` for schema documents only, not for data ([JSON Schema 2020-12 Core](https://json-schema.org/draft/2020-12/json-schema-core)). With `extra="forbid"`, every model at all ten nesting levels would have to declare such a key.
- If specs become TOML, declare the docstring's replacement as a real `notes` field rather than a convention, following Renovate's `description` and caniuse's `notes`.

**The owner's README idea survives only as a generated view.** A ~30-line script can render one section per platform: hosts, detection position, listing alternatives with their `why`, and Notes. The output should open with a do-not-edit banner, be written with LF line endings, and be checked by a test that regenerates it and compares. That regenerate-and-compare check is a standard CI pattern ([git diff --exit-code](https://raw.githubusercontent.com/git/git/master/Documentation/diff-options.adoc); [example PR](https://github.com/filippolmt/global-chart/pull/106)). One pitfall: path-filtered CI workflows have hidden this kind of drift until an unrelated change re-triggered them ([guettli/parca #96](https://github.com/guettli/parca/pull/96)), so run the check on every PR.

Hand-written prose should cover only conventions. A test that every `src/...` path mentioned in `README.md` and `docs/*.md` exists would have caught the stale references from `1d92c47` the day they appeared.

## Growth needs a registry, explicit order and evidence

The measures that actually absorb ten platforms a month are format-independent.

**1. Registration.** Replace the 48 imports and 48 dict entries with one explicit `ORDER` tuple of module names, imported with `importlib`. Discovery belongs in a test, not at runtime: a test asserts that the modules found by `pkgutil.iter_modules` equal `set(ORDER)`, each exactly once. The Nuitka manual recommends this `pkgutil` plus `importlib` pattern. Because the imports become dynamic, the build must add `--include-package=src.config.boards` ([Nuitka User Manual](https://pypi.org/project/Nuitka/)). The minimal variant keeps the hand imports and adds only the completeness test.

**2. Detection precedence.** Comparable projects make precedence explicit. yt-dlp adds YouTube first and Generic last ([yt-dlp extractors.py](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/extractors.py)). Linguist requires a test for every heuristic ([Linguist test_pedantic.rb](https://github.com/github-linguist/linguist/blob/main/test/test_pedantic.rb)). The repo's `test_detect_returns_only_board_keys` checks only that a detection names a real spec (measured in the repo, 2026-10-08). A stronger test runs every fixture and canary through detection and asserts it resolves to *its own* spec. Overlaps that are intended get declared exceptions, for example the `custom` careers-page spec, which `__init__.py` calls the sniffer's last resort. With that test, `ORDER` stops being a hidden contract.

**3. Fixture coverage.** `tests/fixtures` already has 107 recorded entries, and 42 of 48 platforms have a fixture named after them. Five of the six without one are detection-only specs (gohire, gusto, paycom, taleo_enterprise, ukg). One, joincom, has a listing but no fixture (measured in the repo, 2026-10-08). A rule that every spec with a listing has a recorded response and expected rows mirrors Semgrep's one-test-file-per-rule convention ([Semgrep contributing docs](https://github.com/semgrep/semgrep-docs/blob/main/docs/contributing/contributing-to-semgrep-rules-repository.mdx)). Such fixtures double as rationale that CI executes.

**4. Schema and editor tooling.** This pays off once specs are data files, or once anything outside the app reads them. pydantic generates a usable 2020-12 JSON Schema from `BoardSpec`, but `BeforeValidator` and `mode="before"` coercions are invisible to it. Unfixed, VS Code's JSON service flagged **41 of 48 real specs** as invalid ([pydantic JSON Schema docs](https://raw.githubusercontent.com/pydantic/pydantic/main/docs/concepts/json_schema.md); [vscode-json-languageservice](https://www.npmjs.com/package/vscode-json-languageservice)). The fixes:

- add `json_schema_input_type=` on each `BeforeValidator` ([pydantic validators docs](https://raw.githubusercontent.com/pydantic/pydantic/main/docs/concepts/validators.md));
- hand-patch the schema for later listing alternatives, which may omit keys they inherit from the first;
- commit the schema file and test that it stays in sync;
- test that every real spec passes the generated schema.

**5. Scaffolding.** A `tools/new_board.py <name>` script that writes the module skeleton, the `ORDER` line and a fixture stub removes most of the per-platform cost.

## A phased plan that branches on the real driver

Phases 0 and 1 are worth doing whatever the owner decides about formats. Phase 2 depends on naming the real driver.

| Phase | Trigger | Work | Done when |
|---|---|---|---|
| **0. Repair** | Now | ~~Fix the stale `boards.py` references~~ (done in `6edfc7a`). Rewrite the `__init__` docstring paragraph that plans the README move. Restate the REVIEW rule as "board specs stay JSON-compatible (test_invariants pins it)". Add the docs path test. | The path test fails on a deliberately broken link |
| **1. Format-neutral growth** | Now | `ORDER` plus completeness test; precedence test; fixture rule; null removal (A and B below); generated catalog; scaffold script | A new platform costs one module, one `ORDER` line and one fixture. `tomli_w` encodes all 48 specs. The JSON invariant is still green. |
| **Decision** | After Phase 1 | Name the driver (next table) | |
| **2a. Generated export** | Runtime updates, or a non-Python reader | CI writes `boards.json` and the schema. The binary prefers a validated copy next to the executable. | Built binary loads an override and logs it |
| **2b. TOML 1.0** | Routine machine edits, or outside contributors | Pilot icims, bamboohr and workday; then convert all, port comments, delete the `.py` modules | All 48 equal in content and key order; binary smoke test lists 48 boards |
| **3. TOML 1.1** | Python floor reaches 3.15, or the pilot finds 1.0 layout unreadable | Multi-line inline tables; bump the tomlkit pin to `>=0.15` | |

| If the real driver is | Then | Authoring format |
|---|---|---|
| File count, finding a spec, noisy reviews | Phase 1 only | Python |
| Fixing specs for Windows users without a new binary | Phase 2a | Python. Move to TOML only if non-developers write the fixes. |
| A non-Python reader (web UI, extension, another service) | Phase 2a, plus the committed JSON Schema | Python |
| An LLM or script drafting one new spec at a time | Phase 1's checks (pydantic, fixtures, canary probe) are the gate | Python. An LLM writes dict literals as readily as JSON; this is an inference, not tested. |
| Bulk machine edits across many specs, or untrusted contributors | Phase 2b | TOML 1.0 |

### Phase 1 null removal: two schema changes

The 11 nulls are of two kinds (measured in the repo, 2026-10-08). Seven are per-group "no transform" markers, in cornerstone, dayforce, infor, oracle (2) and workday (2). Four are listing-fallback resets: `"params": None, "pager": None` in icims and in jobvite.

**Change A (transforms).** Replace `None` with a named identity transform. `"keep"` does not collide with any existing name:

```python
# src/ats/board/fields.py
TRANSFORMS: dict[str, Transform] = {
    "keep": lambda v: v,        # the group as captured (was None)
    **_UNARY, **_ARGUED, ...
}

# src/ats/board/spec.py, class Detect
transform: tuple[Str, ...] = Field((), description='A fields.TRANSFORMS name per group '
                                                   '("keep": as captured); default none')
# in _groups():
if self.transform and (len(self.transform) != groups
                       or any(t not in fields.TRANSFORMS for t in self.transform)):
    raise ValueError("detect.transform: a known transform per group")

# specs: ["lower", "int", None] -> ["lower", "int", "keep"]
```

In `engine.py`, the `is None` branches at lines 479 and 556 become unreachable for specs. Delete them, or keep the internal default there as `"keep"`.

**Change B (listing resets).** Replace reset-by-`None` with an explicit `reset` list, handled in `BoardSpec._alternatives` and removed before `Listing` sees it. Removing it first matters because `Listing` uses `extra="forbid"`:

```python
# src/ats/board/spec.py, BoardSpec._alternatives (list branch)
first, alts = listing[0], []
if "reset" in first:
    raise ValueError("listing[0].reset: the first alternative inherits nothing")
for alt in listing[1:]:
    reset = set(alt.get("reset", ()))
    if unknown := reset - _field_keys(Listing):        # field names and aliases
        raise ValueError(f"listing reset: no such key {sorted(unknown)}")
    alts.append({**{k: v for k, v in first.items() if k not in alt and k not in reset},
                 **{k: v for k, v in alt.items() if k != "reset"}})
return {**data, "listing": [first, *alts]}

# icims.py, jobvite.py: "params": None, "pager": None  ->  "reset": ["params", "pager"]
```

Keep the existing rule that a value equal to its default also resets, unless a test shows no spec relies on it. Declare `reset` (an array whose items are an enum of `Listing` keys) in the hand-written schema patch for listing alternatives. Update the docstring rule in `__init__.py`.

After A and B, no spec contains `None` and every spec encodes as TOML. This is a format-neutral change worth landing even if TOML never happens.

### Phase 2a: ship data without changing how it is written

For runtime updates, CI writes `boards.json` from `BOARDS`, in `ORDER`, with a `schema_version`. The binary reads a copy next to the executable via `__compiled__.containing_dir` ([Nuitka User Manual](https://pypi.org/project/Nuitka/)). It validates every override with `BoardSpec`, falls back to the compiled-in spec when validation fails, and logs what it replaced.

The override must be applied before `FETCHABLE_HOSTS` and `SHARED_HOSTS` are computed, because `__init__.py` derives them at import time (measured in the repo, 2026-10-08). Users' copies go stale when the schema changes, so treat a `schema_version` mismatch as "ignore the override".

### Phase 2b: the TOML migration, if triggered

1. **Pilot.** Convert icims (the largest spec, with resets), bamboohr (the deepest tree, with the shared constant) and workday (transforms and split regexes). Do it on a branch, in the house layout:
   - top-level scalars first;
   - then `[[detect]]`, `[canary]`, `[discovery]`;
   - then `[[listing]]`, with `[listing.fields]` holding one inline expression per field;
   - line breaks only inside `[ ]`.

   String rules: regexes in `'...'`; strings containing `'` in `"..."`; the 3 strings with both `'` and `\` in `'''...'''`. Long regexes stay on one line; non-regex long strings use `"""` with `\` continuation. Avoid fragment arrays, because `detect.re` is already a list meaning "all must match".
2. **Write a dedicated converter.** Do not use `tomli_w`. In a test round trip it changed key order in 42 of 48 specs, and it emitted 16 table headers for bamboohr alone.
3. **Check content and key order.** Compare `tomllib.loads(out) == SPEC` and also `json.dumps` of both sides, which also checks key order. Order matters because the engine computes `_` fields in dict order (`engine.py:372–386`).
4. **Port the 186 comments by hand.** Move each docstring into the `notes` field.
5. **Replace the loader** with the version below, which reads through the same `tomllib` entry point that `tables.py` uses.
6. **Fix the build glob**, also below.
7. **Configure tooling.** Point Tombi at the schema with `[[schemas]] include = ["src/config/boards/*.toml"]`. Tombi defaults to TOML v1.0.0 and supports 2020-12 keywords; Taplo claims only Draft 4 features ([Tombi docs](https://github.com/tombi-toml/tombi/blob/main/docs/src/routes/docs/json-schema.mdx); [Taplo docs](https://github.com/tamasfe/taplo/blob/master/site/site/configuration/developing-schemas.md)). If check-jsonschema runs in pre-commit, set its `types_or` to `[json, yaml, toml]`, because the default hook skips TOML ([check-jsonschema hooks](https://raw.githubusercontent.com/python-jsonschema/check-jsonschema/main/.pre-commit-hooks.yaml)).
8. **Delete the `.py` modules in the same PR** that updates the README, the REVIEW rule and the docstring.

```python
# src/config/boards/__init__.py (TOML variant)
import tomllib
from importlib.resources import files

ORDER = ("greenhouse", "lever", ..., "paycor")  # detection order, first to last

def _load(name: str) -> dict[str, JSON]:
    return tomllib.loads(files(__package__).joinpath(f"{name}.toml").read_text(encoding="utf-8"))

BOARDS: dict[str, dict[str, JSON]] = {n: _load(n) for n in ORDER}
```

```python
# build_app.py: the current glob is non-recursive and would silently omit
# src/config/boards/*.toml, which would then fail only at runtime on a user's machine
*((p, p) for p in sorted(f.relative_to(ROOT).as_posix()
                         for f in (ROOT / "src" / "config").rglob("*.toml"))),   # was .glob
```

Back the glob fix with a post-build smoke test: the binary must list all boards. Without it, a missing data file surfaces only on a Windows user's machine.

### Questions the owner should answer first

| Question | What the answer decides |
|---|---|
| Which pain made JSON attractive: the two registry edits, finding a spec, review diffs, or something else? | Whether anything beyond Phase 1 is needed |
| Do Windows users need spec fixes faster than a new binary can ship? How often do ATS changes break a spec? | Whether Phase 2a is needed |
| Will anything besides this app read the specs? | Whether to commit a JSON export and schema |
| Will scripts or LLMs bulk-edit specs, or will outsiders contribute them? Python specs execute on import. | Whether Phase 2b is needed, and where the trust boundary sits |
| Which specs truly depend on detection order? Is the `custom` last-resort spec the only real constraint? | `ORDER` list, `priority` field, or an order-independent precedence test |
| Does "board specs stay JSON" in `docs/REVIEW.md` mean JSON-compatible data (true today) or `.json` files? | How the review rule is reworded |
| Should dated `why` fields expire, and after how long? | Whether to add the staleness test, and its threshold |
| When does the Python floor reach 3.15? | TOML 1.1 without `tomli` |
| Which editors do contributors use? PyCharm's TOML schema support was not verified. | How much to invest in editor tooling |

## Conclusion

The useful reframing is that the authoring format and the shipping format are separate decisions. The pinned JSON invariant, strict pydantic models with `extra="forbid"`, dated `why` fields and 107 recorded fixtures already make the specs data in every sense a machine cares about. What is left to choose is the most readable text for a human to write. For 108 regexes and 186 comments that is still Python, with TOML 1.0 as the exit once people other than the owner, or tools, start writing specs. JSON belongs at the shipping end, generated and never hand-edited.

The one move that pays off in every future is removing the 11 nulls. It costs two small schema changes, keeps the existing invariant green, and turns any later migration into a script that can be verified. The bigger risk to this library is not its syntax but its prose. The repo drifted within a day of its last refactor, and the most valuable new test in this plan is also the cheapest: checking that every path the docs mention still exists.
