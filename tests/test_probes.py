"""The (ok, count) contract every ATS slug probe answers.

The twelve probes used to be twelve hand-written request-and-swallow
blocks; they are built from two helpers now (probes._api_probe /
_parser_probe). What matters per ATS is the `require_jobs` decision -- is
a 200 proof of a board, or must it list something -- and that decision is
what these pin, because it is invisible in the table otherwise and it has
been got wrong before: SmartRecruiters answers 200 with totalFound:0 for
ANY slug, so every guessed slug "confirmed" with zero jobs.
"""

from datetime import datetime, timedelta

import pytest

from conftest import fake_response, keep_store_open

from src.discovery.resolve import probes
from src.ops import maintenance as ops
from src.ops import roster
import src.store as store


def _iso_days_ago(days):
    return (datetime.now() - timedelta(days=days)).isoformat()


@pytest.fixture
def answer(monkeypatch):
    """Serve one response to every probe, and record the request."""
    seen = {}

    def _install(resp):
        def _get(url, **kw):
            seen["url"], seen["headers"] = url, kw.get("headers") or {}
            seen["timeout"] = kw.get("timeout")
            return resp
        monkeypatch.setattr(probes.SESSION, "get", _get)
        return seen
    return _install


class TestAPresentBoardIsConfirmed:
    def test_greenhouse_counts_its_jobs(self, answer):
        answer(fake_response({"jobs": [1, 2, 3]}))
        assert probes.probe_greenhouse("acme") == (True, 3)

    def test_lever_counts_a_bare_list(self, answer):
        answer(fake_response([1, 2]))
        assert probes.probe_lever("acme") == (True, 2)

    def test_lever_tolerates_a_non_list_payload(self, answer):
        answer(fake_response({"unexpected": True}))
        assert probes.probe_lever("acme") == (True, 0)

    def test_ashby_reads_the_posting_api_key(self, answer):
        answer(fake_response({"jobs": [1, 2], "jobPostings": []}))
        assert probes.probe_ashby("acme") == (True, 2)

    def test_ashby_falls_back_to_the_embed_key(self, answer):
        answer(fake_response({"jobPostings": [1]}))
        assert probes.probe_ashby("acme") == (True, 1)

    def test_bamboohr_asks_for_json(self, answer):
        seen = answer(fake_response({"result": [1, 2, 3, 4]}))
        assert probes.probe_bamboohr("acme") == (True, 4)
        assert seen["headers"]["Accept"] == "application/json"

    def test_jazzhr_counts_apply_links_in_the_page(self, answer):
        answer(fake_response(text="<a href='/apply/AbC123/'>x</a>"
                          "<a href='/apply/dEf456/'>y</a>"))
        assert probes.probe_jazzhr("acme") == (True, 2)

    def test_kula_accepts_a_substantial_page(self, answer):
        answer(fake_response(text="x" * 1001))
        assert probes.probe_kula("acme") == (True, 0)


class TestAnEmptyBoardIsNotAlwaysAMiss:
    """An ATS that only serves real slugs may legitimately list nothing;
    one that answers for any slug must show postings to count as found."""

    def test_greenhouse_empty_is_still_a_board(self, answer):
        answer(fake_response({"jobs": []}))
        assert probes.probe_greenhouse("acme") == (True, 0)

    def test_smartrecruiters_empty_is_not_a_board(self, answer):
        answer(fake_response({"totalFound": 0}))
        assert probes.probe_smartrecruiters("acme") == (False, 0)

    def test_smartrecruiters_with_postings_is(self, answer):
        answer(fake_response({"totalFound": 7}))
        assert probes.probe_smartrecruiters("acme") == (True, 7)

    def test_jazzhr_with_no_apply_links_is_not_a_board(self, answer):
        answer(fake_response(text="<html>nothing here</html>"))
        assert probes.probe_jazzhr("acme") == (False, 0)

    def test_kula_rejects_a_stub_page(self, answer):
        answer(fake_response(text="too short"))
        assert probes.probe_kula("acme") == (False, 0)


class TestFailureIsReportedNeverRaised:
    """A probe runs inside a discovery fan-out over hundreds of names. It
    reports (False, 0) for everything -- a bad status, a timeout, a
    payload that will not parse -- so one broken host cannot end the pass.
    """

    def test_a_non_200_is_a_miss(self, answer):
        answer(fake_response({"jobs": [1]}, status=404))
        assert probes.probe_greenhouse("acme") == (False, 0)

    def test_unparseable_json_is_a_miss(self, answer):
        answer(fake_response(None))
        assert probes.probe_greenhouse("acme") == (False, 0)

    def test_a_raising_session_is_a_miss(self, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("connection reset")
        monkeypatch.setattr(probes.SESSION, "get", _boom)
        assert probes.probe_greenhouse("acme") == (False, 0)

    def test_kula_retries_once_before_giving_up(self, monkeypatch):
        calls = []

        def _get(url, **kw):
            calls.append(url)
            raise OSError("throttled")
        monkeypatch.setattr(probes.SESSION, "get", _get)
        monkeypatch.setattr(probes.time, "sleep", lambda s: None)
        assert probes.probe_kula("acme") == (False, 0)
        assert len(calls) == 2, "kula gets one retry; the others get none"

    def test_the_others_do_not_retry(self, monkeypatch):
        calls = []

        def _get(url, **kw):
            calls.append(url)
            raise OSError("throttled")
        monkeypatch.setattr(probes.SESSION, "get", _get)
        assert probes.probe_greenhouse("acme") == (False, 0)
        assert len(calls) == 1


class TestParserBackedProbes:
    """Four ATSes reuse the fetcher's own board parser rather than a URL
    of their own. Their `ok` means "has jobs", which is why
    ops.prune_dead_boards refuses to use them to decide a board is dead.
    """

    def test_it_counts_what_the_parser_returned(self, monkeypatch):
        monkeypatch.setattr("src.ats.fetchers.rippling.parse_board",
                            lambda h: [1, 2, 3])
        assert probes.probe_rippling("acme") == (True, 3)

    def test_an_empty_parse_is_not_ok(self, monkeypatch):
        monkeypatch.setattr("src.ats.fetchers.hibob.parse_board",
                            lambda h: [])
        assert probes.probe_hibob("acme") == (False, 0)

    def test_a_raising_parser_is_a_miss(self, monkeypatch):
        def _boom(h):
            raise RuntimeError("shape changed")
        monkeypatch.setattr("src.ats.fetchers.ultipro.parse_board", _boom)
        assert probes.probe_ultipro("CODE|GUID") == (False, 0)


def test_every_registered_probe_is_callable():
    """PROBES is what pipeline.validate_candidate iterates; a name in it
    with nothing behind it fails only in a live discovery run."""
    for ats, probe in probes.PROBES.items():
        assert callable(probe), ats


class TestPruneNamesWhatItDeactivates:
    """ops.prune_dead_boards acts on these probes; its session log must
    say how many boards it probed and name every company it deactivates."""

    @staticmethod
    def _company(db, name, ats, slug, **extra):
        store.upsert_company(db, {"name": name, "ats": ats, "slug": slug,
                                  **extra})

    def test_the_prune_op_names_each_dead_board_and_sums_up(
            self, db, monkeypatch, capsys):
        keep_store_open(monkeypatch, db)
        self._company(db, "Gone Co", "greenhouse", "gone")
        self._company(db, "Live Co", "greenhouse", "live")
        monkeypatch.setattr(probes, "probe_greenhouse",
                            lambda slug: (slug == "live", 3))

        roster.prune()

        out = capsys.readouterr().out
        assert "probing 2 board(s) for a dead ATS endpoint" in out
        [line] = [ln for ln in out.splitlines() if "[dead]" in ln]
        assert "Gone Co" in line and "greenhouse" in line
        assert "board 'gone' no longer resolves" in line
        assert "Live Co" not in out
        assert "deactivated 1 dead-board compan(ies)." in out

    def test_an_off_mission_deactivation_names_its_score(
            self, db, monkeypatch, capsys):
        self._company(db, "Other Co", "lever", "other",
                      mission_tier="other", mission_score=0.05)
        monkeypatch.setattr(probes, "probe_lever", lambda slug: (True, 5))

        assert ops.prune_dead_boards(db, deactivate_offmission=True) == (0, 1)

        [line] = [ln for ln in capsys.readouterr().out.splitlines()
                  if "[other]" in ln]
        assert "Other Co" in line and "lever" in line
        assert "off-mission (score=0.05)" in line


class TestSelfHealRetryMarker:
    """self_heal_unscored must not re-ask Claude about a row forever once it
    has already refused (2026-09: the same 3-7 NC State rows re-refused on
    every crawl for weeks). ops._unscored_marker / ops._unscored_due, driven
    end to end through self_heal_unscored itself."""

    @staticmethod
    def _stub_refusal(monkeypatch):
        """Every call_claude_json call behaves like a refusal: no usable
        JSON, which is what score_resume_fit turns into reason="unscored"."""
        import src.claude.fit as fit_module
        calls = []
        # A configured key, so the empty reply reads as the MODEL saying
        # nothing rather than as the scorer being offline (which is not a
        # verdict on the posting and must never mark a row).
        monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(fit_module, "call_claude_json",
                            lambda *a, **k: calls.append(1) or {})
        return calls

    def test_an_offline_scorer_marks_nothing(self, db, add_job, monkeypatch):
        # The breaker tripping mid-pass (expired key, exhausted balance)
        # would otherwise park every unscored row for UNSCORED_RETRY_DAYS.
        self._stub_refusal(monkeypatch)
        import src.claude.fit as fit_module
        monkeypatch.setattr(fit_module, "api_disabled", lambda: "HTTP 401")
        jid = add_job("j1", description="x" * 300, fit=None)

        assert ops.self_heal_unscored(db, "resume", "local-tech") == 0

        row = dict(db.execute("SELECT * FROM jobs WHERE job_id=?",
                              (jid,)).fetchone())
        assert not (row["fit_reason"] or ""), "an offline scorer judged nothing"

    def test_a_refusal_is_marked_with_cause_and_length(
            self, db, add_job, monkeypatch):
        calls = self._stub_refusal(monkeypatch)
        jid = add_job("j1", description="x" * 300, fit=None)

        n = ops.self_heal_unscored(db, "resume", "local-tech")

        assert n == 0
        assert len(calls) == 1
        row = dict(db.execute("SELECT * FROM jobs WHERE job_id=?",
                              (jid,)).fetchone())
        assert row["resume_fit_score"] is None
        assert row["fit_reason"] == f"unscored:refused:300:{datetime.now().date()}"

    def test_a_fresh_refusal_marker_holds_off_the_next_pass(
            self, db, add_job, monkeypatch):
        calls = self._stub_refusal(monkeypatch)
        add_job("j1", description="x" * 300, fit=None)
        ops.self_heal_unscored(db, "resume", "local-tech")
        assert len(calls) == 1

        n = ops.self_heal_unscored(db, "resume", "local-tech")   # same day, same body

        assert n == 0
        assert len(calls) == 1, "a fresh REFUSED marker must not be re-asked"

    def test_a_30_day_old_refusal_marker_is_retried(
            self, db, add_job, monkeypatch):
        calls = self._stub_refusal(monkeypatch)
        jid = add_job("j1", description="x" * 300, fit=None)
        db.execute("UPDATE jobs SET fit_reason=? WHERE job_id=?",
                  (f"unscored:refused:300:{_iso_days_ago(31)[:10]}", jid))
        db.commit()

        ops.self_heal_unscored(db, "resume", "local-tech")

        assert len(calls) == 1, "30+ days on, the same row is due again"

    def test_a_changed_body_is_retried_the_same_day(
            self, db, add_job, monkeypatch):
        calls = self._stub_refusal(monkeypatch)
        jid = add_job("j1", description="x" * 300, fit=None)
        db.execute(
            "UPDATE jobs SET fit_reason=?, description=? WHERE job_id=?",
            (f"unscored:refused:300:{datetime.now().date()}", "y" * 450, jid))
        db.commit()

        ops.self_heal_unscored(db, "resume", "local-tech")

        assert len(calls) == 1, "a body that grew is due even on the same day"

    def test_scoring_succeeds_once_due_and_replaces_the_marker(
            self, db, add_job, monkeypatch):
        import src.claude.fit as fit_module
        jid = add_job("j1", description="x" * 300, fit=None)
        db.execute("UPDATE jobs SET fit_reason=? WHERE job_id=?",
                  (f"unscored:refused:300:{_iso_days_ago(31)[:10]}", jid))
        db.commit()
        monkeypatch.setattr(fit_module, "call_claude_json", lambda *a, **k: {
            "domain": 0.5, "function": 0.5, "stack": 0.5, "seniority": 0.5,
            "gates": [], "reason": "fits"})

        n = ops.self_heal_unscored(db, "resume", "local-tech")

        assert n == 1
        row = dict(db.execute("SELECT * FROM jobs WHERE job_id=?",
                              (jid,)).fetchone())
        assert row["resume_fit_score"] is not None
        assert not row["fit_reason"].startswith("unscored:")


class TestRescoreAllUsesTheSharedUnscoredMarker:
    """rescore_all's bodyless-row branch shares self_heal_unscored's own
    vocabulary (ops._unscored_marker) instead of a bare, undated string, so
    the store has one shape for "nothing to score here" everywhere it
    appears."""

    def test_a_bodyless_row_gets_the_shared_marker(
            self, db, add_job, monkeypatch):
        keep_store_open(monkeypatch, db)
        monkeypatch.setattr(ops, "resume_text", lambda: "resume")
        jid = add_job("j1", description="too short", fit=0.4)

        ops.rescore_all()

        row = dict(db.execute("SELECT * FROM jobs WHERE job_id=?",
                              (jid,)).fetchone())
        assert row["resume_fit_score"] is None
        assert row["fit_reason"].startswith("unscored:short:")


class TestDeadBoardClosure:
    """check_closed_jobs's board-dead sweep: ops._dead_board_open_rows plus
    the closure loop inside check_closed_jobs itself. Judi Health (47 open
    rows, miss_reason board-dead:greenhouse since 2026-09-11) is the live
    case this was written for."""

    @staticmethod
    def _seed(db, name, ats, miss_reason=None, days_stale=20):
        # A promoted board-dead company writes miss_reason with a raw
        # UPDATE (src.store.companies.mark_harvested), not through
        # record_miss -- record_miss's own "never demote an ACTIVE company"
        # guard is exactly what the promotion cycle exists to override, so
        # a plain record_miss call here would silently no-op and the test
        # would pass for the wrong reason (miss_reason never set at all).
        cid = store.upsert_company(db, {"name": name, "ats": ats})
        store.upsert_job(db, {"job_id": f"{name}-j1", "company_id": cid,
                              "company_name": name, "title": "T",
                              "track": "local-tech",
                              "url": f"https://{ats}.example/{name}"})
        db.execute("UPDATE jobs SET last_seen=? WHERE job_id=?",
                  (_iso_days_ago(days_stale), f"{name}-j1"))
        if miss_reason:
            db.execute("UPDATE companies SET miss_reason=? WHERE name=?",
                      (miss_reason, name))
        db.commit()
        return f"{name}-j1"

    def test_a_dead_boards_stale_open_row_closes_without_a_probe(
            self, db, monkeypatch, capsys):
        jid = self._seed(db, "Judi Health", "greenhouse",
                         "board-dead:greenhouse", days_stale=20)
        probed = []

        def _probe(url):
            probed.append(url)
            return (None, "n/a")
        monkeypatch.setattr(ops.company_fetch, "probe_job_open", _probe)

        # stale_days=999 keeps this row OUT of the URL-probe population
        # entirely (last_seen is only 20 days old) -- proving the closure
        # is a SEPARATE step, not a side effect of probing.
        n = ops.check_closed_jobs(conn=db, stale_days=999)

        assert n == 1
        assert probed == [], "closed by inference; no URL should be fetched"
        row = db.execute("SELECT status FROM jobs WHERE job_id=?",
                         (jid,)).fetchone()
        assert row["status"] == "closed"
        assert "[dead-board]" in capsys.readouterr().out

    def test_a_quiet_board_with_no_miss_is_never_closed_this_way(
            self, db, monkeypatch):
        jid = self._seed(db, "Quiet Co", "lever", miss_reason=None,
                         days_stale=400)
        monkeypatch.setattr(ops.company_fetch, "probe_job_open",
                            lambda url: (None, "n/a"))

        ops.check_closed_jobs(conn=db, stale_days=999)

        row = db.execute("SELECT status FROM jobs WHERE job_id=?",
                         (jid,)).fetchone()
        assert row["status"] != "closed"

    def test_a_miss_in_a_different_family_is_never_closed_this_way(
            self, db, monkeypatch):
        jid = self._seed(db, "Ats Gap", None, "ats-unsupported:ukg",
                         days_stale=400)
        monkeypatch.setattr(ops.company_fetch, "probe_job_open",
                            lambda url: (None, "n/a"))

        ops.check_closed_jobs(conn=db, stale_days=999)

        row = db.execute("SELECT status FROM jobs WHERE job_id=?",
                         (jid,)).fetchone()
        assert row["status"] != "closed"


class TestClosedProbeRotation:
    """check_closed_jobs(limit=N) must eventually reach every stale row
    instead of re-probing the same N forever: 2026-09-17 evidence was
    'ORDER BY company_name' never changing, so a live/unverifiable verdict
    (unlike a closed one) left a row exactly where it was for the next
    pass to pick again."""

    @staticmethod
    def _seed(db, n):
        cid = store.upsert_company(db, {"name": "Acme", "ats": "greenhouse"})
        for i in range(n):
            jid = f"j{i:03d}"
            store.upsert_job(db, {"job_id": jid, "company_id": cid,
                                  "company_name": "Acme", "title": "T",
                                  "track": "local-tech",
                                  "url": f"https://acme.example/{jid}"})
        db.execute("UPDATE jobs SET last_seen=?", (_iso_days_ago(30),))
        db.commit()

    def test_250_stale_rows_are_fully_covered_in_three_passes_of_100(
            self, db, monkeypatch):
        self._seed(db, 250)
        # Worst case: every probe is unverifiable, so nothing ever leaves
        # the WHERE clause by closing -- rotation is the only thing that
        # can cover the backlog.
        monkeypatch.setattr(ops.company_fetch, "probe_job_open",
                            lambda url: (None, "gated"))

        for _ in range(3):
            ops.check_closed_jobs(conn=db, stale_days=1, limit=100)

        covered = db.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE desc_checked_at IS NOT NULL").fetchone()["n"]
        assert covered == 250

    def test_a_single_pass_still_leaves_the_rest_for_next_time(self, db, monkeypatch):
        self._seed(db, 250)
        monkeypatch.setattr(ops.company_fetch, "probe_job_open",
                            lambda url: (None, "gated"))

        ops.check_closed_jobs(conn=db, stale_days=1, limit=100)

        covered = db.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE desc_checked_at IS NOT NULL").fetchone()["n"]
        assert covered == 100
