#!/usr/bin/env python3
"""Mechanical scans of the code base: numbers to decide by, not impressions.

    python tools/scans.py                 # the scans that need nothing installed
    python tools/scans.py patches dead    # the named ones
    python tools/scans.py bandit audit deps
    python tools/scans.py --list

Each scan reads source with `ast` and never imports it, so a module that
fails to import cannot hide from one. A scan reports; the exit code is 0
whatever it finds. `bandit`, `audit` and `deps` wrap a tool that is not in
requirements-dev.txt, and run only when named.

Notes:
    The first version of `dead` was vulture over src/ alone. It named eight
    live functions: three are registered as SQL functions by a decorator,
    one is called from tools/, and one is a SQL function registered under
    another name. A name is now live when anything in src/, tools/, the
    root scripts or a workflow reads it, and a decorated definition is not
    judged at all. The scan cannot see a name built at run time
    (`getattr(mod, f"{kind}_x")`).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOTS = frozenset({"src", "tools", *(p.stem for p in ROOT.glob("*.py"))})
PATCHERS = frozenset({"monkeypatch", "mp"})
SQL_CALLS = frozenset({"execute", "executemany", "executescript"})
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Decorators that leave a definition where it is; any other registers it elsewhere.
TRANSPARENT = frozenset({"cache", "lru_cache", "cached_property", "contextmanager",
                         "asynccontextmanager", "overload", "staticmethod", "classmethod",
                         "property", "dataclass", "final"})
DECISIONS = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
             ast.Assert, ast.match_case)


@dataclass(frozen=True)
class Repo:
    """A checkout: the directory the scans read."""
    root: Path = ROOT

    def files(self, *dirs: str) -> list[tuple[str, str]]:
        """(path from the root, text) of every .py file under `dirs`; "." is the root's own scripts."""
        paths = [p for d in dirs for p in (self.root.glob("*.py") if d == "."
                                           else (self.root / d).rglob("*.py"))]
        return [(p.relative_to(self.root).as_posix(), p.read_text(encoding="utf-8", errors="replace"))
                for p in sorted(paths) if "__pycache__" not in p.parts]

    def trees(self, *dirs: str) -> list[tuple[str, ast.Module]]:
        """`files`, parsed."""
        return [(rel, ast.parse(text)) for rel, text in self.files(*dirs)]


@dataclass(frozen=True)
class Scan:
    name: str
    run: Callable[[Repo], Iterable[str]]
    default: bool

    @property
    def about(self) -> str:
        return (self.run.__doc__ or "").strip().splitlines()[0]


SCANS: dict[str, Scan] = {}


def scan(*, default: bool = True) -> Callable[[Callable[[Repo], Iterable[str]]],
                                              Callable[[Repo], Iterable[str]]]:
    """Register a scan under its function's name."""
    def register(fn: Callable[[Repo], Iterable[str]]) -> Callable[[Repo], Iterable[str]]:
        SCANS[fn.__name__] = Scan(fn.__name__, fn, default)
        return fn
    return register


def name_of(node: ast.expr) -> str:
    """The last name in a callee or a decorator.

    >>> [name_of(ast.parse(s, mode="eval").body) for s in ("f", "a.b.f", "f(1)", "a.f(1)")]
    ['f', 'f', 'f', 'f']
    """
    node = node.func if isinstance(node, ast.Call) else node
    return getattr(node, "attr", getattr(node, "id", ""))


def top(title: str, counts: Counter[str], n: int = 8) -> Iterator[str]:
    """`title`, then the `n` largest counts, as report lines.

    >>> list(top("by file", Counter({"a.py": 3, "b.py": 1}), 1))
    ['by file:', '  3  a.py']
    """
    yield f"{title}:"
    yield from (f"  {c}  {name}" for name, c in counts.most_common(n))


def imports(tree: ast.AST) -> dict[str, str]:
    """{local name: dotted origin} for a module's absolute imports.

    >>> imports(ast.parse("import os.path\\nimport src.net as n\\nfrom src.crawl import triage as t"))
    {'os': 'os', 'n': 'src.net', 't': 'src.crawl.triage'}
    """
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.asname or a.name.split(".")[0]: a.name if a.asname else a.name.split(".")[0]
                    for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out |= {a.asname or a.name: f"{n.module}.{a.name}" for a in n.names}
    return out


# --------------------------------------------------------------------------- #
#  Tests: what they replace
# --------------------------------------------------------------------------- #

def patch_target(call: ast.Call, origin: Mapping[str, str]) -> str | None:
    """The dotted name a `monkeypatch` call replaces, or None for any other call.

    A name that is not imported (a fixture, `self`) is marked with a leading `?`:

    >>> def target(src): return patch_target(ast.parse(src).body[0].value, {"triage": "src.crawl.triage"})
    >>> target("monkeypatch.setattr(triage, 'gate', f)")
    'src.crawl.triage.gate'
    >>> target("monkeypatch.setattr('time.sleep', f)")
    'time.sleep'
    >>> target("monkeypatch.setattr(cfg, 'KEY', 1)")
    '?cfg.KEY'
    >>> target("other.setattr(triage, 'gate', f)") is None
    True
    """
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr in ("setattr", "setitem", "delattr")
            and isinstance(f.value, ast.Name) and f.value.id in PATCHERS and call.args):
        return None
    first, rest = call.args[0], call.args[1:]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    root, _, tail = ast.unparse(first).partition(".")
    named = str(rest[0].value) if rest and isinstance(rest[0], ast.Constant) else "?"
    return ".".join(filter(None, [origin.get(root) or "?" + root, tail, named]))


def patch_kind(target: str) -> str:
    """What kind of thing a patch replaces.

    >>> [patch_kind(t) for t in ("src.crawl.triage.gate", "src.config.API_KEY", "time.sleep", "?cfg.KEY")]
    ['repo function', 'repo constant', 'library', 'unresolved']
    """
    if target.startswith("?"):
        return "unresolved"
    if target.split(".")[0] not in REPO_ROOTS:
        return "library"
    return "repo constant" if target.rsplit(".", 1)[-1].isupper() else "repo function"


@scan()
def patches(repo: Repo) -> Iterator[str]:
    """What the tests replace: repo functions, repo constants, libraries."""
    kinds, files, names = Counter[str](), Counter[str](), Counter[str]()
    for rel, tree in repo.trees("tests"):
        origin = imports(tree)
        for target in (patch_target(n, origin) for n in ast.walk(tree) if isinstance(n, ast.Call)):
            if target is not None:
                kinds[patch_kind(target)] += 1
                if patch_kind(target) == "repo function":
                    files[rel] += 1
                    names[target] += 1
    yield f"{sum(kinds.values())} patches: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common())
    yield from top("repo functions replaced, by test file", files)
    yield from top("repo functions replaced, by name", names)


# --------------------------------------------------------------------------- #
#  Types left open
# --------------------------------------------------------------------------- #

@scan()
def looseness(repo: Repo) -> Iterator[str]:
    """Typing left open in src/: Any, dict[str, Any], cast(), type: ignore, pyrefly: ignore."""
    total, files = Counter[str](), Counter[str]()
    for rel, text in repo.files("src"):
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Name) and n.id == "Any":
                total["Any"] += 1
                files[rel] += 1
            if isinstance(n, ast.Subscript) and ast.unparse(n) == "dict[str, Any]":
                total["dict[str, Any]"] += 1
            if isinstance(n, ast.Call) and name_of(n) == "cast":
                total["cast()"] += 1
        total["type: ignore"] += len(re.findall(r"#\s*type:\s*ignore", text))
        total["pyrefly: ignore"] += len(re.findall(r"#\s*pyrefly:\s*ignore", text))
    yield ", ".join(f"{k} {n}" for k, n in total.items())
    yield from top("most Any, by file", files, 6)


# --------------------------------------------------------------------------- #
#  SQL built from text
# --------------------------------------------------------------------------- #

def sql_piece(expr: str) -> str:
    """How one interpolated piece of SQL text is made.

    >>> sql_piece("', '.join('?' for _ in ids)")
    'placeholders'
    >>> sql_piece("ph"), sql_piece("_COLS")
    ('placeholders', 'constant')
    >>> sql_piece("table")
    'variable'
    """
    if re.search(r"""['"]\?['"]""", expr) or re.fullmatch(r"\w*(?:marks|placeholders?)\w*|qs|ph", expr):
        return "placeholders"
    return "constant" if re.fullmatch(r"_?[A-Z][A-Z0-9_]*(?:\.\w+)*", expr) else "variable"


@scan()
def sql(repo: Repo) -> Iterator[str]:
    """Pieces interpolated into SQL text, by how each is made; a variable wants a human read."""
    kinds, variables = Counter[str](), list[str]()
    for rel, tree in repo.trees("src"):
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and name_of(n) in SQL_CALLS and n.args):
                continue
            arg = n.args[0]
            pieces = ([ast.unparse(v.value) for v in arg.values if isinstance(v, ast.FormattedValue)]
                      if isinstance(arg, ast.JoinedStr)
                      else [ast.unparse(arg)] if isinstance(arg, ast.BinOp) or name_of(arg) == "format"
                      else [])
            for piece in pieces:
                kinds[sql_piece(piece)] += 1
                if sql_piece(piece) == "variable":
                    variables.append(f"{rel}:{n.lineno}  {piece[:80]}")
    yield ", ".join(f"{k} {n}" for k, n in kinds.most_common())
    yield from variables


# --------------------------------------------------------------------------- #
#  Names nothing reads
# --------------------------------------------------------------------------- #

def definitions(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """(name, line) of each module-level function, class and constant that no decorator registers elsewhere.

    >>> src = "@register\\ndef a(): pass\\n@functools.cache\\ndef b(): pass\\nC = 1\\n__all__ = ['b']\\nclass D: pass"
    >>> list(definitions(ast.parse(src)))
    [('b', 4), ('C', 5), ('D', 7)]
    """
    for n in tree.body:
        if isinstance(n, (*FUNCTIONS, ast.ClassDef)):
            if all(name_of(d) in TRANSPARENT for d in n.decorator_list):
                yield n.name, n.lineno
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in n.targets if isinstance(n, ast.Assign) else [n.target]:
                if isinstance(t, ast.Name) and not t.id.startswith("__"):
                    yield t.id, n.lineno


def reads(tree: ast.Module, imported: bool = False) -> Counter[str]:
    """The names a module reads: loads, attributes and exact-name strings.

    An import is not a read unless asked, and `__all__` is never one:

    >>> sorted(reads(ast.parse("from m import a\\nx = b.c + d\\nf('e')\\n__all__ = ['g']")))
    ['b', 'c', 'd', 'e', 'f']
    >>> sorted(reads(ast.parse("from m import a"), imported=True))
    ['a']
    """
    skip = {id(c) for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "__all__" for t in n.targets) for c in ast.walk(n.value)}
    out = Counter[str]()
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out[n.id] += 1
        elif isinstance(n, ast.Attribute):
            out[n.attr] += 1
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out[n.value] += 1
        elif imported and isinstance(n, ast.ImportFrom):
            out.update(a.name for a in n.names)
    return out


@scan()
def dead(repo: Repo) -> Iterator[str]:
    """Module-level names in src/ that no code reads: nothing mentions them, prose does, or only tests do."""
    live, tested, prose = Counter[str](), Counter[str](), Counter[str]()
    for _, tree in repo.trees("src", "tools", "."):
        live.update(reads(tree))
    for workflow in (repo.root / ".github" / "workflows").glob("*.yml"):
        live.update(re.findall(r"\w+", workflow.read_text(encoding="utf-8", errors="replace")))
    for _, tree in repo.trees("tests"):
        tested.update(reads(tree, imported=True))
    for _, text in repo.files("src", "tools", "."):
        prose.update(re.findall(r"\w+", text))
    found = [("test-only" if tested[name] else "prose-only" if prose[name] > 1 else "unreferenced",
              f"{rel}:{line}  {name}")
             for rel, tree in repo.trees("src") for name, line in definitions(tree) if not live[name]]
    yield ", ".join(f"{sum(k == kind for k, _ in found)} {kind}" for kind in ("unreferenced", "prose-only", "test-only"))
    yield from (f"{kind:13}{where}" for kind, where in sorted(found))


# --------------------------------------------------------------------------- #
#  Size and coverage
# --------------------------------------------------------------------------- #

def weight(node: ast.AST) -> int:
    """What one node adds to a function's complexity.

    >>> [weight(ast.parse(s).body[0]) for s in ("if a: pass", "x = 1")]
    [1, 0]
    >>> weight(ast.parse("a or b or c", mode="eval").body)
    2
    """
    if isinstance(node, ast.BoolOp):
        return len(node.values) - 1
    if isinstance(node, ast.comprehension):
        return 1 + len(node.ifs)
    return int(isinstance(node, DECISIONS))


def mccabe(fn: ast.AST) -> int:
    """A function's McCabe number: one, plus the weight of every node inside it.

    >>> mccabe(ast.parse("def f(a, b):\\n    if a and b:\\n        return 1\\n    return [x for x in a if x]").body[0])
    5
    """
    return 1 + sum(weight(n) for n in ast.walk(fn))


@scan()
def complexity(repo: Repo) -> Iterator[str]:
    """Functions in src/ by McCabe number: the refactor queue."""
    rows = sorted(((mccabe(n), f"{rel}:{n.lineno}  {n.name}") for rel, tree in repo.trees("src")
                   for n in ast.walk(tree) if isinstance(n, FUNCTIONS)), reverse=True)
    yield f"{len(rows)} functions; " + ", ".join(f"{k}+: {sum(c >= k for c, _ in rows)}" for k in (10, 15, 20))
    yield from (f"{c:4}  {where}" for c, where in rows[:12])


@scan()
def coverage(repo: Repo) -> Iterator[str]:
    """Coverage from coverage.json, and where the largest gaps are."""
    path = repo.root / "coverage.json"
    if not path.exists():
        yield "no coverage.json: run `pytest --cov --cov-report=json` first"
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    total = data["totals"]
    gaps = sorted(((s["num_statements"] - s["covered_lines"] + s.get("missing_branches", 0),
                    s["percent_covered"], name)
                   for name, f in data["files"].items() for s in [f["summary"]]), reverse=True)
    yield (f"{total['percent_covered']:.1f}% of {total['num_statements']} statements and "
           f"{total.get('num_branches', 0)} branches; {sum(p < 80 for _, p, _ in gaps)} of {len(gaps)} modules under 80%")
    yield from (f"{missing:4} missing  {pct:5.1f}%  {name}" for missing, pct, name in gaps[:10])


# --------------------------------------------------------------------------- #
#  Tools that are not in requirements-dev.txt
# --------------------------------------------------------------------------- #

def run_tool(repo: Repo, module: str, *argv: str) -> Iterator[str]:
    """The first lines `python -m module argv` prints, or how to install `module`."""
    if importlib.util.find_spec(module) is None:
        yield f"{module} is not installed: pip install {module.replace('_', '-')}"
        return
    done = subprocess.run([sys.executable, "-m", module, *argv], cwd=repo.root,
                          capture_output=True, text=True, check=False)
    lines = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout + done.stderr).strip().splitlines()
    yield from lines[:40]
    if len(lines) > 40:
        yield f"... and {len(lines) - 40} more lines"


@scan(default=False)
def bandit(repo: Repo) -> Iterator[str]:
    """bandit over src/, medium severity and up."""
    yield from run_tool(repo, "bandit", "-q", "-r", "src", "-ll")


@scan(default=False)
def audit(repo: Repo) -> Iterator[str]:
    """pip-audit of envs/requirements.txt; asks the network."""
    yield from run_tool(repo, "pip_audit", "-r", "envs/requirements.txt", "--progress-spinner", "off")


@scan(default=False)
def deps(repo: Repo) -> Iterator[str]:
    """deptry: declared but unused, imported but undeclared (src/conftest.py is pytest's, not the app's)."""
    yield from run_tool(repo, "deptry", "src", "--known-first-party", "src",
                        "--extend-exclude", r"src/conftest\.py", "--requirements-files", "envs/requirements.txt")


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    """Run the named scans, or every default one; 0 whatever they find."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="scans to run (default: those needing nothing installed)")
    ap.add_argument("--list", action="store_true", help="name every scan and exit")
    args = ap.parse_args(argv)
    unknown = [n for n in args.names if n not in SCANS]
    if unknown:
        ap.error(f"no such scan: {', '.join(unknown)} (see --list)")
    if args.list:
        for s in SCANS.values():
            print(f"{s.name:11}{'' if s.default else '[named only] '}{s.about}")
        return 0
    for s in [SCANS[n] for n in args.names] or [s for s in SCANS.values() if s.default]:
        print(f"== {s.name}: {s.about}")
        for line in s.run(Repo(root)):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
