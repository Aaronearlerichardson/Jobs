"""The resolver's domain lookup (resolve.domain) and the two steps it feeds:
a looked-up domain's hosts sniffed after the plain guesses, and the public
board directory asked by exact name. The strict-match and TLD cases are
doctests on `domain.pick_domain`.

Offline: http is `serve`d, the data dir is a tmp_path, and the resolver's
network steps are stubbed as test_parsers.py stubs them.
"""

import json
import time

import pytest

from conftest import answer, fake_response

from src import config
from src.discovery.resolve import board as resolve_board
from src.discovery.resolve import domain, sniffer

CLEARBIT = "https://autocomplete.clearbit.com/v1/companies/suggest?query={q}"
HOSTS = ["{domain}", "careers.{domain}", "jobs.{domain}"]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """The domain cache's file, in a data dir of its own."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DISCOVERY_DOMAIN_LOOKUP_URLS", [CLEARBIT])
    return tmp_path / ".cache" / "domains.json"


def _suggest(*suggestions):
    return fake_response(list(suggestions))


def _wikidata(label, site):
    return {"wbsearchentities": fake_response({"search": [{"id": "Q1", "label": label}]}),
            "wbgetentities": fake_response({"entities": {"Q1": {"claims": {"P856": [
                {"mainsnak": {"datavalue": {"value": site}}}]}}}})}


class TestOfficialDomain:
    async def test_a_strict_match_is_cached(self, serve, cache):
        log = serve({"clearbit": _suggest({"name": "Eli Lilly and", "domain": "lilly.com"})})
        assert await domain.official_domain("Eli Lilly and Company") == "lilly.com"
        sent = len(log)
        assert await domain.official_domain("Eli Lilly and Company") == "lilly.com"
        assert len(log) == sent

    async def test_wikidata_answers_where_the_suggester_has_nothing(self, serve, cache):
        serve({"clearbit": _suggest({"name": "Precision Nutrition",
                                     "domain": "precisionnutrition.com"}),
               **_wikidata("Precision BioSciences", "https://www.precisionbio.com/")})
        assert await domain.official_domain("Precision BioSciences") == "precisionbio.com"

    async def test_wikidata_needs_the_same_label(self, serve, cache):
        log = serve({"clearbit": _suggest(), **_wikidata("Precision Nutrition", "https://x.com/")})
        assert await domain.official_domain("Precision BioSciences") is None
        assert not [r for r in log if "wbgetentities" in r]

    async def test_a_miss_is_cached_seven_days_and_a_domain_thirty(self, serve, cache):
        day = 86400
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({
            "acme": {"domain": None, "at": time.time() - 6 * day},
            "beta": {"domain": None, "at": time.time() - 8 * day},
            "gamma": {"domain": "gamma.com", "at": time.time() - 29 * day},
            "delta": {"domain": "delta.com", "at": time.time() - 31 * day}}))
        log = serve({"clearbit": _suggest()})
        assert await domain.official_domain("Acme") is None
        assert await domain.official_domain("Gamma") == "gamma.com"
        assert not log
        assert await domain.official_domain("Beta") is None
        assert await domain.official_domain("Delta") is None
        assert [r for r in log if "Beta" in r] and [r for r in log if "Delta" in r]

    async def test_a_lookup_that_never_answered_is_not_cached(self, serve, cache):
        serve(500)
        assert await domain.official_domain("Acme") is None
        assert not cache.exists()


class TestSeededResolution:
    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        monkeypatch.setattr(config, "DISCOVERY_DOMAIN_LOOKUP", True)
        monkeypatch.setattr(config, "DISCOVERY_DOMAIN_HOSTS", HOSTS)
        monkeypatch.setattr(resolve_board, "official_domain", answer("lilly.com"))
        monkeypatch.setattr(resolve_board, "validate_board", answer((10, 3)))
        monkeypatch.setattr(resolve_board, "probe_company", answer(
            lambda *a, **k: pytest.fail("the seeded sniff should have won")))

    def _sniffs(self, monkeypatch, wins):
        """sniff_ats answering a board for the `wins` URL; the log
        of the URLs it was asked."""
        asked = []

        async def sniff(name, curl=""):
            asked.append(curl)
            return {"ats": "greenhouse", "slug": "lilly", "careers_url": curl} \
                if curl == wins else None

        monkeypatch.setattr(sniffer, "sniff_ats", sniff)
        return asked

    async def test_hosts_are_sniffed_in_order_to_the_first_board(self, monkeypatch):
        asked = self._sniffs(monkeypatch, "https://careers.lilly.com/")
        hit = await resolve_board.resolve_board_sniff_first("Eli Lilly", websearch=False)
        assert asked == ["", "https://lilly.com/", "https://careers.lilly.com/"]
        assert hit["via"] == "sniff" and hit["nc"] == 3

    async def test_a_given_careers_url_or_a_lookup_switched_off_seeds_nothing(
            self, monkeypatch):
        asked = self._sniffs(monkeypatch, "never")
        monkeypatch.setattr(resolve_board, "probe_company", answer(None))
        monkeypatch.setattr(resolve_board, "websearch_board", answer(None))
        await resolve_board.resolve_board_sniff_first("Eli Lilly", "https://x.org/")
        monkeypatch.setattr(config, "DISCOVERY_DOMAIN_LOOKUP", False)
        await resolve_board.resolve_board_sniff_first("Eli Lilly")
        assert asked == ["https://x.org/", ""]

    async def test_a_miss_is_classified_on_the_domain_too(self, monkeypatch):
        seen = []

        async def classify(name, curl):
            seen.append(curl)
            return "ats-unsupported:taleo" if "careers." in curl else "no-board-found"

        monkeypatch.setattr(resolve_board, "_classify", classify)
        assert await resolve_board.classify_miss("Eli Lilly") == "ats-unsupported:taleo"
        assert seen == ["", "https://lilly.com/", "https://careers.lilly.com/"]


class TestDirectoryStep:
    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        monkeypatch.setattr(config, "DISCOVERY_DOMAIN_LOOKUP", False)
        monkeypatch.setattr(sniffer, "sniff_ats", answer(None))
        monkeypatch.setattr(resolve_board, "probe_company", answer(None))
        monkeypatch.setattr(resolve_board, "websearch_board", answer(None))

    def _directory(self, monkeypatch, found):
        monkeypatch.setattr(resolve_board, "find_boards", answer(found))

    async def test_the_candidate_with_most_local_jobs_wins_as_via_directory(
            self, monkeypatch):
        self._directory(monkeypatch, [
            ("greenhouse", "acme", "https://boards.greenhouse.io/acme"),
            ("lever", "acme", "https://jobs.lever.co/acme")])
        monkeypatch.setattr(resolve_board, "validate_board", answer(
            lambda comp: (9, 4) if comp["ats"] == "lever" else (20, 1)))
        hit = await resolve_board.resolve_board_sniff_first("Acme")
        assert (hit["ats"], hit["via"], hit["nc"]) == ("lever", "directory", 4)

    async def test_a_foreign_board_is_not_taken(self, monkeypatch):
        self._directory(monkeypatch, [("workday", ("danaher", 1, "Jobs"), "")])
        monkeypatch.setattr(resolve_board, "foreign_board", answer(True))
        monkeypatch.setattr(resolve_board, "validate_board", answer(
            lambda comp: pytest.fail("a foreign board was fetched")))
        assert await resolve_board.resolve_board_sniff_first("Cepheid") is None
