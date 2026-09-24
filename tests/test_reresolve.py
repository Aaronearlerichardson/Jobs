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

import re

import pytest

from conftest import fake_response, iso_days_ago

import src.store as store
from src import tags
from src.ats import signatures as ats_signatures
from src.discovery import local_sourcing
from src.discovery.resolve import board as resolve_board
from src.ops import maintenance as ops


def _miss(db, name, reason, **fields):
    """One inactive roster row carrying `reason`."""
    store.record_miss(db, name, reason, **fields)


def _silent(db, name, harvested_days_ago=1, **fields):
    """A board added 30 days ago that has listed nothing since and was
    harvested `harvested_days_ago` days ago: a SILENT_FAMILY row."""
    store.upsert_company(db, {"name": name, "ats": "lever",
                              "slug": name.lower(), "total_job_count": 0,
                              **fields})
    db.execute("UPDATE companies SET created_at=?, last_harvested_at=? "
               "WHERE name=?",
               (iso_days_ago(30), iso_days_ago(harvested_days_ago), name))


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


class TestSilentBoardFamily:
    """SILENT_FAMILY selection is doctested on _silent_board_candidates and
    _reresolve_candidates; these are the two clauses the doctests leave
    unstaged."""

    def test_a_row_older_than_the_created_at_column_is_selected(self, db):
        _silent(db, "Legacy")
        db.execute("UPDATE companies SET created_at=NULL WHERE name='Legacy'")
        assert [c["name"] for c in ops._reresolve_candidates(
            db, families=(ops.SILENT_FAMILY,))] == ["Legacy"]

    def test_a_board_last_harvested_long_ago_is_not_selected(self, db):
        _silent(db, "Abandoned", harvested_days_ago=30)
        assert ops._reresolve_candidates(
            db, families=(ops.SILENT_FAMILY,)) == []


class TestReresolveWrites:
    """What a pass writes. The contract with the roster review queue is
    narrow on purpose: board coordinates, a mission score, active=0 and the
    pending-review tag — nothing else."""

    T = {"db_path": None}

    def _wire(self, monkeypatch, result):
        monkeypatch.setattr(resolve_board, "resolve_or_miss",
                            lambda *a, **k: result)
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        monkeypatch.setattr("src.claude.api.score_company_mission",
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

    def test_a_silent_board_retargets_when_the_sniff_finds_a_new_board(
            self, db, monkeypatch):
        """A hit on a SILENT_FAMILY row goes through the exact same write
        as any other family's -- no separate code path."""
        _silent(db, "Quiet", slug="quiet-old", active=1)
        self._wire(monkeypatch, ({"name": "Quiet", "ats": "greenhouse",
                                  "slug": "quiet-new", "careers_url":
                                  "https://quiet.example/careers",
                                  "count": 12, "nc": 2, "via": "sniff"}, None))

        written = ops.reresolve_misses(conn=db, max_workers=1, t=self.T,
                                       families=[ops.SILENT_FAMILY])

        assert len(written) == 1
        row = dict(db.execute(
            "SELECT * FROM companies WHERE name='Quiet'").fetchone())
        assert (row["ats"], row["slug"]) == ("greenhouse", "quiet-new")
        assert row["active"] == 0, \
            "retargeted the same way as any other family: reviewed, not crawled"
        assert tags.PENDING in tags.parse(row["tags"])

    def test_preview_writes_nothing_and_scores_nothing(self, db,
                                                        monkeypatch):
        _silent(db, "Quiet", slug="quiet-old", active=1)
        _miss(db, "Gone", "board-dead:lever", ats="lever", slug="gone")
        before = [dict(r) for r in db.execute("SELECT * FROM companies")]
        results = {
            "Quiet": ({"name": "Quiet", "ats": "greenhouse",
                       "slug": "quiet-new", "careers_url": None,
                       "count": 12, "nc": 2, "via": "sniff"}, None),
            "Gone": (None, "no-board-found:wrong-domain")}
        monkeypatch.setattr(resolve_board, "resolve_or_miss",
                            lambda name, *a, **k: results[name])

        def no_score(*a, **k):
            raise AssertionError("a preview never pays for a mission score")
        monkeypatch.setattr("src.claude.api.score_company_mission", no_score)

        written = ops.reresolve_misses(
            conn=db, max_workers=1, t=self.T, commit=False,
            families=ops.RERESOLVE_FAMILIES + (ops.SILENT_FAMILY,))

        assert [b["slug"] for b in written] == ["quiet-new"]
        assert [dict(r) for r in db.execute(
            "SELECT * FROM companies")] == before

    def test_an_unknown_family_is_refused(self, db):
        with pytest.raises(ValueError):
            ops.reresolve_misses(conn=db, t=self.T, families=["silent"])

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

        monkeypatch.setattr(resolve_board, "resolve_or_miss", _resolve)
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        monkeypatch.setattr("src.claude.api.score_company_mission",
                            lambda *a, **k: ("adjacent", 0.5, "stub"))
        monkeypatch.setattr("src.claude.api.is_active_mission",
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
        assert ats_signatures.detect("", self.URL) == ("semi", "peopleadmin", "unc")

    def test_the_vendor_site_is_not_a_tenant(self):
        assert ats_signatures.detect("", "https://www.peopleadmin.com/") is None

    def test_a_tenant_on_its_own_hostname_has_no_signature(self):
        # Still an import-file job: nothing on jobs.ncsu.edu says which ATS
        # serves it. Documented on local_sourcing.add_board.
        assert ats_signatures.detect(
            "", "https://jobs.ncsu.edu/postings/all_jobs.atom") is None

    def test_every_page_of_a_tenant_packs_to_one_board(self):
        keys = {store.board_key(ats_signatures.pack("peopleadmin", "unc", u))
                for u in (self.URL,
                          "https://unc.peopleadmin.com/postings/all_jobs.atom",
                          "https://unc.peopleadmin.com")}
        assert keys == {("peopleadmin", "https://unc.peopleadmin.com")}

    def test_the_packed_host_is_what_the_fetcher_reads(self, serve):
        from src.ats.fetchers.company import fetch_company
        calls = serve(fake_response(text=""))
        fetch_company(ats_signatures.pack("peopleadmin", "unc", self.URL))
        assert calls[0] == "https://unc.peopleadmin.com/postings/all_jobs.atom"


class TestJobviteSignature:
    """A Jobvite tenant is detectable from any page of its site, so
    --add-board registers it like any other ATS instead of leaving a
    detection-only lead."""

    URL = "https://jobs.jobvite.com/acme/job/oAaa1fwA"

    def test_a_tenant_is_detected_as_fetchable(self):
        assert ats_signatures.detect("", self.URL) == ("fetchable", "jobvite", "acme")

    def test_the_vendor_site_is_not_a_tenant(self):
        assert ats_signatures.detect("", "https://www.jobvite.com/") is None

    def test_the_board_key_is_the_tenant(self):
        assert store.board_key(ats_signatures.pack("jobvite", "acme", self.URL)) \
            == ("jobvite", "acme")

    def test_the_packed_slug_is_what_the_fetcher_reads(self):
        from src.ats.fetchers.board import board_for
        packed = ats_signatures.pack("jobvite", "acme", self.URL)
        assert board_for("jobvite").handle(packed) == "acme"


class TestARaisedResolutionIsReported:
    """`resolve_or_miss` converts the exceptions it can see; the FUTURE can
    still fail (a worker that dies, a cancelled task). Three consumers
    unwrapped that by hand and the reresolve copy had dropped the report
    line, so a resolution that blew up there became a miss with nothing in
    the log to say why. resolve.board.resolved is the one unwrap now."""

    def test_the_reason_and_the_report_both_survive(self, capsys):
        from concurrent.futures import Future
        from src.discovery.resolve.board import resolved

        fut = Future()
        fut.set_exception(RuntimeError("boom"))
        hit, reason = resolved(fut, "Acme Bio")
        assert hit is None
        assert reason == "fetch-error:RuntimeError"
        out = capsys.readouterr().out
        assert "Acme Bio" in out and "RuntimeError" in out

    def test_a_normal_result_passes_straight_through(self):
        from concurrent.futures import Future
        from src.discovery.resolve.board import resolved

        fut = Future()
        fut.set_result(({"name": "Acme"}, None))
        assert resolved(fut, "Acme") == ({"name": "Acme"}, None)


class TestRenameSlugBoards:
    """A board named after nothing but its own dork-guessed slug
    ("Centriaautism", "Medelitellc") reads wrong everywhere the digest or
    the logs name the employer. rename_slug_boards fixes it from the
    board's OWN listing payload -- Greenhouse's company_name, SmartRecruiters'
    company.name -- never from the network in these tests."""

    def _slug_co(self, db, name, ats, slug, source="ats_dork", **fields):
        return store.upsert_company(db, {
            "name": name, "ats": ats, "slug": slug, "active": 1,
            "source": source, **fields})

    def _stub_readers(self, serve, **by_slug):
        """Each board's OWN listing payload, served without HTTP.

        Stubs the NETWORK, not the reader: the two payload shapes
        (Greenhouse's `jobs[].company_name`, SmartRecruiters' nested
        `content[].company.name`) and the URL each slug is asked for are
        exactly what the reader exists to get right, and a stub that
        replaced the reader itself tested neither -- it only pinned the
        shape of a private table, and broke when that table stopped
        holding callables (2026-09-22 dedup: two readers -> one
        `_employer_name` over a URL/shape table).

        A slug not named here answers with an empty board.
        """
        slug_re = re.compile(r"/(?:boards|companies)/([^/]+)/(?:jobs|postings)")

        def _get(url, **kw):
            m = slug_re.search(url)
            name = by_slug.get(m.group(1), "") if m else ""
            if "smartrecruiters" in url:
                return fake_response(
                    {"content": [{"company": {"name": name}}] if name else []})
            return fake_response({"jobs": [{"company_name": name}] if name else []})

        serve(_get)

    def test_a_dork_sourced_slug_name_is_renamed_from_the_payload(
            self, db, serve):
        self._slug_co(db, "Medelitellc", "greenhouse", "medelitellc")
        self._stub_readers(serve, medelitellc="MedElite Group, LLC.")

        out = ops.rename_slug_boards(conn=db, commit=True)

        assert out == [(1, "Medelitellc", "MedElite Group, LLC.")]
        row = db.execute("SELECT name FROM companies WHERE id=1").fetchone()
        assert row["name"] == "MedElite Group, LLC."

    def test_preview_writes_nothing(self, db, serve):
        self._slug_co(db, "Medelitellc", "greenhouse", "medelitellc")
        self._stub_readers(serve, medelitellc="MedElite Group, LLC.")

        out = ops.rename_slug_boards(conn=db, commit=False)

        assert out == [(1, "Medelitellc", "MedElite Group, LLC.")]
        row = db.execute("SELECT name FROM companies WHERE id=1").fetchone()
        assert row["name"] == "Medelitellc", "preview must never write"

    def test_a_legitimately_named_company_is_never_a_candidate(
            self, db, serve):
        """name_is_own_slug alone also matches a real one-word name that
        happens to equal its slug ("Ceribell" / slug "ceribell") -- the
        SLUG_NAME_SOURCE restriction is what keeps this op off rows a
        human (or local_sourcing) named for real. Confirmed live,
        2026-09-18: dropping this restriction would have renamed a real
        neurotech employer, "NeU", to an unrelated company's name because
        NeU's stored Greenhouse slug no longer points at NeU's own board."""
        self._slug_co(db, "Ceribell", "greenhouse", "ceribell",
                      source="local_sourcing")
        self._stub_readers(serve, ceribell="Ceribell, Inc")

        assert ops.rename_slug_boards(conn=db, commit=True) == []
        row = db.execute("SELECT name FROM companies WHERE id=1").fetchone()
        assert row["name"] == "Ceribell"

    def test_a_name_that_is_not_its_own_slug_is_never_a_candidate(
            self, db, serve):
        self._slug_co(db, "Precision for Medicine", "greenhouse", "pfm")
        self._stub_readers(serve, pfm="Precision for Medicine")

        assert ops.rename_slug_boards(conn=db, commit=True) == []

    def test_an_empty_payload_answer_is_skipped(self, db, serve, capsys):
        self._slug_co(db, "Resultspt", "greenhouse", "resultspt")
        self._stub_readers(serve)   # every slug answers ""

        assert ops.rename_slug_boards(conn=db, commit=True) == []
        assert "no employer name" in capsys.readouterr().out

    def test_a_junk_payload_name_is_rejected_not_written(
            self, db, serve, capsys):
        # A payload can carry garbage too -- the same junk_name_reason
        # screen a pasted or re-resolved name goes through applies here.
        self._slug_co(db, "Science37", "greenhouse", "science37")
        self._stub_readers(serve, science37="Science 37")

        assert ops.rename_slug_boards(conn=db, commit=True) == []
        out = capsys.readouterr().out
        assert "rejected" in out and "numbered-duplicate" in out
        row = db.execute("SELECT name FROM companies WHERE id=1").fetchone()
        assert row["name"] == "Science37"

    def test_a_name_matching_what_is_already_stored_is_not_reapplied(
            self, db, serve):
        self._slug_co(db, "Eurofins", "smartrecruiters", "Eurofins")
        self._stub_readers(serve, Eurofins="Eurofins")

        assert ops.rename_slug_boards(conn=db, commit=True) == []

    def test_a_name_that_would_collide_with_another_company_is_rejected(
            self, db, serve, capsys):
        store.upsert_company(db, {"name": "Cortica", "ats": "greenhouse",
                                  "slug": "cortica-hq", "active": 1,
                                  "source": "local_sourcing"})
        self._slug_co(db, "Corticaneuro", "greenhouse", "corticaneuro")
        self._stub_readers(serve, corticaneuro="Cortica")

        assert ops.rename_slug_boards(conn=db, commit=True) == []
        assert "collides" in capsys.readouterr().out
        row = db.execute(
            "SELECT name FROM companies WHERE name='Corticaneuro'").fetchone()
        assert row is not None, "the row must be left exactly as it was"

    def test_an_inactive_board_is_not_a_candidate(self, db, serve):
        self._slug_co(db, "Medelitellc", "greenhouse", "medelitellc",
                      active=0)
        self._stub_readers(serve, medelitellc="MedElite Group, LLC.")

        assert ops.rename_slug_boards(conn=db, commit=True) == []

    def test_an_unsupported_ats_is_not_a_candidate(self, db, monkeypatch):
        # Workday/Lever/Ashby carry no reliable board-level employer field
        # (see the module comment above _employer_atses) -- confirmed
        # live, not merely assumed, so they are not in the reader map at
        # all rather than silently returning "".
        store.upsert_company(db, {"name": "Lifestance", "ats": "lever",
                                  "slug": "lifestance", "active": 1,
                                  "source": "ats_dork"})
        assert ops.rename_slug_boards(conn=db, commit=True) == []


class TestRekeyJobs:
    """rekey_jobs moves stored rows to the id their board spec gives them
    now (Phenom's gained its host, D11), merging a row a harvest already
    stored under the new id only when both name one posting."""

    A, B = "https://careers.a.org/us/en/job/", "https://careers.b.org/us/en/job/"

    @staticmethod
    def _job(db, cid, job_id, url, title, **cols):
        cols = {"job_id": job_id, "company_id": cid, "url": url, "title": title,
                "status": "open", **cols}
        db.execute(f"INSERT INTO jobs ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))

    @pytest.fixture
    def rows(self, db):
        a = store.upsert_company(db, {"name": "A", "ats": "phenom", "slug": "careers.a.org"})
        b = store.upsert_company(db, {"name": "B", "ats": "phenom", "slug": "careers.b.org"})
        self._job(db, a, "phenom_1", self.A + "1", "T1")
        self._job(db, a, "phenom_2", self.A + "2", "T2", disposition="applied",
                  first_seen="2026-01-01")
        self._job(db, a, "phenom_careers_a_org_2", self.A + "2", "t2",
                  resume_fit_score=0.7, first_seen="2026-09-01")
        self._job(db, a, "phenom_3", self.A + "3", "T3")
        self._job(db, a, "phenom_careers_a_org_3", self.A + "3", "Another posting")
        self._job(db, a, "wd_x_1", "https://x.wd1.myworkdayjobs.com/Site/job/Y_1", "W")
        self._job(db, b, "phenom_4", self.B + "4", "T4")
        self._job(db, a, "phenom_careers_b_org_4", self.A + "careers_b_org_4", "T4")
        db.commit()

    def _ids(self, db):
        return sorted(r[0] for r in db.execute("SELECT job_id FROM jobs"))

    def test_the_preview_sorts_every_row_and_writes_nothing(self, db, rows):
        before = self._ids(db)
        assert ops.rekey_jobs("phenom", conn=db) == {
            "unchanged": 2, "rekey": 2, "merge": 1, "conflict": 1,
            "cross-tenant": 1, "unresolvable": 1}
        assert self._ids(db) == before

    def test_apply_rekeys_and_merges_one_posting_into_one_row(self, db, rows):
        ops.rekey_jobs("phenom", commit=True, conn=db)
        assert self._ids(db) == sorted([
            "phenom_careers_a_org_1", "phenom_careers_a_org_2", "phenom_3",
            "phenom_careers_a_org_3", "wd_x_1", "phenom_4",
            "phenom_careers_a_org_careers_b_org_4"])
        merged = db.execute("SELECT disposition, resume_fit_score, first_seen FROM jobs "
                            "WHERE job_id='phenom_careers_a_org_2'").fetchone()
        assert tuple(merged) == ("applied", 0.7, "2026-01-01")
