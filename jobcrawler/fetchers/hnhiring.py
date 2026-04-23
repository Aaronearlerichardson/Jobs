"""
Hacker News "Ask HN: Who is hiring?" monthly-thread fetcher.

Every month on the 1st, user `whoishiring` posts a "Who is hiring?"
thread. Top-level comments are individual job posts written in free form
by the hiring engineer/founder. This is where a lot of early-stage
neurotech / research-adjacent roles surface that never hit an ATS.

Firebase API:
  - https://hacker-news.firebaseio.com/v0/user/whoishiring.json
    -> {"submitted": [ids...]}
  - https://hacker-news.firebaseio.com/v0/item/<id>.json
    -> {title, kids: [comment_ids], ...}

Strategy:
  1. Fetch whoishiring's submission list.
  2. Filter to items whose title starts with "Ask HN: Who is hiring?"
     (there are also "freelancer?" and "wants to be hired?" threads;
     we stick to the main hiring one).
  3. Take the N most recent hiring threads (default 2 = this month +
     last month, to catch mid-month runs).
  4. For each, fetch the top-level comment ids from `kids`, then fetch
     each comment and run is_relevant() on its text.

Comments are free-form HTML. We strip tags, keep the first ~600 chars
as description, and try to pull a company name and location heuristically
from the opening line. Format convention (widely followed) is:
    Company | Role | Location | Remote/Onsite | URL
"""

import html
import re
import time

import requests

from ..filters import is_relevant
from ..http import HEADERS

BASE = "https://hacker-news.firebaseio.com/v0"

_HIRING_TITLE_RE = re.compile(r"^Ask HN:\s*Who is hiring\??", re.I)

# Heuristic: "Company | Role | Location | ..." — split on | or •.
_SPLIT_RE = re.compile(r"\s*[|•·]\s*")
_URL_RE   = re.compile(r"https?://[^\s<>\"']+")


def _strip_html(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def _get_json(url, timeout=15):
    try:
        r = requests.get(url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"    [!] HN {url}: {e}")
        return None


def _parse_post(text):
    """
    Best-effort extract (company, title, location, url) from a HN job
    comment. Returns whatever we can; callers fall back to the comment
    itself for id/url.
    """
    first_line = text.split("\n", 1)[0].split(".", 1)[0]
    parts = [p.strip() for p in _SPLIT_RE.split(first_line) if p.strip()]
    company  = parts[0] if parts else ""
    role     = parts[1] if len(parts) > 1 else ""
    location = parts[2] if len(parts) > 2 else ""

    # If pipes weren't used, role is missing - just use first sentence
    # as title so the filter has something to match against.
    if not role:
        role = first_line[:120]

    url_match = _URL_RE.search(text)
    url       = url_match.group(0) if url_match else ""

    return company, role, location, url


def _find_hiring_threads(submitted_ids, max_threads=2, lookback=30):
    """
    Walk the newest submissions from `whoishiring` until we have
    `max_threads` "Who is hiring?" posts.
    """
    found = []
    for tid in submitted_ids[:lookback]:
        item = _get_json(f"{BASE}/item/{tid}.json")
        if not item:
            continue
        title = item.get("title") or ""
        if _HIRING_TITLE_RE.search(title):
            found.append(item)
            if len(found) >= max_threads:
                break
        time.sleep(0.05)
    return found


def fetch_hnhiring(max_threads=2, max_comments_per_thread=400):
    """
    Scan the latest N "Ask HN: Who is hiring?" threads, return top-level
    job comments whose text matches our relevance filter.
    """
    user = _get_json(f"{BASE}/user/whoishiring.json")
    if not user:
        return []

    submitted = user.get("submitted") or []
    threads = _find_hiring_threads(submitted, max_threads=max_threads)
    if not threads:
        print("    [!] No 'Who is hiring?' threads found in latest submissions.")
        return []

    jobs = []
    for thread in threads:
        tid       = thread.get("id")
        title     = thread.get("title", "")
        kids      = (thread.get("kids") or [])[:max_comments_per_thread]
        print(f"    -> {title} (id {tid}, {len(kids)} top-level posts)")

        for cid in kids:
            comment = _get_json(f"{BASE}/item/{cid}.json")
            time.sleep(0.02)        # gentle on Firebase
            if not comment or comment.get("deleted") or comment.get("dead"):
                continue
            text = _strip_html(comment.get("text", ""))
            if not text:
                continue

            company, role, location, post_url = _parse_post(text)
            if not is_relevant(role, text):
                continue

            hn_url = f"https://news.ycombinator.com/item?id={cid}"
            jobs.append({
                "id":          f"hnhiring_{cid}",
                "company":     company or "HN 'Who is hiring?'",
                "title":       role or "(see post)",
                "url":         post_url or hn_url,
                "location":    location or "See post",
                "description": text[:600],
            })
    return jobs
