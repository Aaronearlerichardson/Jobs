"""The background harvester and the store changes that let it share the
file with the crawl and the web UI."""

import sqlite3
import threading

import pytest

from conftest import company_row as _company, make_board_fn

from src import config, store
from src.claude import api as claude_api
from src.match import gates
from src.crawl import harvest
from src.net import http


def _job(i, desc=""):
    return {"id": f"gh_acme_{i}", "title": f"Engineer {i}",
            "url": f"https://x.test/j/{i}", "location": "Durham, NC",
            "description": desc, "ats": "greenhouse"}


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


# ── plan: off-mission cadence ────────────────────────────────────────────────

class TestOffmissionCadence:
    """A board that is BOTH off-mission (mission_tier 'other', or never
    scored) AND inactive waits config.HARVEST_OFFMISSION_HOURS
    instead of plan()'s ordinary min_age_hours freshness cutoff -- see
    config.is_offmission_inactive and plan()'s docstring for the
    --min-age-hours interaction (an explicit override, including 0, always
    wins for every board)."""

    def _stale_board(self, conn, name, hours_ago, **extra):
        c = _company(conn, name, ats="lever", **extra)
        stamp = (harvest.datetime.now()
                 - harvest.timedelta(hours=hours_ago)).isoformat()
        conn.execute("UPDATE companies SET last_harvested_at=? WHERE id=?",
                     (stamp, c["id"]))
        return c

    def test_offmission_inactive_board_skipped_on_a_6h_pass(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_OFFMISSION_HOURS", 168.0)
        conn = store.connect(tmp_path / "s.db")
        self._stale_board(conn, "Dominos", 24, active=0, mission_tier="other")
        stats = {}
        assert harvest.plan(conn, stats=stats) == []
        assert stats["offmission_skipped"] == 1

    def test_offmission_inactive_board_is_due_after_the_long_interval(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_OFFMISSION_HOURS", 168.0)
        conn = store.connect(tmp_path / "s.db")
        self._stale_board(conn, "Dominos", 200, active=0, mission_tier="other")
        assert [c["name"] for c in harvest.plan(conn)] == ["Dominos"]

    def test_active_core_board_is_unaffected_by_the_long_interval(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_OFFMISSION_HOURS", 168.0)
        conn = store.connect(tmp_path / "s.db")
        # Older than the ordinary 6h cutoff but well under the 168h one --
        # if this board were mistakenly treated as off-mission it would
        # still be skipped, so this also proves the predicate keys off
        # active/mission_tier, not merely age.
        self._stale_board(conn, "Acme", 24, active=1, mission_tier="core-mission")
        assert [c["name"] for c in harvest.plan(conn)] == ["Acme"]

    def test_offmission_board_named_explicitly_still_bypasses_the_interval(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_OFFMISSION_HOURS", 168.0)
        conn = store.connect(tmp_path / "s.db")
        self._stale_board(conn, "Dominos", 1, active=0, mission_tier="other")
        assert [c["name"] for c in harvest.plan(conn, names=["dominos"])] \
            == ["Dominos"]

    def test_explicit_min_age_hours_zero_forces_everything(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_OFFMISSION_HOURS", 168.0)
        conn = store.connect(tmp_path / "s.db")
        self._stale_board(conn, "Dominos", 1, active=0, mission_tier="other")
        assert [c["name"] for c in harvest.plan(conn, min_age_hours=0)] \
            == ["Dominos"]

    def test_run_header_names_offmission_boards_deferred_this_pass(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(config, "HARVEST_OFFMISSION_HOURS", 168.0)
        db = tmp_path / "s.db"
        conn = store.connect(db)
        self._stale_board(conn, "Dominos", 24, active=0, mission_tier="other")
        self._stale_board(conn, "Acme", 24, active=1, mission_tier="core-mission")
        harvest.run(db_path=db, max_workers=1, board_fn=make_board_fn(),
                    triage=False)
        out = capsys.readouterr().out
        header = next(l for l in out.splitlines() if "board(s)" in l)
        assert "1 off-mission board(s) deferred to 168h" in header


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

    def fake_hydrate(job, company=None):
        job["description"] = "fetched body"
        return job
    monkeypatch.setattr(harvest.company_fetch, "hydrate_description",
                        fake_hydrate)
    ticks = []
    stats = harvest.harvest_board(c, db, progress=lambda: ticks.append(1),
                                  delay=0, hydrate=True)

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
    assert stats["err"] == "fetch: RuntimeError: 503" and stats["fetched"] == 0
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "open"
    assert store.get_company(conn, c["id"])["last_harvested_at"] is None


def test_harvest_board_store_error_is_not_reported_as_a_fetch_error(
        tmp_path, monkeypatch):
    """A write that fails -- "database is locked" is the one that happens --
    must say so. Reported as a fetch error (the 2026-09-10 logs), a store
    that could not take the write lock reads as 200 unreachable boards, and
    the real cause (an unindexed full scan inside the write transaction)
    stays invisible."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    monkeypatch.setattr(harvest, "fetch_whole_board",
                        lambda comp: [{"id": "gh_acme_1", "title": "T",
                                       "url": "u", "location": "Durham, NC"}])

    def locked(_path):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(harvest.store, "connect", locked)
    stats = harvest.harvest_board(c, db, delay=0)
    assert stats["err"] == "store: OperationalError: database is locked"
    assert stats["fetched"] == 1, "the board WAS fetched; only the write failed"


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
                        lambda j, company=None: calls.append(j) or j)  # never a body
    naps = []
    monkeypatch.setattr(harvest.time, "sleep", lambda s: naps.append(s))
    stats = harvest.harvest_board(c, db, delay=0, backoff_s=7, hydrate=True)
    # One streak -> pause -> second streak -> stop. 2 * MISS_STREAK calls.
    assert len(calls) == 2 * harvest.MISS_STREAK
    assert naps == [7]
    assert stats["hydrated"] == 0 and stats["unhydrated"] == 40
    assert stats["fetched"] == 40 and stats["new"] == 40   # still stored


def test_harvest_board_reuses_stored_bodies_and_caps_hydration(
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
    monkeypatch.setattr(config, "HYDRATE_CAP_PER_RUN", 100)
    calls = []

    def fake_hydrate(j, company=None):
        calls.append(j["id"])
        j["description"] = "fresh body"
        return j
    monkeypatch.setattr(harvest.company_fetch, "hydrate_description",
                        fake_hydrate)
    stats = harvest.harvest_board(c, db, delay=0, hydrate=True)
    assert not any(cid in calls for cid in ("gh_acme_0", "gh_acme_1", "gh_acme_2"))
    assert len(calls) == 100                         # the cap, not 150
    assert stats["hydrated"] == 100 and stats["unhydrated"] == 50
    n_body = conn.execute("SELECT COUNT(*) FROM jobs WHERE "
                          "length(description) > 0").fetchone()[0]
    assert n_body == 103                             # 3 stored + 100 fresh


def test_harvest_board_keeps_a_resolved_location_over_a_placeholder(
        tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    board = [dict(_job(1), location="2 Locations"),
             dict(_job(2, desc="body 2"), location="Peoria, IL")]
    store.upsert_job(conn, {"job_id": "gh_acme_1", "company_id": c["id"],
                            "title": "Engineer 1", "location": "2 Locations"})
    store.store_body(conn, "gh_acme_1", "body", "Springfield, IL")
    monkeypatch.setattr(harvest, "fetch_whole_board", lambda comp: board)
    harvest.harvest_board(c, db, delay=0)
    locs = dict(conn.execute("SELECT job_id, location FROM jobs"))
    assert locs == {"gh_acme_1": "Springfield, IL", "gh_acme_2": "Peoria, IL"}


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


def test_a_dead_board_is_told_apart_from_an_empty_one(tmp_path, monkeypatch):
    """Both return [], so only the failure COUNT separates them.

    116 of 620 boards came back empty in every harvest run across 25 logs
    -- dead slugs like the Lever board "netherlands" -- and every one was
    recorded as an ordinary empty board, because a fetcher reports its 404
    to stdout and then returns [] exactly as a board with no openings
    does. net.http counts what it reports; the harvester reads the count.
    """
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")

    def empty(comp):
        return []

    def dead(comp):
        return http.fetch_failed("Lever netherlands", "404 Client Error")

    monkeypatch.setattr(harvest, "fetch_whole_board", empty)
    quiet = harvest.harvest_board(c, db, delay=0)

    monkeypatch.setattr(harvest, "fetch_whole_board", dead)
    gone = harvest.harvest_board(c, db, delay=0)

    assert quiet["fetched"] == gone["fetched"] == 0
    assert quiet["fetch_errors"] == 0
    assert gone["fetch_errors"] == 1
    assert quiet["last_error"] is None
    assert gone["last_error"] == "Lever netherlands: 404 Client Error"
    # A soft failure never becomes the hard `err` path -- it must not
    # change the run()-level failed count (see run()'s _report).
    assert gone["err"] is None
    # Neither closes anything: an empty snapshot is still not evidence.
    assert quiet["closed"] == gone["closed"] == 0


# ── dead-board promotion cycle (mark_harvested's soft_fail, end to end) ─────

def test_harvest_board_soft_failure_keeps_the_count_and_records_a_miss(
        tmp_path, monkeypatch):
    """harvest_board's soft_fail verdict (no rows, and the fetch itself
    reported errors) reaching store.mark_harvested; the rest of the cycle
    is tests/test_store.py's TestMarkHarvested."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", ats="lever", total_job_count=9)
    monkeypatch.setattr(harvest, "fetch_whole_board",
                        lambda comp: http.fetch_failed("Acme board", "500"))
    harvest.harvest_board(c, db, delay=0, now=harvest.datetime(2026, 1, 1))
    row = store.get_company(conn, c["id"])
    assert row["total_job_count"] == 9, "the last known-good count survives"
    assert row["last_harvested_at"]
    assert row["miss_reason"] == "fetch-error:harvest"
    assert row["miss_at"] == harvest.datetime(2026, 1, 1).isoformat()


def test_harvest_board_promotes_to_board_dead_after_three_days(
        tmp_path, monkeypatch, capsys):
    from src.store.companies import HARVEST_DEAD_AFTER_DAYS
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", ats="lever")
    monkeypatch.setattr(harvest, "fetch_whole_board",
                        lambda comp: http.fetch_failed("x", "500"))
    harvest.harvest_board(c, db, delay=0, now=harvest.datetime(2026, 1, 1))
    c2 = store.get_company(conn, c["id"])
    harvest.harvest_board(
        c2, db, delay=0,
        now=harvest.datetime(2026, 1, 1) + harvest.timedelta(
            days=HARVEST_DEAD_AFTER_DAYS))
    row = store.get_company(conn, c["id"])
    assert row["miss_reason"] == "board-dead:lever"
    assert row["active"] == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "Acme" in l and "[!]" in l)
    assert "board-dead:lever" in line
    assert "Acme" not in [r["name"] for r in store.harvestable_companies(conn)]


@pytest.mark.parametrize("ats, dead", [("greenhouse", True),
                                       ("workday", False)])
def test_harvest_board_buries_a_second_definitive_404(tmp_path, monkeypatch,
                                                      ats, dead):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", ats=ats)
    monkeypatch.setattr(harvest, "fetch_whole_board",
                        lambda comp: http.fetch_failed("Acme", "HTTP 404"))
    harvest.harvest_board(c, db, delay=0)
    harvest.harvest_board(store.get_company(conn, c["id"]), db, delay=0)
    row = store.get_company(conn, c["id"])
    assert (row["miss_reason"], row["active"]) == (
        ("board-dead:greenhouse", 0) if dead else ("fetch-error:harvest", 1))


@pytest.mark.parametrize("ats, dead", [("greenhouse", True),
                                       ("workday", False)])
def test_crawl_buries_a_second_definitive_404(db, local_track, ats, dead):
    from src.crawl import runner
    c = _company(db, "Acme", ats=ats)
    snap = {"fetch_errors": 1, "incomplete": True, "capped": False,
            "capped_total": None, "last_error": "Acme: HTTP 404"}
    for _ in range(2):
        spec = {"company": store.get_company(db, c["id"]), "name": "Acme",
                "platform": ats}
        runner._gate_sources(db, local_track, [spec], [([], None, snap)],
                             commit=True)
    row = store.get_company(db, c["id"])
    assert (row["miss_reason"], row["active"]) == (
        ("board-dead:greenhouse", 0) if dead else (None, 1))


def test_one_board_never_inherits_another_board_fetch_errors(tmp_path,
                                                             monkeypatch):
    """The count is per-thread, and a worker thread runs one board."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    monkeypatch.setattr(harvest, "fetch_whole_board",
                        lambda comp: http.fetch_failed("x", "boom"))
    assert harvest.harvest_board(c, db, delay=0)["fetch_errors"] == 1
    assert harvest.harvest_board(c, db, delay=0)["fetch_errors"] == 1


# ── snapshot completeness ───────────────────────────────────────────────────

def test_harvest_board_partial_fetch_stores_rows_but_closes_nothing(
        tmp_path, monkeypatch):
    """One page of the board failed while the rest arrived (an HTML error
    page under HTTP 200 closed 1037 Stryker rows on 2026-09-15). The rows
    that arrived are stored; the ones that did not prove nothing, so
    nothing closes."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    store.upsert_job(conn, {"job_id": "gh_acme_old", "company_id": c["id"],
                            "title": "Still open", "track": "local"})

    def partial(comp):
        http.fetch_failed("workday acme p10",
                          "Expecting value: line 1 column 1 (char 0)")
        return [_job(1), _job(2)]

    monkeypatch.setattr(harvest, "fetch_whole_board", partial)
    stats = harvest.harvest_board(c, db, delay=0)
    assert stats["err"] is None
    assert stats["fetch_errors"] == 1 and stats["incomplete"] is True
    assert stats["capped"] is False
    assert stats["closed"] == 0 and stats["reopened"] == 0
    assert stats["new"] == 2, "the rows that DID arrive are still stored"
    row = conn.execute(
        "SELECT status FROM jobs WHERE job_id='gh_acme_old'").fetchone()
    assert row["status"] == "open"


def test_harvest_board_capped_snapshot_never_closes(tmp_path, monkeypatch):
    """A board that reports itself capped (net.http.note_capped) closes
    NOTHING here, however many passes running a row has been absent --
    only ops.check_closed_jobs's URL probe may close a capped board's
    vanished rows (see store.sync_job_statuses's `capped` paragraph).

    Before 2026-09-18 a board-native row closed on its SECOND consecutive
    miss under a cap; replaced outright once a live audit found 25
    Workday/SmartRecruiters boards reading their full page budget on
    EVERY pass, which made "missed twice" no more trustworthy than
    "missed once" for those boards.
    """
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme")
    for jid in ("gh_acme_keep", "gh_acme_flaky"):
        store.upsert_job(conn, {"job_id": jid, "company_id": c["id"],
                                "title": "T"})
    conn.execute(
        "UPDATE jobs SET last_seen=?, first_seen=? WHERE company_id=?",
        ("2020-01-01T00:00:00", "2020-01-01T00:00:00", c["id"]))
    conn.commit()

    pass_n = {"i": 0}

    def capped_fetch(comp):
        http.note_capped(50)             # server total >> what's returned
        pass_n["i"] += 1
        return [_job("flaky")] if pass_n["i"] == 1 else [_job("new")]

    monkeypatch.setattr(harvest, "fetch_whole_board", capped_fetch)

    # Pass 1: the board hands back "flaky" only. "keep" -- absent -- stays
    # open: a capped snapshot's window proves nothing about a row it
    # didn't include.
    s1 = harvest.harvest_board(c, db, delay=0, now=harvest.datetime(2026, 1, 1))
    assert s1["capped"] is True and s1["capped_total"] == 50
    assert s1["closed"] == 0
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='gh_acme_keep'"
    ).fetchone()["status"] == "open"

    # Pass 2: neither "flaky" nor "keep" is on the board this time (a
    # third row, "new", keeps the snapshot non-empty) -- STILL capped, so
    # neither one closes even though "keep" has now missed twice running.
    c2 = store.get_company(conn, c["id"])
    s2 = harvest.harvest_board(c2, db, delay=0, now=harvest.datetime(2026, 1, 2))
    assert s2["capped"] is True
    rows = {r["job_id"]: r["status"] for r in conn.execute(
        "SELECT job_id, status FROM jobs WHERE company_id=?", (c["id"],))}
    assert rows["gh_acme_flaky"] == "open"
    assert rows["gh_acme_keep"] == "open", "still capped: two misses closes nothing now"
    assert s2["closed"] == 0


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

    def pull(company):
        pulled.append(company["name"])
        if company["name"] == "B":
            raise RuntimeError("down")

    s = harvest.run(db_path=db, max_workers=2,
                    board_fn=make_board_fn(before=pull, fetched=3, new=2,
                                           hydrated=1, secs=0.1))
    assert sorted(pulled) == ["A", "B"]
    assert (s["boards"], s["ok"], s["err"], s["stalled"]) == (3 - 1, 1, 1, 0)
    assert s["fetched"] == 3 and s["new"] == 2


def test_run_abandons_a_stalled_board(tmp_path):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _company(conn, "Wedged")
    release = threading.Event()
    # Blocks, and never calls progress() at all.
    board_fn = make_board_fn(before=lambda company: release.wait(5))
    s = harvest.run(db_path=db, max_workers=1, board_fn=board_fn,
                    stall_s=0.0, poll_s=0.1)
    release.set()
    assert s["stalled"] == 1 and s["ok"] == 0


def test_dead_board_status_line_names_the_last_error_and_warns(
        tmp_path, capsys):
    """The soft-failure status line used to only count fetch errors; it now
    names the LAST one (net.http.snapshot_info's last_error), and the line
    is prefixed "[!] " so session_log logs it at WARNING
    (src/session_log.py::_level_for) instead of an ordinary INFO tally."""
    import logging

    from src.session_log import _level_for
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _company(conn, "Acme")
    board_fn = make_board_fn(
        secs=1.0, fetch_errors=1,
        last_error="GET https://x.test/sitemap.xml: 403")
    s = harvest.run(db_path=db, max_workers=1, board_fn=board_fn,
                    triage=False)
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "Acme" in l)

    assert line.lstrip().startswith("[!]")
    assert ("no jobs - 1 fetch error(s): "
            "GET https://x.test/sitemap.xml: 403") in line
    assert _level_for(line, err=False) == logging.WARNING
    # Named and warned about, but still an "ok"/"dead" board, not "failed":
    # `err` staying None throughout is what keeps it out of the exception
    # count.
    assert (s["ok"], s["err"], s["dead"]) == (1, 0, 1)


def test_harvest_summary_names_boards_whose_name_is_just_their_own_slug(
        tmp_path, capsys):
    """Slug-named rows (names.SLUG_NAME_SOURCE) still called by their own
    slug/tenant are listed largest board first; a renamed one, or one a
    person or page named, is not."""
    from src.match.names import SLUG_NAME_SOURCE
    db = tmp_path / "s.db"
    conn = store.connect(db)
    store.upsert_company(conn, {"name": "Xyz", "ats": "workday",
                                "wd_tenant": "xyz", "wd_pod": 5,
                                "wd_site": "Ext", "total_job_count": 5,
                                "source": SLUG_NAME_SOURCE})
    store.upsert_company(conn, {"name": "Bigco", "ats": "lever",
                                "slug": "bigco", "total_job_count": 50,
                                "source": SLUG_NAME_SOURCE})
    store.upsert_company(conn, {"name": "Acme Health", "ats": "lever",
                                "slug": "acme-careers",
                                "source": SLUG_NAME_SOURCE})
    store.upsert_company(conn, {"name": "Solo", "ats": "lever",
                                "slug": "solo", "source": "manual"})
    harvest.run(db_path=db, max_workers=2, board_fn=make_board_fn(),
                triage=False)
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines()
               if "named after their own slug" in l)
    assert line.strip() == ("2 board(s) still named after their own "
                            "slug/tenant, largest first: Bigco, Xyz")


def test_harvest_summary_line_is_bare_when_nothing_is_flagged(
        tmp_path, capsys):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    store.upsert_company(conn, {"name": "Acme Health", "ats": "lever",
                                "slug": "acme-careers"})
    harvest.run(db_path=db, max_workers=1, board_fn=make_board_fn(),
                triage=False)
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines()
               if "named after their own slug" in l)
    assert line.strip() == "0 board(s) still named after their own slug/tenant"


# ── the crawl gets the same completeness guard ──────────────────────────────

@pytest.mark.parametrize("snapshot, closed", [
    (None, 1),                      # complete: an absent row closes at once
    ({"incomplete": True}, 0),      # a page failed: nothing closes
    ({"capped": True}, 0),          # capped: never closes (see store.jobs)
])
def test_gate_company_board_guards_the_sync_by_snapshot(db, local_track,
                                                        snapshot, closed):
    """The crawl reads fetch_all's per-source snapshot and gives
    store.sync_job_statuses the same guard the harvester does."""
    from src.crawl import runner
    c = _company(db, "Acme")
    store.upsert_job(db, {"job_id": "gh_acme_old", "company_id": c["id"],
                          "title": "Still open", "track": local_track["track"]})
    *_, n_reopened, n_closed = runner._gate_company_board(
        db, local_track, c, [_job(1)], commit=True, snapshot=snapshot)
    assert (n_reopened, n_closed) == (0, closed)
    assert db.execute("SELECT status FROM jobs WHERE job_id='gh_acme_old'"
                      ).fetchone()["status"] == ("closed" if closed else "open")


# ── the crawl adopts harvested rows ─────────────────────────────────────────

def test_runner_treats_harvested_rows_as_fresh(tmp_path, monkeypatch):
    """The end-to-end contract: a harvested row (no track) is scored by the
    next crawl, and the crawl reuses the stored description instead of
    re-hydrating."""
    from src import config
    from src.crawl import runner
    from src.ops import maintenance as ops
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
              "description": "", "ats": "greenhouse"}]
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
    monkeypatch.setattr(gates, "is_technical_role",
                        lambda title, tt: True)

    class R:
        score = 0.7

        def as_columns(self):
            return {"resume_fit_score": 0.7, "fit_reason": "ok"}
    monkeypatch.setattr(ops, "score_resume_fit",
                        lambda title, description="", *, location="",
                        max_tokens=300: R())
    monkeypatch.setattr(ops, "self_heal_unscored", lambda *a, **k: 0)
    runner.run_track(t, fit=True, commit=True, send=False, verify=False,
                     websearch=False)
    conn = store.connect(db)
    row = conn.execute("SELECT * FROM jobs WHERE job_id='gh_acme_1'").fetchone()
    assert row["resume_fit_score"] == 0.7
    assert t["track"] in store.track_set(row["track"])
    assert not hydrated, "stored description should have been reused"


# ── the pass runs verify + the closed-URL probe, before the digest ──────────

class TestHarvestPassRunsVerifyAndClosedProbe:
    """After triage.run(), _triage now deep-verifies every roster track
    (bounded by each track's own verify_top/verify_floor) and probes
    tracked open rows stale 7+ days for closure -- both BEFORE the
    per-track digest rewrite -- unless the Claude API has no key or this
    run's breaker has already tripped, in which case verify is skipped
    with one printed note (the closed-URL probe needs no Claude call, so
    it always runs)."""

    def _wire(self, monkeypatch, tmp_path, verify_top=5):
        db = tmp_path / "s.db"
        conn = store.connect(db)
        _company(conn, "A")
        order = []
        monkeypatch.setattr("src.crawl.triage.run",
                            lambda **kw: order.append("triage")
                            or {"pending": 0})
        monkeypatch.setattr(
            "src.crawl.triage.roster_tracks",
            lambda: [{"track": "local-tech", "verify_top": verify_top}])
        monkeypatch.setattr(harvest, "verify_top",
                            lambda **kw: order.append(("verify", kw)))
        monkeypatch.setattr(harvest, "check_closed_jobs",
                            lambda **kw: order.append(("closed", kw)))
        monkeypatch.setattr(harvest, "rewrite_digest",
                            lambda conn, t, **kw: order.append("digest"))
        return db, order

    def _kinds(self, order):
        return [o[0] if isinstance(o, tuple) else o for o in order]

    def test_order_is_triage_then_verify_then_closed_then_digest(
            self, tmp_path, monkeypatch):
        db, order = self._wire(monkeypatch, tmp_path)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(claude_api, "_FATAL_MSG", None)
        harvest.run(db_path=db, max_workers=2, board_fn=make_board_fn())
        assert self._kinds(order) == ["triage", "verify", "closed", "digest"]
        verify_kw = next(o[1] for o in order if isinstance(o, tuple)
                         and o[0] == "verify")
        assert verify_kw["t"]["track"] == "local-tech"
        closed_kw = next(o[1] for o in order if isinstance(o, tuple)
                         and o[0] == "closed")
        assert (closed_kw["limit"], closed_kw["stale_days"]) == (
            harvest.CLOSED_PROBE_LIMIT, harvest.CLOSED_PROBE_STALE_DAYS)

    @pytest.mark.parametrize("key, fatal, why", [
        ("YOUR_ANTHROPIC_API_KEY_HERE", None, "no ANTHROPIC_API_KEY"),
        ("test-key", "HTTP 400: 'credit balance'", "credit balance"),
    ])
    def test_verify_is_one_skip_line_when_the_api_cannot_answer(
            self, tmp_path, monkeypatch, capsys, key, fatal, why):
        db, order = self._wire(monkeypatch, tmp_path)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", key)
        monkeypatch.setattr(claude_api, "_FATAL_MSG", fatal)
        harvest.run(db_path=db, max_workers=2, board_fn=make_board_fn())
        out = capsys.readouterr().out
        assert out.count("verify skipped") == 1 and why in out
        assert self._kinds(order) == ["triage", "closed", "digest"]

    def test_both_are_skipped_when_triage_is_off(self, tmp_path, monkeypatch):
        db, order = self._wire(monkeypatch, tmp_path)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(claude_api, "_FATAL_MSG", None)
        s = harvest.run(db_path=db, max_workers=2, board_fn=make_board_fn(),
                        triage=False)
        assert "triage" not in s
        assert order == []
