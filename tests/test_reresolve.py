"""Re-resolution of roster rows that died at resolution, and the one
resolver the interactive add paths now share.

144 rows carried a "no-board-found" miss and 36 a "board-dead" one, and that
bucket held some of the best-known local employers in the roster. Nothing
ever looked at them again: every add path resolved a name once and, on a
miss, only recorded why. `reresolve_misses` is the retry, and it hands its
hits to a person rather than to the crawler.

Offline: the resolver, the mission scorer and the board fetch are all
stubbed, exactly as the pasted-name tests stub them.
"""

import core.store as store
from core import tags
from discovery import local_sourcing, sniffer
from scrapers import ops


def _miss(db, name, reason, **fields):
    """One inactive roster row carrying `reason`."""
    store.record_miss(db, name, reason, **fields)


class TestReresolveSelection:
    """Which rows a bounded pass picks up. The families and the ordering are
    doctested on ops._reresolve_candidates; these are the cases a doctest
    cannot stage."""

    def test_an_active_row_is_never_retried(self, db):
        # A legacy row can carry both flags — record_miss refuses to write
        # one onto an active company, but nothing back-fills the old ones.
        store.upsert_company(db, {"name": "Guardant", "ats": "lever",
                                  "slug": "guardant", "active": 1})
        db.execute("UPDATE companies SET miss_reason='board-dead:lever' "
                   "WHERE name='Guardant'")
        assert ops._reresolve_candidates(db) == []

    def test_only_the_two_retryable_families_are_selected(self, db):
        for name, reason in [("Emmes", "no-board-found"),
                             ("Advarra", "board-dead:icims"),
                             ("Chiesi", "no-local-jobs"),
                             ("Locus", "ats-unsupported:ukg"),
                             ("Axoft", "fetch-error:ReadTimeout")]:
            _miss(db, name, reason)
        assert sorted(c["name"] for c in ops._reresolve_candidates(db)) == [
            "Advarra", "Emmes"]

    def test_days_filter_keeps_only_older_misses(self, db):
        _miss(db, "Emmes", "no-board-found")
        _miss(db, "Advarra", "no-board-found")
        db.execute("UPDATE companies SET miss_at='2020-01-01' "
                   "WHERE name='Advarra'")
        assert [c["name"] for c in ops._reresolve_candidates(db, days=7)] == [
            "Advarra"]

    def test_names_filter_narrows_rather_than_widens(self, db):
        _miss(db, "Emmes", "no-board-found")
        _miss(db, "Chiesi", "no-local-jobs")
        assert [c["name"] for c in ops._reresolve_candidates(
            db, names=["EMMES", "Chiesi"])] == ["Emmes"]

    def test_limit_takes_the_oldest_misses_first(self, db):
        for name in ("Emmes", "Advarra", "Axoft"):
            _miss(db, name, "no-board-found")
        db.execute("UPDATE companies SET miss_at='2020-01-01' "
                   "WHERE name='Axoft'")
        assert [c["name"] for c in ops._reresolve_candidates(db, limit=1)] == [
            "Axoft"]


class TestReresolveWrites:
    """What a pass writes. The contract with the roster review queue is
    narrow on purpose: board coordinates, a mission score, active=0 and the
    pending-review tag — nothing else."""

    T = {"db_path": None}

    def _wire(self, monkeypatch, result):
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda *a, **k: result)
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        monkeypatch.setattr("core.claude.score_company_mission",
                            lambda *a, **k: ("adjacent", 0.5, "stub"))

    def test_a_hit_is_queued_for_review_not_activated(self, db, monkeypatch):
        store.upsert_company(db, {"name": "Emmes", "active": 0,
                                  "tags": tags.WATCH, "source": "directory"})
        _miss(db, "Emmes", "no-board-found:wrong-domain")
        self._wire(monkeypatch, ({"name": "Emmes", "ats": "greenhouse",
                                  "slug": "emmes", "careers_url":
                                  "https://emmes.com/careers",
                                  "count": 40, "nc": 4, "via": "sniff"}, None))

        assert len(ops.reresolve_misses(conn=db, max_workers=1, t=self.T)) == 1

        row = dict(db.execute(
            "SELECT * FROM companies WHERE name='Emmes'").fetchone())
        assert (row["ats"], row["slug"]) == ("greenhouse", "emmes")
        assert row["active"] == 0, "a re-resolved board is reviewed, not crawled"
        assert tags.parse(row["tags"]) == {tags.WATCH, tags.PENDING}, \
            "the pending tag must merge with the row's existing scope tags"
        assert (row["miss_reason"], row["miss_at"]) == (None, None)
        assert row["last_probed"]
        assert row["mission_tier"] == "adjacent", \
            "the review queue shows the tier, so it has to be scored here"
        assert row["source"] == "directory", "the row's provenance is not ours"

    def test_stale_coordinates_do_not_survive_a_new_board(self, db, monkeypatch):
        # upsert_company drops None values so it can never erase a stored
        # one; without an explicit clear, the dead iCIMS slug would sit
        # beside the newly resolved Workday triple.
        _miss(db, "Advarra", "board-dead:icims", ats="icims", slug="advarra")
        self._wire(monkeypatch, ({"name": "Advarra", "ats": "workday",
                                  "slug": ("advarra", 5, "External"),
                                  "careers_url": None,
                                  "count": 12, "nc": 3, "via": "sniff"}, None))

        ops.reresolve_misses(conn=db, max_workers=1, t=self.T)

        row = dict(db.execute(
            "SELECT * FROM companies WHERE name='Advarra'").fetchone())
        assert row["slug"] is None
        assert (row["ats"], row["wd_tenant"], row["wd_pod"],
                row["wd_site"]) == ("workday", "advarra", 5, "External")
        assert store.board_key(row) == ("workday", "advarra", 5, "External")

    def test_a_repeated_miss_updates_the_reason_and_the_stamp(
            self, db, monkeypatch):
        _miss(db, "Emmes", "no-board-found")
        db.execute("UPDATE companies SET miss_at='2020-01-01' "
                   "WHERE name='Emmes'")
        self._wire(monkeypatch, (None, "no-board-found:domain-unreachable"))

        assert ops.reresolve_misses(conn=db, max_workers=1, t=self.T) == []

        row = dict(db.execute(
            "SELECT * FROM companies WHERE name='Emmes'").fetchone())
        assert row["miss_reason"] == "no-board-found:domain-unreachable"
        assert row["miss_at"] > "2020-01-01", \
            "a retried miss must move to the back of the queue"
        assert row["active"] == 0
        assert tags.PENDING not in tags.parse(row["tags"]), \
            "a row that still does not resolve has nothing to review"

    def test_a_board_another_row_already_owns_is_not_stolen(
            self, db, monkeypatch, capsys):
        store.upsert_company(db, {"name": "SAS Institute", "ats": "icims",
                                  "slug": "globalcareers-sas", "active": 1})
        _miss(db, "SAS", "no-board-found")
        db.execute("UPDATE companies SET miss_at='2020-01-01' WHERE name='SAS'")
        self._wire(monkeypatch, ({"name": "SAS", "ats": "icims",
                                  "slug": "globalcareers-sas",
                                  "careers_url": "https://www.sas.com/careers",
                                  "count": 150, "nc": 30, "via": "sniff"}, None))

        assert ops.reresolve_misses(conn=db, max_workers=1, t=self.T) == []

        assert "[dup]" in capsys.readouterr().out
        row = dict(db.execute(
            "SELECT * FROM companies WHERE name='SAS'").fetchone())
        assert row["ats"] is None and row["miss_reason"] == "no-board-found"
        assert row["miss_at"] > "2020-01-01", \
            "re-stamped, so a bounded rerun moves past it"

    def test_nothing_to_do_is_not_an_error(self, db, capsys):
        assert ops.reresolve_misses(conn=db, t=self.T) == []
        assert "no re-resolvable misses" in capsys.readouterr().out


class TestManualAddUsesTheSharedResolver:
    """add_manual_job resolved through a probe-first resolver of its own —
    a name-guessed slug tried before the company's own careers page, which
    is the collision a hand-typed employer name is most exposed to. It now
    goes through resolve_or_miss like every other interactive add path."""

    def _wire(self, monkeypatch, result, seen):
        def _resolve(name, careers_url=""):
            seen.append(name)
            return result

        monkeypatch.setattr(local_sourcing, "resolve_or_miss", _resolve)
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        monkeypatch.setattr("core.claude.score_company_mission",
                            lambda *a, **k: ("adjacent", 0.5, "stub"))
        monkeypatch.setattr("core.claude.is_active_mission",
                            lambda *a, **k: True)
        # No crawl, no ingest, no résumé read — this test is about the
        # resolver call, and all three would reach the disk or the network.
        monkeypatch.setattr(ops, "ingest_external_jobs", lambda *a, **k: 1)
        monkeypatch.setattr(ops, "crawl_company", lambda *a, **k: (0, 0, 0))
        monkeypatch.setattr(ops, "resume_text", lambda *a, **k: "")

    def test_the_probe_first_resolver_is_gone(self):
        assert not hasattr(local_sourcing, "resolve_company_board"), \
            "one resolver for the interactive paths, not three"

    def test_a_resolved_board_is_written_from_the_shared_resolver(
            self, tmp_path, monkeypatch):
        seen = []
        self._wire(monkeypatch, ({"name": "Emmes", "ats": "greenhouse",
                                  "slug": "emmes",
                                  "careers_url": "https://emmes.com/careers",
                                  "count": 40, "nc": 4, "via": "sniff"}, None),
                   seen)
        t = {"db_path": tmp_path / "t.db"}

        out = ops.add_manual_job("https://emmes.com/jobs/1", "Data Engineer",
                                 "Emmes", "Durham, NC", t=t)

        assert seen == ["Emmes"]
        assert out["board"] is True
        conn = store.connect(t["db_path"])
        row = dict(conn.execute(
            "SELECT * FROM companies WHERE name='Emmes'").fetchone())
        conn.close()
        assert (row["ats"], row["slug"]) == ("greenhouse", "emmes")
        assert row["active"] == 1

    def test_an_unresolved_company_keeps_the_reason_not_a_prose_note(
            self, tmp_path, monkeypatch):
        seen = []
        self._wire(monkeypatch, (None, "no-board-found:domain-unreachable"),
                   seen)
        t = {"db_path": tmp_path / "t.db"}

        out = ops.add_manual_job("https://axoft.com/jobs/1", "Data Engineer",
                                 "Axoft", "Durham, NC", t=t)

        assert out["board"] is False
        conn = store.connect(t["db_path"])
        row = dict(conn.execute(
            "SELECT * FROM companies WHERE name='Axoft'").fetchone())
        conn.close()
        assert row["miss_reason"] == "no-board-found:domain-unreachable"
        assert row["active"] == 0
        # Which is exactly what a later re-resolution pass selects on.
        conn = store.connect(t["db_path"])
        assert [c["name"] for c in ops._reresolve_candidates(conn)] == ["Axoft"]
        conn.close()


class TestPeopleAdminSignature:
    """A hosted PeopleAdmin tenant is detectable from its board URL, so
    --add-board can register a university board instead of the operator
    hand-writing an import file for it."""

    URL = "https://unc.peopleadmin.com/postings/search?query=data"

    def test_a_hosted_tenant_is_detected(self):
        assert sniffer._detect("", self.URL) == ("semi", "peopleadmin", "unc")

    def test_the_vendor_site_is_not_a_tenant(self):
        assert sniffer._detect("", "https://www.peopleadmin.com/") is None

    def test_a_tenant_on_its_own_hostname_has_no_signature(self):
        # Still an import-file job: nothing on jobs.ncsu.edu says which ATS
        # serves it. Documented on local_sourcing.add_board.
        assert sniffer._detect(
            "", "https://jobs.ncsu.edu/postings/all_jobs.atom") is None

    def test_every_page_of_a_tenant_packs_to_one_board(self):
        keys = {store.board_key(sniffer._pack("peopleadmin", "unc", u))
                for u in (self.URL,
                          "https://unc.peopleadmin.com/postings/all_jobs.atom",
                          "https://unc.peopleadmin.com")}
        assert keys == {("peopleadmin", "https://unc.peopleadmin.com")}

    def test_the_packed_host_is_what_the_fetcher_reads(self):
        from scrapers.fetchers.peopleadmin import feed_host
        packed = sniffer._pack("peopleadmin", "unc", self.URL)
        assert feed_host(packed["careers_url"]) == "unc.peopleadmin.com"
