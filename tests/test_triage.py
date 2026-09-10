"""Harvest triage (src/crawl/triage.py): the harvester's unscored rows go
through the crawl's gates cheapest-first, and only survivors pay for a
body fetch or a fit score. Offline: the mission scorer, the hydrator and
the fit scorer are all stubbed."""

import pytest

from src import tags
from src import store
from src.claude.fit import FitResult
from src.crawl import harvest, triage

LOCAL = "local-tech"
SWEEP = "remote-neural"


def _company(conn, name, **extra):
    store.upsert_company(conn, {"name": name, "ats": "greenhouse",
                                "slug": name.lower(), **extra})
    return store.get_company(conn, store.company_id_by_name(conn, name))


def _harvested(conn, c, jid, title, location, description=""):
    store.upsert_job(conn, {"job_id": jid, "company_id": c["id"],
                            "company_name": c["name"], "title": title,
                            "url": f"https://x.test/j/{jid}",
                            "location": location, "description": description,
                            "harvested_at": "2026-09-10T01:00:00"})


def _row(conn, jid):
    return dict(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                             (jid,)).fetchone())


@pytest.fixture
def tracks(local_track, sweep_track):
    """A location-scoped track and a core-anchored sweep track, with the
    profile-dependent knobs pinned so the assertions hold on any profile."""
    local = {**local_track, "id": "t_local", "track": LOCAL,
             "sources": {**local_track["sources"], "store": True},
             "store_tag": None, "geo_gate": True, "require_core_anchor": False,
             "exclude_gate": False, "min_mission": 0.2,
             "remote_mission_floor": 0.85, "digest_min_fit": 0.4,
             "keyword_mode": "extend"}
    sweep = {**sweep_track, "id": "t_sweep", "track": SWEEP,
             "sources": {**sweep_track["sources"], "store": True},
             "store_tag": tags.SWEEP, "geo_gate": False,
             "require_core_anchor": True, "exclude_gate": False,
             "min_mission": None, "digest_min_fit": 0.4,
             "keyword_mode": "extend"}
    return [local, sweep]


@pytest.fixture
def stubs(monkeypatch):
    """Stub the three paid/network steps and record what they were asked."""
    calls = {"mission": [], "hydrate": [], "score": []}

    def mission(name, context=""):
        calls["mission"].append(name)
        return "other", 0.05, "off-mission"

    def hydrate(company, jobs, **kw):
        n = 0
        for j in jobs:
            calls["hydrate"].append(j["id"])
            j["_tried"] = True
            if "nobody" not in j["id"]:
                j["description"] = "python sql pipelines " * 20
                n += 1
        return {"hydrated": n, "unhydrated": len(jobs) - n}

    def score(title, description=""):
        calls["score"].append(title)
        return FitResult(score=0.1 if "weak" in title.lower() else 0.7,
                         reason="stub")

    monkeypatch.setattr(triage, "score_resume_fit", score)
    monkeypatch.setattr(triage, "core_anchor",
                        lambda title, desc="": "eeg"
                        if "eeg" in f"{title} {desc}".lower() else None)
    calls["mission_fn"] = mission
    calls["hydrate_fn"] = hydrate
    return calls


def _run(db, tracks, stubs, **kw):
    return triage.run(db_path=db, tracks=tracks,
                      mission_scorer=stubs["mission_fn"],
                      hydrate_fn=stubs["hydrate_fn"], max_workers=2, **kw)


# ── free gates first ────────────────────────────────────────────────────────

def test_title_drop_costs_no_fetch_and_no_score(tmp_path, tracks, stubs,
                                                local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "nurse", "Registered Nurse", local_addr)
    _harvested(conn, c, "eng", "Data Engineer", local_addr)

    s = _run(db, tracks, stubs)

    assert stubs["hydrate"] == ["eng"] and stubs["score"] == ["Data Engineer"]
    assert not stubs["mission"], "a scored company is never re-scored"
    nurse, eng = _row(conn, "nurse"), _row(conn, "eng")
    assert nurse["triage_status"] == "title"
    assert nurse["triage_detail"] == f"{LOCAL}=title"
    assert nurse["track"] is None and nurse["resume_fit_score"] is None
    assert eng["triage_status"] == "ok"
    assert store.track_set(eng["track"]) == {LOCAL}
    assert eng["resume_fit_score"] == 0.7 and eng["description"]
    assert store.crawl_seen(conn, "eng") and not store.crawl_seen(conn, "nurse")
    assert (s["title"], s["surfaced"], s["hydrated"], s["scored"]) == (1, 1, 1, 1)


def test_geo_drop_before_hydration_unless_trusted(tmp_path, tracks, stubs,
                                                  elsewhere, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    plain = _company(conn, "Plain", mission_tier="adjacent", mission_score=0.5)
    watched = _company(conn, "Watched", mission_tier="adjacent",
                       mission_score=0.5, tags=tags.WATCH)
    _harvested(conn, plain, "far", "Data Engineer", elsewhere)
    _harvested(conn, plain, "rem", "Data Engineer", "Remote - US")
    _harvested(conn, watched, "wrem", "Data Engineer", "Remote - US")
    _harvested(conn, plain, "nloc", "Data Engineer", "2 Locations")
    _harvested(conn, plain, "body", "Data Engineer", elsewhere,
               description=f"Our HQ is in {local_addr}. " * 10)
    _harvested(conn, plain, "hq", "Data Engineer", "See posting",
               description=f"based at our {local_addr} office " * 10)
    trusted = _company(conn, "Trusted", mission_tier="core-mission",
                       mission_score=0.9)
    _harvested(conn, trusted, "tbody", "Data Engineer", elsewhere,
               description="this is a fully remote role " * 10)
    _harvested(conn, trusted, "tunk", "Data Engineer", "",
               description="this is a fully remote role " * 10)

    _run(db, tracks, stubs)

    assert _row(conn, "far")["triage_status"] == "geo"
    assert _row(conn, "rem")["triage_status"] == "geo"      # onsite only
    assert _row(conn, "wrem")["triage_status"] == "ok"      # watch admits remote
    assert _row(conn, "wrem")["remote_eligible"] == 1
    # The Workday placeholder waits for the detail page, which (stubbed
    # here) does not name a local place: judged geo after the body arrives.
    assert _row(conn, "nloc")["triage_status"] == "geo"
    # Body text never makes a plain company's out-of-area row local...
    assert _row(conn, "body")["triage_status"] == "geo"
    # ...unless the listing named no place and the body has "<place>, ST".
    assert _row(conn, "hq")["triage_status"] == "ok"
    # A trusted company's remote admission reads the location field too;
    # body prose counts only when the listing named no place.
    assert _row(conn, "tbody")["triage_status"] == "geo"
    assert _row(conn, "tunk")["triage_status"] == "ok"
    assert sorted(stubs["hydrate"]) == ["nloc", "wrem"]   # tunk had a body


def test_mission_gate_scores_a_company_once_and_caches_it(tmp_path, tracks,
                                                          stubs, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Unknown Co")           # never mission-scored
    _harvested(conn, c, "a", "Data Engineer", local_addr)
    _harvested(conn, c, "b", "Software Engineer", local_addr)

    s = _run(db, tracks, stubs)

    assert stubs["mission"] == ["Unknown Co"]
    assert not stubs["hydrate"] and not stubs["score"]
    assert s["mission"] == 2
    row = store.get_company(conn, c["id"])
    assert (row["mission_tier"], row["mission_score"]) == ("other", 0.05)
    assert row["active"] == 1, "triage caches the score, never deactivates"
    # A later pass finds the cached verdict and does not ask again.
    _harvested(conn, c, "c", "Data Engineer", local_addr)
    _run(db, tracks, stubs)
    assert stubs["mission"] == ["Unknown Co"]
    assert _row(conn, "c")["triage_status"] == "mission"


def test_multi_division_company_waits_for_the_body(tmp_path, tracks, stubs,
                                                   local_addr, monkeypatch):
    from src import config
    monkeypatch.setattr(config, "is_multi_division",
                        lambda name: (name or "").lower() == "megacorp")
    monkeypatch.setattr(triage, "is_relevant",
                        lambda title, desc="": "pipelines" in desc)
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Megacorp", mission_tier="other", mission_score=0.05)
    _harvested(conn, c, "rel", "Data Engineer", local_addr)
    _harvested(conn, c, "irr", "Data Engineer", local_addr,
               description="sells ad space " * 20)

    _run(db, tracks, stubs)

    assert _row(conn, "irr")["triage_status"] == "division"
    assert _row(conn, "rel")["triage_status"] == "ok"
    assert stubs["hydrate"] == ["rel"]


# ── hydration and scoring only for survivors ────────────────────────────────

def test_bodiless_survivor_stays_pending(tmp_path, tracks, stubs, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "nobody", "Data Engineer", local_addr)

    s = _run(db, tracks, stubs)

    row = _row(conn, "nobody")
    assert row["triage_status"] is None and row["track"] is None
    assert row["desc_checked_at"], "the failed fetch is stamped"
    assert not stubs["score"] and s["left"] == 1
    # Next pass: still pending, but not re-fetched inside the retry window.
    _run(db, tracks, stubs)
    assert stubs["hydrate"] == ["nobody"]


def test_score_cap_leaves_the_rest_for_the_next_pass(tmp_path, tracks, stubs,
                                                     local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    for i in range(3):
        _harvested(conn, c, f"j{i}", "Data Engineer", local_addr)

    s = _run(db, tracks, stubs, score_cap=1)

    assert len(stubs["score"]) == 1 and s["surfaced"] == 1 and s["left"] == 2
    pending = store.triage_pending(conn)
    assert len(pending) == 2
    assert all(p["description"] for p in pending), "bodies are kept"
    # The next pass scores them without fetching anything.
    stubs["hydrate"].clear()
    s2 = _run(db, tracks, stubs, score_cap=5)
    assert not stubs["hydrate"] and s2["surfaced"] == 2
    assert not store.triage_pending(conn)


def test_fit_under_the_digest_floor_is_recorded_but_still_tracked(
        tmp_path, tracks, stubs, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "w", "Weak Data Engineer", local_addr)

    s = _run(db, tracks, stubs)

    row = _row(conn, "w")
    assert row["triage_status"] == "fit" and s["fit"] == 1
    assert store.track_set(row["track"]) == {LOCAL}
    assert row["resume_fit_score"] == 0.1


def test_fit_off_stamps_survivors_unscored(tmp_path, tracks, stubs, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "e", "Data Engineer", local_addr)

    _run(db, tracks, stubs, fit=False)

    row = _row(conn, "e")
    assert not stubs["score"]
    assert row["triage_status"] == "ok" and row["resume_fit_score"] is None
    assert store.crawl_seen(conn, "e")     # self-heal scores it later


# ── several tracks, one row ─────────────────────────────────────────────────

def test_row_surfaces_into_the_union_of_passing_tracks(tmp_path, tracks, stubs,
                                                       elsewhere, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Neuro Inc", mission_tier="core-mission",
                 mission_score=0.6, tags=tags.SWEEP)
    _harvested(conn, c, "far_eeg", "EEG Data Engineer", elsewhere)
    _harvested(conn, c, "near_eeg", "EEG Data Engineer", local_addr)
    _harvested(conn, c, "near_plain", "Data Engineer", local_addr)

    _run(db, tracks, stubs)

    far = _row(conn, "far_eeg")
    assert far["triage_status"] == "ok"
    assert store.track_set(far["track"]) == {SWEEP}
    assert far["triage_detail"] == f"{LOCAL}=geo;{SWEEP}=ok"
    assert store.track_set(_row(conn, "near_eeg")["track"]) == {LOCAL, SWEEP}
    near_plain = _row(conn, "near_plain")
    assert store.track_set(near_plain["track"]) == {LOCAL}
    assert near_plain["triage_detail"] == f"{LOCAL}=ok;{SWEEP}=anchor"


def test_tag_scoped_track_ignores_untagged_companies(tmp_path, tracks, stubs,
                                                     elsewhere):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Plain", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "far_eeg", "EEG Data Engineer", elsewhere)

    _run(db, tracks, stubs)

    row = _row(conn, "far_eeg")
    assert row["triage_status"] == "geo"
    assert row["triage_detail"] == f"{LOCAL}=geo"      # no sweep verdict


def test_keyword_focus_is_restored(tmp_path, tracks, stubs, local_addr, cfg):
    before = list(cfg.CORE_KEYWORDS)
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "e", "Data Engineer", local_addr)
    _run(db, tracks, stubs)
    assert cfg.CORE_KEYWORDS == before


# ── wiring ──────────────────────────────────────────────────────────────────

def test_harvest_pass_ends_with_triage(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _company(conn, "A")
    seen = []
    monkeypatch.setattr(triage, "run",
                        lambda **kw: seen.append(kw) or {"pending": 0})

    def fake_board(company, db_path, progress=lambda: None, hydrate=False):
        return {"err": None, "fetched": 1, "new": 1, "hydrated": 0,
                "closed": 0, "reopened": 0, "secs": 0.0}

    s = harvest.run(db_path=db, max_workers=1, board_fn=fake_board,
                    score_cap=7)
    assert s["triage"] == {"pending": 0}
    assert seen[0]["db_path"] == db and seen[0]["score_cap"] == 7
    s = harvest.run(db_path=db, max_workers=1, board_fn=fake_board,
                    triage=False)
    assert "triage" not in s and len(seen) == 1


def test_digest_counts_read_the_funnel(tmp_path, tracks, stubs, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "n", "Registered Nurse", local_addr)
    _harvested(conn, c, "e", "Data Engineer", local_addr)
    _run(db, tracks, stubs)
    assert store.triage_counts(conn) == {"ok": 1, "title": 1}
    assert store.triage_counts(conn, days=1) == {"ok": 1, "title": 1}
