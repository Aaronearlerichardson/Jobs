"""Model-aware deep verification.

A finalist used to be skipped by verify_top for life once its fit_reason
carried the 'deep:' marker, so switching CLAUDE_VERIFY_MODEL never
re-read the rows the old model had scored. Every score now records the
model that wrote it (jobs.fit_model); the default pass re-verifies only
finalists the CURRENT verify model has not checked, and `force` redoes
the whole top N (the "re-verify all" tick box in the web UI).

Offline: the verifier and the live-JD fetch are stubbed.
"""

from core import fit, store
from scrapers import ops


def _use_model(monkeypatch, name):
    monkeypatch.setattr(fit.config, "CLAUDE_VERIFY_MODEL", name)


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
            return {"function": 0.8, "domain": 0.8, "stack": 0.8,
                    "seniority": 0.8, "gates": [], "reason": "fine"}

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

    def _track(self, local_track):
        return dict(local_track, min_mission=0.0, rank_by="fit",
                    remote_mission_floor=None)

    def test_default_pass_leaves_current_model_rows_alone(
            self, db, add_job, local_track, monkeypatch):
        t = self._track(local_track)
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
        t = self._track(local_track)
        self._seed(add_job, t)
        n, calls = self._run(db, monkeypatch, t, force=True)
        assert n == 4
        # The second round finds nothing stale even under force: every row
        # now carries the current model AND was re-read this run.
        assert len(calls) == 4

    def test_a_second_default_pass_is_free(
            self, db, add_job, local_track, monkeypatch):
        t = self._track(local_track)
        self._seed(add_job, t)
        self._run(db, monkeypatch, t)
        n, calls = self._run(db, monkeypatch, t)
        assert (n, calls) == (0, [])


class TestWebOpPassesTheTickBox:
    def test_verify_op_forwards_force(self, monkeypatch):
        from webapp import ops as web_ops
        seen = {}
        monkeypatch.setattr(web_ops.maint, "verify_top_cli",
                            lambda **kw: seen.update(kw))
        web_ops.OPS["verify"]["fn"]({"top": "5", "force": True})
        assert seen["force"] is True and seen["top_n"] == 5
        web_ops.OPS["verify"]["fn"]({"top": "5"})
        assert seen["force"] is False
