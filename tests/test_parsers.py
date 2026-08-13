"""Offline parsing/detection: board sniffing, custom-board link detection,
the HN thread parser, posting-date normalisation, age tags, and the
closed-posting probe guards. No network."""

from datetime import datetime, timedelta

import pytest
from bs4 import BeautifulSoup

import core.digest_md as digest_md
import discovery.ats_dork as dork
import discovery.sniffer as sniffer
import scrapers.fetchers.company as company_fetch
from scrapers.util import norm_posted_date


class TestSniffer:
    def test_extracts_lever_and_greenhouse_slugs(self):
        boards = dork.extract_boards_from_urls([
            "https://jobs.lever.co/bioagilytix/x",
            "https://boards.greenhouse.io/pendo/jobs/1"])
        assert {("lever", "bioagilytix"), ("greenhouse", "pendo")} <= set(boards)

    def test_custom_board_needs_real_job_links(self):
        assert not sniffer._looks_like_custom_board("<a href='/careers/'>Careers</a>")

    def test_detects_adp_cid_ccid(self):
        url = ("workforcenow.adp.com/x?cid=d290c04e-0230-4cd9-8bf0-f116bfab1405"
               "&ccid=19000101_000003")
        assert sniffer._detect(url)[1] == "adp"

    def test_detects_lead_platform(self):
        assert sniffer._detect("via acme.eightfold.ai portal")[0] == "lead"

    def test_probes_cover_sniffable_atses(self):
        from discovery.probes import PROBES
        assert {"greenhouse", "lever", "ashby", "kula", "jazzhr", "bamboohr",
                "smartrecruiters"} <= set(PROBES)


class TestCustomBoardLinks:
    def test_real_job_links_detected(self):
        html = ('<a href="/careers/facilities-engineer-88">Facilities Engineer</a>'
                '<a href="/careers/quality-engineer-19">Quality Engineer</a>'
                '<a href="/careers/data-scientist-3">Data Scientist</a>')
        links = company_fetch.find_job_links(BeautifulSoup(html, "html.parser"))
        assert len(links) == 3

    def test_nav_links_rejected(self):
        html = ('<a href="/careers/open-positions/">Careers</a>'
                '<a href="/careers/career-opportunities/">View Current Job Openings</a>'
                '<a href="/careers/career-opportunities/">Career Opportunities</a>')
        assert company_fetch.find_job_links(BeautifulSoup(html, "html.parser")) == []

    def test_aggregator_host_is_never_a_custom_board(self):
        assert company_fetch.custom_board_listing_url(
            "https://www.indeed.com/jobs?q=x", "<html></html>") is None


class TestHnParser:
    def test_role_found_out_of_order(self):
        from scrapers.fetchers.hnhiring import _parse_post
        _, role, loc, _ = _parse_post(
            "Acme Neuro | Remote (US) | $150k-190k | Senior ML Engineer | Full-time")
        assert role == "Senior ML Engineer"
        assert "Remote" in loc

    def test_inc_suffix_not_chopped(self):
        from scrapers.fetchers.hnhiring import _parse_post
        company, role, _, _ = _parse_post("Foo Inc. | ML Engineer | Durham, NC")
        assert company == "Foo Inc." and role == "ML Engineer"


class TestPostedDates:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-08-04T09:42:41-04:00", "2026-08-04"),
        ("Posted Today", datetime.now().strftime("%Y-%m-%d")),
        ("See posting", None),
    ])
    def test_normalisation(self, raw, expected):
        assert norm_posted_date(raw) == expected

    def test_epoch_millis(self):
        assert (norm_posted_date("1784035164618")
                == datetime.fromtimestamp(1784035164.618).strftime("%Y-%m-%d"))

    def test_relative_days(self):
        assert (norm_posted_date("Posted 3 Days Ago")
                == (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"))

    def test_thirty_plus_is_a_floor(self):
        assert (norm_posted_date("Posted 30+ Days Ago")
                == (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))

    def test_first_known_date_wins(self, db, add_job):
        add_job("gh_acme_1", posted_at="2026-08-01")
        add_job("gh_acme_1", posted_at="2026-08-09")     # later fetch
        got = db.execute("SELECT posted_at FROM jobs WHERE job_id='gh_acme_1'"
                         ).fetchone()[0]
        assert got == "2026-08-01"

    def test_status_sync_backfills_posted_at(self, db, company, add_job):
        import core.store as store
        add_job("gh_acme_2")
        store.sync_job_statuses(db, company, [
            {"id": "gh_acme_2", "title": "Data Engineer",
             "url": "https://acme.io/gh_acme_2", "posted_at": "2026-07-15"}],
            track="local-tech")
        got = db.execute("SELECT posted_at FROM jobs WHERE job_id='gh_acme_2'"
                         ).fetchone()[0]
        assert got == "2026-07-15"


class TestAgeTag:
    def test_new_when_first_seen_today(self):
        assert digest_md.age_tag({"first_seen": datetime.now().isoformat()}) == "NEW"

    def test_days_since_posting(self):
        assert digest_md.age_tag({
            "first_seen": "2026-01-01T00:00:00",
            "posted_at": (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d"),
        }) == "6d"

    def test_stale_flag(self):
        assert digest_md.age_tag({
            "first_seen": "2026-01-01T00:00:00",
            "posted_at": (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"),
        }) == "60d!"

    def test_unknown_date(self):
        assert digest_md.age_tag({"first_seen": "2026-01-01T00:00:00"}) == "?"


class TestClosedProbeGuards:
    def test_gated_host_is_indeterminate(self):
        # Bot-gated hosts must never be read as "job closed".
        assert company_fetch.probe_job_open(
            "https://www.linkedin.com/jobs/view/123")[0] is None

    def test_closed_marker_matches(self):
        assert company_fetch._CLOSED_TEXT_RE.search(
            "This position is no longer available")

    def test_closed_loop_jd_does_not_trip_the_marker(self):
        assert not company_fetch._CLOSED_TEXT_RE.search(
            "develop closed-loop neurostimulation")


class TestDiscoveryWiring:
    def test_brainstorm_disabled_touches_no_api(self):
        import discovery.local_sourcing as ls
        assert ls.brainstorm_company_names(n=0) == []

    def test_populate_companies_has_dork_switch(self):
        import discovery.local_sourcing as ls
        assert "dork" in ls.populate_companies.__code__.co_varnames

    def test_dork_queries_built_from_profile(self):
        from discovery.ats_dork import DORK_QUERIES
        assert len(DORK_QUERIES) >= 4
        assert any("greenhouse" in q for q in DORK_QUERIES)

    def test_probe_instances_coexist_unlaunched(self):
        # Lazy launch: K probes can be constructed and torn down without
        # ever starting a browser.
        from contextlib import ExitStack

        from discovery.probes import WorkdayJsProbe
        with ExitStack() as stack:
            probes = [stack.enter_context(WorkdayJsProbe()) for _ in range(3)]
            assert len(probes) == 3
            assert not any(p._launched for p in probes)
