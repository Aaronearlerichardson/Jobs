"""The SQLite store: schema, company/job upserts, the open/closed
lifecycle, dispositions, crawl dormancy, and track membership."""

from datetime import datetime, timedelta

import core.store as store


class TestSchema:
    def test_companies_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(companies)")}
        assert {"mission_score", "tags"} <= cols

    def test_jobs_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
        assert {"resume_fit_score", "track", "remote_eligible",
                "anchor_signal"} <= cols

    def test_migrations_are_idempotent(self, db):
        # A second connect() over the same schema must not raise.
        store._ensure_columns(db)


class TestCompanies:
    def test_tags_merge_on_upsert(self, db):
        store.upsert_company(db, {"name": "X", "ats": "greenhouse",
                                  "slug": "x", "tags": "sweep"})
        store.upsert_company(db, {"name": "X", "tags": "local"})
        assert store.get_companies(db, tag="sweep")[0]["tags"] == "local,sweep"

    def test_watch_tag_roundtrip(self, db):
        store.upsert_company(db, {"name": "W", "ats": "greenhouse", "slug": "w"})
        assert store.set_company_tag(db, "w", "watch") == "watch"   # case-insensitive
        row = store.get_companies(db, active_only=False)[0]
        import scrapers.ops as ops
        assert ops._is_watched(row) and ops._whole_board(row)
        assert store.set_company_tag(db, "W", "watch", add=False) == ""
        assert store.set_company_tag(db, "Nope", "watch") is None


class TestDormancy:
    """181 of 300 active companies had never produced a single job yet were
    fetched on every crawl, and three off-mission boards produced hundreds
    of rows nobody would apply to. Dormancy demotes both to a weekly
    cadence instead of asking the user to deactivate boards by hand."""

    def _state(self, db, company):
        return db.execute(
            "SELECT crawl_state, empty_streak, next_crawl_at, last_nonempty_at "
            "FROM companies WHERE id=?", (company,)).fetchone()

    def _backdate(self, db, company, days=1):
        """Pretend the last crawl was `days` ago -- the streak only grows once
        per calendar day, so nothing else can fake consecutive empty days."""
        db.execute("UPDATE companies SET last_crawled_at=? WHERE id=?",
                   ((datetime.now() - timedelta(days=days)).isoformat(),
                    company))
        db.commit()

    def _empty_days(self, db, company, n, **kw):
        state = None
        for _ in range(n):
            state = store.record_crawl_outcome(db, company, 0, **kw)
            self._backdate(db, company)
        return state

    def test_streak_grows_once_per_day(self, db, company):
        assert self._empty_days(db, company, 3) == "active"
        assert self._state(db, company)["empty_streak"] == 3

    def test_dormant_after_the_configured_run_of_empty_days(self, db, company):
        assert self._empty_days(db, company, 4) == "dormant"
        assert self._state(db, company)["next_crawl_at"] > datetime.now().isoformat()

    def test_same_day_repeats_count_once(self, db, company):
        # Several tracks (and a re-run after a crash) hit the same board the
        # same afternoon; that is one empty day, not three.
        for _ in range(3):
            store.record_crawl_outcome(db, company, 0)
        assert self._state(db, company)["empty_streak"] == 1

    def test_fetch_error_is_neutral(self, db, company):
        self._empty_days(db, company, 3)
        assert store.record_crawl_outcome(
            db, company, 0, err=RuntimeError("503")) == "active"
        assert self._state(db, company)["empty_streak"] == 3

    def test_a_short_dormant_after_still_needs_distinct_days(self, db, company):
        assert store.record_crawl_outcome(db, company, 0, dormant_after=2) == "active"
        assert store.record_crawl_outcome(db, company, 0, dormant_after=2) == "active"
        self._backdate(db, company)
        assert store.record_crawl_outcome(db, company, 0, dormant_after=2) == "dormant"

    def test_jobs_reset_the_streak_and_wake_a_dormant_row(self, db, company):
        self._empty_days(db, company, 4)
        assert store.record_crawl_outcome(db, company, 7) == "active"
        row = self._state(db, company)
        assert row["empty_streak"] == 0 and row["next_crawl_at"] is None
        assert row["last_nonempty_at"] is not None

    def test_watched_company_never_sleeps(self, db, company):
        store.set_company_tag(db, "Acme", "watch")
        assert self._empty_days(db, company, 6) == "active"

    def test_offmission_volume_sleeps_a_busy_board(self, db, company, add_job):
        # The 663-jobs-best-fit-0.15 pattern: productive every run, and
        # productive of nothing worth reading.
        for i in range(store._OFFMISSION_MIN_JOBS):
            add_job(f"gh_acme_{i}", fit=0.03)
        assert store.record_crawl_outcome(db, company, 663) == "dormant"

    def test_one_good_fit_keeps_a_busy_board_awake(self, db, company, add_job):
        for i in range(store._OFFMISSION_MIN_JOBS):
            add_job(f"gh_acme_{i}", fit=0.03)
        add_job("gh_acme_star", fit=0.71)
        assert store.record_crawl_outcome(db, company, 663) == "active"

    def test_unscored_jobs_are_not_evidence(self, db, company, add_job):
        # A board nobody has scored yet has a NULL best fit; that is missing
        # data, not a verdict.
        for i in range(store._OFFMISSION_MIN_JOBS):
            add_job(f"gh_acme_{i}")
        assert store.record_crawl_outcome(db, company, 40) == "active"

    def test_watched_company_survives_the_volume_rule(self, db, company, add_job):
        store.set_company_tag(db, "Acme", "watch")
        for i in range(store._OFFMISSION_MIN_JOBS):
            add_job(f"gh_acme_{i}", fit=0.03)
        assert store.record_crawl_outcome(db, company, 663) == "active"


class TestCrawlableSelection:
    def _sleeping(self, db, name, wake):
        cid = store.upsert_company(db, {"name": name, "ats": "greenhouse",
                                        "slug": name.lower()})
        db.execute("UPDATE companies SET crawl_state='dormant', "
                   "next_crawl_at=? WHERE id=?", (wake, cid))
        db.commit()
        return cid

    def test_null_state_reads_as_active(self, db, company):
        assert [c["id"] for c in store.crawlable_companies(db)] == [company]

    def test_dormant_row_waits_for_its_slot(self, db, company):
        self._sleeping(db, "Sleepy",
                       (datetime.now() + timedelta(days=6)).isoformat())
        assert {c["name"] for c in store.crawlable_companies(db)} == {"Acme"}

    def test_dormant_row_returns_when_due(self, db, company):
        self._sleeping(db, "Sleepy",
                       (datetime.now() - timedelta(minutes=1)).isoformat())
        assert {c["name"] for c in store.crawlable_companies(db)} == {"Acme",
                                                                      "Sleepy"}

    def test_off_never_comes_back_on_its_own(self, db, company):
        db.execute("UPDATE companies SET crawl_state='off' WHERE id=?", (company,))
        db.commit()
        assert store.crawlable_companies(db) == []

    def test_inactive_row_stays_out(self, db, company):
        db.execute("UPDATE companies SET active=0 WHERE id=?", (company,))
        db.commit()
        assert store.crawlable_companies(db) == []

    def test_reactivation_wakes_it_now(self, db, company):
        cid = self._sleeping(db, "Sleepy",
                             (datetime.now() + timedelta(days=6)).isoformat())
        store.reactivate_company(db, cid)
        row = db.execute("SELECT crawl_state, empty_streak, next_crawl_at "
                         "FROM companies WHERE id=?", (cid,)).fetchone()
        assert (row["crawl_state"], row["empty_streak"],
                row["next_crawl_at"]) == ("active", 0, None)
        assert len(store.crawlable_companies(db)) == 2


class TestDedupe:
    def test_mark_seen_dedupes(self, db):
        store.mark_seen(db, {"id": "j1", "company": "X", "title": "t",
                             "url": "u", "location": "Remote"},
                        track="remote-neural")
        assert not store.is_new(db, "j1")
        assert store.is_new(db, "j2")


class TestClosedLifecycle:
    """Board snapshots decide open/closed. Board-native rows match by exact
    id; externally-sourced rows (foreign id namespace) also match on URL or
    title, and get a grace period before a snapshot may close them."""

    def _seed(self, add_job):
        add_job("gh_acme_1", "Data Engineer", 0.9)
        add_job("gh_acme_2", "ML Engineer", 0.8)
        add_job("linkedin_aaa", "Platform Engineer", 0.7,
                url="https://linkedin.com/jobs/3")
        return [{"id": "gh_acme_2", "title": "ML Engineer",
                 "url": "https://acme.io/gh_acme_2"}]

    def test_vanished_job_closes(self, db, company, add_job, status_of):
        snap = self._seed(add_job)
        store.sync_job_statuses(db, company, snap, track="local-tech")
        assert status_of("gh_acme_1")["status"] == "closed"
        assert status_of("gh_acme_1")["closed_at"] is not None
        assert status_of("gh_acme_2")["status"] == "open"

    def test_fresh_external_row_is_grace_protected(self, db, company, add_job,
                                                   status_of):
        snap = self._seed(add_job)
        store.sync_job_statuses(db, company, snap, track="local-tech")
        assert status_of("linkedin_aaa")["status"] == "open"

    def test_stale_unmatched_external_row_closes(self, db, company, add_job,
                                                 status_of):
        snap = self._seed(add_job)
        db.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00', "
                   "last_seen='2020-01-01T00:00:00' WHERE job_id='linkedin_aaa'")
        db.commit()
        store.sync_job_statuses(db, company, snap, track="local-tech")
        assert status_of("linkedin_aaa")["status"] == "closed"

    def test_reappearance_reopens(self, db, company, add_job, status_of):
        snap = self._seed(add_job)
        store.sync_job_statuses(db, company, snap, track="local-tech")
        store.sync_job_statuses(db, company, snap + [
            {"id": "gh_acme_1", "title": "Data Engineer",
             "url": "https://acme.io/gh_acme_1"}], track="local-tech")
        assert status_of("gh_acme_1")["status"] == "open"
        assert status_of("gh_acme_1")["closed_at"] is None

    def test_reingest_reopens(self, db, company, add_job, status_of):
        add_job("gh_acme_1")
        store.set_job_status(db, "gh_acme_1", "closed")
        add_job("gh_acme_1")                       # re-upsert
        assert status_of("gh_acme_1")["status"] == "open"
        assert status_of("gh_acme_1")["closed_at"] is None

    def test_touch_job_reopens_and_refreshes(self, db, add_job, status_of):
        add_job("gh_acme_1")
        store.set_job_status(db, "gh_acme_1", "closed")
        store.touch_job(db, "gh_acme_1")
        assert status_of("gh_acme_1")["status"] == "open"
        last = db.execute("SELECT last_seen FROM jobs WHERE job_id='gh_acme_1'"
                          ).fetchone()[0]
        assert last > "2020-01-02"

    def test_empty_snapshot_never_closes(self, db, company, add_job):
        add_job("gh_acme_1")
        # Fetchers soft-fail to [] — that must never read as "all closed".
        assert store.sync_job_statuses(db, company, [], track="local-tech") == (0, 0)

    def test_recycled_title_does_not_shield_dead_req(self, db, company, add_job,
                                                     status_of):
        # The repost pattern: a live posting must not keep a dead
        # board-native req of the same title open.
        add_job("gh_acme_old", "Algorithm Engineer")
        add_job("gh_acme_new", "Algorithm Engineer")
        add_job("linkedin_bbb", "Algorithm Engineer",
                url="https://linkedin.com/jobs/9")
        store.sync_job_statuses(db, company, [
            {"id": "gh_acme_new", "title": "Algorithm Engineer",
             "url": "https://acme.io/gh_acme_new"}], track="local-tech")
        assert status_of("gh_acme_old")["status"] == "closed"
        assert status_of("gh_acme_new")["status"] == "open"
        assert status_of("linkedin_bbb")["status"] == "open"   # external: title match


class TestRanking:
    def test_closed_excluded_but_readmittable(self, db, company, add_job):
        add_job("gh_acme_1", fit=0.9)
        add_job("gh_acme_2", fit=0.8)
        store.set_job_status(db, "gh_acme_1", "closed")
        ids = [r["job_id"] for r in store.ranked_jobs(db, track="local-tech")]
        assert ids == ["gh_acme_2"]
        ids = [r["job_id"] for r in store.ranked_jobs(db, track="local-tech",
                                                      include_closed=True)]
        assert "gh_acme_1" in ids

    def test_combined_score_punishes_imbalance(self):
        assert store.combined_score(0.9, 0.2) < store.combined_score(0.5, 0.5)
        assert store.combined_score(0.5, None) is None


class TestDispositions:
    def _seed(self, add_job):
        add_job("gh_acme_100", "Algorithm Engineer", 0.9)
        add_job("gh_acme_200", "TPM Seat", 0.8)
        add_job("gh_acme_300", "Data Engineer", 0.7)

    def test_mark_by_exact_id(self, db, add_job):
        self._seed(add_job)
        row, err = store.set_disposition(db, "gh_acme_200", "dismissed",
                                         note="wrong archetype")
        assert err is None and row["job_id"] == "gh_acme_200"

    def test_mark_by_unique_fragment(self, db, add_job):
        self._seed(add_job)
        row, err = store.set_disposition(db, "300", "applied")
        assert err is None and row["job_id"] == "gh_acme_300"

    def test_mark_by_url(self, db, add_job):
        self._seed(add_job)
        row, err = store.set_disposition(db, "https://acme.io/gh_acme_100/", "saved")
        assert err is None and row["job_id"] == "gh_acme_100"

    def test_ambiguous_fragment_errors(self, db, add_job):
        self._seed(add_job)
        _, err = store.set_disposition(db, "gh_acme", "applied")
        assert err and "ambiguous" in err

    def test_unknown_disposition_errors(self, db, add_job):
        self._seed(add_job)
        _, err = store.set_disposition(db, "gh_acme_100", "bogus")
        assert err

    def test_only_saved_stays_in_ranking(self, db, add_job):
        self._seed(add_job)
        store.set_disposition(db, "gh_acme_200", "dismissed")
        store.set_disposition(db, "gh_acme_300", "applied")
        store.set_disposition(db, "gh_acme_100", "saved")
        ids = [r["job_id"] for r in store.ranked_jobs(db, track="local-tech")]
        assert ids == ["gh_acme_100"]
        assert len(store.get_pipeline(db)) == 3

    def test_clear_removes_from_pipeline(self, db, add_job):
        self._seed(add_job)
        store.set_disposition(db, "gh_acme_100", "saved")
        store.set_disposition(db, "gh_acme_200", "applied")
        _, err = store.set_disposition(db, "gh_acme_100", "clear")
        assert err is None and len(store.get_pipeline(db)) == 1

    def test_dismissal_note_feeds_few_shot_block(self, db, add_job):
        self._seed(add_job)
        store.set_disposition(db, "gh_acme_200", "dismissed", note="wrong archetype")
        store.set_disposition(db, "gh_acme_300", "applied")
        from core.fit import disposition_examples_block
        block = disposition_examples_block(db, 3)
        assert 'PURSUED: "Data Engineer"' in block
        assert "wrong archetype" in block


class TestPipelineTracking:
    """Everything an application needs AFTER 'applied': when to chase it,
    who to chase, whether a referral carried it, why it ended, and which
    kind of application actually converts."""

    def _applied(self, db, add_job, job_id="p1", **kw):
        add_job(job_id, **kw)
        store.set_disposition(db, job_id, "applied")
        return job_id

    def _col(self, db, job_id, col):
        return db.execute(f"SELECT {col} FROM jobs WHERE job_id=?",
                          (job_id,)).fetchone()[col]

    # --- applied_at ---------------------------------------------------- #

    def test_applying_stamps_the_apply_date(self, db, add_job):
        self._applied(db, add_job)
        assert self._col(db, "p1", "applied_at")

    def test_a_decision_that_is_not_an_application_stamps_nothing(self, db,
                                                                  add_job):
        add_job("p1")
        store.set_disposition(db, "p1", "saved")
        assert self._col(db, "p1", "applied_at") is None

    def test_later_dispositions_keep_the_first_apply_date(self, db, add_job):
        # The whole point of a separate column: disposition_at moves with
        # every re-mark, so it cannot answer "how long has this been out?".
        self._applied(db, add_job)
        first = self._col(db, "p1", "applied_at")
        store.set_disposition(db, "p1", "interviewing")
        store.set_disposition(db, "p1", "rejected")
        store.set_disposition(db, "p1", "applied")
        assert self._col(db, "p1", "applied_at") == first
        assert self._col(db, "p1", "disposition_at") != first

    # --- update_pipeline_fields ---------------------------------------- #

    def test_tracking_fields_persist(self, db, add_job):
        self._applied(db, add_job)
        row, err = store.update_pipeline_fields(
            db, "p1", followup_at="2026-09-08", contact="Dana R",
            referral=True, outcome_reason="no-response")
        assert err is None
        assert row["followup_at"] == "2026-09-08"
        assert row["contact"] == "Dana R"
        assert row["referral"] == 1
        assert row["outcome_reason"] == "no-response"

    def test_a_blank_clears_rather_than_storing_an_empty_string(self, db,
                                                                add_job):
        self._applied(db, add_job)
        store.update_pipeline_fields(db, "p1", contact="Dana R")
        row, _ = store.update_pipeline_fields(db, "p1", contact="   ")
        assert row["contact"] is None

    def test_an_unlisted_column_is_refused_not_written(self, db, add_job):
        self._applied(db, add_job, fit=0.9)
        row, err = store.update_pipeline_fields(db, "p1", resume_fit_score=0.0)
        assert row is None and "resume_fit_score" in err
        assert self._col(db, "p1", "resume_fit_score") == 0.9

    def test_disposition_cannot_be_written_through_this_path(self, db, add_job):
        self._applied(db, add_job)
        _, err = store.update_pipeline_fields(db, "p1", disposition="dismissed")
        assert err and self._col(db, "p1", "disposition") == "applied"

    def test_an_outcome_outside_the_vocabulary_is_refused(self, db, add_job):
        self._applied(db, add_job)
        _, err = store.update_pipeline_fields(db, "p1", outcome_reason="ghosted")
        assert err and "outcome_reason" in err
        assert self._col(db, "p1", "outcome_reason") is None

    def test_unknown_job_errors(self, db):
        row, err = store.update_pipeline_fields(db, "nope", contact="Dana R")
        assert row is None and err

    # --- conversion_report --------------------------------------------- #

    def _applications(self, db, add_job):
        """Two mid-band onsite applications, one still interviewing and one
        rejected AFTER an interview; one high-band remote application that
        was never answered."""
        for jid, fit, geo in (("mid1", 0.41, "onsite"), ("mid2", 0.54, "onsite"),
                              ("hi1", 0.82, "remote")):
            add_job(jid, fit=fit, geo_mode=geo)
        store.set_disposition(db, "mid1", "interviewing")
        store.set_disposition(db, "mid2", "rejected")
        store.update_pipeline_fields(db, "mid2",
                                     outcome_reason="rejected-interview")
        store.set_disposition(db, "hi1", "rejected")
        store.update_pipeline_fields(db, "hi1", outcome_reason="no-response")

    def _report(self, db):
        return {(r["band"], r["geo_mode"]): r
                for r in store.conversion_report(db)}

    def test_report_slices_by_fit_band_and_geo(self, db, add_job):
        self._applications(db, add_job)
        rep = self._report(db)
        # Ordered low band to high, so the table reads as a ladder.
        assert list(rep) == [("mid", "onsite"), ("high", "remote")]
        assert rep[("mid", "onsite")]["applications"] == 2
        assert rep[("high", "remote")]["applications"] == 1

    def test_an_interview_still_counts_after_the_rejection(self, db, add_job):
        # Without this, every conversion number would decay to zero as
        # applications resolve — which is exactly the number that shows a
        # mid-fit onsite role converting better than a top-scored remote one.
        self._applications(db, add_job)
        rep = self._report(db)
        assert rep[("mid", "onsite")]["interviews"] == 2
        assert rep[("mid", "onsite")]["interview_rate"] == 1.0
        assert rep[("high", "remote")]["interviews"] == 0
        assert rep[("high", "remote")]["interview_rate"] == 0.0

    def test_only_applications_reach_the_report(self, db, add_job):
        add_job("s1", fit=0.9)
        add_job("d1", fit=0.9)
        store.set_disposition(db, "s1", "saved")
        store.set_disposition(db, "d1", "dismissed")
        assert store.conversion_report(db) == []

    def test_an_unscored_application_is_banded_separately(self, db, add_job):
        add_job("u1", geo_mode="onsite")           # no resume_fit_score
        store.set_disposition(db, "u1", "applied")
        assert list(self._report(db)) == [("unscored", "onsite")]

    # --- followups_due -------------------------------------------------- #

    def test_only_arrived_dates_on_live_applications_come_due(self, db,
                                                              add_job):
        for jid in ("due", "future", "dead"):
            self._applied(db, add_job, jid)
        store.update_pipeline_fields(db, "due", followup_at="2026-01-01")
        store.update_pipeline_fields(db, "future", followup_at="2099-01-01")
        store.update_pipeline_fields(db, "dead", followup_at="2026-01-01")
        store.set_disposition(db, "dead", "rejected")
        assert [r["job_id"] for r in
                store.followups_due(db, "2026-06-01")] == ["due"]

    def test_an_application_with_no_date_is_never_due(self, db, add_job):
        self._applied(db, add_job)
        assert store.followups_due(db, "2099-01-01") == []

    def test_interviewing_rows_are_still_chased(self, db, add_job):
        self._applied(db, add_job)
        store.set_disposition(db, "p1", "interviewing")
        store.update_pipeline_fields(db, "p1", followup_at="2026-01-01")
        assert len(store.followups_due(db, "2026-06-01")) == 1

    # --- the scorer's few-shot block ------------------------------------ #

    def test_outcome_reason_rides_along_to_the_few_shot_block(self, db,
                                                              add_job):
        add_job("p1", "Imaging Scientist")
        store.set_disposition(db, "p1", "rejected", note="no headcount")
        store.update_pipeline_fields(db, "p1",
                                     outcome_reason="rejected-interview")
        from core.fit import disposition_examples_block
        block = disposition_examples_block(db, 3)
        assert "Imaging Scientist" in block
        assert "rejected-interview" in block
        assert "no headcount" in block


class TestTrackMembership:
    """jobs.track is a comma-separated SET: one store, many tracks."""

    def test_second_track_adds_rather_than_steals(self, db, add_job):
        add_job("x1", "EEG Engineer", track="local-tech")
        add_job("x1", "EEG Engineer", track="remote-neural")
        row = db.execute("SELECT track FROM jobs WHERE job_id='x1'").fetchone()
        assert store.track_set(row["track"]) == {"local-tech", "remote-neural"}

    def test_both_tracks_see_the_shared_row(self, db, add_job):
        add_job("x1", track="local-tech")
        add_job("x1", track="remote-neural")
        assert len(store.ranked_jobs(db, track="local-tech")) == 1
        assert len(store.ranked_jobs(db, track="remote-neural")) == 1

    def test_unrelated_track_sees_nothing(self, db, add_job):
        add_job("x1", track="local-tech")
        assert store.ranked_jobs(db, track="nope") == []

    def test_track_set_tolerates_empty(self):
        assert store.track_set(None) == set()
        assert store.track_set("") == set()
        assert store.join_tracks(set()) is None

    def test_join_tracks_is_canonical(self):
        assert store.join_tracks({"b", "a"}) == "a,b"


class TestMisses:
    """Candidates that failed to become companies are kept, with a reason —
    and must stay invisible to every crawl path."""

    def test_miss_rows_never_reach_the_crawl(self, db):
        store.upsert_company(db, {"name": "Live Co", "ats": "lever",
                                  "slug": "live", "active": 1})
        before = len(store.get_companies(db, active_only=True))
        for name, reason in [("Advarra", "ats-unsupported:ukg"),
                             ("Chiesi USA", "no-local-jobs"),
                             ("Cognito Therapeutics", "no-local-jobs")]:
            assert store.record_miss(db, name, reason, ats="greenhouse")
        assert len(store.get_companies(db, active_only=True)) == before
        assert len(store.get_companies(db, active_only=False)) == before + 3

    def test_a_miss_never_deactivates_a_working_company(self, db):
        store.upsert_company(db, {"name": "Live Co", "ats": "lever",
                                  "slug": "live", "active": 1})
        assert store.record_miss(db, "Live Co", "fetch-error:ReadTimeout") is False
        assert store.get_companies(db, active_only=True)[0]["name"] == "Live Co"

    def test_resolving_a_miss_clears_the_reason(self, db):
        store.record_miss(db, "Liquidia", "ats-unsupported:hibob")
        store.upsert_company(db, {"name": "Liquidia", "ats": "hibob",
                                  "slug": "liquidia", "active": 1})
        row = store.get_companies(db, active_only=True)[0]
        assert row["miss_reason"] is None and row["miss_at"] is None

    def test_counts_aggregate_on_the_family_not_the_qualifier(self, db):
        store.record_miss(db, "a", "ats-unsupported:ukg")
        store.record_miss(db, "b", "ats-unsupported:taleo")
        store.record_miss(db, "c", "no-board-found")
        assert store.miss_counts(db) == [("ats-unsupported", 2),
                                         ("no-board-found", 1)]

    def test_created_at_survives_a_reprobe_that_moves_last_probed(self, db):
        store.upsert_company(db, {"name": "X", "ats": "lever", "slug": "x"})
        row = db.execute("SELECT created_at, last_probed FROM companies").fetchone()
        store.upsert_company(db, {"name": "X", "mission_score": 0.5})
        after = db.execute("SELECT created_at, last_probed FROM companies").fetchone()
        assert after["created_at"] == row["created_at"]
        assert after["last_probed"] >= row["last_probed"]
        assert store.roster_growth(db, days=7) == 1


class TestUpsertJobUrlRekey:
    """A posting that arrives under a NEW job_id scheme (company ats/tenant
    change, fetcher id-format change) must re-key its existing row, not
    duplicate it — the 2026-08-28 store held 17 URL pairs like Duke's
    sf__<slug> -> sf_<tenant>_<num> and Keebler's custom_* -> rippling_*,
    each double-ranking and double-spending deep-verify."""

    _URL = "https://acme.io/jobs/42"

    def _job(self, job_id, title="Medical Lab Scientist"):
        return {"job_id": job_id, "company_name": "Acme", "title": title,
                "url": self._URL, "location": "Durham, NC",
                "track": "local-tech"}

    def test_same_url_and_title_rekeys_the_existing_row(self, db):
        assert store.upsert_job(db, self._job("sf__old-slug")) is True
        db.execute("UPDATE jobs SET disposition='saved' WHERE job_id='sf__old-slug'")
        first_seen = db.execute("SELECT first_seen FROM jobs").fetchone()[0]

        assert store.upsert_job(db, self._job("sf_tenant_123")) is False
        rows = db.execute("SELECT job_id, disposition, first_seen FROM jobs "
                          "WHERE url=?", (self._URL,)).fetchall()
        assert len(rows) == 1, "the re-ingest must not duplicate the row"
        assert rows[0]["job_id"] == "sf_tenant_123"
        assert rows[0]["disposition"] == "saved", \
            "the user's decision must survive the re-key"
        assert rows[0]["first_seen"] == first_seen

    def test_different_title_on_a_shared_url_stays_a_separate_row(self, db):
        # Some custom boards give several distinct postings one landing URL.
        store.upsert_job(db, self._job("c_1", title="Data Engineer"))
        store.upsert_job(db, self._job("c_2", title="Research Scientist"))
        n = db.execute("SELECT COUNT(*) FROM jobs WHERE url=?",
                       (self._URL,)).fetchone()[0]
        assert n == 2


class TestDedupCompaniesRenamesJobs:
    """jobs.company_name is the grouping/display key, so a company merge
    has to rename the moved rows too — the 2026-09-01 dedup merged "Red
    Hat" into "Red Hat (IBM subsidiary, RTP HQ)" and the ranking kept
    showing both names."""

    def _wd(self, name, **extra):
        return {"name": name, "ats": "workday", "wd_tenant": "redhat",
                "wd_pod": 5, "wd_site": "jobs", **extra}

    def test_merged_rows_take_the_kept_company_name(self, db):
        keep = store.upsert_company(db, self._wd("Red Hat (IBM subsidiary)",
                                                 mission_tier="other"))
        dup = store.upsert_company(db, self._wd("Red Hat"))
        store.upsert_job(db, {"job_id": "wd_redhat_1", "company_id": dup,
                              "company_name": "Red Hat", "title": "SWE",
                              "url": "https://r/1"})
        store.upsert_job(db, {"job_id": "wd_redhat_2", "company_id": keep,
                              "company_name": "Red Hat (IBM subsidiary, RTP HQ)",
                              "title": "SRE", "url": "https://r/2"})
        assert store.dedup_companies(db) == 1
        names = {r[0] for r in db.execute("SELECT company_name FROM jobs")}
        assert names == {"Red Hat (IBM subsidiary)"}
        ids = {r[0] for r in db.execute("SELECT company_id FROM jobs")}
        assert ids == {keep}


class TestCompanyByBoard:
    """Discovery asks the roster whether a freshly resolved BOARD is already
    tracked before inserting the name it resolved from, so "SAS" cannot land
    beside "SAS Institute" on the same iCIMS slug (three such re-adds on
    2026-09-01: SAS, Veeva Systems, NVIDIA AI)."""

    def test_finds_the_same_board_under_another_name(self, db):
        store.upsert_company(db, {"name": "SAS Institute", "ats": "icims",
                                  "slug": "globalcareers-sas"})
        hit = store.company_by_board(db, {"name": "SAS", "ats": "icims",
                                          "slug": "globalcareers-sas"})
        assert hit and hit["name"] == "SAS Institute"

    def test_workday_triple_is_the_key(self, db):
        store.upsert_company(db, {"name": "NVIDIA", "ats": "workday",
                                  "wd_tenant": "nvidia", "wd_pod": 5,
                                  "wd_site": "NVIDIAExternalCareerSite"})
        same = {"ats": "workday", "wd_tenant": "nvidia", "wd_pod": 5,
                "wd_site": "NVIDIAExternalCareerSite"}
        other_site = dict(same, wd_site="Internal")
        assert store.company_by_board(db, same)["name"] == "NVIDIA"
        assert store.company_by_board(db, other_site) is None

    def test_no_board_means_no_match(self, db):
        store.upsert_company(db, {"name": "Stub", "active": 0})
        assert store.company_by_board(db, {"name": "Stub 2"}) is None


class TestDedupJobs:
    """Same company + same URL modulo query string + same title is one
    posting: iCIMS served SAS's postings as `?in_iframe=1` and
    `?hub=9&in_iframe=1` under two id namespaces (12 pairs, 2026-09-01),
    and upsert_job's exact-URL re-key could not see through the query."""

    _BASE = "https://careers-sas.icims.com/jobs/42453/software-developer/job"

    def _job(self, job_id, query, title="Software Developer", company_id=None):
        return {"job_id": job_id, "company_id": company_id,
                "company_name": "SAS", "title": title,
                "url": f"{self._BASE}?{query}", "location": "Cary, NC",
                "track": "local-tech"}

    def test_query_variants_collapse_and_keep_the_dispositioned_row(
            self, db, company):
        store.upsert_job(db, self._job("icims_careers-sas_42453",
                                       "in_iframe=1", company_id=company))
        store.upsert_job(db, self._job("icims_globalcareers-sas_42453",
                                       "hub=9&in_iframe=1", company_id=company))
        db.execute("UPDATE jobs SET disposition='applied' "
                   "WHERE job_id='icims_globalcareers-sas_42453'")
        assert store.dedup_jobs(db) == 1
        rows = db.execute("SELECT job_id, disposition FROM jobs").fetchall()
        assert [(r["job_id"], r["disposition"]) for r in rows] == \
            [("icims_globalcareers-sas_42453", "applied")]

    def test_earliest_row_wins_among_equals(self, db, company):
        store.upsert_job(db, self._job("icims_a_42453", "in_iframe=1",
                                       company_id=company))
        store.upsert_job(db, self._job("icims_b_42453", "hub=9&in_iframe=1",
                                       company_id=company))
        db.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00' "
                   "WHERE job_id='icims_b_42453'")
        assert store.dedup_jobs(db) == 1
        assert [r[0] for r in db.execute("SELECT job_id FROM jobs")] ==             ["icims_b_42453"]

    def test_distinct_titles_on_one_url_stay_separate(self, db, company):
        store.upsert_job(db, self._job("a", "x=1", title="Data Engineer",
                                       company_id=company))
        store.upsert_job(db, self._job("b", "x=2", title="Research Scientist",
                                       company_id=company))
        assert store.dedup_jobs(db) == 0

    def test_distinct_requisitions_on_a_shared_landing_url_stay_separate(
            self, db, company):
        # Greenhouse companies whose stored URL is one careers landing page
        # (…/careers?gh_jid=N): same title under two requisition numbers is
        # two postings (a second office, a re-opened req), not a duplicate.
        base = "https://butterflynetwork.com/careers"
        for jid, q in (("gh_butterflynetwork_76211920", "gh_jid=76211920"),
                       ("gh_butterflynetwork_76221680", "gh_jid=76221680")):
            store.upsert_job(db, {"job_id": jid, "company_id": company,
                                  "company_name": "Butterfly",
                                  "title": "Staff Engineer, Digital Verification",
                                  "url": f"{base}?{q}", "track": "local-tech"})
        assert store.dedup_jobs(db) == 0

    def test_unlinked_rows_are_left_alone(self, db):
        # No company_id: nothing to scope the merge by, so never touched.
        store.upsert_job(db, self._job("a", "x=1"))
        store.upsert_job(db, self._job("b", "x=2"))
        assert store.dedup_jobs(db) == 0
