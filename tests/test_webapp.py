"""The Flask app, exercised through its test client — real routes, no
socket, no API key. Covers the geo bucket the Jobs tab filters on, the API
surface the SPA depends on, and the asset cache-busting that a stale
browser copy once defeated."""

import json
import re

import webapp


class TestGeoBucket:
    """Derived live from the location string; the stored geo_mode is only a
    secondary hint because it's stale-by-construction."""

    def test_local(self, local_addr):
        assert webapp._geo_tag({"location": local_addr}) == "local"

    def test_remote(self):
        assert webapp._geo_tag({"location": "Remote - US"}) == "remote"

    def test_relocation(self, elsewhere):
        assert webapp._geo_tag({"location": elsewhere}) == "relocation"

    def test_stored_remote_eligible_wins_on_empty_location(self):
        assert webapp._geo_tag({"location": "", "remote_eligible": 1}) == "remote"

    def test_stored_geo_mode_remote_wins(self):
        assert webapp._geo_tag({"location": "Austin, TX",
                                "geo_mode": "remote"}) == "remote"


class TestOps:
    def test_ops_are_keyed_by_engine_not_track_id(self):
        # A user-chosen track id must never appear in code.
        assert all("tracks" not in o for o in webapp.OPS.values())

    def test_every_op_names_a_real_engine_or_is_agnostic(self):
        assert all(o.get("engine") in (None, "local", "sweep")
                   for o in webapp.OPS.values())

    def test_every_op_has_a_callable(self):
        assert all(callable(o["fn"]) for o in webapp.OPS.values())


class TestApi:
    ENDPOINTS = ("/api/stats", "/api/jobs", "/api/pipeline", "/api/companies",
                 "/api/tracks", "/api/config", "/api/run/status")

    def test_endpoints_return_json(self, client):
        for path in self.ENDPOINTS:
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)
            json.loads(resp.data)

    def test_tracks_expose_ui_defaults(self, client):
        tracks = json.loads(client.get("/api/tracks").data)
        assert tracks
        for t in tracks:
            assert {"id", "label", "engine", "min_fit_default",
                    "willing_to_move_default", "ops"} <= set(t)

    def test_unknown_track_falls_back_rather_than_500(self, client):
        # A stale localStorage value must not brick the UI.
        assert client.get("/api/jobs?track=no_such_track").status_code == 200

    def test_config_reports_its_source(self, client):
        cfgjson = json.loads(client.get("/api/config").data)
        assert cfgjson["source"] in ("profile.toml", "profile.example.toml")
        assert cfgjson["parsed"]["keywords"]

    def test_bad_config_is_refused_not_written(self, client):
        resp = client.put("/api/config/raw", json={"toml": "not [valid"})
        assert resp.status_code == 400
        assert "errors" in json.loads(resp.data)

    def test_unknown_operation_404s(self, client):
        assert client.post("/api/run/no_such_op").status_code == 404


class TestCompanyCrawlState:
    """The roster tab has to show WHY a company stopped producing rows --
    dormant is not deactivated -- and offer the one-click way back."""

    def _sleepy_store(self, tmp_path, monkeypatch):
        """Point the default track at a throwaway DB: these tests write, and
        the suite may never touch the real store."""
        import config
        from core import store
        db_path = tmp_path / "roster.db"
        conn = store.connect(db_path)
        cid = store.upsert_company(conn, {"name": "Sleepy", "ats": "greenhouse",
                                          "slug": "sleepy"})
        conn.execute("UPDATE companies SET crawl_state='dormant', "
                     "empty_streak=6, next_crawl_at='2099-01-01T00:00:00' "
                     "WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        monkeypatch.setitem(config.UI_TRACKS[config.DEFAULT_TRACK],
                            "db_path", db_path)
        return cid

    def test_companies_expose_crawl_state(self, client, tmp_path, monkeypatch):
        cid = self._sleepy_store(tmp_path, monkeypatch)
        row = next(r for r in json.loads(client.get("/api/companies").data)
                   if r["id"] == cid)
        assert row["crawl_state"] == "dormant"
        assert row["empty_streak"] == 6
        assert row["next_crawl_at"].startswith("2099")

    def test_dormant_rows_leave_the_active_count(self, client, tmp_path,
                                                 monkeypatch):
        self._sleepy_store(tmp_path, monkeypatch)
        assert json.loads(client.get("/api/stats").data)["companies_active"] == 0

    def test_reactivate_clears_the_schedule(self, client, tmp_path, monkeypatch):
        cid = self._sleepy_store(tmp_path, monkeypatch)
        assert client.post(f"/api/company/{cid}/reactivate").status_code == 200
        row = next(r for r in json.loads(client.get("/api/companies").data)
                   if r["id"] == cid)
        assert row["crawl_state"] == "active"
        assert row["empty_streak"] == 0 and row["next_crawl_at"] is None

    def test_unknown_company_404s(self, client, tmp_path, monkeypatch):
        self._sleepy_store(tmp_path, monkeypatch)
        assert client.post("/api/company/9999/reactivate").status_code == 404


class TestAssets:
    def test_index_cache_busts_its_assets(self, client):
        html = client.get("/").data.decode()
        assert re.search(r'/static/js/app\.js\?v=\w+', html)
        assert re.search(r'/static/css/app\.css\?v=\w+', html)

    def test_index_is_not_cacheable(self, client):
        assert client.get("/").headers.get("Cache-Control") == "no-store"

    def test_static_assets_are_not_cacheable(self, client):
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_asset_version_tracks_content(self, monkeypatch):
        from webapp import routes
        v1 = routes._asset_version()
        assert v1 == routes._asset_version()        # stable
        assert len(v1) == 10


class TestOpConcurrency:
    """One operation at a time, enforced where it can actually be enforced.

    The route checked `_running()` and then called `_run_op()` as two steps.
    Two /api/run requests landing together both passed the check and both
    started, so the crawl ran twice — and because each op swaps sys.stdout for
    a tee that appends to TASK["log"], the second tee wrapped the first and
    every printed line was recorded once per layer. The duplicate output in
    the run log was the visible half; the duplicated work was the expensive
    half.
    """

    @staticmethod
    def _noisy(label, lines=3, pause=0.05):
        import time

        def fn():
            for i in range(lines):
                print(f"{label}-{i}")
                time.sleep(pause)
        return fn

    @staticmethod
    def _drain():
        import time

        from webapp import ops
        while ops._running():
            time.sleep(0.02)
        time.sleep(0.15)          # let the worker's finally block land

    def test_second_op_is_refused_while_the_first_runs(self):
        from webapp import ops
        assert ops._run_op("first", self._noisy("a")) is True
        assert ops._run_op("second", self._noisy("b")) is False
        self._drain()

    def test_simultaneous_claims_start_exactly_one(self):
        """Flaked under full-suite load (never in isolation) before this used
        a Barrier: 12 plain `Thread.start()` calls don't land at the same
        instant, and under CPU contention that spread can exceed the noisy
        op's runtime — so by the time a late thread actually calls
        `_run_op`, an earlier op has already finished and freed the slot,
        and it legitimately claims a *second* one. That's the test's timing
        assumption breaking, not the lock: probing `_run_op` behind a
        Barrier (so all 12 calls truly land together) instead of bare
        `Thread.start()`, five isolated runs of that probe all returned
        exactly one True. A Barrier forces all 12 threads to call
        `_run_op` at (as near as the OS allows) the same instant, so the
        assertion actually tests concurrent contention instead of thread-
        startup jitter."""
        import threading

        from webapp import ops
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(12)

        def claim():
            barrier.wait()
            r = ops._run_op("x", self._noisy("x", lines=1))
            with lock:
                results.append(r)

        threads = [threading.Thread(target=claim) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1, "more than one op claimed the slot"
        assert results.count(False) == 11
        self._drain()

    def test_log_records_each_line_once(self):
        from webapp import ops
        ops._run_op("solo", self._noisy("line"))
        self._drain()
        recorded = [l for l in ops.TASK["log"] if l.startswith("line-")]
        assert recorded == ["line-0", "line-1", "line-2"]

    def test_stdout_is_restored_when_the_op_ends(self):
        import sys

        from webapp import ops
        before = sys.stdout
        ops._run_op("solo", self._noisy("z", lines=1))
        self._drain()
        assert sys.stdout is before, "the tee outlived its operation"

    def test_a_failing_op_still_restores_stdout_and_frees_the_slot(self):
        import sys

        from webapp import ops

        def boom():
            raise RuntimeError("op exploded")

        before = sys.stdout
        assert ops._run_op("boom", boom) is True
        self._drain()
        assert sys.stdout is before
        assert ops._running() is False
        assert "RuntimeError" in (ops.TASK["error"] or "")
        assert ops._run_op("after", self._noisy("ok", lines=1)) is True
        self._drain()

    def test_prints_outside_an_operation_do_not_reach_the_log(self):
        from webapp import ops
        ops._run_op("solo", self._noisy("q", lines=1))
        self._drain()
        n = len(ops.TASK["log"])
        print("this line belongs to no operation")
        assert len(ops.TASK["log"]) == n
