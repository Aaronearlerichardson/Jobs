"""Description-backfill retry throttle.

2026-08-28 session logs showed backfill-descriptions re-fetching the same
dozen boards on every run to fail on the same vanished postings ("0 of 18
backfilled", three runs in a row): a stale row whose posting has dropped off
its board can never match, and nothing recorded the failed attempt. Failed
attempts now stamp jobs.desc_checked_at, and reruns skip rows checked in the
last `retry_days` days.
"""

import core.store as store
from scrapers import ops
from scrapers.fetchers import company as company_fetch


class TestBackfillRetryThrottle:
    @staticmethod
    def _seed(dbp):
        conn = store.connect(dbp)
        cid = store.upsert_company(
            conn, {"name": "Acme", "ats": "greenhouse", "slug": "acme"})
        store.upsert_job(conn, {
            "job_id": "gh_acme_gone", "company_id": cid,
            "company_name": "Acme", "title": "Vanished Engineer",
            "url": "https://acme.io/jobs/1", "location": "Durham, NC",
            "track": "local-tech"})
        conn.close()

    def test_failed_rows_are_stamped_and_skipped_on_rerun(
            self, tmp_path, monkeypatch, capsys):
        dbp = tmp_path / "t.db"
        self._seed(dbp)
        # The board no longer lists the job, and its detail page is gone too.
        monkeypatch.setattr(company_fetch, "fetch_company",
                            lambda *a, **k: [])
        monkeypatch.setattr(company_fetch, "hydrate_description",
                            lambda stub: None)
        t = {"db_path": dbp}

        assert ops.backfill_board_descriptions(t=t) == 0
        out = capsys.readouterr().out
        assert "backfilling 1 description(s)" in out

        # Rerun inside the retry window: the row is skipped, its board is
        # never fetched.
        fetched = []
        monkeypatch.setattr(company_fetch, "fetch_company",
                            lambda *a, **k: fetched.append(1) or [])
        assert ops.backfill_board_descriptions(t=t) == 0
        out = capsys.readouterr().out
        assert "backfilling 0 description(s)" in out
        assert "1 skipped: failed in the last 3d" in out
        assert not fetched, "a recently-failed row must not re-fetch its board"

        # retry_days=0 forces the retry.
        assert ops.backfill_board_descriptions(t=t, retry_days=0) == 0
        out = capsys.readouterr().out
        assert "backfilling 1 description(s)" in out
        assert fetched, "retry_days=0 must retry the row"

    def test_a_successful_backfill_is_not_throttled(
            self, tmp_path, monkeypatch, capsys):
        dbp = tmp_path / "t.db"
        self._seed(dbp)
        board_row = {"title": "Vanished Engineer", "description": ""}
        monkeypatch.setattr(company_fetch, "fetch_company",
                            lambda *a, **k: [board_row])
        monkeypatch.setattr(
            company_fetch, "hydrate_description",
            lambda stub: stub.__setitem__("description", "A real JD body."))
        assert ops.backfill_board_descriptions(t={"db_path": dbp}) == 1
        conn = store.connect(dbp)
        row = conn.execute("SELECT description, desc_checked_at FROM jobs "
                           "WHERE job_id='gh_acme_gone'").fetchone()
        conn.close()
        assert row["description"] == "A real JD body."
        assert row["desc_checked_at"] is None, \
            "success must not stamp the failure timestamp"
