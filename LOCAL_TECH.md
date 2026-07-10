# LOCAL-TECH track (`crawler.py --track local-tech`)

Surfaces **local (Triangle / NC)** technical roles at **health / bio / science**
employers, ranked by **résumé fit**, with each company carrying a cached
**company-fit** (mission) score anchored on the candidate's bullseye
(BCI / neurotech = 1.0). The location-relaxed twin of this track is
`--track remote-neural`; both are pivots of the same shared machinery
(see the unified-architecture section of `setup.md`).

## Flow

```
DISCOVERY ─► companies table ─► CRAWL ─► jobs table ─► ranked digest
(probe/sniff/dork/seeds)   (--track local-tech)     (résumé-fit desc)
```

1. **Discovery** finds companies, resolves their job board, NC-verifies, and
   mission-scores → writes the `companies` table (`discover.py --local`,
   `discover.py --dork`, `crawler.py --import-seeds`).
2. **Crawl** (`python crawler.py --track local-tech`) reads *active* companies,
   fetches their NC jobs **in parallel**, applies exclude + technical-title
   filters, résumé-fit-scores each new job in parallel, writes the `jobs`
   table, and prints/writes a ranked digest. Never emails.

## Data model — unified store (`jobcrawler/store.py`, `local_tech.db`)

- **companies**: name, ats, slug / workday triple, careers_url, hq_location,
  nc_job_count, mission_tier, **mission_score** (0–1 company-fit, cached once),
  **tags** (scope: `neural` / `nc_local`), source, active.
- **jobs**: company_id, title, url, location, **track**, geo_mode, description,
  **resume_fit_score**, fit_reason, remote/neural signal fields (used by the
  remote-neural track), first/last seen.

## Modules

| Module | Role |
|---|---|
| `jobcrawler/nc.py` | **single source of truth** for NC locality (`is_nc`, `NC_RE`, `NC_HQ_RE`) |
| `jobcrawler/store.py` | unified companies + jobs SQL layer (all tracks) |
| `jobcrawler/fetchers/company.py` | company-vetted, location-scoped fetchers per ATS (gh/lever/ashby/workday/smartrecruiters/icims/successfactors/peopleadmin/**custom**) + lazy description hydration |
| `jobcrawler/tracks/local_tech.py` | this track: gates, scorer glue, crawl driver, `ingest_external_jobs()` |
| `jobcrawler/resume.py` | résumé text extraction (.docx) |
| `jobcrawler/claude.py` | scorers: `score_company_mission` (BCI=1.0 anchor), `score_resume_fit`, `score_technical_bar` |
| `jobcrawler/discovery/probes.py` | ATS slug probes (gh/lever/ashby/kula/jazzhr/bamboohr/smartrecruiters) + Workday JS probe pool |
| `jobcrawler/discovery/sniffer.py` | shared careers-page ATS sniffer (static + JS + custom-board), multi-TLD, leads |
| `jobcrawler/discovery/local_sourcing.py` | seed + probe + NC-verify + mission-score + populate |
| `jobcrawler/discovery/ats_dork.py` | search-engine ATS "dorking" → board slugs |

## Scoring

- **Company-fit (mission)**: 1.0 = BCI / neural-interface / neurotech (bullseye,
  deterministic anchor); 0.85–0.98 = other neuro / medical-device / health ML-AI /
  diagnostics; 0.6–0.85 = biotech/pharma R&D; 0.4–0.65 = pharma manufacturing /
  CRO ops; ≤0.2 = non-health.
- **Résumé fit** (per job): LLM scores the job description against the résumé.
  (Also available to the remote-neural track via `--fit`.)

## Known limitations

- **iCIMS / SuccessFactors** boards are JS-gated / company-specific; fetch is
  unreliable (the sniffer surfaces them; treat results as best-effort).
- **NC-verify** requires *current* NC openings to auto-activate a company (except
  `nc_hq_signal`-confirmed local companies, tracked at 0 openings).
- **Indeed** ingestion is agent-mediated (`tracks/local_tech.ingest_external_jobs`;
  the standalone crawler can't call the MCP).

## Test / run

```
python smoke_test.py                    # offline regression guard
python crawler.py --import-seeds        # config seed lists -> store
python crawler.py --track local-tech    # live crawl (needs ANTHROPIC_API_KEY)
```
