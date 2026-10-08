"""resolve_leads' per-lead consume path, offline.

The resolve_leads op (the `--resolve-leads` CLI flag and the "Resolve
captured leads" UI op) had no test that ever completed a lead. A local list
named `resolved` shadowed the `resolved` helper it called for each lead,
so the first lead raised `TypeError: 'list' object is not callable` -- and
the suite stayed green, because nothing drove the loop.

The resolver and the mission scorer are faked; the store is the in-memory
one, kept open past the op's own close.
"""

import src.store as store
from conftest import answer, stub_mission
from src.discovery import local_sourcing
from src.discovery.resolve import board as resolve_board

_HIT = {"name": "Alpaca Health", "ats": "lever", "slug": "alpaca",
        "careers_url": "https://alpaca.example/careers",
        "count": 8, "nc": 3, "via": "sniff"}


def _lead(db, name, source="page_capture"):
    store.upsert_company(db, {"name": name, "source": source, "active": 0})


def _wire(monkeypatch, db, resolver=None):
    stub_mission(monkeypatch, db)
    if resolver:
        monkeypatch.setattr(resolve_board, "resolve_or_miss", answer(resolver))


def _miss_reason(db, name):
    return next(c["miss_reason"] for c in store.get_companies(db, active_only=False)
                if c["name"] == name)


class TestResolveLeads:
    async def test_a_resolved_lead_lands_in_the_review_queue(
            self, monkeypatch, db, capsys):
        _lead(db, "Alpaca Health")
        _wire(monkeypatch, db, lambda name, careers="": ({**_HIT}, None))
        rows = await local_sourcing.resolve_leads(max_workers=1)
        assert [r["name"] for r in rows] == ["Alpaca Health"]
        assert [c["name"] for c in store.pending_companies(db)] == ["Alpaca Health"]
        assert store.crawlable_companies(db) == []
        assert "1 board(s) resolved" in capsys.readouterr().out

    async def test_a_miss_is_recorded_with_its_reason(self, monkeypatch, db, capsys):
        _lead(db, "Ghost Labs")
        _wire(monkeypatch, db, lambda name, careers="": (None, "no-board-found"))
        assert await local_sourcing.resolve_leads(max_workers=1) == []
        assert _miss_reason(db, "Ghost Labs") == "no-board-found"
        assert "[miss] Ghost Labs" in capsys.readouterr().out

    async def test_a_resolution_that_raises_becomes_a_miss(
            self, monkeypatch, db, capsys):
        """The worker dying is the case resolve_or_miss converts; it is
        also the call that used to hit the shadowing list."""
        def _boom(name, careers=""):
            raise RuntimeError("worker died")

        _lead(db, "Wedge Bio")
        _wire(monkeypatch, db)
        monkeypatch.setattr(resolve_board, "_resolve", answer(_boom))
        assert await local_sourcing.resolve_leads(max_workers=1) == []
        assert _miss_reason(db, "Wedge Bio") == "fetch-error:RuntimeError"
        assert "worker died" in capsys.readouterr().out

    async def test_hits_and_misses_in_one_pass_are_tallied_apart(
            self, monkeypatch, db, capsys):
        for name in ("Alpaca Health", "Ghost Labs"):
            _lead(db, name)
        _wire(monkeypatch, db,
              lambda name, careers="": ({**_HIT}, None) if name == "Alpaca Health"
              else (None, "no-board-found"))
        rows = await local_sourcing.resolve_leads(max_workers=1)
        assert [r["name"] for r in rows] == ["Alpaca Health"]
        out = capsys.readouterr().out
        assert "1 board(s) resolved" in out and "1 miss(es)" in out

    async def test_a_lead_from_another_source_is_left_alone(self, monkeypatch, db):
        _lead(db, "Manual Add", source="manual")
        tried = []
        _wire(monkeypatch, db,
              lambda name, careers="": tried.append(name) or (None, "x"))
        assert await local_sourcing.resolve_leads(max_workers=1) == []
        assert tried == []
