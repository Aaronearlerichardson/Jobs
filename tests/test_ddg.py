"""The web-search client's resolver fallback and breaker.

ddgs resolves names with its own resolver (primp), not the OS one. On a
machine with a VPN up and a second adapter connected it picked a server
that answered "Query Refused" for every engine while the crawler's own
requests resolved fine (2026-09-02 add-names run: nine failures, each
retried with backoff). The client now switches ddgs to the configured
public resolvers on the first refusal and retries; if that is refused
too, or nothing is configured, a breaker skips web search for the rest
of the process.

Offline: the DDGS class and ddgs's HTTP-client module are both faked.
"""

import types

import pytest

from scrapers import ddg

REFUSED = Exception(
    "ConnectError: error sending request for url (https://html.duckduckgo.com/"
    "html/) > client error (Connect) > dns error > DNS error: error response: "
    "Query Refused")


class _FakeHttpClient:
    """Stands in for ddgs.http_client: a `primp` namespace whose Client we
    can watch being replaced."""

    def __init__(self):
        self.built = []
        # One bound-method object, held once: a fresh access to self._client
        # would be a new object each time and defeat the identity checks.
        self.original = self._client
        self.primp = types.SimpleNamespace(Client=self.original)

    def _client(self, **kw):
        self.built.append(kw)
        return object()


def _fake_ddgs(script):
    """A DDGS stand-in whose .text() pops outcomes from `script`: an
    Exception raises, a list returns. `made` counts constructions."""
    made = []

    class FakeDDGS:
        def __init__(self, timeout=None):
            made.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, **kw):
            out = script.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

    return FakeDDGS, made


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Fresh breaker state, a throwaway cache, and the fake HTTP module."""
    ddg.reset_resolver()
    hc = _FakeHttpClient()
    monkeypatch.setattr(ddg, "CACHE_DIR", tmp_path / "ddg")
    monkeypatch.setattr(ddg, "_http_client_module", lambda: hc)
    monkeypatch.setattr(ddg, "_ensure_ddgs_engines", lambda: None)
    monkeypatch.setattr(ddg.config, "SEARCH_DNS_FALLBACK", ("1.1.1.1", "8.8.8.8"))
    yield hc
    ddg.reset_resolver()


def _search(monkeypatch, script, q="acme careers"):
    Fake, made = _fake_ddgs(script)
    monkeypatch.setattr(ddg, "_ddgs_class", lambda: Fake)
    return ddg.search(q, budget=5), made


class TestResolverFallback:
    def test_a_refused_resolver_switches_to_the_fallback_and_retries(
            self, monkeypatch, wired, capsys):
        hit = [{"href": "https://acme.example/careers"}]
        out, made = _search(monkeypatch, [REFUSED, hit])
        assert out == hit
        assert len(made) == 2                       # one failure, one retry
        # ddgs's client factory now injects the configured servers.
        wired.primp.Client(timeout=1)
        assert wired.built[-1]["dns_resolver"] == ["1.1.1.1", "8.8.8.8"]
        assert "1.1.1.1" in capsys.readouterr().out

    def test_the_switch_happens_once_per_process(self, monkeypatch, wired):
        _search(monkeypatch, [REFUSED, [{"href": "https://a.example/"}]])
        installed = wired.primp.Client
        _search(monkeypatch, [[{"href": "https://b.example/"}]], q="beta jobs")
        assert wired.primp.Client is installed      # not wrapped twice

    def test_a_fallback_that_is_also_refused_trips_the_breaker(
            self, monkeypatch, wired, capsys):
        out, made = _search(monkeypatch, [REFUSED, REFUSED])
        assert out == [] and len(made) == 2
        out, made = _search(monkeypatch, [[{"href": "https://never/"}]], q="beta")
        assert out == [] and made == []             # no client built at all
        text = capsys.readouterr().out
        assert text.count("unreachable") == 1       # announced once

    def test_no_fallback_configured_trips_immediately(
            self, monkeypatch, wired):
        monkeypatch.setattr(ddg.config, "SEARCH_DNS_FALLBACK", ())
        out, made = _search(monkeypatch, [REFUSED])
        assert out == [] and len(made) == 1
        assert wired.primp.Client is wired.original # nothing installed
        out, made = _search(monkeypatch, [[{"href": "https://never/"}]], q="beta")
        assert out == [] and made == []

    def test_reset_restores_the_original_client(self, monkeypatch, wired):
        _search(monkeypatch, [REFUSED, [{"href": "https://a.example/"}]])
        assert wired.primp.Client is not wired.original
        ddg.reset_resolver()
        assert wired.primp.Client is wired.original

    def test_a_throttle_is_not_a_resolver_failure(self, monkeypatch, wired):
        monkeypatch.setattr(ddg, "RETRY_PAUSE", 0)
        out, made = _search(monkeypatch, [Exception("Ratelimit"),
                                          [{"href": "https://a.example/"}]])
        assert out == [{"href": "https://a.example/"}]
        assert wired.primp.Client is wired.original # ordinary retry path
