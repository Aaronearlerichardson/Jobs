"""The manual capture path for boards nothing can fetch: what a browser save
of a JS-rendered or bot-challenged careers page carries, and filing the
parsed jobs under the roster row that owns the host. Offline: the store is
a throwaway file, the fit scorer a stub, and no fetcher is ever reached
(a capture-only company has no board for the ingest to hydrate from)."""

from pathlib import Path

import pytest

import capture
import config
import core.fit as fit
import core.store as store
import core.tags as tags
import scrapers.ops as ops
from scrapers.page_capture import parse_page

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def by_title(jobs):
    return {j["title"]: j for j in jobs}


class TestGenericBoards:
    """Each fixture is the DOM a browser save carries for one kind of board
    the crawler cannot fetch. The generic layers (JSON-LD, path-shaped job
    links, id-keyed job cards) have to read all of them without a
    site-specific parser."""

    def test_hosted_board_keyed_on_a_bare_id(self):
        # jobs.<vendor>/<tenant>/<id>: nothing job-shaped in the path, so the
        # card sweep (board host + id tail + own heading) is what finds it.
        jobs, source = parse_page("", load("capture_polymer_board.html"))
        assert source == "page"
        got = by_title(jobs)
        assert set(got) == {"Senior Manufacturing Quality Engineer",
                            "Microfabrication Cleanroom Manager",
                            "Signal Processing Engineer"}
        assert got["Senior Manufacturing Quality Engineer"]["url"] == \
            "https://jobs.polymer.co/acmeneuro/40297"
        assert got["Senior Manufacturing Quality Engineer"]["location"] == "Cambridge, MA"
        assert got["Signal Processing Engineer"]["location"] == "Remote (US)"
        assert all(not j["company"] for j in jobs)   # attribution's job, not the parser's

    def test_posting_page_jsonld_names_the_employer_site(self):
        jobs, _ = parse_page("", load("capture_polymer_job.html"))
        assert len(jobs) == 1
        j = jobs[0]
        assert j["title"] == "Microfabrication Cleanroom Manager"
        assert j["company"] == "Acme Neuro"
        assert j["company_url"] == "https://acmeneuro.com"
        assert j["location"] == "Cambridge, MA"
        assert "<" not in j["description"] and "cleanroom" in j["description"]

    def test_workday_fed_table_on_a_company_site(self):
        # Three cells per row link the same posting: one job per row, title
        # from the title cell, location from the location cell.
        jobs, _ = parse_page("", load("capture_wp_workday_table.html"))
        got = by_title(jobs)
        assert set(got) == {"Senior Data Engineer", "Bioinformatics Scientist",
                            "Clinical Data Analyst"}
        assert got["Senior Data Engineer"]["url"].endswith("Senior-Data-Engineer_JR-12345")
        assert got["Senior Data Engineer"]["location"] == "Research Triangle Park, NC"
        assert got["Clinical Data Analyst"]["location"].lower().startswith("remote")

    def test_icims_attract_results_list(self):
        jobs, _ = parse_page("", load("capture_jibe_results.html"))
        got = by_title(jobs)
        assert set(got) == {"Senior Statistical Programmer", "Clinical Data Manager",
                            "Software Engineer, Clinical Systems"}
        # Relative hrefs resolve against the canonical URL's origin.
        assert got["Clinical Data Manager"]["url"] == \
            "https://careers.acmecro.com/uscareers/jobs/6340/clinical-data-manager/job"
        assert got["Software Engineer, Clinical Systems"]["location"] == "Durham, NC"
        assert got["Clinical Data Manager"]["location"] == "Remote"

    def test_workable_board_with_relative_shortcode_links(self):
        jobs, _ = parse_page("", load("capture_workable_board.html"))
        got = by_title(jobs)
        assert set(got) == {"Software Engineer, Integrations", "Implementation Specialist"}
        assert got["Software Engineer, Integrations"]["url"] == \
            "https://apply.workable.com/acmelis/j/8A1B2C3D4E/"
        assert got["Software Engineer, Integrations"]["location"] == "Durham, NC"
        assert got["Implementation Specialist"]["location"] == "Remote"

    def test_results_page_with_id_slug_links(self):
        jobs, _ = parse_page("", load("capture_jobs_host_results.html"))
        got = by_title(jobs)
        assert set(got) == {"Data Platform Engineer", "Clinical Informatics Analyst",
                            "Registered Nurse - ICU"}
        assert got["Data Platform Engineer"]["url"] == \
            "https://jobs.acmehealth.org/jobs/15659622-data-platform-engineer"
        assert got["Data Platform Engineer"]["location"] == "Morrisville, NC"
        assert got["Clinical Informatics Analyst"]["location"] == "Chapel Hill, NC"

    def test_id_shaped_links_off_a_board_host_are_not_jobs(self):
        # A news card is an anchor with a heading and a numeric tail too; only
        # a job-board host (or a /j/ path) makes that a posting.
        html = """<html><head><link rel="canonical" href="https://www.acme.com/news"></head>
        <body><a href="/news/2024"><h3>Series B announced</h3></a>
        <a href="https://www.acme.com/press/10001"><h2>New office</h2></a></body></html>"""
        jobs, _ = parse_page("", html)
        assert jobs == []

    def test_a_card_without_a_place_borrows_none_from_its_neighbours(self):
        html = """<html><head><link rel="canonical" href="https://jobs.acme.org/search"></head>
        <body><ul>
        <li><a href="/jobs/1001-analyst">Analyst</a> <span>Durham, NC</span></li>
        <li><a href="/jobs/1002-engineer">Engineer</a></li>
        </ul></body></html>"""
        got = by_title(parse_page("", html)[0])
        assert got["Analyst"]["location"] == "Durham, NC"
        assert got["Engineer"]["location"] == ""


@pytest.fixture
def roster(tmp_path, monkeypatch):
    """A throwaway store that BOTH capture.py entry points read -- the
    default connect() and the default track's db_path -- plus a stubbed fit
    scorer so the ingest never reaches the Claude API. Yields a connection."""
    db_path = tmp_path / "capture.db"
    monkeypatch.setattr(config, "STORE_DB_PATH", db_path)
    monkeypatch.setitem(config.UI_TRACKS[config.DEFAULT_TRACK], "db_path", db_path)
    monkeypatch.setattr(ops, "score_resume_fit",
                        lambda *a, **k: fit.FitResult(score=0.5, reason="stub"))
    conn = store.connect(db_path)
    yield conn
    conn.close()


def _results_page(host, location):
    return f"""<html><head><link rel="canonical" href="https://{host}/search/jobs"></head>
    <body><div class="job-card"><h2><a href="/jobs/15659622-data-engineer">Data Engineer</a></h2>
    <span>{location}</span></div></body></html>"""


def _row(conn, name):
    return next(c for c in store.get_companies(conn, active_only=False)
                if c["name"] == name)


class TestAttribution:
    """A page saved from a roster company's own careers host lands under that
    company's existing row, and the row becomes capture-only rather than a
    board the crawl keeps failing to fetch."""

    def test_page_from_a_known_host_lands_under_that_row(self, roster, local_addr):
        store.record_miss(roster, "Acme Health", "no-board-found:site-only-no-careers",
                          careers_url="https://www.acmehealth.org/careers/")
        summary = capture.ingest_html("", _results_page("jobs.acmehealth.org", local_addr))
        assert summary["company"] == "Acme Health"
        assert summary["ingested"] == 1 and summary["companies"] == []
        row = _row(roster, "Acme Health")
        assert row["ats"] == store.CAPTURE_ATS and row["active"] == 1
        assert row["miss_reason"] is None
        job = roster.execute("SELECT company_id, company_name FROM jobs").fetchone()
        assert (job["company_id"], job["company_name"]) == (row["id"], "Acme Health")
        # One roster row, still: the page text minted no second company.
        assert len(store.get_companies(roster, active_only=False)) == 1
        # And the crawl loop never picks it up.
        assert store.crawlable_companies(roster) == []

    def test_unknown_host_still_records_a_lead(self, roster, local_addr):
        html = """<html><head><link rel="canonical" href="https://jobs.stranger.org/p/1">
        <script type="application/ld+json">{"@type": "JobPosting", "title": "Data Engineer",
        "url": "https://jobs.stranger.org/p/1",
        "hiringOrganization": {"@type": "Organization", "name": "Stranger Labs",
                               "sameAs": "https://www.stranger.org/"},
        "jobLocation": {"address": {"addressLocality": "%s", "addressRegion": "%s"}}}
        </script></head><body></body></html>""" % tuple(
            p.strip() for p in local_addr.split(",", 1))
        summary = capture.ingest_html("", html)
        assert summary["company"] is None
        assert summary["companies"] == ["Stranger Labs"]
        lead = _row(roster, "Stranger Labs")
        assert lead["ats"] is None and lead["active"] == 0
        assert lead["source"] == "page_capture"

    def test_a_row_with_a_real_board_keeps_it(self, roster, local_addr):
        cid = store.upsert_company(roster, {
            "name": "Acme Dx", "ats": "greenhouse", "slug": "acmedx",
            "careers_url": "https://www.acmedx.com/careers/"})
        capture.ingest_html("", _results_page("www.acmedx.com", local_addr))
        row = store.get_company(roster, cid)
        assert row["ats"] == "greenhouse"
        assert roster.execute("SELECT company_id FROM jobs").fetchone()[0] == cid

    def test_a_row_in_the_review_queue_is_not_activated(self, roster, local_addr):
        store.upsert_company(roster, store.mark_pending({
            "name": "Acme Guess", "careers_url": "https://www.acmeguess.com/"}))
        capture.ingest_html("", _results_page("jobs.acmeguess.com", local_addr))
        row = _row(roster, "Acme Guess")
        assert row["active"] == 0 and row["ats"] is None
        assert tags.has(row["tags"], tags.PENDING)

    def test_jsonld_employer_site_attributes_a_hosted_board_page(self, roster):
        # The page host is the board vendor's; the posting's own JSON-LD says
        # whose site the employer is, and THAT matches the roster.
        store.record_miss(roster, "Acme Neuro", "no-board-found",
                          careers_url="https://acmeneuro.com/")
        summary = capture.ingest_html("", load("capture_polymer_job.html"))
        assert summary["company"] == "Acme Neuro"
        assert _row(roster, "Acme Neuro")["ats"] == store.CAPTURE_ATS

    def test_capture_only_registration_by_hand(self, roster):
        from discovery.local_sourcing import add_board
        assert add_board("Acme Health", "https://jobs.acmehealth.org/", capture=True)
        row = _row(roster, "Acme Health")
        assert row["ats"] == store.CAPTURE_ATS and row["active"] == 1
        assert row["careers_url"] == "https://jobs.acmehealth.org/"
        # The same board under another spelling is the same company.
        assert add_board("Acme Health System", "https://jobs.acmehealth.org/",
                         capture=True) is None
        assert len(store.get_companies(roster, active_only=False)) == 1
        assert store.crawlable_companies(roster) == []
