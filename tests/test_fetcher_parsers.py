"""ATS response parsing, against recorded fixtures.

The live canary (`tools/check_boards.py`, run nightly) answers "is the
endpoint still there?". These answer the other half: "given that response,
do we parse it correctly?" — offline, deterministic, and fast.

Both halves are needed. The Ashby fetcher read `jobPostings` from a payload
whose key is `jobs`, so it returned zero for every Ashby board while looking
perfectly healthy: no exception, no error log, just an empty list that the
crawler treats as "no matches". A test like `test_ashby_reads_the_jobs_key`
fails loudly the moment that regresses.

Fixtures are real responses with the prose redacted — the shape is what's
under test, and nobody's job descriptions need committing.
"""

import json
from pathlib import Path

import pytest

from scrapers.fetchers import ats_api

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def match_everything(cfg, pristine_keywords):
    """Widen the relevance filter so these tests measure PARSING only.

    Mutated in place — filters.py bound the list objects at import time.
    """
    cfg.CORE_KEYWORDS[:] = [""]
    cfg.DOMAIN_KEYWORDS[:] = []
    cfg.SKILL_KEYWORDS[:] = []
    cfg.INCLUDE_KEYWORDS[:] = [""]
    cfg.EXCLUDE_PHRASES[:] = []
    cfg.EXCLUDE_TITLE_PHRASES[:] = []


@pytest.fixture
def fake_get(monkeypatch):
    """Serve a fixture instead of the network."""
    def _install(payload, status=200):
        class _Resp:
            status_code = status
            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"{status} Client Error")
            def json(self):
                return payload
            @property
            def text(self):
                return json.dumps(payload)
        monkeypatch.setattr(ats_api.SESSION, "get",
                            lambda *a, **k: _Resp())
    return _install


class TestGreenhouse:
    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("greenhouse_board.json"))
        jobs = ats_api.fetch_greenhouse("databricks", "Databricks")
        assert jobs
        j = jobs[0]
        assert j["id"].startswith("gh_databricks_")
        assert j["title"] and j["url"].startswith("http")
        assert j["company"] == "Databricks"

    def test_location_falls_back_when_absent(self, fake_get, match_everything):
        payload = load("greenhouse_board.json")
        payload["jobs"][0]["location"] = {}
        fake_get(payload)
        assert ats_api.fetch_greenhouse("x", "X")[0]["location"] == "Unknown"

    def test_http_error_returns_empty_not_raises(self, fake_get, match_everything):
        fake_get({}, status=500)
        assert ats_api.fetch_greenhouse("x", "X") == []

    def test_unexpected_shape_returns_empty(self, fake_get, match_everything):
        fake_get(["not", "a", "dict"])
        assert ats_api.fetch_greenhouse("x", "X") == []


class TestLever:
    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("lever_board.json"))
        jobs = ats_api.fetch_lever("veeva", "Veeva")
        assert jobs
        j = jobs[0]
        # Prefixes are the store's dedup namespace: gh_ / lv_ / ashby_.
        assert j["id"].startswith("lv_veeva_")
        assert j["title"] and j["url"].startswith("http")

    def test_http_error_returns_empty(self, fake_get, match_everything):
        fake_get({}, status=404)
        assert ats_api.fetch_lever("x", "X") == []


class TestAshby:
    def test_reads_the_jobs_key(self, fake_get, match_everything):
        """Regression: the payload key is `jobs`, not `jobPostings`.

        Reading the wrong key returned [] for every Ashby board — silently,
        because a missing key is an empty list and the crawler cannot tell
        that from 'nothing matched'."""
        payload = load("ashby_board.json")
        assert "jobs" in payload and "jobPostings" not in payload
        fake_get(payload)
        jobs = ats_api.fetch_ashby("vanta", "Vanta")
        assert jobs, "Ashby parsed zero postings from a non-empty board"

    def test_parses_postings(self, fake_get, match_everything):
        fake_get(load("ashby_board.json"))
        j = ats_api.fetch_ashby("vanta", "Vanta")[0]
        assert j["id"].startswith("ashby_vanta_")
        assert j["title"] and j["url"].startswith("http")
        assert j["location"]

    def test_department_and_team_both_feed_relevance(self, fake_get,
                                                     match_everything):
        # `department`/`team` are the real keys; `departmentName` never
        # existed, so department text was invisible to the keyword gate.
        payload = load("ashby_board.json")
        assert any({"department", "team"} & set(j) for j in payload["jobs"])

    def test_remote_hint_from_structured_fields(self, fake_get, match_everything):
        payload = load("ashby_board.json")
        payload["jobs"][0]["isRemote"] = False
        payload["jobs"][0]["workplaceType"] = "Remote"
        fake_get(payload)
        assert ats_api.fetch_ashby("v", "V")[0].get("remote_hint") == "ashby:isRemote"

    def test_posted_at_is_captured(self, fake_get, match_everything):
        fake_get(load("ashby_board.json"))
        jobs = ats_api.fetch_ashby("vanta", "Vanta")
        assert any(j.get("posted_at") for j in jobs)

    def test_http_error_returns_empty(self, fake_get, match_everything):
        fake_get({}, status=403)
        assert ats_api.fetch_ashby("x", "X") == []


class TestRelevanceGate:
    """The filter is applied INSIDE the fetchers — which is why the canary
    widens it before judging a board's health."""

    def test_irrelevant_postings_are_dropped(self, cfg, fake_get,
                                             pristine_keywords):
        cfg.CORE_KEYWORDS[:] = ["quantum basket weaving"]
        cfg.DOMAIN_KEYWORDS[:] = []
        cfg.SKILL_KEYWORDS[:] = []
        cfg.INCLUDE_KEYWORDS[:] = ["quantum basket weaving"]
        fake_get(load("greenhouse_board.json"))
        assert ats_api.fetch_greenhouse("databricks", "Databricks") == []
