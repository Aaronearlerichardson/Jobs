"""Cross-module architectural invariants.

Doctests prove that ONE function does what its docstring says. This file
proves things no single docstring can: that a rule lives in exactly one
place, that every module obeys it, and that the test harness itself is
still pointed at the whole tree.

The bug class this exists for: six copies of the company-activation rule
drifted apart, one of them silently, and nothing failed. A doctest on the
correct copy would still have passed. See docs/DOCSTRINGS.md.

Offline like the rest of the suite — these tests read source with `ast`
and call pure functions; nothing here touches the network, the API, or a
real store.
"""

import ast
import asyncio
import builtins
import functools
import re
import time
from collections import Counter
from pathlib import Path

import pytest

from src import config
from src.config import ACTIVE_MISSION_TIERS, is_active_mission

ROOT = Path(__file__).resolve().parent.parent

#: Directories scanned by the source-level guards, plus root-level modules.
SOURCE_DIRS = ("src", "tools")

#: Root-level modules pytest cannot doctest-collect, with the reason. Kept
#: here so `test_pytest_ini_covers_every_source_module` stays honest instead
#: of quietly shrinking its own scope.
#: Empty since the package tree moved under src/: nothing at the root
#: shadows a package name any more, so every launcher is collectable.
UNCOLLECTABLE_ROOT_MODULES = set()


def source_files():
    """Every first-party .py file, as (relative-posix-path, source text)."""
    paths = []
    for d in SOURCE_DIRS:
        paths.extend(sorted((ROOT / d).rglob("*.py")))
    paths.extend(sorted(ROOT.glob("*.py")))
    for p in paths:
        if "__pycache__" in p.parts:
            continue
        yield p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")


@functools.cache
def _parsed():
    """source_files() as (rel, tree), parsed once."""
    return [(rel, ast.parse(src)) for rel, src in source_files()]


# --------------------------------------------------------------------------- #
#  1. The company-activation rule
# --------------------------------------------------------------------------- #
#
# `score_company_mission()` returns (None, None, "") when scoring is
# UNAVAILABLE — no API key, a failed or rate-limited call. That None is not
# a verdict, and every add path must treat it as "keep crawling". Six sites
# implemented that inline; one had drifted to two hard-coded tier names,
# which buried whole discovery sweeps in inactive rows.

#: Every tier name the loaded profile knows, plus the unavailable sentinel.
ALL_TIERS = tuple(t["name"] for t in config.MISSION_TIERS) + (None,)


@pytest.fixture
def multi_division(monkeypatch):
    """Make exactly one name read as a multi-division conglomerate.

    Patched rather than taken from the profile: `profile.example.toml` (what
    CI runs on) configures none, so a profile-derived name would silently
    skip half the truth table.
    """
    name = "Conglomerate Holdings"
    monkeypatch.setattr(config, "is_multi_division",
                        lambda n: (n or "").strip().lower() == name.lower())
    return name


class TestActivationRule:
    """`is_active_mission` is the single source of the active=1/0 decision."""

    def test_active_tier_is_active(self):
        for tier in ACTIVE_MISSION_TIERS:
            assert is_active_mission(tier, "Nowhere Robotics") == 1

    def test_inactive_tier_is_inactive(self):
        inactive = [t["name"] for t in config.MISSION_TIERS if not t["active"]]
        assert inactive, "profile configures no inactive tier to test against"
        for tier in inactive:
            assert is_active_mission(tier, "Nowhere Robotics") == 0

    def test_unknown_tier_is_inactive(self):
        assert is_active_mission("not-a-configured-tier", "Nowhere Robotics") == 0

    def test_unavailable_scoring_stays_active(self):
        """The regression this whole invariant exists for."""
        assert is_active_mission(None, "Nowhere Robotics") == 1

    def test_multi_division_overrides_the_tier(self, multi_division):
        for tier in ALL_TIERS:
            assert is_active_mission(tier, multi_division) == 1

    def test_include_missions_overrides_the_profile(self):
        assert is_active_mission("green", "Nowhere Robotics", ("green",)) == 1
        assert is_active_mission("green", "Nowhere Robotics", ("blue",)) == 0
        # ...but never at the cost of the unavailable arm.
        assert is_active_mission(None, "Nowhere Robotics", ("blue",)) == 1

    def test_returns_int_not_bool(self):
        """It goes straight into an INTEGER column; keep it 1/0."""
        for val in (is_active_mission(None, "X"), is_active_mission("nope", "X")):
            assert type(val) is int

    @pytest.mark.parametrize("tier", ALL_TIERS)
    @pytest.mark.parametrize("multi", [True, False])
    def test_matches_the_pre_refactor_rule(self, tier, multi, multi_division):
        """Truth table: the helper agrees with every inline copy it replaced.

        The lambdas below are the five expressions as they were written at
        the call sites, transcribed verbatim (the arms were in three
        different orders, which is exactly how the drift went unnoticed).
        """
        name = multi_division if multi else "Nowhere Robotics"
        old_rules = [
            # src/discovery/dork.py:138 (harvest_urls), post-fix
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS or t is None
                               or config.is_multi_division(n)) else 0,
            # src/discovery/local_sourcing.py:640 (populate_companies), with
            # include_missions defaulted to ACTIVE_MISSION_TIERS by the caller
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS or t is None
                               or config.is_multi_division(n)) else 0,
            # src/discovery/local_sourcing.py:1114 (resolve_leads)
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS
                               or config.is_multi_division(n)
                               or t is None) else 0,
            # src/discovery/local_sourcing.py:1387 (add_names)
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS or t is None
                               or config.is_multi_division(n)) else 0,
            # src/ops/maintenance.py:697 (add_manual_job)
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS
                               or config.is_multi_division(n) or t is None) else 0,
        ]
        new = is_active_mission(tier, name)
        for i, old in enumerate(old_rules):
            assert new == old(tier, name), (i, tier, name)


class TestOffmissionInactiveIsNotTheActivationRule:
    """`config.offmission_inactive` sits beside `is_active_mission` in
    config/policy.py and reads the same ACTIVE_MISSION_TIERS. They answer
    different questions, and the difference is deliberate -- pinned here
    because nothing else says which is which:

      * is_active_mission decides `active`. An UNSCORED company (tier
        None) is active: scoring was unavailable, so the row is not
        punished for it.
      * offmission_inactive only ever decides a harvest (harvest.plan:
        the long HARVEST_OFFMISSION_HOURS interval, or left out). An
        unscored inactive row reads as off-mission there: nobody has
        bothered to score it, so it does not earn the frequent read.
    """

    def test_an_unscored_row_is_active_but_still_off_mission(self):
        assert is_active_mission(None, "Nowhere Robotics") == 1
        assert config.offmission_inactive({"mission_tier": None,
                                           "active": 0}) == "deferred"

    @pytest.mark.parametrize("tier", ALL_TIERS)
    def test_a_row_the_roster_calls_active_is_never_off_mission(self, tier):
        """Whatever the tier: the activation decision is already recorded
        in `active` (multi-division exemptions included), and this
        predicate never re-litigates it."""
        assert not config.offmission_inactive({"mission_tier": tier,
                                               "active": 1})

    def test_an_active_tier_is_never_off_mission(self):
        for tier in ACTIVE_MISSION_TIERS:
            assert not config.offmission_inactive({"mission_tier": tier,
                                                   "active": 0})


# --------------------------------------------------------------------------- #
#  2. Nobody re-implements the rule inline
# --------------------------------------------------------------------------- #

#: (module, function) pairs allowed to spell the activation rule out.
#:
#: `config.is_active_mission` is the rule (src/config/policy.py, beside
#: `is_multi_division`, which it calls). Still re-exported as
#: `src.claude.api.is_active_mission`, which is what most call sites say.
#:
#: `local_sourcing.score_missions` is the REACTIVATION half and is
#: deliberately NOT the helper: it must not revive a row on `tier is None`.
#: A None tier with a non-None score means the model answered with a mission
#: name outside the profile's taxonomy -- score_company_mission nulls the tier
#: but keeps the score, so the "scoring unavailable" `return` above does
#: not fire. The helper would read that as "unavailable" and revive an
#: already-inactive company off an unrecognised answer.
RULE_SITES_ALLOWED = {
    ("src/config/policy.py", "is_active_mission"),
    ("src/discovery/local_sourcing.py", "score_missions"),
}

#: Names that, compared against with `in`, mean "this is the activation rule".
_TIER_SET_NAMES = {"ACTIVE_MISSION_TIERS", "include_missions"}


def _call_name(node):
    """Dotted-or-bare name of a Call's target, or '' for anything else."""
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _mentions_multi_division(node):
    return any(isinstance(n, ast.Call) and _call_name(n) == "is_multi_division"
               for n in ast.walk(node))


def _is_tier_membership(node):
    """`<x> in ACTIVE_MISSION_TIERS` / `... in include_missions`."""
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)):
        return False
    rhs = node.comparators[0]
    name = rhs.attr if isinstance(rhs, ast.Attribute) else getattr(rhs, "id", "")
    return name in _TIER_SET_NAMES


def find_inline_rule_sites():
    """Every (module, function) that spells the activation rule out inline.

    Two signatures, either of which is the rule being rewritten by hand:

    * an ``or`` whose arms include a ``config.is_multi_division(...)`` call,
    * a membership test against ``ACTIVE_MISSION_TIERS``/``include_missions``.
    """
    hits = set()

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.BoolOp) and isinstance(child.op, ast.Or):
                if _mentions_multi_division(child):
                    hits.add((rel, here))
            if _is_tier_membership(child):
                hits.add((rel, here))
            walk(child, here)

    for rel, tree in _parsed():
        walk(tree, None)
    return hits


def test_activation_rule_is_not_re_implemented():
    """No module may open-code the active=1/0 decision.

    A doctest cannot catch this: the copy it is attached to is correct by
    construction, and the drift lives in the copy nobody looked at.
    """
    found = find_inline_rule_sites()
    unexpected = found - RULE_SITES_ALLOWED
    assert not unexpected, (
        "the company-activation rule is spelled out inline at "
        f"{sorted(unexpected)}. Call config.is_active_mission instead, "
        "or add the site to RULE_SITES_ALLOWED with a written reason.")


def test_allowlisted_rule_sites_still_exist():
    """The allowlist must not rot into a list of places that moved away."""
    found = find_inline_rule_sites()
    stale = RULE_SITES_ALLOWED - found
    assert not stale, (
        f"RULE_SITES_ALLOWED lists {sorted(stale)}, which no longer spells "
        "the rule out. Drop the entry.")


def test_the_guard_can_actually_see_a_violation():
    """The detector is not vacuously passing."""
    src = (
        "def sneaky(tier, name):\n"
        "    return 1 if (tier in ACTIVE_MISSION_TIERS or tier is None\n"
        "                 or config.is_multi_division(name)) else 0\n"
    )
    tree = ast.parse(src)
    boolops = [n for n in ast.walk(tree)
               if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)]
    assert any(_mentions_multi_division(b) for b in boolops)
    assert any(_is_tier_membership(n) for n in ast.walk(tree))


# --------------------------------------------------------------------------- #
#  3. The doctest harness still covers the whole tree
# --------------------------------------------------------------------------- #

def test_pytest_ini_covers_every_source_module():
    """A new root-level module must be added to pytest.ini `testpaths`.

    --doctest-modules only collects from paths pytest is pointed at, so a
    module missing from `testpaths` has its doctests silently skipped —
    which looks exactly like having no doctests.
    """
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    body = ini.split("testpaths", 1)[1].split("addopts", 1)[0]
    listed = {line.strip() for line in body.splitlines() if line.strip()
              and not line.strip().startswith("=")}

    for d in SOURCE_DIRS:
        assert d in listed, f"pytest.ini testpaths is missing the {d}/ tree"
    assert "tests" in listed

    on_disk = {p.name for p in ROOT.glob("*.py")} - UNCOLLECTABLE_ROOT_MODULES
    missing = on_disk - listed
    assert not missing, (
        f"root modules {sorted(missing)} are not in pytest.ini testpaths, so "
        "their doctests never run. Add them (or document an exclusion in "
        "UNCOLLECTABLE_ROOT_MODULES).")


def test_doctests_actually_exist():
    """Guard against the harness going green on an empty collection.

    pytest exits 5 ("no tests collected") on a doctest-only run with zero
    examples, and a `|| true` in CI would turn that into a green tick.
    """
    with_doctests = [rel for rel, src in source_files() if ">>> " in src]
    assert len(with_doctests) >= 5, with_doctests


# --------------------------------------------------------------------------- #
#  4. Threads live in the four sync wrappers                                   #
# --------------------------------------------------------------------------- #

#: The only modules that make, name or reach a thread, and why: the four
#: wrappers around what cannot be awaited. Everything else is coroutines on
#: the entry point's one event loop, CPU work through asyncio.to_thread, and
#: concurrency as tasks (src.net.parallel.fan_out / fetch_all).
THREAD_WRAPPERS = {
    "src/store/schema.py":
        "SQLite: each store.Writer's one DB thread (sqlite3 blocks)",
    "src/net/util.py":
        "CPU parsing: an lxml parser per asyncio.to_thread worker",
    "src/net/ddg.py":
        "ddgs, a sync library: each search on a daemon thread of its own",
    "src/web/server.py":
        "the Flask web UI: its loop thread, and `call`, the one way a request "
        "thread waits on the loop",
}

#: What makes, names or waits on a thread (or a process pool).
THREAD_NAMES = {"threading", "_thread", "concurrent", "ThreadPoolExecutor",
                "ProcessPoolExecutor", "run_coroutine_threadsafe",
                "run_in_executor"}


def _thread_uses(tree):
    """[(line, name)] for each THREAD_NAMES import, name or attribute."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom) and not n.level:
            names = [(n.module or "").split(".")[0], *(a.name for a in n.names)]
        elif isinstance(n, ast.Name):
            names = [n.id]
        elif isinstance(n, ast.Attribute):
            names = [n.attr]
        else:
            continue
        out += [(n.lineno, x) for x in names if x in THREAD_NAMES]
    return out


def test_threads_live_in_the_four_sync_wrappers():
    """A thread outside the wrappers is how a wedged resolution held the web
    UI's single op slot for over an hour (a running thread cannot be
    cancelled), and how a worker waiting on the loop starved it (32 JS
    pages hung discovery). Scans src/, tools/ and the root scripts."""
    snippet = ("import threading\nfrom concurrent.futures import Future\n"
               "async def f(loop):\n    await loop.run_in_executor(None, g)\n")
    assert _thread_uses(ast.parse(snippet)) == [
        (1, "threading"), (2, "concurrent"), (4, "run_in_executor")]
    found = sorted((rel, *u) for rel, tree in _parsed()
                   if rel not in THREAD_WRAPPERS for u in _thread_uses(tree))
    assert not found, (f"{found}: a thread outside the four sync wrappers. Await "
                       "the work, or asyncio.to_thread a CPU-bound call.")


def test_the_thread_wrappers_still_wrap_threads():
    """The allowlist must not rot into a list of modules that moved on."""
    stale = [rel for rel, tree in _parsed()
             if rel in THREAD_WRAPPERS and not _thread_uses(tree)]
    assert not stale and set(THREAD_WRAPPERS) <= {rel for rel, _ in _parsed()}, stale


# --------------------------------------------------------------------------- #
#  5. A store connection is closed however its block ends                     #
# --------------------------------------------------------------------------- #

def _unclosed_connections(src):
    """Functions in `src` that keep a `x = connect(...)` connection without
    a try/finally closing it. Returning it hands it to the caller (the
    factory itself)."""
    out = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        held = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr",
                            getattr(n.value.func, "id", None)) == "connect"
                for t in n.targets if isinstance(t, ast.Name)}
        returned = {n.value.id for n in ast.walk(fn) if isinstance(n, ast.Return)
                    and isinstance(n.value, ast.Name)}
        closed = any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "close"
                     for t in ast.walk(fn) if isinstance(t, ast.Try)
                     for s in t.finalbody for c in ast.walk(s))
        if held - returned and not closed:
            out.append(fn.name)
    return out


def test_store_connections_close_on_every_path():
    """`conn = connect()` ... `conn.close()` leaks on any exception in
    between, and a leaked connection keeps its transaction -- the write
    lock with it -- until garbage collection. Open the store with
    `with closing(store.connect(...)) as conn:`, ops.maintenance.track_store,
    or a try/finally.

    Notes:
        track_store fixed ten of these in ops/ (2026-09); 17 more had
        regrown or survived in crawl/, discovery/, claude/, capture.py and
        tools/ by 2026-09-22 (one never closed at all).
    """
    assert _unclosed_connections(
        "def f():\n    conn = connect()\n    conn.close()\n") == ["f"]
    offenders = {rel: names for rel, src in source_files()
                 if "connect(" in src and (names := _unclosed_connections(src))}
    assert not offenders, f"store connections that can leak: {offenders}"


def test_board_specs_are_json():
    """config.BOARDS holds only what JSON can: moving it to a JSON file
    later must be a copy, not a rewrite. (Each spec is also checked
    against the schema when src.ats.board.engine builds its engine.)"""
    import json
    assert json.loads(json.dumps(config.BOARDS)) == config.BOARDS


# --------------------------------------------------------------------------- #
#  6. The mechanical performance rules (docs/PERFORMANCE.md)                  #
# --------------------------------------------------------------------------- #
#
# Each check is a shape the AST shows without guessing at types, kept narrow
# on purpose: a false positive teaches people to ignore the test. The
# judgment rules (hot paths, caching, __slots__) are the reviewer's.

#: Names a binding may not reuse. site's additions (exit, help, ...) are
#: not language builtins.
BUILTIN_NAMES = frozenset(
    n for n in dir(builtins) if not n.startswith("_")) - {
    "copyright", "credits", "exit", "help", "license", "quit"}

#: rule -> (the fix, a snippet the check must flag).
PERF_RULES = {
    "shadow": ("rename the binding: it hides a builtin",
               "def f(type):\n    return type\n"),
    "eval": ("no eval/exec",
             "def f(s):\n    return eval(s)\n"),
    "scope-write": ("no writes through globals()/locals()",
                    "def f():\n    globals()['x'] = 1\n"),
    "pop0": ("use a collections.deque and popleft()",
             "def f(q):\n    return q.pop(0)\n"),
    "str-concat": ("collect the parts in a list and str.join them",
                   "def f(xs):\n    s = ''\n    for x in xs:\n"
                   "        s += x\n    return s\n"),
    "list-in": ("test membership against a set or dict built outside the loop",
                "def f(xs):\n    seen = []\n"
                "    return [x for x in xs if x in seen]\n"),
    "append-loop": ("use a comprehension (or += / extend with one)",
                    "def f(xs):\n    out = []\n    for x in xs:\n"
                    "        out.append(x * 2)\n    return out\n"),
    "module-loop": ("move the loop into a function",
                    "for i in range(3):\n    print(i)\n"),
}

#: Shapes that look close to a rule and are fine; the checks must pass them.
_PERF_LOOKALIKES = (
    # A method name is an attribute: it hides nothing outside the class.
    "class F:\n    def filter(self, record):\n        return True\n",
    # Grouping, not a list a comprehension could build.
    "def f(rows):\n    by = {}\n    for r in rows:\n"
    "        by.setdefault(r[0], []).append(r)\n    return by\n",
    # The condition reads the list being built.
    "def f(xs):\n    out = []\n    for x in xs:\n"
    "        if len(out) < 3:\n            out.append(x)\n    return out\n",
    # Rebuilt on every pass, never accumulated across passes.
    "def f(xs):\n    for x in xs:\n        s = 'a'\n        s += x\n"
    "        print(s)\n",
    "def f(xs):\n    return [x for x in xs if x in {'a', 'b'}]\n",
)

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _own_nodes(scope):
    """The nodes of `scope` outside any nested def, lambda or class."""
    out, stack = [], list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        out.append(n)
        if not isinstance(n, (*_DEFS, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return out


def _is_str(node):
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str))


def _builds_list(node):
    return isinstance(node, (ast.List, ast.ListComp)) or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in ("list", "sorted"))


def _stored(nodes):
    """Names bound in `nodes` by anything but `+=`."""
    aug = Counter(n.target.id for n in nodes if isinstance(n, ast.AugAssign)
                  and isinstance(n.target, ast.Name))
    return Counter(n.id for n in nodes if isinstance(n, ast.Name)
                   and isinstance(n.ctx, ast.Store)) - aug


def _typed_names(nodes):
    """(str names, list names): names whose every binding in `nodes` is a
    str literal / a list-building expression. `+=` keeps the type;
    parameters and every other binding kind break it."""
    stored = _stored(nodes)
    strs, lists = Counter(), Counter()
    for n in nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    strs[t.id] += _is_str(n.value)
                    lists[t.id] += _builds_list(n.value)
    params = {n.arg for n in nodes if isinstance(n, ast.arg)}
    return ({k for k, c in strs.items() if c and c == stored[k]} - params,
            {k for k, c in lists.items() if c and c == stored[k]} - params)


def _is_append_loop(node):
    """`for x in y: out.append(e)`, optionally under one `if c:`, where
    `out` is a name or attribute that neither e, c nor y reads."""
    if not (isinstance(node, ast.For) and not node.orelse
            and len(node.body) == 1):
        return False
    stmt, reads = node.body[0], [node.iter]
    if isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1:
        reads.append(stmt.test)
        stmt = stmt.body[0]
    call = stmt.value if isinstance(stmt, ast.Expr) else None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "append" and len(call.args) == 1
            and not call.keywords
            and not isinstance(call.args[0], ast.Starred)):
        return False
    recv = call.func.value
    if not isinstance(recv, (ast.Name, ast.Attribute)):
        return False
    key = ast.dump(recv)
    return not any(ast.dump(n) == key for r in [*reads, call.args[0]]
                   for n in ast.walk(r))


def _bound_builtins(node, kind):
    """Builtin names `node` binds. Nothing in a class body: those are
    attributes, and hide no builtin outside the class."""
    if isinstance(node, ast.arg):
        names = [node.arg]
    elif kind == "class":
        return []
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        names = [node.id]
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                           ast.ClassDef)):
        names = [node.name]
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names = [a.asname or a.name.split(".")[0] for a in node.names]
    elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        names = [node.name]
    else:
        return []
    return [n for n in names if n in BUILTIN_NAMES]


def _scope_write(node):
    """A write through globals() or locals()."""
    def is_scope_call(n):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ("globals", "locals"))
    if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
        return is_scope_call(node.value)
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("update", "setdefault", "pop", "popitem",
                                   "clear", "__setitem__", "__delitem__")
            and is_scope_call(node.func.value))


def _str_accumulates(node, strs, loop):
    """`s += ...` / `s = s + ...` on a str, inside `loop`, where the loop
    does not rebind `s` first (so the string grows across passes)."""
    if (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
            and isinstance(node.target, ast.Name)):
        name, added = node.target.id, node.value
    elif (isinstance(node, ast.Assign) and len(node.targets) == 1
          and isinstance(node.targets[0], ast.Name)
          and isinstance(node.value, ast.BinOp)
          and isinstance(node.value.op, ast.Add)
          and isinstance(node.value.left, ast.Name)
          and node.value.left.id == node.targets[0].id):
        name, added = node.targets[0].id, node.value.right
    else:
        return False
    if name not in strs and not _is_str(added):
        return False
    rebinds = [n for n in _own_nodes(loop) if isinstance(n, ast.Assign)
               and n is not node and any(isinstance(t, ast.Name)
                                         and t.id == name for t in n.targets)]
    return not rebinds


def _list_membership(node, lists):
    return isinstance(node, ast.Compare) and any(
        isinstance(op, (ast.In, ast.NotIn))
        and (isinstance(c, (ast.List, ast.ListComp))
             or (isinstance(c, ast.Name) and c.id in lists))
        for op, c in zip(node.ops, node.comparators))


def perf_violations(src):
    """{(function, rule)} for every mechanical-rule break in `src`.
    `function` is the dotted def path ('Cls.meth'), '<module>' outside one."""
    tree = ast.parse(src)
    hits = set()
    mod_strs, mod_lists = _typed_names(_own_nodes(tree))

    def visit(node, qual, kind, strs, lists, loops):
        for child in ast.iter_child_nodes(node):
            hits.update((qual, "shadow") for _ in _bound_builtins(child, kind))
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id in ("eval", "exec")):
                hits.add((qual, "eval"))
            if _scope_write(child):
                hits.add((qual, "scope-write"))
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "pop" and not child.keywords
                    and len(child.args) == 1
                    and isinstance(child.args[0], ast.Constant)
                    and child.args[0].value == 0):
                hits.add((qual, "pop0"))
            if loops and not isinstance(loops[-1], _COMPS) \
                    and _str_accumulates(child, strs, loops[-1]):
                hits.add((qual, "str-concat"))
            if loops and _list_membership(child, lists):
                hits.add((qual, "list-in"))
            if _is_append_loop(child):
                hits.add((qual, "append-loop"))
            if isinstance(child, _LOOPS) and kind != "def":
                hits.add((qual, "module-loop"))

            c_qual, c_kind, c_strs, c_lists, c_loops = (qual, kind, strs,
                                                        lists, loops)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                c_qual = (child.name if qual == "<module>"
                          else f"{qual}.{child.name}")
            if isinstance(child, _DEFS):
                own = _own_nodes(child)
                c_strs, c_lists = _typed_names(own)
                local = set(_stored(own)) | {n.arg for n in own
                                             if isinstance(n, ast.arg)}
                c_lists |= mod_lists - local
                c_kind, c_loops = "def", ()
            elif isinstance(child, ast.ClassDef):
                c_kind, c_loops = "class", ()
            elif isinstance(child, _LOOPS + _COMPS):
                c_loops = loops + (child,)
            visit(child, c_qual, c_kind, c_strs, c_lists, c_loops)

    visit(tree, "<module>", "module", mod_strs, mod_lists, ())
    return hits


@pytest.fixture(scope="module")
def perf_breaks():
    """Every (file, function, rule) the scanned tree breaks."""
    return {(rel, fn, rule) for rel, src in source_files()
            for fn, rule in perf_violations(src)}


def test_perf_checks_see_violations():
    """Each check flags its own example and nothing in the lookalikes."""
    for rule, (_, example) in PERF_RULES.items():
        assert {r for _, r in perf_violations(example)} == {rule}, rule
    for src in _PERF_LOOKALIKES:
        assert not perf_violations(src), src


@pytest.mark.parametrize("rule", sorted(PERF_RULES))
def test_perf_rule_holds(rule, perf_breaks):
    """docs/PERFORMANCE.md's mechanical rules hold in src/, tools/ and the
    root scripts."""
    bad = sorted((rel, fn) for rel, fn, r in perf_breaks if r == rule)
    assert not bad, (f"'{rule}' is broken at {bad}: {PERF_RULES[rule][0]}. "
                     "See docs/PERFORMANCE.md.")


def test_compiled_code_has_no_assert_or_debug():
    """build_app.py compiles with -O, which strips `assert` statements and
    `if __debug__:` blocks, so src/ and the root scripts may use neither."""
    found = sorted(
        (rel, n.lineno) for rel, tree in _parsed()
        if not rel.startswith("tools/")
        for n in ast.walk(tree)
        if isinstance(n, ast.Assert)
        or (isinstance(n, ast.Name) and n.id == "__debug__"))
    assert not found, (f"assert/__debug__ at {found}: -O would drop them. "
                       "Raise an exception instead.")


def test_every_module_keeps_its_annotations_as_strings():
    """Every first-party module has `from __future__ import annotations`:
    one annotation style for mypy, and pydantic models whose annotations
    stay strings. Without it Nuitka compiles each class's `__annotate__`,
    and on Python 3.14 pydantic's FORWARDREF read of that raises TypeError
    whenever an annotation holds a lambda reading a name, or `str.lower`:
    the exe dies at import while every test passes.

    Notes:
        Found 2026-09-24 building JobHarvester.exe (config.secrets.Settings,
        then profile_schema.Methodology); widened from the pydantic-model
        modules to every module when src/ was annotated.
    """
    trees = dict(_parsed())
    assert "src/ats/board/spec.py" in trees, "the scan found nothing"
    bare = sorted(rel for rel, tree in trees.items()
                  if not any(isinstance(n, ast.ImportFrom)
                             and n.module == "__future__"
                             and any(a.name == "annotations" for a in n.names)
                             for n in tree.body))
    assert not bare, f"{bare} lack `from __future__ import annotations`. Add it."


# --------------------------------------------------------------------------- #
#  7. The environment is read in one place                                    #
# --------------------------------------------------------------------------- #

def test_the_environment_is_read_only_through_config_settings():
    """src/config/secrets.py's Settings is the one reader of the
    environment (typed, trimmed, blank-is-unset); everything else reads
    config.SETTINGS."""
    found = sorted(
        (rel, n.lineno) for rel, tree in _parsed()
        if not rel.startswith("src/config/")
        for n in ast.walk(tree)
        if (isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv"))
        or (isinstance(n, ast.Name) and n.id in ("environ", "getenv")))
    assert not found, (f"environment read at {found}: add a field to "
                       "src/config/secrets.Settings and read config.SETTINGS.")


# --------------------------------------------------------------------------- #
#  8. Imports point down the package layers                                   #
# --------------------------------------------------------------------------- #

#: src/'s top-level units, lowest first. A module may import, at module level
#: or inside a function, only units before its own. dispatch sits above crawl
#: (two operations run crawl code) and below web, so the harvester, which
#: imports crawl and nothing above it, never loads the operation table.
LAYERS = ("validation", "tags", "config", "runstate", "match", "net", "session_log",
          "store", "claude", "ats", "digest", "discovery", "ops", "crawl", "dispatch", "web")


def _imported_units(rel, tree):
    """The src units `rel`'s import statements name."""
    package = rel.removesuffix(".py").split("/")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - node.level + 1] if node.level else []
            mod = ".".join(base + ([node.module] if node.module else []))
            names = ([f"{mod}.{a.name}" for a in node.names]
                     if mod == "src" else [mod])
        else:
            continue
        for n in names:
            parts = n.split(".")
            if parts[0] == "src" and len(parts) > 1:
                yield parts[1]


def test_src_imports_point_down_the_layers():
    rank = {u: i for i, u in enumerate(LAYERS)}
    on_disk = {p.stem if p.is_file() else p.name
               for p in (ROOT / "src").iterdir()
               if p.name not in ("__init__.py", "__pycache__")
               and (p.is_dir() or p.suffix == ".py")}
    assert on_disk == set(LAYERS), f"place {on_disk ^ set(LAYERS)} in LAYERS"
    up = sorted({(rel, u) for rel, tree in _parsed()
                 if rel.startswith("src/") and rel.count("/") >= 1
                 and rel != "src/__init__.py"
                 for u in _imported_units(rel, tree)
                 if rank[u] > rank[rel.split("/")[1].removesuffix(".py")]})
    assert not up, f"imports pointing up the layers: {up}"


# --------------------------------------------------------------------------- #
#  9. Async code                                                              #
# --------------------------------------------------------------------------- #
#
# An event loop runs every request at once, so a mistake that cost one
# thread its time now costs all of them: a blocking call stalls every
# request, a swallowed cancellation keeps work running after Ctrl+C, a task
# nobody holds can be collected mid-flight.

def _dotted(node):
    """'a.b.c' for a Name or Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _async_bodies(tree):
    """(async def, the nodes of its body outside any nested def)."""
    return [(fn, _own_nodes(fn)) for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef)]


#: What an `async def` may not touch: a thread's sleep, sqlite3, threads
#: and executors block the loop, and run_coroutine_threadsafe waits on the
#: very loop it would be running on.
BLOCKING = ("time.sleep", "sqlite3", "threading", "concurrent.futures",
            "ThreadPoolExecutor", "ProcessPoolExecutor",
            "run_coroutine_threadsafe")

#: requests' and urllib3's own I/O: nothing calls it, because every request
#: goes through net.http.send.
_REQUESTS_IO = re.compile(r"(requests|urllib3)(\.\w+)*\.(Session|session|get|post|put|"
                          r"patch|delete|head|options|request|adapters|PoolManager|"
                          r"urlopen)\b")


def _blocking(tree):
    """{(async def, name)} for each BLOCKING name an async body uses."""
    def banned(dotted):
        parts = dotted.split(".")
        return any(parts[i:i + len(b)] == b for b in (x.split(".") for x in BLOCKING)
                   for i in range(len(parts)))
    return {(fn.name, d) for fn, nodes in _async_bodies(tree) for n in nodes
            if isinstance(n, (ast.Name, ast.Attribute)) and (d := _dotted(n)) and banned(d)}


def _requests_uses(rel, tree):
    """[(line, what)] for each import of requests or urllib3 outside
    net/http, and each use of their I/O anywhere."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            names = [f"{n.module}.{a.name}" for a in n.names]
        else:
            if isinstance(n, ast.Attribute) and _REQUESTS_IO.match(_dotted(n) or ""):
                out.append((n.lineno, _dotted(n)))
            continue
        out += [(n.lineno, name) for name in names
                if _REQUESTS_IO.match(name) or (rel != "src/net/http.py"
                                                and name.split(".")[0] in ("requests", "urllib3"))]
    return out


def test_async_code_never_blocks():
    """Invariant 1: no BLOCKING name in an async body; requests and urllib3
    imported only by net/http, which uses them as models (preparing a
    request, reading a reply) and never for I/O."""
    snippet = ("import time\nasync def f():\n    time.sleep(1)\n"
               "    asyncio.run_coroutine_threadsafe(g(), loop)\n"
               "def g():\n    time.sleep(1)\n")
    assert _blocking(ast.parse(snippet)) == {
        ("f", "time.sleep"), ("f", "asyncio.run_coroutine_threadsafe")}
    assert _requests_uses("x.py", ast.parse("import requests\nrequests.get('u')\n")) == [
        (1, "requests"), (2, "requests.get")]
    blocked = sorted((rel, *hit) for rel, tree in _parsed() for hit in _blocking(tree))
    assert not blocked, (f"{blocked}: an async def blocks its event loop there. Await "
                         "the async version, or asyncio.to_thread a call with none.")
    uses = sorted((rel, *u) for rel, tree in _parsed() for u in _requests_uses(rel, tree))
    assert not uses, (f"{uses}: requests is net.http's model layer only. Send through "
                      "net.http (send, request, get_json) and catch http.HTTPError / "
                      "http.Unreachable.")


def test_the_one_client_session_is_made_in_net_http_with_a_timeout():
    """Invariant 2: one aiohttp ClientSession, built in net/http with a
    default timeout, so every network wait is bounded."""
    made = [(rel, n) for rel, tree in _parsed() for n in ast.walk(tree)
            if isinstance(n, ast.Call) and (_dotted(n.func) or "").endswith("ClientSession")]
    assert [rel for rel, _ in made] == ["src/net/http.py"], (
        f"ClientSession built at {[rel for rel, _ in made]}: use net.http's one session.")
    assert all(any(k.arg == "timeout" for k in n.keywords) for _, n in made), (
        "net.http's ClientSession lost its timeout=: a network wait is unbounded.")


#: Modules allowed to catch CancelledError, and why: the boundaries where
#: async code meets a caller that is not.
CANCEL_BOUNDARIES = {
    "src/dispatch/registry.py":
        "invoke: a started sync target cannot be cancelled, so its outcome, "
        "not the cancel, is the op's",
}


def _cancel_catches(rel, tree):
    """[(line, why)] for each handler that swallows cancellation: a bare
    `except:` or `except BaseException` with no `raise`, and `except
    CancelledError` outside CANCEL_BOUNDARIES."""
    out = []
    for h in ast.walk(tree):
        if not isinstance(h, ast.ExceptHandler):
            continue
        caught = ({_dotted(t) or "" for t in getattr(h.type, "elts", [h.type])}
                  if h.type else {"BaseException"})
        if "BaseException" in caught and not any(
                isinstance(s, ast.Raise) and s.exc is None for s in ast.walk(h)):
            out.append((h.lineno, "swallows BaseException"))
        if rel not in CANCEL_BOUNDARIES and any(c.endswith("CancelledError") for c in caught):
            out.append((h.lineno, "catches CancelledError"))
    return sorted(out)


def test_nothing_swallows_cancellation():
    """Invariant 3: cancellation (Ctrl+C, a pass budget) reaches the code
    that started the work, and a handler that kept it would let a cut-off
    board store a short snapshot."""
    snippet = ("def f():\n    try:\n        pass\n    except BaseException:\n"
               "        pass\n    try:\n        pass\n    except (OSError, CancelledError):\n"
               "        raise\n")
    assert _cancel_catches("x.py", ast.parse(snippet)) == [
        (4, "swallows BaseException"), (8, "catches CancelledError")]
    found = sorted((rel, *c) for rel, tree in _parsed() for c in _cancel_catches(rel, tree))
    assert not found, (f"{found}: re-raise, or catch Exception, which CancelledError "
                       "is not; a boundary goes in CANCEL_BOUNDARIES with its reason.")


def _dropped_tasks(tree):
    """Lines that throw a create_task / ensure_future result away, except
    onto a TaskGroup the code opened (`async with TaskGroup() as tg`)."""
    groups = {item.optional_vars.id for n in ast.walk(tree) if isinstance(n, ast.AsyncWith)
              for item in n.items if isinstance(item.optional_vars, ast.Name)
              and (_dotted(getattr(item.context_expr, "func", None)) or "").endswith("TaskGroup")}
    return sorted(n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                  and (d := _dotted(n.value.func) or "").split(".")[-1]
                  in ("create_task", "ensure_future")
                  and d.rpartition(".")[0] not in groups)


def test_every_task_is_kept():
    """Invariant 4: the event loop holds its tasks weakly, so a task nobody
    keeps can vanish mid-flight. Keep the result, or start it on a
    TaskGroup."""
    snippet = ("async def f(loop):\n    loop.create_task(g())\n"
               "    async with asyncio.TaskGroup() as tg:\n        tg.create_task(g())\n")
    assert _dropped_tasks(ast.parse(snippet)) == [2]
    dropped = sorted((rel, line) for rel, tree in _parsed() for line in _dropped_tasks(tree))
    assert not dropped, f"{dropped}: a task started and not kept. Keep it or use a TaskGroup."


async def test_a_blocked_loop_fails_the_test(loop_blocks):
    """Invariant 5: async tests run in asyncio's debug mode (pytest.ini),
    which logs a callback that holds the loop past 100 ms, and
    conftest.loop_blocks fails the test for it. This one blocks on purpose
    and takes the record back; the callback before it only starts tasks,
    whose stack captures (debug mode's own cost) are not a block."""
    await asyncio.gather(*[asyncio.create_task(asyncio.sleep(0)) for _ in range(100)])
    time.sleep(0.15)
    await asyncio.sleep(0)
    assert [r.own > 0.1 for r in loop_blocks][-1:] == [True]
    assert sum(r.own > 0.1 for r in loop_blocks) == 1
    loop_blocks.clear()


# --------------------------------------------------------------------------- #
#  Module-level names with one reader, and one-use private helpers            #
# --------------------------------------------------------------------------- #
#
# The standing rule: a module-level constant or compiled regex that one
# function in its module reads, and nothing else in src, tools or tests
# reads, belongs inside that function (`re` caches compiled patterns). A
# `_private` helper called once whose body is one simple statement is
# inlined. Module level stays for shared values, declared tables, measured
# hot compiles, loggers and process-wide state (a name a function rebinds
# with `global`). The allowlists name the exceptions.

MODULE_LEVEL_NAME_ALLOW = {
    ("src/store/jobs.py", "_RANK_SQL"): "declared query: ranked_jobs' SQL, layers named in its comment",
    ("src/store/jobs.py", "_COLLAPSE_SQL"): "declared query: ranked_jobs' collapse layer",
    ("src/discovery/name_sources.py", "_NAV_CHROME_RE"):
        "declared table: ~65 lines of site-chrome vocabulary",
    ("src/crawl/triage.py", "SCORE_CAP"):
        "run()'s default, cited by name in harvest.py's --help and crawl.harvest",
    ("src/match/filters.py", "SUBSTRING"):
        "a match mode token_in()'s doctest reads beside _excluded()",
    **{("src/match/locality.py", name): "measured hot compile: the geo gate, per posting"
       for name in ("_OTHER_STATE_NAME_RE", "_OTHER_STATE_ABBR_RE", "_OWN_STATE_RE",
                    "_WB_LOW_RE", "_NON_US_REGION_RE")},
}

SINGLE_USE_HELPER_ALLOW = {
    ("src/claude/fit.py", "_stack_core_text"):
        "called inside the fit prompt's f-string, where its fallback chain "
        "would be unreadable",
}


@functools.cache
def _parsed_tests():
    """tests/ as (rel, tree), parsed once: the rule counts a test's read."""
    return [(p.relative_to(ROOT).as_posix(), ast.parse(p.read_text(encoding="utf-8")))
            for p in sorted((ROOT / "tests").rglob("*.py"))]


def _module_of(rel):
    parts = rel[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _import_source(rel, node):
    """The module an ImportFrom in file `rel` names, a relative one
    resolved against `rel`'s package."""
    if not node.level:
        return node.module
    pkg = _module_of(rel).split(".")
    if not rel.endswith("__init__.py"):
        pkg = pkg[:-1]
    pkg = pkg[:len(pkg) - node.level + 1]
    return ".".join(pkg + ([node.module] if node.module else []))


def _outside_reads(rel, tree):
    """Names file `rel` can read from another module: what it imports, as
    (source module, name), plus every attribute name and every name a
    `setattr(obj, "name", ...)` patches, as (None, name)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            src = _import_source(rel, n)
            out |= {(src, a.name) for a in n.names}
        elif isinstance(n, ast.Attribute):
            out.add((None, n.attr))
        elif (isinstance(n, ast.Call) and len(n.args) > 1
              and getattr(n.func, "id", getattr(n.func, "attr", None)) == "setattr"
              and isinstance(n.args[1], ast.Constant)):
            out.add((None, n.args[1].value))
    return out


def _readers(tree):
    """{name: the outermost functions reading it}; None stands for code
    that runs at import (module or class body)."""
    out = {}

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                out.setdefault(child.id, set()).add(fn)
            inner = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            visit(child, child.name if inner and fn is None else fn)
    visit(tree, None)
    return out


def module_level_single_reader_names(trees=None):
    """(rel, name, reader) for every module-level constant (a literal, a
    non-empty flat tuple/list/set of literals) or `*.compile(...)` under
    src/ that exactly one function in its module reads and no other file
    reads, minus MODULE_LEVEL_NAME_ALLOW."""
    trees = trees if trees is not None else _parsed() + _parsed_tests()
    outside = {rel: _outside_reads(rel, t) for rel, t in trees}
    found = []
    for rel, tree in trees:
        if not rel.startswith("src/"):
            continue
        rebound = {name for n in ast.walk(tree) if isinstance(n, ast.Global)
                   for name in n.names}
        readers = _readers(tree)
        for node in tree.body:
            target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
            value = getattr(node, "value", None)
            if not isinstance(target, ast.Name) or value is None or target.id in rebound:
                continue
            literal = isinstance(value, ast.Constant) or (
                isinstance(value, (ast.Tuple, ast.List, ast.Set)) and value.elts
                and all(isinstance(e, ast.Constant) for e in value.elts))
            compiled = isinstance(value, ast.Call) and getattr(value.func, "attr", None) == "compile"
            name, fns = target.id, readers.get(target.id, set())
            if (not (literal or compiled) or len(fns) != 1 or None in fns
                    or (rel, name) in MODULE_LEVEL_NAME_ALLOW
                    or any(key in outside[other] for key in ((_module_of(rel), name), (None, name))
                           for other, _ in trees if other != rel)):
                continue
            found.append((rel, name, *fns))
    return found


def single_use_private_helpers(trees=None):
    """(rel, name, lineno) for every undecorated `_private` function under
    src/ whose body (past a docstring) is one simple statement, called
    once by name in src and referenced nowhere else (a callback, an
    attribute, a test's setattr), minus SINGLE_USE_HELPER_ALLOW."""
    trees = trees if trees is not None else _parsed() + _parsed_tests()
    calls, other_refs = Counter(), Counter()
    for rel, tree in trees:
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                (calls if id(n) in called and rel.startswith("src/") else other_refs)[n.id] += 1
        other_refs.update(name for _src, name in _outside_reads(rel, tree))
    simple = (ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Raise, ast.Pass)
    return [(rel, n.name, n.lineno) for rel, tree in trees if rel.startswith("src/")
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("_") and not n.name.startswith("__") and not n.decorator_list
            and len(body := n.body[1:] if ast.get_docstring(n) is not None else n.body) == 1
            and isinstance(body[0], simple)
            and calls[n.name] == 1 and not other_refs[n.name]
            and (rel, n.name) not in SINGLE_USE_HELPER_ALLOW]


def test_module_level_names_have_more_than_one_reader():
    found = module_level_single_reader_names()
    assert not found, (f"{found}: read by one function and nowhere else. Move each "
                       "into its reader, or allow it in MODULE_LEVEL_NAME_ALLOW.")


def test_single_use_private_helpers_are_inlined():
    found = single_use_private_helpers()
    assert not found, f"{found}: one-statement private helpers called once. Inline them."


def test_the_module_level_guards_can_actually_see_a_violation():
    """Both detectors flag a planted violation, and neither flags what the
    rule allows: a second reader in a test, a rebound name, a compound
    body."""
    def trees(**files):
        return [(rel.replace("_", "/", 1) + ".py", ast.parse(src)) for rel, src in files.items()]
    one_reader = "import re\n_PAT = re.compile('x')\ndef reader(s):\n    return _PAT.search(s)\n"
    assert module_level_single_reader_names(trees(src_m=one_reader)) == [
        ("src/m.py", "_PAT", "reader")]
    assert module_level_single_reader_names(trees(
        src_m=one_reader, tests_t="from src import m\nm._PAT\n")) == []
    assert module_level_single_reader_names(trees(
        src_m="_N = None\ndef f():\n    global _N\n    _N = _N or 1\n")) == []
    helper = "def _helper(x):\n    return x + 1\ndef caller(x):\n    return _helper(x)\n"
    assert single_use_private_helpers(trees(src_m=helper)) == [("src/m.py", "_helper", 1)]
    compound = ("def _helper(x):\n    try:\n        return 1 / x\n"
                "    except ZeroDivisionError:\n        return 0\n"
                "def caller(x):\n    return _helper(x)\n")
    assert single_use_private_helpers(trees(src_m=compound)) == []
