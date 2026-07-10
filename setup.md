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

### Gated sites (LinkedIn / Indeed / Wellfound)

Automated login-session scraping was removed — see **Manual page capture**
at the bottom of this file: you browse logged in as yourself and send pages
to `capture.py` with one click (or Ctrl+S).

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


## Manual page capture (gated sites: LinkedIn / Indeed / anything)

Automated login-session scraping was removed (account-ban risk, bot
detection). Instead, you browse logged in as yourself and hand pages to the
crawler:

```
python capture.py                    # start the local capture server
```

Open `http://127.0.0.1:8877/` once to install the userscript (needs
Violentmonkey or Tampermonkey). Then on any LinkedIn/Indeed results or job
page, click the floating **-> Jobs** button: the page's live DOM is parsed
(site-specific selectors -> JSON-LD -> generic job links), postings are
exclude/technical-gated, resume-fit-scored, and written to the store; new
company names are recorded as inactive `page_capture` leads for a later
`python discover.py --local` pass to resolve.

No userscript? Save pages with Ctrl+S and run:

```
python capture.py "Saved Page.html" [more.html ...]
```
