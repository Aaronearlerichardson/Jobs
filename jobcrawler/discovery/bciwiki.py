"""BCIWiki company directory as a discovery seed source.

bciwiki.org is a MediaWiki-backed directory of the brain-computer-interface
industry. It has no jobs board, but its Category:Companies (~700 entries),
Category:Labs (~300), and Category:Organizations (~1300) are a curated,
on-topic list of exactly the employers this crawler targets — far broader
and more relevant than Claude's 15-per-query discovery guesses.

So we use it the way the rest of discovery works: harvest names here, then
run them through validate_candidate (slug probe + careers-page ATS sniff)
"""

import requests

from ..http import HEADERS

API_URL = "https://bciwiki.org/api.php"

# Category -> the ats hint we hand each candidate. Companies/labs/orgs all
# go in as "unknown" so the universal probe + sniffer sweep every platform.
CATEGORIES = {
    "companies": "Companies",
    "labs":      "Labs",
    "organizations": "Organizations",
}

# Wiki pages that are clearly not employers — skip so discovery doesn't
# waste probes on them. Matched case-insensitively as a substring.
_SKIP_SUBSTRINGS = (
    "list of", "category:", "template:", "comparison of", "index of",
)


def _category_members(category, max_items=2000, timeout=25):
    """Return all page titles in a BCIWiki category, following cmcontinue."""
    titles, cont = [], {}
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
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"    [!] BCIWiki {category}: {e}")
            break
        titles += [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
        if "continue" in data:
            cont = data["continue"]
        else:
            break
    return titles


def _looks_like_employer(title):
    t = title.lower()
    return not any(s in t for s in _SKIP_SUBSTRINGS)


def bciwiki_company_names(categories=("companies",), max_items=2000):
    """Deduped, cleaned list of employer names from the given BCIWiki
    categories. `categories` keys are from CATEGORIES."""
    seen, out = set(), []
    for key in categories:
        cat = CATEGORIES.get(key)
        if not cat:
            continue
        for title in _category_members(cat, max_items=max_items):
            name = title.strip()
            if not name or not _looks_like_employer(name):
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(name)
    return out


def bciwiki_seed_candidates(categories=("companies",), max_items=2000):
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
        for name in bciwiki_company_names(categories, max_items=max_items)
    ]
