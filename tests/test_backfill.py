"""Description-backfill retry throttle.

2026-08-28 session logs showed backfill-descriptions re-fetching the same
dozen boards on every run to fail on the same vanished postings ("0 of 18
backfilled", three runs in a row): a stale row whose posting has dropped off
its board can never match, and nothing recorded the failed attempt. Failed
attempts now stamp jobs.desc_checked_at, and reruns skip rows checked in the
last `retry_days` days.
"""

import src.store as store
from src.ops import maintenance as ops
from src.ats.fetchers import company as company_fetch


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

    def test_rows_of_boardless_companies_are_stamped_too(
            self, tmp_path, capsys):
        """A company row with no ats has no board to fetch — that IS a
        failed attempt. Unstamped, these rows were re-selected (and
        silently re-counted, never printed) on every run: the 2026-08-28
        20:00 log said "backfilling 18" while only 14 rows ever appeared."""
        dbp = tmp_path / "t.db"
        conn = store.connect(dbp)
        cid = store.upsert_company(conn, {"name": "Boardless Co"})
        store.upsert_job(conn, {
            "job_id": "bl_1", "company_id": cid,
            "company_name": "Boardless Co", "title": "Engineer",
            "url": "https://boardless.io/1", "location": "Durham, NC",
            "track": "local-tech"})
        conn.close()
        t = {"db_path": dbp}

        ops.backfill_board_descriptions(t=t)
        assert "backfilling 1 description(s)" in capsys.readouterr().out
        ops.backfill_board_descriptions(t=t)
        out = capsys.readouterr().out
        assert "backfilling 0 description(s)" in out
        assert "1 skipped: failed in the last 3d" in out

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


class TestWorkdayBackfillHonoursTheTrack:
    """The Workday backfill lived in the fetcher module, where `track_store`
    was out of reach: it called `store.connect()` with no argument, so it
    always ran against the DEFAULT store no matter which track's button was
    pressed. Under a profile that gives a track its own `db` that meant it
    reported the track's name while reading and writing someone else's
    rows. It sits with the other backfills now and takes `t` like them.
    """

    @staticmethod
    def _seed(dbp, job_id):
        conn = store.connect(dbp)
        store.upsert_job(conn, {
            "job_id": job_id, "company_name": "Acme", "title": "Engineer",
            "url": "https://acme.wd5.myworkdayjobs.com/X/job/RTP/Eng_R1",
            "location": "Durham, NC", "track": "local-tech"})
        conn.close()

    def test_it_reads_and_writes_the_given_track_store(
            self, tmp_path, monkeypatch):
        theirs, mine = tmp_path / "default.db", tmp_path / "track.db"
        self._seed(theirs, "wd_theirs")
        self._seed(mine, "wd_mine")
        # Fail loudly if the op ever reaches for the default store again.
        monkeypatch.setattr(store, "connect", _only(mine))
        monkeypatch.setattr(
            "src.ats.fetchers.workday.fetch_workday_description",
            lambda url: "A real Workday JD body.")

        assert ops.backfill_workday_descriptions(t={"db_path": mine}) == 1

        conn = store.connect(mine)
        assert conn.execute("SELECT description FROM jobs").fetchone()[0] \
            == "A real Workday JD body."
        conn.close()


def _only(allowed):
    """store.connect, refusing any path but `allowed`."""
    real = store.connect

    def _connect(db_path=None, *a, **kw):
        assert db_path == allowed, f"opened {db_path!r}, not the track's store"
        return real(db_path, *a, **kw)
    return _connect


class TestBoardBackfillFetchesCompaniesConcurrently:
    """The board backfill takes `max_workers` and, until 2026-09-11, never
    used it.

    data/logs/session-20260911-162142-webui-backfill-descriptions.log:
    16:21:43 "backfilling 43600 description(s) via company board(s)...";
    by 16:40:39, nineteen minutes later, about twenty company lines and
    1,878 rows had been written, with "J&J MedTech 1063 stale -> 1060
    matched" (16:37:11) holding the whole op for some nine minutes of
    that. The log has no footer because the run was killed. Every board
    pull and every detail hydration was happening on the calling thread,
    one company after another, exactly as if max_workers were 1.
    """

    @staticmethod
    def _seed(dbp, names):
        conn = store.connect(dbp)
        for i, name in enumerate(names):
            cid = store.upsert_company(
                conn, {"name": name, "ats": "greenhouse", "slug": f"s{i}"})
            store.upsert_job(conn, {
                "job_id": f"gh_s{i}_1", "company_id": cid,
                "company_name": name, "title": "Data Engineer",
                "url": f"https://{i}.example/jobs/1", "location": "Durham, NC",
                "track": "local-tech"})
        conn.close()

    @staticmethod
    def _board(company, loc_re=None):
        return [{"title": "Data Engineer",
                 "description": f"A real JD body from {company['name']}."}]

    def test_the_companies_go_through_fan_out_with_the_given_workers(
            self, tmp_path, monkeypatch):
        dbp = tmp_path / "t.db"
        names = ["Acme", "Beacon", "Cirrus"]
        self._seed(dbp, names)
        monkeypatch.setattr(company_fetch, "fetch_company", self._board)
        monkeypatch.setattr(company_fetch, "hydrate_description",
                            lambda job: None)

        seen = {}
        real_fan_out = ops.fan_out

        def _spy(items, fn, label="task", max_workers=None, **kw):
            items = list(items)
            seen["items"], seen["max_workers"] = items, max_workers
            return real_fan_out(items, fn, label, max_workers, **kw)

        monkeypatch.setattr(ops, "fan_out", _spy)
        assert ops.backfill_board_descriptions(t={"db_path": dbp},
                                               max_workers=5) == 3
        assert seen["max_workers"] == 5, "max_workers must reach the pool"
        assert sorted(c["name"] for c, _rows in seen["items"]) == sorted(names), \
            "every company's board fetch must be submitted to the pool"

    def test_three_boards_are_actually_in_flight_at_once(
            self, tmp_path, monkeypatch):
        """A barrier that only releases when three fetches are inside it at
        the same moment: serial execution deadlocks it, the timeout fires,
        board_index reports the failure and nothing is backfilled."""
        import threading

        dbp = tmp_path / "t.db"
        self._seed(dbp, ["Acme", "Beacon", "Cirrus"])
        barrier = threading.Barrier(3, timeout=20)

        def _fetch(company, loc_re=None):
            barrier.wait()
            return self._board(company)

        monkeypatch.setattr(company_fetch, "fetch_company", _fetch)
        monkeypatch.setattr(company_fetch, "hydrate_description",
                            lambda job: None)
        assert ops.backfill_board_descriptions(t={"db_path": dbp},
                                               max_workers=3) == 3

    def test_every_row_is_still_written_and_counted(
            self, tmp_path, monkeypatch, capsys):
        """Concurrency changes when a company line prints (completion order
        now), not what the run does: the same rows are written, the same
        per-company summaries and footer are printed."""
        dbp = tmp_path / "t.db"
        self._seed(dbp, ["Acme", "Beacon"])
        monkeypatch.setattr(company_fetch, "fetch_company", self._board)
        monkeypatch.setattr(company_fetch, "hydrate_description",
                            lambda job: None)

        assert ops.backfill_board_descriptions(t={"db_path": dbp}) == 2
        out = capsys.readouterr().out
        assert "Acme" in out and "Beacon" in out
        assert "1 stale ->  1 matched" in out
        assert "2 of 2 description(s) backfilled." in out

        conn = store.connect(dbp)
        bodies = [r[0] for r in conn.execute(
            "SELECT description FROM jobs ORDER BY job_id").fetchall()]
        conn.close()
        assert bodies == ["A real JD body from Acme.",
                          "A real JD body from Beacon."]
