"""Invariants of `config.BOARDS`: the schema refuses what it cannot read,
no spec restates a default, every platform detected is a spec, every
fetchable one has a canary, and nothing outside the spec names a platform.

Offline: the specs are read, the source is parsed with `ast`, and detection
runs over the recorded fixture pages.
"""

import ast
from pathlib import Path

import pytest

from src import config, tags
from src.ats import signatures
from src.ats.board import BOARDS, spec
from src.ats.registry import seed_tag_for

ROOT = Path(__file__).resolve().parent.parent

#: Where a platform may still be named outside config/boards.py, and why.
#: Every entry is discovery's Workday strategy: a Workday handle is a
#: (tenant, pod, site) triple no name guess reaches, so discovery scans
#: careers pages and a headless browser for it and guards a parent
#: company's shared tenant; a spec key for that strategy is follow-up work.
#: dork.py also skips PeopleAdmin URL hits (a tenant's search pages name
#: no one board).
NAMED_PLATFORMS = {
    "src/discovery/apply.py": {"workday"},
    "src/discovery/dork.py": {"workday", "peopleadmin"},
    "src/discovery/local_sourcing.py": {"workday"},
    "src/discovery/pipeline.py": {"workday"},
    "src/discovery/resolve/board.py": {"workday"},
    "src/discovery/resolve/probes.py": {"workday"},
    "src/discovery/resolve/sniffer.py": {"workday"},
    "src/discovery/resolve/websearch_board.py": {"workday"},
}


_WHY = "a quirk, 2026-09"
_L = {"url": "https://x.test/{slug}", "fields": {"id": "id"}}
_D = {"url": "https://x.test/{jid}"}


def _listing(**kw):
    return {"listing": {**_L, **kw}}


def _pager(**kw):
    return _listing(pager=kw)


def _always(**kw):
    """A rescue on every pull, one key changed (a None one dropped)."""
    rescue = {"when": "always", "unknown": "x", "cap": 1, "why": _WHY, **kw}
    return {**_listing(), "detail": _D, "rescue": {k: v for k, v in rescue.items() if v is not None}}


#: Specs the schema must refuse, one broken key each.
REFUSED = [
    {"sweeps": True}, {"sweep": "yes"}, {"canary": {"name": "A", "handle": 7}},
    {"canary": {"name": "A", "handle": "a", "min_jobs": 0}}, {"unlocated": "maybe"},
    _listing(nope=1), _listing(fields={"salary": "pay"}), _listing(url="{slug|nope}"),
    _listing(fields={"title": {"of": "t", "transform": "nope"}}),
    _listing(fields={"title": {"of": "t", "join": ["a"]}}),
    _listing(decoder={"kind": "xml"}), _listing(decoder={"select": "a"}),
    _listing(decoder={"kind": "json_in_html"}),
    _listing(decoder={"kind": "json_in_html", "regex": "("}),
    _listing(decoder={"kind": "html"}), _listing(decoder={"kind": "html", "select": ""}),
    _pager(kind="scroll", size=1), _pager(kind="offset"), _pager(kind="offset", size=0),
    _pager(kind="offset", size=2, step=1), _pager(kind="overlap", size=2, step=2, why=_WHY),
    _pager(kind="cursor", size=2), _pager(kind="offset", size=2, ceiling="2000", why=_WHY),
    _pager(kind="offset", size=2, ceiling=2000), _pager(kind="offset", size=2, why=_WHY),
    _pager(kind="offset", size=2, ceiling=2000, why="because"),
    _listing(scope={"kind": "facets", "facets": "f"}), _listing(scope={"kind": "param"}),
    {"handle": {"columns": []}}, {"handle": {"nope": 1}}, {"handle": {"follow": {"base": 1}}},
    {"handle": {"try": {"a": ["x"], "b": ["y"]}, "why": _WHY}},
    {"handle": {"accept": {"nope": 1}, "why": _WHY}},
    {"listing": [_L, {"url": "https://x.test/2"}]},
    {"listing": [{**_L, "why": _WHY}, {"url": "https://x.test/2", "why": _WHY}]},
    {**_listing(), "rescue": {"unknown": "x", "cap": 1, "why": _WHY}},
    {**_listing(), "detail": _D, "rescue": {"unknown": "x", "cap": 1, "why": _WHY}},
    _always(unknown="("), _always(cap="1"), _always(fields=["pay"]), _always(why=None),
    {"closure": {"nope": 1}}, {"closure": {"via": "email"}}, {"closure": {"via": "detail"}},
    {**_pager(kind="offset", size=2), "closure": {"via": "listing"}},
    {"closure": {"unmatched": 7, "why": _WHY}}, {"closure": {"unmatched": "gone"}},
    {"closure": {"closed": [{"why": {"const": "x"}}]}},
    {"closure": {"closed": [{"when": {"truthy": "a"}, "if": 1}]}},
    {"closure": {"open": {"maybe": "a"}}},
    {"job_ref": {"re": "(", "parts": []}}, {"job_ref": {"re": "a/(\\d+)", "parts": []}},
    {"employer": {"of": "a", "transform": "nope"}}, {"employer": 7},
    {"detect": [{}]}, {"detect": [{"re": ["abc"]}]},
    {"detect": [{"re": ["(a)"], "transform": ["x"]}]},
    {"detect": [{"re": ["(a)"], "transform": [None, None]}]},
    {"detect": [{"re": ["(a)"], "blocklist": [1]}]},
    {"detect": [{"re": ["(a)"], "careers_url": "{slug|nope}"}]},
]


def test_the_schema_refuses_what_it_cannot_read():
    """Each spec in REFUSED breaks one rule; the error names the spec."""
    for i, raw in enumerate(REFUSED):
        with pytest.raises(ValueError, match=f"^case{i}: "):
            spec.parse(f"case{i}", raw)


def test_no_key_restates_its_default():
    """A key a spec sets to its model's default (or `closure.via` to the
    one it would resolve to) does nothing: drop it."""
    dead = [f"{name}.{path}" for name, b in BOARDS.items()
            for path, value, given, default in spec.walk(b.spec) if given and value == default]
    dead += [f"{name}.closure.via" for name, b in BOARDS.items()
             if b.spec.closure.via == b.spec.default_via]
    assert not dead


def test_detect_returns_only_board_keys():
    """Over every recorded fixture page, and each canary's own handle:
    a detection names a spec, fetchable exactly when it has a listing."""
    texts = [p.read_text(encoding="utf-8", errors="replace")
             for p in (ROOT / "tests" / "fixtures").rglob("*") if p.is_file()]
    texts += [str(s["canary"]["handle"]) for s in config.BOARDS.values() if "canary" in s]
    hits = [h for t in texts for h in (signatures.detect(t), signatures.detect("", t)) if h]
    assert hits
    for kind, ats, _slug in hits:
        assert ats in config.BOARDS
        assert kind == ("fetchable" if config.BOARDS[ats].get("listing") else "lead")


#: Fetchable specs with no canary yet, and why.
NO_CANARY = {"hibob": "the store's only board reports a total of 0"}


def test_every_fetchable_spec_has_a_canary():
    missing = {n for n, s in config.BOARDS.items() if s.get("listing") and "canary" not in s}
    assert missing == set(NO_CANARY)


def test_the_seed_tag_follows_sweep():
    for name, raw in config.BOARDS.items():
        want = (tags.SWEEP if raw.get("sweep") else tags.LOCAL) if raw.get("listing") else None
        assert seed_tag_for(name) == want, name


def _literals(path):
    """The string constants in a module, docstrings left out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)}
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs}


def test_no_platform_names_in_src():
    """No `config.BOARDS` key is a string literal in src/ats, src/store,
    src/discovery or src/crawl, outside config/boards.py and the
    NAMED_PLATFORMS allowlist; nor does the allowlist outlive its need."""
    named = {}
    for top in ("ats", "store", "discovery", "crawl"):
        for path in (ROOT / "src" / top).rglob("*.py"):
            hits = _literals(path) & set(config.BOARDS)
            if hits:
                named[path.relative_to(ROOT).as_posix()] = hits
    assert named == NAMED_PLATFORMS
