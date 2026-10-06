"""The board directory import, offline.

The dataset is a tiny parquet directory built in tmp_path (the same layout
as the jobhive files) and `base_url` points at it. Fetching, the live board
read and the mission scorer are faked; the store is the in-memory one.
"""

import csv
import json

import duckdb
import pytest

import src.store as store
from conftest import answer, keep_store_open
from src import config
from src.runstate import per_run
from src.discovery import board_directory as bd
from src.discovery.resolve import directory as resolve_directory
from src.discovery import dork, local_sourcing

_JOB = "https://boards.greenhouse.io/{slug}/jobs/{n}"


def _parquet(path, rows, columns):
    """Write `rows` (tuples) as a parquet file with the named VARCHAR columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    cols = ", ".join(f"{c} VARCHAR" for c in columns)
    con.execute(f"CREATE TABLE t ({cols})")
    con.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in columns)})", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")
    con.close()


@pytest.fixture
def directory(tmp_path, monkeypatch, local_addr, elsewhere):
    """A directory with three boards on one platform: `bigco` (4 local
    postings, 3 of them passing the title gate), `smallco` (1, gate-passing)
    and `faraway` (none local), a lead-platform board, and a companies file."""
    cols = ("company", "url", "location", "title")
    gh = [("bigco", _JOB.format(slug="bigco", n=n), local_addr,
           "Engineer" if n < 3 else "Cashier") for n in range(4)]
    gh += [("smallco", _JOB.format(slug="smallco", n=1), local_addr, "Engineer"),
           ("faraway", _JOB.format(slug="faraway", n=1), elsewhere, "Engineer"),
           ("nolocation", _JOB.format(slug="nolocation", n=1), None, "Engineer")]
    _parquet(tmp_path / "greenhouse" / "jobs.parquet", gh, cols)
    _parquet(tmp_path / "gohire" / "jobs.parquet",
             [("acme", "https://acme.gohire.io/jobs/1", local_addr, "Engineer")], cols)
    _parquet(tmp_path / "companies.parquet",
             [("greenhouse", "Big Co Inc", "bigco", "https://job-boards.greenhouse.io/bigco"),
              ("greenhouse", "Precision Nutrition", "precisionnutrition",
               "https://job-boards.greenhouse.io/precisionnutrition")],
             ("ats", "name", "slug", "url"))
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"by_ats": {"greenhouse": {}, "gohire": {}}}), encoding="utf-8")
    monkeypatch.setattr(config, "BOARD_DIRECTORY", config.BOARD_DIRECTORY.model_copy(
        update={"base_url": str(tmp_path), "files": []}))
    # The title gate is the profile's; the test names its own.
    monkeypatch.setattr(bd, "is_technical_role", lambda title, track: "Engineer" in title)
    monkeypatch.setattr(bd, "exclude_reason", lambda *a, **k: None)
    monkeypatch.setattr(resolve_directory, "index", per_run(resolve_directory._load_companies))   # conftest stubs it
    return tmp_path


async def test_local_boards_are_grouped_named_and_ranked(directory):
    got = await bd.directory_boards()
    assert [(b["name"], b["nc_postings"], b["gate_passes"]) for b in got] == [
        ("Big Co Inc", 4, 3), ("Smallco", 1, 1)]
    assert got[0]["ats"] == "greenhouse" and got[0]["handle"] == "bigco"
    assert got[0]["sample_titles"] == ["Engineer"] * 3


async def test_a_platform_with_no_fetcher_is_counted_never_listed(directory):
    scan = await bd._scan()
    assert scan.leads == {"gohire": {"acme.gohire.io"}}
    assert all(b["ats"] != "gohire" for b in scan.boards.values())


async def test_configured_files_replace_the_manifest(directory, monkeypatch):
    monkeypatch.setattr(config, "BOARD_DIRECTORY",
                        config.BOARD_DIRECTORY.model_copy(update={"files": ["gohire"]}))
    assert await bd.directory_boards() == []


def test_lookup_name_matches_the_whole_name(directory):
    assert resolve_directory.lookup_name("Big Co Inc") == [
        ("greenhouse", "bigco", "https://job-boards.greenhouse.io/bigco")]
    assert resolve_directory.lookup_name("Precision Bio") == []      # not a suffix-stripped match


async def test_a_dry_run_reports_and_writes_nothing(directory, db, monkeypatch, capsys):
    keep_store_open(monkeypatch, db)
    counts = await bd.import_boards()
    assert (counts["new"], counts["added"]) == (2, 0)
    assert store.get_companies(db, active_only=False) == []
    assert "2 new" in capsys.readouterr().out
    (report,) = config.REPORT_DIR.glob("import_boards_*.csv")
    rows = list(csv.DictReader(report.open(encoding="utf-8")))
    assert list(rows[0]) == ["name", "ats", "slug", "nc_postings", "title_gate_passes",
                             "sample_titles", "sample_url", "status", "prescreen"]
    assert [r["name"] for r in rows] == ["Big Co Inc", "Smallco"]


async def test_the_roster_s_mission_tiers_rank_the_boards(directory, db, monkeypatch):
    keep_store_open(monkeypatch, db)
    for n, (tier, title) in enumerate([("core-mission", "Engineer")] * 3 + [("other", "Cashier")] * 3):
        cid = store.upsert_company(db, {"name": f"Known {n}", "mission_tier": tier})
        store.upsert_job(db, {"job_id": f"k{n}", "title": title, "company_id": cid})
    await bd.import_boards()
    (report,) = config.REPORT_DIR.glob("import_boards_*.csv")
    # Smallco has fewer gate passes, but its titles read like the core-mission ones.
    assert [r["name"] for r in csv.DictReader(report.open(encoding="utf-8"))] == [
        "Smallco", "Big Co Inc"]


async def test_apply_queues_the_best_boards_up_to_the_limit(directory, db, monkeypatch):
    keep_store_open(monkeypatch, db)
    monkeypatch.setattr(dork, "validate_board", answer((9, 3)))
    monkeypatch.setattr(local_sourcing, "_score_hit", answer(("adjacent", 0.5, "stub")))
    counts = await bd.import_boards(apply=True, limit=1)
    assert counts["added"] == 1
    (row,) = store.pending_companies(db)
    assert (row["name"], row["source"], row["ats"], row["slug"]) == (
        "Big Co Inc", "board_directory", "greenhouse", "bigco")


class TestIntake:
    """dork.intake_boards, the one intake of every board-first source."""

    @pytest.fixture(autouse=True)
    def _wire(self, db, monkeypatch):
        keep_store_open(monkeypatch, db)
        monkeypatch.setattr(local_sourcing, "_score_hit", answer(("adjacent", 0.5, "stub")))

    async def test_a_dorked_board_with_no_local_job_and_no_hq_is_skipped(self, monkeypatch, db):
        monkeypatch.setattr(dork.company_fetch, "fetch_company", answer([]))
        monkeypatch.setattr(dork, "nc_hq_signal", answer(False))
        assert await dork.harvest_urls(["https://jobs.lever.co/acmebio/1"], verbose=False) == (0, 1)
        assert store.get_companies(db, active_only=False) == []

    async def test_a_dorked_board_is_named_after_its_slug(self, monkeypatch, db):
        monkeypatch.setattr(dork.company_fetch, "fetch_company", answer([{}, {}]))
        assert await dork.harvest_urls(["https://jobs.lever.co/acme-bio/1"], verbose=False) == (1, 1)
        (row,) = store.pending_companies(db)
        assert (row["name"], row["source"], row["local_job_count"]) == (
            "Acme Bio", "ats_dork", 2)

    async def test_a_directory_board_needs_a_live_local_posting(self, monkeypatch, db):
        monkeypatch.setattr(dork, "validate_board", answer((5, 0)))
        monkeypatch.setattr(dork, "nc_hq_signal", answer(True))      # not consulted
        cand = {"name": "Acme Bio", "ats": "lever", "slug": "acmebio"}
        assert await dork.intake_boards([cand], "board_directory", require_live=True,
                                        verbose=False) == (0, 1)

    async def test_a_board_on_the_roster_is_not_read(self, monkeypatch, db):
        store.upsert_company(db, {"name": "Other Name", "ats": "lever", "slug": "acmebio"})
        read = []
        monkeypatch.setattr(dork, "validate_board", answer(lambda comp: read.append(comp)))
        cand = {"name": "Acme Bio", "ats": "lever", "slug": "acmebio"}
        assert await dork.intake_boards([cand], "board_directory", require_live=True,
                                        verbose=False) == (0, 1)
        assert read == []

    async def test_a_board_keyed_on_its_careers_url_is_taken(self, monkeypatch, db):
        monkeypatch.setattr(dork, "validate_board", answer((5, 2)))
        url = "https://careers.acme.org"
        cand = {"name": "Acme Bio", "ats": "custom", "slug": None, "careers_url": url}
        assert await dork.intake_boards([cand], "board_directory", require_live=True,
                                        verbose=False) == (1, 1)
        (row,) = store.pending_companies(db)
        assert row["careers_url"] == url
