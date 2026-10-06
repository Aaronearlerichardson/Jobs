"""What every board spec shares, run once per spec: a dead board reads as
nothing, a pulled posting is a 404, a vendor's own host is no board, and a
board URL resolves to its handle. Each spec's listing and posting tests live
in its own file (`test_gem.py`, `test_teamtailor.py`, ...).
"""

import pytest

from conftest import fake_response, fixture
from src.ats.board import board_for, company
from src.ats.signatures import detect

#: ats -> (handle, a posting URL on its board, a served posting)
POSTINGS = {
    "gem": ("resprop", "https://jobs.gem.com/resprop/am9icG9zdDod14nfMKbLHoHk2zS1wEih",
            lambda: fake_response(fixture("gem_job_post.json"))),
    "teamtailor": ("slaterconsult.teamtailor.com",
                   "https://slaterconsult.teamtailor.com/jobs/5583037-automation-engineer",
                   lambda: fake_response(text="<html><body>x</body></html>")),
    "recruiterbox": ("aprco", "https://aprco.hire.trakstar.com/jobs/fk0ztte/",
                     lambda: fake_response(text=fixture("recruiterbox_job.html"))),
}

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
]

VENDOR_URLS = [
    "https://api.gem.com/job_board/v0/resprop/job_posts/",
    "https://www.teamtailor.com/en/", "https://app.teamtailor.com/login",
    "https://www.recruiterbox.com/", "https://app.hire.trakstar.com/",
    "https://app.breezy.hr/signin", "https://www.recruitee.com/",
    "https://developers.pinpointhq.com/docs",
    *(f"https://{host}.eightfold.ai/" for host in ("www", "app", "apply", "docs", "support")),
]


@pytest.mark.parametrize("ats,slug", [
    ("gem", "no-such-board"), ("teamtailor", "nobody.teamtailor.com"), ("recruiterbox", "no-such-board"),
    ("breezy", "no-such-board"), ("recruitee", "no-such-board"), ("pinpoint", "no-such-board")])
async def test_a_dead_board_reads_as_nothing(serve, ats, slug):
    serve(fake_response(status=404))
    assert await company.fetch_company({"ats": ats, "slug": slug}) == []


@pytest.mark.parametrize("ats", sorted(POSTINGS))
@pytest.mark.parametrize("status,want", [(200, True), (404, False)])
async def test_a_pulled_posting_is_a_404(serve, ats, status, want):
    _slug, posting, served = POSTINGS[ats]
    serve(served() if status == 200 else fake_response(text="", status=status))
    assert (await board_for(ats).probe_job(posting, "x"))[0] is want


@pytest.mark.parametrize("url,ats,slug", BOARD_URLS)
def test_a_board_url_is_a_fetchable_board(url, ats, slug):
    assert detect("", url) == ("fetchable", ats, slug)


@pytest.mark.parametrize("url", VENDOR_URLS)
def test_a_vendor_host_is_no_board(url):
    assert detect("", url) is None
