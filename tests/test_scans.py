"""tools/scans.py finds what it says it finds, on trees planted for the purpose, and on this one."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

import tools.scans as scans

UNREFERENCED_ALLOW: dict[tuple[str, str], str] = {}


def plant(root: Path, **files: str) -> scans.Repo:
    """A tree under `root` with each file's text (`src__a.py` is src/a.py)."""
    for name, text in files.items():
        path = root / (name.replace("__", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return scans.Repo(root)


def kinds(lines) -> dict[str, str]:
    """{name: kind} from the `dead` scan's report lines."""
    return {line.split()[-1]: line.split()[0] for line in list(lines)[1:]}


def test_dead_sorts_each_name_by_who_still_mentions_it(tmp_path):
    repo = plant(
        tmp_path,
        src__a="""
            def used(): ...
            def orphan(): ...
            def only_tested(): ...
            CITED = 1   # CITED is the default a reader should know about
            YAML_ONLY = 2
            @register
            def registered(): ...
        """,
        tools__t="from src.a import used\nused()\n",
        tests__test_a="from src.a import only_tested\ndef test_x():\n    only_tested()\n",
    )
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("run: python -c 'print(YAML_ONLY)'\n", encoding="utf-8")
    assert kinds(scans.dead(repo)) == {"orphan": "unreferenced", "CITED": "prose-only",
                                       "only_tested": "test-only"}


def test_patches_count_each_kind_and_name_the_repo_function(tmp_path):
    repo = plant(tmp_path, tests__test_x="""
        import time
        from src import a
        def test(monkeypatch, cfg):
            monkeypatch.setattr(a, "fetch", None)
            monkeypatch.setattr(a, "LIMIT", 1)
            monkeypatch.setattr(time, "sleep", None)
            monkeypatch.setattr(cfg, "KEY", 1)
            other.setattr(a, "fetch", None)
    """)
    head, *rest = scans.patches(repo)
    assert head == "4 patches: repo function 1, repo constant 1, library 1, unresolved 1"
    assert "  1  src.a.fetch" in rest


def test_sql_lists_only_the_pieces_a_human_should_read(tmp_path):
    repo = plant(tmp_path, src__db="""
        def f(conn, table, ids):
            conn.execute(f"SELECT * FROM {table} WHERE id IN ({', '.join('?' for _ in ids)}) LIMIT {LIMIT}", ids)
    """)
    head, *rest = scans.sql(repo)
    assert {"variable 1", "placeholders 1", "constant 1"} <= set(head.split(", "))
    assert rest == ["src/db.py:3  table"]


def test_complexity_ranks_the_branchy_function_first(tmp_path):
    repo = plant(tmp_path, src__m="""
        def flat(a): return a
        def branchy(a, b):
            for x in a:
                if x and b:
                    return [y for y in x if y]
            return None
    """)
    head, first, second = scans.complexity(repo)
    assert head.startswith("2 functions")
    assert first.split()[-1] == "branchy" and first.split()[0] == "6"
    assert second.split()[0] == "1" and second.split()[-1] == "flat"


def test_coverage_reads_the_report_and_says_how_to_make_one(tmp_path):
    repo = plant(tmp_path)
    assert "pytest --cov" in next(iter(scans.coverage(repo)))
    summary = {"num_statements": 10, "covered_lines": 5, "missing_branches": 1, "percent_covered": 50.0}
    (tmp_path / "coverage.json").write_text(json.dumps({
        "totals": {"percent_covered": 50.0, "num_statements": 10, "num_branches": 2},
        "files": {"src/a.py": {"summary": summary}}}), encoding="utf-8")
    head, gap = scans.coverage(repo)
    assert head.startswith("50.0% of 10 statements") and "1 of 1 modules under 80%" in head
    assert gap.split()[0] == "6" and gap.endswith("src/a.py")


def test_a_wrapper_says_how_to_install_the_tool_it_cannot_find(tmp_path, monkeypatch):
    monkeypatch.setattr(scans.importlib.util, "find_spec", lambda name: None)
    assert list(scans.audit(plant(tmp_path))) == ["pip_audit is not installed: pip install pip-audit"]


def test_main_lists_every_scan_runs_the_defaults_and_refuses_an_unknown_name(tmp_path, capsys):
    assert scans.main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert all(name in listed for name in scans.SCANS)
    assert scans.main([], root=plant(tmp_path, src__a="x = 1\n").root) == 0
    ran = capsys.readouterr().out
    assert "== dead" in ran and "== bandit" not in ran
    with pytest.raises(SystemExit) as refused:
        scans.main(["nonesuch"])
    assert refused.value.code == 2


def test_nothing_in_src_is_unreferenced():
    """`dead` on this tree names nothing that nothing mentions: the module-level
    single-reader invariant only sees literals and compiled patterns, and a
    constant built by a call (`X = build(0)`) slipped past it for weeks."""
    found = [line for line in scans.dead(scans.Repo())
             if line.startswith("unreferenced") and (line.split()[1].split(":")[0], line.split()[-1])
             not in UNREFERENCED_ALLOW]
    assert not found, f"{found}: nothing reads them. Delete each, or allow it in UNREFERENCED_ALLOW."


def test_every_default_scan_runs_on_this_tree():
    for s in scans.SCANS.values():
        if s.default:
            assert list(s.run(scans.Repo())), s.name
