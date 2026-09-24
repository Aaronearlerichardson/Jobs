"""Invariants of `config.BOARDS`: every spec reads, every platform detected
is one of them, every fetchable one has a canary, and nothing outside the
spec names a platform.

Offline: the specs are read, the source is parsed with `ast`, and detection
runs over the recorded fixture pages.
"""

import ast
import re
from pathlib import Path

from src import config, tags
from src.ats import signatures
from src.ats.board.spec import validate_spec
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
    "src/discovery/resolve/probes.py": {"workday"},
    "src/discovery/resolve/sniffer.py": {"workday"},
    "src/discovery/resolve/websearch_board.py": {"workday"},
}


def _regexes(spec):
    """Every regex string a spec holds, wherever it sits."""
    listings = spec.get("listing") or []
    listings = listings if isinstance(listings, list) else [listings]
    found = [(spec.get("job_ref") or {}).get("re"), (spec.get("rescue") or {}).get("unknown")]
    found += [(alt.get("scope") or {}).get("param_re") for alt in listings]
    found += [(part.get("decoder") or {}).get("regex") for part in listings + [spec.get("detail") or {}]]
    found += [rx for d in spec.get("detect", []) for rx in d.get("re", [])]
    return [rx for rx in found if rx]


def test_every_spec_validates_and_every_regex_compiles():
    for name, spec in config.BOARDS.items():
        validate_spec(name, spec)
        for rx in _regexes(spec):
            re.compile(rx)


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
    for name, spec in config.BOARDS.items():
        want = (tags.SWEEP if spec.get("sweep") else tags.LOCAL) if spec.get("listing") else None
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
