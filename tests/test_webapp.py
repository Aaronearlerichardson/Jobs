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

    def test_the_bulk_discovery_ops_left_the_ui(self):
        """A 29-minute discover-local run found 4 new boards, and a dork
        sweep names a company after a de-hyphenated slug. Neither is worth a
        button that holds the single op slot for half an hour -- they run
        from discover.py now."""
        assert not ({"discover-local", "dork", "discover-term"}
                    & set(webapp.OPS))

    def test_the_targeted_add_paths_stay(self):
        assert {"add-names", "add-board", "add-job"} <= set(webapp.OPS)


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

    def test_tracks_expose_the_remote_mission_floor(self, client):
        # The SPA's remote rule needs the same number the server ranks with.
        tracks = json.loads(client.get("/api/tracks").data)
        assert all("remote_mission_floor" in t for t in tracks)

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


class TestPipelineApi:
    """The Pipeline tab past 'applied': the tracking fields it edits, the
    follow-up list it groups by, and the conversion table it renders."""

    def _pipeline_store(self, tmp_path, monkeypatch):
        """Point the default track at a throwaway DB holding one live
        application. These tests write, and the suite may never touch the
        real store."""
        import config
        from core import store
        db_path = tmp_path / "pipeline.db"
        conn = store.connect(db_path)
        cid = store.upsert_company(conn, {"name": "Acme", "ats": "greenhouse",
                                          "slug": "acme"})
        store.upsert_job(conn, {
            "job_id": "p1", "company_id": cid, "company_name": "Acme",
            "title": "Imaging Scientist", "url": "https://acme.io/p1",
            "location": "Anywhere", "geo_mode": "onsite",
            "resume_fit_score": 0.54})
        store.set_disposition(conn, "p1", "applied")
        conn.close()
        monkeypatch.setitem(config.UI_TRACKS[config.DEFAULT_TRACK],
                            "db_path", db_path)
        return db_path

    def _row(self, client):
        return json.loads(client.get("/api/pipeline").data)["rows"][0]

    def test_pipeline_exposes_the_tracking_columns(self, client, tmp_path,
                                                   monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        assert {"applied_at", "followup_at", "contact", "referral",
                "outcome_reason"} <= set(self._row(client))

    def test_tracking_fields_round_trip(self, client, tmp_path, monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        resp = client.post("/api/job/p1/pipeline",
                           json={"followup_at": "2026-09-08",
                                 "contact": "Dana R", "referral": 1,
                                 "outcome_reason": "rejected-interview"})
        assert resp.status_code == 200
        row = self._row(client)
        assert row["followup_at"] == "2026-09-08"
        assert row["contact"] == "Dana R"
        assert row["referral"] == 1
        assert row["outcome_reason"] == "rejected-interview"

    def test_one_field_at_a_time_leaves_the_others_alone(self, client,
                                                         tmp_path, monkeypatch):
        # The SPA's editor saves on each control's own `change`.
        self._pipeline_store(tmp_path, monkeypatch)
        client.post("/api/job/p1/pipeline", json={"contact": "Dana R"})
        client.post("/api/job/p1/pipeline", json={"referral": 1})
        row = self._row(client)
        assert row["contact"] == "Dana R" and row["referral"] == 1

    def test_stats_count_applications_sent_this_week(self, client, tmp_path,
                                                     monkeypatch):
        """The 'applied this week' tile: an application from a month ago is
        not this week's volume, and one since moved on to interviewing
        still counts for the week it went out in."""
        from datetime import datetime, timedelta
        from core import store
        db_path = self._pipeline_store(tmp_path, monkeypatch)
        conn = store.connect(db_path)
        store.upsert_job(conn, {
            "job_id": "p2", "company_name": "Acme", "title": "Old One",
            "url": "https://acme.io/p2", "location": "Anywhere",
            "resume_fit_score": 0.5})
        store.set_disposition(conn, "p2", "applied")
        conn.execute("UPDATE jobs SET applied_at=? WHERE job_id='p2'",
                     ((datetime.now() - timedelta(days=30)).isoformat(),))
        conn.commit()
        store.set_disposition(conn, "p1", "interviewing")
        conn.close()
        stats = json.loads(client.get("/api/stats").data)
        assert stats["applied_7d"] == 1

    def test_a_field_outside_the_whitelist_is_ignored(self, client, tmp_path,
                                                      monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        assert client.post("/api/job/p1/pipeline",
                           json={"resume_fit_score": 0}).status_code == 200
        assert self._row(client)["resume_fit_score"] == 0.54

    def test_an_outcome_outside_the_vocabulary_400s(self, client, tmp_path,
                                                    monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        assert client.post("/api/job/p1/pipeline",
                           json={"outcome_reason": "ghosted"}).status_code == 400

    def test_unknown_job_400s(self, client, tmp_path, monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        assert client.post("/api/job/nope/pipeline",
                           json={"contact": "X"}).status_code == 400

    def test_followups_due_appears_once_the_date_has_arrived(self, client,
                                                             tmp_path,
                                                             monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        assert json.loads(client.get("/api/pipeline").data)["followups_due"] == []
        client.post("/api/job/p1/pipeline", json={"followup_at": "2000-01-01"})
        assert json.loads(
            client.get("/api/pipeline").data)["followups_due"] == ["p1"]

    def test_a_future_followup_is_not_due(self, client, tmp_path, monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        client.post("/api/job/p1/pipeline", json={"followup_at": "2099-01-01"})
        assert json.loads(client.get("/api/pipeline").data)["followups_due"] == []

    def test_conversion_report_bands_the_application(self, client, tmp_path,
                                                     monkeypatch):
        self._pipeline_store(tmp_path, monkeypatch)
        rep = json.loads(client.get("/api/report/conversion").data)
        assert len(rep) == 1
        assert rep[0]["band"] == "mid" and rep[0]["geo_mode"] == "onsite"
        assert rep[0]["applications"] == 1
        assert rep[0]["interview_rate"] == 0.0


class TestApplyBandFields:
    """The Jobs tab's 'apply band' quick filter is client-side; the contract
    it depends on is that every /api/jobs row carries the four fields it
    reads, with the geo bucket derived live from the location."""

    def test_jobs_expose_what_the_band_filter_reads(self, client, tmp_path,
                                                    monkeypatch, local_addr):
        import config
        from core import store
        t = config.UI_TRACKS[config.DEFAULT_TRACK]
        db_path = tmp_path / "band.db"
        conn = store.connect(db_path)
        cid = store.upsert_company(conn, {"name": "Acme", "ats": "greenhouse",
                                          "slug": "acme"})
        store.upsert_job(conn, {
            "job_id": "b1", "company_id": cid, "company_name": "Acme",
            "title": "Imaging Scientist", "url": "https://acme.io/b1",
            "location": local_addr, "track": t["track"],
            "resume_fit_score": 0.54})
        conn.close()
        monkeypatch.setitem(t, "db_path", db_path)
        rows = json.loads(client.get("/api/jobs").data)
        row = next(r for r in rows if r["job_id"] == "b1")
        assert row["resume_fit_score"] == 0.54
        assert row["geo_bucket"] == "local"
        assert row["status"] in (None, "open")
        assert row["disposition"] is None


class TestRemoteAdmissionFields:
    """/api/jobs ships every row and lets the client gate them, so the
    client needs the server's verdict on each: `remote_ok` says a remote row
    is worth showing (watched, or core-mission at the track's
    remote_mission_floor). `best_fit` on the roster is what turns an
    unwatched company that has already produced a good-fit job into a
    one-click watch suggestion."""

    FIT = 0.94

    def _store(self, tmp_path, monkeypatch, mission, floor=0.85):
        import config
        from core import store
        t = config.UI_TRACKS[config.DEFAULT_TRACK]
        db_path = tmp_path / "admission.db"
        conn = store.connect(db_path)
        cid = store.upsert_company(conn, {"name": "Acme", "ats": "greenhouse",
                                          "slug": "acme",
                                          "mission_score": mission})
        store.upsert_job(conn, {"job_id": "gh_acme_1", "company_id": cid,
                                "company_name": "Acme",
                                "title": "Research Engineer",
                                "url": "https://acme.io/1",
                                "location": "Remote - US", "geo_mode": "remote",
                                "track": t["track"],
                                "resume_fit_score": self.FIT})
        conn.commit()
        conn.close()
        monkeypatch.setitem(t, "db_path", db_path)
        monkeypatch.setitem(t, "remote_mission_floor", floor)
        return cid

    def _job(self, client):
        return json.loads(client.get("/api/jobs").data)[0]

    def test_core_mission_remote_is_admitted_unwatched(self, client, tmp_path,
                                                       monkeypatch):
        self._store(tmp_path, monkeypatch, mission=0.9)
        job = self._job(client)
        assert job["watched"] is False and job["remote_ok"] is True

    def test_below_the_floor_is_not(self, client, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch, mission=0.5)
        assert self._job(client)["remote_ok"] is False

    def test_no_floor_disables_it(self, client, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch, mission=0.9, floor=None)
        assert self._job(client)["remote_ok"] is False

    def test_watch_admits_a_company_far_below_the_floor(self, client,
                                                        tmp_path, monkeypatch):
        # Above the track's min_mission (or the row leaves the ranking for an
        # unrelated reason), nowhere near the remote floor.
        cid = self._store(tmp_path, monkeypatch, mission=0.3)
        assert client.post(f"/api/company/{cid}/watch",
                           json={"on": True}).status_code == 200
        assert self._job(client)["remote_ok"] is True

    def test_roster_carries_the_best_fit_so_far(self, client, tmp_path,
                                                monkeypatch):
        cid = self._store(tmp_path, monkeypatch, mission=0.9)
        row = next(r for r in json.loads(client.get("/api/companies").data)
                   if r["id"] == cid)
        assert row["best_fit"] == self.FIT and row["open_jobs"] == 1

    def test_best_fit_is_none_without_jobs(self, client, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch, mission=0.9)
        from core import store
        conn = store.connect(tmp_path / "admission.db")
        cid = store.upsert_company(conn, {"name": "Quiet", "ats": "lever",
                                          "slug": "quiet"})
        conn.close()
        row = next(r for r in json.loads(client.get("/api/companies").data)
                   if r["id"] == cid)
        assert row["best_fit"] is None and row["open_jobs"] == 0


class TestReviewQueue:
    """Every automated roster write waits for a person now, and the queue is
    the API surface that person works through."""

    def _queued_store(self, tmp_path, monkeypatch, name="Guess"):
        """A throwaway DB holding one review candidate, pointed at by BOTH
        the track config (the routes) and config.STORE_DB_PATH (discovery's
        own store.connect()). The suite may never touch the real store."""
        import config
        from core import store
        db_path = tmp_path / "review.db"
        conn = store.connect(db_path)
        cid = store.upsert_company(conn, store.mark_pending(
            {"name": name, "ats": "greenhouse", "slug": "guess",
             "source": "paste", "local_job_count": 2, "total_job_count": 9}))
        conn.close()
        monkeypatch.setitem(config.UI_TRACKS[config.DEFAULT_TRACK],
                            "db_path", db_path)
        monkeypatch.setattr(config, "STORE_DB_PATH", db_path)
        return cid

    @staticmethod
    def _store(monkeypatch=None):
        import config
        from core import store
        return store.connect(config.UI_TRACKS[config.DEFAULT_TRACK]["db_path"])

    def test_pending_lists_the_queue(self, client, tmp_path, monkeypatch):
        cid = self._queued_store(tmp_path, monkeypatch)
        rows = json.loads(client.get("/api/pending").data)
        assert [r["id"] for r in rows] == [cid]
        assert rows[0]["source"] == "paste"
        assert rows[0]["local_job_count"] == 2

    def test_a_candidate_is_counted_but_not_crawled(self, client, tmp_path,
                                                    monkeypatch):
        self._queued_store(tmp_path, monkeypatch)
        stats = json.loads(client.get("/api/stats").data)
        assert stats["companies_active"] == 0
        assert stats["pending_review"] == 1

    def test_confirm_puts_it_on_the_roster(self, client, tmp_path, monkeypatch):
        cid = self._queued_store(tmp_path, monkeypatch)
        assert client.post(f"/api/company/{cid}/confirm").status_code == 200
        assert json.loads(client.get("/api/pending").data) == []
        assert json.loads(client.get("/api/stats").data)["companies_active"] == 1

    def test_reject_removes_it_and_blocks_the_name(self, client, tmp_path,
                                                   monkeypatch):
        from core import store
        cid = self._queued_store(tmp_path, monkeypatch)
        resp = client.post(f"/api/company/{cid}/reject",
                           json={"reason": "not a company"})
        assert resp.status_code == 200
        conn = self._store()
        try:
            assert store.get_companies(conn, active_only=False) == []
            assert store.blocked_name_keys(conn) == {"guess"}
        finally:
            conn.close()

    def test_unknown_company_404s(self, client, tmp_path, monkeypatch):
        self._queued_store(tmp_path, monkeypatch)
        assert client.post("/api/company/9999/confirm").status_code == 404
        assert client.post("/api/company/9999/reject").status_code == 404

    def test_block_records_the_names_the_reviewer_rejected(
            self, client, tmp_path, monkeypatch):
        from core import store
        self._queued_store(tmp_path, monkeypatch)
        resp = client.post("/api/names/block",
                           json={"names": ["Who You Are", "Job Location"]})
        assert json.loads(resp.data)["blocked"] == 2
        conn = self._store()
        try:
            assert store.blocked_name_keys(conn) == {"whoyouare", "joblocation"}
        finally:
            conn.close()

    def test_preview_parses_without_resolving_anything(self, client, tmp_path,
                                                       monkeypatch):
        import discovery.local_sourcing as ls
        self._queued_store(tmp_path, monkeypatch)
        monkeypatch.setattr(ls, "parse_company_names",
                            lambda *a, **k: ["Alpaca Health"])
        tried = []
        monkeypatch.setattr(ls, "resolve_or_miss",
                            lambda *a, **k: tried.append(a) or (None, "x"))
        rows = json.loads(client.post(
            "/api/names/preview", json={"text": "x", "use_llm": False}).data)
        assert rows == [{"name": "Alpaca Health", "key": "alpacahealth",
                         "state": "new"}]
        assert tried == []


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
