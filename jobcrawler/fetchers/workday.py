"""Workday career-site JSON endpoints (listing + per-job description via CXS).

Workday serves both the job list and each job's full description as plain JSON
under /wday/cxs/, no JavaScript needed:
  POST {host}/wday/cxs/{tenant}/{site}/jobs            -> listing
  GET  {host}/wday/cxs/{tenant}/{site}{externalPath}   -> one job, incl. body
The old fetcher only hit the listing and stored the "Posted N days ago" string
as the description, which is why most Workday rows were unscorable. We now
enrich each relevant posting with its real body, and expose a backfill for rows
already stored without one.
"""

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from ..filters import is_relevant
from ..http import HEADERS
from ..util import stable_id

_JSON_HEADERS = {**HEADERS, "Accept": "application/json"}


def _text_from_html(raw):
    """Strip a Workday jobDescription HTML blob to readable plain text."""
    if not raw:
        return ""
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    txt = re.sub(r"(?i)<(/p|/li|/h[1-6]|br\s*/?|/div)\s*>", "\n", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


def _cxs_detail_url(job_url):
    """Map a Workday job-page URL to its CXS JSON detail endpoint, or None.

    https://{tenant}.wd{N}.myworkdayjobs.com[/en-US]/{site}/job/{path}
      -> https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}
    """
    u = urlparse(job_url)
    if "myworkdayjobs.com" not in u.netloc or ".wd" not in u.netloc:
        return None
    host = f"{u.scheme}://{u.netloc}"
    tenant = u.netloc.split(".wd", 1)[0]
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[0].lower() in ("en-us", "en"):
        parts = parts[1:]
    if "job" not in parts or parts.index("job") == 0:
        return None
    site = parts[0]
    rest = "/".join(parts[parts.index("job"):])
    return f"{host}/wday/cxs/{tenant}/{site}/{rest}"


def _cxs_description(detail_url, timeout=25):
    """GET a CXS job-detail endpoint; return (plain_text_description, remoteType)."""
    try:
        r = requests.get(detail_url, timeout=timeout, headers=_JSON_HEADERS)
        r.raise_for_status()
        info = r.json().get("jobPostingInfo", {}) or {}
    except Exception:
        return None, None
    return _text_from_html(info.get("jobDescription", "")), info.get("remoteType")


def fetch_workday_description(job_url):
    """Public: full JD text for one stored Workday job URL (None on failure)."""
    detail = _cxs_detail_url(job_url)
    if not detail:
        return None
    text, _remote = _cxs_description(detail)
    return text or None


def fetch_workday(tenant, wd_pod, site, company_name, page_size=20, max_pages=25):
    """
    Poll a Workday career-site listing (JSON POST) and enrich each relevant
    posting with its full JD text from the CXS per-job endpoint. Falls back to
    the "postedOn" string if a body fetch fails, so a crawl never breaks on it.
    """
    host = f"https://{tenant}.wd{wd_pod}.myworkdayjobs.com"
    api  = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    link_base = f"{host}/en-US/{site}"
    cxs_base  = f"{host}/wday/cxs/{tenant}/{site}"

    wd_headers = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}

    jobs = []
    for page in range(max_pages):
        body = {"appliedFacets": {}, "limit": page_size,
                "offset": page * page_size, "searchText": ""}
        try:
            r = requests.post(api, json=body, timeout=25, headers=wd_headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"    [!] Workday {company_name} p{page}: {e}")
            break

        postings = data.get("jobPostings", []) or []
        if not postings:
            break

        for p in postings:
            title  = p.get("title", "") or ""
            path   = p.get("externalPath", "") or ""
            loc    = p.get("locationsText", "") or "Unknown"
            posted = p.get("postedOn", "") or ""
            if not is_relevant(title, posted):
                continue
            jid  = path.rsplit("/", 1)[-1] if path else stable_id(title, loc)
            desc = None
            if path:
                desc, _remote = _cxs_description(f"{cxs_base}{path}")
                time.sleep(0.3)
            jobs.append({
                "id":          f"wd_{tenant}_{jid}",
                "company":     company_name,
                "title":       title,
                "url":         f"{link_base}{path}" if path else host,
                "location":    loc,
                "description": desc or posted,
            })

        if len(postings) < page_size:
            break
        time.sleep(0.5)

    return jobs


def backfill_workday_descriptions(max_workers=8, limit=None, min_len=200):
    """One-shot: fill in full JD text for stored Workday jobs missing it
    (description shorter than min_len chars), via the CXS per-job endpoint.
    Only touches rows whose URL is a myworkdayjobs.com board. Safe to re-run."""
    from .. import store
    conn = store.connect()
    rows = conn.execute(
        "SELECT job_id, url FROM jobs "
        "WHERE url LIKE '%myworkdayjobs.com%' "
        "AND length(COALESCE(description,'')) < ?",
        (min_len,),
    ).fetchall()
    if limit:
        rows = rows[:int(limit)]
    print(f"  backfilling {len(rows)} Workday description(s) via CXS...")

    def _one(r):
        return r["job_id"], fetch_workday_description(r["url"])

    n = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_one, r): r for r in rows}):
            try:
                jid, text = fut.result()
            except Exception as e:
                print(f"    [!] backfill error: {e}")
                continue
            if not text:
                continue
            conn.execute("UPDATE jobs SET description=? WHERE job_id=?",
                         (text[:8000], jid))
            conn.commit()
            n += 1
    conn.close()
    print(f"  {n} of {len(rows)} description(s) backfilled.")
    return n
