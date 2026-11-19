"""`handle.prelude` (src.ats.board.engine): parts a request's own answer settles,
the settled values asked once per handle however many callers, and the one
re-settle when a request is refused 401 or 403; then the two platforms
built on it, Dayforce (a CSRF token) and Cornerstone (a bearer token and
the regional API host, both off the site's home page).

`tests/fixtures/dayforce_search.json` is a trimmed REAL answer (aifire,
board IMPACT, three of its postings, descriptions cut), and
`tests/fixtures/cornerstone_jobs.json` one from macomtech's career site 4;
`cornerstone_home.html` is that home page cut to the `csod.context` it
embeds, its token replaced. Recorded live on 2026-10-05.
"""

import asyncio
import json
from pathlib import Path

import pytest

from conftest import fake_response
from src.ats.board import board_for
from src.ats.board.engine import Board
from src.net import http

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _board(**handle):
    """A one-field POST listing wanting the `token` a prelude settles."""
    return Board("t", {
        "handle": {"prelude": [{"url": "https://x.test/token", "set": {"token": "t"}}],
                   **handle},
        "listing": {"method": "POST", "url": "https://x.test/list",
                    "headers": {"Authorization": "Bearer {token}"},
                    "decoder": {"entries": "items"},
                    "fields": {"id": {"format": "t_{id}"}, "title": "title",
                               "url": {"format": "https://x.test/{id}"}}}})


class _Api:
    """A token endpoint handing out `tokens` in turn, and a list answering
    401 to a token it no longer honours."""

    def __init__(self, tokens, live):
        self.tokens, self.live, self.log = list(tokens), live, []

    async def __call__(self, url, **kw):
        self.log.append((url, kw.get("headers", {}).get("Authorization")))
        await asyncio.sleep(0.02)           # concurrent callers are all asking by now
        if url.endswith("/token"):
            return (fake_response({"t": self.tokens.pop(0)}) if self.tokens
                    else fake_response(text="", status=500))
        if kw["headers"].get("Authorization") != f"Bearer {self.live}":
            return fake_response(text="", status=401)
        return fake_response({"items": [{"id": 1, "title": "Data Engineer"}]})

    def asked(self, name):
        return sum(1 for url, _ in self.log if url.endswith(name))


class TestPrelude:
    async def test_a_part_is_settled_once_per_handle_however_many_ask(self, serve):
        api = _Api(["abc"], "abc")
        serve(api)
        b = _board()
        both = await asyncio.gather(b.listing("h"), b.listing("h"))
        assert [len(rows) for rows in both] == [1, 1] and len(await b.listing("h")) == 1
        assert api.asked("/token") == 1 and api.asked("/list") == 3
        assert {auth for url, auth in api.log if url.endswith("/list")} == {"Bearer abc"}

    async def test_a_refused_token_is_settled_again_once_for_every_caller(self, serve):
        api = _Api(["old", "new"], "new")
        serve(api)
        b = _board()
        both = await asyncio.gather(b.listing("h", "t h"), b.listing("h", "t h"))
        assert [len(rows) for rows in both] == [1, 1]
        assert api.asked("/token") == 2, "one caller re-settles; the other takes its token"
        assert http.fetch_failures() == 0

    async def test_a_token_refused_twice_is_a_failed_pull(self, serve):
        api = _Api(["old", "newer"], "never")
        serve(api)
        assert await _board().listing("h", "t h") == []
        assert api.asked("/token") == 2 and api.asked("/list") == 2
        assert http.fetch_failures() == 1

    async def test_an_unanswered_prelude_fails_the_pull_and_is_asked_again_later(self, serve):
        api = _Api([], "abc")
        serve(api)
        b = _board()
        assert await b.listing("h", "t h") == [] and api.asked("/list") == 0
        assert http.fetch_failures() == 1
        api.tokens.append("abc")
        assert len(await b.listing("h", "t h")) == 1

    def test_the_schema_refuses_a_prelude_it_cannot_run(self):
        from src.ats.board import spec
        ok = {"url": "https://x.test/t", "set": {"token": "t"}}
        spec.parse("t", {"handle": {"prelude": [ok]}})
        for handle in ({"prelude": [{**ok, "set": {}}]}, {"prelude": [ok, ok]},
                       {"prelude": [ok], "follow": {"token": "{slug}"}},
                       {"prelude": [{**ok, "nope": 1}]},
                       {"prelude": [ok], "why": "because"}):
            with pytest.raises(ValueError):
                spec.parse("t", {"handle": handle})


DAYFORCE = board_for("dayforce")
CORNERSTONE = board_for("cornerstone")


class TestDayforce:
    async def test_a_board_is_searched_with_the_csrf_token_its_auth_route_hands_out(self, serve):
        page = {**json.loads(_load("dayforce_search.json")), "maxCount": 3}

        def reply(url, **kw):
            if url.endswith("/api/auth/csrf"):
                return fake_response({"csrfToken": "tok"})
            return fake_response(page if kw["json"]["paginationStart"] == 0
                                 else {**page, "jobPostings": []})
        calls = serve(reply)
        rows = await DAYFORCE.listing("aifire|IMPACT")
        search = [c for c in calls if "jobposting/search" in c.url]
        assert [c.url for c in calls][0] == "https://jobs.dayforcehcm.com/api/auth/csrf"
        assert search[0].url == "https://jobs.dayforcehcm.com/api/geo/aifire/jobposting/search"
        assert search[0].headers["X-CSRF-TOKEN"] == "tok" and len(search) == 1
        assert search[0].kw["json"] == {"clientNamespace": "aifire", "jobBoardCode": "IMPACT",
                                        "cultureCode": "en-US", "distanceUnit": 0,
                                        "paginationStart": 0}
        assert [r["title"] for r in rows] == ["Safety Specialist", "Fire Sprinkler Technician",
                                              "Fire Sprinkler Assistant Project Manager"]
        assert rows[0]["url"] == "https://jobs.dayforcehcm.com/en-US/aifire/IMPACT/jobs/44800"
        assert rows[0]["location"] == "Salt Lake City, UT; Albuquerque, NM; Phoenix, AZ"
        assert rows[0]["id"] == "dayforce_aifire_44800"

    def test_a_board_url_names_its_client_and_board(self):
        def handle(blob):
            return DAYFORCE.detect(blob, lambda p: True)
        assert handle("https://jobs.dayforcehcm.com/en-US/AIFIRE/IMPACT/jobs/43260") == "aifire|IMPACT"
        assert handle("https://jobs.dayforcehcm.com/aan/CANDIDATEPORTAL") == "aan|CANDIDATEPORTAL"
        assert handle("https://jobs.dayforcehcm.com/en-US/aifire") is None
        url = "https://jobs.dayforcehcm.com/en-US/aifire/IMPACT/jobs/43260"
        assert DAYFORCE.job_ref(url) == {"client": "aifire", "board": "IMPACT", "jid": "43260"}


class TestCornerstone:
    async def test_the_home_page_hands_out_the_token_and_the_api_host(self, serve):
        jobs = json.loads(_load("cornerstone_jobs.json"))
        calls = serve({"/home?c=macomtech": fake_response(text=_load("cornerstone_home.html")),
                       "rec-job-search": fake_response(jobs)})
        rows = await CORNERSTONE.listing("macomtech.csod.com|4")
        assert calls[0].url == "https://macomtech.csod.com/ux/ats/careersite/4/home?c=macomtech"
        search = calls[1]
        assert search.url == "https://us.api.csod.com/rec-job-search/external/jobs"
        assert search.headers["Authorization"] == "Bearer eyJhbGciOiJIUzUxMiJ9.cut.cut"
        assert search.kw["json"] == {"careerSiteId": "4", "careerSitePageId": "4", "pageNumber": 1,
                                     "pageSize": 100, "cultureId": 1, "cultureName": "en-US"}
        assert [r["location"] for r in rows] == ["Morrisville, NC, US", "Durham, NC, US",
                                                 "Morgan Hill, CA, US"]
        assert rows[0]["url"] == ("https://macomtech.csod.com/ux/ats/careersite/4/home/"
                                  "requisition/3727?c=macomtech")
        assert rows[0]["id"] == "cornerstone_macomtech_4_3727" and len(calls) == 2

    async def test_a_posting_is_open_while_its_page_carries_it(self, serve):
        page = ('<script type="application/ld+json">{"@type": "JobPosting", '
                          '"title": "Engineer", "description": "<p>Build.</p>"}</script>')
        url = "https://macomtech.csod.com/ux/ats/careersite/4/home/requisition/3727?c=macomtech"
        serve({"/home/requisition/3727": fake_response(text=page),
               "/home/requisition/1": fake_response(text="<html></html>"),
               "/home?c=": fake_response(text=_load("cornerstone_home.html"))})
        assert (await CORNERSTONE.probe_job(url))[0] is True
        gone = url.replace("3727", "1")
        assert (await CORNERSTONE.probe_job(gone))[0] is False

    def test_a_career_site_url_names_its_host_and_site(self):
        blob = "https://macomtech.csod.com/ux/ats/careersite/4/job/3721?c=macomtech"
        assert CORNERSTONE.detect(blob, lambda p: True) == "macomtech.csod.com|4"
        assert CORNERSTONE.job_ref(blob) == {"host": "macomtech.csod.com", "site": "4",
                                             "jid": "3721"}
