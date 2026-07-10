# BCI Job Crawler — Setup Guide

## What this does
Polls ~20 target companies daily for new BCI/neuro/ML job postings matching your
profile. Sends a Gmail digest and saves a Markdown report to `job_reports/`.

Jobs are deduplicated via SQLite (`seen_jobs.db`), so you only get alerted to
genuinely new postings.

---

## Step 1 — Install Python dependencies

Open a terminal (PowerShell or Command Prompt) and run:

```
pip install -r requirements.txt
```

---

## Step 1.5 — Set your API keys as environment variables (recommended)

Hardcoding keys in `crawler.py` means they end up in git history. Prefer env
vars:

**PowerShell (persistent for your user):**
```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-…", "User")
[Environment]::SetEnvironmentVariable("GMAIL_APP_PASSWORD", "abcdefghijklmnop", "User")
```

**PowerShell (current session only):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-…"
$env:GMAIL_APP_PASSWORD = "abcdefghijklmnop"
```

You can also override the model used for Claude calls:
```powershell
$env:CLAUDE_MODEL = "claude-sonnet-4-6"
```

---

## Step 2 — Create a Gmail App Password

Google requires an App Password (not your regular password) for SMTP.

1. Go to your Google Account → **Security**
2. Make sure **2-Step Verification** is ON (required)
3. Search for **"App passwords"** in the search bar at the top of the page
4. Select app: **Mail** — Select device: **Windows Computer**
5. Click **Generate**
6. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

Set the app password as an env var (see Step 1.5) or, if you must, open
`crawler.py` and replace the `GMAIL_APP_PASSWORD` fallback.

---

## Step 3 — Test it

Run a dry-run first (no email, no DB writes, just prints what it finds):

```
python crawler.py --dry-run
```

Then run for real:

```
python crawler.py
```

Check your inbox and the `job_reports/` folder next to the script.

---

## Step 4 — Schedule with Windows Task Scheduler

This makes the script run automatically every day (e.g. 8 AM).

### Option A — Quickest: one PowerShell command

Open **PowerShell as Administrator** and run (edit the path to match where you saved the script):

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
           -Argument "C:\Users\YourName\bci_crawler\crawler.py" `
           -WorkingDirectory "C:\Users\YourName\bci_crawler"

$trigger = New-ScheduledTaskTrigger -Daily -At "8:00AM"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
            -StartWhenAvailable

Register-ScheduledTask -TaskName "BCI Job Crawler" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest -Force
```

Replace `C:\Users\YourName\bci_crawler` with the actual folder path.

### Option B — GUI

1. Open **Task Scheduler** (search in Start menu)
2. Click **Create Basic Task…**
3. Name: `BCI Job Crawler`
4. Trigger: **Daily**, time: **8:00 AM**
5. Action: **Start a program**
   - Program: `python`
   - Arguments: `crawler.py`
   - Start in: `C:\path\to\your\folder`
6. Finish → right-click the task → **Properties** → **Conditions tab**:
   - Uncheck "Start the task only if the computer is on AC power"
   - Check "Wake the computer to run this task" (optional)

---

## Customizing targets

**Add a Greenhouse company:**
Find the company's Greenhouse slug from their jobs URL:
`https://boards.greenhouse.io/SLUG` → add `"SLUG": "Company Name"` to `GREENHOUSE_COMPANIES`

**Add a Lever company:**
`https://jobs.lever.co/SLUG` → add `"SLUG": "Company Name"` to `LEVER_COMPANIES`

**Add keywords:**
Edit `INCLUDE_KEYWORDS` in the script.

**Add PhD filter phrases:**
Edit `EXCLUDE_PHRASES` (currently catches "PhD required", "Ph.D. required", etc.)

**Filter by location:**
Edit `LOCATION_INCLUDE` and `LOCATION_EXCLUDE`. A job passes the location
filter only if its location matches at least one `LOCATION_INCLUDE` entry
(or the list is empty) AND doesn't match any `LOCATION_EXCLUDE` entry.

---

## New CLI flags

| Flag | Purpose |
|------|---------|
| `--expand TERM` | Print alternative titles/keywords/sectors for a term, then exit. |
| `--expand-live TERM` | Same, but fold the suggestions into this run's `INCLUDE_KEYWORDS`. |
| `--expand-location TERM` | Print related locations for a term (e.g. "NC" → Durham/Raleigh/RTP), then exit. |
| `--expand-location-live TERM` | Same, but fold into this run's `LOCATION_INCLUDE`/`LOCATION_EXCLUDE`. |
| `--keyword-report` | Bulk-expand every existing `INCLUDE_KEYWORDS` entry and write a suggestions markdown report. |

---

## Company discovery — `discover.py`

Separate script that finds *new* companies to crawl:

```
python discover.py "neurotech startups"
python discover.py "medical imaging ML companies"
python discover.py --from-keywords      # run discovery for every INCLUDE_KEYWORDS entry
```

For each suggestion, it asks an ATS (Greenhouse / Lever / Ashby / Kula) if
the slug exists. Confirmed slugs get written into a markdown report as
ready-to-paste dict entries for `crawler.py`.

### Credentials for gated sites — two options

**Option A — session capture (preferred).**
Opens a real browser, you log in normally (2FA, captchas, device
challenges — all handled by you, not the script). When you're done,
the script saves the browser's cookies + localStorage to
`sessions/<site>.json`. Your password never touches disk.

```
pip install playwright playwright-stealth
playwright install chrome       # recommended — real Chrome bypasses Cloudflare
playwright install chromium     # fallback (test build — often blocked)
playwright install firefox      # alternative engine

python discover.py --capture-session linkedin
python discover.py --capture-session indeed
python discover.py --capture-session wellfound

python discover.py --list-sessions          # show captured sessions + age
python discover.py --test-session linkedin  # verify session is still valid
```

Supported sites: `linkedin`, `indeed`, `wellfound`. `sessions/` is
gitignored by default.

**Cloudflare error 300031 / Turnstile failing?** Playwright's bundled
Chromium has a "HeadlessChrome" build signature that Cloudflare flags on
sites like LinkedIn. Use `--browser chrome` (the default) to launch your
real installed Chrome instead — that's what gets past most Turnstile
challenges. If Chrome still fails, try Firefox:

```
python discover.py --capture-session linkedin --browser chrome    # default
python discover.py --capture-session linkedin --browser firefox   # alt
python discover.py --capture-session linkedin --browser chromium  # test build
```

**Still failing? Use a COPY of your REAL Chrome profile (`--use-profile`).**

This makes a copy of your live Chrome profile (cookies, history,
extensions, signed-in state) at `sessions/chrome-profile/` and launches
Chrome against the copy. Why a copy? Chrome refuses to enable its remote
debugging interface on the default user-data dir — Playwright can't
control a Chrome instance running on your live profile directly.

Cloudflare still sees the cookies / history / trust signal from your real
profile, just routed through the copy. This is the strongest bypass
short of a residential proxy.

```
# Quit Chrome FULLY first (check the system tray). Then:
python discover.py --capture-session linkedin --use-profile
```

The copy is created on first run (10-60s, depending on profile size) and
reused on subsequent runs. To re-copy from your live Chrome (e.g. after
logging into a new site in regular Chrome):

```
python discover.py --capture-session linkedin --use-profile --refresh-profile
```

If you use multiple Chrome profiles, pick the one already signed into the
target site:

```
python discover.py --capture-session linkedin --use-profile --profile-directory "Profile 1"
```

Override the user-data dir if yours isn't at the standard Windows location
(`%LOCALAPPDATA%\Google\Chrome\User Data`):

```
python discover.py --capture-session linkedin --use-profile --user-data-dir "D:\chrome-data"
```

**Gotchas:**
- Chrome must be fully quit. If you see "ProcessSingleton" or "user data
  directory is already in use", Chrome is still running somewhere. Kill it.
- Once you've captured a session JSON, you don't need `--use-profile` for
  `--test-session` or automated fetches. The saved cookies work with plain
  ephemeral Chrome.
- `--use-profile` forces `--browser chrome` (persistent context only works
  with the chromium-based engine).

**Option B — paste raw cookies (legacy).**
If you already have cookie values from DevTools:

```
python discover.py --credentials-init
python discover.py --credentials-check
```

`credentials.json` is gitignored by default.

**Fair warning (applies to both options):** automated access to
LinkedIn / Indeed / Glassdoor / Wellfound violates their ToS and can get
your account suspended or banned. Session capture protects your password
but does NOT reduce the ToS risk. Run sparingly and space requests out
(1 req / 5–15 s with jitter is a reasonable starting point).

---

## Files created at runtime

| File | Purpose |
|------|---------|
| `seen_jobs.db` | SQLite DB tracking jobs already reported |
| `job_reports/jobs_YYYY-MM-DD.md` | Daily markdown report |
| `job_reports/keyword_expansion_YYYY-MM-DD.md` | `--keyword-report` output |
| `job_reports/discovery_*.md` | `discover.py` output |

To reset and re-alert on all current postings, delete `seen_jobs.db`.

---

## Current target companies

### Greenhouse (reliable JSON API)
- Neuralink
- NeuroPace
- Kitware
- Beacon Biosignals
- Forest Neurotech
- Kernel
- Cognixion
- Biogen
- Align Technology
- RTI International
- Cogstate
- United Therapeutics

### Lever (reliable JSON API)
- Neurable
- Paradromics
- Sciome LLC
- IQVIA

### Custom HTML scrape (less reliable)
- Zyphra
- Meta Reality Labs (partial — their site has anti-bot measures)

**Note on Meta:** Their careers site blocks automated access aggressively.
The scraper will attempt a best-effort fetch but may fail silently.
Recommended: check https://metacareers.com/jobs manually and set a keyword alert
for "Research Engineer" + "Reality Labs" on LinkedIn.

### SuccessFactors (HTML scrape, paginated)
- Duke University — `https://careers.duke.edu`
- Duke Health — `https://careers.dukehealth.org`

Both are scraped via `/search/?startrow=N&pageSize=100`. The location filter
keeps the results in NC/VA/remote; the include-keyword list is the primary
relevance gate.

**Add another SF site:** append `(company_name, base_url)` to
`SUCCESSFACTORS_COMPANIES` in `crawler.py`. The base URL is the root of the
career site (no trailing `/search/`).

### Workday (JSON POST API)
- **UNC Health — not yet wired up.** Their public portal
  `jobs.unchealthcare.org` is behind Cloudflare, which blocks automated
  tenant discovery. To add it:
  1. Open `https://jobs.unchealthcare.org` in a browser.
  2. Open DevTools → Network tab, click a job.
  3. Find a request like
     `https://{TENANT}.wd{N}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}/jobs`.
  4. Uncomment and fill the tuple in `WORKDAY_COMPANIES` in `crawler.py`.

Add any Workday site the same way:
`(tenant, pod_number, site, "Company Name")` → append to `WORKDAY_COMPANIES`.

### PeopleAdmin (Atom feed)
- UNC Chapel Hill — `unc.peopleadmin.com`

Add a PeopleAdmin site by appending `(host, "Company Name")` to
`PEOPLEADMIN_COMPANIES` in `crawler.py`. The scraper reads
`/postings/search.atom`.

---

## Unified track architecture (post-merge)

The two development branches — `track-remote-neural` (BCI constraint kept,
location relaxed to remote) and `track-local-clinical-ml` (location kept to
the Triangle, BCI relaxed to health/bio/science) — are merged into one
project with pluggable *tracks* over shared machinery.

### Running a track

```
python crawler.py --track remote-neural [--commit] [--send] [--fit] [--no-websearch]
python crawler.py --track local-tech    [--top 20] [--workers 8]
python crawler.py                       # classic keyword crawl + email
```

Both tracks default to a read-only preview (no email); `--commit` persists
matches, `--send` emails the tagged digest (remote-neural only).

### Shared machinery

- **`jobcrawler/store.py`** — ONE SQLite store (`local_tech.db`):
  `companies` (cached LLM mission score, scope tags `neural` / `nc_local`)
  + `jobs` (dedup state, per-track fields, technical-bar and resume-fit
  scores). Old DBs migrate in place; `jobcrawler/db.py` adapts legacy
  callers. Pre-merge `seen_jobs_*.db` files are not imported — the first
  run re-surfaces previously seen postings once.
- **`jobcrawler/fetchers/`** — keyword-gated board fetchers (Greenhouse,
  Lever, Ashby, Kula, JazzHR, BambooHR, ADP, Workday, SuccessFactors,
  PeopleAdmin, RSS, HN, RemoteOK, Remotive, DDG/JSON-LD) plus
  `fetchers/company.py`: company-vetted, location-scoped pulls (adds
  SmartRecruiters + iCIMS + custom careers boards + lazy description
  hydration) used by the local track.
- **`jobcrawler/discovery/`** — Claude-driven discovery, BCIWiki sweeps
  (`discover.py --from-bciwiki`), NC local sourcing (`discover.py --local`),
  ATS dorking (`discover.py --dork`); all share `discovery/sniffer.py`
  (single careers-page ATS sniffer: fetchable coordinates, confirm-by-live-
  count, detection-only leads, headless-browser fallback) and the parallel
  validation pipeline with VERIFY flags.
- **Scorers** (`jobcrawler/claude.py`) — technical-bar, company-mission and
  resume-fit scorers are available to every track; `--fit` ranks the
  remote-neural digest by resume fit.
- **Parallelism** (`jobcrawler/parallel.py`) — every track fetches sources
  on a thread pool (n_cpus-1 default; `CRAWLER_WORKERS`/`DISCOVERY_WORKERS`
  env to raise).

### Company roster

Companies live in the store, scoped by tags. `config.py`'s lists remain the
human-reviewable seeds; load them with:

```
python crawler.py --import-seeds     # config lists -> store (neural / nc_local)
python discover.py --local           # NC sourcing pass -> store (nc_local)
python discover.py --from-bciwiki --apply   # BCIWiki -> config lists
```

The remote-neural track sweeps store companies tagged `neural` (config lists
as fallback/seed); the local track crawls all active store companies with an
NC-scoped pull.

### Pivoting

To pivot the search: run the other track. Both mutate the shared keyword
filter *in-process only* (`tracks/*.apply_to_config`), so nothing on disk
changes and tracks can run back to back or concurrently (same store; SQLite
handles the locking on short transactions).
