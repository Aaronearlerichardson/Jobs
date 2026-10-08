"""Every board spec, checked once per spec or once over all of them.

Per spec: a dead board reads as nothing, a pulled posting is a 404, a
vendor's own host is no board, a board URL resolves to its handle and a
posting URL to its job reference. Over `config.BOARDS`: the schema refuses
what it cannot read, no spec restates a default, every platform detected is
a spec, every fetchable one has a canary, and nothing outside the spec names
a platform. Each spec's listing and posting tests live in its own file
(`test_gem.py`, `test_teamtailor.py`, ...).

Offline: fixtures are served, the source is parsed with `ast`.
"""

import ast
from pathlib import Path
from typing import get_args

import pytest

from conftest import fake_response, fixture
from src import config, tags
from src.ats import signatures
from src.ats.board import BOARDS, board_for, company, spec
from src.ats.registry import seed_tag_for
from src.ats.signatures import detect
from src.rows import FetchedJob, FitColumns

#: ats -> (handle, a posting URL on its board, a served posting). Only the
#: platforms whose posting URL is judged by its own detail endpoint: a spec
#: that closes by `listing` or `page` has no per-posting 404 to test.
POSTINGS = {
    "gem": ("resprop", "https://jobs.gem.com/resprop/am9icG9zdDod14nfMKbLHoHk2zS1wEih",
            lambda: fake_response(fixture("gem_job_post.json"))),
    "teamtailor": ("slaterconsult.teamtailor.com",
                   "https://slaterconsult.teamtailor.com/jobs/5583037-automation-engineer",
                   lambda: fake_response(text="<html><body>x</body></html>")),
    "recruiterbox": ("aprco", "https://aprco.hire.trakstar.com/jobs/fk0ztte/",
                     lambda: fake_response(text=fixture("recruiterbox_job.html"))),
    "greenhouse": ("acme", "https://job-boards.greenhouse.io/acme/jobs/4277627009",
                   lambda: fake_response({"id": 4277627009, "content": "&lt;p&gt;Build.&lt;/p&gt;"})),
    "lever": ("acme", "https://jobs.lever.co/acme/2e1a8d40-0f2b-4c7e-9a11-5b6c7d8e9f01",
              lambda: fake_response({"text": "Eng", "descriptionPlain": "Build."})),
    "bamboohr": ("acme", "https://acme.bamboohr.com/careers/29",
                 lambda: fake_response(fixture("bamboohr_detail.json"))),
    "rippling": ("acme", "https://ats.rippling.com/acme/jobs/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
                 lambda: fake_response(fixture("rippling_detail.json"))),
    "workable": ("acme", "https://apply.workable.com/acme/j/8A1B2C3D4E/",
                 lambda: fake_response(fixture("workable_job_detail.json"))),
    "smartrecruiters": ("Acme", "https://jobs.smartrecruiters.com/Acme/3743990014860306",
                        lambda: fake_response({"id": "3743990014860306", "active": True})),
    "workday": (("acme", 5, "External"),
                "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Durham-NC/Eng_R1",
                lambda: fake_response({"jobPostingInfo": {"title": "Engineer",
                                                          "jobDescription": "<p>Build.</p>"}})),
    "infor": ("css-acme-prd.inforcloudsuite.com|42",
              "https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/"
              "JobPosting%5BJobPostingSet%5D%2842%2C207651%2C1%29.JobPostingDisplay"
              "?pagesize=1&csk.JobBoard=EXTERNAL&csk.HROrganization=42",
              lambda: fake_response({"fields": {"PostingDateRange_prd_End": {"value": "00000000"}}})),
    "jazzhr": ("acme", "https://acme.applytojob.com/apply/8LvYWTHbW7/Data-Engineer",
               lambda: fake_response(text="<html><body>x</body></html>")),
    "oracle": ("fa-eups-saasfaprod1.fa.ocs.oraclecloud.com|ULSolutionsCareers",
               "https://fa-eups-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/"
               "sites/ULSolutionsCareers/job/10139",
               lambda: fake_response(fixture("oracle_job_detail.json"))),
    "eightfold": ("acme.eightfold.ai", "https://acme.eightfold.ai/careers/job/1",
                  lambda: fake_response(fixture("eightfold_apply_job.json"))),
    "adp": ("9a6de238-e301-469b-8a29-d35b7eaeebd9|19000101_000001",
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=9a6de238-e301-469b-8a29-d35b7eaeebd9&ccId=19000101_000001&jobId=123456&lang=en_US",
            lambda: fake_response(fixture("adp_detail.json"))),
    "amazon": ("North Carolina", "https://www.amazon.jobs/en/jobs/10509450/data-engineer",
               lambda: fake_response(fixture("amazon_search.json"))),
    "joincom": ("bexio", "https://join.com/companies/bexio/16789521-ai-engineer-m-w-d-80-100",
             lambda: fake_response(text="<html><body>x</body></html>")),
    "personio": ("clark", "https://clark.jobs.personio.de/job/2415353",
                 lambda: fake_response(text="<html><body>x</body></html>")),
    "manatal": ("manatal", "https://www.careers-page.com/manatal/job/L975Y966",
                lambda: fake_response(text="<html><body>x</body></html>")),
    "zohorecruit": ("biotech", "https://biotech.zohorecruit.com/jobs/Careers/474128000070267033",
                    lambda: fake_response(text=fixture("zohorecruit_job.html"))),
}

COMEET_POSTING = "https://www.comeet.com/jobs/cheq/65.005/ai-engineer/DD.B6F"

#: a board's own URLs, each resolving to (ats, handle)
BOARD_URLS = [
    ("https://jobs.gem.com/resprop", "gem", "resprop"),
    (POSTINGS["gem"][1], "gem", "resprop"),
    ("https://slaterconsult.teamtailor.com/jobs", "teamtailor", "slaterconsult.teamtailor.com"),
    (POSTINGS["teamtailor"][1], "teamtailor", "slaterconsult.teamtailor.com"),
    ("https://slaterconsult.teamtailor.com/", "teamtailor", "slaterconsult.teamtailor.com"),
    ("https://aprco.hire.trakstar.com/", "recruiterbox", "aprco"),
    (POSTINGS["recruiterbox"][1], "recruiterbox", "aprco"),
    ("https://aprco.recruiterbox.com/jobs/fk0ztte/", "recruiterbox", "aprco"),
    ("https://highlights-healthcare.breezy.hr/", "breezy", "highlights-healthcare"),
    ("https://highlights-healthcare.breezy.hr/p/9b5dbab30b78-x", "breezy", "highlights-healthcare"),
    ("https://hudsonmanpower.recruitee.com/o/head-of-recruitment-operations", "recruitee",
     "hudsonmanpower"),
    ("https://trilongroup.pinpointhq.com/en/postings/6547427b-f71c", "pinpoint", "trilongroup"),
    ("https://boards.greenhouse.io/acme", "greenhouse", "acme"),
    (POSTINGS["greenhouse"][1], "greenhouse", "acme"),
    ("https://jobs.lever.co/acme", "lever", "acme"),
    (POSTINGS["lever"][1], "lever", "acme"),
    ("https://jobs.ashbyhq.com/acme", "ashby", "acme"),
    ("https://jobs.ashbyhq.com/acme/f21013b3-0152-49d9-accb-3a46d33c8a82", "ashby", "acme"),
    ("https://acme.bamboohr.com/careers", "bamboohr", "acme"),
    (POSTINGS["bamboohr"][1], "bamboohr", "acme"),
    ("https://ats.rippling.com/acme/jobs", "rippling", "acme"),
    (POSTINGS["rippling"][1], "rippling", "acme"),
    ("https://acme.careers.hibob.com/jobs", "hibob", "acme"),
    ("https://apply.workable.com/acme/", "workable", "acme"),
    (POSTINGS["workable"][1], "workable", "acme"),
    ("https://recruiting.paylocity.com/Recruiting/Jobs/All/d527ad39-680d-45fa-9178-38a81898aec2",
     "paylocity", "d527ad39-680d-45fa-9178-38a81898aec2"),
    ("https://recruiting.ultipro.com/BAY1006BML/JobBoard/0669eed3-5441-4f8e-a7b1-c5df596a4dfe/",
     "ultipro", "BAY1006BML|0669eed3-5441-4f8e-a7b1-c5df596a4dfe"),
    ("https://recruiting2.ultipro.com/BAY1006BML/JobBoard/0669eed3-5441-4f8e-a7b1-c5df596a4dfe/"
     "OpportunityDetail?opportunityId=1",
     "ultipro", "BAY1006BML|0669eed3-5441-4f8e-a7b1-c5df596a4dfe"),
    ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
     "?cid=9a6de238-e301-469b-8a29-d35b7eaeebd9&ccId=19000101_000001&lang=en_US",
     "adp", "9a6de238-e301-469b-8a29-d35b7eaeebd9|19000101_000001"),
    (POSTINGS["adp"][1], "adp", "9a6de238-e301-469b-8a29-d35b7eaeebd9|19000101_000001"),
    ("https://careers.smartrecruiters.com/Eurofins", "smartrecruiters", "Eurofins"),
    (POSTINGS["smartrecruiters"][1], "smartrecruiters", "Acme"),
    ("https://acme.wd5.myworkdayjobs.com/External", "workday", ("acme", 5, "External")),
    (POSTINGS["workday"][1], "workday", ("acme", 5, "External")),
    (POSTINGS["infor"][1], "infor", "css-acme-prd.inforcloudsuite.com|42"),
    ("https://acme.applytojob.com/apply", "jazzhr", "acme"),
    (POSTINGS["jazzhr"][1], "jazzhr", "acme"),
    ("https://jobs.jobvite.com/neogenomics", "jobvite", "neogenomics"),
    ("https://jobs.jobvite.com/neogenomics/job/oAbC1dEf", "jobvite", "neogenomics"),
    ("https://careers.kula.ai/precision-neuroscience", "kula", "precision-neuroscience"),
    ("https://www.amazon.jobs/en/search?base_query=&region=North%20Carolina", "amazon",
     "North Carolina"),
    ("https://join.com/companies/bexio", "joincom", "bexio"),
    (POSTINGS["joincom"][1], "joincom", "bexio"),
    ("https://clark.jobs.personio.de/", "personio", "clark"),
    (POSTINGS["personio"][1], "personio", "clark"),
    ("https://www.careers-page.com/manatal", "manatal", "manatal"),
    (POSTINGS["manatal"][1], "manatal", "manatal"),
    ("https://www.comeet.com/jobs/cheq/65.005", "comeet", "cheq|65.005"),
    (COMEET_POSTING, "comeet", "cheq|65.005"),
    ("https://biotech.zohorecruit.com/jobs/Careers", "zohorecruit", "biotech"),
    (POSTINGS["zohorecruit"][1], "zohorecruit", "biotech"),
    ("https://acme.icims.com/jobs/search", "icims", "acme"),
    ("https://careers-acme.icims.com/jobs/42423/data-engineer/job", "icims", "careers-acme"),
    ("https://unc.peopleadmin.com/postings/123", "peopleadmin", "unc"),
    ("https://recruitingbypaycor.com/career/CareerHome.action?clientId="
     "8a788267543c64a8015453881fd50633", "paycor", "8a788267543c64a8015453881fd50633"),
    ("https://fa-eups-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
     "ULSolutionsCareers/jobs", "oracle", "fa-eups-saasfaprod1.fa.ocs.oraclecloud.com|ULSolutionsCareers"),
    (POSTINGS["oracle"][1], "oracle", "fa-eups-saasfaprod1.fa.ocs.oraclecloud.com|ULSolutionsCareers"),
    ("https://arcadis.eightfold.ai/careers", "eightfold", "arcadis.eightfold.ai"),
    (POSTINGS["eightfold"][1], "eightfold", "acme.eightfold.ai"),
    ("https://jobs.dayforcehcm.com/en-US/aifire/IMPACT", "dayforce", "aifire|IMPACT"),
    ("https://jobs.dayforcehcm.com/en-US/aifire/IMPACT/jobs/43260", "dayforce", "aifire|IMPACT"),
    ("https://macomtech.csod.com/ux/ats/careersite/4/home?c=macomtech", "cornerstone",
     "macomtech.csod.com|4"),
    ("https://macomtech.csod.com/ux/ats/careersite/4/home/requisition/3727?c=macomtech",
     "cornerstone", "macomtech.csod.com|4"),
    ("https://phh.tbe.taleo.net/phh04/ats/careers/v2/searchResults?org=NFINDY&cws=37", "taleo",
     "phh.tbe.taleo.net/phh04|NFINDY|37"),
    ("https://phh.tbe.taleo.net/phh04/ats/careers/v2/viewRequisition?org=NFINDY&cws=37&rid=17",
     "taleo", "phh.tbe.taleo.net/phh04|NFINDY|37"),
    ("https://careers.unifiservice.com/careers/SearchJobs", "avature",
     "https://careers.unifiservice.com/careers"),
    ("https://careers.unifiservice.com/careers/JobDetail/Engineer/14712", "avature",
     "https://careers.unifiservice.com/careers"),
]

#: ats, a posting URL, the job reference it names (None: not a posting)
JOB_REFS = [
    ("avature", "https://careers.unifiservice.com/careers/JobDetail/"
     "1160-RDU-Delta-Cabin-CB2SUP-Cabin-Supervisor-FT/14712",
     {"base": "https://careers.unifiservice.com/careers", "jid": "14712"}),
    ("comeet", COMEET_POSTING, {"slug": "cheq", "cid": "65.005", "jid": "DD.B6F"}),
    ("gem", POSTINGS["gem"][1], {"slug": "resprop", "jid": "am9icG9zdDod14nfMKbLHoHk2zS1wEih"}),
    ("gem", "https://jobs.gem.com/the-swift-group/4123291008",
     {"slug": "the-swift-group", "jid": "4123291008"}),
    ("joincom", "https://join.com/companies/bexio/16789521-ai-engineer",
     {"slug": "bexio", "jid": "16789521"}),
    ("personio", "https://clark.jobs.personio.de/job/2415353?language=en",
     {"slug": "clark", "jid": "2415353"}),
    ("manatal", POSTINGS["manatal"][1], {"slug": "manatal", "jid": "L975Y966"}),
    ("recruiterbox", POSTINGS["recruiterbox"][1], {"slug": "aprco", "jid": "fk0ztte"}),
    ("recruiterbox", "https://aprco.recruiterbox.com/jobs/fk0ztte/",
     {"slug": "aprco", "jid": "fk0ztte"}),
    ("zohorecruit", "https://biotech.zohorecruit.com/jobs/Careers/474128000070267033/Project-Engineer",
     {"slug": "biotech", "jid": "474128000070267033"}),
    ("teamtailor", POSTINGS["teamtailor"][1],
     {"slug": "slaterconsult.teamtailor.com", "jid": "5583037"}),
    ("teamtailor", "https://careers.example.com/jobs/5583037-automation-engineer", None),
    ("taleo", "https://phh.tbe.taleo.net/phh04/ats/careers/v2/viewRequisition?org=NFINDY&cws=37&rid=17",
     {"site": "phh.tbe.taleo.net/phh04", "org": "NFINDY", "cws": "37", "jid": "17"}),
]

VENDOR_URLS = [
    "https://api.gem.com/job_board/v0/resprop/job_posts/",
    "https://www.teamtailor.com/en/", "https://app.teamtailor.com/login",
    "https://www.recruiterbox.com/", "https://app.hire.trakstar.com/",
    "https://app.breezy.hr/signin", "https://www.recruitee.com/",
    "https://developers.pinpointhq.com/docs", "https://www.jobs.personio.de/",
    "https://join.com/companies/sitemap", "https://www.comeet.com/jobs/sitemap/65.005",
    "https://www.zohorecruit.com/", "https://accounts.zohorecruit.com/jobs/Careers",
    "https://career4.successfactors.com/career?company=acme",
    "https://acme.successfactors.com/",
    *(f"https://{host}.eightfold.ai/" for host in ("www", "app", "apply", "docs", "support")),
]


#: every platform with a listing, with the handle its canary names
FETCHABLE = {n: str(b.get("canary", {}).get("handle", "no-such-board"))
             for n, b in config.BOARDS.items() if "listing" in b}


@pytest.mark.parametrize("ats,slug", sorted(FETCHABLE.items()))
async def test_a_dead_board_reads_as_nothing(serve, ats, slug):
    serve(fake_response(status=404))
    assert await company.fetch_company({"ats": ats, "slug": slug}) == []


def test_every_platform_with_a_detectable_form_has_a_row():
    """A platform added with a host pattern but no table row goes untested:
    its board URL must resolve (`BOARD_URLS`), and a posting judged by its own
    detail endpoint (no `listing`/`page` closure, no prelude) must 404 shut
    (`POSTINGS`). `jibe`, `phenom` and `successfactors` detect from a page's
    text, not a URL (SuccessFactors' own pages are `off_page`)."""
    urlable = {n for n, b in config.BOARDS.items() if n in FETCHABLE
               and any("host" in rule for rule in b.get("detect") or ())
               and not any("off_page" in rule for rule in b.get("detect") or ())}
    judged = {n for n, b in config.BOARDS.items() if n in FETCHABLE
              and b.get("job_ref") and b.get("detail") and not b.get("handle", {}).get("prelude")
              and b.get("closure", {}).get("via") is None}
    assert urlable - {ats for _u, ats, _s in BOARD_URLS} == set()
    assert judged - set(POSTINGS) == set()


def test_the_tables_name_only_platforms_in_boards():
    """A hand-kept table naming a platform that is not in `config.BOARDS` is stale."""
    named = set(POSTINGS) | {ats for _u, ats, _s in BOARD_URLS}
    assert named <= set(FETCHABLE)


@pytest.mark.parametrize("ats", sorted(POSTINGS))
@pytest.mark.parametrize("status,want", [(200, True), (404, False)])
async def test_a_pulled_posting_is_a_404(serve, ats, status, want):
    _slug, posting, served = POSTINGS[ats]
    serve(served() if status == 200 else fake_response(text="", status=status))
    assert (await board_for(ats).probe_job(posting, "x"))[0] is want


@pytest.mark.parametrize("url,ats,slug", BOARD_URLS)
def test_a_board_url_is_a_fetchable_board(url, ats, slug):
    assert detect("", url) == ("fetchable", ats, slug)


@pytest.mark.parametrize("ats,url,ref", JOB_REFS)
def test_a_posting_url_names_its_job_ref(ats, url, ref):
    assert board_for(ats).job_ref(url) == ref


@pytest.mark.parametrize("url", VENDOR_URLS)
def test_a_vendor_host_is_no_board(url):
    assert detect("", url) is None


ROOT = Path(__file__).resolve().parent.parent


_WHY = "a quirk, 2026-09"
_L = {"url": "https://x.test/{slug}", "fields": {"id": "id"}}
_D = {"url": "https://x.test/{jid}"}


def _listing(**kw):
    return {"listing": {**_L, **kw}}


def _pager(**kw):
    return _listing(pager=kw)


def _always(**kw):
    """A rescue on every pull, one key changed (a None one dropped)."""
    rescue = {"when": "always", "unknown": "x", "cap": 1, "why": _WHY, **kw}
    return {**_listing(), "detail": _D, "rescue": {k: v for k, v in rescue.items() if v is not None}}


#: Specs the schema must refuse, one broken key each.
REFUSED = [
    {"sweeps": True}, {"sweep": "yes"}, {"canary": {"name": "A", "handle": 7}},
    {"canary": {"name": "A", "handle": "a", "min_jobs": 0}}, {"unlocated": "maybe"},
    _listing(nope=1), _listing(fields={"salary": "pay"}), _listing(url="{slug|nope}"),
    _listing(fields={"title": {"of": "t", "transform": "nope"}}),
    _listing(fields={"title": {"of": "t", "join": ["a"]}}),
    _listing(decoder={"kind": "xml"}), _listing(decoder={"select": "a"}),
    _listing(decoder={"kind": "json_in_html"}),
    _listing(decoder={"kind": "json_in_html", "regex": "("}),
    _listing(decoder={"kind": "html"}), _listing(decoder={"kind": "html", "select": ""}),
    _listing(decoder={"kind": "html", "select": "a[href*='/{slug}/' i]"}),
    _listing(decoder={"kind": "jsonld", "cells": {"d": "dt:-soup-contains('Pay') + dd"}}),
    _pager(kind="scroll", size=1), _pager(kind="offset", size=0),
    _pager(kind="offset", size=2, step=1), _pager(kind="overlap", size=2, step=2, why=_WHY),
    _pager(kind="cursor", size=2), _pager(kind="offset", size=2, ceiling="2000", why=_WHY),
    _pager(kind="offset", size=2, ceiling=2000, why="because"),
    _listing(scope={"kind": "facets", "facets": "f"}), _listing(scope={"kind": "param"}),
    {"handle": {"columns": []}}, {"handle": {"nope": 1}}, {"handle": {"follow": {"base": 1}}},
    {"handle": {"try": {"a": ["x"], "b": ["y"]}}}, {"handle": {"try": {"a": []}}},
    {"handle": {"accept": {"nope": 1}, "why": _WHY}},
    {"listing": [_L, {"url": "https://x.test/2"}]},
    {"listing": [{**_L, "why": _WHY}, {"url": "https://x.test/2", "why": _WHY}]},
    {**_listing(), "rescue": {"unknown": "x", "cap": 1, "why": _WHY}},
    {**_listing(), "detail": _D, "rescue": {"unknown": "x", "cap": 1, "why": _WHY}},
    _always(unknown="("), _always(cap="1"), _always(fields=["pay"]), _always(why=None),
    _always(fields=["department"]), _always(fields=["title"]),
    {"closure": {"nope": 1}}, {"closure": {"via": "email"}}, {"closure": {"via": "detail"}},
    {**_pager(kind="offset", size=2), "closure": {"via": "listing"}},
    {"closure": {"unmatched": 7, "why": _WHY}}, {"closure": {"unmatched": "gone", "why": "because"}},
    {"closure": {"closed": [{"why": {"const": "x"}}]}},
    {"closure": {"closed": [{"when": {"truthy": "a"}, "if": 1}]}},
    {"closure": {"open": {"maybe": "a"}}},
    {"job_ref": {"re": "(", "parts": []}}, {"job_ref": {"re": "a/(\\d+)", "parts": []}},
    {"employer": {"of": "a", "transform": "nope"}}, {"employer": 7},
    {"detect": [{}]}, {"detect": [{"re": ["abc"]}]},
    {"detect": [{"re": ["(a)"], "transform": ["x"]}]},
    {"detect": [{"re": ["(a)"], "transform": [None, None]}]},
    {"detect": [{"re": ["(a)"], "blocklist": [1]}]},
    {"detect": [{"re": ["(a)"], "careers_url": "{slug|nope}"}]},
]


def test_the_schema_refuses_what_it_cannot_read():
    """Each spec in REFUSED breaks one rule; the error names the spec. An
    adaptation key needs no `why`."""
    spec.parse("ok", _pager(kind="offset", size=2, ceiling=2000))
    spec.parse("ok", _pager(kind="offset", size=2, why=_WHY))
    for i, raw in enumerate(REFUSED):
        with pytest.raises(ValueError, match=f"^case{i}: "):
            spec.parse(f"case{i}", raw)


def test_the_engine_row_names_the_spec_row_fields():
    """A row holds the spec's row fields but `department` (it goes into
    `head`), plus `head` and `_free`; a rescue fills only some of them."""
    keys = set(spec.EngineRow.__annotations__)
    assert keys == (spec.ROW_FIELDS - {"department"}) | {"head", "_free"}
    assert set(get_args(spec.FillField)) <= keys


def test_the_fetched_job_names_the_row_fields_and_fit_columns():
    """A fetched job holds the spec's row fields but `department`, every fit
    column a score stamps, and none of the engine row's own keys."""
    keys = set(FetchedJob.__annotations__)
    assert spec.ROW_FIELDS - {"department"} <= keys
    assert set(FitColumns.__annotations__) <= keys
    assert not {"head", "_free"} & keys


def test_no_key_restates_its_default():
    """A key a spec sets to its model's default (or `closure.via` to the
    one it would resolve to) does nothing: drop it."""
    dead = [f"{name}.{path}" for name, b in BOARDS.items()
            for path, value, given, default in spec.walk(b.spec) if given and value == default]
    dead += [f"{name}.closure.via" for name, b in BOARDS.items()
             if b.spec.closure.via == b.spec.default_via]
    assert not dead


def test_detect_returns_only_board_keys():
    """Over every recorded fixture page, and each canary's own handle:
    a detection names a spec, fetchable exactly when it has a listing."""
    texts = [p.read_text(encoding="utf-8", errors="replace")
             for p in (ROOT / "tests" / "fixtures").rglob("*") if p.is_file()]
    texts += [str(s["canary"]["handle"]) for s in config.BOARDS.values() if "canary" in s]
    hits = [h for t in texts for h in (signatures.detect(t), signatures.detect("", t)) if h]
    assert hits
    for kind, ats, _slug in hits:
        assert ats in config.BOARDS
        assert kind == ("fetchable" if config.BOARDS[ats].get("listing") else "lead")


#: Fetchable specs with no canary yet, and why.
NO_CANARY = {"hibob": "the store's only board reports a total of 0"}


def test_every_fetchable_spec_has_a_canary():
    missing = {n for n, s in config.BOARDS.items() if s.get("listing") and "canary" not in s}
    assert missing == set(NO_CANARY)


def test_the_seed_tag_follows_sweep():
    for name, raw in config.BOARDS.items():
        want = (tags.SWEEP if raw.get("sweep") else tags.LOCAL) if raw.get("listing") else None
        assert seed_tag_for(name) == want, name


def _literals(path):
    """The string constants in a module, docstrings left out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)}
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs}


def test_no_platform_names_in_src():
    """No `config.BOARDS` key is a string literal anywhere in src/ but
    config/boards.py: a platform's rules are its spec's data."""
    named = {}
    for path in (ROOT / "src").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        hits = _literals(path) & set(config.BOARDS) if not rel.startswith("src/config/boards/") else None
        if hits:
            named[rel] = hits
    assert named == {}
