"""resolve_leads' per-lead consume path, offline.

The resolve_leads op (the `--resolve-leads` CLI flag and the "Resolve
captured leads" UI op) had no test that ever completed a lead. A local list
named `resolved` shadowed the `resolved(fut, name)` helper it called for each
completed future, so the first lead raised `TypeError: 'list' object is not
callable` -- and the suite stayed green, because nothing drove the loop.

The resolver and the mission scorer are faked; the store is the in-memory
one, kept open past the op's own `conn.close()`.
"""

import src.claude.api as claude
import src.store as store
from conftest import keep_store_open
from src.discovery import local_sourcing

_HIT = {"name": "ignored", "ats": "lever", "slug": "alpaca",
        "careers_url": "https://alpaca.example/careers",
        "count": 8, "nc": 3, "via": "sniff"}


def _lead(db, name, source="page_capture"):
    store.upsert_company(db, {"name": name, "source": source, "active": 0})


def _wire(monkeypatch, db, resolver):
    keep_store_open(monkeypatch, db)
    monkeypatch.setattr(local_sourcing, "resolve_or_miss", resolver)
    monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
    monkeypatch.setattr(claude, "score_company_mission",
                        lambda *a, **k: ("adjacent", 0.5, "stub"))


def _miss_reason(db, name):
    return next(c["miss_reason"] for c in store.get_companies(db, active_only=False)
                if c["name"] == name)


class TestResolveLeads:
    def test_a_resolved_lead_lands_in_the_review_queue(
            self, monkeypatch, db, capsys):
        _lead(db, "Alpaca Health")
        _wire(monkeypatch, db, lambda name, careers="": ({**_HIT}, None))
        rows = local_sourcing.resolve_leads(max_workers=1)
        assert [r["name"] for r in rows] == ["Alpaca Health"]
        assert [c["name"] for c in store.pending_companies(db)] == ["Alpaca Health"]
        assert store.crawlable_companies(db) == []
        assert "1 board(s) resolved" in capsys.readouterr().out

    def test_a_miss_is_recorded_with_its_reason(self, monkeypatch, db, capsys):
        _lead(db, "Ghost Labs")
        _wire(monkeypatch, db, lambda name, careers="": (None, "no-board-found"))
        assert local_sourcing.resolve_leads(max_workers=1) == []
        assert _miss_reason(db, "Ghost Labs") == "no-board-found"
        assert "[miss] Ghost Labs" in capsys.readouterr().out

    def test_a_resolution_that_raises_becomes_a_miss(
            self, monkeypatch, db, capsys):
        """The worker dying is the case `board.resolved` exists for; it is
        also the call that used to hit the shadowing list."""
        def _boom(name, careers=""):
            raise RuntimeError("worker died")

        _lead(db, "Wedge Bio")
        _wire(monkeypatch, db, _boom)
        assert local_sourcing.resolve_leads(max_workers=1) == []
        assert _miss_reason(db, "Wedge Bio") == "fetch-error:RuntimeError"
        assert "worker died" in capsys.readouterr().out

    def test_hits_and_misses_in_one_pass_are_tallied_apart(
            self, monkeypatch, db, capsys):
        for name in ("Alpaca Health", "Ghost Labs"):
            _lead(db, name)
        _wire(monkeypatch, db,
              lambda name, careers="": ({**_HIT}, None) if name == "Alpaca Health"
              else (None, "no-board-found"))
        rows = local_sourcing.resolve_leads(max_workers=1)
        assert [r["name"] for r in rows] == ["Alpaca Health"]
        out = capsys.readouterr().out
        assert "1 board(s) resolved" in out and "1 miss(es)" in out

    def test_a_lead_from_another_source_is_left_alone(self, monkeypatch, db):
        _lead(db, "Manual Add", source="manual")
        tried = []
        _wire(monkeypatch, db,
              lambda name, careers="": tried.append(name) or (None, "x"))
        assert local_sourcing.resolve_leads(max_workers=1) == []
        assert tried == []
