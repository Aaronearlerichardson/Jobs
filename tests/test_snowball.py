"""Snowball extraction: mining third-party company names out of already-
stored job descriptions (discovery/snowball.py).

Fixture postings below are written in realistic biotech/health-tech
job-posting prose -- partnership/investor/acquisition language plus the
standard EEO/benefits/aggregator boilerplate every real posting carries --
so the precision assertions pin against the actual failure modes this
module exists to avoid (see discovery/local_sourcing.py's paste-ingestion
path, whose permissive parser let "Home"/"My Network"/"Create cover letter"
through), not toy strings.
"""

import discovery.snowball as sb
from core.store import connect, upsert_company, upsert_job

# --------------------------------------------------------------------------- #
#  Realistic fixture descriptions                                             #
# --------------------------------------------------------------------------- #

DESC_IRIS = """
Acme Health Analytics is partnered with Iris Diagnostics to bring genomic
screening technology to underserved rural clinics across the Southeast. This
role sits at the intersection of that partnership, building the data
pipelines that move results between our systems and theirs.

Acme Health Analytics is proud to be backed by Bessemer Ventures and Foo
Capital; our investors include Aldrich Health Partners. As a subsidiary of
MegaCorp Holdings, we draw on enterprise-grade compliance infrastructure
most startups can't access.

We offer competitive benefits including medical coverage through Aetna and
Cigna, a 401k managed by Fidelity, and short-term disability through The
Hartford. Acme Health Analytics is an Equal Opportunity Employer and does
not discriminate on the basis of race, color, religion, sex, national
origin, disability, or veteran status, in accordance with the
Americans with Disabilities Act. Apply on LinkedIn or through our
Greenhouse-powered careers page. The Company is growing fast and Our Team
would love to meet you!
"""

DESC_NOVA = """
Join our clinical data science team! Our clients include Bright Path
Therapeutics, Nova Biosciences, and Summit Diagnostics Inc, and we
collaborate closely with Duke University on several ongoing clinical
trials.

We are hiring a Senior Software Engineer to join The Team. Benefits
administered through UnitedHealthcare and Guardian. This posting was
generated for our Workday-powered application system; questions can be
directed via ZipRecruiter or Indeed. My Network wants to hear from you!
"""

DESC_MERGER = """
Acme Health Analytics was recently acquired by Global Health Systems,
extending our reach into international markets. We license our core
imaging algorithms to Iris Diagnostics under a multi-year agreement and
maintain a strategic alliance with Nova Biosciences on shared tooling.

Create cover letter and apply today! Home | About | Careers | Privacy
Policy. Acme Health Analytics values Affirmative Action and complies with
Title VII and the Family Medical Leave Act.
"""

# Two more postings from a DIFFERENT employer, so mentions of "Iris
# Diagnostics" and "Nova Biosciences" corroborate across companies too, not
# just repeats from Acme.
DESC_OTHER_CO_1 = """
Helix Bio is a fast-growing genomics startup working alongside Iris
Diagnostics on next-generation sequencing panels. We're a portfolio company
of Frontier Health Ventures.
"""

DESC_OTHER_CO_2 = """
At Helix Bio, our research partnerships with Nova Biosciences and Duke
University drive everything we build. Standard EEO language applies; see
our careers page powered by Lever.
"""

# A posting with NO description at all (NULL/empty), to make sure the
# harvest query's WHERE clause and the loop both tolerate it.
DESC_EMPTY = ""


def _seed_store(conn, descriptions, company="Acme Health Analytics",
                fit_scores=None):
    """Insert one company + N jobs (one per description) with distinct
    job_ids. `fit_scores` (parallel list) lets a test mark a posting as
    high-fit so the score-boost path is exercised."""
    cid = upsert_company(conn, {"name": company, "ats": "greenhouse",
                                "slug": company.lower().replace(" ", "-")})
    for i, desc in enumerate(descriptions):
        fit = (fit_scores[i] if fit_scores else None)
        upsert_job(conn, {
            "job_id": f"{company}_{i}", "company_id": cid,
            "company_name": company, "title": f"Role {i}",
            "url": f"https://example.com/{company}/{i}",
            "description": desc, "resume_fit_score": fit,
        })


# --------------------------------------------------------------------------- #
#  Precision: every hard negative must be rejected                            #
# --------------------------------------------------------------------------- #

# Names that MUST NEVER appear in the harvested output, grouped by why --
# each is a real failure mode named in the task, not a made-up edge case.
_HARD_NEGATIVES = {
    "employer's own name":        ["Acme Health Analytics", "Helix Bio"],
    "benefits/insurance providers": ["Aetna", "Cigna", "Fidelity",
                                     "The Hartford", "UnitedHealthcare",
                                     "Guardian"],
    "EEO/legal boilerplate":      ["Equal Opportunity Employer",
                                   "Americans with Disabilities Act",
                                   "Affirmative Action", "Title VII",
                                   "Family Medical Leave Act"],
    "job-board aggregators/ATS":  ["LinkedIn", "Greenhouse", "Workday",
                                   "ZipRecruiter", "Indeed", "Lever"],
    "generic self-referential":   ["The Company", "Our Team", "The Team",
                                   "My Network", "Home", "Create Cover Letter"],
}


def test_precision_rejects_every_hard_negative():
    conn = connect(":memory:")
    _seed_store(conn, [DESC_IRIS, DESC_NOVA, DESC_MERGER], "Acme Health Analytics")
    _seed_store(conn, [DESC_OTHER_CO_1, DESC_OTHER_CO_2], "Helix Bio")
    _seed_store(conn, [DESC_EMPTY], "Empty Desc Co")

    out = sb.harvest_from_store(conn, min_mentions=1)
    got_keys = {sb._norm_key(c["name"]) for c in out}

    failures = []
    for reason, names in _HARD_NEGATIVES.items():
        for n in names:
            if sb._norm_key(n) in got_keys:
                failures.append(f"{n!r} ({reason}) leaked into output")
    assert not failures, "\n".join(failures)


def test_precision_rejects_hard_negatives_at_extractor_level():
    """Lower-level pin: is_plausible_org() alone rejects every hard negative
    that is context-INDEPENDENT (benefits providers, EEO phrases,
    aggregators, generic self-referential filler) -- so a future change to
    the harvest loop can't accidentally let one back in without this test
    catching it at the source. "Employer's own name" is deliberately
    excluded here: is_plausible_org only knows about the ONE employer_key
    passed in, so a paste-ingestion-style "does this name-shaped word have
    ANY corroborating evidence" check belongs to the extraction regexes
    (which require a corp suffix or an intro phrase -- a bare word like
    "Home" or "My Network" never satisfies either, so it never becomes a
    candidate in the first place; see test_precision_rejects_every_hard_negative
    for that end-to-end guarantee)."""
    employer_key = sb._norm_key("Acme Health Analytics")
    context_independent = (
        _HARD_NEGATIVES["benefits/insurance providers"]
        + _HARD_NEGATIVES["EEO/legal boilerplate"]
        + _HARD_NEGATIVES["job-board aggregators/ATS"]
        + ["The Company", "Our Team", "The Team"]
    )
    survivors = [n for n in context_independent
                if sb.is_plausible_org(n, employer_key)]
    assert not survivors, f"is_plausible_org kept hard negatives: {survivors}"


# --------------------------------------------------------------------------- #
#  Recall: real third-party mentions are found and ranked sensibly            #
# --------------------------------------------------------------------------- #

_EXPECTED_HITS = {
    "Iris Diagnostics", "Bessemer Ventures", "Foo Capital",
    "Aldrich Health Partners", "MegaCorp Holdings", "Global Health Systems",
    "Bright Path Therapeutics", "Nova Biosciences", "Summit Diagnostics Inc",
    "Duke University", "Frontier Health Ventures",
}


def test_recall_finds_real_third_party_mentions():
    conn = connect(":memory:")
    _seed_store(conn, [DESC_IRIS, DESC_NOVA, DESC_MERGER], "Acme Health Analytics")
    _seed_store(conn, [DESC_OTHER_CO_1, DESC_OTHER_CO_2], "Helix Bio")

    out = sb.harvest_from_store(conn, min_mentions=1)
    got_keys = {sb._norm_key(c["name"]) for c in out}
    missing = [n for n in _EXPECTED_HITS if sb._norm_key(n) not in got_keys]
    assert not missing, f"expected third-party names not recovered: {missing}"
    # Recall as a fraction of the labeled positive set -- regression bar for
    # future filter tightening.
    found = len(_EXPECTED_HITS) - len(missing)
    assert found / len(_EXPECTED_HITS) >= 0.9


def test_corroborated_mentions_rank_above_singletons():
    """Iris Diagnostics is named in postings from BOTH employers (Acme +
    Helix); Bessemer Ventures appears in exactly one posting. Cross-company
    corroboration should score higher -- this is the ranking signal the task
    asks for ('how many distinct postings mention it')."""
    conn = connect(":memory:")
    _seed_store(conn, [DESC_IRIS, DESC_NOVA, DESC_MERGER], "Acme Health Analytics")
    _seed_store(conn, [DESC_OTHER_CO_1, DESC_OTHER_CO_2], "Helix Bio")

    out = sb.harvest_from_store(conn, min_mentions=1)
    by_key = {sb._norm_key(c["name"]): c for c in out}
    iris = by_key[sb._norm_key("Iris Diagnostics")]
    bessemer = by_key[sb._norm_key("Bessemer Ventures")]
    assert iris["postings"] >= 2
    assert bessemer["postings"] == 1
    assert iris["score"] > bessemer["score"]


def test_high_fit_posting_boosts_score():
    # Same candidate ("Iris Diagnostics"), one store with a high-fit posting
    # and one without, isolates the boost's effect.
    conn_hi = connect(":memory:")
    _seed_store(conn_hi, [DESC_IRIS], "Acme Health Analytics", fit_scores=[0.9])
    conn_lo = connect(":memory:")
    _seed_store(conn_lo, [DESC_IRIS], "Acme Health Analytics", fit_scores=[0.2])

    hi = {sb._norm_key(c["name"]): c for c in sb.harvest_from_store(conn_hi, min_mentions=1)}
    lo = {sb._norm_key(c["name"]): c for c in sb.harvest_from_store(conn_lo, min_mentions=1)}
    key = sb._norm_key("Iris Diagnostics")
    assert hi[key]["high_fit"] is True
    assert lo[key]["high_fit"] is False
    assert hi[key]["score"] > lo[key]["score"]


def test_min_mentions_drops_singletons():
    conn = connect(":memory:")
    _seed_store(conn, [DESC_IRIS], "Acme Health Analytics")  # Bessemer: 1 mention
    out_strict = sb.harvest_from_store(conn, min_mentions=2)
    out_loose = sb.harvest_from_store(conn, min_mentions=1)
    keys_strict = {sb._norm_key(c["name"]) for c in out_strict}
    keys_loose = {sb._norm_key(c["name"]) for c in out_loose}
    assert sb._norm_key("Bessemer Ventures") not in keys_strict
    assert sb._norm_key("Bessemer Ventures") in keys_loose


def test_excludes_names_already_in_companies_case_and_punctuation_insensitive():
    conn = connect(":memory:")
    _seed_store(conn, [DESC_IRIS, DESC_NOVA, DESC_MERGER], "Acme Health Analytics")
    # Already tracked under a different casing/punctuation spelling.
    upsert_company(conn, {"name": "iris-diagnostics!!", "ats": "lever",
                          "slug": "iris"})
    out = sb.harvest_from_store(conn, min_mentions=1)
    got_keys = {sb._norm_key(c["name"]) for c in out}
    assert sb._norm_key("Iris Diagnostics") not in got_keys
    # A different candidate from the same posting is untouched.
    assert sb._norm_key("Bessemer Ventures") in got_keys


def test_empty_and_null_descriptions_are_skipped_without_error():
    conn = connect(":memory:")
    _seed_store(conn, [DESC_EMPTY], "Nothing To See Co")
    upsert_job(conn, {"job_id": "explicit_null", "company_name": "Nothing To See Co",
                      "title": "Role", "url": "https://example.com/x",
                      "description": None})
    out = sb.harvest_from_store(conn, min_mentions=1)
    assert out == []


# --------------------------------------------------------------------------- #
#  CLI end-to-end                                                             #
# --------------------------------------------------------------------------- #

def test_run_snowball_end_to_end(tmp_path):
    """Exercises the callable CLI entry point against a real on-disk SQLite
    file (not :memory:), the same way `python discover.py --snowball` would
    connect. Confirms the report prints and the return value is usable."""
    db_path = tmp_path / "snowball_fixture.db"
    conn = connect(str(db_path))
    _seed_store(conn, [DESC_IRIS, DESC_NOVA, DESC_MERGER], "Acme Health Analytics")
    _seed_store(conn, [DESC_OTHER_CO_1, DESC_OTHER_CO_2], "Helix Bio")
    conn.close()

    out = sb.run_snowball(min_mentions=1, db_path=str(db_path))
    assert out, "expected at least one candidate from the fixture store"
    names = {c["name"] for c in out}
    assert any(sb._norm_key(n) == sb._norm_key("Iris Diagnostics") for n in names)
    # Ranked descending by score.
    scores = [c["score"] for c in out]
    assert scores == sorted(scores, reverse=True)


def test_cli_main_runs_against_fixture_db(tmp_path, monkeypatch, capsys):
    """The actual `python discover.py --snowball`-equivalent path: argv ->
    main() -> connect(config.STORE_DB_PATH). Points config.STORE_DB_PATH at
    the fixture so no real store is touched."""
    db_path = tmp_path / "cli_fixture.db"
    conn = connect(str(db_path))
    _seed_store(conn, [DESC_IRIS, DESC_MERGER], "Acme Health Analytics")
    conn.close()

    import config
    monkeypatch.setattr(config, "STORE_DB_PATH", str(db_path))
    monkeypatch.setattr("sys.argv", ["snowball.py", "--min-mentions", "1"])
    sb.main()
    captured = capsys.readouterr()
    assert "Snowball" in captured.out
    assert "Iris Diagnostics" in captured.out
