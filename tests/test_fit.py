"""Résumé-fit rubric and the Claude payload shape. No API calls: every
assertion is about prompt construction, clipping, and arithmetic."""

import pytest

import src.claude.api as claude
import src.claude.fit as fit


class TestClipping:
    def test_clip_keeps_the_requirements_tail(self):
        # Corporate JDs put requirements LAST — a head-only truncation is
        # what let a TPM posting score on its mission boilerplate.
        long_jd = "INTRO " + ("boilerplate " * 2000) + "REQUIREMENTS: 8+ years TPM"
        clipped = fit.clip_desc(long_jd, max_chars=5000)
        assert clipped.endswith("REQUIREMENTS: 8+ years TPM")
        assert "elided" in clipped

    def test_short_text_passes_through(self):
        assert fit.clip_desc("short jd", max_chars=5000) == "short jd"


class TestGates:
    def test_management_gate_registered(self):
        assert "management" in fit.GATES

    def test_management_gate_bites(self):
        axes = dict(domain=.35, function=.30, stack=.35, seniority=.45)
        assert fit.combine(axes, ["management"]) < fit.combine(axes, []) * 0.5

    def test_profile_penalties_merge_rather_than_replace(self, cfg):
        saved = getattr(cfg, "FIT_GATE_PENALTY", None)
        cfg.FIT_GATE_PENALTY = {"geo": 0.10}       # a pre-management profile
        try:
            merged = fit._effective_penalties()
        finally:
            cfg.FIT_GATE_PENALTY = saved
        assert merged["geo"] == 0.10               # profile wins where set
        assert merged["management"] == 0.35        # default survives

    def test_clearance_regex_needs_a_held_clearance(self):
        assert fit._clearance_required("must have an active TS/SCI clearance")
        # "able to obtain" is not a disqualifier for a clearable citizen.
        assert not fit._clearance_required("eligible to obtain a clearance")


class TestUnscoredCause:
    """The vocabulary src.ops.maintenance's retry-marker system keys off:
    the two "gave up without a score" reasons score_resume_fit hands back,
    each mapped to what a retry policy needs (does this body just need to
    GROW, or might the exact same call succeed later)."""

    # The three reasons score_resume_fit itself produces are unscored_cause's
    # own doctests; what needs a test here is everything around them.

    def test_an_unrecognized_reason_is_not_a_verdict_either(self):
        assert fit.unscored_cause("some future reason") is None

    def test_a_body_long_enough_to_reach_the_api_never_takes_the_short_path(
            self, monkeypatch):
        # self_heal_unscored only calls in here once its OWN query has
        # already guaranteed length >= MIN_DESC_CHARS; this pins that the
        # "no description; unscored" branch genuinely cannot ALSO fire in
        # that case, which is what makes treating a bare "unscored" as the
        # REFUSED class (rather than needing the exact HTTP-level cause
        # from src.claude.api) sound.
        monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: {})
        res = fit.score_resume_fit("T", "x" * fit.MIN_DESC_CHARS)
        assert res.score is None
        assert fit.unscored_cause(res.reason) == "refused"

    def test_a_scorer_that_never_asked_is_not_a_refusal(self, monkeypatch):
        # No key, and a tripped breaker, both make call_claude_json return
        # {} WITHOUT asking the model. Reporting those as "unscored" would
        # let ops.maintenance's retry marker hold a perfectly scorable row
        # for UNSCORED_RETRY_DAYS over one billing hiccup.
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: {})
        monkeypatch.setattr("src.config.ANTHROPIC_API_KEY",
                            "YOUR_ANTHROPIC_API_KEY_HERE")
        res = fit.score_resume_fit("T", "x" * fit.MIN_DESC_CHARS)
        assert (res.score, res.reason) == (None, "scorer unavailable")
        assert fit.unscored_cause(res.reason) is None

        monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(fit, "api_disabled", lambda: "credit balance")
        res = fit.score_resume_fit("T", "x" * fit.MIN_DESC_CHARS)
        assert fit.unscored_cause(res.reason) is None


class TestPrompts:
    def test_verify_prompt_extracts_requirements(self):
        prompt = fit.build_verify_prompt()
        assert all(k in prompt for k in
                   ("years_required", "seat_type", "candidate_gaps"))

    def test_verify_refuses_stub_descriptions(self):
        assert fit.verify_fit("T", "too short").score is None

    @pytest.mark.parametrize("scorer", [fit.score_resume_fit, fit.verify_fit])
    def test_the_stored_location_reaches_the_user_turn_only(self, monkeypatch,
                                                             scorer):
        # Both scorers build the turn with fit._user_turn, whose doctest pins
        # the rendering. This pins that each one hands the location over,
        # and that it never reaches the cached system prompt.
        seen = {}

        def fake(system, user, **kw):
            seen.update(system=system, user=user)
            return {}                       # -> unscored, score None

        monkeypatch.setattr(fit, "call_claude_json", fake)
        body = "x" * (fit.MIN_DESC_CHARS + 10)
        scorer("ML Engineer", body, location="Nowhereville, TX")
        assert "JOB LOCATION (stored): Nowhereville, TX\n" in seen["user"]
        assert "Nowhereville" not in seen["system"]
        # ...while the rule for reading that line is in both prompts.
        assert '"JOB LOCATION (stored)" line' in seen["system"]
        scorer("ML Engineer", body)          # no location -> no line
        assert "JOB LOCATION" not in seen["user"]


class TestPromptCache:
    """The cache breakpoint must sit on the STABLE system prompt, never on
    the per-posting user turn (which would write a fresh entry per job)."""

    def test_system_carries_the_breakpoint(self):
        p = claude.build_payload("SYSTEM RUBRIC", "JOB TITLE: X")
        assert isinstance(p["system"], list)
        assert p["system"][0]["cache_control"]["type"] == "ephemeral"

    def test_user_turn_has_no_breakpoint(self):
        p = claude.build_payload("SYSTEM RUBRIC", "JOB TITLE: X")
        assert "cache_control" not in str(p["messages"])

    def test_cache_off_falls_back_to_plain_string(self):
        assert claude.build_payload("S", "U", cache=False)["system"] == "S"

    def test_system_prompt_is_byte_stable(self):
        assert (claude.build_payload("S", "U1")["system"]
                == claude.build_payload("S", "U2")["system"])

    def test_thin_profile_still_builds(self, cfg):
        """An empty profile must still produce a coherent rubric — a new user
        runs their first crawl before filling anything in."""
        keys = ("CORE_KEYWORDS", "DOMAIN_KEYWORDS", "SKILL_KEYWORDS",
                "FIT_DOMAIN_LADDER", "FIT_STACK_CORE", "FIT_STACK_ANTI",
                "FIT_REGION", "LOCALITY_NAME", "LOCALITY_SUBSTRINGS")
        saved = {k: getattr(cfg, k, None) for k in keys}
        for k in keys:
            setattr(cfg, k, [] if k.endswith(("KEYWORDS", "SUBSTRINGS")) else None)
        try:
            prompt = fit.build_system_prompt()
        finally:
            for k, v in saved.items():
                setattr(cfg, k, v)
        assert fit.FALLBACK_STACK_CORE in prompt
        assert "remote" in prompt
        assert "~0.15" in prompt          # the ladder's floor rung survives
