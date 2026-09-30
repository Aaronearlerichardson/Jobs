"""tools/mutants.py on a tree of one function: a change no test notices comes back as a diff.

The test-file choice and the config are doctested in the tool. This runs
the real thing, so it needs cosmic-ray (not in requirements-dev.txt) and is
skipped without it.
"""

from __future__ import annotations

import re

import pytest

import tools.mutants as mutants

pytest.importorskip("cosmic_ray")


def plant(root, tested: str):
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "m.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef positive(a):\n    return a > 0  # pragma: no mutate\n",
        encoding="utf-8")
    (root / "tests" / "test_m.py").write_text(
        f"from src.m import add\n\n\ndef test_add():\n    assert {tested}\n", encoding="utf-8")


@pytest.mark.parametrize("tested, survives", [
    ("add(0, 0) == 0", True),       # cannot tell + from -, * or |
    ("add(2, 3) == 5", False)])     # pins the sum
def test_a_change_no_test_notices_comes_back_as_a_diff(tmp_path, tested, survives):
    plant(tmp_path, tested)
    lines = list(mutants.run(tmp_path, "src/m.py"))
    survivors = int(re.search(r"surviving mutants: (\d+)", "\n".join(lines))[1])
    assert lines[0] == "src/m.py: 1 test file(s): tests/test_m.py"
    assert (survivors > 0) is survives and ("+    return a - b" in lines) is survives
    assert not any("a > 0" in line for line in lines if line[:1] in "+-")   # `# pragma: no mutate`


def test_main_names_a_missing_module_and_returns_zero(tmp_path, capsys):
    assert mutants.main(["src/none.py"], root=tmp_path) == 0
    assert "no such module: src/none.py" in capsys.readouterr().out
