# Jobs Crawler

A configurable job-search crawler. **Your entire search — what you want, where,
and who you are — lives in one `profile.toml`; the code stays generic.** It
ships with an example profile for a genuinely hard case: *there aren't many BCI
jobs in North Carolina.* So it runs the same machinery in two postures — relax
the location, or relax the BCI constraint — and lets you pivot between them:

| Track | Keeps | Relaxes | Command |
|---|---|---|---|
| **remote-neural** | neural signals (BCI/EEG/iEEG/ECoG/...), high technical bar, clinical mission | location → remote (US-eligible) | `python crawler.py --track remote-neural` |
| **local-tech** | Triangle/NC location (~2.5 h ring), technical bar, health/bio/science mission | neural requirement | `python crawler.py --track local-tech` |

Those specifics are just the shipped profile — swap `profile.toml` and the
tracks retarget any field/region. Both tracks share the fetchers, discovery
pipeline, company store, Claude scorers, and parallel fetch pool; they differ
only in gates and ranking. A third, older mode (`python crawler.py` with no
flags) runs the classic keyword crawl and emails a digest.

```
DISCOVERY                    STORE (local_tech.db)            CRAWL
discover.py ..............>  companies                        crawler.py --track ...
  Claude suggestions           (ats, slug, mission score,       fetch boards (parallel)
  BCIWiki directory             tags: neural | nc_local,        -> gates (per track)
  local sourcing / dorking      active flag)                    -> score (resume fit /
  page-capture leads          jobs                                 tech bar / remote)
capture.py ...............>    (dedup, per-track fields,       -> ranked digest
  browse gated sites yourself   fit scores)                        job_reports/*.md
```

---

## Setup

```
pip install -r requirements.txt
```

**1. Your search profile.** Copy the template and edit it — this is the only
file that holds your criteria:

```
cp profile.example.toml profile.toml
```

`profile.toml` (gitignored) holds your keywords, locations, candidate identity,
mission tiers, locality, and discovery seeds. `config.py` is now just plumbing
(paths, HTTP headers, source toggles, secrets). See **[Customizing](#customizing--profiletoml)**.

**2. API keys** as env vars (PowerShell, persistent):

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
[Environment]::SetEnvironmentVariable("GMAIL_APP_PASSWORD", "abcdefghijklmnop", "User")
# optional feeds/models:
# [Environment]::SetEnvironmentVariable("CAREERONESTOP_USER_ID", "...", "User")
# [Environment]::SetEnvironmentVariable("CAREERONESTOP_TOKEN",   "...", "User")
```

- **ANTHROPIC_API_KEY** powers the scorers (resume fit, technical bar, company
  mission) and Claude-driven discovery. Everything degrades gracefully without
  it (heuristic fallbacks, unscored missions).
- **GMAIL_APP_PASSWORD** is only needed for emailed digests (Google Account →
  Security → 2-Step Verification → "App passwords" → Mail).
- **CAREERONESTOP_*** (optional) unlock the NLx feed for gated federal
  contractors — see [Gated employers](#gated--big-company-employers).
- `config.RESUME_PATH` points at your resume (.docx); it drives per-job fit
  scoring.

**3. First run:**

```
python discover.py --local                # source local companies into the store
python discover.py --score-missions       # tier the new companies
python crawler.py --track local-tech      # or --track remote-neural
```

(There is no more `--import-seeds` — the company roster is stored in the DB, not
in `config.py`. Bootstrap it with discovery, or `--import-companies`.)

---

## The tracks

### remote-neural

Surfaces REMOTE, US-eligible roles that keep all three of: a neural-signal
anchor (word-boundary matched — "ecog" never fires inside "recognized"), a
technical title, and remote eligibility (structured ATS hints like Lever
`workplaceType` beat regex; hard negations like "on-site only" veto). Sweeps
priority companies, then store companies tagged `neural`, then forums and
remote boards (RemoteOK/Remotive/HN/RSS), and optional DDG web searches.

```
python crawler.py --track remote-neural                  # read-only preview
python crawler.py --track remote-neural --commit         # persist to store
python crawler.py --track remote-neural --send           # email the digest
python crawler.py --track remote-neural --fit            # resume-fit-rank matches
python crawler.py --track remote-neural --no-websearch   # skip flaky DDG
```

### local-tech

Crawls every **active company in the store**, pulls their local (profile
`[locality]`) postings, drops clinical-ops and defense roles, keeps technical
titles, resume-fit-scores each new job in parallel, and writes a digest ranked
by a combined **√(resume-fit × company-mission)** score. Never emails.

```
python crawler.py --track local-tech [--top 20] [--workers 8]
```

Companies carry a cached mission score (bullseye = 1.0 → down the profile's
mission tiers), judged once per company instead of once per job. **Multi-division
conglomerates** (profile `[policy].multi_division`) are the exception — they're
crawled through the keyword filter so only their aligned-subdivision roles
survive, and ranked at a floor rather than their low company score.

---

## Growing the company roster — `discover.py`

The store's `companies` table **is** the roster — there are no company lists in
`config.py` anymore. Ways to add companies:

| Command | What it does |
|---|---|
| `python discover.py "neurotech startups"` | Claude suggests employers; slugs probed against Greenhouse/Lever/Ashby/Kula/JazzHR/BambooHR/SmartRecruiters; careers pages sniffed; Workday resolved via headless browser. `--apply` upserts confirmed boards **into the store** (mission left NULL). |
| `python discover.py --from-keywords` | Same, for every profile keyword. |
| `python discover.py --from-bciwiki [--js]` | Resolve the BCIWiki company directory (~700 companies) to crawlable boards. |
| `python discover.py --local` | Local sourcing: profile seeds + directory scrapes + **web-search name harvesting** → probe → locality-verify → mission-score into the store (tag `nc_local`). |
| `python discover.py --dork` (`--ats-dork`) | Mine search-indexed board URLs (`site:jobs.lever.co "Durham"`) built from your profile locality + keywords. |
| `python discover.py --resolve-leads` | Resolve page-capture company leads: slug probe → careers sniff → Workday probe → web-search fallback. Idempotent. |
| `python discover.py --add-board "NVIDIA" URL` | You already know the board: paste its ATS or careers URL. Coordinates extracted, locality-verified, activated. |
| `python discover.py --score-missions` | Tier any active company that has a board but no mission yet (run after `--apply`/`--local`). `--rescore-missions` re-scores everything. |

After `--apply` or `--local`, run `--score-missions` — the apply step
deliberately leaves mission NULL so scoring happens in one pass.

Slug probing can confirm the wrong company ("seer" the proteomics shop vs Seer
Medical) — such hits carry `VERIFY:` notes through reports. Eyeball them.

### Sharing / backing up the roster

```
python crawler.py --export-companies roster.json    # dump the roster (secrets-free)
python crawler.py --import-companies roster.json    # upsert a shared roster
```

`roster.json` is the diffable, shareable "starter set" that replaced the old
config seed lists — hand it to someone and they bootstrap instantly.

---

## Gated & big-company employers

Some employers can't be crawled directly. Route by type:

- **Secretly on a standard ATS** (e.g. NVIDIA on Workday) — just add the board:
  `python discover.py --add-board "NVIDIA" https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`.
- **Bot-gated custom sites** (Meta, Google, Qualcomm — no public feed):
  - **NLx feed** — `python crawler.py --nlx "Meta,Google,Qualcomm"`. Federal
    contractors must list US openings via the National Labor Exchange; this
    reads them through the free **CareerOneStop** API (register at
    careeronestop.org/Developers, set `CAREERONESTOP_USER_ID`/`_TOKEN`). No
    scraping, no bot walls. Results carry a description snippet.
  - **Manual** — browse the site yourself and add postings with `capture.py`
    (metacareers.com is parsed; any single job via `capture.py --add`).
- **Multi-division conglomerates** — list them in profile `[policy].multi_division`
  so their healthcare-aligned subdivisions surface even though the company's
  overall mission is "other".

---

## Manual page capture — `capture.py`

For gated boards (LinkedIn, Indeed, metacareers): **you** browse logged in as
yourself; the crawler just keeps what you saw. No automation touches your account.

```
python capture.py                 # capture server on http://127.0.0.1:8877/
python capture.py --watch         # or: watch ./captures for Ctrl+S saves
python capture.py page.html ...   # or: ingest saved files one-off
python capture.py --add --url URL --title "..." --company "Meta" --location "Durham, NC"
```

- **Userscript button** (needs Violentmonkey/Tampermonkey): open
  `http://127.0.0.1:8877/` once to install; a **➤ Jobs** button appears on
  LinkedIn / Indeed / metacareers pages and sends the live DOM on click.
- **Watch mode** (zero installs): run `--watch`, Ctrl+S → "Web Page, complete"
  into `captures/`. Ingested within ~2 s.
- **`--add`** hand-adds one curated posting from a gated/JS site: it skips the
  exclude/technical guesswork (you chose it) but **keeps the locality gate**,
  registers the company, and pulls its other local jobs if the board resolves.

Captured pages are parsed in layers (LinkedIn markup generations, Indeed cards,
metacareers job cards, JSON-LD, generic job links), gated, fit-scored, stored.
Company names seen become **leads** → `discover.py --resolve-leads`.

---

## Maintenance

```
python crawler.py --prune                     # deactivate dead (404) ATS boards
python crawler.py --prune --prune-offmission  # also drop off-mission "other" companies
```

`--prune` probes every active Greenhouse/Lever/Ashby/BambooHR board and
deactivates the dead ones — run it whenever the crawl starts spamming `HTTP 404`
(usually after a big discovery import leaves stale slugs). It never touches a
live board; `--prune-offmission` additionally retires `other`-tier companies
(keeping multi-division giants).

---

## Keyword & scoring utilities

```
python crawler.py --expand "eeg engineer"        # Claude: alt titles/keywords/sectors
python crawler.py --expand-live TERM             # ...folded into this run
python crawler.py --expand-location "NC"         # location synonym expansion
python crawler.py --keyword-report               # bulk-expand profile keywords
python crawler.py --score "job description..."   # technical-bar score one posting
python crawler.py --db alt.db ...                # isolated store (concurrent runs)
python smoke_test.py                             # offline regression guard
```

---

## Résumé-fit scoring

Each job gets a résumé-fit score in [0, 1] from a **multi-axis rubric**
(`jobcrawler/fit.py`), not a single opaque number. The LLM scores four
orthogonal axes and flags disqualifying gates; Python combines them (a weighted
geometric mean times the worst gate penalty), so the math is transparent and
tunable:

- **domain** — how close the role's subject matter is to yours, on a graded
  ladder (e.g. iEEG/EEG ~1.0 down to non-health ~0.15).
- **function** — whether the role's *discipline* matches (research / ML /
  scientific-pipeline high; analytics-warehouse, embedded, generic backend low),
  judged from the JD body, not the title.
- **stack** — overlap of the tools the JD actually requires with your stack; a
  role centred on tools you lack (Snowflake/dbt, Kubernetes, RTOS) scores low
  even when the title matches.
- **seniority** — do you clear the level without being wildly over/under.

Gates are disqualifiers (they multiply the score down, worst gate wins), not
deductions: **geo** (not remote and not in your region), **embedded**
(firmware/PCB/RTOS), **level** (below your technical bar), **phd** (hard PhD
requirement). Together the axes keep two different non-domain roles distinct, and
the gates — not more axes — create the spread, so a warehouse "Data Engineer"
stops scoring like your pipeline work.

Everything is profile-driven: weights, gate penalties, the domain ladder, your
stack vocabulary, and region terms live in `profile.toml [fit]` (omit it for the
built-in neural/biosignal defaults). The rubric is calibrated against a small
anchor set — run `python -m jobcrawler.fit` to print the predicted-vs-hand table
after retuning weights.

**Stored columns.** `resume_fit_score` is the combined scalar; the breakdown is
also stored per axis (`fit_domain`, `fit_function`, `fit_stack`,
`fit_seniority`), plus `fit_gates` (comma-joined) and a compact `fit_reason` tag,
so you can query and sort on any axis:

```
sqlite3 local_tech.db "SELECT resume_fit_score, fit_domain, fit_stack, fit_gates, title \
  FROM jobs WHERE fit_gates IS NULL ORDER BY resume_fit_score DESC LIMIT 20"
```

**No description, no score.** A posting with no real JD body (under ~200 chars)
scores `None` and is left unranked, rather than floated at a fabricated mid
value — so fetch the bodies first.

### Re-scoring & description backfill

```
python crawler.py --local-tech --backfill-descriptions            # fetch full JD text for stored Workday jobs (CXS)
python crawler.py --local-tech --backfill-descriptions --limit 20 # try a small batch first
python crawler.py --local-tech --rescore                          # re-score every stored job with the current rubric/profile
python crawler.py --local-tech --rescore --described-only         # ...only jobs that already have a JD body
```

Workday serves each job's full description as plain JSON from
`/wday/cxs/{tenant}/{site}{externalPath}` — the live fetcher now pulls it, and
`--backfill-descriptions` fills it in for rows stored before that (idempotent;
only touches `myworkdayjobs.com` URLs missing a body). Run the backfill, then
`--rescore`: real text goes in, and the no-description rows that used to clog the
top are cleared out. Use `--rescore` after changing your resume, the `[fit]`
block, or the prompt — a normal crawl only scores jobs it hasn't seen.

---

## Customizing — `profile.toml`

Everything personal lives in `profile.toml` (gitignored; `profile.example.toml`
is the checked-in template). Sections:

| Section | Controls |
|---|---|
| `[keywords]` | `core` / `domain` / `skill` relevance tiers |
| `[exclude]` | `phrases` (title+body) and `title_phrases` (title only) |
| `[locations]` | `onsite` / `remote` terms, `accept_remote` |
| `[policy]` | `multi_division` conglomerates + ranking floor |
| `[candidate]` | who you are — injected verbatim into every Claude scoring/discovery prompt |
| `[fit]` | résumé-fit rubric: axis `weights`, `gate_penalty`, the `domain_ladder`, your `stack_core`/`stack_anti`, and `region_terms` (all optional; sensible defaults) |
| `[mission]` | employer mission tiers (name, definition, score band, active) + the bullseye pin |
| `[locality]` | what counts as "local" for the local track (`jobcrawler/nc.py`) |
| `[discovery]` | seed company names, Workday majors, directory URLs, web-search name queries |

**Relevance model:** a job passes if it hits any `core` term, OR a `domain` +
`skill` pair. Keep `core` narrow (high-signal); let `domain`+`skill` pull in
adjacent roles without opening the floodgates. Precision notes are preserved as
comments in the file — short acronyms are word-boundary matched, and bare
generic terms ("signal", "medical") are qualified because they leaked military
RF and benefits-boilerplate roles into a clinical search.

**It's fully swappable.** Drop in a different `profile.toml` and the whole system
retargets — mission tiers, locality regex, the LLM prompts, and discovery
sourcing all follow. `config.py` needs no edits.

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

- **companies** — name, ats, slug / Workday triple / careers_url, NC job count,
  cached mission tier + score, `tags` (`neural`, `nc_local`, `multi_division` —
  which tracks crawl it), `source` (`discovery:<term>` / `local_sourcing` /
  `ats_dork` / `page_capture` / `manual` / `nlx`), `active` flag.
- **jobs** — stable job_id (dedup), track, geo_mode, remote/neural signals,
  description, résumé-fit score + reason, the per-axis breakdown (`fit_domain`,
  `fit_function`, `fit_stack`, `fit_seniority`, `fit_gates`), first/last seen.
  The combined résumé-fit×mission ranking score is computed at read time, not
  stored.

Schema migrations are additive/automatic; old DBs upgrade in place (and shed
retired columns). Runtime artifacts (all gitignored): `local_tech.db`,
`profile.toml`, `job_reports/*.md`, `captures/`, `*.log`.

**One writer at a time:** SQLite locking does not span the boundary between your
shell and an agent sandbox mounting the same folder — two concurrent writers
corrupt the DB.

## Module map

| Module | Role |
|---|---|
| `crawler.py` / `discover.py` / `capture.py` | entry points: crawl/maintain, roster growth, manual capture |
| `config.py` / `profile.toml` | plumbing (config.py) vs. all search criteria (profile.toml) |
| `jobcrawler/tracks/` | the two tracks (gates, ranking, digests) |
| `jobcrawler/sources.py` | declarative ATS registry: store rows ↔ fetch thunks |
| `jobcrawler/fetchers/` | board fetchers (10 ATSes + RSS/HN/RemoteOK/Remotive/DDG/JSON-LD/sitemap + CareerOneStop/NLx); `workday.py` also pulls full JD text via the CXS per-job endpoint and exposes `backfill_workday_descriptions` |
| `jobcrawler/fetchers/company.py` | company-vetted, location-scoped pulls + lazy description hydration + custom-board scraper |
| `jobcrawler/discovery/` | pipeline, slug probes, careers-page sniffer, BCIWiki, local sourcing (+ web-search name harvest), dorking; `apply.py` upserts into the store |
| `jobcrawler/page_capture.py` | parse captured LinkedIn / Indeed / metacareers / any-board HTML |
| `jobcrawler/store.py` | unified companies + jobs store (+ export/import, prune, `update_job_scores`) |
| `jobcrawler/fit.py` | multi-axis résumé-fit rubric (axes + gates + deterministic combiner), templated from profile `[fit]`; calibration harness via `python -m jobcrawler.fit` |
| `jobcrawler/claude.py` | Claude API wrapper + discovery/expansion/mission/tech-bar prompts; `score_resume_fit` delegates to `fit.py` |
| `jobcrawler/filters.py` / `remote_filter.py` / `nc.py` | keyword tiers, remote eligibility, locality — all driven by `profile.toml` |
| `jobcrawler/parallel.py` | thread-pool source fetching (`CRAWLER_WORKERS`/`DISCOVERY_WORKERS` env) |
