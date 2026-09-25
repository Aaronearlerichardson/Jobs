"""call_claude_json failure handling: the unrecoverable-error circuit breaker
and the transient-status retry ladder, plus the per-call latency record, the
usage footer every call feeds, the reply schema every call is held to, and
the thinking setting each model family accepts.
All offline -- SESSION is stubbed.

Why the breaker exists: the 2026-08-31 rescore run hit "credit balance is too
low" (HTTP 400) and, because every job's call failed independently, hammered
the API with 973 identical requests over five minutes instead of stopping
after the first.
"""

import logging
import re

import pytest

from conftest import fake_response
import src.claude.api as claude
from src.claude.reply import Reply


_BILLING = ('{"message":"Your credit balance is too low to access the '
            'Anthropic API."}')
_TOO_LARGE = '{"message":"max_tokens too large"}'


class _Ok(Reply):
    ok: bool


_OK = fake_response({
    "content": [{"type": "text", "text": '{"ok": true}'}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
})


@pytest.fixture
def api(monkeypatch, serve):
    """Stub the API's own session; returns the list of responses to serve
    (see conftest.serve) and the request log."""
    responses = []
    calls = serve(responses, session=claude.SESSION)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(claude, "_FATAL_MSG", None)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    return responses, calls


def test_billing_400_trips_breaker(api):
    responses, calls = api
    responses.append(fake_response(status=400, text=_BILLING))
    assert claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 1
    # Breaker is tripped: later calls fail fast without touching the API.
    assert claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 1


def test_auth_401_trips_breaker(api):
    responses, calls = api
    responses.append(
        fake_response(status=401, text='{"message":"invalid x-api-key"}'))
    claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert len(calls) == 1


def test_ordinary_400_does_not_trip_breaker(api):
    responses, calls = api
    responses.append(fake_response(status=400, text=_TOO_LARGE))
    assert claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 2


def test_transient_500_retries_then_succeeds(api):
    responses, calls = api
    responses.extend([fake_response(status=500, text="overloaded"), _OK])
    assert claude.call_claude_json("sys", "user", cache=False, reply=_Ok) == _Ok(ok=True)
    assert len(calls) == 2


def test_persistent_500_gives_up_without_tripping(api):
    responses, calls = api
    responses.append(fake_response(status=500, text="overloaded"))
    assert claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 1 + len(claude._RETRY_DELAYS)
    # 5xx is transient — the next call must still reach the API.
    claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert len(calls) == 2 * (1 + len(claude._RETRY_DELAYS))


def test_reset_breaker_rearms_and_reprints_the_banner(api, capsys):
    """The web UI runs many operations in one process (src/dispatch/background._run_op).
    On 2026-09-09 a crawl tripped the breaker on an exhausted balance and the
    next two verify runs skipped every call silently — the banner prints once
    per trip. Re-arming per operation makes a topped-up balance take effect
    without a server restart, and a still-dead API fails once and explains
    itself again."""
    responses, calls = api
    responses.append(fake_response(status=400, text=_BILLING))
    claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert claude.api_disabled() and len(calls) == 1
    claude.reset_breaker()
    assert claude.api_disabled() is None
    claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert len(calls) == 2                       # reached the API again
    assert capsys.readouterr().out.count("Claude API disabled") == 2


@pytest.mark.parametrize("reply, logged", [
    (_OK, r"\| HTTP 200 in \d+\.\d\ds$"),
    (fake_response(status=400, text=_TOO_LARGE),
     r"^claude call failed: HTTP 400 in \d+\.\d\ds$"),
    (RuntimeError("connection reset"),
     r"^claude call failed: RuntimeError in \d+\.\d\ds$"),
], ids=["ok", "http-400", "exception"])
def test_every_call_logs_its_outcome_and_elapsed(api, caplog, reply, logged):
    """API latency used to be invisible: call_claude_json posts through its
    own plain SESSION, never net.http's per-request DEBUG trace (robots /
    Crawl-delay do not apply to the API), so a slow or failing call left no
    timing anywhere in the session log."""
    responses, _ = api
    responses.append(reply)
    with caplog.at_level(logging.DEBUG, logger="claude"):
        claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert any(re.search(logged, r.getMessage())
               for r in caplog.records if r.name == "claude")


def test_a_legacy_model_answers_through_a_forced_tool_call(api):
    responses, calls = api
    responses.append(fake_response({"content": [{
        "type": "tool_use", "name": "_Ok", "input": {"ok": True}}], "usage": {}}))
    assert claude.call_claude_json("sys", "user", model="claude-sonnet-4-0",
                                   cache=False, reply=_Ok) == _Ok(ok=True)
    assert calls[0].kw["json"]["tool_choice"] == {"type": "tool", "name": "_Ok"}


def test_each_model_family_gets_the_request_it_accepts():
    """Thinking the caller did not ask for, per the docs' per-model table
    (build-with-claude/thinking-troubleshooting, 2026-09): off where
    "disabled" is accepted, effort low where thinking is always on (those
    400 on "disabled"), untouched where it is off by default. Thinking
    asked for is the model's default everywhere."""
    fmt = {"format": {"type": "json_schema", "schema": _Ok.model_json_schema()}}
    off, low = {"type": "disabled"}, {"effort": "low", **fmt}
    for model, want in {
            "claude-sonnet-5": (off, fmt), "claude-opus-5": (off, fmt),
            "claude-opus-5-5": (None, low), "claude-fable-5": (None, low),
            "claude-fable-5-1": (None, low), "claude-mythos-5-1": (None, low),
            "claude-opus-4-8": (None, fmt),
            "claude-sonnet-4-0": (None, None)}.items():   # forced tool call
        p = claude.build_payload("S", "U", model=model, reply=_Ok)
        assert (p.get("thinking"), p.get("output_config")) == want, model
        p = claude.build_payload("S", "U", model=model, thinking=True, reply=_Ok)
        assert "thinking" not in p and "effort" not in p.get("output_config", {}), model


def test_an_invalid_reply_is_no_answer_named_once(api, capsys, monkeypatch):
    """board_is_own read {"same_employer": "false"} as True -- bool() of a
    non-empty string -- and cached it. A string is not a boolean: the
    verdict is None (keep the hit, cache nothing), and one line names the
    field."""
    responses, calls = api
    responses.append(fake_response({"content": [{
        "type": "text", "text": '{"same_employer": "false", "reason": "x"}'}]}))
    assert claude.board_is_own("Ripple Neuro", "greenhouse:ripple") is None
    assert claude.board_is_own("Ripple Neuro", "greenhouse:ripple") is None
    assert len(calls) == 2                       # nothing was cached
    out = capsys.readouterr().out.splitlines()
    assert [ln for ln in out if "same_employer" in ln] == [
        "  [!] Claude reply is not a valid BoardOwnerReply: "
        "same_employer: Input should be a valid boolean"] * 2


class TestUsageReporting:
    """report_cache_stats: the spend footer a harvest pass or a web-UI op
    prints for its own Claude calls, reusing the one cumulative counter
    (cache_stats()) instead of a second one -- and the atexit trailer must
    not repeat what one of those already printed."""

    def test_a_baseline_report_prints_only_the_calls_since_it(
            self, api, capsys):
        responses, calls = api
        responses.append(_OK)
        claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        baseline = claude.cache_stats()
        claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        claude.report_cache_stats(baseline)
        printed = capsys.readouterr().out
        assert "[claude] 2 call(s) | input 2 tok " in printed

    def test_atexit_style_report_does_not_repeat_an_already_reported_pass(
            self, api, capsys):
        """A harvest pass (or web op) calls report_cache_stats(baseline)
        itself; the atexit trailer (report_cache_stats(), no baseline) must
        then find nothing new to say for a CLI one-shot that already
        reported everything mid-run."""
        responses, calls = api
        responses.append(_OK)
        baseline = claude.cache_stats()
        claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        claude.report_cache_stats(baseline)          # the pass's own footer
        capsys.readouterr()                          # discard it
        claude.report_cache_stats()                  # what atexit calls
        assert capsys.readouterr().out == ""

    def test_atexit_style_report_covers_a_one_shot_that_never_reported(
            self, api, capsys):
        """A bare script that calls call_claude_json directly, with no
        harvest pass or web op ever calling report_cache_stats, still gets
        its whole spend from the atexit trailer -- exactly once."""
        responses, calls = api
        responses.append(_OK)
        claude.report_cache_stats()                  # flush: nothing owed
        capsys.readouterr()
        claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        claude.report_cache_stats()                  # what atexit calls
        printed = capsys.readouterr().out
        assert printed.count("[claude]") == 1
        assert "1 call(s)" in printed
