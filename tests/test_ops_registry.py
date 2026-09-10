"""The one operation table behind the web UI's buttons and the CLIs' flags.

Three front ends used to carry their own dispatch over the same functions
and drifted (a UI helper was lost in a refactor while its button survived;
the CLI and the UI passed different defaults to one call). These tests pin
the properties that make a single table safe: every target resolves,
every front end reaches the registry, and the params each front end
passes are the ones the target accepts.
"""

import inspect
from types import SimpleNamespace

import pytest

import config
import discover
import run_scraper
from core import ops_registry
from core.ops_registry import OMIT, Param, build_kwargs


class TestTable:
    def test_every_entry_is_well_formed(self):
        for name, e in ops_registry.REGISTRY.items():
            assert set(e) >= {"label", "engine", "target", "params"}, name
            assert e["engine"] in (None, "local", "sweep"), name
            assert ":" in e["target"], name
            assert all(isinstance(p, Param) for p in e["params"]), name

    def test_every_target_resolves_to_a_callable(self):
        for name, e in ops_registry.REGISTRY.items():
            assert callable(ops_registry.resolve(e["target"])), name

    def test_every_param_names_a_real_keyword_of_its_target(self):
        """A typo in a Param.kw would surface only when someone clicked
        the button: the call would raise TypeError inside the op thread."""
        for name, e in ops_registry.REGISTRY.items():
            sig = inspect.signature(ops_registry.resolve(e["target"]))
            accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD
                              for p in sig.parameters.values())
            for p in e["params"]:
                kw = p.kw or p.key
                assert accepts_any or kw in sig.parameters, (name, kw)

    def test_required_target_arguments_are_always_supplied(self):
        """A target parameter with no default must come from a Param that
        always produces a value (a default, or a kind that never omits)."""
        never_omits = {"track", "db_path", "veto", "assert", "not"}
        for name, e in ops_registry.REGISTRY.items():
            sig = inspect.signature(ops_registry.resolve(e["target"]))
            supplied = {(p.kw or p.key) for p in e["params"]
                        if p.default is not OMIT or p.kind in never_omits}
            required = {n for n, prm in sig.parameters.items()
                        if prm.default is inspect.Parameter.empty
                        and prm.kind not in (inspect.Parameter.VAR_KEYWORD,
                                             inspect.Parameter.VAR_POSITIONAL)}
            assert required <= supplied, (name, required - supplied)

    def test_ui_view_hides_only_the_flagged_entries(self):
        hidden = {n for n, e in ops_registry.REGISTRY.items()
                  if e.get("ui") is False}
        assert hidden, "expected at least one CLI-only op"
        assert set(ops_registry.ui_ops()) == set(ops_registry.REGISTRY) - hidden


class TestInvoke:
    def test_resolves_the_track_from_params_or_the_default(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("scrapers.ops.sync_status_all",
                            lambda **kw: seen.update(kw))
        ops_registry.invoke("sync", {"top": "7"})
        assert seen == {"top_n": 7, "t": config.UI_TRACKS[config.DEFAULT_TRACK]}
        ops_registry.invoke("sync", {}, track=None)
        assert seen == {"top_n": 15, "t": None}

    def test_honors_a_monkeypatched_target_at_call_time(self, monkeypatch):
        """Targets are looked up when invoked, not captured at import, so
        tests (and reloads) see the current function."""
        calls = []
        monkeypatch.setattr("core.ops_targets.dedup", lambda **kw: calls.append(kw))
        ops_registry.invoke("dedup", {}, track=None)
        assert calls == [{"t": None}]

    def test_unknown_op_raises(self):
        with pytest.raises(KeyError):
            ops_registry.invoke("no-such-op", {})


class TestParamCoercion:
    def test_blank_web_fields_fall_back_like_the_old_int_helper(self):
        spec = [Param("limit", kind="int", default=None),
                Param("top", "top_n", "int", 15)]
        assert build_kwargs(spec, {"limit": "", "top": ""}, None) == \
            {"limit": None, "top_n": 15}

    def test_absent_without_default_is_omitted(self):
        assert build_kwargs([Param("workers", "max_workers", "int")], {}, None) == {}

    def test_explicit_false_is_a_value_not_an_absence(self):
        """argparse hands over False for an unset store_true flag; that is
        a real value and must reach the target (rescore's described_only)."""
        spec = [Param("described_only", kind="bool", default=True)]
        assert build_kwargs(spec, {"described_only": False}, None) == \
            {"described_only": False}


class TestWebView:
    def test_ops_is_the_ui_subset_with_the_legacy_shape(self):
        import webapp
        assert set(webapp.OPS) == set(ops_registry.ui_ops())
        for name, o in webapp.OPS.items():
            assert set(o) == {"label", "engine", "fn"}, name
            assert o["label"] == ops_registry.REGISTRY[name]["label"]

    def test_fn_runs_the_registry_with_the_posted_params(self, monkeypatch):
        import webapp
        seen = {}
        monkeypatch.setattr("scrapers.ops.check_closed_jobs",
                            lambda **kw: seen.update(kw))
        webapp.OPS["check-closed"]["fn"]({"stale_days": "3", "limit": "",
                                          "track": config.DEFAULT_TRACK})
        assert seen["stale_days"] == 3 and seen["limit"] is None
        assert seen["t"]["id"] == config.DEFAULT_TRACK
        assert "max_workers" not in seen        # the target's own default


def _args(**given):
    """An argparse-shaped namespace: every dest a command reads, defaulted
    the way run_scraper's parser defaults it, with `given` overriding."""
    base = dict(workers=6, top=15, limit=None, stale_days=2, score_cap=None,
                described_only=False, verify_all=False, miss_days=None,
                prune_offmission=False, no_fit=False, preview=False,
                send=False, no_verify=False, no_websearch=False,
                confirm_cost=False, samples=5)
    base.update(given)
    return SimpleNamespace(**base)


class TestCliDispatch:
    """run_scraper's flag table is generated over the registry: every
    registry-backed flag reaches invoke() with the parameters the old
    if-chain passed by hand."""

    @pytest.fixture
    def calls(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ops_registry, "invoke",
                            lambda name, params, track=None: seen.append((name, params, track)))
        return seen

    def _handler(self, dest):
        return dict(run_scraper._COMMANDS)[dest]

    def test_verify_top_passes_n_workers_and_force(self, calls):
        self._handler("verify_top")(_args(verify_top=20, verify_all=True), None)
        assert calls == [("verify", {"top": 20, "workers": 6, "force": True}, None)]

    def test_check_closed_passes_the_probe_knobs(self, calls):
        t = {"id": "x"}
        self._handler("check_closed")(_args(limit=40, stale_days=5), t)
        assert calls == [("check-closed",
                          {"workers": 6, "limit": 40, "stale_days": 5}, t)]

    def test_triage_forwards_score_cap(self, calls):
        self._handler("triage")(_args(score_cap=50), None)
        assert calls[0][1] == {"limit": None, "workers": 6, "score_cap": 50}

    def test_every_registry_backed_flag_names_a_registered_op(self, calls):
        for dest, handler in run_scraper._COMMANDS:
            calls.clear()
            try:
                handler(_args(**{dest: True, "verify_top": 15,
                                 "reresolve_misses": 50, "nlx": "A,B"}), None)
            except Exception:
                continue          # the non-registry commands touch the store
            for name, _params, _t in calls:
                assert name in ops_registry.REGISTRY, (dest, name)

    def test_selected_treats_zero_as_given(self):
        assert run_scraper._selected(_args(verify_top=0), "verify_top")
        assert not run_scraper._selected(_args(verify_top=None), "verify_top")
        assert not run_scraper._selected(_args(rescore=False), "rescore")


class TestDiscoverDispatch:
    def test_flags_map_onto_registry_ops(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ops_registry, "invoke",
                            lambda name, params, track=None: seen.append((name, params)))
        cmds = dict(discover.__dict__["_COMMANDS"])
        cmds["add_board"](SimpleNamespace(add_board=["Acme", "https://x"], capture=True))
        cmds["rescore_missions"](SimpleNamespace(rescore_missions=True))
        cmds["dork"](SimpleNamespace())
        assert seen == [
            ("add-board", {"name": "Acme", "url": "https://x", "capture": True}),
            ("score-missions", {"rescore": True}),
            ("dork", {}),
        ]
