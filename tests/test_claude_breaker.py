"""call_claude_json failure handling: the unrecoverable-error circuit breaker
and the transient-status retry ladder. All offline — SESSION.post is stubbed.

Why the breaker exists: the 2026-08-31 rescore run hit "credit balance is too
low" (HTTP 400) and, because every job's call failed independently, hammered
the API with 973 identical requests over five minutes instead of stopping
after the first.
"""

import logging
import re

import pytest
import requests

import src.claude.api as claude


class _Resp:
    def __init__(self, status_code, body="", headers=None, payload=None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err

    def json(self):
        return self._payload


_OK = _Resp(200, payload={
    "content": [{"type": "text", "text": '{"ok": true}'}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
})


@pytest.fixture
def api(monkeypatch):
    """Stub the HTTP layer; returns the list of responses to serve (popped
    left-to-right, last one repeats) plus a call counter."""
    calls = []
    responses = []

    class _Session:
        @staticmethod
        def post(url, **kw):
            calls.append(url)
            return responses.pop(0) if len(responses) > 1 else responses[0]

    monkeypatch.setattr(claude, "SESSION", _Session)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(claude, "_FATAL_MSG", None)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    return responses, calls


def test_billing_400_trips_breaker(api):
    responses, calls = api
    responses.append(_Resp(400, body='{"message":"Your credit balance is '
                                     'too low to access the Anthropic API."}'))
    assert claude.call_claude_json("sys", "user", cache=False) == {}
    assert len(calls) == 1
    # Breaker is tripped: later calls fail fast without touching the API.
    assert claude.call_claude_json("sys", "user", cache=False) == {}
    assert len(calls) == 1


def test_auth_401_trips_breaker(api):
    responses, calls = api
    responses.append(_Resp(401, body='{"message":"invalid x-api-key"}'))
    claude.call_claude_json("sys", "user", cache=False)
    claude.call_claude_json("sys", "user", cache=False)
    assert len(calls) == 1


def test_ordinary_400_does_not_trip_breaker(api):
    responses, calls = api
    responses.append(_Resp(400, body='{"message":"max_tokens too large"}'))
    assert claude.call_claude_json("sys", "user", cache=False) == {}
    assert claude.call_claude_json("sys", "user", cache=False) == {}
    assert len(calls) == 2


def test_transient_500_retries_then_succeeds(api):
    responses, calls = api
    responses.extend([_Resp(500, body="overloaded"), _OK])
    assert claude.call_claude_json("sys", "user", cache=False) == {"ok": True}
    assert len(calls) == 2


def test_persistent_500_gives_up_without_tripping(api):
    responses, calls = api
    responses.append(_Resp(500, body="overloaded"))
    assert claude.call_claude_json("sys", "user", cache=False) == {}
    assert len(calls) == 1 + len(claude._RETRY_DELAYS)
    # 5xx is transient — the next call must still reach the API.
    claude.call_claude_json("sys", "user", cache=False)
    assert len(calls) == 2 * (1 + len(claude._RETRY_DELAYS))


def test_reset_breaker_rearms_and_reprints_the_banner(api, capsys):
    """The web UI runs many operations in one process (src/ops/background._run_op).
    On 2026-09-09 a crawl tripped the breaker on an exhausted balance and the
    next two verify runs skipped every call silently — the banner prints once
    per trip. Re-arming per operation makes a topped-up balance take effect
    without a server restart, and a still-dead API fails once and explains
    itself again."""
    responses, calls = api
    responses.append(_Resp(400, body='{"message":"Your credit balance is '
                                     'too low to access the Anthropic API."}'))
    claude.call_claude_json("sys", "user", cache=False)
    assert claude.api_disabled() and len(calls) == 1
    claude.reset_breaker()
    assert claude.api_disabled() is None
    claude.call_claude_json("sys", "user", cache=False)
    assert len(calls) == 2                       # reached the API again
    assert capsys.readouterr().out.count("Claude API disabled") == 2


class TestCallLatencyLogging:
    """API latency used to be invisible: call_claude_json posts through its
    own plain SESSION, never net.http's per-request DEBUG trace (robots /
    Crawl-delay do not apply to the API), so a slow or failing call left no
    timing anywhere in the session log."""

    def test_a_successful_call_logs_status_and_elapsed(self, api, caplog):
        responses, calls = api
        responses.append(_OK)
        with caplog.at_level(logging.DEBUG, logger="claude"):
            claude.call_claude_json("sys", "user", cache=False)
        msgs = [r.getMessage() for r in caplog.records if r.name == "claude"]
        assert any(re.search(r"HTTP 200 in \d+\.\d\ds", m) for m in msgs)

    def test_a_failed_call_logs_status_and_elapsed(self, api, caplog):
        responses, calls = api
        responses.append(_Resp(400, body='{"message":"max_tokens too large"}'))
        with caplog.at_level(logging.DEBUG, logger="claude"):
            claude.call_claude_json("sys", "user", cache=False)
        msgs = [r.getMessage() for r in caplog.records if r.name == "claude"]
        assert any("claude call failed: HTTP 400" in m for m in msgs)

    def test_an_exception_logs_its_type_and_elapsed(self, api, monkeypatch,
                                                     caplog):
        def boom(url, **kw):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(claude.SESSION, "post", boom)
        with caplog.at_level(logging.DEBUG, logger="claude"):
            claude.call_claude_json("sys", "user", cache=False)
        msgs = [r.getMessage() for r in caplog.records if r.name == "claude"]
        assert any("claude call failed: RuntimeError" in m for m in msgs)


class TestUsageReporting:
    """format_cache_stats(since=...) / report_cache_stats: the spend footer
    a harvest pass or a web-UI op prints for its own Claude calls, reusing
    the one cumulative counter (cache_stats()) instead of a second one --
    and the atexit trailer must not repeat what one of those already
    printed."""

    def test_format_cache_stats_reports_only_the_delta_since_baseline(
            self, api):
        responses, calls = api
        responses.extend([_OK, _OK])
        baseline = claude.cache_stats()
        claude.call_claude_json("sys", "user", cache=False)
        claude.call_claude_json("sys", "user", cache=False)
        line = claude.format_cache_stats(since=baseline)
        assert "2 call(s)" in line
        assert "input 2 tok" in line

    def test_report_cache_stats_prints_the_delta_once(self, api, capsys):
        responses, calls = api
        responses.append(_OK)
        baseline = claude.cache_stats()
        claude.call_claude_json("sys", "user", cache=False)
        claude.report_cache_stats(baseline)
        printed = capsys.readouterr().out
        assert "1 call(s)" in printed

    def test_atexit_style_report_does_not_repeat_an_already_reported_pass(
            self, api, capsys):
        """A harvest pass (or web op) calls report_cache_stats(baseline)
        itself; the atexit trailer (report_cache_stats(), no baseline) must
        then find nothing new to say for a CLI one-shot that already
        reported everything mid-run."""
        responses, calls = api
        responses.append(_OK)
        baseline = claude.cache_stats()
        claude.call_claude_json("sys", "user", cache=False)
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
        claude.call_claude_json("sys", "user", cache=False)
        claude.report_cache_stats()                  # what atexit calls
        printed = capsys.readouterr().out
        assert printed.count("[claude]") == 1
        assert "1 call(s)" in printed
