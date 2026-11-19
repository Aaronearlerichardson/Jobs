"""Comeet (its `config.BOARDS` spec). `comeet_board.html` is a Comeet
careers page (CHEQ) with three of its positions, recorded live on
2026-10-07, the bodies cut and contact details redacted.
"""

from conftest import fake_response, fixture
from src.ats.board import board_for

COMEET = board_for("comeet")


class TestComeet:
    async def test_a_row_is_a_position_with_its_office_and_department(self, serve):
        log = serve(fake_response(text=fixture("comeet_board.html")))
        rows = await COMEET.listing("cheq|65.005")
        assert [r["id"] for r in rows] == ["comeet_cheq_DD.B6F", "comeet_cheq_AF.242",
                                           "comeet_cheq_31.15F"]
        assert rows[0]["title"] == "AI Engineer"
        assert rows[0]["url"] == "https://www.comeet.com/jobs/cheq/65.005/ai-engineer/DD.B6F"
        assert rows[0]["location"] == "Tel Aviv, IL"
        assert rows[0]["head"].endswith("Data & Analytics")
        assert len(log) == 1  # one page holds every position
