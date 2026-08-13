"""Résumé-fit rubric and the Claude payload shape. No API calls: every
assertion is about prompt construction, clipping, and arithmetic."""

import core.claude as claude
import core.fit as fit


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


class TestPrompts:
    def test_verify_prompt_extracts_requirements(self):
        prompt = fit.build_verify_prompt()
        assert all(k in prompt for k in
                   ("years_required", "seat_type", "candidate_gaps"))

    def test_verify_refuses_stub_descriptions(self):
        assert fit.verify_fit("T", "too short").score is None


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

    def test_screen_prompt_clears_the_cache_floor(self):
        # Below the model's floor a prompt simply doesn't cache.
        assert len(fit.build_system_prompt()) // 4 > claude.min_cacheable_tokens()
