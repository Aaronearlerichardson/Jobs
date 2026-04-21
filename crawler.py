#!/usr/bin/env python3
"""
BCI Job Crawler for Aaron Earle-Richardson
Polls verified company career boards for new relevant postings.
Sends Gmail digest and saves Markdown report.

Usage:
    python crawler.py                         # full run
    python crawler.py --dry-run               # print only, no DB/email
    python crawler.py --expand "eeg engineer" # print expanded titles/keywords/sectors
    python crawler.py --expand-location "NC"  # expand a location term
    python crawler.py --keyword-report        # bulk-expand all INCLUDE_KEYWORDS
"""

import argparse
import json
import os
import sqlite3
import smtplib
import requests
import re
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import urljoin

# =========================================================================
#  CONFIG
#  Secrets are read from environment variables first, then fall back.
#  Prefer env vars - hardcoded keys get committed to git.
#
#  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
#  cmd.exe:     set ANTHROPIC_API_KEY=sk-ant-...
#  bash/zsh:    export ANTHROPIC_API_KEY=sk-ant-...
# =========================================================================

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "jakdaxter31@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "YOUR_APP_PASSWORD_HERE")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_API_KEY_HERE")
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL",       "claude-sonnet-4-6")

SCRIPT_DIR = Path(__file__).parent
DB_PATH    = SCRIPT_DIR / "seen_jobs.db"
REPORT_DIR = SCRIPT_DIR / "job_reports"

# =========================================================================
#  KEYWORD FILTERS
# =========================================================================

INCLUDE_KEYWORDS = [
    "bci", "brain-computer", "brain computer",
    "neural interface", "neural decoding", "neuroprosthetic",
    "closed-loop", "cortical", "electrophysiology",
    "eeg", "ecog", "ieeg", "lfp", "spike sorting",
    "fnirs", "meg", "emg", "neurotech", "neurostimulation",
    "neuroscience", "neuroimaging", "computational neuroscience",
    "neuroscientist", "neural",
    "pytorch", "signal processing", "research engineer",
    "scientific software", "scientific computing", "data pipeline",
    "biomedical engineer", "fmri", "biomedical", "mri", "medical device",
    "real-time", "embedded software", "data manager",
    "signal processing", "MNE-Python", "wearable", "numpy", "backend"
]

EXCLUDE_PHRASES = [
    "phd required", "ph.d. required", "doctoral degree required",
    "must have a phd", "requires a phd", "ph.d is required",
    "postdoc position", "post-doctoral", "postdoctoral position",
]

# --- Location filtering ---
# A job passes only if:
#   - LOCATION_INCLUDE is empty OR location matches at least one entry, AND
#   - location does not match any LOCATION_EXCLUDE entry.
# Lowercase substring matching.
LOCATION_INCLUDE = [
    # e.g. "remote", "united states", "usa",
    # "new york", "boston", "san francisco", "bay area",
    "research triangle", "durham", "raleigh", "chapel hill", "rtp", "carrboro", "nc", "cary", "apex", "north carolina",
    "charlotte", "greensboro", "winston-salem", "asheville", "richmond", "virginia", "va",
    "remote", "work from home", "wfh", "fully remote", "distributed", "anywhere", "mid atlantic"
]

LOCATION_EXCLUDE = [
    # e.g. "india", "china", "russia",
    # "uk only", "eu only", "europe only",
]

# =========================================================================
#  TARGET COMPANIES
# =========================================================================

GREENHOUSE_COMPANIES = {
    "neuralink":        "Neuralink",
    "beaconbiosignals": "Beacon Biosignals",
    "neuropace":        "NeuroPace",
}

LEVER_COMPANIES = {
    "kitware": "Kitware",
}

ASHBY_COMPANIES = {
}

KULA_COMPANIES = [
    ("Precision Neuroscience", "precision-neuroscience"),
]

DISCOURSE_BOARDS = [
    ("MNE Forum Jobs",          "https://mne.discourse.group", 9),
    ("Neurostars Announcements","https://neurostars.org",       6),
]

CUSTOM_COMPANIES = [
]

# SuccessFactors career sites (HTML scrape).
# Tuple: (company_name, base_url).  The scraper appends /search/ + paging.
SUCCESSFACTORS_COMPANIES = [
    ("Duke University", "https://careers.duke.edu"),
    ("Duke Health",     "https://careers.dukehealth.org"),
]

# Workday career sites (JSON POST API).
# Tuple: (tenant, wd_pod, site, company_name).
#   tenant  = subdomain before .wd{N}.myworkdayjobs.com  (e.g. "unchealth")
#   wd_pod  = the "wdN" number (1, 5, 12, ...) in the full hostname
#   site    = path segment for the career site  (e.g. "External_Careers")
#
# UNC Health: jobs.unchealthcare.org is Cloudflare-protected, which blocks
# automated tenant-discovery.  To wire it up, open the site in a browser,
# watch the Network tab for a request like:
#     https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
# and paste the pieces into the tuple below.  Known-wrong guesses that 422:
# tenant=unchealth/pod=1/site=External_Careers, UNCHealthCareers, External.
WORKDAY_COMPANIES = [
    # ("<tenant>", <pod>, "<site>", "UNC Health"),
]

# PeopleAdmin career sites (Atom feed).
# Tuple: (host, company_name).
PEOPLEADMIN_COMPANIES = [
    ("unc.peopleadmin.com", "UNC Chapel Hill"),
]

# =========================================================================
#  DATABASE
# =========================================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id     TEXT PRIMARY KEY,
            company    TEXT,
            title      TEXT,
            url        TEXT,
            location   TEXT,
            first_seen TEXT
        )
    """)
    conn.commit()
    return conn

def is_new(conn, job_id):
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
    ).fetchone() is None

def mark_seen(conn, job):
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs VALUES (?,?,?,?,?,?)",
        (job["id"], job["company"], job["title"],
         job["url"], job["location"], datetime.now().isoformat())
    )
    conn.commit()

# =========================================================================
#  FILTERING
# =========================================================================

def is_relevant(title, description=""):
    combined = (title + " " + description).lower()
    if any(p in combined for p in EXCLUDE_PHRASES):
        return False
    return any(kw in combined for kw in INCLUDE_KEYWORDS)


def is_location_allowed(location):
    """
    True if location passes LOCATION_INCLUDE/LOCATION_EXCLUDE.
    Empty/unknown locations are treated as allowed.
    """
    if not location:
        return True
    loc = location.lower()
    if any(bad.lower() in loc for bad in LOCATION_EXCLUDE):
        return False
    if LOCATION_INCLUDE:
        return any(good.lower() in loc for good in LOCATION_INCLUDE)
    return True

# =========================================================================
#  FETCHERS
# =========================================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_greenhouse(slug, company_name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Greenhouse {company_name}: {e}")
        return []
    jobs = []
    for job in r.json().get("jobs", []):
        title = job.get("title", "")
        jid   = str(job.get("id", ""))
        jurl  = job.get("absolute_url", "")
        loc   = job.get("location", {}).get("name", "Unknown")
        desc  = BeautifulSoup(job.get("content", ""), "html.parser").get_text(" ")[:600]
        dept  = " ".join(d.get("name", "") for d in job.get("departments", []))
        if is_relevant(f"{title} {dept}", desc):
            jobs.append({"id": f"gh_{slug}_{jid}", "company": company_name,
                         "title": title, "url": jurl, "location": loc, "description": desc})
    return jobs


def fetch_lever(slug, company_name):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Lever {company_name}: {e}")
        return []
    jobs = []
    for job in r.json():
        title = job.get("text", "")
        jid   = job.get("id", "")
        jurl  = job.get("hostedUrl", "")
        loc   = job.get("categories", {}).get("location", "Unknown")
        team  = job.get("categories", {}).get("team", "")
        desc  = (job.get("descriptionPlain") or "")[:600]
        if is_relevant(f"{title} {team}", desc):
            jobs.append({"id": f"lv_{slug}_{jid}", "company": company_name,
                         "title": title, "url": jurl, "location": loc, "description": desc})
    return jobs


def fetch_ashby(slug, company_name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Ashby {company_name}: {e}")
        return []
    jobs = []
    for job in r.json().get("jobPostings", []):
        title   = job.get("title", "")
        jid     = job.get("id", "")
        jurl    = job.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{jid}"
        loc     = job.get("location", "Unknown") or "Unknown"
        dept    = job.get("departmentName", "")
        desc    = job.get("descriptionPlain", "") or ""
        if is_relevant(f"{title} {dept}", desc[:600]):
            jobs.append({"id": f"ashby_{slug}_{jid}", "company": company_name,
                         "title": title, "url": jurl, "location": loc, "description": desc[:600]})
    return jobs


def fetch_kula(company_name, kula_slug):
    base_url = f"https://careers.kula.ai/{kula_slug}"
    try:
        r = requests.get(base_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Kula {company_name}: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    apply_links = soup.find_all("a", href=re.compile(rf"/{re.escape(kula_slug)}/\d+"))
    jobs = []
    for a in apply_links:
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin("https://careers.kula.ai", href)
        jid = re.search(r"/(\d+)/?$", href)
        if not jid:
            continue

        parent = a.parent
        lines = []
        for _ in range(8):
            raw = parent.get_text("\n").strip()
            lines = [l.strip() for l in raw.split("\n") if len(l.strip()) > 3]
            if len(lines) >= 2:
                break
            parent = parent.parent

        title = lines[1] if len(lines) > 1 else lines[0] if lines else "Unknown"
        dept  = lines[0] if len(lines) > 1 else ""
        loc   = lines[2] if len(lines) > 2 else "See posting"

        if is_relevant(f"{title} {dept}"):
            jobs.append({
                "id": f"kula_{kula_slug}_{jid.group(1)}",
                "company": company_name,
                "title": title,
                "url": href,
                "location": loc.split(";")[0].strip(),
                "description": "",
            })
    return jobs


def fetch_discourse(display_name, base_url, category_id):
    url = f"{base_url}/c/job-opportunities/{category_id}.json"
    dsc_headers = {**HEADERS, "Accept": "application/json"}
    try:
        r = requests.get(url, timeout=20, headers=dsc_headers)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Discourse {display_name}: {e}")
        return []

    topics = r.json().get("topic_list", {}).get("topics", [])
    jobs = []
    for t in topics:
        if t.get("posts_count", 0) == 1 and t.get("reply_count", 0) == 0:
            continue
        title = t.get("title", "")
        slug  = t.get("slug", "")
        tid   = t.get("id", "")
        jurl  = f"{base_url}/t/{slug}/{tid}"
        loc   = t.get("last_posted_at", "")[:10] if t.get("last_posted_at") else "See post"
        if is_relevant(title):
            jobs.append({
                "id":          f"discourse_{base_url.split('.')[0].split('//')[1]}_{tid}",
                "company":     display_name,
                "title":       title,
                "url":         jurl,
                "location":    f"Posted {loc}",
                "description": "",
            })
    return jobs


def fetch_custom(company_name, page_url, css_selector=None):
    try:
        r = requests.get(page_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Custom {company_name}: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    anchors = soup.select(css_selector) if css_selector else [
        a for a in soup.find_all("a", href=True)
        if is_relevant(a.get_text(strip=True))
    ]
    jobs, seen = [], set()
    for a in anchors:
        title = a.get_text(strip=True)
        href  = a.get("href", "")
        if not href.startswith("http"):
            href = urljoin(page_url, href)
        if len(title) < 5 or href in seen or not is_relevant(title):
            continue
        seen.add(href)
        jobs.append({
            "id": f"custom_{company_name.replace(' ','_')}_{abs(hash(href))}",
            "company": company_name, "title": title,
            "url": href, "location": "See posting", "description": "",
        })
    return jobs


def fetch_successfactors(company_name, base_url, step=25, max_pages=80):
    """
    Scrape a SuccessFactors career site (e.g. careers.duke.edu,
    careers.dukehealth.org).  SF serves ~25 jobs per HTML page at
    /search/?startrow=N.  Each tile has two anchors (image + title) so we
    deduplicate by URL.  We stop when a page contributes zero new URLs.
    """
    jobs, seen = [], set()
    sf_headers = {**HEADERS, "Accept": "text/html"}
    for page in range(max_pages):
        startrow = page * step
        url = f"{base_url.rstrip('/')}/search/?startrow={startrow}"
        try:
            r = requests.get(url, timeout=25, headers=sf_headers)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] SuccessFactors {company_name} p{page}: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        # SF job tiles have class "jobTitle-link" on the <a>; fall back to
        # any anchor whose href points at /job/.
        anchors = soup.select("a.jobTitle-link") or [
            a for a in soup.find_all("a", href=True) if "/job/" in a["href"]
        ]
        if not anchors:
            break

        new_on_page = 0
        for a in anchors:
            href = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            if href in seen:
                continue
            seen.add(href)

            title = a.get_text(strip=True)
            # Location usually lives in a sibling/nearby element; walk up and
            # grab the row text as a fallback.
            loc = "See posting"
            row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("div")
            if row is not None:
                text = row.get_text(" ", strip=True)
                m = re.search(
                    r"(Durham|Chapel Hill|Raleigh|Research Triangle|RTP|"
                    r"Carrboro|Cary|Morrisville|Charlotte|Greensboro|"
                    r"Winston[- ]Salem|Asheville|North Carolina|NC|"
                    r"Remote|Virginia|VA|Richmond)[^|\n]{0,40}",
                    text, flags=re.I,
                )
                if m:
                    loc = m.group(0).strip(" ,-")

            jid_m = re.search(r"/job/([^/?#]+)", href)
            jid = jid_m.group(1) if jid_m else str(abs(hash(href)))
            new_on_page += 1
            if is_relevant(title):
                jobs.append({
                    "id":          f"sf_{company_name.replace(' ','_')}_{jid}",
                    "company":     company_name,
                    "title":       title,
                    "url":         href,
                    "location":    loc,
                    "description": "",
                })

        # If this page added no new URLs, we've hit the end (or SF is cycling
        # the same result set regardless of startrow).
        if new_on_page == 0:
            break
        time.sleep(0.3)

    return jobs


def fetch_workday(tenant, wd_pod, site, company_name, page_size=20, max_pages=25):
    """
    Poll a Workday career site via the JSON POST endpoint.
      POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
      body: {"appliedFacets":{},"limit":N,"offset":K,"searchText":""}
    Returns at most page_size * max_pages postings.
    """
    host = f"https://{tenant}.wd{wd_pod}.myworkdayjobs.com"
    api  = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    # External link pattern: {host}/en-US/{site}{externalPath}
    link_base = f"{host}/en-US/{site}"

    wd_headers = {
        **HEADERS,
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }

    jobs = []
    for page in range(max_pages):
        offset = page * page_size
        body = {
            "appliedFacets": {},
            "limit":         page_size,
            "offset":        offset,
            "searchText":    "",
        }
        try:
            r = requests.post(api, json=body, timeout=25, headers=wd_headers)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] Workday {company_name} p{page}: {e}")
            break

        try:
            data = r.json()
        except ValueError:
            print(f"    [!] Workday {company_name}: non-JSON response")
            break

        postings = data.get("jobPostings", []) or []
        if not postings:
            break

        for p in postings:
            title  = p.get("title", "") or ""
            path   = p.get("externalPath", "") or ""
            jurl   = f"{link_base}{path}" if path else host
            loc    = p.get("locationsText", "") or "Unknown"
            posted = p.get("postedOn", "") or ""
            jid    = path.rsplit("/", 1)[-1] if path else str(abs(hash(title + loc)))
            desc   = posted  # we don't fetch the full description page
            if is_relevant(title, desc):
                jobs.append({
                    "id":          f"wd_{tenant}_{jid}",
                    "company":     company_name,
                    "title":       title,
                    "url":         jurl,
                    "location":    loc,
                    "description": desc,
                })

        if len(postings) < page_size:
            break
        time.sleep(0.5)

    return jobs


def fetch_peopleadmin(host, company_name):
    """
    Scrape a PeopleAdmin career site via its Atom feed:
      https://{host}/postings/search.atom
    Entries have <title>, <link rel="alternate">, <summary>, <updated>.
    """
    url = f"https://{host}/postings/search.atom"
    try:
        r = requests.get(url, timeout=25, headers={**HEADERS, "Accept": "application/atom+xml"})
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] PeopleAdmin {company_name}: {e}")
        return []

    soup = BeautifulSoup(r.text, "xml")
    entries = soup.find_all("entry")
    jobs = []
    for e in entries:
        title_el = e.find("title")
        title    = title_el.get_text(strip=True) if title_el else ""
        link_el  = e.find("link")
        jurl     = link_el.get("href") if link_el and link_el.has_attr("href") else ""
        if not jurl or not title:
            continue
        summary  = e.find("summary")
        desc_raw = summary.get_text(" ", strip=True) if summary else ""
        desc     = desc_raw[:600]

        # PeopleAdmin summaries often embed location text; try to pull NC/VA
        # mentions for our location filter, else fall back.
        loc = "See posting"
        m = re.search(
            r"(Chapel Hill|Durham|Raleigh|Carrboro|Research Triangle|RTP|"
            r"Charlotte|Greensboro|Winston[- ]Salem|Asheville|"
            r"North Carolina|NC|Remote)[^|\n]{0,40}",
            desc_raw, flags=re.I,
        )
        if m:
            loc = m.group(0).strip(" ,-")

        jid_m = re.search(r"/postings/(\d+)", jurl)
        jid   = jid_m.group(1) if jid_m else str(abs(hash(jurl)))
        if is_relevant(title, desc):
            jobs.append({
                "id":          f"pa_{host.split('.')[0]}_{jid}",
                "company":     company_name,
                "title":       title,
                "url":         jurl,
                "location":    loc,
                "description": desc,
            })
    return jobs

# =========================================================================
#  CLAUDE EXPANSION
# =========================================================================

_BCI_EXPAND_SYSTEM = """You are a job search strategist specializing in neurotechnology and BCI (brain-computer interface) careers. The user does NOT have a PhD - avoid suggesting roles that require one (Research Scientist at most companies requires a PhD). The user has: extensive PyTorch experience, 7+ years of EEG/ECoG/iEEG signal processing pipeline development, authored the sliceTCA tensor decomposition library for neural data, contributed to MNE-Python, BME background with medical device hardware experience, and a BCI paper in preparation.

Given a job title, skill, or BCI concept, return ONLY a JSON object with exactly three keys:
- "titles": array of up to 12 alternative job title strings to search for. Prioritize non-PhD tracks: Research Engineer, ML Engineer, Software Engineer (Neuro/BCI), Applied Scientist, Signal Processing Engineer, Neurotech Engineer, Systems Engineer, Data Scientist. Avoid "Research Scientist" unless it is documented that the role does not require a PhD.
- "keywords": array of up to 12 technical keywords, skills, or domain terms to include in job searches that will surface more relevant listings.
- "sectors": array of up to 12 specific company types, industry verticals, or named employers/labs where these roles exist without PhD requirements.
Return ONLY valid JSON. No markdown, no explanation, no preamble."""


def _call_claude_json(system_prompt, user_content, max_tokens=1000):
    """
    Shared helper: POST to /v1/messages, parse out the JSON block from
    the text response, return it as a dict. Returns {} on any failure.
    """
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY_HERE":
        print("  [!] Set ANTHROPIC_API_KEY env var (or edit crawler.py).")
        return {}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "system":     system_prompt,
                "messages":   [{"role": "user", "content": user_content}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = next(
            (b["text"] for b in r.json().get("content", []) if b.get("type") == "text"),
            "{}"
        )
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except requests.HTTPError as e:
        body = getattr(e.response, "text", "")[:300]
        print(f"  [!] Claude API error: {e}  body={body!r}")
        return {}
    except json.JSONDecodeError as e:
        print(f"  [!] Claude returned non-JSON: {e}")
        return {}
    except Exception as e:
        print(f"  [!] Claude call failed: {e}")
        return {}


def expand_search(term):
    return _call_claude_json(_BCI_EXPAND_SYSTEM, term)


_LOCATION_EXPAND_SYSTEM = """You are a geographic search strategist. Given a location term (a city, region, country, or qualifier like "remote"), return ONLY a JSON object with exactly two keys:
- "include": array of up to 15 related location strings that should ALSO match when filtering jobs for this area. Examples: for "North Carolina", include "NC", "Durham", "Raleigh", "Chapel Hill", "Research Triangle", "RTP". For "remote", include "work from home", "wfh", "fully remote", "distributed", "anywhere".
- "exclude": array of up to 8 location strings that should be explicitly excluded when someone specifies this search. Examples: for "us only", include common offshore locations the user likely wants to filter out.
Use lowercase unless the token is normally capitalized (country codes etc). Return ONLY valid JSON, no markdown, no explanation."""


def expand_location(term):
    return _call_claude_json(_LOCATION_EXPAND_SYSTEM, term)


def print_expansion(term, expanded):
    w = 62
    bar = "=" * w
    print(f"\n{bar}")
    print(f"  BCI Expansion: '{term}'")
    print(f"{bar}")

    titles   = expanded.get("titles",   [])
    keywords = expanded.get("keywords", [])
    sectors  = expanded.get("sectors",  [])

    print(f"\n  JOB TITLES TO SEARCH ({len(titles)})")
    for t in titles:
        print(f"    - {t}")

    print(f"\n  KEYWORDS TO ADD ({len(keywords)})")
    for k in keywords:
        marker = "  [already in list]" if k.lower() in INCLUDE_KEYWORDS else ""
        print(f"    - {k}{marker}")

    print(f"\n  SECTORS / COMPANIES TO INVESTIGATE ({len(sectors)})")
    for s in sectors:
        print(f"    - {s}")

    print(f"\n  {'-'*58}")
    print(f"  To fold these into a live crawl, rerun with:")
    print(f'    python crawler.py --expand-live "{term}"')
    print(f"{bar}\n")


def print_location_expansion(term, expanded):
    w = 62
    bar = "=" * w
    print(f"\n{bar}")
    print(f"  Location Expansion: '{term}'")
    print(f"{bar}")
    include = expanded.get("include", [])
    exclude = expanded.get("exclude", [])

    print(f"\n  LOCATION_INCLUDE additions ({len(include)})")
    for x in include:
        marker = "  [already in list]" if x.lower() in [i.lower() for i in LOCATION_INCLUDE] else ""
        print(f"    - {x}{marker}")

    print(f"\n  LOCATION_EXCLUDE additions ({len(exclude)})")
    for x in exclude:
        marker = "  [already in list]" if x.lower() in [i.lower() for i in LOCATION_EXCLUDE] else ""
        print(f"    - {x}{marker}")

    print(f"\n  {'-'*58}")
    print(f"  Copy entries you want into LOCATION_INCLUDE / LOCATION_EXCLUDE.")
    print(f"{bar}\n")


def generate_keyword_report(delay=0.5):
    """
    Run expand_search() on every INCLUDE_KEYWORDS entry, aggregate unique
    new titles/keywords/sectors, write a markdown report.
    """
    REPORT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"keyword_expansion_{date_str}.md"

    existing_kw = {k.lower() for k in INCLUDE_KEYWORDS}
    all_titles, all_keywords, all_sectors = {}, {}, {}

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  Keyword Report - expanding {len(INCLUDE_KEYWORDS)} keyword(s)")
    print(f"{bar}\n")

    for i, kw in enumerate(INCLUDE_KEYWORDS, 1):
        print(f"  [{i}/{len(INCLUDE_KEYWORDS)}] '{kw}'")
        expanded = expand_search(kw)
        if not expanded:
            continue
        for t in expanded.get("titles", []):
            all_titles.setdefault(t.strip(), []).append(kw)
        for k in expanded.get("keywords", []):
            all_keywords.setdefault(k.strip(), []).append(kw)
        for s in expanded.get("sectors", []):
            all_sectors.setdefault(s.strip(), []).append(kw)
        time.sleep(delay)

    def sort_by_freq(d):
        return sorted(d.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Keyword Expansion Report - {date_str}\n\n")
        f.write(f"Seeded from **{len(INCLUDE_KEYWORDS)}** existing keyword(s) in `INCLUDE_KEYWORDS`.\n")
        f.write(f"Suggestions ranked by how many seed terms surfaced them.\n\n")

        f.write("## New keywords to consider\n\n")
        f.write("Items marked `[already in list]` are in `INCLUDE_KEYWORDS`.\n\n")
        f.write("| Suggestion | Surfaced by | Status |\n|---|---|---|\n")
        for term, seeds in sort_by_freq(all_keywords):
            flag = "already in list" if term.lower() in existing_kw else "NEW"
            f.write(f"| `{term}` | {len(seeds)} | {flag} |\n")

        f.write("\n## Alternative job titles to search\n\n")
        f.write("| Title | Surfaced by |\n|---|---|\n")
        for term, seeds in sort_by_freq(all_titles):
            f.write(f"| {term} | {len(seeds)} |\n")

        f.write("\n## Sectors / employers to investigate\n\n")
        f.write("Pass any of these to `discover.py` to get ATS slug candidates.\n\n")
        f.write("| Sector / Employer | Surfaced by |\n|---|---|\n")
        for term, seeds in sort_by_freq(all_sectors):
            f.write(f"| {term} | {len(seeds)} |\n")

        new_only = [t for t in all_keywords if t.lower() not in existing_kw]
        if new_only:
            f.write("\n## Copy-paste block (new keywords only)\n\n")
            f.write("```python\n")
            for t in sorted(new_only, key=str.lower):
                f.write(f'    "{t.lower()}",\n')
            f.write("```\n")

    print(f"\n  Report -> {path}\n")
    return path


# =========================================================================
#  MAIN CRAWL
# =========================================================================

def crawl(dry_run=False):
    conn = None if dry_run else init_db()
    REPORT_DIR.mkdir(exist_ok=True)
    all_new, total_relevant = [], 0

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  BCI Job Crawler  -  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{bar}")
    if dry_run:
        print("  *** DRY-RUN: no DB writes, no email ***")
    print()

    def process(jobs):
        nonlocal total_relevant
        filtered = []
        for job in jobs:
            if is_location_allowed(job.get("location", "")):
                filtered.append(job)
            else:
                print(f"    [LOC-SKIP] {job['title']} - {job.get('location','?')}")
        total_relevant += len(filtered)
        for job in filtered:
            new = dry_run or is_new(conn, job["id"])
            if new:
                all_new.append(job)
                if not dry_run:
                    mark_seen(conn, job)
                print(f"    {'[DRY]' if dry_run else '[NEW]'} {job['title']}")

    for slug, name in GREENHOUSE_COMPANIES.items():
        print(f"  > {name} (Greenhouse)")
        process(fetch_greenhouse(slug, name))
        time.sleep(0.5)

    for slug, name in LEVER_COMPANIES.items():
        print(f"  > {name} (Lever)")
        process(fetch_lever(slug, name))
        time.sleep(0.5)

    for slug, name in ASHBY_COMPANIES.items():
        print(f"  > {name} (Ashby)")
        process(fetch_ashby(slug, name))
        time.sleep(0.5)

    for name, slug in KULA_COMPANIES:
        print(f"  > {name} (Kula)")
        process(fetch_kula(name, slug))
        time.sleep(0.5)

    for name, base_url, cat_id in DISCOURSE_BOARDS:
        print(f"  > {name} (Discourse)")
        process(fetch_discourse(name, base_url, cat_id))
        time.sleep(0.5)

    for name, url, sel in CUSTOM_COMPANIES:
        print(f"  > {name} (HTML scrape)")
        process(fetch_custom(name, url, sel))
        time.sleep(1.0)

    for name, base_url in SUCCESSFACTORS_COMPANIES:
        print(f"  > {name} (SuccessFactors)")
        process(fetch_successfactors(name, base_url))
        time.sleep(1.0)

    for tenant, wd_pod, site, name in WORKDAY_COMPANIES:
        print(f"  > {name} (Workday)")
        process(fetch_workday(tenant, wd_pod, site, name))
        time.sleep(1.0)

    for host, name in PEOPLEADMIN_COMPANIES:
        print(f"  > {name} (PeopleAdmin)")
        process(fetch_peopleadmin(host, name))
        time.sleep(1.0)

    print(f"\n  Done - {total_relevant} relevant listing(s), {len(all_new)} new.\n")
    if conn:
        conn.close()
    return all_new

# =========================================================================
#  REPORT
# =========================================================================

def write_report(new_jobs):
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"jobs_{date_str}.md"
    by_company = {}
    for job in new_jobs:
        by_company.setdefault(job["company"], []).append(job)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# BCI Job Alert - {date_str}\n\n")
        if not new_jobs:
            f.write("_No new relevant postings since last run._\n")
        else:
            f.write(f"**{len(new_jobs)} new posting(s)**\n\n")
            f.write("| Company | Title | Location |\n|---------|-------|----------|\n")
            for j in new_jobs:
                f.write(f"| {j['company']} | [{j['title']}]({j['url']}) | {j['location']} |\n")
            f.write("\n---\n\n")
            for company, jobs in sorted(by_company.items()):
                f.write(f"## {company}\n\n")
                for j in jobs:
                    f.write(f"### [{j['title']}]({j['url']})\n")
                    f.write(f"**Location:** {j['location']}  \n")
                    if j.get("description"):
                        f.write(f"{j['description'].replace(chr(10),' ').strip()[:400]}...\n")
                    f.write("\n")
    print(f"  Report -> {path}")
    return path

# =========================================================================
#  EMAIL
# =========================================================================

def send_email(new_jobs, report_path):
    if not new_jobs:
        print("  No new jobs - skipping email.")
        return
    if GMAIL_APP_PASSWORD == "YOUR_APP_PASSWORD_HERE":
        print("  [!] Set GMAIL_APP_PASSWORD before emailing.")
        return

    subject = f"[BCI Jobs] {len(new_jobs)} new posting(s) - {datetime.now().strftime('%Y-%m-%d')}"
    plain = "\n".join(
        [subject, ""] +
        [f"- {j['title']}\n  {j['company']} | {j['location']}\n  {j['url']}\n" for j in new_jobs]
    )
    rows = "".join(
        f"<tr><td><a href='{j['url']}'>{j['title']}</a></td>"
        f"<td>{j['company']}</td><td>{j['location']}</td></tr>"
        for j in new_jobs
    )
    html = f"""<html><body style="font-family:sans-serif;max-width:700px">
<h2>BCI Job Alert - {datetime.now().strftime('%Y-%m-%d')}</h2>
<p><strong>{len(new_jobs)} new posting(s) found</strong></p>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
  <tr><th>Title</th><th>Company</th><th>Location</th></tr>{rows}
</table>
<p>Full report: {report_path}</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            srv.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
        print("  Email sent.")
    except smtplib.SMTPAuthenticationError:
        print("  [!] Gmail auth failed - check your App Password.")
    except Exception as e:
        print(f"  [!] Email error: {e}")

# =========================================================================
#  ENTRY POINT
# =========================================================================

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BCI Job Crawler")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan without DB writes or email")
    ap.add_argument("--expand", metavar="TERM",
                    help="Expand a term into job titles/keywords/sectors and exit")
    ap.add_argument("--expand-live", metavar="TERM",
                    help="Expand a term and fold results into this crawl run")
    ap.add_argument("--expand-location", metavar="TERM",
                    help="Expand a location term into include/exclude substrings and exit")
    ap.add_argument("--expand-location-live", metavar="TERM",
                    help="Expand a location term and fold results into this crawl run")
    ap.add_argument("--keyword-report", action="store_true",
                    help="Bulk-expand every INCLUDE_KEYWORDS entry and write a suggestions report")
    args = ap.parse_args()

    if args.expand:
        expanded = expand_search(args.expand)
        if expanded:
            print_expansion(args.expand, expanded)
        raise SystemExit(0)

    if args.expand_location:
        expanded = expand_location(args.expand_location)
        if expanded:
            print_location_expansion(args.expand_location, expanded)
        raise SystemExit(0)

    if args.keyword_report:
        generate_keyword_report()
        raise SystemExit(0)

    if args.expand_live:
        print(f"\n  Expanding search for '{args.expand_live}'...")
        expanded = expand_search(args.expand_live)
        if expanded:
            print_expansion(args.expand_live, expanded)
            added = []
            for term in expanded.get("titles", []) + expanded.get("keywords", []):
                kw = term.lower()
                if kw not in INCLUDE_KEYWORDS:
                    INCLUDE_KEYWORDS.append(kw)
                    added.append(kw)
            if added:
                print(f"  + {len(added)} new keyword(s) added to this run.\n")

    if args.expand_location_live:
        print(f"\n  Expanding location '{args.expand_location_live}'...")
        expanded = expand_location(args.expand_location_live)
        if expanded:
            print_location_expansion(args.expand_location_live, expanded)
            added_inc, added_exc = [], []
            for loc in expanded.get("include", []):
                if loc.lower() not in [i.lower() for i in LOCATION_INCLUDE]:
                    LOCATION_INCLUDE.append(loc.lower())
                    added_inc.append(loc)
            for loc in expanded.get("exclude", []):
                if loc.lower() not in [i.lower() for i in LOCATION_EXCLUDE]:
                    LOCATION_EXCLUDE.append(loc.lower())
                    added_exc.append(loc)
            if added_inc or added_exc:
                print(f"  + {len(added_inc)} include / {len(added_exc)} exclude location filter(s).\n")

    new_jobs    = crawl(dry_run=args.dry_run)
    report_path = write_report(new_jobs)
    if not args.dry_run:
        send_email(new_jobs, report_path)
