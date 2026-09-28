"""
Generic RSS/Atom job-feed fetcher.

Works for any aggregator that publishes a standard feed. Seeded with
WeWorkRemotely's category feeds, but the `fetch_rss` function takes any
URL, so config can add more (Jobicy, RemoteRocketship, most ATSs).

WeWorkRemotely feeds:
    https://weworkremotely.com/categories/remote-programming-jobs.rss
    https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from lxml import etree

from src.net import http
from src.net.http import HEADERS, fetch_failed
from src.net.util import named, node_text, parse_markup, stable_id, strip_html


def _parse_title(title: str | None) -> tuple[str, str, str]:
    """
    Return (role, company, region) - any piece may be empty.

    Tries colon-style first ('Company: Role [| extra | extra]'),
    falls back to 'Role at Company (Region)', then returns the raw
    title as role.
    """
    # WWR titles take either shape:
    #   "Company Name: Role Title"            (current convention)
    #   "Role Title at Company Name (Region)" (older posts)
    # Some titles also embed sub-detail behind pipes ("Role | Region | Remote").
    t = (title or "").strip()
    if not t:
        return "", "", ""

    # Colon-style: "Company Name: Role Title | Region | Remote"
    m = re.match(r"^([^:]+?):\s*(.+)$", t)
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
    m = re.match(r"^(.*?)\s+at\s+(.*?)(?:\s*\(([^)]+)\))?\s*$", t, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()

    return t, "", ""


def _find(item: etree._Element, name: str) -> tuple[etree._Element | None, str]:
    """(element, its text) for `item`'s first descendant named `name` in any
    namespace; (None, "") when it has none.

    >>> entry = parse_markup('<entry xmlns="http://www.w3.org/2005/Atom"><title>Chemist</title>'
    ...                      '<link href="https://x.test/7"/></entry>', xml=True)
    >>> _find(entry, "title")[1], _find(entry, "link")[0].get("href"), _find(entry, "guid")
    ('Chemist', 'https://x.test/7', (None, ''))
    """
    el = named(item, name, one=True)
    return (el, node_text(el, "", strip=False)) if el is not None else (None, "")


async def fetch_rss(source_label: str, url: str, default_location: str = "Remote",
                    max_items: int = 200, remote_board: bool = False,
                    gate: Callable[..., bool] | None = None) -> list[dict[str, Any]]:
    """
    Pull an RSS/Atom feed, yield relevant jobs, the feed read off the loop.

    `source_label` is used as a fallback company name. If the feed is
    WWR-shaped we extract the real company from each item's title.
    `remote_board=True` marks every item with a structured remote hint —
    use for feeds from remote-only boards (WeWorkRemotely, Jobicy) where
    the parsed region is an eligibility constraint, not an office.
    """
    try:
        r = await http.send("GET", url, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        return fetch_failed(f"RSS {source_label}", e)
    return await asyncio.to_thread(_jobs, r.content, source_label, url, default_location,
                                   max_items, remote_board, gate)


def _jobs(feed: bytes, source_label: str, url: str, default_location: str, max_items: int,
          remote_board: bool, gate: Callable[..., bool] | None) -> list[dict[str, Any]]:
    """The feed body `feed` as fetch_rss's job dicts."""
    root = parse_markup(feed, xml=True, url=url)
    items = named(root, "item") or named(root, "entry")
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

        # The item's body: the first of these it has.
        bodies = (_find(it, n) for n in ("description", "summary", "content"))
        desc = next((strip_html(text) for el, text in bodies if el is not None), "")

        role, company, region = _parse_title(raw_title)

        # WWR-specific: prefer <region> tag over parsed region
        region_text = _find(it, "region")[1]
        if region_text:
            region = region_text.strip()

        location = region or default_location

        if gate is not None and not gate(role, desc):
            continue

        job: dict[str, Any] = {
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
