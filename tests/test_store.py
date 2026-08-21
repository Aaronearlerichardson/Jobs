"""The SQLite store: schema, company/job upserts, the open/closed
lifecycle, dispositions, and track membership."""

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
