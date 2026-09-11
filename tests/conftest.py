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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                      # importable as `pytest tests`
    sys.path.insert(0, str(ROOT))

from src import config as _config                           # noqa: E402
import src.session_log as _session_log            # noqa: E402
import src.store as _store                        # noqa: E402
import src.crawl.runner as _runner                  # noqa: E402


@pytest.fixture(autouse=True)
def _session_logs_to_tmp(tmp_path, monkeypatch):
    """Session logs never land in the real data dir during tests. The
    webapp op runner opens one per op, and several tests drive it."""
    monkeypatch.setattr(_session_log, "_log_dir",
                        lambda: tmp_path / "session-logs")


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


@pytest.fixture
def pristine_keywords(cfg):
    """Snapshot/restore config's shared keyword lists.

    `runner.apply_keyword_focus` mutates them IN PLACE (that's the contract
    filters.py depends on), so any test that applies a track's focus would
    leak into the next one without this.
    """
    saved = cfg.keyword_snapshot(cfg)
    yield
    cfg.restore_keywords(saved, cfg)


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
    """Factory: add_job('gh_acme_1', title=..., fit=0.9, **overrides)."""
    def _add(job_id, title="Data Engineer", fit=None, track="local-tech",
             **overrides):
        row = {"job_id": job_id, "company_id": company, "company_name": "Acme",
               "title": title, "url": f"https://acme.io/{job_id}",
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


# --------------------------------------------------------------------------- #
#  Store plumbing the tests share
# --------------------------------------------------------------------------- #

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


def fake_response(payload=None, *, text=None, status=200, content=None):
    """A stand-in for a requests Response: `status_code`,
    `raise_for_status()`, `json()`, `text`, `content`.

    Thirteen of these were defined across five test files, each with its
    own idea of which two or three attributes mattered and what
    raise_for_status should raise. That is fine until a fetcher starts
    reading an attribute one stub happens not to have, and the test that
    should have caught it passes because a DIFFERENT stub has it.

    `payload` is what json() returns; `text` defaults to that payload as
    JSON, so a stub serves both the JSON and the scrape paths. `status`
    >= 400 makes raise_for_status raise, which is what net.http.get_json
    turns into a reported miss.
    """
    body = text if text is not None else (
        json.dumps(payload) if payload is not None else "")

    class _Response:
        status_code = status

        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"{status} Error")

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
