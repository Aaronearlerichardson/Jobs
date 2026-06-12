"""
Generic RSS/Atom job-feed fetcher.

Works for any aggregator that publishes a standard feed. Seeded with
WeWorkRemotely's category feeds, but the `fetch_rss` function takes any
URL, so config can add more (Jobicy, RemoteRocketship, most ATSs).

WeWorkRemotely feeds:
    https://weworkremotely.com/categories/remote-programming-jobs.rss
    https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss
    https://weworkremotely.com/remote-jobs.rss           (all categories)
    https://weworkremotely.com/categories/all-other-remote-jobs.rss

Each <item> gives <title>, <link>, <description> (HTML-escaped), and
<region> (WWR-specific) or <pubDate>. Location is often embedded inside
the title as "Job Title at Company (Region)" - we parse that out.
"""

import html
import re

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS
from ..util import stable_id


# WWR titles take either shape:
#   "Company Name: Role Title"            (current convention)
#   "Role Title at Company Name (Region)" (older posts)
# Some titles also embed sub-detail behind pipes ("Role | Region | Remote").
_WWR_COLON_RE = re.compile(r"^([^:]+?):\s*(.+)$")
_WWR_AT_RE    = re.compile(r"^(.*?)\s+at\s+(.*?)(?:\s*\(([^)]+)\))?\s*$", re.I)


def _strip_html(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def _parse_title(title):
    """
    Return (role, company, region) - any piece may be empty.

    Tries colon-style first ('Company: Role [| extra | extra]'),
    falls back to 'Role at Company (Region)', then returns the raw
    title as role.
    """
    t = (title or "").strip()
    if not t:
        return "", "", ""

    # Colon-style: "Company Name: Role Title | Region | Remote"
    m = _WWR_COLON_RE.match(t)
    if m:
        company = m.group(1).strip()
        tail    = m.group(2).strip()
        # Split tail on pipes - first chunk is role, rest are region/mode hints
        pieces  = [p.strip() for p in re.split(r"\s*\|\s*", tail) if p.strip()]
        role    = pieces[0] if pieces else tail
        region  = " | ".join(pieces[1:]) if len(pieces) > 1 else ""
        # Guard against obvious mis-split (colon inside role like "Engineer III: Data")
        # Heuristic: if the "company" looks like a sentence, fall through.
        if len(company) <= 60 and not company.endswith((",", ".", " ")):
            return role, company, region

    # Fallback: "Role Title at Company (Region)"
    m = _WWR_AT_RE.match(t)
    if m:
        return m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()

    return t, "", ""


def fetch_rss(source_label, url, default_location="Remote", max_items=200,
              remote_board=False):
    """
    Pull an RSS/Atom feed, yield relevant jobs.

    `source_label` is used as a fallback company name. If the feed is
    WWR-shaped we extract the real company from each item's title.
    `remote_board=True` marks every item with a structured remote hint —
    use for feeds from remote-only boards (WeWorkRemotely, Jobicy) where
    the parsed region is an eligibility constraint, not an office.
    """
    try:
        r = requests.get(url, timeout=25, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] RSS {source_label}: {e}")
        return []

    # Use the xml parser; lxml is already a dep for sitemap.
    soup = BeautifulSoup(r.content, "xml")
    items = soup.find_all("item") or soup.find_all("entry")
    jobs = []
    for it in items[:max_items]:
        raw_title = (it.title.text if it.title else "") or ""
        link_tag  = it.find("link")
        if link_tag and link_tag.text:
            link = link_tag.text.strip()
        elif link_tag and link_tag.get("href"):
            link = link_tag.get("href")
        else:
            link = ""
        guid = (it.guid.text if it.guid else "") or link or raw_title

        desc_tag = it.find("description") or it.find("summary") or it.find("content")
        desc     = _strip_html(desc_tag.text) if desc_tag else ""

        role, company, region = _parse_title(raw_title)

        # WWR-specific: prefer <region> tag over parsed region
        region_tag = it.find("region")
        if region_tag and region_tag.text:
            region = region_tag.text.strip()

        location = region or default_location

        if not is_relevant(role, desc):
            continue

        job = {
            "id":          f"rss_{source_label.replace(' ', '_')}_{stable_id(guid)}",
            "company":     company or source_label,
            "title":       role,
            "url":         link,
            "location":    location,
            "description": desc,
        }
        if remote_board:
            job["remote_hint"] = "board:rss"
        jobs.append(job)
    return jobs
