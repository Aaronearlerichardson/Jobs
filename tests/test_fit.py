"""Résumé-fit rubric and the Claude payload shape. No API calls: every
assertion is about prompt construction, clipping, and arithmetic."""

import pytest

import src.claude.api as claude
import src.claude.fit as fit
from src.config.profile_schema import FitWeights, GatePenalty


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
    def test_the_calibration_defaults_are_the_schemas(self):
        assert fit.DEFAULT_WEIGHTS == FitWeights().model_dump()
        assert fit.DEFAULT_GATE_PENALTY == GatePenalty().model_dump()

    def test_management_gate_bites(self):
        axes = dict(domain=.35, function=.30, stack=.35, seniority=.45)
        assert fit.combine(axes, ["management"]) < fit.combine(axes, []) * 0.5

    def test_clearance_regex_needs_a_held_clearance(self):
        assert fit._clearance_required("must have an active TS/SCI clearance")
        # "able to obtain" is not a disqualifier for a clearable citizen.
        assert not fit._clearance_required("eligible to obtain a clearance")


class TestGateOverrides:
    """The STRIP side of the deterministic backstop.

    _clearance_required only ever ADDED a gate, so a gate the profile's own
    rules say must not fire (an eligibility-only clearance line; a geo gate
    on a posting whose stored location lists the candidate's own city, or on
    a US-remote row) rode all the way into the stored score. These pin that
    the override removes exactly those and nothing else. Locations come from
    the profile fixtures, so the assertions hold on any profile.
    """

    ONSITE = "Onsite role in our main office."

    # --- geo ---------------------------------------------------------------

    def test_a_local_row_loses_the_geo_gate(self, local_addr):
        assert fit.apply_gate_overrides(["geo"], location=local_addr,
                                        description=self.ONSITE) == []

    def test_a_multi_site_row_listing_the_local_office_loses_it(
            self, local_addr, elsewhere):
        # The Merck shape: "USA - New Jersey - Rahway; USA - North Carolina -
        # Durham" — the candidate's own city IS one of the valid locations.
        loc = f"{elsewhere}; {local_addr}"
        assert fit.apply_gate_overrides(["geo"], location=loc,
                                        description=self.ONSITE) == []

    def test_a_us_remote_row_loses_the_geo_gate(self):
        assert fit.apply_gate_overrides(["geo"], location="Remote - US") == []

    def test_a_remote_body_phrase_also_clears_it(self):
        assert fit.apply_gate_overrides(
            ["geo"], location="",
            description="This role is remote, open to US applicants.") == []

    def test_an_out_of_area_onsite_row_keeps_the_geo_gate(self, elsewhere):
        # Not a no-op: the gate has to survive where it is earned.
        assert fit.apply_gate_overrides(["geo"], location=elsewhere,
                                        description=self.ONSITE) == ["geo"]

    def test_a_place_name_loose_in_the_body_does_not_clear_it(
            self, elsewhere, local_addr):
        # Every JD in the region's orbit names the region somewhere; only the
        # STORED location field decides the locality half.
        assert fit.apply_gate_overrides(
            ["geo"], location=elsewhere,
            description=f"Partnered with labs in {local_addr}.") == ["geo"]

    # --- clearance ---------------------------------------------------------

    def test_eligibility_only_language_loses_the_clearance_gate(self):
        for desc in ("Applicants must be eligible to obtain and maintain a "
                     "U.S. security clearance.",
                     "US citizenship required for eligibility; a clearance "
                     "may be required after hire.",
                     "- Must be a US citizen able to obtain a security "
                     "clearance\n- 5 years of Python"):
            assert fit.apply_gate_overrides(["clearance"],
                                            description=desc) == [], desc

    def test_an_active_clearance_requirement_keeps_the_gate(self):
        for desc in ("Must currently hold an active TS/SCI clearance with "
                     "polygraph.",
                     "Candidates must possess a current DoD Top Secret "
                     "clearance.",
                     "US citizenship required for eligibility; an active "
                     "clearance is required on day one."):
            assert fit.apply_gate_overrides(
                ["clearance"], description=desc) == ["clearance"], desc

    def test_a_held_clearance_beside_eligibility_language_keeps_the_gate(self):
        # "currently hold" is not caught by _CLEARANCE_RE (it wants "current
        # <type> clearance"), so the clause reader has to see it.
        assert fit.apply_gate_overrides(
            ["clearance"],
            description="Applicants must be US citizens and must currently "
                        "hold a Secret clearance.") == ["clearance"]

    def test_no_eligibility_evidence_means_no_strip(self):
        # The strip needs POSITIVE eligibility evidence. A bare demand with
        # no eligibility language, and a body with no clearance language at
        # all, both leave the model's gate standing.
        assert fit.apply_gate_overrides(
            ["clearance"],
            description="Requires a TS/SCI with polygraph.") == ["clearance"]
        assert fit.apply_gate_overrides(
            ["clearance"],
            description="We build neural data pipelines.") == ["clearance"]

    # --- shape -------------------------------------------------------------

    def test_the_override_never_adds_and_never_touches_other_gates(self):
        assert fit.apply_gate_overrides(
            ["management", "phd", "geo"], location="Remote - US",
            description="Eligible to obtain a clearance.") == ["management", "phd"]
        assert fit.apply_gate_overrides([], location="Remote - US") == []

    # --- wiring ------------------------------------------------------------

    @staticmethod
    def _ungated(res):
        return fit.combine(res.axes, [], fit.config.FIT_WEIGHTS,
                           fit.config.FIT_GATE_PENALTY)

    def test_score_resume_fit_applies_it(self, monkeypatch, local_addr):
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: fit.FitReply(
            domain=.8, function=.8, stack=.7, seniority=1.0,
            gates=["geo", "clearance"], reason="good lane"))
        body = ("Build neural data pipelines. " * 20
                + "Applicants must be eligible to obtain a U.S. security "
                  "clearance.")
        res = fit.score_resume_fit("ML Engineer", body, location=local_addr)
        assert res.gates == []
        assert res.score == self._ungated(res)
        assert "gate:" not in res.summary()

    def test_verify_fit_applies_it_without_disarming_its_own_backstops(
            self, monkeypatch, local_addr):
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: fit.VerifyReply(
            years_required=None, seat_type="management", must_haves=[],
            candidate_gaps=[], domain=.8, function=.8, stack=.7,
            seniority=1.0, gates=["geo"], reason="deep"))
        res = fit.verify_fit("Program Lead", "x " * 200, location=local_addr)
        assert res.gates == ["management"]      # geo stripped, seat gate kept

    def test_the_add_side_backstop_still_wins(self, monkeypatch, local_addr):
        # A local posting that really does demand an active clearance keeps
        # it: the strip must not undo the regex that just added it.
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: fit.FitReply(
            domain=.8, function=.8, stack=.7, seniority=1.0,
            gates=[], reason="cleared shop"))
        body = ("Signal processing work. " * 20
                + "Must hold an active TS/SCI clearance; eligibility to "
                  "upgrade is a plus.")
        res = fit.score_resume_fit("DSP Engineer", body, location=local_addr)
        assert res.gates == ["clearance"]


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
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: None)
        res = fit.score_resume_fit("T", "x" * fit.MIN_DESC_CHARS)
        assert res.score is None
        assert fit.unscored_cause(res.reason) == "refused"

    def test_a_scorer_that_never_asked_is_not_a_refusal(self, monkeypatch):
        # No key, and a tripped breaker, both make call_claude_json return
        # None WITHOUT asking the model. Reporting those as "unscored" would
        # let ops.maintenance's retry marker hold a perfectly scorable row
        # for UNSCORED_RETRY_DAYS over one billing hiccup.
        monkeypatch.setattr(fit, "call_claude_json", lambda *a, **k: None)
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
        # ...and the schema asks for them before the axes, as the prompt does.
        assert list(fit.VerifyReply.model_fields)[:5] == [
            "years_required", "seat_type", "must_haves", "candidate_gaps",
            "domain"]

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
            return None                     # -> unscored, score None

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
