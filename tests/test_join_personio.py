"""JOIN, Personio and Manatal (their `config.BOARDS` specs).

`tests/fixtures/join_board.html` is page one of a JOIN company page (bexio),
`personio_feed.xml` four positions of a Personio tenant's XML feed (clark),
`manatal_board.json` three jobs of a Manatal page API answer; all recorded
live on 2026-10-07, the long bodies and keywords cut.
"""

from conftest import fake_response, fixture
from src.ats.board import board_for

JOIN = board_for("joincom")
PERSONIO = board_for("personio")


class TestJoin:
    async def test_a_row_is_a_posting_with_its_place_and_category(self, serve):
        log = serve(fake_response(text=fixture("join_board.html")))
        rows = await JOIN.listing("bexio")
        assert len(rows) == 5 and len({r["id"] for r in rows}) == 5
        assert rows[0]["id"] == "joincom_bexio_16789521"
        assert rows[0]["url"] == "https://join.com/companies/bexio/16789521-ai-engineer-m-w-d-80-100"
        assert rows[0]["location"] == "Rapperswil-Jona, Switzerland"
        assert rows[0]["head"].endswith("IT")
        assert len(log) == 2  # pageCount says 2: the second page is asked too

    def test_a_posting_url_names_its_company_and_id(self):
        assert JOIN.job_ref("https://join.com/companies/bexio/16789521-ai-engineer") == {
            "slug": "bexio", "jid": "16789521"}


class TestPersonio:
    async def test_a_row_is_a_position_with_its_office_and_department(self, serve):
        serve(fake_response(text=fixture("personio_feed.xml")))
        rows = await PERSONIO.listing("clark")
        assert [(r["id"], r["location"]) for r in rows][:2] == [
            ("personio_clark_2415353", "Berlin; Frankfurt am Main"),("personio_clark_2377588", "Remote")]
        assert rows[0]["title"] == "(Senior) CRM Manager (m/w/d)"
        assert rows[0]["url"] == "https://clark.jobs.personio.de/job/2415353"
        assert rows[0]["head"].endswith("Group - Marketing")

    async def test_a_description_is_every_section_headed_by_its_name(self, serve):
        serve(fake_response(text=fixture("personio_feed.xml")))
        rows = await PERSONIO.listing("clark")
        description = rows[0]["description"]
        assert description.startswith("Wer wir sind\n") and "Deine Aufgaben\n" in description
        assert "<" not in description and "CDATA" not in description

    def test_a_posting_url_names_its_tenant_and_id(self):
        assert PERSONIO.job_ref("https://clark.jobs.personio.de/job/2415353?language=en") == {
            "slug": "clark", "jid": "2415353"}


class TestManatal:
    async def test_a_row_is_a_job_with_its_place_body_and_organization(self, serve):
        log = serve(fake_response(fixture("manatal_board.json")))
        rows = await board_for("manatal").listing("manatal")
        assert rows[0]["id"] == "manatal_manatal_QW3VVV8W"
        assert rows[0]["url"] == "https://www.careers-page.com/manatal/job/QW3VVV8W"
        assert rows[0]["location"] == "Bangkok, Bangkok, Thailand"
        assert "<" not in rows[0]["description"] and "Manatal" in rows[0]["description"]
        assert len(log) == 2  # count 33 at 20 a page: a second page is asked

    def test_a_posting_url_names_its_company_and_hash(self):
        assert board_for("manatal").job_ref("https://www.careers-page.com/manatal/job/L975Y966") == {
            "slug": "manatal", "jid": "L975Y966"}
