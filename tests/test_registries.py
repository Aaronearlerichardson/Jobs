"""The registry name sources, offline.

Both APIs are served from tests/fixtures; the resolver and the store are the
test's own (the in-memory store, a stand-in for `queue_names`).
"""

import pytest

from conftest import answer, fake_response, fixture, keep_store_open
from src import config, store
from src.discovery import registries as reg


async def test_nih_names_are_titled_stripped_and_deduped(serve):
    sent = serve({"reporter.nih.gov": fake_response(fixture("nih_reporter.json"))})
    got = await reg.nih_sbir("NC")
    assert [(s.name, s.city, s.source, s.blurb) for s in got] == [
        ("Acme", "Durham", "registry:nih_sbir", "Gene therapy for ALS; Spinal cord interface"),
        ("Neuro Widgets", "Chapel Hill", "registry:nih_sbir", "Cortical array")]
    body = sent[0].kw["json"]
    assert body["criteria"]["org_states"] == ["NC"] and "R43" in body["criteria"]["activity_codes"]
    assert "ProjectTitle" in body["include_fields"]


async def test_nih_pages_follow_the_total(serve, monkeypatch):
    monkeypatch.setitem(config.REGISTRIES["nih_sbir"], "page", 2)
    rows = fixture("nih_reporter.json")["results"]

    def page(_url, **kw):
        offset = kw["json"]["offset"]
        return fake_response({"meta": {"total": 4}, "results": rows[offset:offset + 2]})

    sent = serve({"reporter.nih.gov": page})
    assert len(await reg.nih_sbir("NC")) == 2
    assert [r.kw["json"]["offset"] for r in sent] == [0, 2]


async def test_openfda_drops_duplicates_and_non_names(serve):
    sent = serve({"api.fda.gov": fake_response(fixture("openfda_devices.json"))})
    assert [s.name for s in await reg.openfda_devices("NC")] == [
        "Teleflex Medical", "Neuro Widgets", "Zeta Devices"]
    assert sent[0].kw["params"]["search"] == "registration.state_code:NC"


async def test_openfda_specialties_narrow_the_query(serve, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_REGISTRY_SPECIALTIES", ["Radiology"])
    sent = serve({"api.fda.gov": fake_response(fixture("openfda_devices.json"))})
    await reg.openfda_devices("NC")
    assert sent[0].kw["params"]["search"].endswith('AND (products.openfda.medical_specialty_'
                                                   'description.exact:"Radiology")')


class TestDiscoverRegistries:
    """The op: roster names dropped, a batch at a time, a cursor between runs."""

    @pytest.fixture(autouse=True)
    def _wire(self, db, tmp_path, monkeypatch):
        keep_store_open(monkeypatch, db)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "LOCALITY_STATE_SUFFIX", ["nc", "north carolina"])
        monkeypatch.setattr(config, "DISCOVERY_REGISTRIES", ["nih_sbir"])
        names = ["Alpha", "Bravo", "Charlie", "Delta", "Neuro Widgets"]
        blurbs = {"Alpha": "store", "Delta": "clinical"}
        monkeypatch.setattr(reg, "title_vocab", lambda _conn: {"store": -1.0, "clinical": 1.0})
        monkeypatch.setitem(reg.READERS, "nih_sbir", answer(
            [reg.NamedSource(n, None, None, "registry:nih_sbir", blurbs.get(n))
             for n in names]))
        store.upsert_company(db, {"name": "Neuro Widgets Inc", "ats": "lever", "slug": "nw"})
        store.record_miss(db, "Bravo", "no-board-found")
        self.queued = []

        async def queue(writer, names, source, careers_urls=None, **_kw):
            self.queued.append((list(names), source))
            for n in names:
                await writer.run(store.record_miss, n, "no-board-found")
            return [{}] * len(names), []

        monkeypatch.setattr(reg, "queue_names", queue)

    async def test_a_dry_run_counts_and_queues_nothing(self):
        counts = await reg.discover_registries()
        assert (counts["nih_sbir"], counts["nih_sbir_new"], counts["batch"]) == (5, 3, 3)
        assert self.queued == []

    async def test_roster_and_missed_names_are_dropped_and_batches_take_the_best_unprocessed(self):
        assert (await reg.discover_registries(apply=True, limit=2))["queued"] == 2
        assert (await reg.discover_registries(apply=True, limit=2))["queued"] == 1
        assert self.queued == [(["Delta", "Charlie"], "registry:nih_sbir"),
                               (["Alpha"], "registry:nih_sbir")]

    async def test_no_state_skips_the_registries(self, monkeypatch, capsys):
        monkeypatch.setattr(config, "LOCALITY_STATE_SUFFIX", ["california"])
        assert await reg.discover_registries(apply=True) == {}
        assert "skipped" in capsys.readouterr().out
