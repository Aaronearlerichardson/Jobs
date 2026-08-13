# Jobs Crawler

A configurable job-search crawler. **Your entire search — what you want, where,
and who you are — lives in one `profile.toml`; the code stays generic.** It
ships with an example profile for a genuinely hard case: *there aren't many BCI
jobs in North Carolina.* So it runs the same machinery in two postures — relax
the location, or relax the BCI constraint — and lets you pivot between them:

| Track | Keeps | Relaxes | Command |
|---|---|---|---|
| **remote-neural** | neural signals (BCI/EEG/iEEG/ECoG/...), high technical bar, clinical mission | location → remote (US-eligible) | `python run_scraper.py --track remote_neural` |
| **local-tech** | Triangle/NC location (~2.5 h ring), technical bar, health/bio/science mission | neural requirement | `python run_scraper.py --track local_tech` |

Those specifics are just the shipped profile — swap `profile.toml` and the
tracks retarget any field/region: a track is pure configuration
(`[tracks.*]`: engine, db, sources, gates, scoring budget, digest email) run
through ONE pipeline (`scrapers/runner.py`). `python run_scraper.py` with no
flags refreshes every configured track — the daily-refresh entry.

```
DISCOVERY                    STORE (data/*.db)                CRAWL
discover.py ..............>  companies                        run_scraper.py [--track X]
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
- **Prompt caching is on by default.** Every scorer sends the same stable
  system prompt (rubric + profile) with a per-posting user turn, so
  `core/claude.py` puts a cache breakpoint at the end of `system`: the
  first call in a run writes the prefix (1.25x) and the rest read it (0.1x),
  which is roughly a 5-10x cut on input cost for a several-hundred-job crawl.
  Each run prints a `[claude] ... % of cacheable prefix hit` line on exit —
  a 0% hit rate over a long run means something is invalidating the prefix.
  `CLAUDE_PROMPT_CACHE=0` disables it, `CLAUDE_CACHE_TTL=1h` buys the 1-hour
  cache (2x writes; only worth it if calls are >5 min apart),
  `CLAUDE_USAGE_SUMMARY=0` silences the summary line. Prompts below the
  model's floor (1024 tokens on Sonnet 5, 512 on Opus 5) simply don't cache,
  at no extra cost — today that's the tech-bar and company-mission prompts.
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
python run_scraper.py --track local_tech      # or --track remote_neural, or no flags = all tracks
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
python run_scraper.py --track remote_neural --preview        # read-only preview
python run_scraper.py --track remote_neural                    # crawl + persist
python run_scraper.py --track remote_neural --send             # email the digest
python run_scraper.py --track remote_neural --no-fit           # skip resume-fit scoring
python run_scraper.py --track remote_neural --no-websearch     # skip flaky DDG
```

### local-tech

Crawls every **active company in the store**, pulls their local (profile
`[locality]`) postings, drops clinical-ops and defense roles, keeps technical
titles, resume-fit-scores each new job in parallel, and writes a digest ranked
by a combined **√(resume-fit × company-mission)** score. Never emails.

```
python run_scraper.py --track local_tech [--top 20] [--workers 8]
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
python run_scraper.py --export-companies roster.json    # dump the roster (secrets-free)
python run_scraper.py --import-companies roster.json    # upsert a shared roster
```

`roster.json` is the diffable, shareable "starter set" that replaced the old
config seed lists — hand it to someone and they bootstrap instantly.

---

## Gated & big-company employers

Some employers can't be crawled directly. Route by type:

- **Secretly on a standard ATS** (e.g. NVIDIA on Workday) — just add the board:
  `python discover.py --add-board "NVIDIA" https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`.
- **Bot-gated custom sites** (Meta, Google, Qualcomm — no public feed):
  - **NLx feed** — `python run_scraper.py --nlx "Meta,Google,Qualcomm"`. Federal
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
python run_scraper.py --prune                     # deactivate dead (404) ATS boards
python run_scraper.py --prune --prune-offmission  # also drop off-mission "other" companies
```

`--prune` probes every active Greenhouse/Lever/Ashby/BambooHR board and
deactivates the dead ones — run it whenever the crawl starts spamming `HTTP 404`
(usually after a big discovery import leaves stale slugs). It never touches a
live board; `--prune-offmission` additionally retires `other`-tier companies
(keeping multi-division giants).

---

## Keyword & scoring utilities

```
python run_scraper.py --expand "eeg engineer"        # Claude: alt titles/keywords/sectors
python run_scraper.py --expand-location "NC"         # location synonym expansion
python run_scraper.py --keyword-report               # bulk-expand profile keywords
python run_scraper.py --score "job description..."   # technical-bar score one posting
python run_scraper.py --db alt.db ...                # isolated store (concurrent runs)
```

---

## Tests & CI

```
pip install -r requirements-dev.txt
pytest                                   # the whole suite (offline, ~8s)
pytest tests/test_store.py -k closed     # one area
```

The suite (`tests/`) is **offline by contract** — no network, no Claude API,
no writes to your real profile or store. Fixtures in `tests/conftest.py`
derive their inputs from whichever profile is loaded, so the same tests pass
on your `profile.toml` and on the shipped `profile.example.toml`; write tests
redirect `PROFILE_PATH`/`DATA_DIR` to a `tmp_path`.

`.github/workflows/ci.yml` runs **only around `main`** (PRs targeting it and
pushes to it — a three-OS Nuitka build is not cheap):

- **test** (Ubuntu) — asserts the run really is example-profile + API-free,
  then byte-compiles, lints (pyflakes), validates the example profile, runs
  pytest, checks every CLI `--help`, and boots the web app to exercise the
  API and the asset cache-busting.
- **build** (Ubuntu + Windows + macOS) — `python build_app.py`, then
  *launches the built binary* and requires its API to answer; a build that
  compiles but can't boot is not a pass. Artifacts upload per OS.

`python smoke_test.py` still works — it's a shim that runs pytest.

---

## Résumé-fit scoring

Each job gets a résumé-fit score in [0, 1] from a **multi-axis rubric**
(`core/fit.py`), not a single opaque number. The LLM scores four
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
anchor set — run `python -m core.fit` to print the predicted-vs-hand table
after retuning weights.

**Stored columns.** `resume_fit_score` is the combined scalar; the breakdown is
also stored per axis (`fit_domain`, `fit_function`, `fit_stack`,
`fit_seniority`), plus `fit_gates` (comma-joined) and a compact `fit_reason` tag,
so you can query and sort on any axis:

```
sqlite3 data/jobs.db "SELECT resume_fit_score, fit_domain, fit_stack, fit_gates, title \
  FROM jobs WHERE fit_gates IS NULL ORDER BY resume_fit_score DESC LIMIT 20"
```

**No description, no score.** A posting with no real JD body (under ~200 chars)
scores `None` and is left unranked, rather than floated at a fabricated mid
value — so fetch the bodies first.

### Re-scoring & description backfill

```
python run_scraper.py --track local_tech --backfill-descriptions            # fetch full JD text for stored Workday jobs (CXS)
python run_scraper.py --track local_tech --backfill-descriptions --limit 20 # try a small batch first
python run_scraper.py --track local_tech --rescore                          # re-score every stored job with the current rubric/profile
python run_scraper.py --track local_tech --rescore --described-only         # ...only jobs that already have a JD body
```

Workday serves each job's full description as plain JSON from
`/wday/cxs/{tenant}/{site}{externalPath}` — the live fetcher now pulls it, and
`--backfill-descriptions` fills it in for rows stored before that (idempotent;
only touches `myworkdayjobs.com` URLs missing a body). Run the backfill, then
`--rescore`: real text goes in, and the no-description rows that used to clog the
top are cleared out. Use `--rescore` after changing your resume, the `[fit]`
block, or the prompt — a normal crawl only scores jobs it hasn't seen.

### Deep verification & the watchlist

```
python run_scraper.py --track local_tech                     # crawl deep-verifies the top N before writing the digest
python run_scraper.py --track local_tech --no-verify         # skip the second pass
python run_scraper.py --track local_tech --verify-top 15     # re-verify the current top 15 (no crawl), rewrite digest
python run_scraper.py --watch "Ceribell"                     # never miss a new technical posting there
python run_scraper.py --unwatch "Ceribell"
```

Scoring is two-pass. The **screen** scores every new job once on up to
`config.MAX_DESC_CHARS` of text (one budget shared by fetchers, storage, and
the prompt; clipping keeps head + tail so a requirements block at the end of a
long JD survives). The **deep verify** pass then re-reads the finalists' full
postings (live detail fetch — Workday CXS / Greenhouse API / JSON-LD — over
stored text), first extracting hard requirements (years, seat type,
must-haves, candidate gaps) and only then re-scoring all axes and gates;
demotions re-rank and newly-promoted rows get verified too. Born of a real
failure: a 20k-char Ceribell "Sr Manager, Applied AI" JD scored 0.69 off its
EEG preamble while "8+ years program management + end-to-end GCP" sat past
every truncation cap — the fixed pipeline gates it as the management seat it
is (`management` gate, and the domain axis now scores the role's own work,
not the employer's halo).

**Watched companies** (`--watch`) get their whole board fetched every crawl,
and every new technical posting there is flagged in a dedicated digest
section regardless of fit rank or geography — the "wait for the right role
at this employer" list.

### Web UI

```
python webapp.py        ->  http://127.0.0.1:5533
```

Everything above in one local page (Flask + a single self-contained
[webapp/](webapp/) (templates/index.html + static/css+js), light/dark, no build step):

* **Tracks** — a header dropdown switches between the search tracks defined
  in `profile.toml [tracks.*]` (each bundles a DB, crawl engine, ranking
  knobs, and UI filter defaults). Every tab, stat, and operation re-scopes
  to the selected track.
* **Jobs** — the exact digest ranking (fit, combined, verified badge, axis
  meters, gates, age/NEW/stale chips), filters (search, min fit, geo bucket
  local/remote/**relocation**, a "willing to relocate" toggle, age,
  verified-only, watched-companies, include closed/decided), row expand for
  the full deep-verify reason + description, and one-click dispositions
  (dismiss/reject prompt for the --why note that teaches the scorer). The
  geo bucket is derived live from each posting's location against your
  `[locality]` config — jobs you'd have to move for stay hidden until you
  opt in.
* **Pipeline** — applied/interviewing/saved/rejected/dismissed groups, with
  the closed-after-you-applied flag and change/clear actions.
* **Companies** — roster with mission tier/score, open-job counts,
  active toggles, and the ★ watchlist; roster JSON export.
* **Operations** — crawl, status sync, deep-verify, stale-URL probe,
  rescore, description backfills (board + Workday CXS), NLx ingest,
  **company discovery** (directories + web search + a cached LLM
  name-brainstorm + the ATS-dork sweep; `[discovery] brainstorm_names`
  tunes the brainstorm, 0 disables), a standalone ATS dork sweep, mission
  scoring, prune, dedup, manual job add, and add-company-board — run as
  background tasks with the console streamed live into the page (one at a
  time; buttons lock while something runs). One **Crawl** command serves
  every track: a single pipeline (`scrapers/runner.py`) whose
  methodology — keyword handling (`keyword_mode`), source families
  (`sources`), gates (`require_core_anchor`, `geo_gate`), scoring budget
  (`verify_top`, `cost_guard`), and digest email — comes entirely from the
  track's `[tracks.*]` config, with sensible defaults per `engine`.
* **Settings** — edit `profile.toml` from the browser: chip-style editors
  for every keyword/exclude/location/locality list (global and per-track),
  numeric fit weights/gate penalties, per-track UI defaults, and a
  validated raw-TOML editor for everything else. Each save backs the file
  up to `config_backups/` (last 20 kept) and gracefully restarts the server
  so import-time config snapshots refresh — the page reconnects itself.

The UI talks to the same SQLite store and modules as the CLI, so the two are
interchangeable; start it from a shell where `ANTHROPIC_API_KEY` is set or
scoring operations will no-op (the header shows a warning).

**Launchers.** Double-click `run_webui.bat` to start from source (opens your
browser once the server is up). To ship the UI to a machine **without Python
or any pip installs**, run `build_exe.bat`: it compiles the whole app with
[Nuitka](https://nuitka.net) into a self-contained folder,
`webapp.dist\`, whose `JobCrawlerUI.exe` bundles CPython, Flask, the crawler
package, and lxml. The exe finds its data (`jobs.db`, `profile.toml`,
résumé, `job_reports\`) in this order: a `JOBS_DATA_DIR` env var if set; its
own folder when data already sits there; the folder **above** it when that
holds `jobs.db` (so a dist folder still inside this project uses the
project's real data rather than spawning a second empty DB); otherwise its
own folder, creating a fresh DB there — the copied-to-a-new-machine case.
The header of the UI always shows which DB file is live.
`ANTHROPIC_API_KEY` still comes from the environment. The only feature
missing from the compiled build is the optional Playwright headless-browser
probing for JS-only boards (it requires its own browser download and can't
ship inside an exe; those code paths degrade gracefully). First build
downloads a C compiler if none is present and takes 10–30 minutes; rebuilds
are fast.

### Dispositions — record your decisions, teach the scorer

```
python run_scraper.py --mark applied gh_beaconbiosignals_4361153009
python run_scraper.py --mark dismissed 6113860004 --why "TPM seat, wrong archetype"
python run_scraper.py --mark saved <url>          # shortlist; stays in the ranking
python run_scraper.py --mark clear <job>          # undo
python run_scraper.py --pipeline                  # everything you've dispositioned
```

`JOB` is a job_id, any unique fragment of one, or the posting URL.
Dispositions: `saved` (shortlisted — stays ranked), `applied` / `interviewing`
(move to the digest's **pipeline** section, which also flags when a posting
closes after you applied), `rejected` / `dismissed` (leave the ranking).
Rows out of the ranking are also skipped by `--rescore`, self-heal, and
verification — no API spend on decided jobs.

The feedback loop: your most recent applied/dismissed decisions are injected
into the fit-scoring prompt as few-shot calibration, with `--why` notes
verbatim — so "dismissed: TPM seat, wrong archetype" teaches the screen to
score the next such posting down. Tune the count via profile.toml `[fit]
disposition_examples` (0 disables).

### Posting dates & freshness

Every board fetch now captures the posting's real publish date
(`posted_at`: Greenhouse `first_published`, Lever `createdAt`, Ashby,
SmartRecruiters `releasedDate`, JSON-LD `datePosted`, Workday's relative
"Posted N Days Ago" — approximate, "30+" is a floor). The status sync
backfills dates for already-stored rows on every crawl, so coverage grows
with zero extra requests. SuccessFactors/custom boards publish no dates and
show `?`. The console top-N and the digest carry an **Age** tag: `NEW` =
first seen today, `Nd` = days since posted, `!` = 45+ days (often a ghost
req), `?` = unknown. `first_seen` (when the crawler noticed it) is
deliberately kept separate from `posted_at` (when it actually went up).

### Closed-job tracking

```
python run_scraper.py --track local_tech                        # crawl also syncs open/closed per board
python run_scraper.py --track local_tech --sync-status          # reconcile statuses only (no scoring/API) + rewrite digest
python run_scraper.py --track local_tech --check-closed         # probe rows no board has vouched for lately
python run_scraper.py --track local_tech --check-closed --stale-days 5 --limit 20
```

Every job row carries `status` (`open`/`closed`) and `closed_at`. The crawl
marks them itself: each successful, non-empty board fetch is treated as the
authoritative list of what that company currently posts — stored rows that
vanished from it are closed and rows that reappear are reopened. Rows whose
id came from the board itself match by **exact id only** (boards recycle
titles across requisitions, so one live "Algorithm Engineer" must not shield
five dead ones); externally-ingested rows (LinkedIn captures, `--add`) can
never id-match, so they match by URL/title and get a few days' grace before
closing. Empty or failed fetches never close anything, so a dead board can't
nuke its history. `--check-closed` covers what board fetches can't vouch for
— orphans, inactive companies, and boards that died or moved (a renamed
Greenhouse slug) — by probing each job URL for definite death signals
(404/410, "no longer accepting" notices, past JSON-LD `validThrough`, a
Workday CXS miss); indeterminate probes (e.g. bot-gated LinkedIn URLs) leave
rows open. `--sync-status` re-fetches every active board and reconciles
statuses without scoring anything — the cheap recovery pass.

Closed jobs drop out of `ranked_jobs()` (the top-N digest) automatically and
are skipped by `--rescore` and both description backfills, so no Claude API
or hydration spend goes to dead postings. Re-seeing a job anywhere — a crawl,
a re-capture, an ingest — reopens it.

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
| `[locality]` | what counts as "local" for the local track (`core/locality.py`) |
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
           -Argument "C:\Users\Jakda\git\Jobs\run_scraper.py --track local_tech" `
           -WorkingDirectory "C:\Users\Jakda\git\Jobs"
$trigger = New-ScheduledTaskTrigger -Daily -At "8:00AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable
Register-ScheduledTask -TaskName "Jobs Crawler" -Action $action -Trigger $trigger -Settings $settings -Force
```

(Or Task Scheduler GUI: daily trigger → program `python`, arguments
`run_scraper.py --track ...`, "Start in" = the repo folder. Existing tasks
that still point at `crawler.py` keep working — it's a deprecation shim that
forwards to `run_scraper.py` — but repoint them when convenient. Note
`run_scraper.py` with NO flags now refreshes EVERY configured track.)

---

## Data model & files

One SQLite store for everything — `data/jobs.db` (`core/store.py`). A track
can still get its own file via `[tracks.*].db`, but by default they share:

- **companies** — name, ats, slug / Workday triple / careers_url, NC job count,
  cached mission tier + score, `tags` (`neural`, `nc_local`, `multi_division` —
  which tracks crawl it), `source` (`discovery:<term>` / `local_sourcing` /
  `ats_dork` / `page_capture` / `manual` / `nlx`), `active` flag.
- **jobs** — stable job_id (dedup), `track`, geo_mode, remote/neural signals,
  description, résumé-fit score + reason, the per-axis breakdown (`fit_domain`,
  `fit_function`, `fit_stack`, `fit_seniority`, `fit_gates`), first/last seen.
  The combined résumé-fit×mission ranking score is computed at read time, not
  stored.
  `track` is a comma-separated **set**, not one value: a posting that belongs
  to two tracks is one row visible to both, and a second track's crawl adds
  its name rather than stealing the row (`store.track_set`).

Schema migrations are additive/automatic; old DBs upgrade in place (and shed
retired columns). All local data lives in the gitignored `data/` directory
(DBs, resume, `job_reports/*.md`, `captures/`, caches, profile backups);
override its location with the `JOBS_DATA_DIR` env var. `profile.toml`
stays at the app root beside `config.py`.

**One writer at a time:** SQLite locking does not span the boundary between your
shell and an agent sandbox mounting the same folder — two concurrent writers
corrupt the DB.

## Module map

| Module | Role |
|---|---|
| `run_scraper.py` / `webapp.py` | entry points: daily refresh + maintenance CLI, web UI launcher (`crawler.py` = deprecation shim) |
| `discover.py` / `capture.py` | entry points: roster growth, manual page capture |
| `config.py` / `profile.toml` | plumbing (config.py: paths, DATA_DIR, track parsing) vs. all search criteria (profile.toml) |
| `data/` | every local artifact: DBs, resume, `job_reports/`, `captures/`, caches, profile backups (gitignored; `JOBS_DATA_DIR` overrides) |
| `scrapers/runner.py` | THE crawl pipeline — one runner for every track, methodology from `[tracks.*]` |
| `scrapers/ops.py` | track-agnostic maintenance: status sync, deep-verify, closed-probe, rescore, backfills, ingest, manual adds |
| `scrapers/sources.py` | declarative ATS registry: store rows ↔ fetch thunks |
| `scrapers/fetchers/` | board fetchers (10 ATSes + RSS/HN/RemoteOK/Remotive/DDG/JSON-LD/sitemap + CareerOneStop/NLx); `workday.py` also pulls full JD text via the CXS per-job endpoint |
| `scrapers/fetchers/company.py` | company-vetted, location-scoped pulls + lazy description hydration + custom-board scraper |
| `scrapers/page_capture.py` | parse captured LinkedIn / Indeed / metacareers / any-board HTML |
| `discovery/` | pipeline, slug probes, careers-page sniffer, BCIWiki, local sourcing (+ web-search name harvest), dorking; `apply.py` upserts into the store |
| `core/store.py` | unified companies + jobs store (+ export/import, prune, `update_job_scores`) |
| `core/fit.py` | multi-axis résumé-fit rubric (axes + gates + deterministic combiner), templated from profile `[fit]`; calibration harness via `python -m core.fit` |
| `core/claude.py` | Claude API wrapper (prompt caching + token accounting) + discovery/expansion/mission/tech-bar prompts; `score_resume_fit` delegates to `fit.py` |
| `core/gates.py` / `core/digest_md.py` | config-driven title/exclude gates; ranked + matches digest renderers |
| `core/filters.py` / `remote_filter.py` / `locality.py` | keyword tiers, remote eligibility, locality — all driven by `profile.toml` |
| `webapp/` | Flask package: `routes.py`, `ops.py` (op registry), `server.py` (restart/ports), `templates/` + `static/` (the SPA) |
| `scrapers/parallel.py` | thread-pool source fetching (`CRAWLER_WORKERS`/`DISCOVERY_WORKERS` env) |
