"""The manual capture path for boards nothing can fetch: what a browser save
of a JS-rendered or bot-challenged careers page carries, and filing the
parsed jobs under the roster row that owns the host. Offline: the store is
a throwaway file, the fit scorer a stub, and no fetcher is ever reached
(a capture-only company has no board for the ingest to hydrate from)."""

import pytest

import capture
from conftest import answer, fake_response, fixture
import src.claude.fit as fit
import src.store as store
from src.ops import ingest
import src.ops.maintenance as ops
from src.crawl.page_capture import parse_page


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
        jobs, source = parse_page("", fixture("capture_polymer_board.html"))
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
        jobs, _ = parse_page("", fixture("capture_polymer_job.html"))
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
        jobs, _ = parse_page("", fixture("capture_wp_workday_table.html"))
        got = by_title(jobs)
        assert set(got) == {"Senior Data Engineer", "Bioinformatics Scientist",
                            "Clinical Data Analyst"}
        assert got["Senior Data Engineer"]["url"].endswith("Senior-Data-Engineer_JR-12345")
        assert got["Senior Data Engineer"]["location"] == "Research Triangle Park, NC"
        assert got["Clinical Data Analyst"]["location"].lower().startswith("remote")

    def test_icims_attract_results_list(self):
        jobs, _ = parse_page("", fixture("capture_jibe_results.html"))
        got = by_title(jobs)
        assert set(got) == {"Senior Statistical Programmer", "Clinical Data Manager",
                            "Software Engineer, Clinical Systems"}
        # Relative hrefs resolve against the canonical URL's origin.
        assert got["Clinical Data Manager"]["url"] == \
            "https://careers.acmecro.com/uscareers/jobs/6340/clinical-data-manager/job"
        assert got["Software Engineer, Clinical Systems"]["location"] == "Durham, NC"
        assert got["Clinical Data Manager"]["location"] == "Remote"

    def test_workable_board_with_relative_shortcode_links(self):
        jobs, _ = parse_page("", fixture("capture_workable_board.html"))
        got = by_title(jobs)
        assert set(got) == {"Software Engineer, Integrations", "Implementation Specialist"}
        assert got["Software Engineer, Integrations"]["url"] == \
            "https://apply.workable.com/acmelis/j/8A1B2C3D4E/"
        assert got["Software Engineer, Integrations"]["location"] == "Durham, NC"
        assert got["Implementation Specialist"]["location"] == "Remote"

    def test_results_page_with_id_slug_links(self):
        jobs, _ = parse_page("", fixture("capture_jobs_host_results.html"))
        got = by_title(jobs)
        assert set(got) == {"Data Platform Engineer", "Clinical Informatics Analyst",
                            "Registered Nurse - ICU"}
        assert got["Data Platform Engineer"]["url"] == \
            "https://jobs.acmehealth.org/jobs/15659622-data-platform-engineer"
        assert got["Data Platform Engineer"]["location"] == "Morrisville, NC"
        assert got["Clinical Informatics Analyst"]["location"] == "Chapel Hill, NC"


@pytest.fixture
def roster(wired_db_path, monkeypatch):
    """A throwaway store that BOTH capture.py entry points read -- the
    default connect() and the default track's db_path (conftest's
    `wired_db_path`) -- plus a stubbed fit scorer so the ingest never
    reaches the Claude API. Yields a connection."""
    monkeypatch.setattr(ops, "score_resume_fit",
                        answer(fit.FitResult(score=0.5, reason="stub")))
    conn = store.connect(wired_db_path)
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

    async def test_page_from_a_known_host_lands_under_that_row(self, roster, local_addr):
        store.record_miss(roster, "Acme Health", "no-board-found:site-only-no-careers",
                          careers_url="https://www.acmehealth.org/careers/")
        summary = await capture.ingest_html("", _results_page("jobs.acmehealth.org", local_addr))
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

    async def test_unknown_host_still_records_a_lead(self, roster, local_addr):
        html = """<html><head><link rel="canonical" href="https://jobs.stranger.org/p/1">
        <script type="application/ld+json">{"@type": "JobPosting", "title": "Data Engineer",
        "url": "https://jobs.stranger.org/p/1",
        "hiringOrganization": {"@type": "Organization", "name": "Stranger Labs",
                               "sameAs": "https://www.stranger.org/"},
        "jobLocation": {"address": {"addressLocality": "%s", "addressRegion": "%s"}}}
        </script></head><body></body></html>""" % tuple(
            p.strip() for p in local_addr.split(",", 1))
        summary = await capture.ingest_html("", html)
        assert summary["company"] is None
        assert summary["companies"] == ["Stranger Labs"]
        lead = _row(roster, "Stranger Labs")
        assert lead["ats"] is None and lead["active"] == 0
        assert lead["source"] == "page_capture"

    async def test_a_row_with_a_real_board_keeps_it(self, roster, local_addr, serve):
        cid = store.upsert_company(roster, {
            "name": "Acme Dx", "ats": "greenhouse", "slug": "acmedx",
            "careers_url": "https://www.acmedx.com/careers/"})
        serve(fake_response({"jobs": []}))   # ingest hydrates from the board
        await capture.ingest_html("", _results_page("www.acmedx.com", local_addr))
        row = store.get_company(roster, cid)
        assert row["ats"] == "greenhouse"
        assert roster.execute("SELECT company_id FROM jobs").fetchone()[0] == cid

    async def test_a_row_in_the_review_queue_is_not_activated(self, roster, local_addr):
        store.upsert_company(roster, store.mark_pending({
            "name": "Acme Guess", "careers_url": "https://www.acmeguess.com/"}))
        await capture.ingest_html("", _results_page("jobs.acmeguess.com", local_addr))
        row = _row(roster, "Acme Guess")
        assert row["active"] == 0 and row["ats"] is None
        assert row["review"] == "pending"

    async def test_jsonld_employer_site_attributes_a_hosted_board_page(self, roster):
        # The page host is the board vendor's; the posting's own JSON-LD says
        # whose site the employer is, and THAT matches the roster.
        store.record_miss(roster, "Acme Neuro", "no-board-found",
                          careers_url="https://acmeneuro.com/")
        summary = await capture.ingest_html("", fixture("capture_polymer_job.html"))
        assert summary["company"] == "Acme Neuro"
        assert _row(roster, "Acme Neuro")["ats"] == store.CAPTURE_ATS

    async def test_capture_only_registration_by_hand(self, roster):
        from src.discovery.local_sourcing import add_board
        assert await add_board("Acme Health", "https://jobs.acmehealth.org/",
                               capture=True)
        row = _row(roster, "Acme Health")
        assert row["ats"] == store.CAPTURE_ATS and row["active"] == 1
        assert row["careers_url"] == "https://jobs.acmehealth.org/"
        # The same board under another spelling is the same company.
        assert await add_board("Acme Health System", "https://jobs.acmehealth.org/",
                         capture=True) is None
        assert len(store.get_companies(roster, active_only=False)) == 1
        assert store.crawlable_companies(roster) == []


async def test_ingest_links_jobs_to_their_company_and_hydrates_per_board(
        roster, local_addr, serve):
    """A new job is filed under the roster company its name resolves to, and
    that link picks the bodyless jobs hydrated from a board: one fetch per
    linked company, none for a company not in the roster."""
    cid = store.upsert_company(roster, {"name": "Acme Dx", "ats": "greenhouse",
                                        "slug": "acmedx"})
    requests = serve(fake_response({"jobs": [
        {"id": n, "title": title, "absolute_url": f"https://acmedx.test/j{n}",
         "content": "from the board", "location": {"name": local_addr}}
        for n, title in enumerate(["Data Engineer", "Software Engineer"])]}))
    jobs =[{"title": title, "company": company, "url": "", "location": local_addr, **extra}
            for title, company, extra in [
                ("Data Engineer", "Acme Dx", {}), ("Software Engineer", "Acme Dx", {}),
                ("Analyst", "Acme Dx", {"description": "already here"}),
                ("Data Engineer", "Stranger Labs", {})]]

    assert await ingest.ingest_external_jobs(jobs, source="test", curated=True) == 4

    assert len(requests) == 1 and "/acmedx/" in requests[0]
    assert {(r["company_name"], r["title"]): (r["company_id"], r["description"] or "")
            for r in roster.execute("SELECT * FROM jobs")} == {
        ("Acme Dx", "Data Engineer"): (cid, "from the board"),
        ("Acme Dx", "Software Engineer"): (cid, "from the board"),
        ("Acme Dx", "Analyst"): (cid, "already here"),
        ("Stranger Labs", "Data Engineer"): (None, "")}
