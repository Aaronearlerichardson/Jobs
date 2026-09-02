"""Offline parsing/detection: board sniffing, custom-board link detection,
the HN thread parser, posting-date normalisation, age tags, and the
closed-posting probe guards. No network."""

from datetime import datetime, timedelta

import pytest
from bs4 import BeautifulSoup

import core.digest_md as digest_md
import discovery.ats_dork as dork
import discovery.local_sourcing as local_sourcing
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


class _FakeResp:
    def __init__(self, text, url):
        self.text = text
        self.url = url


def _stub_fetch_all(monkeypatch, mapping):
    """Replace sniffer._fetch_all with a lookup into `mapping` (url -> html),
    so no network call happens; any URL not in `mapping` fetches as None."""
    def _fake(urls):
        return {u: (_FakeResp(mapping[u], u) if u in mapping else None)
                for u in urls}
    monkeypatch.setattr(sniffer, "_fetch_all", _fake)


class TestDeadHostCache:
    """_fetch_page skips a host that refused a connection earlier in the
    run: the candidate list tries several paths per name-guessed host and
    the miss path rebuilds it a few more times, so one dead host cost seven
    identical GETs per name in the 2026-09-01 add-names run."""

    def _session(self, monkeypatch, exc):
        calls = []

        class _S:
            def get(self, url, **kw):
                calls.append(url)
                raise exc

        monkeypatch.setattr(sniffer, "SESSION", _S())
        monkeypatch.setattr(sniffer, "_DEAD_HOSTS", {})
        monkeypatch.setattr(sniffer, "_PAGE_MEMO", {})
        return calls

    def test_refused_host_is_not_retried_on_other_paths(self, monkeypatch):
        import requests
        calls = self._session(monkeypatch, requests.exceptions.ConnectionError("dns"))
        assert sniffer._fetch_page("https://www.dead.example/") is None
        assert sniffer._fetch_page("https://www.dead.example/careers") is None
        assert sniffer._fetch_page("https://www.other.example/careers") is None
        assert calls == ["https://www.dead.example/",
                         "https://www.other.example/careers"]

    def test_read_timeout_is_not_a_dead_host(self, monkeypatch):
        # A slow host may still answer another path; only refused
        # connections (DNS, TLS, connect timeout) are remembered.
        import requests
        calls = self._session(monkeypatch, requests.exceptions.ReadTimeout("slow"))
        sniffer._fetch_page("https://www.slow.example/")
        sniffer._fetch_page("https://www.slow.example/careers")
        assert len(calls) == 2

    def test_entry_expires(self, monkeypatch):
        import time
        import requests
        calls = self._session(monkeypatch, requests.exceptions.ConnectionError("dns"))
        sniffer._DEAD_HOSTS["www.dead.example"] = time.time() - sniffer._DEAD_HOST_TTL - 1
        sniffer._fetch_page("https://www.dead.example/careers")
        assert calls == ["https://www.dead.example/careers"]


class TestPageMemo:
    """_fetch_page answers a repeated URL from its per-run memo: each stage
    of a name's resolution rebuilds the candidate list, so live hosts were
    fetched up to seven times per name (sgs.com, intertek.com, a 403ing
    infosys.com in the 2026-09-01 add-names runs)."""

    def _session(self, monkeypatch, status=200, body="x" * 400):
        calls = []

        class _R:
            status_code = status
            text = body
            content = body.encode()

        class _S:
            def get(self, url, **kw):
                calls.append(url)
                return _R()

        monkeypatch.setattr(sniffer, "SESSION", _S())
        monkeypatch.setattr(sniffer, "_DEAD_HOSTS", {})
        monkeypatch.setattr(sniffer, "_PAGE_MEMO", {})
        return calls

    def test_live_page_is_fetched_once_per_run(self, monkeypatch):
        calls = self._session(monkeypatch)
        a = sniffer._fetch_page("https://www.sgs.com/")
        b = sniffer._fetch_page("https://www.sgs.com/")
        assert a is b and a is not None
        assert calls == ["https://www.sgs.com/"]

    def test_misses_are_memoized_too(self, monkeypatch):
        calls = self._session(monkeypatch, status=403)
        assert sniffer._fetch_page("https://www.infosys.com/") is None
        assert sniffer._fetch_page("https://www.infosys.com/") is None
        assert len(calls) == 1

    def test_distinct_urls_still_fetch(self, monkeypatch):
        calls = self._session(monkeypatch)
        sniffer._fetch_page("https://www.sgs.com/")
        sniffer._fetch_page("https://www.sgs.com/careers")
        assert len(calls) == 2

    def test_oversized_bodies_are_not_hoarded(self, monkeypatch):
        calls = self._session(monkeypatch, body="x" * (sniffer._PAGE_MEMO_MAX_BYTES + 1))
        sniffer._fetch_page("https://big.example/")
        sniffer._fetch_page("https://big.example/")
        assert len(calls) == 2 and sniffer._PAGE_MEMO == {}

    def test_cap_evicts_the_oldest_entry(self, monkeypatch):
        calls = self._session(monkeypatch)
        monkeypatch.setattr(sniffer, "_PAGE_MEMO_CAP", 2)
        for u in ("https://a.example/", "https://b.example/", "https://c.example/"):
            sniffer._fetch_page(u)
        assert "https://a.example/" not in sniffer._PAGE_MEMO
        assert len(sniffer._PAGE_MEMO) == 2


class TestJobPageMeta:
    """URL-only manual adds read their title (and JD) off the posting page;
    nine empty-title rows sat in the 2026-09-01 store because nothing did."""

    def _serve(self, monkeypatch, html):
        class _R:
            status_code = 200
            text = html

        class _S:
            def get(self, *a, **k):
                return _R()

        monkeypatch.setattr(company_fetch, "SESSION", _S())

    def test_jsonld_title_and_description(self, monkeypatch):
        body = " ".join(["word"] * 60)
        self._serve(monkeypatch,
                    '<script type="application/ld+json">{"@type": "JobPosting", '
                    '"title": "Sr Vision Software Engineer", '
                    '"description": "' + body + '"}</script>')
        title, desc = company_fetch.job_page_meta("https://x.example/jobs/1")
        assert title == "Sr Vision Software Engineer"
        assert desc.startswith("word word")

    def test_title_falls_back_to_og_title_then_html_title(self, monkeypatch):
        self._serve(monkeypatch,
                    '<html><head><meta property="og:title" content="Data Engineer II">'
                    '<title>Data Engineer II | Acme Careers</title></head></html>')
        assert company_fetch.job_page_meta("https://x.example/j")[0] == "Data Engineer II"
        self._serve(monkeypatch,
                    '<html><head><title> Data  Engineer II | Acme Careers</title>'
                    '</head></html>')
        assert company_fetch.job_page_meta("https://x.example/j")[0] == "Data Engineer II"

    def test_fetch_failure_is_a_double_miss(self, monkeypatch):
        class _S:
            def get(self, *a, **k):
                raise RuntimeError("down")

        monkeypatch.setattr(company_fetch, "SESSION", _S())
        assert company_fetch.job_page_meta("https://x.example/j") == ("", "")


class TestRootScan:
    """sniff_ats/sniff_careers_ats fall back to the bare homepage when every
    careers-path candidate misses (Task 2: NALA Membranes / Merakris
    Therapeutics reference their ATS on the root page, not a /careers path)."""

    def test_sniff_ats_recovers_ats_badge_on_homepage(self, monkeypatch):
        root = "https://www.acmegenomics.com/"
        _stub_fetch_all(monkeypatch, {
            root: '<a href="https://jobs.lever.co/acmegenomics">Jobs</a>'})
        hit = sniffer.sniff_ats("Acme Genomics")
        assert hit == {"ats": "lever", "slug": "acmegenomics",
                       "careers_url": root}

    def test_sniff_careers_ats_falls_back_to_root_as_a_lead(self, monkeypatch):
        # Ashby has no probe-confirmable count path for a bare slug fixture
        # here, so this exercises the unconfirmed-lead branch of the
        # root-scan fallback.
        root = "https://www.acmegenomics.com/"
        _stub_fetch_all(monkeypatch, {
            root: '<a href="acme-genomics.dayforcehcm.com/careers/openings">Jobs</a>'})
        lead = sniffer.sniff_careers_ats("Acme Genomics")
        assert lead["confirmed"] is False
        assert lead["ats"] == "dayforce"

    def test_no_root_fallback_when_a_candidate_already_hit(self, monkeypatch):
        # A hit on a real careers-path candidate must win outright -- the
        # root scan only runs when nothing else did.
        candidate = "https://www.acmegenomics.com/careers"
        root = "https://www.acmegenomics.com/"
        _stub_fetch_all(monkeypatch, {
            candidate: '<a href="https://jobs.lever.co/acmegenomics">Jobs</a>',
            root: '<a href="https://boards.greenhouse.io/wrongone">Jobs</a>'})
        hit = sniffer.sniff_ats("Acme Genomics")
        assert hit["ats"] == "lever"

    def test_risky_root_token_still_needs_corroboration(self, monkeypatch):
        # "Galaxy Diagnostics" -> root guess "galaxy.com" is a truncated
        # (risky) token; a page that doesn't mention "diagnostics" must not
        # be trusted even when it's the only thing that answered.
        root = "https://www.galaxy.com/"
        _stub_fetch_all(monkeypatch, {
            root: '<a href="https://jobs.lever.co/galaxyfintech">Jobs</a>'})
        assert sniffer.sniff_ats("Galaxy Diagnostics") is None

    def test_risky_root_token_recovered_when_it_corroborates(self, monkeypatch):
        root = "https://www.galaxy.com/"
        _stub_fetch_all(monkeypatch, {
            root: ('Careers at Galaxy Diagnostics<br>'
                   '<a href="https://jobs.lever.co/galaxydx">Jobs</a>')})
        hit = sniffer.sniff_ats("Galaxy Diagnostics")
        assert hit and hit["ats"] == "lever"


class TestDiagnoseNoBoard:
    """The four "no-board-found" qualifiers classify_miss appends (Task 1)."""

    def test_domain_unreachable_when_nothing_answers(self, monkeypatch):
        _stub_fetch_all(monkeypatch, {})
        assert sniffer.diagnose_no_board("Acme Genomics") == "domain-unreachable"

    def test_site_only_no_careers_when_a_page_answers_with_nothing_on_it(
            self, monkeypatch):
        root = "https://www.acmegenomics.com/"
        _stub_fetch_all(monkeypatch, {root: "<html><body>Welcome</body></html>"})
        assert (sniffer.diagnose_no_board("Acme Genomics")
                == "site-only-no-careers")

    def test_careers_page_no_ats_when_real_job_links_but_no_ats(
            self, monkeypatch):
        careers = "https://www.acmegenomics.com/careers"
        html = ('<a href="/careers/facilities-engineer-88">Facilities Engineer</a>'
                '<a href="/careers/quality-engineer-19">Quality Engineer</a>'
                '<a href="/careers/data-scientist-3">Data Scientist</a>')
        _stub_fetch_all(monkeypatch, {careers: html})
        assert (sniffer.diagnose_no_board("Acme Genomics")
                == "careers-page-no-ats")

    def test_wrong_domain_when_only_a_risky_token_answers_uncorroborated(
            self, monkeypatch):
        root = "https://www.galaxy.com/careers"
        _stub_fetch_all(monkeypatch, {
            root: "<html><body>Galaxy Digital hires blockchain engineers</body></html>"})
        assert (sniffer.diagnose_no_board("Galaxy Diagnostics")
                == "wrong-domain")

    def test_risky_uncorroborated_noise_ignored_when_a_safe_page_answers(
            self, monkeypatch):
        # The precise domain DOES answer (just with nothing careers-shaped
        # on it); an unrelated site coincidentally living at the truncated
        # "galaxy.com" guess must not override that with "wrong-domain" --
        # that reason is reserved for when the precise domain is the one
        # that's dead (see the sibling test above).
        safe = "https://www.galaxydiagnostics.com/"
        risky = "https://www.galaxy.com/careers"
        _stub_fetch_all(monkeypatch, {
            safe: "<html><body>Welcome to Galaxy Diagnostics</body></html>",
            risky: "<html><body>Galaxy Digital hires blockchain engineers</body></html>"})
        assert (sniffer.diagnose_no_board("Galaxy Diagnostics")
                == "site-only-no-careers")


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


class TestPastedPageNames:
    """Company names out of text copied off a results page.

    The parser is deliberately permissive: a junk name costs one failed
    resolve and is dropped by the same probe -> validate chain that guards
    the LLM brainstorm, while a dropped real name is invisible. So these
    pin the two things that actually matter — real employers survive, and
    the specific noise a results page emits does not.
    """

    # Shaped like a LinkedIn job search: title / company / location / chrome.
    RESULTS = """Senior Data Engineer
Fennec Pharmaceuticals
Durham, NC (Hybrid)
Promoted
2 days ago
Clinical Research Scientist
Locus Biosciences
Research Triangle Park, NC
Easy Apply
Over 100 applicants
Precision BioSciences · Durham, NC
1K followers
Principal Statistician
Chimerix
Raleigh-Durham-Chapel Hill Area
Reposted 3 days ago
G1 Therapeutics
See all jobs
Page 1 of 4"""

    def test_finds_every_employer(self):
        names = local_sourcing.parse_company_names(self.RESULTS)
        assert set(names) == {
            "Fennec Pharmaceuticals", "Locus Biosciences", "Precision BioSciences",
            "Chimerix", "G1 Therapeutics",
        }

    def test_keeps_the_order_they_appeared_in(self):
        names = local_sourcing.parse_company_names(self.RESULTS)
        assert names[0] == "Fennec Pharmaceuticals"
        assert names[-1] == "G1 Therapeutics"

    @pytest.mark.parametrize("line", [
        "2 days ago", "3 weeks ago", "Reposted 3 days ago", "Promoted",
        "Easy Apply", "Actively recruiting", "1K followers", "10,001 employees",
        "Over 100 applicants", "See all jobs", "Page 1 of 4", "Remote",
        "Durham, NC", "Durham, NC (Hybrid)", "Raleigh-Durham-Chapel Hill Area",
    ])
    def test_page_furniture_is_dropped(self, line):
        assert local_sourcing.parse_company_names(line) == []

    @pytest.mark.parametrize("line", [
        "Senior Data Engineer", "Clinical Research Scientist",
        "Principal Statistician", "Director of Operations",
        "Registered Nurse", "Software Developer II",
    ])
    def test_job_titles_are_dropped(self, line):
        """Results pages interleave titles with employers; a title resolves to
        nothing, so dropping it saves a pointless probe."""
        assert local_sourcing.parse_company_names(line) == []

    def test_a_digit_prefixed_stat_is_not_mistaken_for_a_list_item(self):
        """Stripping list markers before the noise check turned '2 days ago'
        into 'days ago' and '1K followers' into 'K followers', both of which
        then looked like company names."""
        assert local_sourcing.parse_company_names("2 days ago\n1K followers") == []

    def test_numbered_lists_still_have_their_markers_stripped(self):
        assert local_sourcing.parse_company_names(
            "1. Fennec Pharmaceuticals\n2) Chimerix\n- G1 Therapeutics") == [
            "Fennec Pharmaceuticals", "Chimerix", "G1 Therapeutics"]

    def test_separator_suffixes_are_trimmed(self):
        assert local_sourcing.parse_company_names(
            "Precision BioSciences · Durham, NC\nChimerix • 1K followers") == [
            "Precision BioSciences", "Chimerix"]

    def test_duplicates_collapse_case_insensitively(self):
        assert local_sourcing.parse_company_names(
            "Chimerix\nCHIMERIX\nchimerix") == ["Chimerix"]

    def test_accepts_a_list_as_well_as_a_blob(self):
        assert local_sourcing.parse_company_names(
            ["Chimerix", "2 days ago"]) == ["Chimerix"]

    def test_empty_input_is_not_an_error(self):
        for empty in ("", None, [], "   \n\n  "):
            assert local_sourcing.parse_company_names(empty) == []

    def test_urls_are_not_company_names(self):
        assert local_sourcing.parse_company_names(
            "https://linkedin.com/jobs/view/123\nwww.example.com\nChimerix") == ["Chimerix"]

    def test_limit_is_honoured(self):
        blob = "\n".join(f"Company {i} Bio" for i in range(50))
        assert len(local_sourcing.parse_company_names(blob, limit=10)) == 10


class TestPastedNameExtractionFallback:
    def test_llm_extraction_falls_back_to_the_parser(self, monkeypatch):
        """No API key (or a failed call) must not lose the paste — the regex
        parser still runs, so the card works for free."""
        monkeypatch.setattr(local_sourcing, "extract_names_llm",
                            lambda *a, **k: [])
        captured = {}

        def _resolve(n, *a, **k):
            # resolve_or_miss, not resolve_board_sniff_first: add_names now
            # goes through the wrapper that also yields a miss reason, and
            # the real one would hit the network on a failed resolve.
            captured[n] = None
            return None, "no-board-found"

        monkeypatch.setattr(local_sourcing, "resolve_or_miss", _resolve)

        class _Conn:
            def execute(self, *a):
                return self

            def fetchall(self):
                return []

            def fetchone(self):
                return None

            def commit(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr("core.store.connect", lambda *a, **k: _Conn())
        local_sourcing.add_names("Chimerix\n2 days ago", use_llm=True)
        assert "Chimerix" in captured, "the paste was lost when the LLM returned nothing"


class TestPastedNameBoardGuard:
    """A pasted name the roster spells differently passes the name check,
    resolves to a board already on file, and used to land as a second row
    ("SAS" beside "SAS Institute", "Veeva Systems" beside "Veeva", "NVIDIA
    AI" beside "NVIDIA" on 2026-09-01). The board is checked before the
    insert now."""

    def _wire(self, monkeypatch, db, hit):
        import core.store as store
        import core.claude as claude

        class _NoClose:
            # add_names closes the connection it opens; the test still
            # needs to read the fixture DB afterwards.
            def __getattr__(self, k):
                return getattr(db, k)

            def close(self):
                pass

        monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose())
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda *a, **k: (hit, None))
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        monkeypatch.setattr(claude, "score_company_mission",
                            lambda *a, **k: ("adjacent", 0.5, "stub"))
        monkeypatch.setattr(local_sourcing, "nc_hq_signal", lambda *a, **k: True)

    def test_same_board_under_another_name_is_not_added(self, monkeypatch, db, capsys):
        import core.store as store
        store.upsert_company(db, {"name": "SAS Institute", "ats": "icims",
                                  "slug": "globalcareers-sas"})
        self._wire(monkeypatch, db, {"name": "SAS", "ats": "icims",
                                     "slug": "globalcareers-sas",
                                     "careers_url": "https://www.sas.com/careers",
                                     "count": 150, "nc": 30, "via": "sniff"})
        local_sourcing.add_names("SAS", max_workers=1)
        names = [r[0] for r in db.execute("SELECT name FROM companies")]
        assert names == ["SAS Institute"]
        assert "[dup]" in capsys.readouterr().out

    def test_a_new_board_is_still_written(self, monkeypatch, db):
        import core.store as store
        store.upsert_company(db, {"name": "SAS Institute", "ats": "icims",
                                  "slug": "globalcareers-sas"})
        self._wire(monkeypatch, db, {"name": "Veeva", "ats": "lever",
                                     "slug": "veeva",
                                     "careers_url": "https://www.veeva.com/careers",
                                     "count": 40, "nc": 12, "via": "sniff"})
        local_sourcing.add_names("Veeva", max_workers=1)
        names = sorted(r[0] for r in db.execute("SELECT name FROM companies"))
        assert names == ["SAS Institute", "Veeva"]


class TestResolutionStallWatchdog:
    """One wedged resolution must not hold a resolution pass — and the web
    UI's one-op-at-a-time slot — forever. 2026-08-28: 59 of 60 pasted names
    finished in 8 minutes; the 60th hung for over an hour and wedged the op
    slot until the app was restarted. The shared helper behind add_names and
    discover_local's sniff/websearch passes is _drain_or_abandon."""

    def test_drain_or_abandon_abandons_only_the_stuck_future(self, monkeypatch):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        monkeypatch.setattr(local_sourcing, "RESOLVE_STALL_S", 0.3)
        release = threading.Event()
        consumed, stalled = [], []
        ex = ThreadPoolExecutor(max_workers=2)
        futs = {ex.submit(lambda: "ok"): "fast",
                ex.submit(release.wait, 10): "slow"}
        try:
            local_sourcing._drain_or_abandon(
                ex, futs,
                lambda fut, n: consumed.append((n, fut.result())),
                stalled.append)
        finally:
            release.set()
        assert consumed == [("fast", "ok")]
        assert stalled == ["slow"]

    class _Conn:
        def execute(self, *a):
            return self

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def commit(self):
            pass

        def close(self):
            pass

    def test_a_hung_resolve_is_abandoned_and_recorded(self, monkeypatch):
        import threading
        release = threading.Event()

        def _resolve(n, *a, **k):
            if n.startswith("Hangs"):
                release.wait(10)      # far past the patched stall window
            return None, "no-board-found"

        monkeypatch.setattr(local_sourcing, "resolve_or_miss", _resolve)
        monkeypatch.setattr(local_sourcing, "RESOLVE_STALL_S", 0.3)
        misses = []
        monkeypatch.setattr(
            "core.store.record_miss",
            lambda conn, n, r, **kw: misses.append((n, r)))
        monkeypatch.setattr("core.store.connect",
                            lambda *a, **k: self._Conn())
        try:
            out = local_sourcing.add_names("Chimerix\nHangs Forever Inc")
        finally:
            release.set()             # unblock the abandoned worker thread
        assert out == []
        assert ("Hangs Forever Inc", "fetch-error:stalled") in misses, \
            "the wedged name was not abandoned as a recorded miss"
        assert any(n == "Chimerix" for n, _ in misses), \
            "names that resolved before the stall must still be processed"


class TestDoubledTitleExtraction:
    """A results page that repeats each job title pins the employer by position.

    LinkedIn renders every hit as "<title>(Verified job)<title>" with the
    employer on the next line. Reading that marker beats filtering the page:
    filtering a real 329-line search page yielded 15 employers plus 102 pieces
    of chrome ("Past month", "Posted 3 days ago3 days ago", markdown nav
    links), each of which would have cost a network resolve. The marker yields
    the same 15 and never looks at the rest.
    """

    PAGE = """73 results
Durham, NC (50 mi)
How promoted jobs are ranked
Signal Processing Engineer (Verified job)Signal Processing Engineer
CoVar
Durham, NC (On-site)
You'd be a top applicant
Viewed
 ·
Posted 2 weeks ago2 weeks ago
 ·
Easy Apply
Senior AI/ML EngineerSenior AI/ML Engineer
Pedestal Health
Raleigh, NC (Remote)
Posted 1 week ago1 week ago
Postdoctoral AssociatePostdoctoral Associate
Duke University
Durham, NC
4 connections work here
Statistical Software DeveloperStatistical Software Developer
Headwater Science
Durham, NC (Hybrid)
[About](https://about.linkedin.com/)
[Help Center](https://www.linkedin.com/help/linkedin/)"""

    def test_pulls_exactly_the_employers(self):
        assert local_sourcing.parse_company_names(self.PAGE) == [
            "CoVar", "Pedestal Health", "Duke University", "Headwater Science"]

    def test_page_chrome_is_never_considered(self):
        got = local_sourcing.parse_company_names(self.PAGE)
        for chrome in ("73 results", "How promoted jobs are ranked",
                       "Durham, NC (50 mi)", "Viewed", "Easy Apply"):
            assert chrome not in got

    def test_markdown_nav_links_are_dropped(self):
        assert local_sourcing.parse_company_names(
            "[Help Center](https://www.linkedin.com/help/linkedin/)") == []

    def test_employer_names_containing_title_words_survive(self):
        """The keyword screen that rejects job titles must not run on a name
        the marker already proved is an employer — 'Headwater Science' and
        'Vadum Inc.' would otherwise be collateral."""
        page = ("Data EngineerData Engineer\nHeadwater Science\n"
                "Machine Learning EngineerMachine Learning Engineer\nVadum Inc.")
        assert local_sourcing.parse_company_names(page) == [
            "Headwater Science", "Vadum Inc."]

    def test_a_doubled_date_is_not_a_doubled_title(self):
        """'Posted 2 weeks ago2 weeks ago' repeats a SUFFIX, not the whole
        line, so it must not mark the next line as a company."""
        assert local_sourcing._DOUBLED_TITLE_RE.match(
            "Posted 2 weeks ago2 weeks ago") is None

    @pytest.mark.parametrize("line", [
        "Signal Processing Engineer (Verified job)Signal Processing Engineer",
        "Senior AI/ML EngineerSenior AI/ML Engineer",
        "Postdoctoral AssociatePostdoctoral Associate",
    ])
    def test_marker_matches_both_badged_and_bare_doubles(self, line):
        assert local_sourcing._DOUBLED_TITLE_RE.match(line)

    def test_falls_back_when_the_page_has_no_marker(self):
        """A source that doesn't repeat titles (a directory, an article) still
        goes through the permissive line filter."""
        assert local_sourcing.parse_company_names(
            "Chimerix\n2 days ago\nG1 Therapeutics") == ["Chimerix", "G1 Therapeutics"]

    def test_one_stray_double_does_not_hijack_a_plain_list(self):
        """Two markers are required, so a coincidental repeat in prose cannot
        switch a plain list over to positional reading. (Positional reading
        would return nothing but the line after the stray double; every real
        name surviving proves line mode ran. "bla bla" itself is multi-word
        lowercase prose, which the line filter now drops on sight.)"""
        assert local_sourcing.parse_company_names(
            "Chimerix\nbla bla\nG1 Therapeutics\nBiogen") == [
            "Chimerix", "G1 Therapeutics", "Biogen"]


class TestResolveBoardSniffFirstCustomShortCircuit:
    """Offline coverage for the Task 1 fix: a `custom` sniff hit only wins
    immediately when it already carries local jobs. No network — sniff_ats,
    probe_company, _websearch_board and _validate_board are all faked."""

    @staticmethod
    def _sniff_custom(careers_url="https://x.example/careers"):
        return lambda name, careers_url=careers_url: {
            "ats": "custom", "careers_url": careers_url}

    def test_weak_custom_hit_falls_through_to_probe_and_websearch(self, monkeypatch):
        """nc == 0 on the custom hit must not short-circuit: both probe and
        websearch get a chance before anything is returned."""
        monkeypatch.setattr(sniffer, "sniff_ats", self._sniff_custom())
        calls = {"probe": 0, "websearch": 0}

        def _probe(name, try_workday=True):
            calls["probe"] += 1
            return None

        def _websearch(name, max_results=8):
            calls["websearch"] += 1
            return None

        monkeypatch.setattr(local_sourcing, "probe_company", _probe)
        monkeypatch.setattr(local_sourcing, "_websearch_board", _websearch)
        # The marketing page "validates" (a handful of scraped fragments)
        # but has zero LOCAL jobs.
        monkeypatch.setattr(local_sourcing, "_validate_board", lambda comp: (9, 0))

        hit = local_sourcing.resolve_board_sniff_first("Pfizer")

        assert calls == {"probe": 1, "websearch": 1}
        assert hit is not None and hit["ats"] == "custom" and hit["nc"] == 0

    def test_strong_custom_hit_short_circuits(self, monkeypatch):
        """nc > 0 on the custom hit DOES win immediately: neither probe nor
        websearch is ever called."""
        monkeypatch.setattr(sniffer, "sniff_ats", self._sniff_custom())
        calls = {"probe": 0, "websearch": 0}
        monkeypatch.setattr(
            local_sourcing, "probe_company",
            lambda *a, **k: calls.update(probe=calls["probe"] + 1))
        monkeypatch.setattr(
            local_sourcing, "_websearch_board",
            lambda *a, **k: calls.update(websearch=calls["websearch"] + 1))
        monkeypatch.setattr(local_sourcing, "_validate_board", lambda comp: (5, 2))

        hit = local_sourcing.resolve_board_sniff_first("Science.xyz")

        assert calls == {"probe": 0, "websearch": 0}
        assert hit["ats"] == "custom" and hit["nc"] == 2 and hit["via"] == "sniff"

    def test_held_custom_fallback_returned_when_nothing_better(self, monkeypatch):
        """probe and websearch both miss entirely -> the weak custom hit,
        not None, is the answer: it still beats no answer at all."""
        monkeypatch.setattr(sniffer, "sniff_ats", self._sniff_custom())
        monkeypatch.setattr(local_sourcing, "probe_company", lambda *a, **k: None)
        monkeypatch.setattr(local_sourcing, "_websearch_board", lambda *a, **k: None)
        monkeypatch.setattr(local_sourcing, "_validate_board", lambda comp: (9, 0))

        hit = local_sourcing.resolve_board_sniff_first("Novozymes")

        assert hit is not None
        assert hit["ats"] == "custom" and hit["nc"] == 0 and hit["via"] == "sniff"

    def test_better_probe_hit_wins_over_held_fallback(self, monkeypatch):
        """A real ATS found at step 2 (probe) beats the held custom
        fallback, even though the custom hit was found first."""
        monkeypatch.setattr(sniffer, "sniff_ats", self._sniff_custom())
        monkeypatch.setattr(
            local_sourcing, "probe_company",
            lambda name, try_workday=True: {
                "name": name, "ats": "greenhouse", "slug": "acme",
                "count": 10, "nc": 4})
        monkeypatch.setattr(
            local_sourcing, "_websearch_board",
            lambda *a, **k: pytest.fail("websearch must not run: probe already won"))

        def _validate(comp):
            return (9, 0) if comp["ats"] == "custom" else (10, 4)

        monkeypatch.setattr(local_sourcing, "_validate_board", _validate)

        hit = local_sourcing.resolve_board_sniff_first("Acme")

        assert hit["ats"] == "greenhouse" and hit["nc"] == 4 and hit["via"] == "probe"

    def test_better_websearch_hit_wins_over_held_fallback(self, monkeypatch):
        """A real ATS found only at step 3 (websearch) beats the held
        custom fallback when probe also misses."""
        monkeypatch.setattr(sniffer, "sniff_ats", self._sniff_custom())
        monkeypatch.setattr(local_sourcing, "probe_company", lambda *a, **k: None)
        monkeypatch.setattr(
            local_sourcing, "_websearch_board",
            lambda name, max_results=8: {
                "ats": "workday", "triple": ("acme", 1, "Acme")})

        def _validate(comp):
            return (9, 0) if comp["ats"] == "custom" else (20, 6)

        monkeypatch.setattr(local_sourcing, "_validate_board", _validate)

        hit = local_sourcing.resolve_board_sniff_first("Acme")

        assert hit["ats"] == "workday" and hit["nc"] == 6 and hit["via"] == "websearch"


class TestDiscoverLocalWebsearchPass:
    """Offline coverage for the Task 2 fix: discover_local's bulk pass now
    runs a bounded websearch step for names probe+sniff left boardless.
    gather_names, probe_company, _websearch_board, and core.store are all
    faked -- no network, no real DB."""

    class _FakeConn:
        def close(self):
            pass

    def _patch_common(self, monkeypatch, names, recent=frozenset()):
        monkeypatch.setattr(local_sourcing, "gather_names", lambda extra=None: list(names))
        monkeypatch.setattr(local_sourcing, "probe_company", lambda *a, **k: None)
        import core.store as store
        monkeypatch.setattr(store, "connect", lambda *a, **k: self._FakeConn())
        monkeypatch.setattr(store, "recent_miss_names",
                            lambda conn, days=14: set(recent))

    def test_websearch_cap_bounds_attempts(self, monkeypatch):
        names = [f"Company {i}" for i in range(6)]
        self._patch_common(monkeypatch, names)
        calls = []
        monkeypatch.setattr(
            local_sourcing, "_websearch_board",
            lambda name, max_results=8: calls.append(name))

        local_sourcing.discover_local(
            max_workers=2, js_majors=False, sniff=False,
            websearch=True, websearch_cap=2)

        assert len(calls) == 2

    def test_websearch_cap_zero_disables_the_pass(self, monkeypatch):
        names = ["Alpha", "Beta"]
        # Deliberately do NOT patch core.store here: cap=0 must short-circuit
        # before any DB connection is even attempted.
        monkeypatch.setattr(local_sourcing, "gather_names", lambda extra=None: list(names))
        monkeypatch.setattr(local_sourcing, "probe_company", lambda *a, **k: None)
        calls = []
        monkeypatch.setattr(
            local_sourcing, "_websearch_board",
            lambda name, max_results=8: calls.append(name))

        local_sourcing.discover_local(
            max_workers=2, js_majors=False, sniff=False,
            websearch=True, websearch_cap=0)

        assert calls == []

    def test_recently_missed_names_are_skipped(self, monkeypatch):
        names = ["Alpha", "Beta"]
        self._patch_common(monkeypatch, names, recent={"Alpha"})
        calls = []
        monkeypatch.setattr(
            local_sourcing, "_websearch_board",
            lambda name, max_results=8: calls.append(name))

        local_sourcing.discover_local(
            max_workers=2, js_majors=False, sniff=False,
            websearch=True, websearch_cap=10)

        assert calls == ["Beta"]


class TestPastedNamePreview:
    """Step one of the two-step paste flow: the names are shown, with what
    the store already knows about each, BEFORE anything is resolved.
    Resolving is the expensive half -- one page's 15 junk names cost about a
    thousand HTTP requests."""

    def _wire(self, monkeypatch, db):
        import core.store as store

        class _NoClose:
            # preview_names closes the connection it opens; the test still
            # needs to read the fixture DB afterwards.
            def __getattr__(self, k):
                return getattr(db, k)

            def close(self):
                pass

        monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose())

    def test_states_split_new_tracked_blocked_and_missed(self, monkeypatch, db):
        import core.store as store
        store.upsert_company(db, {"name": "IQVIA", "ats": "workday",
                                  "wd_tenant": "iqvia"})
        store.block_name(db, "Oncology", "not a company")
        store.record_miss(db, "Fennec Pharmaceuticals", "no-board-found")
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Alpaca Health", "IQVIA",
                                             "Oncology",
                                             "Fennec Pharmaceuticals"])
        rows = local_sourcing.preview_names("<pasted page>", use_llm=False)
        assert {r["name"]: r["state"] for r in rows} == {
            "Alpaca Health": "new", "IQVIA": "tracked",
            "Oncology": "blocked", "Fennec Pharmaceuticals": "missed"}
        assert [r["key"] for r in rows][0] == "alpacahealth"

    def test_the_profile_blocklist_counts_too(self, monkeypatch, db):
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing, "NAME_BLOCKLIST", {"biotech"})
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Biotech"])
        assert [r["state"] for r in
                local_sourcing.preview_names("x", use_llm=False)] == ["blocked"]

    def test_preview_resolves_nothing(self, monkeypatch, db):
        self._wire(monkeypatch, db)
        tried = []
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda *a, **k: tried.append(a) or (None, "x"))
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Alpaca Health"])
        assert [r["name"] for r in
                local_sourcing.preview_names("x", use_llm=False)] \
            == ["Alpaca Health"]
        assert tried == [], "the preview step resolved a name"

    def test_the_model_reads_the_paste_when_a_key_is_configured(
            self, monkeypatch, db):
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing.config, "ANTHROPIC_API_KEY",
                            "sk-ant-test")
        monkeypatch.setattr(local_sourcing, "extract_names_llm",
                            lambda *a, **k: ["Model Named Co"])
        assert [r["name"] for r in local_sourcing.preview_names("x")] \
            == ["Model Named Co"]

    def test_no_key_means_the_regex_parser(self, monkeypatch, db):
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing.config, "ANTHROPIC_API_KEY",
                            "YOUR_ANTHROPIC_API_KEY_HERE")
        monkeypatch.setattr(local_sourcing, "extract_names_llm",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("called with no API key")))
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Parsed Co"])
        assert [r["name"] for r in local_sourcing.preview_names("x")] \
            == ["Parsed Co"]


class TestAddNamesQueue:
    """Step two: add_names takes the CONFIRMED list, and everything it
    resolves lands in the review queue rather than on the roster."""

    _HIT = {"name": "Alpaca Health", "ats": "lever", "slug": "alpaca",
            "careers_url": "https://alpaca.example/careers",
            "count": 8, "nc": 3, "via": "sniff"}

    def _wire(self, monkeypatch, db, hit=None):
        import core.claude as claude
        import core.store as store

        class _NoClose:
            def __getattr__(self, k):
                return getattr(db, k)

            def close(self):
                pass

        monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose())
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda *a, **k: (hit or self._HIT, None))
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])
        monkeypatch.setattr(claude, "score_company_mission",
                            lambda *a, **k: ("adjacent", 0.5, "stub"))

    def test_a_resolved_name_lands_in_the_queue_not_the_roster(
            self, monkeypatch, db):
        import core.store as store
        self._wire(monkeypatch, db)
        local_sourcing.add_names(["Alpaca Health"], max_workers=1)
        assert [c["name"] for c in store.pending_companies(db)] \
            == ["Alpaca Health"]
        assert store.crawlable_companies(db) == []

    def test_a_blocklisted_name_is_never_resolved(self, monkeypatch, db):
        import core.store as store
        store.block_name(db, "Oncology", "not a company")
        self._wire(monkeypatch, db, {**self._HIT, "name": "Oncology"})
        tried = []
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda n, *a, **k: tried.append(n) or (None, "x"))
        local_sourcing.add_names(["Oncology"], max_workers=1)
        assert tried == []
        assert store.get_companies(db, active_only=False) == []

    def test_a_name_already_on_the_roster_is_not_re_resolved(
            self, monkeypatch, db):
        import core.store as store
        store.upsert_company(db, {"name": "Alpaca Health", "ats": "lever",
                                  "slug": "alpaca"})
        self._wire(monkeypatch, db)
        tried = []
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda n, *a, **k: tried.append(n) or (None, "x"))
        local_sourcing.add_names(["Alpaca Health"], max_workers=1)
        assert tried == []

    def test_a_raw_blob_still_works(self, monkeypatch, db):
        # The thin single-step path the CLI (and an older client) still use.
        import core.store as store
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Alpaca Health"])
        local_sourcing.add_names("Alpaca Health\n2 days ago", max_workers=1)
        assert [c["name"] for c in store.pending_companies(db)] \
            == ["Alpaca Health"]

    def test_a_websearch_only_hit_is_queued_instead_of_corroborated(
            self, monkeypatch, db):
        """add_names used to spend extra fetches proving a websearch hit had
        a local HQ, and wrote it inactive when it could not. The queue is
        that judgement now, and it costs nothing."""
        import core.store as store
        probed = []
        monkeypatch.setattr(local_sourcing, "nc_hq_signal",
                            lambda *a, **k: probed.append(a) or False)
        self._wire(monkeypatch, db, {**self._HIT, "nc": 0, "via": "websearch"})
        local_sourcing.add_names(["Alpaca Health"], max_workers=1)
        assert [c["name"] for c in store.pending_companies(db)] \
            == ["Alpaca Health"]
        assert probed == [], "the corroboration probe still runs"


class TestScoreMissionsHonoursTheReviewQueue:
    """score_missions revives an inactive, unscored row that scores to an
    on-mission tier (its original mission call is assumed to have failed).
    A row in the review queue is inactive for a different reason: nobody
    has confirmed it yet. The 2026-09-01 re-resolution pass queued 24
    unscored rows that this healer would otherwise have activated wholesale,
    skipping the queue."""

    def _wire(self, monkeypatch, db):
        import core.claude as claude
        import core.store as store

        class _NoClose:
            def __getattr__(self, k):
                return getattr(db, k)

            def close(self):
                pass

        monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose())
        monkeypatch.setattr(claude, "score_company_mission",
                            lambda *a, **k: ("adjacent", 0.5, "stub"))
        monkeypatch.setattr(local_sourcing, "_sample_titles", lambda h: [])

    def test_pending_rows_are_scored_but_not_revived(self, monkeypatch, db):
        import core.store as store
        from core import tags
        self._wire(monkeypatch, db)
        store.upsert_company(db, {"name": "Queued Co", "ats": "lever",
                                  "slug": "queued", "active": 0,
                                  "tags": tags.PENDING})
        store.upsert_company(db, {"name": "Failed Call Co", "ats": "lever",
                                  "slug": "failed", "active": 0})
        local_sourcing.score_missions(max_workers=1)
        rows = {r["name"]: dict(r) for r in
                db.execute("SELECT name, active, tags, mission_tier FROM companies")}
        assert rows["Queued Co"]["active"] == 0
        assert tags.has(rows["Queued Co"]["tags"], tags.PENDING)
        assert rows["Queued Co"]["mission_tier"] == "adjacent"
        assert rows["Failed Call Co"]["active"] == 1
