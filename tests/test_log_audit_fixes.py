"""Fixes from the 2026-09-09 session-log audit.

Two defects the logs showed, each pinned here offline (the other two, a
scope the board ignored and the location rescue's budget, are the board
engine's, in tests/test_snapshot_completeness.py):

  * names that are section headings, category nouns or location strings
    never reach the resolver (preview state, add_names miss, reresolve
    retirement);
  * the careers-page sniffer resolves each guessed host once, bounded,
    before fetching any path on it.
"""

import requests

from conftest import keep_store_open
import src.store as store
from src.discovery import local_sourcing, paste_ingest
from src.discovery.resolve import board as resolve_board, fetchpool
from src.match.names import junk_name_reason
from src.ops import maintenance as ops


# ─── junk names ──────────────────────────────────────────────────────────

class TestJunkNames:
    def test_the_audit_names_are_all_caught(self):
        seen = ["Required Qualifications", "Proficiency in SQL.", "Oncology",
                "Medical Devices", "99+ results", "Title",
                "Raleigh-Durham-Chapel Hill Area (On-site)",
                "Luna Physical Therapy 1", "Fairwai 1"]
        assert all(junk_name_reason(n) for n in seen), \
            [n for n in seen if not junk_name_reason(n)]

    def test_roster_names_pass(self, db):
        # The names the roster actually carries must not be screened out.
        for n in ("Beacon Biosignals", "SAS Institute", "Judi Health",
                  "Cala Health, Inc.", "Duke University", "UNC Chapel Hill",
                  "3M", "Q2 Solutions", "Blue Cross NC", "GRAIL", "NVIDIA",
                  "Bausch + Lomb", "J&J MedTech", "Red Hat", "Aah",
                  "Music and Cognition Lab", "Precision Neuroscience",
                  "Wellcome Centre for Human Neuroimaging"):
            assert junk_name_reason(n) == "", n


class TestJunkNamesInThePasteFlow:
    def _wire(self, monkeypatch, db):
        keep_store_open(monkeypatch, db)

    def test_preview_marks_junk_unticked_with_a_reason(self, monkeypatch, db):
        self._wire(monkeypatch, db)
        monkeypatch.setattr(paste_ingest, "parse_company_names",
                            lambda *a, **k: ["Alpaca Health",
                                             "Required Qualifications"])
        rows = paste_ingest.preview_names("x", use_llm=False)
        assert [(r["name"], r["state"]) for r in rows] == [
            ("Alpaca Health", "new"), ("Required Qualifications", "junk")]
        assert rows[1]["why"] == "section-heading"

    def test_blocked_beats_junk_in_the_preview(self, monkeypatch, db):
        store.block_name(db, "Oncology", "not a company")
        self._wire(monkeypatch, db)
        monkeypatch.setattr(paste_ingest, "parse_company_names",
                            lambda *a, **k: ["Oncology"])
        assert [r["state"] for r in
                paste_ingest.preview_names("x", use_llm=False)] == ["blocked"]

    def test_add_names_records_junk_as_a_miss_and_never_resolves_it(
            self, monkeypatch, db):
        self._wire(monkeypatch, db)
        tried = []
        monkeypatch.setattr(paste_ingest, "resolve_or_miss",
                            lambda n, *a, **k: tried.append(n) or (None, "x"))
        paste_ingest.add_names(["Proficiency in SQL.", "Alpaca Health"],
                                 max_workers=1)
        assert tried == ["Alpaca Health"]
        row = db.execute("SELECT miss_reason, active FROM companies "
                         "WHERE name='Proficiency in SQL.'").fetchone()
        assert row["miss_reason"] == "junk-name:section-heading"
        assert row["active"] == 0
        # ...and a junk-family miss is not a re-resolution candidate.
        assert [c["name"] for c in ops._reresolve_candidates(db)] == []


class TestJunkNamesInReresolve:
    T = {"db_path": None}

    def test_old_junk_misses_are_retired_not_retried(self, monkeypatch, db):
        store.record_miss(db, "Required Qualifications", "no-board-found:x")
        store.record_miss(db, "Emmes", "no-board-found:wrong-domain")
        tried = []
        monkeypatch.setattr(resolve_board, "resolve_or_miss",
                            lambda n, *a, **k: tried.append(n)
                            or (None, "no-board-found:x"))
        ops.reresolve_misses(conn=db, max_workers=1, t=self.T)
        assert tried == ["Emmes"]
        assert db.execute("SELECT miss_reason FROM companies WHERE "
                          "name='Required Qualifications'").fetchone()[0] \
            == "junk-name:section-heading"
        assert [c["name"] for c in ops._reresolve_candidates(db)] == ["Emmes"]


# ─── sniffer DNS pre-check ───────────────────────────────────────────────

class TestSnifferResolvesHostsOnce:
    def test_unresolvable_hosts_are_never_fetched(self, monkeypatch):
        looked_up, fetched = [], []

        def _gai(host, *a, **k):
            looked_up.append(host)
            if host.startswith("dead"):
                raise OSError("no such host")
            return [("addr",)]
        monkeypatch.setattr(fetchpool.socket, "getaddrinfo", _gai)
        monkeypatch.setattr(fetchpool, "_fetch_page",
                            lambda u, **k: fetched.append(u) or None)
        urls = ["https://dead.example/careers", "https://dead.example/",
                "https://dead.example/jobs", "https://live.example/careers"]
        out = fetchpool._fetch_all(urls)
        assert sorted(looked_up) == ["dead.example", "live.example"], \
            "each host resolved once, not once per path"
        assert fetched == ["https://live.example/careers"]
        assert set(out) == set(urls) and out["https://dead.example/"] is None
        # a later stage rebuilding the list asks the resolver nothing
        looked_up.clear()
        fetchpool._fetch_all(["https://dead.example/en/jobs",
                            "https://live.example/"])
        assert looked_up == []

    def test_a_silent_resolver_skips_the_host_this_pass_only(self, monkeypatch):
        import threading
        gate = threading.Event()

        def _gai(host, *a, **k):
            gate.wait(2)
            return [("addr",)]
        monkeypatch.setattr(fetchpool.socket, "getaddrinfo", _gai)
        kept = fetchpool._drop_unresolvable(["https://slow.example/"], timeout=0.05)
        assert kept == []
        assert not fetchpool._DEAD_HOSTS.dead("https://slow.example/"), \
            "a slow resolver is not a missing name"
        gate.set()

    def test_a_refused_connection_still_marks_the_host_dead(self, monkeypatch,
                                                             serve):
        serve(requests.exceptions.ConnectionError("refused"))
        monkeypatch.setattr(fetchpool.socket, "getaddrinfo",
                            lambda *a, **k: [("addr",)])
        assert fetchpool._fetch_page("https://x.example/") is None
        assert fetchpool._drop_unresolvable(["https://x.example/careers"]) == []
