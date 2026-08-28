"""Shared fixtures.

Everything here is OFFLINE and profile-agnostic: the suite has to pass on
the shipped `profile.example.toml` (what CI checks out) and on a real
`profile.toml` (what you have locally). So fixtures derive their inputs
from whatever profile is loaded rather than hard-coding one person's
cities, keywords, or track names.

Nothing in the suite may touch the Claude API or the network.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                      # importable as `pytest tests`
    sys.path.insert(0, str(ROOT))

import config as _config                           # noqa: E402
import core.session_log as _session_log            # noqa: E402
import core.store as _store                        # noqa: E402
import scrapers.runner as _runner                  # noqa: E402


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
    import core.locality as locality
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
    saved = (list(cfg.CORE_KEYWORDS), list(cfg.DOMAIN_KEYWORDS),
             list(cfg.SKILL_KEYWORDS), list(cfg.INCLUDE_KEYWORDS),
             cfg.ACCEPT_REMOTE)
    yield
    core, dom, skill, inc, accept = saved
    cfg.CORE_KEYWORDS[:] = core
    cfg.DOMAIN_KEYWORDS[:] = dom
    cfg.SKILL_KEYWORDS[:] = skill
    cfg.INCLUDE_KEYWORDS[:] = inc
    cfg.ACCEPT_REMOTE = accept


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
    import webapp
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()
