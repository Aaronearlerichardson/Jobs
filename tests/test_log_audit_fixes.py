"""Fixes from the 2026-09-09 session-log audit.

Four defects the logs showed, each pinned here offline:

  * a locality-scoped Workday pull that the tenant answered unnarrowed
    (NVIDIA, 2026-09-09: 60 pages, 1,199 detail GETs, 1,200 rows kept as
    local) is detected on page 0 and filtered on listed location only;
  * the "N Locations" detail rescue has a per-board budget;
  * names that are section headings, category nouns or location strings
    never reach the resolver (preview state, add_names miss, reresolve
    retirement);
  * the careers-page sniffer resolves each guessed host once, bounded,
    before fetching any path on it.
"""

import re

import requests

import core.store as store
from discovery import local_sourcing, sniffer
from discovery.names import junk_name_reason
from scrapers import ops
from scrapers.fetchers import workday as wd


# ─── Workday scope guard ─────────────────────────────────────────────────

# The fixtures are North Carolina boards; the suite runs on whatever
# profile is loaded, so the scope regex is spelled out rather than taken
# from core.locality.
NC_RE = re.compile(r"\bNC\b|North Carolina", re.I)


def _posting(loc, path, title="Data Engineer"):
    return {"title": title, "locationsText": loc, "externalPath": path,
            "postedOn": "Posted Today"}


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _WorkdaySession:
    """A Workday CXS tenant: `total` and `postings` answer every listing
    POST regardless of scope (the 2026-09-09 NVIDIA behaviour when
    `ignores_scope`), detail GETs are counted."""

    def __init__(self, postings, scoped, ignores_scope=False):
        self.postings, self.scoped = postings, scoped
        self.ignores_scope = ignores_scope
        self.detail_gets = []

    def post(self, url, json=None, **kw):
        body = json or {}
        scoped = bool(body.get("appliedFacets")) or bool(body.get("searchText"))
        rows = self.postings if (self.ignores_scope or not scoped) else self.scoped
        page = rows[body.get("offset", 0):body.get("offset", 0) + body.get("limit", 20)]
        return _Resp({"total": len(rows), "jobPostings": page,
                      "facets": [{"facetParameter": "locations", "values": [
                          {"id": "nc-id", "descriptor": "North Carolina"}]}]})

    def get(self, url, **kw):
        self.detail_gets.append(url)
        return _Resp({"jobPostingInfo": {"location": "US, NC, Durham"}})


class TestWorkdayScopeGuard:
    def _wire(self, monkeypatch, session):
        monkeypatch.setattr(wd, "SESSION", session)
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        monkeypatch.setattr(wd, "_wd_detail_locations",
                            lambda *a: ["US, NC, Durham"] if session.get(a[-1]) else [])
        monkeypatch.setattr(wd.time, "sleep", lambda *a: None)

    def test_unnarrowed_scope_keeps_listed_matches_only_without_detail_gets(
            self, monkeypatch, capsys):
        board = ([_posting("5 Locations", f"/job/US-CA-Santa-Clara/Eng_{i}")
                  for i in range(40)]
                 + [_posting("US, NC, Durham", "/job/US-NC-Durham/Eng_NC1"),
                    _posting("3 Locations", "/job/US-NC-Durham/Eng_NC2")])
        s = _WorkdaySession(board, scoped=board[:2], ignores_scope=True)
        self._wire(monkeypatch, s)
        out = wd.fetch_workday_all("nvidia", 5, "Site", loc_re=NC_RE,
                                   page_size=20, max_pages=3)
        assert [j["id"] for j in out] == ["wd_nvidia_Eng_NC1", "wd_nvidia_Eng_NC2"]
        assert s.detail_gets == [], "a failed scope must not detail-GET the board"
        assert "unnarrowed" in capsys.readouterr().out

    def test_a_scope_that_narrowed_still_expands_multi_location_rows(
            self, monkeypatch):
        board = [_posting("5 Locations", f"/job/US-CA-Santa-Clara/Eng_{i}")
                 for i in range(30)]
        local = [_posting("5 Locations", "/job/US-CA-Santa-Clara/Eng_NC")]
        s = _WorkdaySession(board, scoped=local)
        self._wire(monkeypatch, s)
        out = wd.fetch_workday_all("acme", 5, "Site", loc_re=NC_RE,
                                   page_size=20, max_pages=3)
        assert len(out) == 1 and out[0]["location"] == "US, NC, Durham"
        assert len(s.detail_gets) == 1

    def test_detail_rescue_has_a_per_board_budget(self, monkeypatch, capsys):
        local = [_posting("5 Locations", f"/job/US-CA-Santa-Clara/Eng_{i}")
                 for i in range(6)]
        s = _WorkdaySession(local + [_posting("US, TX, Austin", "/job/x/y")] * 30,
                            scoped=local)
        self._wire(monkeypatch, s)
        monkeypatch.setattr(wd, "_WD_RESCUE_CAP", 4)
        out = wd.fetch_workday_all("acme", 5, "Site", loc_re=NC_RE,
                                   page_size=20, max_pages=3)
        assert len(s.detail_gets) == 4
        # facet-vouched rows past the budget are kept on their listed text
        assert [j["location"] for j in out].count("5 Locations") == 2
        assert "detail budget" in capsys.readouterr().out

    def test_whole_board_pull_is_untouched_by_the_guard(self, monkeypatch, capsys):
        board = [_posting("US, CA, Santa Clara", f"/job/US-CA/Eng_{i}")
                 for i in range(25)]
        s = _WorkdaySession(board, scoped=[], ignores_scope=True)
        self._wire(monkeypatch, s)
        out = wd.fetch_workday_all("acme", 5, "Site", loc_re=None,
                                   page_size=20, max_pages=3)
        assert len(out) == 25
        assert "unnarrowed" not in capsys.readouterr().out


class TestWorkdayLocalCount:
    """The discovery probe's NC count goes through the same guard: a scope
    that returned the whole board counts listed locations instead of
    reporting local_job_count == total_job_count == cap."""

    def test_scoped_total_when_the_scope_narrowed(self, monkeypatch):
        s = _WorkdaySession([_posting("x", "/job/a/b")] * 50,
                            scoped=[_posting("US, NC, Durham", "/job/US-NC/a")] * 3)
        monkeypatch.setattr(wd, "SESSION", s)
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        assert wd.wd_local_count("acme", 5, "Site", NC_RE) == 3

    def test_listed_location_count_when_the_scope_failed(self, monkeypatch):
        board = ([_posting("US, CA, Santa Clara", f"/job/US-CA/Eng_{i}")
                  for i in range(150)]
                 + [_posting("US, NC, Durham", "/job/US-NC-Durham/Eng_NC")])
        s = _WorkdaySession(board, scoped=[], ignores_scope=True)
        monkeypatch.setattr(wd, "SESSION", s)
        monkeypatch.setattr(wd, "_wd_cxs_tenant", lambda t, p, s: t)
        n = wd.wd_local_count("nvidia", 5, "Site", NC_RE, page_size=20,
                              sample_pages=2)
        assert n == 0            # the NC row sits past the sampled pages
        assert n != len(board)   # and the whole board is never reported


# ─── junk names ──────────────────────────────────────────────────────────

class TestJunkNames:
    def test_the_audit_names_are_all_caught(self):
        seen = ["Required Qualifications", "Proficiency in SQL.", "Oncology",
                "Medical Devices", "99+ results", "Title",
                "Raleigh-Durham-Chapel Hill Area (On-site)",
                "Luna Physical Therapy 1", "Fairwai 1"]
        assert all(junk_name_reason(n) for n in seen), \
            [n for n in seen if not junk_name_reason(n)]

    def test_roster_names_pass(self, db):
        # The names the roster actually carries must not be screened out.
        for n in ("Beacon Biosignals", "SAS Institute", "Judi Health",
                  "Cala Health, Inc.", "Duke University", "UNC Chapel Hill",
                  "3M", "Q2 Solutions", "Blue Cross NC", "GRAIL", "NVIDIA",
                  "Bausch + Lomb", "J&J MedTech", "Red Hat", "Aah",
                  "Music and Cognition Lab", "Precision Neuroscience",
                  "Wellcome Centre for Human Neuroimaging"):
            assert junk_name_reason(n) == "", n


class TestJunkNamesInThePasteFlow:
    def _wire(self, monkeypatch, db):
        class _NoClose:
            def __getattr__(self, k):
                return getattr(db, k)

            def close(self):
                pass
        monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose())

    def test_preview_marks_junk_unticked_with_a_reason(self, monkeypatch, db):
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Alpaca Health",
                                             "Required Qualifications"])
        rows = local_sourcing.preview_names("x", use_llm=False)
        assert [(r["name"], r["state"]) for r in rows] == [
            ("Alpaca Health", "new"), ("Required Qualifications", "junk")]
        assert rows[1]["why"] == "section-heading"

    def test_blocked_beats_junk_in_the_preview(self, monkeypatch, db):
        store.block_name(db, "Oncology", "not a company")
        self._wire(monkeypatch, db)
        monkeypatch.setattr(local_sourcing, "parse_company_names",
                            lambda *a, **k: ["Oncology"])
        assert [r["state"] for r in
                local_sourcing.preview_names("x", use_llm=False)] == ["blocked"]

    def test_add_names_records_junk_as_a_miss_and_never_resolves_it(
            self, monkeypatch, db):
        self._wire(monkeypatch, db)
        tried = []
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda n, *a, **k: tried.append(n) or (None, "x"))
        local_sourcing.add_names(["Proficiency in SQL.", "Alpaca Health"],
                                 max_workers=1)
        assert tried == ["Alpaca Health"]
        row = db.execute("SELECT miss_reason, active FROM companies "
                         "WHERE name='Proficiency in SQL.'").fetchone()
        assert row["miss_reason"] == "junk-name:section-heading"
        assert row["active"] == 0
        # ...and a junk-family miss is not a re-resolution candidate.
        assert [c["name"] for c in ops._reresolve_candidates(db)] == []


class TestJunkNamesInReresolve:
    T = {"db_path": None}

    def test_old_junk_misses_are_retired_not_retried(self, monkeypatch, db):
        store.record_miss(db, "Required Qualifications", "no-board-found:x")
        store.record_miss(db, "Emmes", "no-board-found:wrong-domain")
        tried = []
        monkeypatch.setattr(local_sourcing, "resolve_or_miss",
                            lambda n, *a, **k: tried.append(n)
                            or (None, "no-board-found:x"))
        ops.reresolve_misses(conn=db, max_workers=1, t=self.T)
        assert tried == ["Emmes"]
        assert db.execute("SELECT miss_reason FROM companies WHERE "
                          "name='Required Qualifications'").fetchone()[0] \
            == "junk-name:section-heading"
        assert [c["name"] for c in ops._reresolve_candidates(db)] == ["Emmes"]


# ─── sniffer DNS pre-check ───────────────────────────────────────────────

class TestSnifferResolvesHostsOnce:
    def _reset(self, monkeypatch):
        monkeypatch.setattr(sniffer, "_DEAD_HOSTS", {})
        monkeypatch.setattr(sniffer, "_DNS_CACHE", {})
        monkeypatch.setattr(sniffer, "_PAGE_MEMO", {})

    def test_unresolvable_hosts_are_never_fetched(self, monkeypatch):
        self._reset(monkeypatch)
        looked_up, fetched = [], []

        def _gai(host, *a, **k):
            looked_up.append(host)
            if host.startswith("dead"):
                raise OSError("no such host")
            return [("addr",)]
        monkeypatch.setattr(sniffer.socket, "getaddrinfo", _gai)
        monkeypatch.setattr(sniffer, "_fetch_page",
                            lambda u, **k: fetched.append(u) or None)
        urls = ["https://dead.example/careers", "https://dead.example/",
                "https://dead.example/jobs", "https://live.example/careers"]
        out = sniffer._fetch_all(urls)
        assert sorted(looked_up) == ["dead.example", "live.example"], \
            "each host resolved once, not once per path"
        assert fetched == ["https://live.example/careers"]
        assert set(out) == set(urls) and out["https://dead.example/"] is None
        # a later stage rebuilding the list asks the resolver nothing
        looked_up.clear()
        sniffer._fetch_all(["https://dead.example/en/jobs",
                            "https://live.example/"])
        assert looked_up == []

    def test_a_silent_resolver_skips_the_host_this_pass_only(self, monkeypatch):
        import threading
        self._reset(monkeypatch)
        gate = threading.Event()

        def _gai(host, *a, **k):
            gate.wait(2)
            return [("addr",)]
        monkeypatch.setattr(sniffer.socket, "getaddrinfo", _gai)
        kept = sniffer._drop_unresolvable(["https://slow.example/"], timeout=0.05)
        assert kept == []
        assert sniffer._dead_host("https://slow.example/") == "", \
            "a slow resolver is not a missing name"
        gate.set()

    def test_a_refused_connection_still_marks_the_host_dead(self, monkeypatch):
        self._reset(monkeypatch)

        class _S:
            def get(self, url, **kw):
                raise requests.exceptions.ConnectionError("refused")
        monkeypatch.setattr(sniffer, "SESSION", _S())
        monkeypatch.setattr(sniffer.socket, "getaddrinfo",
                            lambda *a, **k: [("addr",)])
        assert sniffer._fetch_page("https://x.example/") is None
        assert sniffer._drop_unresolvable(["https://x.example/careers"]) == []
