"""Shared fixtures.

Everything here is OFFLINE and profile-agnostic: the suite has to pass on
the shipped `profile.example.toml` (what CI checks out) and on a real
`profile.toml` (what you have locally). So fixtures derive their inputs
from whatever profile is loaded rather than hard-coding one person's
cities, keywords, or track names.

Nothing in the suite may touch the Claude API or the network.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                      # importable as `pytest tests`
    sys.path.insert(0, str(ROOT))

from src import config as _config                           # noqa: E402
import src.session_log as _session_log            # noqa: E402
import src.store as _store                        # noqa: E402
import src.crawl.runner as _runner                  # noqa: E402
import src.ats.fetchers.board as _board             # noqa: E402
import src.ats.fetchers.probe as _job_probe         # noqa: E402
import src.discovery.resolve.fetchpool as _fetchpool  # noqa: E402
from src.net import http as _http                   # noqa: E402


@pytest.fixture(autouse=True)
def _outputs_to_tmp(tmp_path, monkeypatch):
    """Session logs and digest files never land in the real data dir
    during tests: the webapp op runner opens a log per op, and a harvest
    pass rewrites every roster track's digest."""
    monkeypatch.setattr(_session_log, "_log_dir",
                        lambda: tmp_path / "session-logs")
    monkeypatch.setattr(_config, "REPORT_DIR", tmp_path / "job_reports")


@pytest.fixture(autouse=True)
def _fresh_run_state(monkeypatch):
    """Per-run memos start empty in every test: both dead-host breakers,
    discovery's page memo and DNS cache, and the board engine's listing
    memo."""
    for mod in (_fetchpool, _job_probe):
        old = mod._DEAD_HOSTS
        monkeypatch.setattr(mod, "_DEAD_HOSTS", _http.HostBreaker(old.ttl, old.trips))
    monkeypatch.setattr(_fetchpool, "_PAGE_MEMO", {})
    monkeypatch.setattr(_fetchpool, "_DNS_CACHE", {})
    monkeypatch.setattr(_board, "_MEMO", {})


# --------------------------------------------------------------------------- #
#  Config / profile
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def cfg():
    """The live config module (profile.toml if present, else the example)."""
    return _config


@pytest.fixture(scope="session")
def local_track():
    return _runner.track_for_engine("local")


@pytest.fixture(scope="session")
def sweep_track():
    return _runner.track_for_engine("sweep")


@pytest.fixture(scope="session")
def local_addr(cfg):
    """An address the ACTIVE profile considers local, e.g. 'Durham, NC' or
    'San Francisco, CA'. Skips the test if no locality is configured."""
    place = next((s for s in cfg.LOCALITY_SUBSTRINGS if len(s) > 4), None)
    if not place:
        pytest.skip("profile configures no locality substrings")
    suffix = (cfg.LOCALITY_STATE_SUFFIX or [""])[0].upper()
    return f"{place.title()}, {suffix}".strip().rstrip(",")


@pytest.fixture(scope="session")
def elsewhere():
    """Somewhere no sane profile calls local — verified, not assumed."""
    import src.match.locality as locality
    for place in ("Ulaanbaatar, Mongolia", "Reykjavik, Iceland",
                  "Hobart, Tasmania"):
        if not locality.is_nc(place):
            return place
    pytest.skip("every candidate 'elsewhere' matches this profile's locality")


#: What `pristine_keywords` promises to hand back. Spelled out here rather
#: than read off the snapshot helpers' own tuples: the check below has to be
#: independent of the code it checks, or it can only ever agree with it.
_KEYWORD_LISTS = ("CORE_KEYWORDS", "DOMAIN_KEYWORDS", "SKILL_KEYWORDS",
                  "INCLUDE_KEYWORDS", "EXCLUDE_PHRASES",
                  "EXCLUDE_TITLE_PHRASES")


def _keyword_state(cfg):
    return {**{n: list(getattr(cfg, n)) for n in _KEYWORD_LISTS},
            "ACCEPT_REMOTE": bool(cfg.ACCEPT_REMOTE)}


@pytest.fixture
def pristine_keywords(cfg):
    """Snapshot/restore config's shared keyword and exclude lists.

    `runner.apply_keyword_focus` mutates the keyword lists IN PLACE (that's
    the contract filters.py depends on) and `cfg.widen_keywords` empties the
    exclude lists too, so any test that does either would leak into the
    next one without this.

    Verified on the way out, so a list the restore fails to put back fails
    the test that changed it. It used to fail nothing: a widening emptied
    EXCLUDE_TITLE_PHRASES for the rest of the session, and the only symptom
    was a later, unrelated test quietly skipping.
    """
    saved = cfg.keyword_snapshot(cfg)
    before = _keyword_state(cfg)
    yield
    cfg.restore_keywords(saved, cfg)
    after = _keyword_state(cfg)
    leaked = sorted(n for n, was in before.items() if after[n] != was)
    assert not leaked, f"pristine_keywords did not put back: {leaked}"


@pytest.fixture
def exclude_vocab(monkeypatch):
    """Factory: `exclude_vocab("t_x", clinical_titles=[...], ...)` gives
    track "t_x" that [exclude.<track>] vocabulary for one test and returns
    the track id.

    A synthetic table rather than the active profile's, so a test can pin
    the SHAPE of a vocabulary whether or not profile.toml (or the
    profile.example.toml CI runs against) happens to configure it.

    `gates._exclude_tables` is lru_cache-backed, so the cache is cleared on
    BOTH sides: on the way in so the profile's own table for that id is not
    already memoized, and on the way out so a later test sharing the id
    does not read this one's vocabulary. Defined here because two files
    configure one this way and the cache half is the easy half to forget.
    """
    import src.match.gates as gates

    def _configure(track_id, **tables):
        monkeypatch.setitem(gates.config.EXCLUDE_BY_TRACK, track_id, tables)
        gates._exclude_tables.cache_clear()
        return track_id

    gates._exclude_tables.cache_clear()
    yield _configure
    gates._exclude_tables.cache_clear()


# --------------------------------------------------------------------------- #
#  Store
# --------------------------------------------------------------------------- #

@pytest.fixture
def db():
    """A fresh in-memory store with the full schema applied."""
    conn = _store.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def company(db):
    """One greenhouse-backed company row; returns its id."""
    return _store.upsert_company(
        db, {"name": "Acme", "ats": "greenhouse", "slug": "acme"})


@pytest.fixture
def add_job(db, company, local_addr):
    """Factory: add_job('gh_acme_1', title=..., fit=0.9, **overrides).

    The default title carries the job_id, so two rows added at this one
    company are two DISTINCT openings. They have to be: store.ranked_jobs
    collapses same-company/same-title rows to one survivor by default, so a
    shared default title silently cost every ranking-backed test one of its
    rows. Pass the SAME explicit title to both rows when a test wants that
    collapse (tests/test_store.py's TestCollapse), or an exact title when it
    asserts on the rendered string.
    """
    def _add(job_id, title=None, fit=None, track="local-tech",
             **overrides):
        row = {"job_id": job_id, "company_id": company, "company_name": "Acme",
               "title": title or f"Data Engineer {job_id}",
               "url": f"https://acme.io/{job_id}",
               "location": local_addr, "track": track,
               "resume_fit_score": fit}
        row.update(overrides)
        _store.upsert_job(db, row)
        return job_id
    return _add


@pytest.fixture
def status_of(db):
    """Read a job's (status, closed_at) — the closed-lifecycle assertions."""
    def _status(job_id):
        return db.execute(
            "SELECT status, closed_at FROM jobs WHERE job_id=?",
            (job_id,)).fetchone()
    return _status


# --------------------------------------------------------------------------- #
#  Web app
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def client():
    """Flask test client — exercises the real routes with no socket."""
    from src import web
    web.app.config["TESTING"] = True
    return web.app.test_client()


@pytest.fixture
def wired_db_path(tmp_path, monkeypatch):
    """A throwaway store, wired into BOTH places the code looks one up.

    A route opens `UI_TRACKS[track]["db_path"]` (ops.maintenance.track_store);
    everything that runs outside a request — discovery's preview_names,
    capture's ingest, harvest/triage's own defaults — falls back to
    `config.STORE_DB_PATH`. Seven helpers across two files wired one or both
    of those by hand, and four of them wired only the track config, which
    holds only for as long as the test stays on the route side of that line.
    Both are wired here, always, so no test can reach the real store by
    taking one step past what its own setup happened to cover.

    Returns the path; the file itself is created by whoever connects first.
    """
    db_path = tmp_path / "wired.db"
    monkeypatch.setattr(_config, "STORE_DB_PATH", db_path)
    monkeypatch.setitem(_config.UI_TRACKS[_config.DEFAULT_TRACK],
                        "db_path", db_path)
    return db_path


# --------------------------------------------------------------------------- #
#  Store plumbing the tests share
# --------------------------------------------------------------------------- #

def iso_days_ago(days):
    """An ISO timestamp `days` days back — the shape every age column in the
    store (last_seen, harvested_at, miss_at) is compared against."""
    return (datetime.now() - timedelta(days=days)).isoformat()


def company_row(conn, name, ats="greenhouse", **extra):
    """Upsert one company by name and hand back the stored row.

    `extra` lands last, so a caller's own `ats`/`slug` still wins. Two files
    had written this same three-liner out byte for byte.
    """
    _store.upsert_company(conn, {"name": name, "ats": ats,
                                 "slug": name.lower(), **extra})
    return _store.get_company(conn, _store.company_id_by_name(conn, name))


def make_board_fn(*, before=None, err=None, fetched=0, new=0, hydrated=0,
                  closed=0, reopened=0, secs=0.0, **extra):
    """A stand-in for `harvest.harvest_board`, for tests of `harvest.run`.

    `run()` reads a fixed set of keys off whatever board_fn hands back, and
    eight tests across two files had each spelled that dict out by hand — so
    a key run() starts reading has to be added in eight places, and the ones
    that miss it fail with a KeyError nowhere near the change.

    `extra` carries the optional keys only some callers set (fetch_errors,
    last_error). `before(company)` runs before the dict is returned, for the
    tests that need the call recorded, blocked, or raised from.
    """
    def board_fn(company, db_path, progress=lambda: None, hydrate=True):
        if before is not None:
            before(company)
        return {"err": err, "fetched": fetched, "new": new,
                "hydrated": hydrated, "closed": closed, "reopened": reopened,
                "secs": secs, **extra}
    return board_fn


def keep_store_open(monkeypatch, db):
    """Point `store.connect()` at the test's OWN connection, with close()
    disarmed.

    The paths under test (add_names, preview_names, populate_companies,
    score_missions, reresolve) open their own connection and close it when
    they are done, which would leave the test with nothing to assert
    against. Five test classes across two files had written this same
    eight-line passthrough out.

    A plain function rather than a fixture: it is called from the `_wire`
    helpers those classes already have, and threading one more fixture
    through twenty-six test signatures to reach them would cost more than
    it saves.
    """
    import src.store as store

    class _NoClose:
        def __getattr__(self, k):
            return getattr(db, k)

        def close(self):
            pass

    monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose())
    return db


def fake_response(payload=None, *, text=None, status=200, content=None, url=""):
    """A stand-in for a requests Response: `status_code`,
    `raise_for_status()`, `json()`, `text`, `content`, `url`,
    `headers` (empty).

    Thirteen of these were defined across five test files, each with its
    own idea of which two or three attributes mattered and what
    raise_for_status should raise. That is fine until a fetcher starts
    reading an attribute one stub happens not to have, and the test that
    should have caught it passes because a DIFFERENT stub has it.

    `payload` is what json() returns; `text` defaults to that payload as
    JSON, so a stub serves both the JSON and the scrape paths. `status` >=
    400 makes raise_for_status raise requests.HTTPError carrying this
    response, as requests does (claude.api reads its status and body). `url`
    is the FINAL url a redirect-following GET reports (what
    sniffer.candidate_pages reads); it is always present, so no caller has to
    bolt one on.
    """
    body = text if text is not None else (
        json.dumps(payload) if payload is not None else "")

    final_url = url

    class _Response:
        status_code = status
        url = final_url
        headers = {}

        def raise_for_status(self):
            if status >= 400:
                raise requests.HTTPError(f"{status} Error", response=self)

        def json(self):
            if payload is None:
                raise ValueError("no JSON payload on this stub")
            return payload

        @property
        def text(self):
            return body

        @property
        def content(self):
            return body.encode() if content is None else content

    return _Response()


class Request(str):
    """One request a `serve`d session answered: equal to its URL with any
    `params` appended as a query string. `.url` is the URL alone; `.method`,
    `.params`, `.headers` and `.kw` are what was sent."""


@pytest.fixture
def serve(monkeypatch):
    """Answer SESSION's GETs and POSTs from the test, never the network:
    `serve(reply)` installs `reply` and returns the log of `Request`s.

    `reply` is a response (fake_response); an Exception, raised; a str
    body or an int status; a callable(url, **kw) returning any of these; a
    {fragment: reply} dict, routed on the first fragment found in the URL
    and its query (none found is a 404, or an AssertionError with
    `strict=True`); or a list served front first, its last item repeating
    -- the caller's own list, so a test may append after installing.

    Patches net.http.SESSION, the one every fetcher shares; `session=`
    names another (claude.api keeps its own).
    """
    def _resolve(reply, url, full, kw, strict):
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
            return _resolve(reply, url, full, kw, strict)
        if isinstance(reply, dict):
            for fragment, routed in reply.items():
                if fragment in full:
                    return _resolve(routed, url, full, kw, strict)
            if strict:
                raise AssertionError(f"request to an unrouted URL: {full}")
            return fake_response(text="", status=404)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, str):
            return fake_response(text=reply)
        if isinstance(reply, int):
            return fake_response(text="", status=reply)
        if callable(reply):
            return _resolve(reply(url, **kw), url, full, kw, strict)
        return reply

    def _install(reply, session=None, strict=False):
        log = []

        def _method(name):
            def _call(url, *args, **kw):
                params = dict(kw.get("params") or {})
                full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items())
                              if params else "")
                req = Request(full)
                req.url, req.method, req.params, req.kw = url, name, params, kw
                req.headers = dict(kw.get("headers") or {})
                log.append(req)
                return _resolve(reply, url, full, kw, strict)
            return _call

        target = session or _http.SESSION
        monkeypatch.setattr(target, "get", _method("GET"))
        monkeypatch.setattr(target, "post", _method("POST"))
        return log
    return _install
