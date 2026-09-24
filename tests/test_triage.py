"""Harvest triage (src/crawl/triage.py): the harvester's unscored rows go
through the crawl's gates cheapest-first, and only survivors pay for a
body fetch or a fit score. Offline: the mission scorer, the hydrator and
the fit scorer are all stubbed."""

import logging

import pytest

from conftest import company_row as _company, iso_days_ago, make_board_fn

from src import tags
from src import store
from src.claude.fit import MIN_DESC_CHARS, FitResult
from src.crawl import harvest, triage

LOCAL = "local-tech"
SWEEP = "remote-neural"


def _wd_company(conn, name="Wd"):
    """A Workday company, mission-scored so the location tests below never
    reach the mission scorer."""
    return _company(conn, name, ats="workday", wd_tenant=name.lower(),
                    wd_pod=5, wd_site="External",
                    mission_tier="core-mission", mission_score=0.9)


def _wd_url(jid):
    """A URL the workday spec's job_ref reads as a Workday job page (the
    company row's own wd_tenant/pod/site win over whatever sits in the
    URL; only the /job/<path> tail is read from it)."""
    return f"https://acme.wd5.myworkdayjobs.com/External/job/{jid}"


def _harvested(conn, c, jid, title, location, description="", url=None):
    store.upsert_job(conn, {"job_id": jid, "company_id": c["id"],
                            "company_name": c["name"], "title": title,
                            "url": f"https://x.test/j/{jid}" if url is None else url,
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

    def score(title, description="", *, location="", max_tokens=300):
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


# ── mission context ─────────────────────────────────────────────────────────

def test_mission_is_scored_on_titles_else_on_the_board_address(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    sent = []

    def scorer(name, context=""):
        sent.append(context)
        return "adjacent", 0.5, "stub"

    c = _company(conn, "Studycast", ats="rippling", slug="core-sound-imaging",
                 careers_url="https://ats.rippling.com/core-sound-imaging/jobs")
    triage.ensure_mission(conn, c, ["PACS Engineer", None, "Sales Lead"], scorer)
    assert sent == ["PACS Engineer | Sales Lead"]

    d = _company(conn, "Nameless", ats="bamboohr", slug="npi")
    triage.ensure_mission(conn, d, [], scorer)
    assert 'bamboohr "npi"' in sent[-1]


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


@pytest.fixture
def clinical_exclude_vocab(exclude_vocab):
    """gates.exclude_reason's clinical_titles/clinical_markers on the
    "t_local" track id the `tracks` fixture builds. conftest's
    exclude_vocab owns the lru_cache dance."""
    exclude_vocab("t_local", clinical_titles=["technologist", "technician"],
                  clinical_markers=["research"])


def test_clinical_service_title_excluded_before_hydration(
        tmp_path, tracks, stubs, local_addr, clinical_exclude_vocab):
    """Same contract as test_title_drop_costs_no_fetch_and_no_score, for
    the clinical_titles/clinical_markers vocabulary (src/match/gates.py,
    added 2026-09-18 for the Duke Health hospital-roster false positives):
    the exclude gate is free-gate phase, so a drop costs no fetch and no
    Claude call, and a title carrying a research/data/engineering marker
    is spared and scored as usual."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    local_on = [dict(tracks[0], exclude_gate=True), tracks[1]]
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "clin", "CT Technologist", local_addr)
    _harvested(conn, c, "rsrch", "Research Technician", local_addr)

    s = _run(db, local_on, stubs)

    assert stubs["hydrate"] == ["rsrch"]
    assert stubs["score"] == ["Research Technician"]
    clin = _row(conn, "clin")
    assert clin["triage_status"] == "exclude"
    assert clin["triage_detail"] == f"{LOCAL}=exclude"
    assert clin["track"] is None and clin["resume_fit_score"] is None
    assert store.crawl_seen(conn, "rsrch") and not store.crawl_seen(conn, "clin")
    assert s["exclude"] == 1


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
                        lambda title, desc="", *, watch_titles=False:
                        "pipelines" in desc)
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


def test_watching_a_multi_division_company_admits_its_remote_rows(
        tmp_path, tracks, stubs, local_addr, monkeypatch):
    """The two gates a conglomerate's remote row meets are independent, and
    only ONE of them answers to the watch tag.

    A multi-division employer scores "other" overall (that is what the
    [policy] multi_division exemption exists for), so it is never
    mission-trusted and its REMOTE rows are geo-dropped however relevant
    they are. The `watch` tag on the company row is what admits them --
    the same tag, and the same code path, as any other watched company.
    The division keyword gate is NOT lifted by it: that one is keyed off
    [policy] multi_division, and an off-division posting still drops. The
    watch tag only WIDENS its vocabulary, by the titles [policy]
    watch_division_titles names -- see
    test_a_watched_conglomerates_own_engineering_titles_pass_the_division_gate
    below, whose stub (unlike this one) honours the `watch_titles` flag.
    """
    from src import config
    monkeypatch.setattr(config, "is_multi_division",
                        lambda name: (name or "").lower().startswith("megacorp"))
    monkeypatch.setattr(triage, "is_relevant",
                        lambda title, desc="", *, watch_titles=False:
                        "pipelines" in desc)
    db = tmp_path / "s.db"
    conn = store.connect(db)
    plain = _company(conn, "Megacorp", mission_tier="other",
                     mission_score=0.25)
    watched = _company(conn, "Megacorp Watched", mission_tier="other",
                       mission_score=0.25, tags=tags.WATCH)
    _harvested(conn, plain, "prem", "Data Engineer", "Remote - US")
    _harvested(conn, watched, "wrem", "Data Engineer", "Remote - US")
    _harvested(conn, watched, "woff", "Data Engineer", "Remote - US",
               description="sells ad space " * 20)
    _harvested(conn, watched, "wloc", "Data Engineer", local_addr)

    _run(db, tracks, stubs)

    assert _row(conn, "prem")["triage_status"] == "geo"
    assert _row(conn, "wrem")["triage_status"] == "ok"
    assert _row(conn, "wrem")["remote_eligible"] == 1
    assert _row(conn, "wloc")["triage_status"] == "ok"
    assert _row(conn, "woff")["triage_status"] == "division", \
        "the watch tag admits the row's geography, not its division"


@pytest.fixture
def division_vocab(cfg, pristine_keywords, monkeypatch):
    """A conglomerate whose division gate runs on a KNOWN vocabulary.

    The two tests below exercise the REAL `filters.is_relevant` rather than
    a stub -- the rule itself is what they are about -- so the loaded
    profile must not be what decides the answer. The keyword tiers are
    narrowed to one unmistakable in-field term, the [exclude] title phrase
    that matters here is pinned, and [policy] watch_division_titles is set
    to a two-entry stand-in for the real list. `pristine_keywords` puts the
    profile's own lists back (and fails the test if it cannot).
    """
    for name, terms in (("CORE_KEYWORDS", ["electrophysiology"]),
                        ("DOMAIN_KEYWORDS", ["electrophysiology"]),
                        ("SKILL_KEYWORDS", ["electrophysiology"]),
                        ("INCLUDE_KEYWORDS", ["electrophysiology"]),
                        ("EXCLUDE_PHRASES", []),
                        ("EXCLUDE_TITLE_PHRASES", ["manager"])):
        getattr(cfg, name)[:] = terms
    monkeypatch.setattr(cfg, "WATCH_DIVISION_TITLES",
                        ("software engineer", "solutions architect"))
    monkeypatch.setattr(cfg, "is_multi_division",
                        lambda name: (name or "").lower().startswith("megacorp"))


#: A posting body from the division that is actually worth watching: plain
#: infrastructure engineering, carrying none of the profile's field
#: vocabulary. This is the text the division gate used to refuse.
_PLAIN_ENG_BODY = "kubernetes golang cluster scheduling telemetry " * 20


def test_a_watched_conglomerates_own_engineering_titles_pass_the_division_gate(
        tmp_path, tracks, stubs, local_addr, division_vocab):
    """[policy] watch_division_titles, the division gate's escape hatch.

    A conglomerate's postings are asked for the profile's health/bio
    vocabulary because a corporate mission score says nothing about the
    division that is hiring. At a WATCHED one that is the wrong question:
    the watch tag already says "show me this employer's technical roles",
    and the division worth watching is a plain engineering org. NVIDIA's
    "Software Engineer - AI Research Clusters" (Durham, NC) is the real
    case -- applied to off LinkedIn, dropped here.

    Widened, not lifted: a title the list does not name still drops, and
    the [exclude] gate inside is_relevant still runs first.
    """
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Megacorp Watched", mission_tier="other",
                 mission_score=0.25, tags=tags.WATCH)
    for jid, title in (("sw", "Senior Software Engineer, AI Research Clusters"),
                       ("sa", "Senior Solutions Architect, AI Factory"),
                       ("asic", "Senior ASIC Design Engineer"),
                       ("mgr", "Senior Manager, Software Engineering")):
        _harvested(conn, c, jid, title, local_addr,
                   description=_PLAIN_ENG_BODY)

    _run(db, tracks, stubs)

    assert _row(conn, "sw")["triage_status"] == "ok"
    assert _row(conn, "sa")["triage_status"] == "ok"
    assert _row(conn, "asic")["triage_status"] == "division", \
        "the silicon seats the list does not name are still refused"
    assert _row(conn, "mgr")["triage_status"] == "division", \
        "an [exclude] title phrase still beats watch_division_titles"


def test_an_unwatched_conglomerate_keeps_the_narrow_division_gate(
        tmp_path, tracks, stubs, local_addr, division_vocab):
    """The widening is keyed off the `watch` tag, not off multi_division:
    the same title at a conglomerate nobody watches drops exactly as it did
    before (SAS Institute, GRAIL, Labcorp, Google/Microsoft/Meta -- 2,114
    open rows, none of which moved in the 2026-09-22 differential)."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Megacorp", mission_tier="other", mission_score=0.25)
    _harvested(conn, c, "sw", "Senior Software Engineer, AI Research Clusters",
               local_addr, description=_PLAIN_ENG_BODY)
    _harvested(conn, c, "rel", "Senior Electrophysiology Engineer", local_addr,
               description=_PLAIN_ENG_BODY)

    _run(db, tracks, stubs)

    assert _row(conn, "sw")["triage_status"] == "division"
    assert _row(conn, "rel")["triage_status"] == "ok", \
        "the field vocabulary still admits an unwatched conglomerate's row"


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


def test_hydration_spends_the_board_budget_on_relevant_titles_first(
        tmp_path, tracks, stubs, local_addr, monkeypatch):
    """The per-host detail budget is finite, so the order it is spent in is
    the whole question. A multi-division board's off-lane rows (chip design
    at NVIDIA, 2026-09-18) pass the title gate and can only be refused by
    the division gate, which needs a body -- so they were consuming the
    budget ahead of rows that would surface. Relevance is consulted through
    the module's own `is_relevant`, stubbed here so the assertion does not
    depend on which keywords the loaded profile configures."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    for jid, title in (("chip", "Mask Design Engineer"),
                       ("ctrl", "Memory Controller Verification Engineer"),
                       ("eng", "Data Engineer")):
        _harvested(conn, c, jid, title, local_addr)
    monkeypatch.setattr(triage, "is_relevant",
                        lambda title, desc="": "data" in (title or "").lower())
    order = []

    def hydrate(company, jobs, **kw):
        order.extend(j["id"] for j in jobs)
        for j in jobs:
            j["_tried"] = True
            j["description"] = "python sql pipelines " * 20
        return {"hydrated": len(jobs), "unhydrated": 0}

    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
               hydrate_fn=hydrate, max_workers=2)

    assert order[0] == "eng", f"hydrated in arrival order: {order}"


def test_a_decided_row_still_outranks_a_relevant_undecided_one():
    """Relevance orders WITHIN the existing decided-first rule, it does not
    replace it: a row some track has already passed is still hydrated
    before one no gate could rule on."""
    survivors = {"ok_row": (None, None, triage.OK),
                 "defer_row": (None, None, triage.DEFER)}
    key = triage._hydrate_order(survivors)
    batch = [{"id": "defer_row", "title": "Data Engineer"},
             {"id": "ok_row", "title": "Mask Design Engineer"}]
    assert [j["id"] for j in sorted(batch, key=key)] == ["ok_row", "defer_row"]


# ── observability: a dropped/scored/waiting row is nameable, not just counted ──

def test_drop_logs_one_debug_record_per_dropped_row(tmp_path, tracks, stubs,
                                                     local_addr, caplog):
    """_write_verdicts is the one place that sees every verdict, so it is
    also the one place that must log every drop -- the 2026-09-16 audit
    found no record naming a dropped row anywhere in a week of logs."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "nurse", "Registered Nurse", local_addr)

    with caplog.at_level(logging.DEBUG, logger="src.crawl.triage"):
        _run(db, tracks, stubs)

    assert (f"drop title | Acme | Registered Nurse | {local_addr} | "
            f"{LOCAL}=title") in caplog.messages


def test_score_line_printed_per_scored_row(tmp_path, tracks, stubs, local_addr,
                                           capsys):
    """Every row the scorer actually returns a number for gets one printed
    line, labeled "surfaced" (not "ok" plus a redundant [SURFACED] tag)
    only when it clears the digest floor -- the audit's other gap: the
    `claude` DEBUG line carried token counts only, never the score or
    which row it was for."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "eng", "Data Engineer", local_addr)
    _harvested(conn, c, "weak", "Weak Data Engineer", local_addr)

    _run(db, tracks, stubs)

    out = capsys.readouterr().out
    assert (f"score 0.70 surfaced | Acme | Data Engineer | {local_addr} | "
            "stub\n") in out
    assert (f"score 0.10 fit | Acme | Weak Data Engineer | {local_addr} | "
            "stub\n") in out
    assert "SURFACED" not in out


def test_waiting_reason_names_a_failed_fetch_then_the_retry_window(
        tmp_path, tracks, stubs, local_addr, capsys):
    """The row this whole feature was written for: a survivor that fails to
    hydrate names ITSELF and why, instead of the funnel's bare "N still
    waiting on a body" (see the 2026-09-13 18:42 pass in
    data/logs/session-20260913-184219-harvest.log, which never even printed
    a "hydrating" line for its one stuck row)."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "nobody", "Data Engineer", local_addr)

    _run(db, tracks, stubs)
    out = capsys.readouterr().out
    assert (f"waiting: Acme | Data Engineer | {local_addr} | "
            "fetch failed this pass") in out

    # Retried immediately, the fresh desc_checked_at excludes it again --
    # by design (RETRY_DAYS), and now the reason says so explicitly.
    _run(db, tracks, stubs)
    out2 = capsys.readouterr().out
    assert "waiting: Acme | Data Engineer" in out2 and "retries after" in out2


def test_waiting_reason_names_a_row_with_no_url(tmp_path, tracks, stubs,
                                                local_addr, capsys):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "nourl", "Data Engineer", local_addr, url="")

    _run(db, tracks, stubs)

    out = capsys.readouterr().out
    assert (f"waiting: Acme | Data Engineer | {local_addr} | "
            "no URL to fetch a body from") in out
    assert "nourl" not in stubs["hydrate"]


def test_waiting_list_is_capped(tmp_path, tracks, stubs, local_addr, capsys):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    n = triage._WAITING_CAP + 5
    for i in range(n):
        c = _company(conn, f"Co{i}", mission_tier="core-mission",
                     mission_score=0.9)
        _harvested(conn, c, f"j{i}", "Data Engineer", local_addr, url="")

    _run(db, tracks, stubs)

    out = capsys.readouterr().out
    assert out.count("waiting: Co") == triage._WAITING_CAP
    assert "... and 5 more" in out


def test_skip_score_short_body_counted_in_summary(tmp_path, tracks, stubs,
                                                   local_addr, monkeypatch,
                                                   capsys):
    """A row that reaches the scorer with a body under fit.MIN_DESC_CHARS is
    refused (SKIP-SCORE), which used to vanish into "left" indistinguishably
    from a row still waiting on a body. It now has its own summary count --
    and, since the row still surfaces (unscored) into its track, that count
    is a QUALIFIER on `surfaced`, not a second, overlapping tally: one row,
    counted once ("1 surfaced (1 unscored: short body)"), not two."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "stub", "Data Engineer", local_addr,
              description="too short")

    def score(title, description="", *, location="", max_tokens=300):
        if len(description) < MIN_DESC_CHARS:
            return FitResult(score=None, reason="no description; unscored")
        return FitResult(score=0.7, reason="stub")
    monkeypatch.setattr(triage, "score_resume_fit", score)

    s = _run(db, tracks, stubs)

    assert s["skip_score"] == 1 and s["surfaced"] == 1
    row = _row(conn, "stub")
    assert row["triage_status"] == "ok" and row["resume_fit_score"] is None
    out = capsys.readouterr().out
    assert "1 surfaced (1 unscored: short body)" in out


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

def test_harvest_pass_ends_with_triage_then_digests(tmp_path, monkeypatch):
    """A harvest pass can move rows into a track's ranking with no crawl
    ever running, so after triage it rewrites every roster track's digest
    -- from the SAME store the pass wrote -- whether or not any board was
    due (harvest._triage)."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _company(conn, "A")
    seen, order = [], []
    monkeypatch.setattr(triage, "run", lambda **kw: seen.append(kw)
                        or order.append("triage") or {"pending": 0})
    monkeypatch.setattr(triage, "roster_tracks",
                        lambda: [{"track": LOCAL}, {"track": SWEEP}])
    monkeypatch.setattr(harvest, "rewrite_digest",
                        lambda conn, t, **kw: order.append(t["track"]))
    fake_board = make_board_fn(fetched=1, new=1)

    s = harvest.run(db_path=db, max_workers=1, board_fn=fake_board,
                    score_cap=7)
    assert s["triage"] == {"pending": 0}
    assert seen[0]["db_path"] == db and seen[0]["score_cap"] == 7
    assert order == ["triage", LOCAL, SWEEP]
    s = harvest.run(db_path=db, max_workers=1, board_fn=fake_board,
                    triage=False)
    assert "triage" not in s and len(seen) == 1 and len(order) == 3
    s = harvest.run(db_path=tmp_path / "empty.db")      # no board due
    assert s["triage"] == {"pending": 0}
    assert order[3:] == ["triage", LOCAL, SWEEP]


def test_digest_counts_read_the_funnel(tmp_path, tracks, stubs, local_addr):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    _harvested(conn, c, "n", "Registered Nurse", local_addr)
    _harvested(conn, c, "e", "Data Engineer", local_addr)
    _run(db, tracks, stubs)
    assert store.triage_counts(conn) == {"ok": 1, "title": 1}
    assert store.triage_counts(conn, days=1) == {"ok": 1, "title": 1}


# ── Workday location resolution (unknown-location survivors) ───────────────
#
# Workday's "N Locations" listing placeholder (location_unknown) is not a
# place -- the detail JSON is what names the real list, so the geo gate
# must wait for it rather than falling straight to the strict body regex.

def test_bodiless_workday_n_locations_resolves_before_geo_gate(
        tmp_path, tracks, stubs, local_addr, elsewhere):
    """The listing's placeholder defers; the bodiless hydrate call (the
    same one that fetches the body) names the real place, and THAT is
    what the geo gate judges -- local passes, elsewhere drops as geo."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _wd_company(conn)
    _harvested(conn, c, "near", "Data Engineer", "2 Locations",
               url=_wd_url("near"))
    _harvested(conn, c, "far", "Data Engineer", "2 Locations",
               url=_wd_url("far"))

    def hydrate(company, jobs, **kw):
        for j in jobs:
            j["_tried"] = True
            j["description"] = "python sql pipelines " * 20
            j["location"] = local_addr if j["id"] == "near" else elsewhere
        return {"hydrated": len(jobs), "unhydrated": 0}

    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
              hydrate_fn=hydrate, max_workers=2)

    near, far = _row(conn, "near"), _row(conn, "far")
    assert near["triage_status"] == "ok" and near["location"] == local_addr
    assert far["triage_status"] == "geo" and far["location"] == elsewhere


def test_bodied_workday_n_locations_resolves_via_cached_location_lookup(
        tmp_path, tracks, stubs, local_addr):
    """A Workday row that already has a body but still carries the "N
    Locations" placeholder gets ONLY its location refreshed -- no body
    refetch (needs_detail's second clause, the engine's cached location
    read)."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _wd_company(conn)
    _harvested(conn, c, "j", "Data Engineer", "2 Locations",
               description="python sql pipelines " * 20, url=_wd_url("j"))

    def hydrate(company, jobs, **kw):
        for j in jobs:
            j["_tried"] = True
            j["location"] = local_addr        # simulates a resolved lookup
        return {"hydrated": 1, "unhydrated": 0}

    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
              hydrate_fn=hydrate, max_workers=2)

    row = _row(conn, "j")
    assert row["triage_status"] == "ok" and row["location"] == local_addr


def test_bodied_workday_location_lookup_failure_defers_then_expires(
        tmp_path, tracks, stubs):
    """A cached location lookup that resolves nothing defers the row (a
    waiting reason, triage_status stays NULL) and is not retried inside
    RETRY_DAYS; only once that window has passed does the geo gate give
    up on the lookup and let today's strict body rule decide."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _wd_company(conn)
    _harvested(conn, c, "j", "Data Engineer", "2 Locations",
               description="python sql pipelines " * 20, url=_wd_url("j"))
    calls = []

    def hydrate_fails(company, jobs, **kw):
        for j in jobs:
            calls.append(j["id"])
            j["_tried"] = True          # the lookup ran; it resolved nothing
        return {"hydrated": 0, "unhydrated": len(jobs)}

    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
              hydrate_fn=hydrate_fails, max_workers=2)
    assert calls == ["j"]
    row = _row(conn, "j")
    assert row["triage_status"] is None and row["desc_checked_at"]

    # Inside the retry window: no second lookup attempt.
    calls.clear()
    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
              hydrate_fn=hydrate_fails, max_workers=2)
    assert calls == [] and _row(conn, "j")["triage_status"] is None

    # The last attempt is now older than RETRY_DAYS: the geo gate stops
    # waiting and decides on the body already stored -- no further lookup.
    old = iso_days_ago(triage.RETRY_DAYS + 1)
    conn.execute("UPDATE jobs SET desc_checked_at=? WHERE job_id='j'", (old,))
    conn.commit()
    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
              hydrate_fn=hydrate_fails, max_workers=2)
    assert calls == []
    assert _row(conn, "j")["triage_status"] == "geo"


# ── re-queue (rows an earlier rule mis-judged) ──────────────────────────────

def test_requeue_reports_both_reasons_and_touches_nothing_by_default(
        tmp_path, tracks, stubs, elsewhere):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    wd = _wd_company(conn)
    _harvested(conn, wd, "unk", "Data Engineer", "2 Locations",
               url=_wd_url("unk"))
    store.record_triage(conn, "unk", "geo", f"{LOCAL}=geo")
    # The same placeholder on a board with no location lookup: re-judging
    # it would reach the same body-rule verdict, so it is not selected.
    _harvested(conn, c, "unk2", "Data Engineer", "2 Locations")
    store.record_triage(conn, "unk2", "geo", f"{LOCAL}=geo")
    _harvested(conn, c, "far", "Data Engineer", elsewhere,
               description="an onsite engineering role")
    store.record_triage(conn, "far", "ok", f"{LOCAL}=ok", tracks=[LOCAL],
                        scores={"resume_fit_score": 0.7})
    # A row the CRAWL adopted directly: same location, but no triage_status
    # at all, so requeue must never touch it.
    _harvested(conn, c, "adopted", "Data Engineer", elsewhere)
    store.upsert_job(conn, {"job_id": "adopted", "track": LOCAL})

    # Passed the non-geo track too: its labels stay.
    _harvested(conn, c, "both", "Data Engineer", elsewhere)
    store.record_triage(conn, "both", "ok", f"{LOCAL}=ok;{SWEEP}=ok",
                        tracks=[LOCAL, SWEEP])

    s = triage.requeue_rows(db_path=db, tracks=tracks)

    assert s["counts"] == {"geo:unknown-location": 1, "geo:non-local": 1}
    assert _row(conn, "both")["triage_status"] == "ok"
    assert s["requeued"] == 0
    assert _row(conn, "unk")["triage_status"] == "geo"
    far_row = _row(conn, "far")
    assert far_row["triage_status"] == "ok" and far_row["track"] == LOCAL
    assert _row(conn, "adopted")["triage_status"] is None


def test_requeue_apply_clears_track_score_and_triage_fields_then_rejudges(
        tmp_path, tracks, stubs, elsewhere):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    c = _company(conn, "Acme", mission_tier="core-mission", mission_score=0.9)
    wd = _wd_company(conn)
    _harvested(conn, wd, "unk", "Data Engineer", "2 Locations",
               description="python sql pipelines " * 20, url=_wd_url("unk"))
    store.record_triage(conn, "unk", "geo", f"{LOCAL}=geo")
    _harvested(conn, c, "far", "Data Engineer", elsewhere,
               description="an onsite engineering role")
    store.record_triage(conn, "far", "ok", f"{LOCAL}=ok", tracks=[LOCAL],
                        scores={"resume_fit_score": 0.7, "fit_reason": "stub"})

    s = triage.requeue_rows(db_path=db, apply=True, tracks=tracks)

    assert s["requeued"] == 2
    for jid in ("unk", "far"):
        row = _row(conn, jid)
        assert row["triage_status"] is None and row["track"] is None
        assert row["resume_fit_score"] is None and row["fit_reason"] is None
        assert row["description"], "the body already fetched is kept"
    pending = {r["job_id"] for r in store.triage_pending(conn)}
    assert {"unk", "far"} <= pending

    # A later plain pass is what re-judges it -- requeue_rows never does.
    # The Workday row now waits on its location lookup (the stub resolves
    # none) instead of being dropped by the body rule at once.
    triage.run(db_path=db, tracks=tracks, mission_scorer=stubs["mission_fn"],
              hydrate_fn=stubs["hydrate_fn"], max_workers=2)
    assert stubs["hydrate"] == ["unk"]
    unk = _row(conn, "unk")
    assert unk["triage_status"] is None and unk["desc_checked_at"]
    assert _row(conn, "far")["triage_status"] == "geo"
