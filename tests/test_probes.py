"""The (ok, count) contract every ATS slug probe answers.

The twelve probes used to be twelve hand-written request-and-swallow
blocks; they are built from two helpers now (probes._api_probe /
_parser_probe). What matters per ATS is the `require_jobs` decision -- is
a 200 proof of a board, or must it list something -- and that decision is
what these pin, because it is invisible in the table otherwise and it has
been got wrong before: SmartRecruiters answers 200 with totalFound:0 for
ANY slug, so every guessed slug "confirmed" with zero jobs.
"""

import pytest

from conftest import fake_response

from src.discovery.resolve import probes


@pytest.fixture
def answer(monkeypatch):
    """Serve one response to every probe, and record the request."""
    seen = {}

    def _install(resp):
        def _get(url, **kw):
            seen["url"], seen["headers"] = url, kw.get("headers") or {}
            seen["timeout"] = kw.get("timeout")
            return resp
        monkeypatch.setattr(probes.SESSION, "get", _get)
        return seen
    return _install


class TestAPresentBoardIsConfirmed:
    def test_greenhouse_counts_its_jobs(self, answer):
        answer(fake_response({"jobs": [1, 2, 3]}))
        assert probes.probe_greenhouse("acme") == (True, 3)

    def test_lever_counts_a_bare_list(self, answer):
        answer(fake_response([1, 2]))
        assert probes.probe_lever("acme") == (True, 2)

    def test_lever_tolerates_a_non_list_payload(self, answer):
        answer(fake_response({"unexpected": True}))
        assert probes.probe_lever("acme") == (True, 0)

    def test_ashby_reads_the_posting_api_key(self, answer):
        answer(fake_response({"jobs": [1, 2], "jobPostings": []}))
        assert probes.probe_ashby("acme") == (True, 2)

    def test_ashby_falls_back_to_the_embed_key(self, answer):
        answer(fake_response({"jobPostings": [1]}))
        assert probes.probe_ashby("acme") == (True, 1)

    def test_bamboohr_asks_for_json(self, answer):
        seen = answer(fake_response({"result": [1, 2, 3, 4]}))
        assert probes.probe_bamboohr("acme") == (True, 4)
        assert seen["headers"]["Accept"] == "application/json"

    def test_jazzhr_counts_apply_links_in_the_page(self, answer):
        answer(fake_response(text="<a href='/apply/AbC123/'>x</a>"
                          "<a href='/apply/dEf456/'>y</a>"))
        assert probes.probe_jazzhr("acme") == (True, 2)

    def test_kula_accepts_a_substantial_page(self, answer):
        answer(fake_response(text="x" * 1001))
        assert probes.probe_kula("acme") == (True, 0)


class TestAnEmptyBoardIsNotAlwaysAMiss:
    """An ATS that only serves real slugs may legitimately list nothing;
    one that answers for any slug must show postings to count as found."""

    def test_greenhouse_empty_is_still_a_board(self, answer):
        answer(fake_response({"jobs": []}))
        assert probes.probe_greenhouse("acme") == (True, 0)

    def test_smartrecruiters_empty_is_not_a_board(self, answer):
        answer(fake_response({"totalFound": 0}))
        assert probes.probe_smartrecruiters("acme") == (False, 0)

    def test_smartrecruiters_with_postings_is(self, answer):
        answer(fake_response({"totalFound": 7}))
        assert probes.probe_smartrecruiters("acme") == (True, 7)

    def test_jazzhr_with_no_apply_links_is_not_a_board(self, answer):
        answer(fake_response(text="<html>nothing here</html>"))
        assert probes.probe_jazzhr("acme") == (False, 0)

    def test_kula_rejects_a_stub_page(self, answer):
        answer(fake_response(text="too short"))
        assert probes.probe_kula("acme") == (False, 0)


class TestFailureIsReportedNeverRaised:
    """A probe runs inside a discovery fan-out over hundreds of names. It
    reports (False, 0) for everything -- a bad status, a timeout, a
    payload that will not parse -- so one broken host cannot end the pass.
    """

    def test_a_non_200_is_a_miss(self, answer):
        answer(fake_response({"jobs": [1]}, status=404))
        assert probes.probe_greenhouse("acme") == (False, 0)

    def test_unparseable_json_is_a_miss(self, answer):
        answer(fake_response(None))
        assert probes.probe_greenhouse("acme") == (False, 0)

    def test_a_raising_session_is_a_miss(self, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("connection reset")
        monkeypatch.setattr(probes.SESSION, "get", _boom)
        assert probes.probe_greenhouse("acme") == (False, 0)

    def test_kula_retries_once_before_giving_up(self, monkeypatch):
        calls = []

        def _get(url, **kw):
            calls.append(url)
            raise OSError("throttled")
        monkeypatch.setattr(probes.SESSION, "get", _get)
        monkeypatch.setattr(probes.time, "sleep", lambda s: None)
        assert probes.probe_kula("acme") == (False, 0)
        assert len(calls) == 2, "kula gets one retry; the others get none"

    def test_the_others_do_not_retry(self, monkeypatch):
        calls = []

        def _get(url, **kw):
            calls.append(url)
            raise OSError("throttled")
        monkeypatch.setattr(probes.SESSION, "get", _get)
        assert probes.probe_greenhouse("acme") == (False, 0)
        assert len(calls) == 1


class TestParserBackedProbes:
    """Four ATSes reuse the fetcher's own board parser rather than a URL
    of their own. Their `ok` means "has jobs", which is why
    ops.prune_dead_boards refuses to use them to decide a board is dead.
    """

    def test_it_counts_what_the_parser_returned(self, monkeypatch):
        monkeypatch.setattr("src.ats.fetchers.rippling.parse_board",
                            lambda h: [1, 2, 3])
        assert probes.probe_rippling("acme") == (True, 3)

    def test_an_empty_parse_is_not_ok(self, monkeypatch):
        monkeypatch.setattr("src.ats.fetchers.hibob.parse_board",
                            lambda h: [])
        assert probes.probe_hibob("acme") == (False, 0)

    def test_a_raising_parser_is_a_miss(self, monkeypatch):
        def _boom(h):
            raise RuntimeError("shape changed")
        monkeypatch.setattr("src.ats.fetchers.ultipro.parse_board", _boom)
        assert probes.probe_ultipro("CODE|GUID") == (False, 0)


def test_every_registered_probe_is_callable():
    """PROBES is what pipeline.validate_candidate iterates; a name in it
    with nothing behind it fails only in a live discovery run."""
    for ats, probe in probes.PROBES.items():
        assert callable(probe), ats
