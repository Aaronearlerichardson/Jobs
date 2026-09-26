"""call_claude_json failure handling: the unrecoverable-error circuit breaker
and the transient-status retry ladder, plus the per-call latency record, the
usage footer every call feeds, the reply schema every call is held to, and
the thinking setting each model family accepts.
All offline -- the transport (net.http.send) is stubbed.

Why the breaker exists: the 2026-08-31 rescore run hit "credit balance is too
low" (HTTP 400) and, because every job's call failed independently, hammered
the API with 973 identical requests over five minutes instead of stopping
after the first.
"""

import asyncio
import logging
import re

import pytest

from conftest import fake_response
import src.claude.api as claude
from src import runstate
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
    """Stub the API's requests; returns the list of responses to serve
    (see conftest.serve) and the request log."""
    responses = []
    calls = serve(responses)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("src.config.CLAUDE_RETRY_DELAYS_S", (0.0, 0.0))
    return responses, calls


async def test_billing_400_trips_breaker(api):
    responses, calls = api
    responses.append(fake_response(status=400, text=_BILLING))
    assert await claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 1
    # Breaker is tripped: later calls fail fast without touching the API.
    assert await claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 1


async def test_auth_401_trips_breaker(api):
    responses, calls = api
    responses.append(
        fake_response(status=401, text='{"message":"invalid x-api-key"}'))
    await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert len(calls) == 1


async def test_ordinary_400_does_not_trip_breaker(api):
    responses, calls = api
    responses.append(fake_response(status=400, text=_TOO_LARGE))
    assert await claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert await claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 2


async def test_transient_500_retries_then_succeeds(api):
    responses, calls = api
    responses.extend([fake_response(status=500, text="overloaded"), _OK])
    assert await claude.call_claude_json("sys", "user", cache=False, reply=_Ok) == _Ok(ok=True)
    assert len(calls) == 2


async def test_persistent_500_gives_up_without_tripping(api):
    responses, calls = api
    responses.append(fake_response(status=500, text="overloaded"))
    assert await claude.call_claude_json("sys", "user", cache=False, reply=_Ok) is None
    assert len(calls) == 1 + len(claude.config.CLAUDE_RETRY_DELAYS_S)
    # 5xx is transient — the next call must still reach the API.
    await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert len(calls) == 2 * (1 + len(claude.config.CLAUDE_RETRY_DELAYS_S))


async def test_the_next_run_rearms_the_breaker_and_reprints_the_banner(api, capsys):
    """The web UI runs many operations in one process, each a run of its
    own (src/dispatch/background._run). On 2026-09-09 a crawl tripped the
    breaker on an exhausted balance and the next two verify runs skipped
    every call silently: the banner prints once per trip. A fresh breaker
    per run makes a topped-up balance take effect without a server
    restart, and a still-dead API fails once and explains itself again."""
    responses, calls = api
    responses.append(fake_response(status=400, text=_BILLING))
    await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert claude.api_disabled() and len(calls) == 1
    async with runstate.Run():
        assert claude.api_disabled() is None
        await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert len(calls) == 2                       # reached the API again
    assert capsys.readouterr().out.count("Claude API disabled") == 2


@pytest.mark.parametrize("reply, logged", [
    (_OK, r"\| HTTP 200 in \d+\.\d\ds$"),
    (fake_response(status=400, text=_TOO_LARGE),
     r"^claude call failed: HTTP 400 in \d+\.\d\ds$"),
    (RuntimeError("connection reset"),
     r"^claude call failed: RuntimeError in \d+\.\d\ds$"),
], ids=["ok", "http-400", "exception"])
async def test_every_call_logs_its_outcome_and_elapsed(api, caplog, reply, logged):
    """API latency used to be invisible: call_claude_json posts plain
    (polite=False), never through net.http's per-request DEBUG trace (robots
    / Crawl-delay do not apply to the API), so a slow or failing call left no
    timing anywhere in the session log."""
    responses, _ = api
    responses.append(reply)
    with caplog.at_level(logging.DEBUG, logger="claude"):
        await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
    assert any(re.search(logged, r.getMessage())
               for r in caplog.records if r.name == "claude")


async def test_a_legacy_model_answers_through_a_forced_tool_call(api):
    responses, calls = api
    responses.append(fake_response({"content": [{
        "type": "tool_use", "name": "_Ok", "input": {"ok": True}}], "usage": {}}))
    assert await claude.call_claude_json("sys", "user", model="claude-sonnet-4-0",
                                         cache=False, reply=_Ok) == _Ok(ok=True)
    assert calls[0].kw["json"]["tool_choice"] == {"type": "tool", "name": "_Ok"}


def test_each_model_family_gets_the_request_it_accepts():
    """Thinking the caller did not ask for, per the docs' per-model table
    (build-with-claude/thinking-troubleshooting, 2026-09): off where
    "disabled" is accepted, effort low where thinking is always on (those
    400 on "disabled"), untouched where it is off by default. Where it is
    always on, max_tokens (which caps thinking and reply together) gains
    room beyond the reply's budget. Thinking asked for is the model's
    default everywhere, within the caller's max_tokens."""
    fmt = {"format": {"type": "json_schema", "schema": _Ok.model_json_schema()}}
    off, low = {"type": "disabled"}, {"effort": "low", **fmt}
    for model, want in {
            "claude-sonnet-5": (off, fmt), "claude-opus-5": (off, fmt),
            "claude-opus-5-5": (None, low), "claude-fable-5": (None, low),
            "claude-fable-5-1": (None, low), "claude-mythos-5-1": (None, low),
            "claude-opus-4-8": (None, fmt),
            "claude-sonnet-4-0": (None, None)}.items():   # forced tool call
        p = claude.build_payload("S", "U", 300, model=model, reply=_Ok)
        assert (p.get("thinking"), p.get("output_config")) == want, model
        assert (p["max_tokens"] > 300) == (want[1] == low), model
        p = claude.build_payload("S", "U", 300, model=model, thinking=True, reply=_Ok)
        assert "thinking" not in p and "effort" not in p.get("output_config", {}), model
        assert p["max_tokens"] == 300, model


async def test_a_cut_off_owner_check_still_keeps_its_verdict(monkeypatch):
    """An asker cancelled mid-call (a JS scrape's budget) leaves the paid
    call to finish; its verdict is kept, and the next asker makes none,
    in the next run (a web UI op, a harvest pass) too."""
    calls, release = [], asyncio.Event()

    async def call(*_a, **_kw):
        calls.append(1)
        await release.wait()
        return claude.BoardOwnerReply(same_employer=False, reason="x")
    monkeypatch.setattr(claude, "call_claude_json", call)
    first = asyncio.create_task(claude.board_is_own("Acme", "greenhouse:acme"))
    while not calls:
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await claude._BOARD_OWNER_CACHE["acme", "greenhouse:acme"] is False
    # The verdict alone is kept: a kept task would pin its run's state.
    assert not isinstance(claude._BOARD_OWNER_CACHE["acme", "greenhouse:acme"], asyncio.Task)
    assert await claude.board_is_own("Acme", "greenhouse:acme") is False
    async with runstate.Run():
        assert await claude.board_is_own("Acme", "greenhouse:acme") is False
    assert calls == [1]


async def test_a_run_ending_mid_owner_check_waits_for_its_verdict(monkeypatch):
    """A run that ends while an owner check it started is still running
    (its asker cut off) waits for it, config.CLAUDE_OWNER_WAIT_S at most,
    before closing the session the call opened: the paid verdict is kept,
    and the next run makes no call."""
    calls, release, closed = [], asyncio.Event(), asyncio.Event()

    async def call(*_a, **_kw):
        runstate.at_exit(closed.set, last=True)     # as the session it opens
        calls.append(1)
        await release.wait()
        assert not closed.is_set()
        return claude.BoardOwnerReply(same_employer=False, reason="x")
    monkeypatch.setattr(claude, "call_claude_json", call)
    async with runstate.Run():
        asker = asyncio.create_task(claude.board_is_own("Acme", "greenhouse:acme"))
        while not calls:
            await asyncio.sleep(0)
        asker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asker
        asyncio.get_running_loop().call_later(0.05, release.set)
    assert await claude._BOARD_OWNER_CACHE["acme", "greenhouse:acme"] is False
    async with runstate.Run():
        assert await claude.board_is_own("Acme", "greenhouse:acme") is False
    assert calls == [1]


async def test_an_invalid_reply_is_no_answer_named_once(api, capsys, monkeypatch):
    """board_is_own read {"same_employer": "false"} as True -- bool() of a
    non-empty string -- and cached it. A string is not a boolean: the
    verdict is None (keep the hit, cache nothing), and one line names the
    field."""
    responses, calls = api
    responses.append(fake_response({"content": [{
        "type": "text", "text": '{"same_employer": "false", "reason": "x"}'}]}))
    assert await claude.board_is_own("Ripple Neuro", "greenhouse:ripple") is None
    assert await claude.board_is_own("Ripple Neuro", "greenhouse:ripple") is None
    assert len(calls) == 2                       # nothing was cached
    out = capsys.readouterr().out.splitlines()
    assert [ln for ln in out if "same_employer" in ln] == [
        "  [!] Claude reply is not a valid BoardOwnerReply: "
        "same_employer: Input should be a valid boolean"] * 2


class TestUsageReporting:
    """report_cache_stats: the spend footer a harvest pass or a web-UI op
    prints for its own Claude calls -- and the footer every run prints as
    it ends must not repeat what one of those already printed."""

    async def test_a_report_prints_only_the_calls_since_the_last_one(
            self, api, capsys):
        responses, calls = api
        responses.append(_OK)
        await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        claude.report_cache_stats()
        capsys.readouterr()
        await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        claude.report_cache_stats()
        assert "[claude] 2 call(s) | input 2 tok " in capsys.readouterr().out

    async def test_a_run_prints_what_no_footer_covered_as_it_ends(
            self, api, capsys, monkeypatch):
        """A CLI one-shot prints its whole spend once, as its run ends; a
        pass that printed its own footer prints nothing more."""
        monkeypatch.setattr(claude.config.SETTINGS, "claude_usage_summary", True)
        responses, calls = api
        responses.append(_OK)
        async with runstate.Run():
            await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
        printed = capsys.readouterr().out
        assert printed.count("[claude]") == 1 and "1 call(s)" in printed
        async with runstate.Run():
            await claude.call_claude_json("sys", "user", cache=False, reply=_Ok)
            claude.report_cache_stats()              # the pass's own footer
            capsys.readouterr()
        assert capsys.readouterr().out == ""
