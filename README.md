# Jobs Crawler

[![CI](https://github.com/Aaronearlerichardson/Jobs/actions/workflows/ci.yml/badge.svg)](https://github.com/Aaronearlerichardson/Jobs/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/Aaronearlerichardson/Jobs/branch/main/graph/badge.svg)](https://codecov.io/gh/Aaronearlerichardson/Jobs)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://github.com/Aaronearlerichardson/Jobs/actions/workflows/ci.yml)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](https://github.com/Aaronearlerichardson/Jobs/actions/workflows/ci.yml)
[![Boards](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Aaronearlerichardson/Jobs/main/.github/badges/boards.json)](BOARDS.md)

**A personal job search that runs itself.** It crawls employers' own job
boards — not aggregators — scores every posting against your résumé, and gives
you one ranked list in a local web app. It runs entirely on your machine
against a local SQLite database; there is no account and no server.

The point is precision. Job sites optimise for volume; this optimises for the
twenty postings actually worth your afternoon. It gets there by knowing three
things about you — **what** you want to work on, **where**, and **who you
are** — all of which live in one `profile.toml` you own. The code stays
generic: swap the profile and the whole system retargets to a different
field, region, and career.

```
DISCOVERY                    STORE (your data dir)            CRAWL
discover.py ..............>  companies                        run_scraper.py [--track X]
  LLM suggestions              (board, mission score,           fetch boards (parallel)
  public directories            scope tags, active flag)        -> gates (per track)
  local sourcing / dorking    jobs                              -> score (résumé fit /
  page-capture leads            (dedup, per-track fields,          technical bar / remote)
capture.py ...............>     fit scores)                     -> ranked digest + web UI
  browse gated sites yourself
```

---

## Quickstart

```bash
git clone https://github.com/Aaronearlerichardson/Jobs && cd Jobs
pip install -r envs/requirements.txt
python webapp.py
```

That's a working app at <http://127.0.0.1:5533> with an empty database. On
first run it creates your own `profile.toml` on your machine (outside the
checkout) and tells you where. Then, in order:

1. **Describe your search** — the **Settings** tab, or edit `profile.toml`
   directly. Start with `[keywords]`, `[locality]`, and `[candidate]`.
2. **Drop your résumé** into your data directory as `resume.docx`
   (`python run_scraper.py --where` prints the path). Optional — without it
   everything works except fit scoring.
3. **Set `ANTHROPIC_API_KEY`** if you want scoring and LLM-assisted discovery.
   Optional — the crawler degrades to heuristics without it.
4. **Find employers to crawl** — the **Operations** tab's discovery buttons,
   or `python discover.py "climate tech startups"`. The crawler only searches
   companies it knows about, so this is the step that makes it yours.
5. **Crawl** — the **Crawl** button, or `python run_scraper.py`.

### Where your data lives

Everything personal is written to a per-user directory **outside the
repository**, so `git pull` never touches it and a re-clone never inherits
someone else's search:

| OS | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\JobCrawler` |
| macOS | `~/Library/Application Support/JobCrawler` |
| Linux | `$XDG_DATA_HOME/job-crawler` (default `~/.local/share/job-crawler`) |

```bash
python run_scraper.py --where     # print the exact paths this install uses
```

That directory holds `jobs.db`, your `profile.toml`, your résumé,
`job_reports/`, caches, captures, and profile backups. Three ways to move it:

- **`JOBS_DATA_DIR`** — point it anywhere (a synced folder, an encrypted
  volume, a second profile for a friend's search).
- **`JOBS_PROFILE`** — point just the profile file somewhere else.
- **A `data/` folder inside the checkout** — if one exists it wins, which
  keeps older installs working and makes the whole app portable if that's
  what you want. Backing up your search is copying one folder either way.

### API keys

```powershell
# PowerShell, persistent
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
[Environment]::SetEnvironmentVariable("GMAIL_ADDRESS",      "you@gmail.com", "User")
[Environment]::SetEnvironmentVariable("GMAIL_APP_PASSWORD", "abcdefghijklmnop", "User")
```

- **ANTHROPIC_API_KEY** powers the scorers (résumé fit, technical bar, company
  mission) and LLM-driven discovery. Everything degrades gracefully without it
  (heuristic fallbacks, unscored missions).
- **GMAIL_ADDRESS / GMAIL_APP_PASSWORD** are only for emailed digests, and
  only if you turn them on (Google Account → Security → 2-Step Verification →
  "App passwords" → Mail). Emailing is off until both are set.
- **CAREERONESTOP_USER_ID / _TOKEN** (optional) unlock the NLx feed for
  employers with no public board — see [Gated employers](#gated--big-company-employers).

**Prompt caching is on by default.** Every scorer sends the same stable system
prompt (rubric + profile) with a per-posting user turn, so `core/claude.py`
puts a cache breakpoint at the end of `system`: the first call in a run writes
the prefix (1.25x) and the rest read it (0.1x), roughly a 5-10x cut on input
cost for a several-hundred-job crawl. Each run prints a
`[claude] ... % of cacheable prefix hit` line on exit — 0% over a long run
means something is invalidating the prefix. `CLAUDE_PROMPT_CACHE=0` disables
it, `CLAUDE_CACHE_TTL=1h` buys the 1-hour cache (2x writes; only worth it if
calls are >5 min apart), `CLAUDE_USAGE_SUMMARY=0` silences the summary line.
Prompts below the model's floor (1024 tokens on Sonnet 5, 512 on Opus 5)
simply don't cache, at no extra cost.

---

## Tracks — running the same search two ways

A hard search usually has a tension in it: the roles you want most aren't
where you are, or the roles near you aren't quite the thing. Rather than
compromise into one mediocre query, you define **tracks** — parallel searches
you flip between in the UI header, each relaxing a different constraint.

The shipped profile sets up the classic pair:

| Track | Keeps | Relaxes | Command |
|---|---|---|---|
| **local** | your region, your technical bar | the subject matter | `python run_scraper.py --track local` |
| **remote** | the subject matter, your technical bar | location → remote | `python run_scraper.py --track remote` |

A track is **pure configuration** — engine, database, sources, gates, scoring
budget, digest email — run through ONE pipeline (`scrapers/runner.py`). Rename
them, delete one, add five. `python run_scraper.py` with no flags refreshes
every configured track: the daily-refresh entry point.

Each track picks an **engine**, which is the one thing that isn't
configuration — it selects a crawl posture:

- **`local`** — location-scoped. Asks each board for *your* region, so it
  stays cheap even against employers with 10,000 openings. Geo-gated; ranks by
  a combined **√(résumé-fit × company-mission)** score.
- **`sweep`** — location-agnostic. Pulls whole boards plus aggregator feeds
  and web search, gated hard on your `core` keywords so the wider net doesn't
  flood the digest.

```bash
python run_scraper.py --track remote --preview      # read-only preview
python run_scraper.py --track remote --send         # email the digest
python run_scraper.py --track local --no-fit        # skip résumé-fit scoring
python run_scraper.py --track local --no-websearch  # skip flaky web search
python run_scraper.py --track local --top 20 --workers 8
```

Companies carry a cached **mission score** (how much you care about the
employer, judged once per company rather than once per job, against the tiers
in your profile's `[mission]`). **Multi-division conglomerates**
(`[policy].multi_division`) are the exception — crawled through the keyword
filter so only their aligned roles survive, and ranked at a floor rather than
their low company score.

---

## Growing the company roster — `discover.py`

The store's `companies` table **is** the roster. Ways to add to it:

| Command | What it does |
|---|---|
| `python discover.py "climate tech startups"` | An LLM suggests employers; slugs probed against Greenhouse/Lever/Ashby/Kula/JazzHR/BambooHR/SmartRecruiters; careers pages sniffed; Workday resolved via headless browser. `--apply` upserts confirmed boards **into the store**. |
| `python discover.py --from-keywords` | Same, once per keyword in your profile. |
| `python discover.py --local` | Local sourcing: profile seeds + directory scrapes + **web-search name harvesting** → probe → locality-verify → mission-score into the store. |
| `python discover.py --dork` | Mine search-indexed board URLs (`site:jobs.lever.co "<your city>"`) built from your locality + keywords. |
| `python discover.py --resolve-leads` | Resolve company leads left by page capture: slug probe → careers sniff → Workday probe → web-search fallback. Idempotent. |
| `python discover.py --add-board "NVIDIA" URL` | You already know the board: paste its ATS or careers URL. Coordinates extracted, locality-verified, activated. |
| `python discover.py --score-missions` | Tier any active company with a board but no mission yet (run after `--apply`/`--local`). `--rescore-missions` re-scores everything. |
| `python discover.py --from-bciwiki` | Bulk-import a public industry directory (bciwiki.org's ~700 brain-computer-interface companies). A worked example of the pattern; only useful if that's your field. |

After `--apply` or `--local`, run `--score-missions` — the apply step
deliberately leaves mission NULL so scoring happens in one pass.

Slug probing can confirm the wrong company (a proteomics "Seer" vs a medical
one) — such hits carry `VERIFY:` notes through reports. Eyeball them.

### Sharing / backing up the roster

```bash
python run_scraper.py --export-companies roster.json    # dump the roster (secrets-free)
python run_scraper.py --import-companies roster.json    # upsert a shared roster
```

`roster.json` is diffable and shareable — hand someone in your field a
starter set and they bootstrap instantly.

---

## Gated & big-company employers

Some employers can't be crawled directly. Route by type:

- **Secretly on a standard ATS** (e.g. NVIDIA on Workday) — just add the
  board: `python discover.py --add-board "NVIDIA" https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`.
- **Sites with no public feed** (Meta, Google, Qualcomm):
  - **NLx feed** — `python run_scraper.py --nlx "Meta,Google,Qualcomm"`.
    Federal contractors must list US openings via the National Labor Exchange;
    this reads them through the free **CareerOneStop** API (register at
    careeronestop.org/Developers, set `CAREERONESTOP_USER_ID`/`_TOKEN`). An
    official government API — nothing is scraped.
  - **Manual** — browse the site yourself and add postings with `capture.py`.
- **Multi-division conglomerates** — list them in `[policy].multi_division`
  so their aligned subdivisions surface even though the company's overall
  mission scores low.

---

## Manual page capture — `capture.py`

Sites that require a login (LinkedIn, Indeed, metacareers) are **never fetched
by this tool**. Instead, *you* browse them yourself, signed in as yourself,
and the crawler parses the page your own browser already loaded. No automation
touches those sites or your account — and their hosts are on an explicit skip
list (`_GATED_HOST_RE` in `scrapers/fetchers/company.py`), so even a stale link
there is reported "unverifiable" rather than fetched.

```bash
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
- **`--add`** hand-adds one curated posting: it skips the exclude/technical
  guesswork (you chose it) but **keeps the locality gate**, registers the
  company, and pulls its other local jobs if the board resolves.

Captured pages are parsed in layers (LinkedIn markup generations, Indeed
cards, metacareers job cards, JSON-LD, generic job links), gated, fit-scored,
stored. Company names seen become **leads** → `discover.py --resolve-leads`.

---

## Résumé-fit scoring

Each job gets a résumé-fit score in [0, 1] from a **multi-axis rubric**
(`core/fit.py`), not a single opaque number. The LLM scores four orthogonal
axes and flags disqualifying gates; Python combines them (a weighted geometric
mean times the worst gate penalty), so the math is transparent and tunable:

- **domain** — how close the role's subject matter is to yours, on a graded
  ladder you define.
- **function** — whether the role's *discipline* matches, judged from the JD
  body, not the title.
- **stack** — overlap of the tools the JD actually requires with yours; a role
  centred on tools you lack scores low even when the title matches.
- **seniority** — do you clear the level without being wildly over/under.

Gates are disqualifiers (they multiply the score down, worst gate wins), not
deductions: **geo** (not remote and not in your region), **embedded**
(firmware/PCB/RTOS), **level** (below your technical bar), **phd** (hard PhD
requirement). Together the axes keep two different off-domain roles distinct,
and the gates — not more axes — create the spread, so a warehouse "Data
Engineer" stops scoring like your actual work.

Everything is profile-driven: weights, gate penalties, the domain ladder, your
stack vocabulary, and region terms live in `[fit]` (omit it for built-in
defaults). Run `python -m core.fit` to print the predicted-vs-hand calibration
table after retuning weights.

**Stored columns.** `resume_fit_score` is the combined scalar; the breakdown
is stored per axis (`fit_domain`, `fit_function`, `fit_stack`,
`fit_seniority`), plus `fit_gates` and a compact `fit_reason` tag, so you can
query and sort on any axis:

```bash
sqlite3 jobs.db "SELECT resume_fit_score, fit_domain, fit_stack, fit_gates, title \
  FROM jobs WHERE fit_gates IS NULL ORDER BY resume_fit_score DESC LIMIT 20"
```

**No description, no score.** A posting with no real JD body (under ~200
chars) scores `None` and is left unranked, rather than floated at a fabricated
mid value — so fetch the bodies first.

### Re-scoring & description backfill

```bash
python run_scraper.py --track local --backfill-descriptions            # fetch full JD text for stored Workday jobs (CXS)
python run_scraper.py --track local --backfill-descriptions --limit 20 # try a small batch first
python run_scraper.py --track local --rescore                          # re-score every stored job with the current rubric
python run_scraper.py --track local --rescore --described-only         # ...only jobs that already have a JD body
```

Workday serves each job's full description as plain JSON from
`/wday/cxs/{tenant}/{site}{externalPath}` — the live fetcher pulls it, and
`--backfill-descriptions` fills it in for rows stored before that (idempotent).
Run the backfill, then `--rescore`: real text goes in, and the no-description
rows that used to clog the top clear out. Use `--rescore` after changing your
résumé, the `[fit]` block, or the prompt — a normal crawl only scores jobs it
hasn't seen.

### Deep verification & the watchlist

```bash
python run_scraper.py --track local                # crawl deep-verifies the top N before the digest
python run_scraper.py --track local --no-verify    # skip the second pass
python run_scraper.py --track local --verify-top 15 # re-verify the current top 15 (no crawl)
python run_scraper.py --watch "Ceribell"           # never miss a new technical posting there
python run_scraper.py --unwatch "Ceribell"
```

Scoring is two-pass. The **screen** scores every new job once on up to
`config.MAX_DESC_CHARS` of text (one budget shared by fetchers, storage, and
the prompt; clipping keeps head + tail so a requirements block at the end of a
long JD survives). The **deep verify** pass then re-reads the finalists' full
postings (live detail fetch — Workday CXS / Greenhouse API / JSON-LD), first
extracting hard requirements (years, seat type, must-haves, candidate gaps)
and only then re-scoring all axes and gates; demotions re-rank and
newly-promoted rows get verified too.

Born of a real failure: a 20k-char "Sr Manager, Applied AI" JD scored 0.69 off
its on-topic preamble while "8+ years program management + end-to-end GCP" sat
past every truncation cap. The fixed pipeline gates it as the management seat
it is, and the domain axis now scores the role's own work rather than the
employer's halo.

**Watched companies** (`--watch`) get their whole board fetched every crawl,
and every new technical posting there is flagged in a dedicated digest section
regardless of fit rank or geography — the "wait for the right role at this
employer" list.

### Dispositions — record your decisions, teach the scorer

```bash
python run_scraper.py --mark applied gh_examplecorp_4361153009
python run_scraper.py --mark dismissed 6113860004 --why "TPM seat, wrong archetype"
python run_scraper.py --mark saved <url>          # shortlist; stays in the ranking
python run_scraper.py --mark clear <job>          # undo
python run_scraper.py --pipeline                  # everything you've dispositioned
```

`JOB` is a job_id, any unique fragment of one, or the posting URL.
Dispositions: `saved` (shortlisted — stays ranked), `applied` / `interviewing`
(move to the digest's **pipeline** section, which also flags when a posting
closes after you applied), `rejected` / `dismissed` (leave the ranking). Rows
out of the ranking are skipped by `--rescore`, self-heal, and verification —
no API spend on decided jobs.

The feedback loop: your most recent applied/dismissed decisions are injected
into the fit-scoring prompt as few-shot calibration, with `--why` notes
verbatim — so "dismissed: TPM seat, wrong archetype" teaches the screen to
score the next such posting down. Tune the count via `[fit]
disposition_examples` (0 disables).

---

## Web UI

```bash
python webapp.py        ->  http://127.0.0.1:5533
```

Everything above in one local page (Flask + a single self-contained
[webapp/](webapp/), light/dark, no build step):

* **Tracks** — a header dropdown switches between the searches defined in
  `[tracks.*]`. Every tab, stat, and operation re-scopes to the selected track.
* **Jobs** — the digest ranking (fit, combined, verified badge, axis meters,
  gates, age/NEW/stale chips), filters (search, min fit, geo bucket
  local/remote/**relocation**, a "willing to relocate" toggle, age,
  verified-only, watched-companies, include closed/decided), row expand for the
  full deep-verify reason + description, and one-click dispositions (with the
  `--why` prompt that teaches the scorer). The geo bucket is derived live from
  each posting's location against your `[locality]` — jobs you'd have to move
  for stay hidden until you opt in.
* **Pipeline** — applied/interviewing/saved/rejected/dismissed groups, with
  the closed-after-you-applied flag and change/clear actions.
* **Companies** — roster with mission tier/score, open-job counts, active
  toggles, the ★ watchlist, and roster JSON export.
* **Operations** — crawl, status sync, deep-verify, stale-URL probe, rescore,
  description backfills, NLx ingest, **company discovery** (directories + web
  search + a cached LLM name-brainstorm + the dork sweep), mission scoring,
  prune, dedup, manual job add, add-company-board — run as background tasks
  with the console streamed live into the page.
* **Settings** — edit your `profile.toml` from the browser: chip-style editors
  for every keyword/exclude/location/locality list (global and per-track),
  numeric fit weights/gate penalties, per-track UI defaults, and a validated
  raw-TOML editor for everything else. Each save backs the file up to
  `config_backups/` (last 20 kept) and gracefully restarts the server so
  import-time config snapshots refresh — the page reconnects itself.

The UI talks to the same SQLite store and modules as the CLI, so the two are
interchangeable. The header shows which store and profile are live, and warns
if `ANTHROPIC_API_KEY` is unset.

**Launchers.** Double-click `run_webui.bat` to start from source. Both `.bat`
files activate the `jobs` conda environment first via `activate_env.bat`
(create it once with `conda env create -f envs\environment.yml`; set `JOBS_ENV`
to use a different name). To ship the UI to a machine **without Python or any
pip installs**, run `build_exe.bat`:
it compiles the whole app with [Nuitka](https://nuitka.net) into a
self-contained single-file binary, `JobCrawlerUI.exe`, bundling CPython,
Flask, the crawler package, and lxml. The exe finds its data the same way the
source install does (`JOBS_DATA_DIR`, then a `data/` beside or above it, then
the per-user directory), so copying it to a new machine starts a clean install
and dropping it into an existing one picks up that store. The only feature
missing from a compiled build is the optional Playwright headless-browser
probing for JS-only boards (it needs its own browser download; those code
paths degrade gracefully). First build downloads a C compiler if none is
present and takes 10–30 minutes; rebuilds are fast.

---

## Freshness & closed-job tracking

Every board fetch captures the posting's real publish date (`posted_at`:
Greenhouse `first_published`, Lever `createdAt`, Ashby, SmartRecruiters
`releasedDate`, JSON-LD `datePosted`, Workday's relative "Posted N Days Ago").
The status sync backfills dates for already-stored rows on every crawl, so
coverage grows with zero extra requests. Boards that publish no dates show `?`.
The console top-N and the digest carry an **Age** tag: `NEW` = first seen
today, `Nd` = days since posted, `!` = 45+ days (often a ghost req), `?` =
unknown. `first_seen` (when the crawler noticed it) is deliberately kept
separate from `posted_at` (when it actually went up).

```bash
python run_scraper.py --track local                  # crawl also syncs open/closed per board
python run_scraper.py --track local --sync-status    # reconcile statuses only (no scoring/API)
python run_scraper.py --track local --check-closed   # probe rows no board has vouched for lately
python run_scraper.py --track local --check-closed --stale-days 5 --limit 20
```

Every job row carries `status` (`open`/`closed`) and `closed_at`. The crawl
marks them itself: each successful, non-empty board fetch is treated as the
authoritative list of what that company currently posts — stored rows that
vanished are closed, rows that reappear are reopened. Rows whose id came from
the board match by **exact id only** (boards recycle titles across
requisitions, so one live "Algorithm Engineer" must not shield five dead
ones); externally-ingested rows can never id-match, so they match by URL/title
and get a few days' grace. Empty or failed fetches never close anything, so a
dead board can't nuke its history. `--check-closed` covers what board fetches
can't vouch for — orphans, inactive companies, boards that died or moved — by
probing each job URL for definite death signals (404/410, "no longer
accepting" notices, past JSON-LD `validThrough`, a Workday CXS miss);
indeterminate probes leave rows open.

Closed jobs drop out of the ranked digest automatically and are skipped by
`--rescore` and both backfills, so no API spend goes to dead postings.
Re-seeing a job anywhere reopens it.

---

## Maintenance & utilities

```bash
python run_scraper.py --prune                     # deactivate dead (404) ATS boards
python run_scraper.py --prune --prune-offmission  # also drop off-mission companies
python run_scraper.py --expand "eeg engineer"     # LLM: alt titles/keywords/sectors
python run_scraper.py --expand-location "NC"      # location synonym expansion
python run_scraper.py --keyword-report            # bulk-expand your profile keywords
python run_scraper.py --score "job description…"  # technical-bar score one posting
python run_scraper.py --db alt.db ...             # isolated store (concurrent runs)
python run_scraper.py --where                     # print profile + data paths
```

`--prune` probes every active Greenhouse/Lever/Ashby/BambooHR board and
deactivates the dead ones — run it whenever a crawl starts spamming `HTTP 404`
(usually after a big discovery import leaves stale slugs). It never touches a
live board.

### Scheduling (Windows)

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
           -Argument "run_scraper.py --track local" `
           -WorkingDirectory "C:\path\to\Jobs"
$trigger = New-ScheduledTaskTrigger -Daily -At "8:00AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable
Register-ScheduledTask -TaskName "Jobs Crawler" -Action $action -Trigger $trigger -Settings $settings -Force
```

(Or the Task Scheduler GUI: daily trigger → program `python`, arguments
`run_scraper.py --track ...`, "Start in" = the repo folder. Tasks still
pointing at `crawler.py` keep working — it's a shim that forwards to
`run_scraper.py`. Note that `run_scraper.py` with NO flags refreshes EVERY
configured track.)

---

## Customizing — `profile.toml`

Everything personal lives in `profile.toml` on your machine;
`profile.example.toml` in the repo is the annotated template it's copied from.

| Section | Controls |
|---|---|
| `[keywords]` | `core` / `domain` / `skill` relevance tiers (plus `[keywords.<track>]` overrides) |
| `[exclude]` | `phrases` (title+body), `title_phrases` (title only), boilerplate scrubbing |
| `[locations]` | `onsite` / `remote` terms, `accept_remote`, remote-eligibility vocabulary |
| `[tracks.<id>]` | the searches you switch between: engine, DB, sources, gates, scoring budget, UI defaults |
| `[policy]` | `multi_division` conglomerates + ranking floor |
| `[candidate]` | who you are — injected verbatim into every scoring/discovery prompt; also your `resume` path |
| `[fit]` | résumé-fit rubric: axis `weights`, `gate_penalty`, `domain_ladder`, `stack_core`/`stack_anti`, `region_terms` |
| `[mission]` | employer mission tiers (name, definition, score band, active) + the bullseye pin |
| `[locality]` | what counts as "local" (`core/locality.py`) |
| `[sources]` | non-company feeds: RemoteOK/Remotive/HN toggles, RSS feeds, Discourse forums, web-search queries |
| `[discovery]` | seed companies, Workday majors, directory URLs, web-search name queries, priority companies |

**Relevance model:** a job passes if it hits any `core` term, OR a `domain` +
`skill` pair. Keep `core` narrow (high-signal); let `domain`+`skill` pull in
adjacent roles without opening the floodgates. Short acronyms are
word-boundary matched, and bare generic terms are worth qualifying — an
unqualified "signal" or "medical" leaks military RF and benefits boilerplate
into a search.

**It's fully swappable.** Drop in a different `profile.toml` and the whole
system retargets — mission tiers, locality regex, the LLM prompts, and
discovery sourcing all follow. No code edits.

---

## Data model & files

One SQLite store for everything — `jobs.db` in your data directory
(`core/store.py`). A track can get its own file via `[tracks.*].db`, but by
default they share:

- **companies** — name, ats, slug / Workday triple / careers_url,
  `local_job_count`, cached mission tier + score, `tags`, `source`
  (`discovery:<term>` / `local_sourcing` / `ats_dork` / `page_capture` /
  `manual` / `nlx`), `active` flag.
- **jobs** — stable job_id (dedup), `track`, geo_mode, remote/anchor signals,
  description, résumé-fit score + reason, the per-axis breakdown, first/last
  seen. The combined fit×mission ranking score is computed at read time.
  `track` is a comma-separated **set**, not one value: a posting belonging to
  two tracks is one row visible to both, and a second track's crawl adds its
  name rather than stealing the row (`store.track_set`).

Company `tags` are **scope** tokens describing how to crawl a company, not
what it does (`core/tags.py`): `local` (query its board per-region — the
expensive enterprise boards), `sweep` (pull the whole board — the cheap JSON
APIs), `watch` (human-set: fetch every crawl, flag anything new).

Schema migrations are additive and automatic; old DBs upgrade in place, shed
retired columns, and carry renamed columns' values across.

**One writer at a time:** SQLite locking does not span the boundary between
your shell and an agent sandbox mounting the same folder — two concurrent
writers corrupt the DB.

---

## Tests & CI

```bash
pip install -r envs/requirements-dev.txt
pytest                                   # the whole suite (offline, ~8s)
pytest tests/test_store.py -k closed     # one area
pytest --cov --cov-report=term-missing   # with coverage
```

`pytest` runs two things at once: the fixture-backed suite in `tests/`, and
the **doctests embedded in the source** (`--doctest-modules` over `core`,
`scrapers`, `discovery`, `webapp`, `tools` and the root modules). Docstrings
here are held to a rule — *a docstring may only state a claim that a doctest
or an invariant test enforces; everything else goes under `Notes:`* — because
prose that drifted from the code has already cost this project a real
misdiagnosis. The standard, with worked examples and the house style for
awkward outputs, is **[docs/DOCSTRINGS.md](docs/DOCSTRINGS.md)**.
`tests/test_invariants.py` carries the cross-module claims no single docstring
can prove.

The suite is **offline by contract** — no network, no LLM API, no writes to
your real profile or store. Fixtures derive their inputs from whichever
profile is loaded, so the same tests pass on your `profile.toml` and on the
shipped `profile.example.toml`; write tests redirect `PROFILE_PATH`/`DATA_DIR`
to a `tmp_path`.

`.github/workflows/ci.yml` runs on **every pull request** and on **pushes to
`main`**:

- **test** (Ubuntu × Python 3.12, 3.13, 3.14) — asserts the run really is
  example-profile + API-free, then byte-compiles, lints (pyflakes), validates
  the example profile, runs pytest with coverage, checks every CLI `--help`,
  and boots the web app to exercise the API and asset cache-busting.
- **build** (Ubuntu + Windows + macOS) — `python build_app.py`, then *launches
  the built binary* and requires its API to answer; a build that compiles but
  can't boot is not a pass. Runs only after the tests pass.

`python smoke_test.py` still works — a shim that runs pytest.

### Board health (the other half of "does it work?")

Coverage says whether code ran; it can't say whether **Greenhouse still
answers on the same endpoint with the same JSON shape**. The fetchers swallow
HTTP errors and return `[]` on purpose — one dead board must not abort a crawl
— so a platform that changes its payload fails *silently*: job counts just
quietly drop.

```bash
python tools/check_boards.py                 # one request per platform
python tools/check_boards.py --fail-on-broken
```

`.github/workflows/boards.yml` runs it nightly and publishes
**[BOARDS.md](BOARDS.md)** plus the badge above. Two design notes:

- It is **not** part of the merge gate. Network checks fail for reasons a PR
  author can't fix (site down, runner IP challenged), and a gate that
  red-lights for unfixable reasons is one people learn to ignore.
- It **widens the keyword filter** before judging a board. `is_relevant()`
  runs *inside* each fetcher, so "0 jobs" would otherwise conflate "the board
  is broken" with "nothing matched your search". Statuses separate `blocked`
  (rate-limited/challenged — not our bug) from `broken` (4xx/5xx) from
  `degraded` (reachable but suspiciously empty).

This paid for itself immediately: it caught `fetch_ashby` reading a
`jobPostings` key from a payload whose key is `jobs`, which had been returning
**zero postings for every Ashby board** with no error. Recorded fixtures in
`tests/fixtures/` (real responses, prose redacted) now pin the parsers offline.

### Source health — what can this crawler actually reach?

`check_boards.py` asks one question per ATS platform. The companion covers
everything else a crawl touches — aggregator feeds, web search, forums, keyed
APIs, robots.txt policy per host, and your own roster:

```bash
python tools/check_sources.py                  # every section
python tools/check_sources.py --only search --deep   # just web search, full pipeline
python tools/check_sources.py --roster         # + probe every active board you've saved
```

The point is the classification. "Nothing came back" has four very different
causes and only one of them is a bug:

- **🤖 robots** — the host's robots.txt asks us not to fetch that path, and
  the crawler honors it. Not fixable, and not something to route around;
  those postings are simply out of scope for automated fetching.
- **🚧 blocked** — reachable but refusing us (401/403/429, a CAPTCHA, an
  anti-bot wall). Often IP- or rate-based, so it may pass on a retry.
- **❌ broken** — 404/5xx/parse error. A real failure: a dead slug, a moved
  API, a shape change.
- **✅ ok** — alive, allowed, returning postings.

Like the board canary it **widens the keyword filter first**, so a zero means
the source gave us nothing rather than "nothing matched your search". Without
that, a perfectly healthy feed that happens not to carry your field reads
identically to a dead one.

---

## Module map

| Module | Role |
|---|---|
| `run_scraper.py` / `webapp.py` | entry points: daily refresh + maintenance CLI, web UI launcher (`crawler.py` = deprecation shim) |
| `discover.py` / `capture.py` | entry points: roster growth, manual page capture |
| `config.py` / `profile.toml` | plumbing (paths, data dir, track parsing) vs. all search criteria |
| `core/bootstrap.py` | first-run setup: seeds your profile, reports where data lives |
| `scrapers/runner.py` | THE crawl pipeline — one runner for every track, methodology from `[tracks.*]` |
| `scrapers/ops.py` | track-agnostic maintenance: status sync, deep-verify, closed-probe, rescore, backfills, ingest, manual adds |
| `scrapers/sources.py` | declarative ATS registry: store rows ↔ fetch thunks |
| `scrapers/fetchers/` | board fetchers (10 ATSes + RSS/HN/RemoteOK/Remotive/web-search/JSON-LD/sitemap + CareerOneStop/NLx) |
| `scrapers/fetchers/company.py` | company-vetted, location-scoped pulls + lazy description hydration + custom-board scraper |
| `scrapers/page_capture.py` | parse captured LinkedIn / Indeed / metacareers / any-board HTML |
| `discovery/` | pipeline, slug probes, careers-page sniffer, directory imports, local sourcing, dorking; `apply.py` upserts into the store |
| `core/store.py` | unified companies + jobs store (+ export/import, prune, migrations) |
| `core/tags.py` | company scope tags (`local` / `sweep` / `watch`) + legacy aliases |
| `core/fit.py` | multi-axis résumé-fit rubric, templated from `[fit]`; calibration harness via `python -m core.fit` |
| `core/claude.py` | LLM wrapper (prompt caching + token accounting) + discovery/expansion/mission/tech-bar prompts |
| `core/gates.py` / `core/digest_md.py` | config-driven title/exclude gates; ranked + matches digest renderers |
| `core/filters.py` / `remote_filter.py` / `locality.py` | keyword tiers, remote eligibility, locality — all profile-driven |
| `webapp/` | Flask package: `routes.py`, `ops.py` (op registry), `server.py`, `templates/` + `static/` |
| `scrapers/parallel.py` | thread-pool source fetching (`CRAWLER_WORKERS`/`DISCOVERY_WORKERS` env) |
| `scrapers/robots.py` | robots.txt fetch + cache + RFC 9309 path matching (stdlib's matcher is not compliant — see the module docstring) |
| `tools/check_boards.py` / `check_sources.py` | per-ATS canary; whole-crawl source health (robots/blocked/broken) |
