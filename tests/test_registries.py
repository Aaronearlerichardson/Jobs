"""The registry name sources, offline.

The public APIs are served from tests/fixtures, Apify's from the test; the
resolver and the store are the test's own (the in-memory store, a stand-in
for `queue_names`).
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


async def test_openfda_drops_duplicates_and_non_names(serve, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_REGISTRY_SPECIALTIES", [])
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


def _run(status, **more):
    return fake_response({"data": {"id": "r1", "status": status, **more}})


class TestGoogleJobs:
    """The paid registry: one Apify actor run, cached; the employers of its
    postings in your locality, under the names they post as."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch, tmp_path, local_addr, elsewhere):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "APIFY_API_KEY", "k")
        monkeypatch.setattr(config, "GOOGLE_JOBS", config.PROFILE.sources.google_jobs.model_copy(
            update={"queries": ["data scientist"], "locations": [local_addr]}))
        self.search = f"data scientist {local_addr}"
        self.items = [
            {"company_name": "Ten63 Therapeutics, Inc.", "location": local_addr,
             "job_title": "Data Scientist"},
            {"company_name": "Ten63 Therapeutics", "location": local_addr, "job_title": "ML Engineer"},
            {"company_name": "Far Away Co", "location": elsewhere, "job_title": "Data Scientist"},
            {"company_name": None, "location": local_addr, "job_title": "Analyst"},
            {"company_name": local_addr.split(",")[0], "location": local_addr,
             "job_title": "Clinical Informatics Director"}]

    async def test_local_employers_once_each_with_their_titles_and_one_paid_run(self, serve):
        sent = serve({"/runs": _run("SUCCEEDED", defaultDatasetId="d1", usageTotalUsd=0.02),
                      "datasets/d1/items": fake_response(self.items)})
        got = await reg.google_jobs("ST")
        assert [(s.name, s.blurb, s.source) for s in got] == [
            ("Ten63 Therapeutics", "Data Scientist; ML Engineer", "registry:google_jobs")]
        assert sent[0].kw["json"] == {"searchQueries": [self.search], "maxResults": 100}
        assert sent[0].params["maxTotalChargeUsd"] == 0.5
        assert [s.name for s in await reg.google_jobs("ST")] == ["Ten63 Therapeutics"]
        assert len(sent) == 2                       # the second read is the cached run's

    async def test_a_running_actor_is_polled_and_a_failed_run_names_no_one(self, serve, capsys):
        sent = serve({"/runs": _run("RUNNING"),
                      "actor-runs/r1": _run("FAILED", statusMessage="quota")})
        assert await reg.google_jobs("ST") == []
        assert [r.url.rsplit("/", 1)[-1] for r in sent] == ["runs", "r1"]
        assert "run FAILED" in capsys.readouterr().out

    async def test_no_key_runs_nothing(self, serve, monkeypatch, capsys):
        monkeypatch.setattr(config, "APIFY_API_KEY", "")
        sent = serve({})
        assert await reg.google_jobs("ST") == [] and sent == []
        assert "APIFY_API_KEY" in capsys.readouterr().out


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

        async def queue(writer, names, careers_urls=None, **_kw):
            self.queued.append(dict(names))
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
        source = "registry:nih_sbir"
        assert self.queued == [{"Delta": source, "Charlie": source}, {"Alpha": source}]

    async def test_no_state_skips_the_registries(self, monkeypatch, capsys):
        monkeypatch.setattr(config, "LOCALITY_STATE_SUFFIX", ["california"])
        assert await reg.discover_registries(apply=True) == {}
        assert "skipped" in capsys.readouterr().out
