#!/usr/bin/env python3
"""Mutation testing of one module: change it a line at a time, see which changes no test notices.

    python tools/mutants.py src/net/robots.py
    python tools/mutants.py src/store/pipeline.py --tests tests/test_store.py
    python tools/mutants.py src/net/robots.py --show-killed

Needs `pip install cosmic-ray`. It runs in a copy of the tree, never this
one, because cosmic-ray rewrites the module in place; the copy is deleted
afterwards. The tests it runs are the ones that name the module (or those
given), and the module's own doctests. A survivor is printed as a diff: a
line whose change no test saw. It is either a test that is missing or a
change that cannot matter (a log line, a timeout), which a `# pragma: no
mutate` comment then records. The exit code is 0 whatever survives.

Notes:
    About three seconds a mutant, one at a time: a 200-line module is ten
    minutes. cosmic-ray was chosen after mutmut, which assumes a `src/`
    directory that is not itself a package and stops at the first
    `from src import config`. The tool runs the tests once on the module as
    it is before it starts, because cosmic-ray's own baseline exits 0 when
    the test command cannot even start, and its report then says "surviving
    mutants: 0": on Windows the interpreter path lost its backslashes to
    `shlex.split` and every mutant looked killed (2026-09-30).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "data", ".venv", "venv*", "build",
                                "dist", "*.build", "*.dist", "htmlcov", ".mypy_cache", ".pytest_cache")


def covering_tests(module: str, tests: Mapping[str, str]) -> list[str]:
    """The test files whose text names `module`'s file stem as a word.

    >>> tests = {"tests/a.py": "from src.net import robots", "tests/b.py": "import os",
    ...          "tests/c.py": "robotstxt = 1"}
    >>> covering_tests("src/net/robots.py", tests)
    ['tests/a.py']
    """
    word = re.compile(rf"\b{re.escape(Path(module).stem)}\b")
    return sorted(name for name, text in tests.items() if word.search(text))


def judging_command(module: str, tests: Iterable[str], python: str) -> str:
    """The command that runs `tests` and the module's doctests, written for `shlex.split`.

    cosmic-ray splits it the POSIX way, which eats the backslashes of a
    Windows interpreter path and the spaces of "Program Files":

    >>> command = judging_command("src/m.py", ["tests/test_m.py"], r"C:\\Program Files\\Py\\python.exe")
    >>> command
    "'C:/Program Files/Py/python.exe' -m pytest -x -q -p no:cacheprovider tests/test_m.py src/m.py"
    >>> shlex.split(command)[:3]
    ['C:/Program Files/Py/python.exe', '-m', 'pytest']
    """
    files = " ".join(shlex.quote(name) for name in [*tests, module])
    return f"{shlex.quote(PureWindowsPath(python).as_posix())} -m pytest -x -q -p no:cacheprovider {files}"


def config(module: str, command: str, timeout: float = 60.0) -> str:
    """The cosmic-ray config that mutates `module` and judges each change by `command`.

    >>> print(config("src/m.py", "python -m pytest 't 1.py'"))
    [cosmic-ray]
    module-path = "src/m.py"
    timeout = 60.0
    excluded-modules = []
    test-command = "python -m pytest 't 1.py'"
    <BLANKLINE>
    [cosmic-ray.distributor]
    name = "local"
    """
    return "\n".join([
        "[cosmic-ray]", f"module-path = {json.dumps(module)}", f"timeout = {timeout}", "excluded-modules = []",
        f"test-command = {json.dumps(command)}",
        "", "[cosmic-ray.distributor]", 'name = "local"'])


def annotation_spans(source: str) -> list[tuple[int, int, int, int]]:
    """(line, column, end line, end column) of each annotation in `source`.

    The project's modules keep their annotations as strings, so a change
    inside one (`int | None` to `int + None`) is a mutant nothing can kill.

    >>> annotation_spans("def f(a: int | None = 1) -> str:\\n    x: list[int] = []\\n")
    [(1, 9, 1, 19), (1, 28, 1, 31), (2, 7, 2, 16)]
    """
    tree = ast.parse(source)
    nodes = [n.annotation for n in ast.walk(tree) if isinstance(n, (ast.arg, ast.AnnAssign)) and n.annotation]
    nodes += [n.returns for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns]
    return sorted((n.lineno, n.col_offset, n.end_lineno or n.lineno, n.end_col_offset or 0) for n in nodes)


def drop_annotation_mutants(session: Path, source: str) -> int:
    """Delete from `session` the mutants that change an annotation; how many.

    >>> import tempfile
    >>> db = Path(tempfile.mkdtemp()) / "s.sqlite"
    >>> with closing(sqlite3.connect(db)) as c:
    ...     _ = c.execute("CREATE TABLE mutation_specs (job_id, start_pos_row, start_pos_col)")
    ...     _ = c.execute("CREATE TABLE work_items (job_id)")
    ...     _ = c.execute("CREATE TABLE work_results (job_id)")
    ...     _ = c.executemany("INSERT INTO mutation_specs VALUES (?, ?, ?)", [("a", 1, 12), ("b", 2, 12)])
    ...     _ = c.executemany("INSERT INTO work_items VALUES (?)", [("a",), ("b",)])
    ...     c.commit()
    >>> drop_annotation_mutants(db, "def f(a: int | None):\\n    return a | 1\\n")
    1
    >>> with closing(sqlite3.connect(db)) as c:
    ...     c.execute("SELECT job_id FROM work_items").fetchall()
    [('b',)]
    """
    spans = annotation_spans(source)
    with closing(sqlite3.connect(session)) as conn:
        doomed = [(job,) for job, row, col in conn.execute(
            "SELECT job_id, start_pos_row, start_pos_col FROM mutation_specs").fetchall()
            if any((line, start) <= (row, col) < (end_line, end) for line, start, end_line, end in spans)]
        conn.executemany("DELETE FROM work_results WHERE job_id=?", doomed)
        conn.executemany("DELETE FROM mutation_specs WHERE job_id=?", doomed)
        conn.executemany("DELETE FROM work_items WHERE job_id=?", doomed)
        conn.commit()
    return len(doomed)


def tool(work: Path, entry: str, *argv: str) -> subprocess.CompletedProcess[str]:
    """A cosmic-ray console script, `package.module:function`, run in `work` with `argv`."""
    module, _, func = entry.partition(":")
    return subprocess.run([sys.executable, "-c", f"import sys; from {module} import {func}; sys.exit({func}())", *argv],
                          cwd=work, capture_output=True, text=True, check=False)


def run(root: Path, module: str, tests: list[str] | None = None, show_killed: bool = False) -> Iterator[str]:
    """Mutate `module` in a copy of `root` and report what the tests missed."""
    if importlib.util.find_spec("cosmic_ray") is None:
        yield "cosmic-ray is not installed: pip install cosmic-ray"
        return
    if not (root / module).is_file():
        yield f"no such module: {module}"
        return
    if tests is None:
        tests = covering_tests(module, {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                                   for p in sorted((root / "tests").glob("test_*.py"))})
    yield f"{module}: {len(tests)} test file(s): {' '.join(tests) or 'none'}"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "tree"
        shutil.copytree(root, work, ignore=IGNORE)
        command = judging_command(module, tests, sys.executable)
        (work / "mutants.toml").write_text(config(module, command), encoding="utf-8")
        try:
            base = subprocess.run(shlex.split(command), cwd=work, capture_output=True, text=True, check=False)
        except OSError as e:
            yield f"the test command cannot start ({e}): {command}"
            return
        if base.returncode:
            yield "the tests fail on the module as it is, so no change to it could be told apart:"
            yield from (base.stdout + base.stderr).strip().splitlines()[-12:]
            return
        steps = (("cosmic_ray.cli:main", "init", "mutants.toml", "session.sqlite"),
                 ("cosmic_ray.tools.filters.pragma_no_mutate:main", "session.sqlite"),
                 ("drop", "annotations"),
                 ("cosmic_ray.cli:main", "exec", "mutants.toml", "session.sqlite"))
        for entry, *argv in steps:
            if entry == "drop":
                yield f"{drop_annotation_mutants(work / 'session.sqlite', (work / module).read_text(encoding='utf-8'))} " \
                      "mutant(s) inside annotations dropped"
                continue
            done = tool(work, entry, *argv)
            if done.returncode:
                yield f"{entry} {' '.join(argv)} failed:"
                yield from (done.stdout + done.stderr).strip().splitlines()[-12:]
                return
        done = tool(work, "cosmic_ray.tools.report:report", *([] if show_killed else ["--surviving-only"]),
                    "--show-diff", "session.sqlite")
        yield from (done.stdout + done.stderr).strip().splitlines()


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    """Print the survivors of one module's mutants; 0 whatever they are."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("module", help="the module to mutate, from the repo root (src/net/robots.py)")
    ap.add_argument("--tests", nargs="+", help="test files to run (default: those naming the module)")
    ap.add_argument("--show-killed", action="store_true", help="list every mutant, not only survivors")
    args = ap.parse_args(argv)
    for line in run(root, args.module, args.tests, args.show_killed):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
