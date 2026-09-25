"""
Generic RSS/Atom job-feed fetcher.

Works for any aggregator that publishes a standard feed. Seeded with
WeWorkRemotely's category feeds, but the `fetch_rss` function takes any
URL, so config can add more (Jobicy, RemoteRocketship, most ATSs).

WeWorkRemotely feeds:
    https://weworkremotely.com/categories/remote-programming-jobs.rss
    https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss
"""

import re

from src.net.http import HEADERS, SESSION, fetch_failed
from src.net.util import node_text, parse_markup, stable_id, strip_html, xpath

#: The elements named $name, in any namespace (an Atom feed's are in its own).
_NAMED = "//*[local-name()=$name]"
#: An item's body, the first of these it has.
_BODIES = ("description", "summary", "content")

# WWR titles take either shape:
#   "Company Name: Role Title"            (current convention)
#   "Role Title at Company Name (Region)" (older posts)
# Some titles also embed sub-detail behind pipes ("Role | Region | Remote").
_WWR_COLON_RE = re.compile(r"^([^:]+?):\s*(.+)$")
_WWR_AT_RE    = re.compile(r"^(.*?)\s+at\s+(.*?)(?:\s*\(([^)]+)\))?\s*$", re.I)


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


def _find(item, name):
    """(element, its text) for `item`'s first descendant named `name` in any
    namespace; (None, "") when it has none.

    >>> entry = parse_markup('<entry xmlns="http://www.w3.org/2005/Atom"><title>Chemist</title>'
    ...                      '<link href="https://x.test/7"/></entry>', xml=True)
    >>> _find(entry, "title")[1], _find(entry, "link")[0].get("href"), _find(entry, "guid")
    ('Chemist', 'https://x.test/7', (None, ''))
    """
    hit = xpath("." + _NAMED)(item, name=name)
    return (hit[0], node_text(hit[0], "", strip=False)) if hit else (None, "")


def fetch_rss(source_label, url, default_location="Remote", max_items=200,
              remote_board=False, gate=None):
    """
    Pull an RSS/Atom feed, yield relevant jobs.

    `source_label` is used as a fallback company name. If the feed is
    WWR-shaped we extract the real company from each item's title.
    `remote_board=True` marks every item with a structured remote hint —
    use for feeds from remote-only boards (WeWorkRemotely, Jobicy) where
    the parsed region is an eligibility constraint, not an office.
    """
    try:
        r = SESSION.get(url, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        return fetch_failed(f"RSS {source_label}", e)

    root = parse_markup(r.content, xml=True, url=url)
    items = xpath(_NAMED)(root, name="item") or xpath(_NAMED)(root, name="entry")
    jobs = []
    for it in items[:max_items]:
        _, raw_title = _find(it, "title")
        # An RSS <link> holds its URL; an Atom one names it in href.
        link_tag, link_text = _find(it, "link")
        if link_text:
            link = link_text.strip()
        elif link_tag is not None and link_tag.get("href"):
            link = link_tag.get("href")
        else:
            link = ""
        guid = _find(it, "guid")[1] or link or raw_title

        desc = next((strip_html(text) for el, text in (_find(it, n) for n in _BODIES)
                     if el is not None), "")

        role, company, region = _parse_title(raw_title)

        # WWR-specific: prefer <region> tag over parsed region
        region_text = _find(it, "region")[1]
        if region_text:
            region = region_text.strip()

        location = region or default_location

        if gate is not None and not gate(role, desc):
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
