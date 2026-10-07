"""BCIWiki company directory as a discovery seed source.

bciwiki.org is a MediaWiki-backed directory of the brain-computer-interface
industry. It has no jobs board, but its Category:Companies (~700 entries),
Category:Labs (~300), and Category:Organizations (~1300) are a curated,
on-topic list of exactly the employers this crawler targets — far broader
and more relevant than Claude's 15-per-query discovery guesses.

So we use it the way the rest of discovery works: harvest names here, then
run them through validate_candidate, which hands each name to the shared
resolver (careers-page sniff, then slug probe, every hit live-validated).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from src.config import FETCH_TIMEOUT
from src.net import http
from src.net.http import HEADERS

# Category -> the ats hint we hand each candidate. Companies/labs/orgs all
# go in as "unknown": the resolver reads the ATS off the company's own
# careers page rather than taking a hint for it.
CATEGORIES = {
    "companies": "Companies",
    "labs":      "Labs",
    "organizations": "Organizations",
}

async def _category_members(category: str, max_items: int = 2000,
                            timeout: float | tuple[float, float] = FETCH_TIMEOUT
                            ) -> list[str]:
    """Return all page titles in a BCIWiki category, following cmcontinue;
    each page's JSON decoded off the loop."""
    titles: list[str] = []
    cont: dict[str, str] = {}
    while len(titles) < max_items:
        params = {
            "action":  "query",
            "list":    "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": "500",
            "cmtype":  "page",
            "format":  "json",
            **cont,
        }
        try:
            r = await http.send("GET", "https://bciwiki.org/api.php", params=params,
                                headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            data = await asyncio.to_thread(r.json)
        except Exception as e:
            print(f"    [!] BCIWiki {category}: {e}")
            break
        titles += [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
        if "continue" in data:
            cont = data["continue"]
        else:
            break
    return titles


def _looks_like_employer(title: str) -> bool:
    # Wiki pages that are clearly not employers — skip so discovery doesn't
    # waste probes on them. Matched case-insensitively as a substring.
    skip_substrings = (
        "list of", "category:", "template:", "comparison of", "index of",
    )
    t = title.lower()
    return not any(s in t for s in skip_substrings)


async def bciwiki_company_names(categories: Iterable[str] = ("companies",),
                                max_items: int = 2000) -> list[str]:
    """Deduped, cleaned list of employer names from the given BCIWiki
    categories. `categories` keys are from CATEGORIES."""
    seen: set[str] = set()
    out: list[str] = []
    for key in categories:
        cat = CATEGORIES.get(key)
        if not cat:
            continue
        for title in await _category_members(cat, max_items=max_items):
            name = title.strip()
            if not name or not _looks_like_employer(name):
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(name)
    return out


async def bciwiki_seed_candidates(categories: Iterable[str] = ("companies",),
                                  max_items: int = 2000) -> list[dict[str, str | None]]:
    """Candidate dicts (same shape as Claude's discovery payload) so the
    names flow through candidate_from_dict / validate_candidate unchanged."""
    return [
        {
            "name":        name,
            "ats":         "unknown",   # universal probe + sniffer sweep
            "slug_guess":  None,
            "careers_url": "",
            "notes":       "[bciwiki]",
        }
        for name in await bciwiki_company_names(categories, max_items=max_items)
    ]
