# LOCAL-TECH pipeline (`track-local-clinical-ml`)

Surfaces **local (Triangle / NC)** technical roles at **health / bio / science**
employers, ranked by **résumé fit**, with each company carrying a cached
**company-fit** (mission) score anchored on the candidate's bullseye
(BCI / neurotech = 1.0). See [user profile + priorities in memory].

## Flow

```
DISCOVERY ─► companies table ─► CRAWL ─► jobs table ─► ranked digest
(probe/sniff/dork/Indeed)      (--local-tech)         (résumé-fit desc)
```

1. **Discovery** finds companies and resolves their job board, NC-verifies, and
   scores mission → writes the `companies` table.
2. **Crawl** (`python crawler.py --local-tech`) reads *active* companies, fetches
   their NC jobs, applies exclude + technical-title filters, résumé-fit-scores
   each new job in parallel, writes the `jobs` table, and prints/writes a ranked
   digest. Never emails.

## Data model — `local_tech.db` (`jobcrawler/store.py`)

- **companies**: name, ats, slug / workday triple, careers_url, hq_location,
  nc_job_count, mission_tier, **mission_score** (0–1 company-fit, cached once),
  source, active.
- **jobs**: company_id, title, url, location, geo_mode, description,
  **resume_fit_score**, fit_reason, first/last seen.

## Modules

| Module | Role |
|---|---|
| `jobcrawler/nc.py` | **single source of truth** for NC locality (`is_nc`, `NC_HQ_RE`) |
| `jobcrawler/store.py` | companies + jobs SQL layer |
| `jobcrawler/local_fetch.py` | NC-only fetchers per ATS (gh/lever/ashby/workday/smartrecruiters/icims/successfactors/peopleadmin/**custom**) |
| `jobcrawler/local_tech.py` | crawl driver + `ingest_external_jobs()` (Indeed) |
| `jobcrawler/resume.py` | résumé text extraction (.docx) |
| `jobcrawler/claude.py` | scorers: `score_company_mission` (BCI=1.0 anchor), `score_resume_fit` |
| `jobcrawler/discovery/probes.py` | ATS slug probes (gh/lever/ashby/workday/smartrecruiters) |
| `jobcrawler/discovery/sniffer.py` | careers-page ATS sniffer (static + JS + custom-board), multi-TLD |
| `jobcrawler/discovery/local_sourcing.py` | seed + probe + NC-verify + mission-score + populate |
| `jobcrawler/discovery/ats_dork.py` | search-engine ATS "dorking" → board slugs |
| `jobcrawler/local_clinical.py` | **legacy** `--local-clinical` pipeline; also exports shared `exclude_reason` / `geo_mode` / `_broaden_relevance` used by local_tech |

## Scoring

- **Company-fit (mission)**: 1.0 = BCI / neural-interface / neurotech (bullseye,
  deterministic anchor); 0.85–0.98 = other neuro / medical-device / health ML-AI /
  diagnostics; 0.6–0.85 = biotech/pharma R&D; 0.4–0.65 = pharma manufacturing /
  CRO ops; ≤0.2 = non-health.
- **Résumé fit** (per job): LLM scores the job description against the résumé.

## Known limitations

- **Custom-board scraper** (`fetch_custom_careers_nc`) is tuned to Science's DOM;
  it does not follow `careers → openings` or handle arbitrary structures — the
  open sub-project.
- **iCIMS / SuccessFactors** boards are JS-gated / company-specific; fetch is
  unreliable.
- **NC-verify** requires *current* NC openings to auto-activate a company (except
  curated `nc_hq_signal`-confirmed local companies, tracked at 0 openings).
- **Indeed** ingestion is agent-mediated (the standalone crawler can't call the
  MCP).

## Test / run

```
python smoke_test.py            # offline regression guard
python crawler.py --local-tech  # live crawl (needs ANTHROPIC_API_KEY)
```

**Merge constraint:** shared files (`discover.py`, `orchestrator.py`, `report.py`,
`fetchers/`, `filters.py`, `discovery/{apply,pipeline,seeds}.py`) belong to the
sibling `track-remote-neural` branch — do not delete/rewrite them.
