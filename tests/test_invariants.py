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
from pathlib import Path

import pytest

import config
from core.claude import ACTIVE_MISSION_TIERS, is_active_mission

ROOT = Path(__file__).resolve().parent.parent

#: Directories scanned by the source-level guards, plus root-level modules.
SOURCE_DIRS = ("core", "scrapers", "discovery", "webapp", "tools")

#: Root-level modules pytest cannot doctest-collect, with the reason. Kept
#: here so `test_pytest_ini_covers_every_source_module` stays honest instead
#: of quietly shrinking its own scope.
UNCOLLECTABLE_ROOT_MODULES = {
    # `webapp/` (the package) owns the name `webapp`, so pytest raises an
    # import-file-mismatch on the launcher. It is a launcher, not logic.
    "webapp.py",
}


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
            # discovery/ats_dork.py:138 (harvest_urls), post-fix
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS or t is None
                               or config.is_multi_division(n)) else 0,
            # discovery/local_sourcing.py:640 (populate_companies), with
            # include_missions defaulted to ACTIVE_MISSION_TIERS by the caller
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS or t is None
                               or config.is_multi_division(n)) else 0,
            # discovery/local_sourcing.py:1114 (resolve_leads)
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS
                               or config.is_multi_division(n)
                               or t is None) else 0,
            # discovery/local_sourcing.py:1387 (add_names)
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS or t is None
                               or config.is_multi_division(n)) else 0,
            # scrapers/ops.py:697 (add_manual_job)
            lambda t, n: 1 if (t in ACTIVE_MISSION_TIERS
                               or config.is_multi_division(n) or t is None) else 0,
        ]
        new = is_active_mission(tier, name)
        for i, old in enumerate(old_rules):
            assert new == old(tier, name), (i, tier, name)


# --------------------------------------------------------------------------- #
#  2. Nobody re-implements the rule inline
# --------------------------------------------------------------------------- #

#: (module, function) pairs allowed to spell the activation rule out.
#:
#: `core.claude.is_active_mission` is the rule.
#:
#: `local_sourcing.score_missions` is the REACTIVATION half and is
#: deliberately NOT the helper: it must not revive a row on `tier is None`.
#: A None tier with a non-None score means the model answered with a mission
#: name outside the profile's taxonomy — score_company_mission nulls the tier
#: but keeps the score, so the "scoring unavailable" `continue` above does
#: not fire. The helper would read that as "unavailable" and revive an
#: already-inactive company off an unrecognised answer.
RULE_SITES_ALLOWED = {
    ("core/claude.py", "is_active_mission"),
    ("discovery/local_sourcing.py", "score_missions"),
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
        f"{sorted(unexpected)}. Call core.claude.is_active_mission instead, "
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
