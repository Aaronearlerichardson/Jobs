"""Amazon (its `config.BOARDS` spec, run by src.ats.board.engine): a search
narrowed server-side to one region (the handle), and a requisition read back.

`tests/fixtures/amazon_search.json` is a trimmed REAL answer recorded 2026-10-07
from `amazon.jobs/en/search.json?region=North Carolina`.
"""

from conftest import fake_response, fixture, no_pacing
from src.ats.board import board_for
from src.ats.signatures import detect

AMAZON = board_for("amazon")


async def test_the_region_search_maps_a_row(monkeypatch, serve):
    no_pacing(monkeypatch)
    log = serve(lambda url, params=None, **kw: fake_response(
        fixture("amazon_search.json") if not params["offset"] else {"hits": 2, "jobs": []}))
    rows = await AMAZON.listing("North Carolina")
    assert log[0].params["region"] == "North Carolina"
    assert [r["id"] for r in rows] == ["amazon_10509450", "amazon_10509449"]
    assert rows[0]["url"].startswith("https://www.amazon.jobs/en/jobs/10509450/")
    assert rows[0]["location"] == "Charlotte, North Carolina, USA"
    assert rows[0]["posted_at"] == "2026-08-20"
    assert rows[0]["description"]


async def test_a_pulled_requisition_is_closed(serve):
    serve(fake_response({"hits": 0, "jobs": []}))
    assert (await AMAZON.probe_job("https://www.amazon.jobs/en/jobs/1/x"))[0] is False


def test_a_region_search_url_is_a_fetchable_board():
    url = "https://www.amazon.jobs/en/search?base_query=&region=North%20Carolina"
    assert detect("", url) == ("fetchable", "amazon", "North Carolina")
