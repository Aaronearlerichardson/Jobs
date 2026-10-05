# Modelling and typing data in Python: literature review and a thesis for the Jobs crawler

Date: 2026-09-29. Scope: pydantic vs dataclass vs TypedDict vs attrs/msgspec/NamedTuple, typed SQL rows, boundary validation, one source of truth, dict vs Mapping. Grounded in a read-only pass over `src/rows.py`, `src/config/tracks.py`, `src/config/profile_schema.py`, `src/store/companies.py`, `src/discovery/pipeline.py`, `src/claude/fit.py`, `src/ats/coords.py`, `docs/REVIEW.md`, `docs/DOCSTRINGS.md`, `mypy.ini`, the CI workflow and the migrations. Experiments ran in the scratchpad on Python 3.14.7, mypy 2.3.1, pydantic 2.13.5 and attrs 26.1.0. Around 55 sources were read (listed in section 6).

---

## 1. Executive summary and thesis

**Thesis.** Validate once, at the edge. After that, keep the most precise type you already have, and write a shape out by hand only where a checker or a test verifies the copy. For this codebase that comes to five rules:

1. **Validation runs at trust boundaries. The validated object may go anywhere.** A frozen pydantic model that was parsed at the edge is a legitimate internal type. The anti-pattern is `model_dump()` followed by `cast` to a hand-written TypedDict, not carrying the model inward. Hypothesis 1 as worded ("pydantic belongs only at trust boundaries") is too strong. It also contradicts hypothesis 3, which keeps the parsed Track.
2. **TypedDict is right exactly where the value has to stay a dict.** Here that means SQL rows and patches (key absence means "leave what is stored", `{**row}`, SQL parameters, JSON), `**kwargs`, and data whose keys are themselves data. A fixed-field record accessed by name is a class: a dataclass for working state, a frozen pydantic model for validated configuration.
3. **For SQL-shaped data the SQL is the source of truth, and TypedDicts are a checked mirror.** The sync test should cover base value types as well as names. Any list of columns (the upsert list) is derived from the mirror and never kept by hand.
4. **The single `cast` at the SQLite read boundary is honest only if the store enforces its declared types.** SQLite does not enforce them by default, so either make the tables STRICT or audit them with a test.
5. **Performance decides nothing here.** Every option costs 0.1 to 8 µs per object (measured below). A crawler that waits on the network cannot tell the difference, and the owner's rule already says speed only counts where a profile shows it hot.

**How this compares with the working view.** Hypotheses 2 and 3 are broadly confirmed, with four corrections:

- **The companies duplication is miscounted.** The runtime `create_model` is derived from the live schema, so it is not a hand-kept copy. The copy that can actually drift is `upsert_company`'s untested 20-name tuple.
- **TypeAdapter over a TypedDict is not a drop-in replacement for the current import validator.** It silently drops unknown columns unless the TypedDict is closed or configured `forbid`. It loses the `name` non-empty check. It cannot make `name` required through a subclass.
- **PEP 728 closed TypedDicts are the tool that makes the TypedDict side work well** (import forbid, ReadOnly "views" for hit-or-row parameters, `in` narrowing). mypy's support is partial, though, and it needs `typing_extensions` plus a mypy floor of at least 2.2.
- **Removing TrackDict exposes a latent bug.** `Track(engine="sweep")` built through `__init__` silently keeps the local engine's defaults (verified). This has to be fixed before Track objects travel.

---

## 2. The map of opinion, question by question

### A. Internal trusted data: TypedDict vs dataclass vs BaseModel vs attrs vs msgspec vs NamedTuple

**Camp A1: validate at the edges, use plain classes inside.** This is the majority view among library maintainers.

- **Holders:**
  - The attrs docs (Hynek Schlawack) say pydantic suits parsing untrusted input ("Commands"), not trusted data, and that dicts are not for fixed fields.
  - Hynek's 2025 PyCon US / EuroPython keynote *Design Pressure* argues for several distinct models (data, object, resource, message) and for serialisation as a translation layer at the edges.
  - The cattrs docs (Tin Tvrtković) keep validation and serialisation at the edges, not the core.
  - The msgspec docs (Jim Crist-Harif) say Struct annotations are deliberately not checked on `__init__`. Static checkers are expected to cover internal construction.
  - Mike Bayer (SQLAlchemy, 2023) turned down pydantic partly because data coming back from the database does not need re-validation.
- **Practitioner posts in the same camp:**
  - Sebastian Buczyński (2023) and Han Lee (2025): pydantic at service boundaries, dataclasses inside.
  - Erik van de Ven (2025): keep pydantic out of the domain layer.
  - The BiteCode_dev comments on HN (2025).
- **Strongest argument.** Runtime validation of trusted data buys nothing. Worse, it creates design pressure: the core gets shaped by the wire format and the validator's features.
- **Evidence.** Mostly expert opinion from highly credible maintainers, plus microbenchmarks.

**Camp A2: one pydantic model per concept, used everywhere.**

- **Holders:** SQLModel (Sebastián Ramírez) uses one class as both table and validation model to cut duplication. Speakeasy (2024, a vendor blog) chose pydantic for all its generated SDK models. HN commenters in the 2025 thread treated validation as an "overpowered assert" and warned that splitting models breeds DTO sprawl.
- **Strongest argument.** Separate models mean mapping code and duplicate DTOs. Python's static types are not enforced at runtime, so runtime checks catch what mypy cannot.
- **Weakness.** SQLModel's own table models do not validate on construction (sqlmodel issue #52), so its "one class that validates everywhere" does not hold.

**Camp A3: TypedDict for dict-shaped data.**

- PEP 589 made TypedDict for existing dict-heavy code and JSON-like data.
- Jamie Chang (2024) argues TypedDict beats dataclasses for partial updates, where an absent key differs from `None`, and for typed `**kwargs`.
- BiteCode (2025) presents TypedDict as the middle rung between `NewType` and full classes.
- Critics:
  - The attrs docs, as above.
  - Donatas Rasiukevicius (Lab Digital, 2020; stale, since several of his complaints predate PEP 655).
  - Lucy Linder (2023), who found literal-only keys a real limit.

**Camp A4: msgspec Structs for everything.** The msgspec docs present Structs as the preferred way to define structured types. The payoff is decode speed and GC behaviour.

**Cost evidence.** Numbers that hold up across sources:

| Source | Construct one small record |
|---|---|
| msgspec benchmark (msgspec 0.18.5, attrs 23.1, pydantic 2.5.2) | msgspec 0.09 µs, class/dataclass/attrs ~0.35-0.37 µs, pydantic 1.54 µs |
| Han Lee 2025 | dataclass ~6.5x faster than pydantic from a dict |
| This review, Py 3.14 (section 5) | dict literal 0.14 µs, attrs 0.27, dataclass 0.35, `TypeAdapter(TypedDict)` 0.72, pydantic 1.24, `model_construct` 2.17 (slower than validating) |

- **Attribute reads cost the same everywhere.** A dict subscript takes 29 ns, a dataclass attribute 24 ns and a pydantic attribute 43 ns.
- **Pydantic's own docs say it is usually not the bottleneck**, and that `model_construct` is no longer much faster than validating (matching the measurement above).
- **Counter-example:** Samuel Colvin says the pydantic v1-to-v2 upgrade cut an LLM company's time-to-first-token by 20%. So pydantic can be a real share of CPU in request-hot paths. That is not the situation of a network-bound crawler.

**Consensus.**
- Runtime validation belongs where untrusted data enters.
- Inside, static typing plus cheap classes is the default.
- Performance does not settle the question for I/O-bound programs.

**Genuine disagreement.**
- (a) **Can a validated boundary model be used internally?** Hynek, cattrs and van de Ven say translate it into a domain object. A2 and the pragmatists say that adds DTO duplication without benefit.
- (b) **Should internal dicts be typed as TypedDicts or turned into classes?** attrs says classes. PEP 589 and Chang say TypedDict when dict semantics are needed.

The literature does not settle (a) in general. It turns on whether the boundary model and the internal concept really differ. For configuration they usually do not.

### B. "Parse, don't validate" and boundary discipline

**Primary source.** Alexis King (2019): parse input at the boundary into a type that carries the evidence of its checks, then keep that type. She names the anti-pattern "shotgun parsing" (checks scattered through processing).

**The Python adaptation.** BiteCode (2025) agrees: parse at program edges. It warns that Python enforces none of this, so the checker is what makes the contract hold.

**Is `model_dump()` then cast/TypedDict an anti-pattern?**
- On King's terms, yes. The dump throws away the parsed type, and the cast re-asserts a shape that nothing checks.
- The static side cannot fix this. A TypedDict cannot be derived from a pydantic model or a dataclass:
  - pydantic discussion #2574 (2021, still answered "no" in 2025)
  - mypy issue #10104, open since 2021 (typing `asdict()` results as TypedDicts)
  - the 2025 discuss.python.org "`dataclasses.asdicttype`" thread, which ended with no decision, pending intersection types
- So a hand copy like TrackDict can only be synced by a names test. Experiment E9 shows a `cast` letting `verify_top: str` stand against a real `int` with no error.
- The pragmatic norm is legitimate at output edges only: dumping right before writing JSON, SQL parameters or a UI payload is itself a boundary.

**Frozen models vs dicts for configuration.**
- Pydantic supports `frozen=True` (shallow).
- *Architecture Patterns with Python* (Percival and Gregory) recommends immutable value objects.
- No credible source argues for untyped dicts as post-validation configuration, except maps whose keys are data (weights by axis name).

**Consensus:** parse once, keep the rich type, and serialise only at output edges. **Disagreement:** only about whether the rich type may be the boundary library's own class (see A(a)).

### C. Typing SQL rows

**C1. Types-first ORM.**
- SQLAlchemy 2.0 derives column type and nullability from `Mapped[...]` annotations. SQLModel goes further and makes one class the table and the validator.
- Even SQLAlchemy cannot type `row.id` on Core rows. The maintainers point users to `.tuples()` / `.t` instead (SQLAlchemy discussion #10487, 2023).

**C2. SQL-first, with a row factory at the I/O boundary.**
- psycopg 3's `Cursor[Row]` generics and `class_row(Model)` carry the row type statically. The docs are candid that the match between query and class is trust-based; pydantic models add a runtime check.
- Denis Laxalde (Dalibo, 2022) argues for converting rows to domain classes early.
- The sqlite3 docs show dict and namedtuple factories and say a dataclass works the same way. They also recommend `sqlite3.Row` as the optimised default.

**C3. SQL-first codegen.**
- sqlc-gen-python generates dataclasses or pydantic models from SQL. For SQLite, every field comes out as `Any` (issue #64, open since 2024-11). That makes it immature for this stack.
- aiosql loads SQL files as methods but offers no static result types.

**C4. Dicts plus a TypedDict mirror plus one cast.** This is PEP 589's stated motivation, and it is what the project does.

**Trust.**
- Bayer's "database data needs no re-validation" assumes the database enforces its types.
- SQLite by default does not. Its documentation says any column can hold any storage class; affinity is only a preference.
- STRICT tables (SQLite 3.37, 2021) make the database reject values it cannot convert losslessly.

**Consensus:** convert or type rows at the I/O boundary; nobody recommends untyped dicts. **Disagreement:** classes vs dicts, and schema-first vs types-first. Large Postgres shops lean on C1 or C2. SQLite-centric tools (sqlite-utils style) stay dict-based.

### D. One source of truth for a shape that lives in SQL, a validator and a static type

| Direction | Tools | Failure mode |
|---|---|---|
| SQL to static types (codegen) | sqlc | Build step; SQLite types come out `Any` in sqlc-gen-python; generated code drifts if not regenerated in CI |
| SQL to runtime validator (reflection) | `create_model` from PRAGMA (today's import) | No static type; validates keys only |
| Types to SQL | SQLAlchemy / SQLModel | Needs migration tooling; SQLModel table models skip validation |
| Model to TypedDict | none statically (pydantic #2574, mypy #10104, asdicttype 2025); datamodel-code-generator `--input-model` can emit TypedDict code from a pydantic class | Codegen still leaves a dump+cast seam at runtime |
| TypedDict to runtime validator | pydantic `TypeAdapter` (verified). A `closed=True` TypedDict becomes `extra='forbid'` (pydantic source reads `__closed__`); `Annotated[..., MinLen(1)]` carries constraints | Lax coercions differ from SQLite's; mypy ignores the Annotated constraints; the default extra mode silently drops unknown keys |
| Duplicate plus sync test | tests/test_store.py name checks; Luke Plant (2025) argues for import-time assertions over type-level cleverness | Checks names, not value types (true of both of our tests) |

For a two-table SQLite app with one developer, **a sync test that checks names and base types** is the cheapest honest option. **Deriving write lists from the mirror** removes the one duplicate that drifts silently.

### E. TypedDict mechanics this design relies on

**Rules (typing spec, PEPs 589, 655, 692, 705 and 728):**
- **Mapping, not dict.** A TypedDict is assignable to `Mapping[str, object]`, never to `dict[str, Any]`, because dict permits destructive operations. Jukka Lehtosalo reaffirmed this on discuss.python.org in 2023 and pointed to `Mapping` as the answer, so the CompanyRow cascade is the design working as intended.
- **`.get(e)` and `e in d` are allowed for any `str`, by the spec itself.** Not flagging `td.get("typo")` is spec-conformant, not a mypy bug. A 2025 thread proposing a stricter rule found mypy, pyright and pytype all permit `.get` on unknown keys; it has no resolution.
- **Requiredness:** `Required` and `NotRequired` override `total`. A subclass cannot flip a mutable inherited key's requiredness (mypy 2.3.1 errors with "can be deleted in base class", E14).
- **PEP 705 (`ReadOnly`, final, Python 3.13):** read-only items let a function accept structurally compatible TypedDicts.
- **PEP 728 (`closed`, `extra_items`, final 2025-08-15, typing in 3.15, typing_extensions ≥4.13):** a closed source may omit a read-only, non-required key of the target.

**Checker status.** Lines marked "verified" were checked with mypy 2.3.1; the rest come from docs and trackers.

| Feature | mypy 2.3.1 | pyright | pyrefly | ty |
|---|---|---|---|---|
| Literal-typed variable / Literal-union keys | works, read and write (verified; mypy docs still say "only string literals", which is stale) | supported (not verified locally) | not verified | TypedDict support incomplete (ty #154) |
| PEP 692 `**kw: Unpack[TD]` | works, including typo suggestions (verified) | supported | not verified | open (#154) |
| `ReadOnly` | works (verified; `from typing` needs 3.13, and CI still runs 3.12) | supported | not verified | open |
| `closed=True` | accepted; `.get("typo")` then types as `None` (verified); ReadOnly-view assignability and `in` narrowing work (verified) | supported per conformance suite | 96.9% conformance claim (Meta's own blog) | open (#3096) |
| closed TD to `Mapping[str, V]` for non-Any `V` | **rejected** (spec says allowed; verified) | not verified | not verified | open |
| `extra_items=` | **not supported** (verified: "Unexpected keyword argument") | supported | supported per blog | open |
| subscript of a NotRequired key | allowed silently (verified) | error by default (`reportTypedDictNotRequiredAccess`, all modes) | not verified | not verified |

**Safe to depend on in 2026 with mypy:** `Mapping` parameters, Literal keys, `Unpack`, `ReadOnly` (from typing_extensions), `closed=True`. **Not yet safe:** `extra_items`, Mapping-assignability of closed TypedDicts, and anything ty-based. **Portability warning:** under pyright, every `row["name"]` on a `total=False` CompanyRow is an error by default.

### F. Experience with migrating dict-heavy code

- **Dropbox (2019, old but primary):** about three years to type 4M lines. Types paid off most in refactoring and comprehension. TypedDict itself came out of their JSON-shaped dicts. Their regret was partial coverage, where unchecked imports degraded precision.
- **Lucy Linder (2023):** reverted a Django mypy migration. Literal-only keys, Optional cascades and mixins made the added complexity not worth it. Her lesson: type from the start or accept a hard retrofit.
- **Jukka Lehtosalo (2023):** the dict/TypedDict incompatibility exists to protect people migrating from `dict[str, Any]`, and `Mapping` covers the legitimate cases. The cascade is the price of that protection.
- **Armin Ronacher (2023):** types have real costs (syntax, slowness, awkwardness) as well as value. Choose them without stigma.
- **Tin Tvrtković (2025, asdicttype thread):** repeatedly questioned whether precisely typed dicts help serialisation-heavy code at all.
- **Boundary-only typing:** Buczyński and Han Lee argue that validation at the edge plus strict mypy inside is enough.

**Takeaway.** Typed shapes pay off where data crosses module boundaries and where a silent drop or typo has bitten. The payoff falls off for local, short-lived dicts. None of the sources ran a controlled measurement; all of it is experience reporting.

### G. Opinions that the whole framing is wrong

- **"Your schema models are not your domain" (Hynek, cattrs):** do not let pydantic or DB shapes become the core. Partly accepted: for rows and hits the "domain" is thin, and the owner's principle of folding into existing modules argues against a parallel domain layer.
- **"Pydantic everywhere" (SQLModel, Speakeasy, HN):** one class per concept, runtime asserts everywhere. Partly accepted for configuration only; see section 4.
- **"Classes everywhere" (attrs):** no dicts for fixed fields. Rejected for SQL rows (see decision (i)), accepted for records that never need dict semantics.
- **"Test behaviour, not dict shapes" (Ronacher-adjacent, Linder, Luke Plant's plain runtime assertions):** partly accepted. The repo already verifies claims with doctests and invariant tests. Section 4 accepts the diminishing-returns half and keeps typing only the shapes that cross modules.

---

## 3. Application to our case: decision table

| # | Decision | Recommendation | Confidence | Basis | Cost | Evidence that would flip it |
|---|---|---|---|---|---|---|
| i | TypedDict for SQL-shaped store rows | **Keep.** `JobIn`'s "missing key means leave what is stored" is PATCH semantics that only key absence can express. Rows also flow into `{**row}`, SQL parameters, JSON and dynamic column lists. **Add:** (a) extend the sync tests from names to base types (INTEGER to int, TEXT to str, REAL to float, ignoring `\| None`); (b) split the reader type (full `SELECT *`, total=True) from the writer/patch type (total=False) when convenient, so reads use checked subscripts instead of unchecked `.get`, and pyright or pyrefly would not flood with NotRequired-access errors | High (keep); medium (split) | PEP 589; Chang 2024; psycopg/Dalibo (type at the I/O boundary); spec on `.get`; pyright config docs; E1, E2 | Split: moderate (reader signatures); type test: ~20 lines | Rows stop needing dict operations (for example a move to an ORM or a row factory into classes), or partial SELECT readers turn out to dominate, making a total=True reader type a lie |
| ii | Remove TrackDict; use the parsed Track with attribute access | **Do it.** The 27-field hand copy is re-described with `cast`; its value types are unverified (E9); it cannot be derived statically; attribute access costs nothing extra (42 ns vs 29 ns). **Required first:** move `_engine_fills_the_rest` from an `after` validator returning `model_copy` to a `before` validator. `Track(engine="sweep")` via `__init__` currently keeps local defaults (geo_gate True, verify_top 15) with only a UserWarning (verified). Model the runtime fields (`id`, resolved `label`/`track`, `db_path`) as a `RuntimeTrack(Track)` subclass built once in `_runtime` (composition would change every read site to `t.spec.x`). Make it frozen and update the fixtures: `local_track.model_copy(update=...)` and `monkeypatch.setitem(UI_TRACKS, id, copy)`. Remember `model_copy(update=)` does not validate (verified) | High (direction); medium (subclass vs composition) | King 2019; pydantic #2574; mypy #10104; asdicttype thread; pydantic frozen docs; E9, timing, Track experiment | Codemod of ~105 `t["..."]` sites in 17 files, the fixtures in conftest/test_digest, the doctests in tracks.py (`default_track_id` takes dicts), plus the validator fix. About half a day | Tracks must become user-editable or JSON-merged dicts at runtime (for example the Settings tab rewriting them live), or many consumers genuinely need `{**t}` |
| iii | BoardHit as a TypedDict | **Keep, but prefer `closed=True` over (or alongside) `@final`.** Hits are passed to functions that also take store rows, and tests index them. `closed` makes `.get("typo")` type as `None`, allows the ReadOnly-view parameter in (vi), and keeps `in` narrowing (verified without `@final`). **Watch** the hit-vs-miss overloading (`reason` only on a miss). If misses grow fields, make them a separate type in a union | Medium | attrs "dicts are not for fixed fields" argues for a dataclass; outweighed by the row polymorphism; E7, E15 | Small; needs `typing_extensions.TypedDict` and mypy ≥2.2 | Hits stop being passed to row-accepting functions: then a frozen dataclass plus an explicit `coords.from_hit` conversion is cleaner |
| iv | Derive upsert_company's columns from a TypedDict; validate imports via TypeAdapter | **Derive the write list: yes.** The hand-kept 20-name tuple is untested and is exactly the "stores nothing, says nothing" risk `rows.py` exists to prevent. It deliberately omits `id` and 6 schedule columns, so declare a `CompanyWrite` base (total=False, closed) and a `CompanyRow(CompanyWrite)` adding `id` and the schedule columns (the split-base pattern). The list then derives from `CompanyWrite`'s keys. **Import via TypeAdapter: yes, with four guards**, or it regresses: (1) the TypedDict must be `closed=True` (or `with_config(extra="forbid")`), since the default silently drops unknown columns (verified); (2) `name: Annotated[str, MinLen(1)]` keeps the non-empty check (verified; `annotated_types` keeps pydantic out of `rows.py`); (3) `name` required: a subclass cannot flip it (verified), so check it after validation or declare it Required in the writer type; (4) build the adapter once. **It is stricter:** `"5"` becomes 5, `True` becomes 1, `5.0` becomes 5, but 5.5 in an int column or 123 in a text column now fails the whole import, where today's `Any` fields let them through (verified). That is the right strictness at a user-file boundary. Note the create_model it replaces was derived from the live schema, not hand-kept, so the gain is value validation, not de-duplication | High (derive the list); medium (import) | pydantic TypeAdapter docs and source (`__closed__` becomes forbid); E14 and the runtime experiment; SQLite affinity doc | Small: one split, one adapter, the guards, one test | Real exports contain values SQLite accepted but pydantic rejects (legacy mixed-type columns). Then keep key-only validation, or make the tables STRICT first |
| v | dataclass for FitResult and Candidate | **Keep both.** FitResult is a derived object (post-processed gates, computed score, model id, unscored variants with no axes) with behaviour: a dataclass, not a copy of `FitReply`. Candidate is mutable pipeline state. `Candidate(**DiscoveredCompany...model_dump())` loses static checking but fails loudly at runtime on any field drift (unexpected or missing keyword). Optional: construct it with explicit keywords, and split `slug_guess`'s two meanings (guess in, resolved out) into two fields. That dual meaning is the kind of state "parse, don't validate" warns about | High (keep); low (tweaks) | Cosmic Python; attrs; msgspec docs; Buczyński; timing (all options sub-µs) | None / tiny | FitResult or Candidate crossing a trust boundary (then pydantic), or a profile showing construction hot (no evidence it ever will be) |
| vi | `Mapping[str, Any]` vs a union of TypedDicts for hit-or-row parameters | **Short term, keep `Mapping[str, Any]`,** following the typing best-practices advice to take abstract types as parameters. It is honest, but typo-blind (E13). **Target: once BoardHit and CompanyRow are `closed`, use a ReadOnly "view" TypedDict** (for example `BoardCoords` with `ats`, `slug`, `wd_*`, `careers_url` as `ReadOnly`, non-required). mypy 2.3.1 accepts both closed types for it (verified), while plain dicts are rejected, which forces callers to be typed first. **Avoid a raw `BoardHit \| CompanyRow` union:** `.get(col)` with a Literal-union key collapses to `object` (verified), and it fails for open types without `@final` narrowing tricks | Medium | typing best-practices doc; PEP 705; the spec's closed-source rule; E13, E15, E16 | View type: ~10 lines plus migrating untyped callers | Moving to a checker without PEP 728 (ty today), or untyped dict callers staying common, in which case Mapping remains the pragmatic choice |
| vii | What we are doing wrong or missing | See the list below | Mixed | Mixed | Mixed | Mixed |

**Decision vii, in detail:**

1. **SQLite does not enforce the types the `cast` claims.** Convert `companies` and `jobs` to STRICT tables (the columns already use INTEGER/TEXT/REAL; this needs a table rebuild in a migration), or add a cheap invariant test over the real store checking that `typeof(col)` matches affinity. Medium: it makes decision (i)'s one cast honest.
2. **Sync tests check names only** (CompanyRow, TrackDict). By DOCSTRINGS.md's own rule, the annotated value types are unenforced claims. Extend the tests to base types. High.
3. **If adopting `closed` or `ReadOnly`:**
   - import from `typing_extensions` (CI runs 3.12; `typing.ReadOnly` is 3.13+, `closed` is 3.15);
   - list `typing_extensions` in `requirements.txt` (today it is only transitive via pydantic);
   - raise the mypy floor in `requirements-dev.txt` from `>=1.14` to `>=2.2` (closed support), ideally `>=2.3`, since the Literal-key behaviour was verified on 2.3.1.
   High.
4. **Other dump seams in `src/config/profile.py`.** `MISSION_TIERS`, `KEYWORDS_BY_TRACK` / `EXCLUDE_BY_TRACK` and `FIT_DOMAIN_LADDER` are `model_dump()`ed into module constants, the same seam as TrackDict. `FIT_WEIGHTS` and `FIT_GATE_PENALTY` are legitimate dicts (keys are data, iterated by axis). Convert the fixed-field ones opportunistically. Low to medium.
5. **Do not adopt msgspec, attrs or a codegen step.**
   - msgspec's advantages (decode speed, GC) do not apply to a network-bound crawler, and it would be a third modelling library.
   - attrs adds little over dataclasses for three classes (it is already installed transitively, but that does not make it the right tool).
   - sqlc-gen-python emits `Any` for SQLite.
   - A PRAGMA-to-`rows.py` generator costs more than a type-aware sync test for two tables.
   High.
6. **The `.get("literal")` gap cannot be closed by any checker today** (the spec allows it). The practical fix is decision (i)'s total=True reader type (subscripts are checked), plus closed types (a typo's `.get` types as `None`). An AST invariant would be noisy. Medium.
7. **Correction to hypothesis 1's wording.** Pydantic is not "the only one that validates at runtime": attrs validators, cattrs structuring and msgspec decoding also do. The accurate rule is "runtime validation runs only at trust boundaries, and pydantic is our tool for it". Low.

---

## 4. Steelman of the opposite thesis, and how much of it I accept

**The opposite thesis:** "Stop hand-typing dicts. Parse every fixed-shape datum into a pydantic model, SQL rows included, and use attribute access everywhere."

- The crawler is network-bound, so validating every row costs 1-8 µs × a few thousand rows ≈ tens of ms per pass: invisible.
- SQLite is not a trustworthy boundary. Any column holds any type, so `cast(CompanyRow, dict(row))` is an unchecked promise, and zzzeek's "DB data is already validated" argument does not transfer from Postgres.
- One model per table gives static and runtime checking from one declaration. `model_dump(exclude_unset=True)` preserves key-absence semantics for writes.
- It removes the TypedDict mechanics that checkers disagree about: `.get` typos, total=False noise under pyright, closed support gaps, the `dict[str, Any]` cascade.
- Hand-kept TypedDicts need sync tests. A model is its own validator.

**What I accept:**
- **The SQLite-trust point, fully.** That is why STRICT tables or a typeof audit (vii.1) is recommended.
- **The performance argument, fully.** It removes performance as a reason to prefer TypedDict.
- **For configuration (Track), the thesis wins outright.**

**What I reject:**
- **For rows**, the dict operations the codebase depends on (`{**row}`, `.get`, dynamic column lists in `upsert`/`apply_update`, JSON export, partial SELECTs with different column subsets) would each need a model or a dump. That brings back the dump-then-dict seam this review identifies as the anti-pattern, spread across more places, plus mapping code for every partial SELECT.
- `model_dump(exclude_unset=True)` does preserve absence, but only for models built from dicts. Code that builds a row by keyword (`coords.columns(... **extra)`) must then track `model_fields_set` discipline by hand.
- **Net:** about 40% accepted, concentrated where it matters (trust in SQLite, configuration). The rows stay dicts, but made honest.

**The "stop typing dicts, test behaviour" steelman.** `rows.py` exists because a misspelled key silently stored nothing, yet mypy still cannot catch the `.get("typo")` form of that same bug. 473 `dict[str, Any]` remain, and typing them all is churn.

I accept the diminishing-returns half: do not chase the remaining 473 wholesale; type only shapes that cross module boundaries or feed writes. I reject abandoning types: construction-time checking (E10: unknown and mis-typed keys in dict literals and `Unpack` kwargs) is exactly the class of bug `rows.py` was created for, and it works.

---

## 5. What I could not verify, and the experiments I ran

**Could not verify locally:**
- pyright, pyrefly and ty behaviour. These come from their docs, trackers and the Meta-authored conformance post; treat pyrefly's numbers with that bias in mind.
- pydantic on Python 3.12. The docs say `typing_extensions.TypedDict` is required on "3.12 and lower", but pydantic 2.13.5's source checks `sys.version_info >= (3, 12)`, so `typing.TypedDict` should be fine on 3.12. I trusted the source.
- msgspec timings (not installed; I used its published benchmark).
- The maintainers' reply in SQLModel issue #52.
- Colvin's 20% TTFT anecdote (single unnamed-company claim).
- Whether sqlc-gen-python's SQLite `Any` issue has been fixed since the fetch.

All scratch files are in the scratchpad directory (`exp_mypy*.py`, `exp_pydantic.py`, `exp_timing.py`, `exp_minlen.py`).

**mypy 2.3.1 (`--strict`), abbreviated code and results:**

- **E1.** `r.get("slgu")` on an open total=False TD reveals `object` with no error; `r["slgu"]` is an error.
- **E2.** `return r["name"]` on a NotRequired key: no error.
- **E3.** `c: Literal["slug","wd_pod"]`: `r[c]` reads as a union and assignment works; a plain `str` key is an error.
- **E4.** A TD passed as `dict[str, Any]` is an error; `Mapping[str, Any]` and `Mapping[str, object]` are OK.
- **E5.** `def build(**kw: Unpack[Row])`: `build(wd_pdo=3)` gives "did you mean wd_pod?", and a wrong type is caught.
- **E6 and E12.** `class Closed(typing_extensions.TypedDict, total=False, closed=True)`: `c.get("nmae")` reveals `None` (then `.upper()` errors); still not assignable to `dict[str, Any]`; **not assignable to `Mapping[str, int | str]` or `Mapping[str, str]` even when every value fits** (a spec deviation).
- **E7.** `@final` hit/row union: `if "wd_tenant" in x` narrows to CompanyRow.
- **E8.** `ReadOnly` parameter accepts a wider TD; rejects one where the key is not required.
- **E9.** `cast(TrackD, Track().model_dump())` with `verify_top: str` declared gives no error, and the reveal says `str` while the value is an `int`; `m.verify_topp` is caught with a suggestion.
- **E10.** `{**r, "nmae": "a"}` gives "Extra key"; `{"id": "1"}` gives an item type error.
- **E11.** `@final` open TD: `.get("nmae")` is `object`, no error.
- **E13.** `extra_items=str` gives "Unexpected keyword argument" (unsupported). Under `Mapping[str, Any]`, `.get("atz")` is `Any | None`.
- **E14.** `class CompanyImport(CompanyRow): name: Required[str]` gives "Field 'name' can be deleted in base class". A column tuple derived from `__annotations__` is `tuple[str, ...]` and cannot index a TD.
- **E15.** A union parameter `BoardHit | CompanyRow`: `.get("ats")` is precise, `.get(first: HandleColumn)` is `object`, and a plain dict argument is rejected.
- **E16.** A ReadOnly view `BoardCoords` rejects an open BoardHit even with `wd_tenant: ReadOnly[object]` (the spec allows this), but **accepts both types once they are `closed=True`**, and `in` narrowing works on closed types without `@final`.

**pydantic 2.13.5 runtime (import-row validation, one JSON row each):**

| Input | create_model(Any) today | TypedDict default | TypedDict forbid / closed |
|---|---|---|---|
| `wd_pod: "5"` | kept `"5"` | 5 | 5 |
| `active: true` | kept `True` | 1 | 1 |
| `wd_pod: 5.5` | kept | int_from_float error | error |
| `slug: 123` | kept | string_type error | error |
| unknown column | extra_forbidden | **silently dropped** | extra_forbidden |
| blank name | string_too_short | **accepted** | accepted (unless `Annotated[str, MinLen(1)]`, verified) |
| missing name | missing | missing (with `Required[str]`) | missing |

**Timing (best of 5 × 200k, Python 3.14.7):**

| Operation | Time |
|---|---|
| dict literal | 141 ns |
| attrs | 267 ns |
| dataclass | 353 ns |
| dataclass(slots, frozen) | 606 ns |
| TypeAdapter(TypedDict) | 718 ns |
| BaseModel(**kw) | 1243 ns |
| model_construct | 2168 ns |
| read one field | 23-43 ns for all |
| real `Track.model_dump()` | 2.4 µs |
| `Track.model_validate({})` | 8.4 µs |

**Track bug:** `Track(engine="sweep")` yields geo_gate True, verify_top 15, store_tag None (local defaults, with a pydantic UserWarning). `Track.model_validate({"engine": "sweep"})` yields False, 0, "sweep".

---

## 6. Sources

Credibility: **P** = primary (spec, PEP, maintainer docs), **M** = maintainer or recognised expert opinion, **O** = practitioner opinion, **V** = vendor.

**Typing spec, PEPs and checkers**
1. Typing spec, TypedDict chapter. https://typing.python.org/en/latest/spec/typeddict.html. Current. P.
2. PEP 589 (TypedDict). https://peps.python.org/pep-0589/. 2019. P; its dict/Mapping rationale is still current.
3. PEP 692 (Unpack kwargs). https://peps.python.org/pep-0692/. Final, 3.12. P.
4. PEP 705 (ReadOnly). https://peps.python.org/pep-0705/. Final, 3.13. P.
5. PEP 728 (closed/extra_items). https://peps.python.org/pep-0728/. Final 2025-08-15, 3.15. P.
6. Typing best practices. https://typing.python.org/en/latest/reference/best_practices.html. Current. P.
7. mypy TypedDict docs (2.3.1). https://mypy.readthedocs.io/en/stable/typed_dict.html. P; the "only string literals" line is stale.
8. mypy changelog. https://mypy.readthedocs.io/en/stable/changelog.html. P (closed TypedDicts in 2.2).
9. mypy #18176, PEP 728 support. https://github.com/python/mypy/issues/18176. Open. P.
10. mypy #8923, TypedDict vs Dict[str, Any]. https://github.com/python/mypy/issues/8923. Closed. P (thin page).
11. mypy #10104, asdict to TypedDict. https://github.com/python/mypy/issues/10104. 2021, open. P.
12. discuss.python.org, "Should TypedDict be compatible with dict[Any, Any]?". https://discuss.python.org/t/should-typeddict-be-compatible-with-dict-any-any/40935. 2023-12. M (Jukka Lehtosalo).
13. discuss.python.org, "TypedDict operations with unknown literal keys". https://discuss.python.org/t/typeddict-operations-with-unknown-literal-keys/89653. 2025-04. O (checker behaviour table).
14. discuss.python.org, "`dataclasses.asdicttype(type)`". https://discuss.python.org/t/dataclasses-asdicttype-type/103448. 2025-08/09. M (Tin Tvrtković).
15. Pyright configuration docs. https://github.com/microsoft/pyright/blob/main/docs/configuration.md. Current. P.
16. ty #154, Advanced TypedDict support. https://github.com/astral-sh/ty/issues/154. Open. P.
17. Pyrefly, "Typing spec conformance comparison". https://pyrefly.org/blog/typing-conformance-comparison/. 2026-03-10. V (Meta, authors of pyrefly).
18. Brett Cannon, "The varying strictness of TypedDict". https://snarky.ca/the-varying-strictness-of-typeddict/. 2025-11-20. M (CPython core dev).

**Modelling libraries and their maintainers**
19. attrs, "Why not…". https://www.attrs.org/en/stable/why.html. attrs 26.1. M (Hynek Schlawack).
20. Hynek Schlawack, "Design Pressure" talk page. https://hynek.me/talks/design-pressure/. 2025-05. M.
21. Hynek Schlawack, "Know Your Models". https://hynek.me/articles/know-your-models/. 2013, updated 2019. M; older.
22. cattrs, "Why cattrs?". https://catt.rs/en/stable/why.html. Current. M.
23. msgspec benchmarks. https://msgspec.dev/benchmarks. Versions from 2023. M; author's caveats noted.
24. msgspec Structs. https://msgspec.dev/structs. Current. M.
25. msgspec, "Why msgspec". https://msgspec.dev/why. Current. M (thin on comparisons).
26. pydantic, Performance tips. https://pydantic.dev/docs/validation/latest/concepts/performance/. Current. P.
27. pydantic, Models (`model_construct`, frozen). https://pydantic.dev/docs/validation/latest/concepts/models/. Current. P.
28. pydantic, TypeAdapter. https://pydantic.dev/docs/validation/latest/concepts/type_adapter/. Current. P.
29. pydantic, Standard library types (TypedDict). https://pydantic.dev/docs/validation/latest/api/standard_library_types/. Current. P; its version note conflicts with the source.
30. pydantic, Dataclasses. https://pydantic.dev/docs/validation/latest/concepts/dataclasses/. Current. P.
31. pydantic discussion #2574, TypedDict from BaseModel. https://github.com/pydantic/pydantic/discussions/2574. 2021, comments into 2025. M.
32. Samuel Colvin on Latent Space. https://www.latent.space/p/pydantic. 2025-02-06. M; anecdotal.
33. SQLAlchemy discussion #9385, pydantic instead of dataclasses. https://github.com/sqlalchemy/sqlalchemy/discussions/9385. 2023-02-28. M (Mike Bayer).

**Parse, don't validate, and architecture**
34. Alexis King, "Parse, don't validate". https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/. 2019. M; foundational.
35. BiteCode, "What 'Parse, don't validate' means in Python". https://www.bitecode.dev/p/what-parse-dont-validate-means-in. 2025-07-23. O (experienced).
36. Percival and Gregory, *Architecture Patterns with Python*, ch. 1. https://www.cosmicpython.com/book/chapter_01_domain_model.html. 2023 edition. M.
37. Erik van de Ven, "Keep Pydantic out of your Domain Layer". https://coderik.nl/posts/keep-pydantic-out-of-your-domain-layer/. 2025-07-22. O.
38. HN discussion of #37. https://news.ycombinator.com/item?id=44656419 and https://news.ycombinator.com/item?id=44694484. 2025-07. O (both camps).
39. Sebastian Buczyński, "Why so pedantic about pydantic". https://pythoneer.substack.com/p/why-so-pedantic-about-pydantic-or. 2023-02-16. O (experienced).
40. Han Lee, "Pydantic is all you need for performance spaghetti". https://leehanchung.github.io/blogs/2025/07/03/pydantic-is-all-you-need-for-performance-spaghetti/. 2025-07-03. O; ad-hoc benchmark.
41. Speakeasy, "Pydantic vs dataclasses". https://www.speakeasy.com/blog/pydantic-vs-dataclasses. 2024-08-29. V.
42. Jamie Chang, "TypedDicts are better than you think". https://blog.changs.co.uk/typeddicts-are-better-than-you-think.html. 2024-10-01. O.
43. Donatas Rasiukevicius, "Sorry, you're just not my type". https://www.labdigital.nl/blog/sorry-you-re-just-not-my-type. 2020-03-04. O; stale.
44. Luke Plant, "Statically checking Python dicts for completeness". https://lukeplant.me.uk/blog/posts/statically-checking-python-dicts-for-completeness/. 2025-06-27. M (Django core alumnus).
45. Luke Plant, "The different uses of Python type hints". https://lukeplant.me.uk/blog/posts/the-different-uses-of-python-type-hints/. 2023-04-05. M.
46. Armin Ronacher, "Untyped Python: The Python That Was". https://lucumr.pocoo.org/2023/12/1/the-python-that-was/. 2023-12-01. M.
47. Adam Johnson, "How to use TypedDict". https://adamj.eu/tech/2021/05/10/python-type-hints-how-to-use-typeddict/. 2021. M; thin on this question.

**Migration experience**
48. Dropbox, "Our journey to type checking 4 million lines of Python". https://dropbox.tech/application/our-journey-to-type-checking-4-million-lines-of-python. 2019. P (experience); old.
49. Lucy Linder, "My unsuccessful journey of migrating a large Django project to mypy". https://blog.derlin.ch/my-unsuccessful-journey-of-migrating-a-large-django-project-to-mypy/. 2023-07-10. O (experience).

**SQL rows and codegen**
50. Python sqlite3 docs, row factories. https://docs.python.org/3/library/sqlite3.html. Current. P.
51. SQLite, "Datatypes In SQLite" (type affinity). https://www.sqlite.org/datatype3.html. Current. P.
52. SQLite, STRICT tables. https://www.sqlite.org/stricttables.html. 3.37 (2021). P.
53. psycopg 3, static typing. https://www.psycopg.org/psycopg3/docs/advanced/typing.html. Current. P.
54. Denis Laxalde (Dalibo), "psycopg row factories". https://blog.dalibo.com/2022/06/01/psycopg-row-factories.html. 2022-06-01. M.
55. SQLAlchemy 2.0, declarative tables and `Mapped`. https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html. Current. P.
56. SQLAlchemy discussion #10487, Row typing. https://github.com/sqlalchemy/sqlalchemy/discussions/10487. 2023-10. M.
57. SQLModel home. https://sqlmodel.tiangolo.com/. Current. V/M (promotional).
58. SQLModel #52, table=True does not validate. https://github.com/fastapi/sqlmodel/issues/52. 2021. P (issue).
59. sqlc-gen-python. https://github.com/sqlc-dev/sqlc-gen-python. Current. P.
60. sqlc-gen-python #64, SQLite fields typed Any. https://github.com/sqlc-dev/sqlc-gen-python/issues/64. 2024-11-27, open. P.
61. aiosql docs. https://nackjicholson.github.io/aiosql/. Current. P.
62. datamodel-code-generator. https://github.com/koxudaxi/datamodel-code-generator. Current. P.
