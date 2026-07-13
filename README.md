# Jobs Crawler

A job-search crawler built around one hard problem: **there aren't many BCI
jobs in North Carolina.** So it runs the same machinery in two postures —
relax the location, or relax the BCI constraint — and lets you pivot between
them:

| Track | Keeps | Relaxes | Command |
|---|---|---|---|
| **remote-neural** | neural signals (BCI/EEG/iEEG/ECoG/...), high technical bar, clinical mission | location → remote (US-eligible) | `python crawler.py --track remote-neural` |
| **local-tech** | Triangle/NC location (~2.5 h ring), technical bar, health/bio/science mission | neural requirement | `python crawler.py --track local-tech` |

Both tracks share the fetchers, discovery pipeline, company store, Claude
scorers, and parallel fetch pool; they differ only in gates and ranking.
A third, older mode (`python crawler.py` with no flags) runs the classic
keyword crawl and emails a digest.

```
DISCOVERY                    STORE (local_tech.db)            CRAWL
discover.py ..............>  companies                        crawler.py --track ...
  Claude suggestions           (ats, slug, mission score,       fetch boards (parallel)
  BCIWiki directory             tags: neural | nc_local)        -> gates (per track)
  NC sourcing / dorking                                         -> score (resume fit /
  page-capture leads          jobs                                 tech bar / remote)
capture.py ...............>    (dedup, per-track fields,       -> ranked digest
  browse LinkedIn yourself      fit scores)                        job_reports/*.md
```

---

## Setup

```
pip install -r requirements.txt
```

API keys as env vars (PowerShell, persistent):

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
[Environment]::SetEnvironmentVariable("GMAIL_APP_PASSWORD", "abcdefghijklmnop", "User")
# optional: $env:CLAUDE_MODEL = "claude-sonnet-4-6"
```

- **ANTHROPIC_API_KEY** powers the scorers (resume fit, technical bar,
  company mission) and Claude-driven discovery. Everything degrades
  gracefully without it (heuristic fallbacks, unscored missions).
- **GMAIL_APP_PASSWORD** is only needed for emailed digests: Google Account
  → Security → 2-Step Verification ON → "App passwords" → Mail/Windows →
  copy the 16-char password.
- `config.RESUME_PATH` points at your resume (.docx); it drives per-job
  fit scoring.

First run:

```
python crawler.py --import-seeds          # load config company lists -> store
python crawler.py --track local-tech      # or --track remote-neural
```

---

## The tracks

### remote-neural

Surfaces REMOTE, US-eligible roles that keep all three of: a neural-signal
anchor (word-boundary matched — "ecog" never fires inside "recognized"), a
technical title, and remote eligibility (structured ATS hints like Lever
`workplaceType` beat regex; hard negations like "on-site only" veto).
Sweeps priority companies (Beacon, Precision, Paradromics), then store
companies tagged `neural`, then config seed lists, forums, remote boards
(RemoteOK/Remotive/HN/RSS), and optional DDG web searches.

```
python crawler.py --track remote-neural                  # read-only preview
python crawler.py --track remote-neural --commit         # persist to store
python crawler.py --track remote-neural --send           # email the digest
python crawler.py --track remote-neural --fit            # resume-fit-rank matches
python crawler.py --track remote-neural --no-websearch   # skip flaky DDG
```

Preview prints a per-source funnel (RELV → NEUR → TECH → REMOTE → NEW) and
sample matches so you can sanity-check precision before anything emails.

### local-tech

Crawls every **active company in the store**, pulls their Triangle/NC
postings (company-vetted, so no keyword gate), drops clinical-ops and
defense roles, keeps technical titles, resume-fit-scores each new job in
parallel, and writes a digest ranked by fit (company mission as tiebreak).
Never emails.

```
python crawler.py --track local-tech [--top 20] [--workers 8]
```

Companies carry a cached mission score (BCI/neurotech = 1.0 bullseye →
healthcare-tech → health-bio-science → other), judged once per company
instead of once per job.

---

## Growing the company roster — `discover.py`

The store is the operational roster; `config.py`'s lists are reviewable
seeds. Ways to add companies:

| Command | What it does |
|---|---|
| `python discover.py "neurotech startups"` | Claude suggests employers; slugs probed against Greenhouse/Lever/Ashby/Kula/JazzHR/BambooHR/SmartRecruiters; careers pages sniffed; Workday resolved via headless-browser pool. Confirmed boards → report with VERIFY flags; `--apply` writes them into config.py. |
| `python discover.py --from-keywords` | Same, for every `INCLUDE_KEYWORDS` entry. |
| `python discover.py --from-bciwiki [--js]` | Resolve the BCIWiki company directory (~700 BCI companies) to crawlable boards. |
| `python discover.py --local` | NC sourcing pass: curated Triangle seeds + RTP directory + careers-page sniffing → NC-verified boards, mission-scored into the store (tag `nc_local`). |
| `python discover.py --dork` | ATS "dorking": mine search-indexed board URLs (`site:jobs.lever.co "Durham"`) into the store. |
| `python discover.py --resolve-leads` | Resolve page-capture company leads: slug probe → careers sniff → Workday probe → web-search fallback. Idempotent; reruns retry only unresolved leads. |
| `python discover.py --add-board "NC DHHS" URL` | You already know the board: paste its ATS or careers URL. Coordinates extracted (Workday triples parse straight from job URLs), NC-verified, activated. |
| `python crawler.py --import-seeds` | Config lists → store (`neural` for the BCI set, `nc_local` for the big RTP employers). |

Slug probing can confirm the wrong company ("seer" the proteomics shop vs
Seer Medical) — such hits carry `VERIFY:` notes through reports and config
comments. Eyeball them.

---

## Manual page capture — `capture.py`

For gated boards (LinkedIn, Indeed): **you** browse logged in as yourself;
the crawler just keeps what you saw. No automation touches your account.

```
python capture.py                 # capture server on http://127.0.0.1:8877/
python capture.py --watch         # or: watch ./captures for Ctrl+S saves
python capture.py page.html ...   # or: ingest saved files one-off
```

- **Userscript button** (needs Violentmonkey/Tampermonkey): open
  `http://127.0.0.1:8877/` once to install; a **➤ Jobs** button appears on
  LinkedIn/Indeed pages and sends the live DOM on click. (A plain
  bookmarklet can't work — LinkedIn's CSP blocks page-context calls to
  localhost.)
- **Watch mode** (zero installs): run `--watch`, then Ctrl+S → "Web Page,
  complete" into `captures/`. Ingested within ~2 s.

Captured pages are parsed in layers (LinkedIn's current obfuscated markup,
classic cards, guest cards, detail pages via the `<title>` tag + "About the
job" section; Indeed cards; JSON-LD; generic job links), gated, fit-scored,
and stored. Company names seen on captured pages become **leads** —
`python discover.py --resolve-leads` turns them into crawlable boards.

Notes: LinkedIn "Top job picks" collection pages are virtualized and save
almost empty — capture **Job tracker / search / detail** pages instead.
LinkedIn reshuffles markup periodically; expect to touch
`jobcrawler/page_capture.py` occasionally.

---

## Keyword & scoring utilities

```
python crawler.py --expand "eeg engineer"        # Claude: alt titles/keywords/sectors
python crawler.py --expand-live TERM             # ...folded into this run
python crawler.py --expand-location "NC"         # location synonym expansion
python crawler.py --keyword-report               # bulk-expand INCLUDE_KEYWORDS
python crawler.py --score "job description..."   # technical-bar score one posting
python crawler.py --db alt.db ...                # isolated store (concurrent runs)
python smoke_test.py                             # offline regression guard
```

Keyword filter design (learned the hard way, see `jobcrawler/filters.py` /
`config.py`): CORE terms pass alone; DOMAIN+SKILL must pair, and only in
the posting head (benefits boilerplate says "medical, dental, vision");
short acronyms are word-boundary matched; bare generic terms ("signal",
"data", "medical") are qualified — they leaked military RF and fintech
roles into a clinical search.

---

## Customizing

- **Keywords / excludes / locations:** `CORE_KEYWORDS`, `DOMAIN_KEYWORDS`,
  `SKILL_KEYWORDS`, `EXCLUDE_PHRASES` (+ title-only `EXCLUDE_TITLE_PHRASES`),
  `LOCATION_INCLUDE`/`LOCATION_EXCLUDE` in `config.py`.
- **Companies by hand:** add to the config lists (`"slug": "Name"` for
  Greenhouse/Lever/Ashby; tuples for Kula/ADP/Workday) and re-run
  `--import-seeds`, or skip config entirely with `--add-board`.
- **Track keyword focus** lives in `jobcrawler/tracks/*.py`
  (`apply_to_config` mutates the live lists in-process only — nothing on
  disk changes, so tracks can run back to back).

---

## Scheduling (Windows)

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
           -Argument "C:\Users\Jakda\git\Jobs\crawler.py --track local-tech" `
           -WorkingDirectory "C:\Users\Jakda\git\Jobs"
$trigger = New-ScheduledTaskTrigger -Daily -At "8:00AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable
Register-ScheduledTask -TaskName "Jobs Crawler" -Action $action -Trigger $trigger -Settings $settings -Force
```

(Or Task Scheduler GUI: daily trigger → program `python`, arguments
`crawler.py --track ...`, "Start in" = the repo folder.)

---

## Data model & files

One SQLite store, `local_tech.db` (`jobcrawler/store.py`):

- **companies** — name, ats, slug / Workday triple / careers_url, NC job
  count, cached mission tier + score, `tags` (`neural`, `nc_local` — which
  tracks crawl it), source (config_seed / local_sourcing / ats_dork /
  page_capture / manual), active flag.
- **jobs** — stable job_id (dedup), track, geo_mode, remote/neural signals,
  description, technical-bar + resume-fit scores, first/last seen.

Schema migrations are additive and automatic; old DBs upgrade in place.
Runtime artifacts (all gitignored): `local_tech.db`, `job_reports/*.md`,
`captures/`, `*.log`.

**One writer at a time:** SQLite locking does not span the boundary between
your shell and an agent sandbox mounting the same folder — running two
writers concurrently corrupts the DB (ask us how we know).

## Module map

| Module | Role |
|---|---|
| `crawler.py` / `discover.py` / `capture.py` | entry points: crawl, roster growth, manual capture |
| `jobcrawler/tracks/` | the two tracks (gates, ranking, digests) |
| `jobcrawler/sources.py` | declarative ATS registry: config lists ↔ fetch thunks ↔ store seeds |
| `jobcrawler/fetchers/` | keyword-gated board fetchers (10 ATSes + RSS/HN/RemoteOK/Remotive/DDG/JSON-LD/sitemap) |
| `jobcrawler/fetchers/company.py` | company-vetted, location-scoped pulls + lazy description hydration + custom-board scraper |
| `jobcrawler/discovery/` | pipeline, slug probes, shared careers-page sniffer, BCIWiki, NC sourcing, dorking |
| `jobcrawler/page_capture.py` | parse captured LinkedIn/Indeed/any-board HTML |
| `jobcrawler/store.py` | unified companies + jobs store |
| `jobcrawler/claude.py` | scorers + discovery/expansion prompts |
| `jobcrawler/filters.py` / `remote_filter.py` / `nc.py` | keyword tiers, remote eligibility, NC locality (single sources of truth) |
| `jobcrawler/parallel.py` | thread-pool source fetching (`CRAWLER_WORKERS`/`DISCOVERY_WORKERS` env) |
