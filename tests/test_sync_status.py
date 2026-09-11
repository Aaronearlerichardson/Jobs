"""Boards the status sync cannot reconcile are named, not dropped.

data/logs/session-20260911-161836-webui-sync.log: 16:18:37 "reconciling
statuses across 216 active compan(ies)...", 16:20:51 "208 board(s)
reconciled: 32 closed, 236 reopened; 1517 open job(s) in ranking." Eight
companies disappeared between those two lines with no WARNING or ERROR
anywhere in the file, so nothing in the log said which boards' statuses
were left stale, or why.

Skipping is the right behaviour — a fetcher soft-fails to [], so closing
a board's jobs on an error or an empty snapshot would close them on the
strength of a failed fetch — but it has to be visible.
"""

import src.store as store
from src.ops import maintenance as ops


class TestSyncStatusReportsSkippedBoards:

    @staticmethod
    def _wire(monkeypatch, roster, answers):
        """Roster and board answers, with the digest/ranking tail stubbed:
        this is a test about what the sync SAYS, and rewrite_digest would
        otherwise write a real digest file."""
        monkeypatch.setattr(store, "crawlable_companies",
                            lambda conn, tag=None: roster)
        monkeypatch.setattr(ops, "fetch_all",
                            lambda sources, *a, **k: [answers[name]
                                                      for name, _ats, _fn
                                                      in sources])
        monkeypatch.setattr(ops, "_ranked", lambda *a, **k: [])
        monkeypatch.setattr(
            ops, "rewrite_digest",
            lambda conn, t, top_n=15, heading="": print(heading) or [])

    def _run(self, tmp_path, monkeypatch, capsys, local_track):
        dbp = tmp_path / "t.db"
        conn = store.connect(dbp)
        cid = store.upsert_company(
            conn, {"name": "Good Co", "ats": "greenhouse", "slug": "good"})
        store.upsert_job(conn, {
            "job_id": "gh_good_1", "company_id": cid,
            "company_name": "Good Co", "title": "Data Engineer",
            "url": "https://good.example/jobs/1", "location": "Durham, NC",
            "track": local_track["track"]})
        conn.close()

        roster = [
            {"id": cid, "name": "Good Co", "ats": "greenhouse", "slug": "good"},
            {"id": 2, "name": "Broken Co", "ats": "lever", "slug": "broken"},
            {"id": 3, "name": "Silent Co", "ats": "ashby", "slug": "silent"},
            # A roster row with no primary key: nothing can be written
            # against it, which is the third way a board vanished from the
            # 2026-09-11 count.
            {"name": "Idless Co", "ats": "greenhouse", "slug": "idless"},
        ]
        listed = [{"id": "gh_good_1", "title": "Data Engineer",
                   "url": "https://good.example/jobs/1"}]
        answers = {"Good Co": (listed, None),
                   "Broken Co": ([], RuntimeError("HTTP 404")),
                   "Silent Co": ([], None),
                   "Idless Co": (listed, None)}
        self._wire(monkeypatch, roster, answers)
        ops.sync_status_all(t={**local_track, "db_path": dbp})
        return capsys.readouterr().out

    def test_each_skipped_board_gets_its_own_warning_line(
            self, tmp_path, monkeypatch, capsys, local_track):
        out = self._run(tmp_path, monkeypatch, capsys, local_track)
        lines = [ln for ln in out.splitlines() if "[!]" in ln]
        assert len(lines) == 3, f"one warning per skipped board, got: {lines}"
        assert any("Broken Co" in ln and "(fetch error)" in ln
                   and "HTTP 404" in ln for ln in lines), lines
        assert any("Silent Co" in ln and "(empty board)" in ln
                   for ln in lines), lines
        assert any("Idless Co" in ln and "(no roster id)" in ln
                   for ln in lines), lines
        # "  [!]" is what src/session_log.py::_level_for turns into a
        # WARNING record, so these reach the session log at WARNING.
        for ln in lines:
            assert ln.lstrip().startswith("[!]"), ln

    def test_the_footer_counts_the_skips_beside_the_reconciled_count(
            self, tmp_path, monkeypatch, capsys, local_track):
        out = self._run(tmp_path, monkeypatch, capsys, local_track)
        assert ("1 board(s) reconciled: 0 closed, 0 reopened, 3 skipped "
                "(1 fetch error, 1 empty board, 1 no roster id)") in out

    def test_a_failed_fetch_never_closes_that_board_s_jobs(
            self, tmp_path, monkeypatch, capsys, local_track):
        """The skip itself is the point: an unreadable board must not have
        its stored jobs closed."""
        dbp = tmp_path / "t.db"
        conn = store.connect(dbp)
        cid = store.upsert_company(
            conn, {"name": "Broken Co", "ats": "lever", "slug": "broken"})
        store.upsert_job(conn, {
            "job_id": "lv_broken_1", "company_id": cid,
            "company_name": "Broken Co", "title": "Data Engineer",
            "url": "https://broken.example/jobs/1", "location": "Durham, NC",
            "track": local_track["track"]})
        conn.close()

        roster = [{"id": cid, "name": "Broken Co", "ats": "lever",
                   "slug": "broken"}]
        self._wire(monkeypatch, roster,
                   {"Broken Co": ([], RuntimeError("HTTP 500"))})
        ops.sync_status_all(t={**local_track, "db_path": dbp})

        conn = store.connect(dbp)
        status = conn.execute("SELECT COALESCE(status,'open') FROM jobs "
                              "WHERE job_id='lv_broken_1'").fetchone()[0]
        conn.close()
        assert status == "open"
        assert "0 board(s) reconciled: 0 closed, 0 reopened, 1 skipped " \
               "(1 fetch error)" in capsys.readouterr().out
