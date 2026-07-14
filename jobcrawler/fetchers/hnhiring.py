"""
Hacker News "Ask HN: Who is hiring?" monthly-thread fetcher.

Every month on the 1st, user `whoishiring` posts a "Who is hiring?"
thread. Top-level comments are individual job posts written in free form
by the hiring engineer/founder. This is where a lot of early-stage
neurotech / research-adjacent roles surface that never hit an ATS.

Firebase API:
  - https://hacker-news.firebaseio.com/v0/user/whoishiring.json
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

# Field classifiers for the loosely-structured first line. The convention is
# "Company | Role | Location | ..." but in practice the middle fields appear in
# any order and include comp/employment-type/url noise, so we identify the role
# field by what it looks like rather than by position.
_ROLE_HINT_RE = re.compile(
    r"\b(engineer|engineering|developer|dev\b|scientist|analyst|manager|"
    r"designer|architect|researcher|programmer|consultant|specialist|"
    r"director|lead|head\s+of|founding|technician|administrator|devops|"
    r"sre|data|machine\s+learning|\bml\b|\bai\b|full[\s-]?stack|"
    r"back[\s-]?end|front[\s-]?end|\bqa\b|quality|test|product|software|"
    r"research|infrastructure|platform|security|biostat|bioinformatic)\b",
    re.I,
)
_LOC_HINT_RE = re.compile(
    r"\b(remote|onsite|on-site|hybrid|wfh|anywhere|relocation|visa|"
    r"usa?|uk|eu|emea|apac|canada|europe|worldwide|global|nationwide|"
    r"(north|south|latin)\s+america|america|americas|latam|united\s+states|"
    r"new\s+york|boston|london|berlin|austin|seattle|durham|raleigh|"
    r"san\s+francisco|sf\b|nyc\b)\b",
    re.I,
)
_COMP_RE = re.compile(
    r"(\$|€|£|\d{2,3}\s*[-–]\s*\d{2,3}\s*k|\bk\+|/yr|/year|salary|equity|"
    r"benefits|compensation|\bcomp\b)",
    re.I,
)
_EMPLOY_RE = re.compile(
    r"^\s*(full[\s-]?time|part[\s-]?time|contract|permanent|intern(ship)?|"
    r"w2|c2c|freelance|multiple\s+roles?|various\s+roles?)\s*$",
    re.I,
)
_URLISH_RE = re.compile(r"https?://|www\.|\.(com|io|ai|org|net|co|health|dev)\b", re.I)


def _clean_company(s):
    """Strip trailing URLs / parentheticals and surrounding punctuation."""
    s = _URL_RE.sub("", s)
    s = re.sub(r"[\(\[][^)\]]*[\)\]]?", "", s)   # drop "(…)" incl. unbalanced
    return re.sub(r"\s+", " ", s).strip(" -—|·•:,")


def _is_role(s):
    return bool(_ROLE_HINT_RE.search(s)) and not _COMP_RE.search(s) \
        and not _URLISH_RE.search(s)


def _is_location(s):
    return bool(_LOC_HINT_RE.search(s)) and not _ROLE_HINT_RE.search(s)


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
    comment. The first line is "Company | … | … | …" with the role,
    location, comp, and employment-type fields in no fixed order; we
    classify fields by appearance instead of trusting position, so a
    salary or location no longer ends up as the job title.
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    first_line = lines[0] if lines else ""
    # Prose-style headers (no pipe separators) get cut at the first
    # sentence so a paragraph doesn't swallow the parse. Pipe-delimited
    # headers are used whole — "Acme Inc. | ML Engineer | ..." must NOT
    # be chopped at "Inc. ".
    if "|" not in first_line:
        first_line = first_line.split(". ", 1)[0]
    parts = [p.strip() for p in _SPLIT_RE.split(first_line) if p.strip()]

    company = _clean_company(parts[0]) if parts else ""
    rest = parts[1:]

    # 1) Title = first first-line field that looks like a role.
    role = next((p for p in rest if _is_role(p)), "")

    # 2) Multi-role posts list titles on their own lines below the header;
    #    take the first role-like line.
    if not role:
        for ln in lines[1:6]:
            cand = re.sub(r"^[\-\*•·•]\s*", "", ln).strip()
            if _is_role(cand) and len(cand) <= 110:
                role = cand
                break

    # 3) Last resort: a field that isn't location / comp / employment / url.
    if not role:
        role = next(
            (p for p in rest
             if not _is_location(p) and not _COMP_RE.search(p)
             and not _EMPLOY_RE.match(p) and not _URLISH_RE.search(p)),
            "",
        )

    # Location = first field that looks like one (excluding the chosen title).
    location = next((p for p in rest if p != role and _is_location(p)), "")

    role = re.sub(r"\s+", " ", role).strip(" -—|·•:,")
    if len(role) > 110:
        role = role[:107].rstrip() + "..."

    url_match = _URL_RE.search(text)
    url = url_match.group(0) if url_match else ""

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
                "description": text,
            })
    return jobs
