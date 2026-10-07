"""
Generic JSON-LD JobPosting fetcher.

Most modern career pages embed schema.org JobPosting data in a
<script type="application/ld+json"> tag — this is the exact format that
Google for Jobs and other aggregators consume. One parser covers
hundreds of sites with zero per-vendor code.

Use it two ways:

  await fetch_jsonld_page(company, url)
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from typing import Protocol, TypeGuard

from typing_extensions import TypedDict

from src.net import http
from src.net.http import HEADERS, fetch_failed
from src.net.util import JSON, jsonld_scripts, parse_markup, stable_id, text_from_html
from src.net.util import norm_posted_date as _norm_posted
from src.rows import FetchedJob


class Page(Protocol):
    """A fetched page: all `_page_meta` and `_page_verdict` read."""
    @property
    def text(self) -> str: ...


class Posting(TypedDict, closed=True):
    """A JobPosting's values as `read_posting` reads them."""

    title: str
    url: str
    location: str
    description: str
    posted_at: JSON
    key: str
    telecommute: bool


def extract_jsonld(html: str, url: str = "") -> list[JSON]:
    """Find every <script type=application/ld+json> block in the page at
    `url`; return parsed objects, a list's items and an @graph's members
    each one; a trailing comma is forgiven.

    >>> extract_jsonld('<script type="application/ld+json">{"@graph": [{"@type": "JobPosting",'
    ...                ' "title": "Chemist",}]}</script>')
    [{'@type': 'JobPosting', 'title': 'Chemist'}]
    """
    out: list[JSON] = []
    for script in jsonld_scripts(parse_markup(html, url=url)):
        txt = script.text
        if not txt:
            continue
        txt = txt.strip().lstrip("\ufeff")
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", txt))
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            out.extend(data)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                out.extend(data["@graph"])
            else:
                out.append(data)
    return out


def is_jobposting(obj: JSON) -> TypeGuard[Mapping[str, JSON]]:
    if not isinstance(obj, dict):
        return False
    t = obj.get("@type")
    if isinstance(t, list):
        return any("JobPosting" in str(x) for x in t)
    return "JobPosting" in str(t or "")


def _one_location(loc: JSON) -> str:
    """One jobLocation entry -> display string ('' when unreadable)."""
    if not isinstance(loc, dict):
        return str(loc or "").strip()
    addr = loc.get("address", {})
    if isinstance(addr, dict):
        parts = [addr.get("addressLocality"),
                 addr.get("addressRegion"),
                 addr.get("addressCountry")]
        joined = ", ".join(str(p) for p in parts if p and str(p).upper() != "UNAVAILABLE")
        if joined:
            return joined
    if loc.get("name"):
        return str(loc["name"])
    return ""


def _normalize_location(jp: Mapping[str, JSON]) -> str:
    # Multi-location postings list several jobLocation entries; taking only
    # the first hid every secondary site (a "Remote"-first posting with a
    # Durham office read as just "Remote"). Join them all.
    loc = jp.get("jobLocation")
    locs = loc if isinstance(loc, list) else [loc] if loc else []
    parts: list[str] = []
    seen: set[str] = set()
    for l in locs:
        s = _one_location(l)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            parts.append(s)
    if parts:
        return "; ".join(parts)
    alr = jp.get("applicantLocationRequirements")
    if isinstance(alr, dict) and alr.get("name"):
        return str(alr["name"])
    if jp.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return "Unknown"


def read_posting(jp: Mapping[str, JSON], page_url: str = "") -> Posting:
    """A JobPosting's values, plain: `title`, `url` (the posting's own, else
    `page_url`), `location` ("" when it names none), `description` (text),
    `posted_at` (as written), `key` (its identifier, else a stable id of
    its URL) and `telecommute`.

    >>> r = read_posting({"@type": "JobPosting", "title": " Eng ", "identifier": {"value": 7},
    ...                   "jobLocationType": "TELECOMMUTE"}, "https://x.test/j/7")
    >>> r["title"], r["url"], r["location"], r["key"], r["telecommute"]
    ('Eng', 'https://x.test/j/7', 'Remote', '7', True)
    >>> read_posting({"title": "E", "description": ["a"]})["description"]
    ''
    """
    job_url = jp.get("url") or jp.get("mainEntityOfPage") or page_url
    if isinstance(job_url, dict):
        job_url = job_url.get("@id", page_url)
    identifier = jp.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value", "")
    location = _normalize_location(jp)
    description = jp.get("description")
    return {
        "title": str(jp.get("title") or jp.get("name") or "").strip(),
        "url": str(job_url) if job_url else page_url,
        "location": "" if location == "Unknown" else location,
        "description": text_from_html(description if isinstance(description, str) else ""),
        "posted_at": jp.get("datePosted"),
        "key": str(identifier or stable_id(str(job_url))),
        # Structured remote signal: schema.org marks remote roles explicitly.
        "telecommute": str(jp.get("jobLocationType", "")).upper() == "TELECOMMUTE",
    }


def postings(html: str, page_url: str = "") -> list[Posting]:
    """Every JobPosting on a page, as `read_posting` records."""
    return [read_posting(o, page_url) for o in extract_jsonld(html, page_url) if is_jobposting(o)]


def _job_from_posting(jp: Mapping[str, JSON], company_name: str, source_url: str) -> FetchedJob:
    p = read_posting(jp, source_url)
    job: FetchedJob = {
        "id":          f"jsonld_{company_name.replace(' ', '_')}_{p['key']}",
        "company":     company_name,
        "title":       p["title"],
        "url":         p["url"],
        "location":    p["location"] or "Unknown",
        "description": p["description"],
        "posted_at":   _norm_posted(p["posted_at"]),
    }
    if p["telecommute"]:
        job["remote_hint"] = "jsonld:telecommute"
    return job


async def fetch_jsonld_page(company_name: str, page_url: str,
                            gate: Callable[..., bool] | None = None,
                            timeout: tuple[float, float] | None = None) -> list[FetchedJob]:
    """Fetch ONE URL; extract JobPosting records from its JSON-LD, read off
    the loop."""
    try:
        r = await http.send("GET", page_url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        return fetch_failed(f"JSON-LD {company_name} {page_url}", e)
    jobs = await asyncio.to_thread(
        lambda: [_job_from_posting(obj, company_name, page_url)
                 for obj in extract_jsonld(r.text, page_url) if is_jobposting(obj)])
    return [j for j in jobs if gate is None or gate(j["title"], j["description"])]



