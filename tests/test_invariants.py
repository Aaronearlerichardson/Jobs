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
import builtins
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
    """`config.is_offmission_inactive` sits beside `is_active_mission` in
    config/policy.py and reads the same ACTIVE_MISSION_TIERS, one word
    apart. They answer different questions, and the difference is
    deliberate -- pinned here because nothing else says which is which:

      * is_active_mission decides `active`. An UNSCORED company (tier
        None) is active: scoring was unavailable, so the row is not
        punished for it.
      * is_offmission_inactive only ever narrows a CADENCE
        (harvest.plan's HARVEST_OFFMISSION_HOURS) or a BUDGET
        (config.board_max_pages). An unscored row reads as off-mission
        there: nobody has bothered to score it, so it does not earn the
        frequent, wide read.
    """

    def test_an_unscored_row_is_active_but_still_off_mission(self):
        assert is_active_mission(None, "Nowhere Robotics") == 1
        assert config.is_offmission_inactive({"mission_tier": None,
                                              "active": 0}) is True

    @pytest.mark.parametrize("tier", ALL_TIERS)
    def test_a_row_the_roster_calls_active_is_never_off_mission(self, tier):
        """Whatever the tier: the activation decision is already recorded
        in `active` (multi-division exemptions included), and this
        predicate never re-litigates it."""
        assert not config.is_offmission_inactive({"mission_tier": tier,
                                                  "active": 1})

    def test_an_active_tier_is_never_off_mission(self):
        for tier in ACTIVE_MISSION_TIERS:
            assert not config.is_offmission_inactive({"mission_tier": tier,
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
#: `local_sourcing.score_missions` (its per-row consumer, `_scored`) is
#: the REACTIVATION half and is
#: deliberately NOT the helper: it must not revive a row on `tier is None`.
#: A None tier with a non-None score means the model answered with a mission
#: name outside the profile's taxonomy — score_company_mission nulls the tier
#: but keeps the score, so the "scoring unavailable" `return` above does
#: not fire. The helper would read that as "unavailable" and revive an
#: already-inactive company off an unrecognised answer.
RULE_SITES_ALLOWED = {
    ("src/config/policy.py", "is_active_mission"),
    ("src/discovery/local_sourcing.py", "_scored"),
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

    for rel, src in source_files():
        walk(ast.parse(src, filename=rel), None)
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
#  4. Parallelism goes through src/net/parallel.py                             #
# --------------------------------------------------------------------------- #

#: Modules allowed to build a thread pool of their own, and why. Everything
#: else calls net.parallel (fan_out for work that can FAIL, drain for work
#: that can HANG, fetch_all for the crawl's source fan-out).
POOL_OWNERS = {
    "src/net/parallel.py":
        "owns the shared primitives",
    "src/crawl/harvest.py":
        "its own FIRST_COMPLETED watchdog, with a per-pass wall-clock budget",
    "src/discovery/resolve/fetchpool.py":
        "per-run candidate-URL memo; the pool is part of the cache",
    "src/discovery/resolve/probes.py":
        "pins one headless browser to one dedicated thread (Playwright "
        "thread affinity)",
    "tools/check_sources.py":
        "the pool sits inside a per-thread stdout capture that has to wrap "
        "the whole threaded section (see _ThreadCapture)",
}


def test_thread_pools_go_through_net_parallel():
    """A hand-rolled pool is how the submit/as_completed/try/print block
    came back eight times, and how one wedged resolution held the web UI's
    single op slot for over an hour. The exceptions are real and named;
    a new one has to be argued for here."""
    offenders = {}
    for rel, src in source_files():
        if rel in POOL_OWNERS or "ThreadPoolExecutor(" not in src:
            continue
        offenders[rel] = [l.strip() for l in src.splitlines()
                          if "ThreadPoolExecutor(" in l and
                          not l.strip().startswith("#")]
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, (
        f"{sorted(offenders)} build their own thread pool. Use "
        "src.net.parallel (fan_out / drain / fetch_all), or add the module "
        "to POOL_OWNERS with the reason.")


def test_pool_owners_still_own_pools():
    """The allowlist must not rot into a list of modules that moved on."""
    stale = [rel for rel in POOL_OWNERS
             if not any(r == rel and "ThreadPoolExecutor(" in s
                        for r, s in source_files())]
    assert not stale, f"POOL_OWNERS lists {stale}, which no longer build one."


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
        (rel, n.lineno) for rel, src in source_files()
        if not rel.startswith("tools/")
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Assert)
        or (isinstance(n, ast.Name) and n.id == "__debug__"))
    assert not found, (f"assert/__debug__ at {found}: -O would drop them. "
                       "Raise an exception instead.")


def _model_classes(trees):
    """(rel, class node) for every pydantic model class: a subclass, by
    base name, of BaseModel, BaseSettings or another model class."""
    classes = [(rel, c) for rel, t in trees.items() for c in ast.walk(t)
               if isinstance(c, ast.ClassDef)]
    bases = {id(c): {getattr(b, "id", getattr(b, "attr", None)) for b in c.bases}
             for _, c in classes}
    names, grew = {"BaseModel", "BaseSettings"}, True
    while grew:
        found = {c.name for _, c in classes if bases[id(c)] & names}
        grew, names = not found <= names, names | found
    return [(rel, c) for rel, c in classes if bases[id(c)] & names]


def test_pydantic_models_keep_their_annotations_as_strings():
    """Every module defining a pydantic model has `from __future__ import
    annotations`. Without it Nuitka compiles each class's `__annotate__`,
    and on Python 3.14 pydantic's FORWARDREF read of that raises TypeError
    whenever an annotation holds a lambda reading a name, or `str.lower`:
    the exe dies at import while every test passes.

    Notes:
        Found 2026-09-24 building JobHarvester.exe (config.secrets.Settings,
        then profile_schema.Methodology).
    """
    trees = {rel: ast.parse(src) for rel, src in source_files()}
    modules = {rel for rel, _ in _model_classes(trees)}
    assert "src/ats/board/spec.py" in modules, "the model scan found nothing"
    bare = sorted(rel for rel in modules
                  if not any(isinstance(n, ast.ImportFrom)
                             and n.module == "__future__"
                             and any(a.name == "annotations" for a in n.names)
                             for n in trees[rel].body))
    assert not bare, (f"pydantic models in {bare} without `from __future__ "
                      "import annotations`: the compiled exe cannot import "
                      "them. Add the import.")


# --------------------------------------------------------------------------- #
#  7. The environment is read in one place                                    #
# --------------------------------------------------------------------------- #

def test_the_environment_is_read_only_through_config_settings():
    """src/config/secrets.py's Settings is the one reader of the
    environment (typed, trimmed, blank-is-unset); everything else reads
    config.SETTINGS."""
    found = sorted(
        (rel, n.lineno) for rel, src in source_files()
        if not rel.startswith("src/config/")
        for n in ast.walk(ast.parse(src))
        if (isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv"))
        or (isinstance(n, ast.Name) and n.id in ("environ", "getenv")))
    assert not found, (f"environment read at {found}: add a field to "
                       "src/config/secrets.Settings and read config.SETTINGS.")
