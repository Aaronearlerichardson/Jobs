"""Model-aware deep verification.

A finalist used to be skipped by verify_top for life once its fit_reason
carried the 'deep:' marker, so switching CLAUDE_VERIFY_MODEL never
re-read the rows the old model had scored. Every score now records the
model that wrote it (jobs.fit_model); the default pass re-verifies only
finalists the CURRENT verify model has not checked, and `force` redoes
the whole top N (the "re-verify all" tick box in the web UI).

Offline: the verifier and the live-JD fetch are stubbed.
"""

from src import store
from src.claude import fit
from src.ops import maintenance as ops


def _use_model(monkeypatch, name):
    monkeypatch.setattr(fit.config, "CLAUDE_VERIFY_MODEL", name)


def _track(local_track):
    """The local track ranked by fit alone, with no mission floors."""
    return dict(local_track, min_mission=0.0, rank_by="fit",
                remote_mission_floor=None)


class TestFitResultCarriesTheModel:
    def test_as_columns_names_the_model_and_nulls_when_unknown(self):
        cols = fit.FitResult(score=0.5, axes={a: 0.5 for a in fit.AXES},
                             model="m-1").as_columns()
        assert cols["fit_model"] == "m-1"
        assert "fit_model" in store._SCORE_COLS
        assert fit.FitResult(score=None).as_columns()["fit_model"] is None

    def test_verify_fit_stamps_the_verify_model(self, monkeypatch):
        _use_model(monkeypatch, "m-verify")
        seen = {}

        def fake(system, user, **kw):
            seen["model"] = kw.get("model")
            return fit.VerifyReply(
                years_required=None, seat_type="ic-engineering",
                must_haves=[], candidate_gaps=[], function=0.8, domain=0.8,
                stack=0.8, seniority=0.8, gates=[], reason="fine")

        monkeypatch.setattr(fit, "call_claude_json", fake)
        res = fit.verify_fit("Data Engineer", "x" * (fit.MIN_DESC_CHARS + 10))
        assert seen["model"] == "m-verify"
        assert res.model == "m-verify"
        assert res.reason.startswith("deep:")

    def test_verify_model_falls_back_to_the_screen_model(self, monkeypatch):
        _use_model(monkeypatch, None)
        monkeypatch.setattr(fit.config, "CLAUDE_MODEL", "m-screen")
        assert fit.verify_model() == "m-screen"


class TestStorePersistsTheModel:
    def test_upsert_and_update_round_trip(self, db, add_job):
        add_job("gh_acme_1", fit=0.6, fit_reason="deep: a", fit_model="m-old")
        row = db.execute("SELECT fit_model FROM jobs WHERE job_id='gh_acme_1'"
                         ).fetchone()
        assert row["fit_model"] == "m-old"
        store.update_job_scores(db, "gh_acme_1", fit.FitResult(
            score=0.7, axes={a: 0.7 for a in fit.AXES}, reason="deep: b",
            model="m-new").as_columns())
        row = db.execute("SELECT fit_model, fit_reason FROM jobs "
                         "WHERE job_id='gh_acme_1'").fetchone()
        assert row["fit_model"] == "m-new"
        assert "deep: b" in row["fit_reason"]      # summary() prefixes the axis tag


class TestVerifyTopSkipsOnlyCurrentModelRows:
    """Four finalists: never verified, verified by an older model, verified
    before fit_model existed (NULL), verified by the current model."""

    def _seed(self, add_job, t):
        add_job("gh_acme_fresh", fit=0.9, track=t["track"],
                description="d" * 400)
        add_job("gh_acme_old", fit=0.8, track=t["track"],
                description="d" * 400, fit_reason="deep: old", fit_model="m-old")
        add_job("gh_acme_null", fit=0.7, track=t["track"],
                description="d" * 400, fit_reason="deep: pre-column")
        add_job("gh_acme_cur", fit=0.6, track=t["track"],
                description="d" * 400, fit_reason="deep: current",
                fit_model="m-new")

    def _run(self, db, monkeypatch, t, **kw):
        _use_model(monkeypatch, "m-new")
        calls = []

        def fake_verify(title, text, *, location=""):
            calls.append(title)
            return fit.FitResult(score=0.75, axes={a: 0.75 for a in fit.AXES},
                                 reason="deep: re-read", model="m-new")

        monkeypatch.setattr(fit, "verify_fit", fake_verify)
        monkeypatch.setattr(ops, "_live_jd", lambda r: r.get("description") or "")
        n = ops.verify_top(top_n=10, max_workers=1, conn=db, t=t, **kw)
        return n, calls

    def test_default_pass_leaves_current_model_rows_alone(
            self, db, add_job, local_track, monkeypatch):
        t = _track(local_track)
        self._seed(add_job, t)
        n, calls = self._run(db, monkeypatch, t)
        assert n == 3 and len(calls) == 3
        models = {r["job_id"]: r["fit_model"] for r in
                  db.execute("SELECT job_id, fit_model FROM jobs")}
        assert set(models.values()) == {"m-new"}
        cur = db.execute("SELECT fit_reason FROM jobs WHERE job_id='gh_acme_cur'"
                         ).fetchone()["fit_reason"]
        assert cur == "deep: current"            # untouched

    def test_force_re_verifies_every_finalist(
            self, db, add_job, local_track, monkeypatch):
        t = _track(local_track)
        self._seed(add_job, t)
        n, calls = self._run(db, monkeypatch, t, force=True)
        assert n == 4
        # The second round finds nothing stale even under force: every row
        # now carries the current model AND was re-read this run.
        assert len(calls) == 4

    def test_past_the_head_rows_under_the_floor_are_not_verified(
            self, db, add_job, local_track, monkeypatch):
        """2026-09-22: top-200 runs spent ~20 Opus calls on rows stored at
        0.19-0.24. The first VERIFY_HEAD ranks are checked whatever their
        fit; past them a stale row needs verify_floor (0.25)."""
        t = _track(local_track)
        for i in range(ops.VERIFY_HEAD):
            add_job(f"gh_head_{i}", fit=0.9 - i / 100, track=t["track"],
                    description="d" * 400, fit_reason="deep: current",
                    fit_model="m-new")
        add_job("gh_acme_above", fit=0.3, track=t["track"],
                description="d" * 400)
        add_job("gh_acme_below", fit=0.2, track=t["track"],
                description="d" * 400)
        _use_model(monkeypatch, "m-new")
        seen = []
        monkeypatch.setattr(fit, "verify_fit", lambda title, text, **k:
                            seen.append(text) or fit.FitResult(
                                score=0.3, reason="deep: v", model="m-new"))
        monkeypatch.setattr(ops, "_live_jd", lambda r: r["job_id"])
        ops.verify_top(top_n=30, max_workers=1, conn=db, t=t, rounds=1)
        assert seen == ["gh_acme_above"]
        ops.verify_top(top_n=30, max_workers=1, conn=db, t=t, rounds=1,
                       force=True)
        assert "gh_acme_below" in seen

    def test_a_second_default_pass_is_free(
            self, db, add_job, local_track, monkeypatch):
        t = _track(local_track)
        self._seed(add_job, t)
        self._run(db, monkeypatch, t)
        n, calls = self._run(db, monkeypatch, t)
        assert (n, calls) == (0, [])


class TestWebOpPassesTheTickBox:
    def test_verify_op_forwards_force(self, monkeypatch):
        from src.ops import background as web_ops
        seen = {}
        # The registry resolves "src.ops.maintenance:verify_top_cli" at call time,
        # so patching the target module is enough.
        monkeypatch.setattr("src.ops.maintenance.verify_top_cli",
                            lambda **kw: seen.update(kw))
        web_ops.OPS["verify"]["fn"]({"top": "5", "force": True})
        assert seen["force"] is True and seen["top_n"] == 5
        web_ops.OPS["verify"]["fn"]({"top": "5"})
        assert seen["force"] is False


class TestVerifyTopStopsWhenTheApiIsDisabled:
    """A tripped breaker (src.claude) used to leak through as 121 '[?] kept
    ... unverified' lines per round, two rounds, every row's live JD fetched
    for nothing (2026-09-09, twice). Now: one line, no fetches, no round 2."""

    def _seed(self, add_job, t, n=4):
        for i in range(n):
            add_job(f"gh_acme_{i}", fit=0.9 - i / 100, track=t["track"],
                    description="d" * 400)

    def test_tripped_before_the_pass_skips_it_in_one_line(
            self, db, add_job, local_track, monkeypatch, capsys):
        from src.claude import api
        t = _track(local_track)
        self._seed(add_job, t)
        _use_model(monkeypatch, "m-new")
        monkeypatch.setattr(api, "_FATAL_MSG", "HTTP 400: 'credit balance'")
        fetched, verified = [], []
        monkeypatch.setattr(ops, "_live_jd",
                            lambda r: fetched.append(r["job_id"]) or "")
        monkeypatch.setattr(fit, "verify_fit",
                            lambda *a, **k: verified.append(a)
                                            or fit.FitResult(score=None))
        n = ops.verify_top(top_n=10, max_workers=1, conn=db, t=t)
        out = capsys.readouterr().out
        assert (n, fetched, verified) == (0, [], [])
        assert out.count("deep verify skipped") == 1
        assert "[?] kept" not in out

    def test_tripping_mid_round_halts_without_fetching_the_rest(
            self, db, add_job, local_track, monkeypatch, capsys):
        from src.claude import api
        t = _track(local_track)
        self._seed(add_job, t)
        _use_model(monkeypatch, "m-new")
        monkeypatch.setattr(api, "_FATAL_MSG", None)
        fetched = []
        monkeypatch.setattr(ops, "_live_jd",
                            lambda r: fetched.append(r["job_id"]) or "d" * 400)

        def dead_api(title, text, *, location=""):
            api._trip_fatal("HTTP 400: 'credit balance'")
            return fit.FitResult(score=None, reason="unverified")

        monkeypatch.setattr(fit, "verify_fit", dead_api)
        n = ops.verify_top(top_n=10, max_workers=1, conn=db, t=t)
        out = capsys.readouterr().out
        assert n == 0
        assert len(fetched) == 1                 # only the row that tripped it
        assert out.count("deep verify halted") == 1
        assert "round 2/2" not in out
        assert "[?] kept" not in out


class TestVerifyFloorCandidates:
    """verify_top spends the budget its stale top-N rows leave unused on
    _verify_floor_candidates (whose doctest says who qualifies): the
    2026-09-09 case, a row the screen put at 0.16 and a deep read at 0.50,
    was under digest_min_fit and so never near the top."""

    def _fit(self, db, add_job, t, job_id, score, **overrides):
        """A row triage scored under digest_min_fit, labelled as triage
        labels one."""
        add_job(job_id, fit=score, track=t["track"], description="d" * 400,
                **overrides)
        store.record_triage(db, job_id, "fit", f"{t['track']}=ok",
                            tracks=[t["track"]])

    def _fill_top_n(self, add_job, t, n=2):
        """`n` already-current rows that outscore every candidate, so the
        top-n slice spends nothing and a candidate is reached only through
        the floor rule."""
        for i in range(n):
            add_job(f"gh_ok_{i}", fit=0.9 + i / 100, track=t["track"],
                    description="d" * 400, fit_reason="deep: current",
                    fit_model="m-new")

    def _verify(self, db, monkeypatch, t, score, reason="deep: v", **kw):
        """verify_top with the verifier answering `score`: (n, calls)."""
        _use_model(monkeypatch, "m-new")
        calls = []
        monkeypatch.setattr(fit, "verify_fit", lambda *a, **k: calls.append(a)
                            or fit.FitResult(score=score, reason=reason,
                                             model="m-new"))
        monkeypatch.setattr(ops, "_live_jd", lambda r: r.get("description") or "")
        return ops.verify_top(max_workers=1, conn=db, t=t, **kw), calls

    def _row(self, db, job_id):
        return tuple(db.execute(
            "SELECT resume_fit_score, triage_status FROM jobs WHERE job_id=?",
            (job_id,)).fetchone())

    def test_a_candidate_reaching_digest_min_fit_is_relabelled_ok(
            self, db, add_job, local_track, monkeypatch, local_addr):
        t = _track(local_track)
        self._fit(db, add_job, t, "gh_acme_fit", 0.16, location=local_addr)
        n, _ = self._verify(db, monkeypatch, t, 0.5, top_n=10)
        assert n == 1
        # 0.5 clears the track's digest_min_fit (0.4 by default).
        assert self._row(db, "gh_acme_fit") == (0.5, "ok")

    def test_a_candidate_staying_under_digest_min_fit_keeps_fit(
            self, db, add_job, local_track, monkeypatch, local_addr):
        t = _track(local_track)
        self._fit(db, add_job, t, "gh_acme_weak", 0.16, location=local_addr)
        self._verify(db, monkeypatch, t, 0.2, top_n=10)
        assert self._row(db, "gh_acme_weak") == (0.2, "fit")

    def test_a_row_under_the_floor_is_not_a_candidate(
            self, db, add_job, local_track, monkeypatch, local_addr):
        t = _track(local_track)
        self._fill_top_n(add_job, t)
        self._fit(db, add_job, t, "gh_acme_toolow", 0.1, location=local_addr)
        assert self._verify(db, monkeypatch, t, 0.9, top_n=2) == (0, [])

    def test_a_candidate_the_current_model_verified_is_skipped(
            self, db, add_job, local_track, monkeypatch, local_addr, capsys):
        t = _track(local_track)
        self._fit(db, add_job, t, "gh_acme_seen", 0.3, location=local_addr,
                  fit_reason="deep: already", fit_model="m-new")
        assert self._verify(db, monkeypatch, t, 0.9, top_n=10) == (0, [])
        assert (f"deep-verify [{t['track']}]: nothing new in the top 10"
                in capsys.readouterr().out)

    def test_candidates_fill_only_the_slots_the_top_n_left(
            self, db, add_job, local_track, monkeypatch, local_addr):
        """The top-2 slice is all current, so both slots go to the two
        best candidates, and the third waits."""
        t = _track(local_track)
        self._fill_top_n(add_job, t, n=3)
        for job_id, score in (("gh_fit_a", 0.30), ("gh_fit_b", 0.28),
                              ("gh_fit_c", 0.26)):
            self._fit(db, add_job, t, job_id, score, location=local_addr)
        n, _ = self._verify(db, monkeypatch, t, 0.1, top_n=2, rounds=1)
        verified = {r["job_id"] for r in db.execute(
            "SELECT job_id FROM jobs WHERE fit_reason='deep: v'")}
        assert n == 2
        assert verified == {"gh_fit_a", "gh_fit_b"}

    def test_verified_row_prints_old_new_score_company_title_reason(
            self, db, add_job, local_track, monkeypatch, capsys):
        t = _track(local_track)
        add_job("gh_acme_1", "Data Engineer", fit=0.6, track=t["track"],
                description="d" * 400)
        self._verify(db, monkeypatch, t, 0.7, reason="deep: solid fit",
                     top_n=10)
        out = capsys.readouterr().out
        assert "0.60 -> 0.70, Acme, Data Engineer, solid fit" in out
