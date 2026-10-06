"""Taleo Business Edition (its `config.BOARDS` spec): a career center keyed on
(site, org, cws), read page by page; a later page asks `next`, which only the
session the first request opened can answer.

`tests/fixtures/taleo_*` are trimmed REAL pages recorded 2026-10-05 from a
staffing firm's career center (phh.tbe.taleo.net/phh04, org NFINDY, cws 37):
three result rows and one posting's JSON-LD.
"""

import re

import pytest

from conftest import fake_response, fixture, no_pacing
from src.ats.board import board_for
from src.ats.signatures import detect

TALEO = board_for("taleo")
SITE = "phh.tbe.taleo.net/phh04"
SLUG = f"{SITE}|NFINDY|37"
POSTING = f"https://{SITE}/ats/careers/v2/viewRequisition?org=NFINDY&cws=37&rid=17"


def full_page(first):
    """Ten rows, a full page, made from the fixture's first row."""
    row = re.search(r"<div class=\"oracletaleocwsv2-accordion-head-info\">.*?</div></div>",
                    fixture("taleo_results.html"), re.S)[0]
    return "<html><body>" + "".join(row.replace("rid=17", f"rid={first + i}")
                                    for i in range(10)) + "</body></html>"


@pytest.fixture
def center(monkeypatch, serve):
    """Serve a career center: the fixture's three rows as one short page, or
    (`full`) two full pages then an empty one, by `rowFrom`."""
    no_pacing(monkeypatch)

    def _install(full=False):
        def reply(url, params=None, **kw):
            params = params or {}
            if not url.endswith("/searchResults"):
                return fake_response(text=fixture("taleo_detail.html"))
            if not full:
                return fake_response(text=fixture("taleo_results.html"))
            start = params.get("rowFrom", 0)
            return fake_response(text=full_page(start) if start < 20 else "<html></html>")
        return serve(reply)
    return _install


class TestListing:
    async def test_a_row_is_the_requisition_with_its_place(self, center):
        center()
        rows = await TALEO.listing(SLUG)
        assert [(r["id"], r["location"]) for r in rows] == [
            ("taleo_nfindy_37_17", "IN - Lafayette"),
            ("taleo_nfindy_37_389", "IN - Indianapolis"),
            ("taleo_nfindy_37_618", "NC - Charlotte")]
        assert rows[0]["url"] == POSTING and rows[2]["title"] == "CNA / PCA"

    async def test_only_a_later_page_asks_next(self, center):
        log = center(full=True)
        assert len(await TALEO.listing(SLUG)) == 20
        asked = [r.params for r in log]
        assert "next" not in asked[0] and asked[0]["org"] == "NFINDY" and asked[0]["cws"] == "37"
        assert [(p["next"], p["rowFrom"]) for p in asked[1:]] == [(1, 10), (2, 20)]


class TestPosting:
    async def test_the_json_ld_gives_the_description(self, center):
        center()
        assert "Certified Nursing Assistants" in await TALEO.description_for(POSTING)

    def test_a_posting_url_names_its_career_center(self):
        assert TALEO.job_ref(POSTING) == {"site": SITE, "org": "NFINDY", "cws": "37", "jid": "17"}


class TestDetection:
    @pytest.mark.parametrize("url", [
        f"https://{SITE}/ats/careers/v2/searchResults?org=NFINDY&cws=37",
        POSTING,
    ])
    def test_a_career_center_url_is_a_fetchable_board(self, url):
        assert detect(url) == ("fetchable", "taleo", SLUG)

    def test_an_enterprise_taleo_site_stays_a_lead(self):
        assert detect("https://acme.taleo.net/careersection/ex/jobsearch.ftl") == (
            "lead", "taleo_enterprise", "acme.taleo.net")
