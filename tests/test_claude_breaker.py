"""call_claude_json failure handling: the unrecoverable-error circuit breaker
and the transient-status retry ladder. All offline — SESSION.post is stubbed.

Why the breaker exists: the 2026-08-31 rescore run hit "credit balance is too
low" (HTTP 400) and, because every job's call failed independently, hammered
the API with 973 identical requests over five minutes instead of stopping
after the first.
"""

import pytest
import requests

import core.claude as claude


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
    monkeypatch.setattr(claude, "ANTHROPIC_API_KEY", "test-key")
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
