"""The background harvester and the store changes that let it share the
file with the crawl and the web UI."""

import sqlite3
import threading

import pytest

from core import store
from scrapers import harvest


def _company(conn, name, ats="greenhouse", **extra):
    store.upsert_company(conn, {"name": name, "ats": ats,
                                "slug": name.lower(), **extra})
    return store.get_company(conn, store.company_id_by_name(conn, name))


def _job(i, desc=""):
    return {"id": f"gh_acme_{i}", "title": f"Engineer {i}",
            "url": f"https://x.test/j/{i}", "location": "Durham, NC",
            "description": desc, "ats": "greenhouse", "_wd": None}


# ── store: concurrency ──────────────────────────────────────────────────────

def test_connect_uses_wal_and_busy_timeout(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000


def test_two_writers_in_two_threads_do_not_collide(tmp_path):
    """The failure this guards: one writer got 'database is locked' the
    moment another process overlapped it."""
    db = tmp_path / "s.db"
    store.connect(db).close()
    errors = []

    def writer(tag):
        try:
            c = store.connect(db)
            with store.batch(c):
                for i in range(200):
                    store.upsert_job(c, {"job_id": f"{tag}{i}", "title": "T"})
            c.close()
        except sqlite3.OperationalError as e:      # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    c = store.connect(db)
    assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 400


def test_batch_is_one_transaction(tmp_path):
    db = tmp_path / "s.db"
    a = store.connect(db)
    b = store.connect(db)
    with store.batch(a):
        store.upsert_job(a, {"job_id": "x1", "title": "T"})
        # Uncommitted to other connections while the block is open.
        assert not store.job_exists(b, "x1")
    assert store.job_exists(b, "x1")


# ── store: harvested rows vs the crawl ──────────────────────────────────────

def test_upsert_never_blanks_a_description():
    conn = store.connect(":memory:")
    store.upsert_job(conn, {"job_id": "j", "title": "T", "description": "body"})
    store.upsert_job(conn, {"job_id": "j", "title": "T", "description": ""})
    store.upsert_job(conn, {"job_id": "j", "title": "T"})
    assert conn.execute("SELECT description FROM jobs").fetchone()[0] == "body"


def test_harvested_row_is_unseen_by_crawl_until_tracked():
    conn = store.connect(":memory:")
    store.upsert_job(conn, {"job_id": "h", "title": "T",
                            "harvested_at": "2026-09-10T00:00:00"})
    assert store.job_exists(conn, "h") and not store.crawl_seen(conn, "h")
    store.upsert_job(conn, {"job_id": "h", "title": "T", "track": "local",
                            "resume_fit_score": 0.5})
    assert store.crawl_seen(conn, "h")
    row = conn.execute("SELECT harvested_at, track FROM jobs").fetchone()
    assert row["harvested_at"] == "2026-09-10T00:00:00"   # stamp survives
    assert row["track"] == "local"


def test_offmission_volume_ignores_unscored_harvest_rows():
    conn = store.connect(":memory:")
    c = _company(conn, "Acme")
    for i in range(40):                      # 40 harvested, unscored
        store.upsert_job(conn, {"job_id": f"u{i}", "company_id": c["id"],
                                "title": "T", "harvested_at": "x"})
    for i in range(3):                       # 3 scored, all poor
        store.upsert_job(conn, {"job_id": f"s{i}", "company_id": c["id"],
                                "title": "T", "resume_fit_score": 0.1})
    assert not store._offmission_volume(conn, c["id"])


def test_harvestable_ignores_active_dormant_and_tags_but_not_dead_boards():
    conn = store.connect(":memory:")
    _company(conn, "Live")
    _company(conn, "Parked", crawl_state="dormant", next_crawl_at="2999-01-01")
    _company(conn, "Inactive", active=0)
    store.upsert_company(conn, store.mark_pending(
        {"name": "Pending", "ats": "lever", "slug": "p"}))
    store.record_miss(conn, "NoLocal", "no-local-jobs", ats="lever", slug="n")
    store.record_miss(conn, "Gone", "no-board-found", ats="lever", slug="g")
    store.record_miss(conn, "Dead", "board-dead:lever", ats="lever", slug="d")
    store.upsert_company(conn, {"name": "NoAts"})
    names = sorted(c["name"] for c in store.harvestable_companies(conn))
    assert names == ["Inactive", "Live", "NoLocal", "Parked", "Pending"]


# ── harvest_board ───────────────────────────────────────────────────────────

def test_harvest_board_stores_hydrates_and_closes(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    # A row from an earlier crawl that has since left the board.
    store.upsert_job(conn, {"job_id": "gh_acme_old", "company_id": c["id"],
                            "title": "Gone", "track": "local",
                            "resume_fit_score": 0.4})
    board = [_job(1, "already has a body"), _job(2)]
    monkeypatch.setattr(harvest, "fetch_whole_board", lambda comp: board)

    def fake_hydrate(job):
        job["description"] = "fetched body"
        return job
    monkeypatch.setattr(harvest.company_fetch, "hydrate_description",
                        fake_hydrate)
    ticks = []
    stats = harvest.harvest_board(c, db, progress=lambda: ticks.append(1),
                                  delay=0)

    assert stats["err"] is None
    assert (stats["fetched"], stats["new"], stats["hydrated"],
            stats["closed"]) == (2, 2, 1, 1)
    assert len(ticks) == 2                   # listing + one hydrated row
    rows = {r["job_id"]: dict(r) for r in
            conn.execute("SELECT * FROM jobs")}
    assert rows["gh_acme_old"]["status"] == "closed"
    assert rows["gh_acme_2"]["description"] == "fetched body"
    assert rows["gh_acme_1"]["description"] == "already has a body"
    assert rows["gh_acme_1"]["harvested_at"] and rows["gh_acme_1"]["track"] is None
    assert rows["gh_acme_1"]["resume_fit_score"] is None
    comp = store.get_company(conn, c["id"])
    assert comp["last_harvested_at"] and comp["total_job_count"] == 2
    # The crawl's own bookkeeping is untouched.
    assert comp["crawl_state"] is None and comp["last_crawled_at"] is None


def test_harvest_board_fetch_error_leaves_store_alone(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    store.upsert_job(conn, {"job_id": "gh_acme_old", "company_id": c["id"],
                            "title": "Still open", "track": "local"})

    def boom(comp):
        raise RuntimeError("503")
    monkeypatch.setattr(harvest, "fetch_whole_board", boom)
    stats = harvest.harvest_board(c, db, delay=0)
    assert stats["err"] == "RuntimeError: 503" and stats["fetched"] == 0
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "open"
    assert store.get_company(conn, c["id"])["last_harvested_at"] is None


def test_harvest_board_stops_hydrating_a_host_that_stopped_answering(
        tmp_path, monkeypatch):
    """Workday dropped the connection after ~150 detail GETs; without this
    the remaining rows each burned two doomed requests."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", ats="workday", wd_tenant="acme", wd_pod=5,
                 wd_site="Ext")
    board = [_job(i) for i in range(40)]
    monkeypatch.setattr(harvest, "fetch_whole_board", lambda comp: board)
    calls = []
    monkeypatch.setattr(harvest.company_fetch, "hydrate_description",
                        lambda j: calls.append(j) or j)      # never a body
    naps = []
    monkeypatch.setattr(harvest.time, "sleep", lambda s: naps.append(s))
    stats = harvest.harvest_board(c, db, delay=0, backoff_s=7)
    # One streak -> pause -> second streak -> stop. 2 * MISS_STREAK calls.
    assert len(calls) == 2 * harvest.MISS_STREAK
    assert naps == [7]
    assert stats["hydrated"] == 0 and stats["unhydrated"] == 40
    assert stats["fetched"] == 40 and stats["new"] == 40   # still stored


def test_harvest_board_reuses_stored_bodies_and_caps_workday(
        tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", ats="workday", wd_tenant="acme", wd_pod=5,
                 wd_site="Ext")
    # 3 rows already carry a body in the store (an earlier harvest).
    for i in range(3):
        store.upsert_job(conn, {"job_id": f"gh_acme_{i}", "company_id": c["id"],
                                "title": "T", "description": "stored body",
                                "harvested_at": "x"})
    board = [_job(i) for i in range(3 + 150)]       # all bodiless listings
    monkeypatch.setattr(harvest, "fetch_whole_board", lambda comp: board)
    monkeypatch.setattr(harvest, "HYDRATE_CAP", {"workday": 100})
    calls = []

    def fake_hydrate(j):
        calls.append(j["id"])
        j["description"] = "fresh body"
        return j
    monkeypatch.setattr(harvest.company_fetch, "hydrate_description",
                        fake_hydrate)
    stats = harvest.harvest_board(c, db, delay=0)
    assert not any(cid in calls for cid in ("gh_acme_0", "gh_acme_1", "gh_acme_2"))
    assert len(calls) == 100                         # the cap, not 150
    assert stats["hydrated"] == 100 and stats["unhydrated"] == 50
    n_body = conn.execute("SELECT COUNT(*) FROM jobs WHERE "
                          "length(description) > 0").fetchone()[0]
    assert n_body == 103                             # 3 stored + 100 fresh


def test_hydrate_delay_uses_the_registry_pause():
    assert harvest.hydrate_delay("workday") == 1.0
    assert harvest.hydrate_delay("greenhouse") == harvest.HYDRATE_DELAY_S


def test_harvest_board_empty_snapshot_closes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    store.upsert_job(conn, {"job_id": "gh_acme_old", "company_id": c["id"],
                            "title": "Still open", "track": "local"})
    monkeypatch.setattr(harvest, "fetch_whole_board", lambda comp: [])
    stats = harvest.harvest_board(c, db, delay=0)
    assert stats["err"] is None and stats["closed"] == 0
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "open"


# ── run ─────────────────────────────────────────────────────────────────────

def test_run_reports_and_skips_fresh_boards(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _company(conn, "A")
    _company(conn, "B")
    _company(conn, "Fresh")
    conn.execute("UPDATE companies SET last_harvested_at=? WHERE name='Fresh'",
                 (harvest.datetime.now().isoformat(),))
    conn.commit()
    pulled = []

    def fake_board(company, db_path, progress=lambda: None, hydrate=True):
        pulled.append(company["name"])
        if company["name"] == "B":
            raise RuntimeError("down")
        return {"err": None, "fetched": 3, "new": 2, "hydrated": 1,
                "closed": 0, "reopened": 0, "secs": 0.1}

    s = harvest.run(db_path=db, max_workers=2, board_fn=fake_board)
    assert sorted(pulled) == ["A", "B"]
    assert (s["boards"], s["ok"], s["err"], s["stalled"]) == (3 - 1, 1, 1, 0)
    assert s["fetched"] == 3 and s["new"] == 2


def test_run_abandons_a_stalled_board(tmp_path):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _company(conn, "Wedged")
    release = threading.Event()

    def fake_board(company, db_path, progress=lambda: None, hydrate=True):
        release.wait(5)                      # no progress() calls at all
        return {"err": None, "fetched": 0, "new": 0, "hydrated": 0,
                "closed": 0, "reopened": 0, "secs": 0.0}

    s = harvest.run(db_path=db, max_workers=1, board_fn=fake_board,
                    stall_s=0.0, poll_s=0.1)
    release.set()
    assert s["stalled"] == 1 and s["ok"] == 0


# ── the crawl adopts harvested rows ─────────────────────────────────────────

def test_runner_treats_harvested_rows_as_fresh(tmp_path, monkeypatch):
    """The end-to-end contract: a harvested row (no track) is scored by the
    next crawl, and the crawl reuses the stored description instead of
    re-hydrating."""
    import config
    from scrapers import ops, runner
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path)
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_score=0.9, tags="local")
    store.upsert_job(conn, {"job_id": "gh_acme_1", "company_id": c["id"],
                            "company_name": "Acme", "title": "Data Engineer",
                            "url": "https://x.test/j/1",
                            "location": "Durham, NC",
                            "description": "python sql pipelines " * 30,
                            "harvested_at": "2026-09-10T00:00:00"})
    board = [{"id": "gh_acme_1", "title": "Data Engineer",
              "url": "https://x.test/j/1", "location": "Durham, NC",
              "description": "", "ats": "greenhouse", "_wd": None}]
    t = next(iter(config.UI_TRACKS.values()))
    t = {**t, "db_path": db, "sources": {**t["sources"]},
         "email": False, "verify_top": 0, "require_core_anchor": False,
         "exclude_gate": False, "geo_gate": False, "cost_guard": 0}
    monkeypatch.setattr(runner, "build_sources", lambda cfg, tt, include_websearch=None: [
        {"name": "Acme", "platform": "greenhouse", "company": c,
         "thunk": lambda: board}])
    monkeypatch.setattr(runner, "resume_text", lambda: "resume text")
    hydrated = []                # jobs that reached the network path bodiless

    def fake_hydrate(j):
        if not j.get("description"):
            hydrated.append(j)
        return j
    monkeypatch.setattr(ops.company_fetch, "hydrate_description", fake_hydrate)
    monkeypatch.setattr(ops.gates, "is_technical_role",
                        lambda title, tt: True)

    class R:
        score = 0.7

        def as_columns(self):
            return {"resume_fit_score": 0.7, "fit_reason": "ok"}
    monkeypatch.setattr(ops, "score_resume_fit",
                        lambda resume, title, desc: R())
    monkeypatch.setattr(ops, "self_heal_unscored", lambda *a, **k: 0)
    runner.run_track(t, fit=True, commit=True, send=False, verify=False,
                     websearch=False)
    conn = store.connect(db)
    row = conn.execute("SELECT * FROM jobs WHERE job_id='gh_acme_1'").fetchone()
    assert row["resume_fit_score"] == 0.7
    assert t["track"] in store.track_set(row["track"])
    assert not hydrated, "stored description should have been reused"
