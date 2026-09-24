"""The (ok, count) contract every ATS slug probe answers, and the
three-valued verdict the per-JOB closure probe answers.

Every platform probes through the engine (`Board.probe`: one cheap read,
ok only when it lists a posting). A 200 alone is no proof of a board:
SmartRecruiters answers 200 with totalFound:0 for ANY slug, so every
guessed slug once "confirmed" with zero jobs.

The job probe (fetchers.probe.probe_job_open, driven by
ops.check_closed_jobs) has the same shape of hidden decision, one per ATS
family: which answer counts as PROOF the posting is gone. Getting it
wrong in either direction is silent -- too strict and the op closes
nothing (2026-09-21: 1 closed, 0 live, 36 unverifiable of 37), too loose
and it closes live postings off a 403.
"""

import time
from datetime import datetime, timedelta

import pytest
import requests

from conftest import fake_response, iso_days_ago, keep_store_open

from src.ats.fetchers import probe as job_probe
from src.ats.fetchers.board import board_for
from src.discovery.resolve import probes
from src.net.http import HEADERS, PLAIN_HEADERS
from src.ops import maintenance as ops
from src.ops import roster
import src.store as store


def seed_stale(db, name="Acme", *, ats="greenhouse", urls=None, n=1,
               days_stale=30, harvested=None, miss_reason=None, **company):
    """One company and its OPEN job rows, none of them seen for
    `days_stale` days -- the population check_closed_jobs selects from.
    Returns the job ids, in order.

    `urls` seeds one row per URL (what the per-family reporting needs);
    otherwise `n` rows get a synthetic URL each. `harvested` fills
    companies.last_harvested_at, which the selection reads.

    Notes:
        Five classes below had written this out, each with its own id and
        URL convention. `miss_reason` and `last_harvested_at` go in by raw
        UPDATE, the way src.store.companies.mark_harvested does:
        record_miss's "never demote an ACTIVE company" guard is exactly
        what the promotion cycle overrides, so a record_miss call here
        would silently no-op and the test would pass having set nothing.
        `slug` is not seeded because store.harvestable_companies -- the
        rule under test -- never reads one.
    """
    cid = store.upsert_company(db, {"name": name, "ats": ats, **company})
    ids = []
    for i, url in enumerate([None] * n if urls is None else urls):
        jid = f"{name}-j{i}"
        store.upsert_job(db, {"job_id": jid, "company_id": cid,
                              "company_name": name, "title": "T",
                              "track": "local-tech",
                              "url": url or f"https://acme.example/{jid}"})
        ids.append(jid)
    db.execute("UPDATE jobs SET last_seen=? WHERE company_id=?",
               (iso_days_ago(days_stale), cid))
    db.execute("UPDATE companies SET last_harvested_at=?, miss_reason=? "
               "WHERE id=?", (harvested, miss_reason, cid))
    db.commit()
    return ids


def _ids(n):
    """`n` listing entries: a posting is an entry with an id."""
    return [{"id": i} for i in range(n)]


class TestAPresentBoardIsConfirmed:
    def test_greenhouse_counts_its_jobs(self, serve):
        serve(fake_response({"jobs": _ids(3) + [{}]}))
        assert probes.PROBES["greenhouse"]("acme") == (True, 3)

    def test_lever_counts_a_bare_list(self, serve):
        serve(fake_response(_ids(2)))
        assert probes.PROBES["lever"]("acme") == (True, 2)

    def test_lever_tolerates_a_non_list_payload(self, serve):
        serve(fake_response({"unexpected": True}))
        assert probes.PROBES["lever"]("acme") == (False, 0)

    def test_ashby_reads_the_posting_api_key(self, serve):
        serve(fake_response({"jobs": _ids(2), "jobPostings": []}))
        assert probes.PROBES["ashby"]("acme") == (True, 2)

    def test_ashby_falls_back_to_the_embed_key(self, serve):
        serve(fake_response({"jobPostings": _ids(1)}))
        assert probes.PROBES["ashby"]("acme") == (True, 1)

    def test_bamboohr_asks_for_json(self, serve):
        seen = serve(fake_response({"result": _ids(4)}))
        assert probes.PROBES["bamboohr"]("acme") == (True, 4)
        assert seen[-1].headers["Accept"] == "application/json"

    def test_jazzhr_counts_apply_links_in_the_page(self, serve):
        serve(fake_response(text="<a href='/apply/AbC123/Engineer'>x</a>"
                          "<a href='/apply/dEf456/Scientist'>y</a>"))
        assert probes.PROBES["jazzhr"]("acme") == (True, 2)


class TestAnEmptyBoardIsAMiss:
    """A guessed slug confirms only by listing postings: an empty board
    gives the mission scorer nothing and the roster an empty row. (Prune's
    "is the board still there" check is `Board.alive`, which an empty
    board passes.)"""

    def test_greenhouse_empty_is_not_a_board(self, serve):
        serve(fake_response({"jobs": []}))
        assert probes.PROBES["greenhouse"]("acme") == (False, 0)

    def test_smartrecruiters_empty_is_not_a_board(self, serve):
        serve(fake_response({"totalFound": 0}))
        assert probes.PROBES["smartrecruiters"]("acme") == (False, 0)

    def test_smartrecruiters_with_postings_is(self, serve):
        serve(fake_response({"totalFound": 7}))
        assert probes.PROBES["smartrecruiters"]("acme") == (True, 7)

    def test_jazzhr_with_no_posting_links_is_not_a_board(self, serve):
        # Every JazzHR page links /apply/confirm/, postings or not.
        serve(fake_response(text="<a href='/apply/confirm/'>x</a>" * 2))
        assert probes.PROBES["jazzhr"]("acme") == (False, 0)


class TestFailureIsReportedNeverRaised:
    """A probe runs inside a discovery fan-out over hundreds of names. It
    reports (False, 0) for everything -- a bad status, a timeout, a
    payload that will not parse -- so one broken host cannot end the pass.
    """

    def test_a_non_200_is_a_miss(self, serve):
        serve(fake_response({"jobs": [{}]}, status=404))
        assert probes.PROBES["greenhouse"]("acme") == (False, 0)

    def test_unparseable_json_is_a_miss(self, serve):
        serve(fake_response(None))
        assert probes.PROBES["greenhouse"]("acme") == (False, 0)

    def test_a_raising_session_is_a_miss(self, serve):
        serve(OSError("connection reset"))
        assert probes.PROBES["greenhouse"]("acme") == (False, 0)

    def test_a_probe_does_not_retry(self, serve):
        calls = serve(OSError("throttled"))
        assert probes.PROBES["kula"]("acme") == (False, 0)
        assert len(calls) == 1


def test_every_registered_probe_is_callable():
    """PROBES is what sniffer._confirm_coords looks a sniffed ATS up in; a
    name in it with nothing behind it fails only in a live discovery run."""
    for ats, probe in probes.PROBES.items():
        assert callable(probe), ats


def test_a_hung_js_scrape_is_abandoned_at_the_budget(monkeypatch):
    """discover-local 2026-09-22 sat 338s silent in the JS pass: one name's
    scrape has to give up, and not hand its hung page to the next name."""
    class HungPage:
        url = ""

        def goto(self, *_a, **_k):
            time.sleep(1)

        def content(self):
            return ""

        def wait_for_load_state(self, *_a, **_k):
            pass

    monkeypatch.setattr(probes, "JS_PROBE_BUDGET_S", 0.1)
    with probes.WorkdayJsProbe() as js:
        monkeypatch.setattr(js, "_ensure_page", HungPage)
        stuck = js._executor
        t0 = time.monotonic()
        assert js.probe("Acme") == (None, "budget exceeded")
        assert time.monotonic() - t0 < 0.5
        assert js._executor is not stuck and js._page is None


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
        monkeypatch.setattr(board_for("greenhouse"), "alive",
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
        monkeypatch.setattr(board_for("lever"), "alive", lambda slug: (True, 5))

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
                  (f"unscored:refused:300:{iso_days_ago(31)[:10]}", jid))
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

    def test_padding_whitespace_does_not_look_like_a_changed_body(
            self, db, add_job, monkeypatch):
        """The marker's length -- and the length _unscored_due compares it
        against -- is the STRIPPED body, the same measure
        score_resume_fit's own MIN_DESC_CHARS check uses. Before this, the
        length recorded/compared here was the RAW body: added or removed
        whitespace around an otherwise-unchanged posting looked exactly
        like "the posting changed" and re-asked Claude about a row that
        had refused hours earlier."""
        calls = self._stub_refusal(monkeypatch)
        body = "x" * 300
        jid = add_job("j1", description=body, fit=None)
        db.execute(
            "UPDATE jobs SET fit_reason=?, description=? WHERE job_id=?",
            (f"unscored:refused:300:{datetime.now().date()}",
             "   " + body + "   ", jid))          # same content, padded
        db.commit()

        ops.self_heal_unscored(db, "resume", "local-tech")

        assert calls == [], "padding whitespace alone must not trigger a retry"

    def test_scoring_succeeds_once_due_and_replaces_the_marker(
            self, db, add_job, monkeypatch):
        import src.claude.fit as fit_module
        jid = add_job("j1", description="x" * 300, fit=None)
        db.execute("UPDATE jobs SET fit_reason=? WHERE job_id=?",
                  (f"unscored:refused:300:{iso_days_ago(31)[:10]}", jid))
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

    def test_a_dead_boards_stale_open_row_closes_without_a_probe(
            self, db, monkeypatch, capsys):
        [jid] = seed_stale(db, "Judi Health", ats="greenhouse",
                           miss_reason="board-dead:greenhouse", days_stale=20)
        probed = []

        def _probe(url, job_id=None):
            probed.append(url)
            return (None, "n/a")
        monkeypatch.setattr(ops.probe, "probe_job_open", _probe)

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
        [jid] = seed_stale(db, "Quiet Co", ats="lever", miss_reason=None,
                           days_stale=400)
        monkeypatch.setattr(ops.probe, "probe_job_open",
                            lambda url, job_id=None: (None, "n/a"))

        ops.check_closed_jobs(conn=db, stale_days=999)

        row = db.execute("SELECT status FROM jobs WHERE job_id=?",
                         (jid,)).fetchone()
        assert row["status"] != "closed"

    def test_a_miss_in_a_different_family_is_never_closed_this_way(
            self, db, monkeypatch):
        [jid] = seed_stale(db, "Ats Gap", ats=None,
                           miss_reason="ats-unsupported:ukg", days_stale=400)
        monkeypatch.setattr(ops.probe, "probe_job_open",
                            lambda url, job_id=None: (None, "n/a"))

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

    def test_250_stale_rows_are_fully_covered_in_three_passes_of_100(
            self, db, monkeypatch):
        seed_stale(db, n=250)
        # Worst case: every probe is unverifiable, so nothing ever leaves
        # the WHERE clause by closing -- rotation is the only thing that
        # can cover the backlog.
        monkeypatch.setattr(ops.probe, "probe_job_open",
                            lambda url, job_id=None: (None, "gated"))

        for _ in range(3):
            ops.check_closed_jobs(conn=db, stale_days=1, limit=100)

        covered = db.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE desc_checked_at IS NOT NULL").fetchone()["n"]
        assert covered == 250

    def test_a_single_pass_still_leaves_the_rest_for_next_time(self, db, monkeypatch):
        seed_stale(db, n=250)
        monkeypatch.setattr(ops.probe, "probe_job_open",
                            lambda url, job_id=None: (None, "gated"))

        ops.check_closed_jobs(conn=db, stale_days=1, limit=100)

        covered = db.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE desc_checked_at IS NOT NULL").fetchone()["n"]
        assert covered == 100


class TestClosedProbeGiveUp:
    """Rotation alone still re-probes a row that can NEVER be verified --
    a bot-gated host, a JS-only detail page, an ATS with no closure signal,
    a company with no resolved board at all -- for as long as it stays
    open. ops.CLOSED_PROBE_GIVE_UP consecutive unverifiable answers
    (jobs.probe_streak) take it out of the selection instead, without
    closing it."""

    @staticmethod
    def _answer(monkeypatch, verdict):
        """Serve one probe verdict and record every URL probed."""
        probed = []

        def _probe(url, job_id=None):
            probed.append(url)
            return verdict
        monkeypatch.setattr(ops.probe, "probe_job_open", _probe)
        return probed

    def _run(self, db, n):
        for _ in range(n):
            ops.check_closed_jobs(conn=db, stale_days=1)

    def _streak(self, db, job_id="Acme-j0"):
        return db.execute("SELECT COALESCE(probe_streak, 0) AS s FROM jobs "
                          "WHERE job_id=?", (job_id,)).fetchone()["s"]

    def test_ten_unverifiable_probes_drop_the_row_from_the_selection(
            self, db, monkeypatch):
        [jid] = seed_stale(db)
        probed = self._answer(monkeypatch, (None, "gated"))

        self._run(db, ops.CLOSED_PROBE_GIVE_UP)
        assert len(probed) == ops.CLOSED_PROBE_GIVE_UP
        assert self._streak(db) == ops.CLOSED_PROBE_GIVE_UP

        self._run(db, 3)                      # three further passes

        assert len(probed) == ops.CLOSED_PROBE_GIVE_UP, \
            "a given-up row must never be selected again"
        row = db.execute("SELECT status FROM jobs WHERE job_id=?",
                         (jid,)).fetchone()
        assert (row["status"] or "open") == "open", \
            "unverifiable is not closed: the row stays open, just unprobed"

    def test_three_unverifiable_probes_leave_the_row_in_the_queue(
            self, db, monkeypatch):
        seed_stale(db)
        probed = self._answer(monkeypatch, (None, "gated"))

        self._run(db, 3)
        assert self._streak(db) == 3

        self._run(db, 1)

        assert len(probed) == 4, "3 < CLOSED_PROBE_GIVE_UP: still selected"

    def test_a_confirmed_live_probe_resets_the_streak(self, db, monkeypatch):
        seed_stale(db)
        self._answer(monkeypatch, (None, "gated"))
        self._run(db, ops.CLOSED_PROBE_GIVE_UP - 1)
        assert self._streak(db) == ops.CLOSED_PROBE_GIVE_UP - 1

        probed = self._answer(monkeypatch, (True, "200"))
        self._run(db, 1)

        assert self._streak(db) == 0
        assert len(probed) == 1
        # ...and the row is still in the queue afterwards.
        self._answer(monkeypatch, (None, "gated"))
        self._run(db, 1)
        assert self._streak(db) == 1

    def test_a_board_sighting_puts_a_given_up_row_back_in_the_queue(
            self, db, monkeypatch):
        [jid] = seed_stale(db)
        self._answer(monkeypatch, (None, "gated"))
        self._run(db, ops.CLOSED_PROBE_GIVE_UP)
        assert self._streak(db) == ops.CLOSED_PROBE_GIVE_UP

        # The board lists it again: store.touch_job is the sighting, and
        # the probe history behind it is stale.
        store.touch_job(db, jid)

        assert self._streak(db) == 0

    def test_a_given_up_row_still_closes_when_its_board_dies(
            self, db, monkeypatch):
        """The give-up rule parks the URL PROBE, not the row: every other
        closure path still reaches it."""
        [jid] = seed_stale(db)
        self._answer(monkeypatch, (None, "gated"))
        self._run(db, ops.CLOSED_PROBE_GIVE_UP)
        db.execute("UPDATE jobs SET last_seen=? WHERE job_id=?",
                   (iso_days_ago(ops.DEAD_BOARD_CLOSE_DAYS + 6), jid))
        db.execute("UPDATE companies SET miss_reason='board-dead:greenhouse' "
                   "WHERE name='Acme'")
        db.commit()

        ops.check_closed_jobs(conn=db, stale_days=1)

        row = db.execute("SELECT status FROM jobs WHERE job_id=?",
                         (jid,)).fetchone()
        assert row["status"] == "closed"


# --- the per-JOB closure probe -------------------------------------------- #

LEVER_JOB = "https://jobs.lever.co/acme/2e1a8d40-0f2b-4c7e-9a11-5b6c7d8e9f01"
GH_JOB = "https://job-boards.greenhouse.io/acme/jobs/4277627009"
ASHBY_JOB = "https://jobs.ashbyhq.com/acme/f21013b3-0152-49d9-accb-3a46d33c8a82"
SR_JOB = "https://jobs.smartrecruiters.com/Acme/3743990014860306"
BAMBOO_JOB = "https://acme.bamboohr.com/careers/29"
JAZZ_JOB = "https://acme.applytojob.com/apply/8LvYWTHbW7/Data-Engineer"
ICIMS_JOB = "https://careers-acme.icims.com/jobs/42423/data-engineer/job?in_iframe=1"
INFOR_JOB = ("https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/"
             "JobPosting%5BJobPostingSet%5D%2842%2C207651%2C1%29"
             ".JobPostingDisplay?pagesize=1&csk.JobBoard=EXTERNAL"
             "&csk.HROrganization=42")
WD_JOB = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Durham-NC/Eng_R1"

#: The endpoint each family is expected to ask, keyed by job URL. A probe
#: that stopped calling its API would otherwise pass the closure tests by
#: falling through to the page.
FAMILY_API = {
    LEVER_JOB: "api.lever.co/v0/postings/acme/2e1a8d40",
    GH_JOB: "boards-api.greenhouse.io/v1/boards/acme/jobs/4277627009",
    ASHBY_JOB: "api.ashbyhq.com/posting-api/job-board/acme",
    SR_JOB: "api.smartrecruiters.com/v1/companies/Acme/postings/3743990014860306",
    BAMBOO_JOB: "acme.bamboohr.com/careers/29/detail",
    JAZZ_JOB: "acme.applytojob.com/apply/8LvYWTHbW7",
    INFOR_JOB: ".JobPostingDisplay?pageop=load",
    WD_JOB: "wday/cxs/acme/External/job/Durham-NC/Eng_R1",
}


@pytest.fixture
def probe_http(serve):
    """`serve` by URL fragment, where an unrouted URL fails the test
    rather than answering 404."""
    return lambda routes: serve(routes, strict=True)


class TestProbeIsDecisivePerFamily:
    """Lever, Greenhouse, Ashby, BambooHR, JazzHR and SmartRecruiters all
    serve a PULLED posting's own page as a plain HTTP 200 with no closure
    marker, so a page-only probe can neither close such a row nor confirm
    it -- 36 of 37 probes were "unverifiable" on 2026-09-21. Each family
    is asked its own public endpoint instead, and what these pin is which
    answer there counts as proof."""

    def test_a_404_from_the_lever_api_closes(self, probe_http):
        seen = probe_http({FAMILY_API[LEVER_JOB]: fake_response(status=404)})
        assert job_probe.probe_job_open(LEVER_JOB)[0] is False
        assert len(seen) == 1, "the page must not be fetched as well"

    def test_a_200_from_the_lever_api_confirms_live(self, probe_http):
        probe_http({FAMILY_API[LEVER_JOB]: fake_response({"text": "Eng"})})
        assert job_probe.probe_job_open(LEVER_JOB)[0] is True

    def test_a_404_from_the_greenhouse_api_closes(self, probe_http):
        probe_http({FAMILY_API[GH_JOB]: fake_response(status=404)})
        assert job_probe.probe_job_open(GH_JOB)[0] is False

    def test_a_404_from_the_bamboohr_detail_endpoint_closes(self, probe_http):
        probe_http({FAMILY_API[BAMBOO_JOB]: fake_response(status=404)})
        assert job_probe.probe_job_open(BAMBOO_JOB)[0] is False

    def test_a_410_from_the_jazzhr_apply_url_closes(self, probe_http):
        probe_http({FAMILY_API[JAZZ_JOB]: fake_response(status=410)})
        assert job_probe.probe_job_open(JAZZ_JOB)[0] is False

    def test_an_id_missing_from_a_non_empty_ashby_board_closes(self, probe_http):
        probe_http({FAMILY_API[ASHBY_JOB]: fake_response(
            {"jobs": [{"id": "aaaaaaaa-0000-0000-0000-000000000000"}]})})
        assert job_probe.probe_job_open(ASHBY_JOB)[0] is False

    def test_an_id_the_ashby_board_still_lists_is_live(self, probe_http):
        probe_http({FAMILY_API[ASHBY_JOB]: fake_response(
            {"jobs": [{"id": "f21013b3-0152-49d9-accb-3a46d33c8a82"}]})})
        assert job_probe.probe_job_open(ASHBY_JOB)[0] is True

    def test_an_empty_ashby_board_proves_nothing(self, probe_http):
        """A fetcher soft-fails to [], so an empty listing is
        indistinguishable from a board that did not answer -- the same
        reason store.sync_job_statuses refuses to close on one."""
        probe_http({FAMILY_API[ASHBY_JOB]: fake_response({"jobs": []}),
                    "jobs.ashbyhq.com": fake_response(url=ASHBY_JOB)})
        assert job_probe.probe_job_open(ASHBY_JOB)[0] is None

    def test_one_ashby_board_fetch_serves_every_row_on_it(self, probe_http):
        """A company with many stale rows must not re-fetch its board once
        per row."""
        seen = probe_http({FAMILY_API[ASHBY_JOB]: fake_response(
            {"jobs": [{"id": "aaaaaaaa-0000-0000-0000-000000000000"}]})})
        for _ in range(4):
            job_probe.probe_job_open(ASHBY_JOB)
        assert len(seen) == 1

    def test_smartrecruiters_closes_on_the_active_flag(self, probe_http):
        """SmartRecruiters keeps serving a pulled posting at HTTP 200, so
        the status code says nothing and `active` says everything."""
        probe_http({FAMILY_API[SR_JOB]: fake_response(
            {"id": "3743990014860306", "active": False})})
        assert job_probe.probe_job_open(SR_JOB)[0] is False

    def test_an_active_smartrecruiters_posting_is_live(self, probe_http):
        probe_http({FAMILY_API[SR_JOB]: fake_response(
            {"id": "3743990014860306", "active": True,
             "postingUrl": "https://jobs.smartrecruiters.com/Acme/"
                           "3743990014860306-data-engineer"})})
        assert job_probe.probe_job_open(SR_JOB)[0] is True

    def test_a_smartrecruiters_repost_is_not_a_verdict_on_this_row(
            self, probe_http):
        """A reposted requisition answers active=true under its
        SUCCESSOR's id (carried in postingUrl); that says the successor is
        live, not this row, so neither verdict is earned."""
        probe_http({FAMILY_API[SR_JOB]: fake_response(
            {"id": "3743990014860306", "active": True,
             "postingUrl": "https://jobs.smartrecruiters.com/Acme/"
                           "3743990015521846-data-engineer"}),
            "jobs.smartrecruiters.com/Acme/3743990014860306":
                fake_response(url=SR_JOB)})
        assert job_probe.probe_job_open(SR_JOB)[0] is None

    @staticmethod
    def _infor(end=None, **extra):
        """An Infor detail-form answer. The host returns HTTP 200 for a
        pulled posting as readily as for a live one, so every one of these
        is a 200 and only the BODY differs."""
        if end is None:
            return fake_response(extra)
        return fake_response({"fields": {"PostingDateRange_prd_End":
                                         {"value": end}}, **extra})

    def test_a_deleted_infor_record_closes(self, probe_http):
        """A pulled posting usually loses its record: the body says so
        while the status line still says 200."""
        seen = probe_http({FAMILY_API[INFOR_JOB]: self._infor(
            status="DOES_NOT_EXIST", statusCode=404)})
        is_open, reason = job_probe.probe_job_open(INFOR_JOB)
        assert is_open is False
        assert reason == "infor api: posting record gone"
        assert len(seen) == 1, "the page must not be fetched as well"

    def test_a_past_infor_posting_end_date_closes(self, probe_http):
        """The other half: the record survives the pull, with its posting
        window now closed (verified live -- every unlisted requisition that
        still had a record carried a years-stale end date)."""
        probe_http({FAMILY_API[INFOR_JOB]: self._infor(end="20220630")})
        is_open, reason = job_probe.probe_job_open(INFOR_JOB)
        assert is_open is False
        assert reason == "infor api: posting ended 2022-06-30"

    def test_an_open_ended_infor_posting_is_live(self, probe_http):
        probe_http({FAMILY_API[INFOR_JOB]: self._infor(end="00000000")})
        assert job_probe.probe_job_open(INFOR_JOB)[0] is True

    def test_an_infor_end_date_still_ahead_is_live(self, probe_http):
        """A posting that names a closing date is open until that date --
        closing it on the date's mere presence would close live rows."""
        ahead = (datetime.now() + timedelta(days=30)).strftime("%Y%m%d")
        probe_http({FAMILY_API[INFOR_JOB]: self._infor(end=ahead)})
        assert job_probe.probe_job_open(INFOR_JOB)[0] is True

    def test_an_infor_body_with_no_record_proves_nothing(self, probe_http):
        """Neither a record nor a DOES_NOT_EXIST verdict: unverifiable. The
        page fall-through cannot help either -- an Infor job URL serves the
        same JS shell whether the posting is live or long gone."""
        probe_http({FAMILY_API[INFOR_JOB]: self._infor(),
                    INFOR_JOB.split("?")[0]: fake_response(url=INFOR_JOB)})
        assert job_probe.probe_job_open(INFOR_JOB)[0] is None

    @pytest.mark.parametrize("info,is_open", [
        ({"title": "Engineer", "jobDescription": "<p>Build.</p>"}, True),
        ({}, False),                 # the record answers without a posting
        (None, False),               # no record at all
    ])
    def test_workday_is_open_while_its_record_names_the_posting(
            self, probe_http, info, is_open):
        """A pulled Workday posting's detail still answers 200."""
        probe_http({FAMILY_API[WD_JOB]: fake_response(
            {} if info is None else {"jobPostingInfo": info})})
        assert job_probe.probe_job_open(WD_JOB)[0] is is_open

    def test_icims_sends_the_headers_its_waf_accepts(self, probe_http):
        """iCIMS's WAF 405s the crawler's default Chrome-like UA; its spec's
        detail headers name one it accepts. 17 of the 36 unverifiable probes
        on 2026-09-21 were that 405, not a dead posting."""
        seen = probe_http({"careers-acme.icims.com": fake_response(url=ICIMS_JOB)})
        job_probe.probe_job_open(ICIMS_JOB)
        [sent] = [r.headers for r in seen]
        assert sent["User-Agent"] == PLAIN_HEADERS["User-Agent"]
        assert sent["User-Agent"] != HEADERS["User-Agent"]

    def test_a_pulled_icims_posting_answers_410(self, probe_http):
        probe_http({"careers-acme.icims.com": fake_response(status=410, url=ICIMS_JOB)})
        assert job_probe.probe_job_open(ICIMS_JOB)[0] is False

    def test_every_family_reports_the_endpoint_it_asked(self, probe_http):
        """The reason string is what the pass summary tallies, so it names
        the family that answered rather than a bare status."""
        for url, api in FAMILY_API.items():
            probe_http({api: fake_response(status=404)})
            reason = job_probe.probe_job_open(url)[1]
            assert job_probe.probe_family(url) in reason, url


class TestOnlyPositiveEvidenceCloses:
    """A host that refuses to answer has said nothing about the posting.
    Every one of these leaves the row OPEN and unverifiable."""

    @pytest.mark.parametrize("status", [403, 405, 429, 500, 503])
    @pytest.mark.parametrize("url", sorted(FAMILY_API))
    def test_a_refusal_never_closes(self, probe_http, url, status):
        # The page fall-through gets the same refusal: neither layer may
        # turn one into a closure.
        probe_http({FAMILY_API[url]: fake_response(status=status),
                    url.split("?")[0]: fake_response(status=status, url=url)})
        assert job_probe.probe_job_open(url)[0] is None

    @pytest.mark.parametrize("url", sorted(FAMILY_API))
    def test_a_timeout_never_closes(self, probe_http, url):
        probe_http({FAMILY_API[url]: OSError("read timed out"),
                    url.split("?")[0]: OSError("read timed out")})
        assert job_probe.probe_job_open(url)[0] is None

    def test_an_icims_405_is_unverifiable_not_closed(self, probe_http):
        probe_http({"careers-acme.icims.com": fake_response(status=405, url=ICIMS_JOB)})
        assert job_probe.probe_job_open(ICIMS_JOB)[0] is None

    def test_a_bot_gated_host_is_never_fetched_at_all(self, probe_http):
        seen = probe_http({})
        assert job_probe.probe_job_open(
            "https://www.linkedin.com/jobs/view/123")[0] is None
        assert seen == []


class TestAnUnreachableUrlCostsLittle:
    """The 2026-09-22 13:21 pass spent 20 instant ConnectionErrors on
    BioSpace rows whose stored URLs carried CR/LF/tab runs."""

    def test_embedded_whitespace_is_stripped_before_the_get(self, probe_http):
        seen = probe_http({"jobs.biospace.com/job/1/": fake_response(status=404)})
        assert job_probe.probe_job_open(
            "https://jobs.biospace.com \r\n\t/job/1/\r\n\r\n")[0] is False
        assert seen[0] == "https://jobs.biospace.com/job/1/"

    def test_a_refusing_host_is_asked_three_times_per_pass(self, probe_http):
        seen = probe_http({"dead.example": requests.ConnectionError()})
        reasons = [job_probe.probe_job_open(f"https://dead.example/job/{i}")[1]
                   for i in range(20)]
        assert len(seen) == 3
        assert reasons[-1] == "host unreachable this pass: skipped"


class TestProbeSelectionFollowsTheHarvestCadence:
    """A row is worth a GET when its own board WAS walked and no longer
    listed it, or when no board snapshot has ever ruled on it. A board on
    the long config.HARVEST_OFFMISSION_HOURS cadence (168h, against a
    7-day CLOSED_PROBE_STALE_DAYS) puts every one of its rows over the
    staleness line just before each walk; probing those says nothing the
    imminent walk will not say better."""

    @staticmethod
    def _probed(monkeypatch):
        urls = []
        monkeypatch.setattr(ops.probe, "probe_job_open",
                            lambda url, job_id=None: urls.append(url) or (None, "gated"))
        return urls

    def test_a_board_walked_inside_the_window_still_has_its_rows_probed(
            self, db, monkeypatch):
        # Walked two days ago and the row has been unseen for thirty: the
        # walk happened and did not list it.
        [jid] = seed_stale(db, "Walked", harvested=iso_days_ago(2))
        urls = self._probed(monkeypatch)

        ops.check_closed_jobs(conn=db, stale_days=7)

        assert urls == [f"https://acme.example/{jid}"]

    def test_a_board_whose_next_walk_is_not_due_yet_contributes_nothing(
            self, db, monkeypatch, capsys):
        # Off-mission and inactive, so it waits 168h; last walked just
        # past the 7-day staleness line, i.e. due but not yet run.
        seed_stale(db, "Deferred", harvested=iso_days_ago(8),
                   active=0, mission_tier="other")
        urls = self._probed(monkeypatch)

        ops.check_closed_jobs(conn=db, stale_days=7)

        assert urls == []
        assert "1 skipped: board not walked since" in capsys.readouterr().out

    def test_a_company_with_no_board_is_always_probed(self, db, monkeypatch):
        [jid] = seed_stale(db, "No Board", ats=None, harvested=None)
        urls = self._probed(monkeypatch)

        ops.check_closed_jobs(conn=db, stale_days=7)

        assert urls == [f"https://acme.example/{jid}"]

    def test_a_board_the_harvester_skips_is_always_probed(self, db, monkeypatch):
        """'no-board-found' keeps a company out of
        store.harvestable_companies, so its rows can never be answered by
        a board diff however recently the ats column was filled in."""
        [jid] = seed_stale(db, "Unresolved", harvested=iso_days_ago(90),
                           miss_reason="no-board-found")
        urls = self._probed(monkeypatch)

        ops.check_closed_jobs(conn=db, stale_days=7)

        assert urls == [f"https://acme.example/{jid}"]


class TestProbeOutcomesAreReportedPerFamily:
    """"36 unverifiable" named nothing an audit could act on. The summary
    breaks the pass down by ATS family (by host for what no family claims)
    with the reasons behind each."""

    def test_each_family_gets_a_line_with_its_counts_and_reasons(
            self, db, monkeypatch, capsys):
        seed_stale(db, harvested=iso_days_ago(1),
                   urls=[LEVER_JOB, LEVER_JOB + "x", ICIMS_JOB,
                         "https://www.linkedin.com/jobs/view/1"])
        verdicts = {LEVER_JOB: (True, "lever api: posting live"),
                    LEVER_JOB + "x": (False, "lever api HTTP 404"),
                    ICIMS_JOB: (None, "HTTP 405"),
                    "https://www.linkedin.com/jobs/view/1":
                        (None, "bot-gated aggregator host")}
        monkeypatch.setattr(ops.probe, "probe_job_open",
                            lambda url, job_id=None: verdicts[url])

        ops.check_closed_jobs(conn=db, stale_days=7)

        out = capsys.readouterr().out
        assert "1 closed, 1 confirmed live, 2 unverifiable" in out
        assert "lever            1 live, 1 closed" in out
        assert "icims            1 unverifiable [HTTP 405]" in out
        assert "www.linkedin.com 1 unverifiable [bot-gated aggregator host]" in out

    def test_a_quoted_page_phrase_is_detail_not_a_reason_of_its_own(
            self, db, monkeypatch, capsys):
        """Two rows closed by the same kind of notice must tally as one
        reason, not as two singletons named after their own wording."""
        seed_stale(db, harvested=iso_days_ago(1),
                   urls=[ICIMS_JOB, ICIMS_JOB + "&x=1"])
        monkeypatch.setattr(
            ops.probe, "probe_job_open",
            lambda url, job_id=None: (False, f"page says {url[-12:]!r}"))

        ops.check_closed_jobs(conn=db, stale_days=7)

        assert "icims            2 closed [page says ... x2]" in \
            capsys.readouterr().out
