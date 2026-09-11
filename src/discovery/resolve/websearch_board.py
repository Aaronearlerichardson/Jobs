"""Resolve a company name to its board through web search, for names whose
domain the careers-page sniffer cannot guess (gov/org domains, acronyms,
product-named domains -- 'Core Sound Imaging' -> corestudycast.com).

Third and last step of board.resolve_board_sniff_first. Two guards
keep a search result from becoming the wrong employer's board: job
aggregators are skipped outright (_is_aggregator), and a hit is taken only
when its slug or host plausibly belongs to the name (_slug_matches_name /
_host_matches_name), with the shared parent-tenant check on Workday
(identity._foreign_board).
"""

import re

from src import config
from src.ats.signatures import detect, pack
from src.match.names import name_key
from src.net import ddg
from src.net.http import HEADERS, SESSION
from .identity import _foreign_board

# Job aggregators / company-directory sites: they rank highly for
# '"<name>" careers' but are never the employer's own ATS board, so sniffing
# them wastes fetch slots. Skipped when picking result URLs to resolve.
# Source: config.DISCOVERY_AGGREGATOR_HOSTS (profile.toml [discovery]
# aggregator_hosts); falls back to these defaults when unconfigured.
_DEFAULT_AGGREGATOR_HOSTS = (
    "linkedin.com", "indeed.", "glassdoor.", "ziprecruiter.com", "simplyhired.com",
    "builtin.com", "rocketreach.co", "careerjet.", "monster.com", "dice.com",
    "lensa.com", "jobcase.com", "themuse.com", "wellfound.com", "levels.fyi",
    "trueup.io", "salary.com", "comparably.com", "talent.com", "unifygtm.com",
    "getro.com", "jooble.org", "adzuna.", "snagajob.com", "careers.tufts.edu",
    "google.com/search", "bing.com", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "crunchbase.com", "pitchbook.com", "zippia.com",
)
_AGGREGATOR_HOSTS = tuple(
    getattr(config, "DISCOVERY_AGGREGATOR_HOSTS", None) or _DEFAULT_AGGREGATOR_HOSTS
)


def _is_aggregator(url):
    return any(h in url.lower() for h in _AGGREGATOR_HOSTS)


# Generic words that don't distinguish a company's domain — excluded when
# matching a result host to a name, so "medicaljobs.com" doesn't match
# "Sampson Regional Medical Center" on the word "medical". Source:
# config.DISCOVERY_GENERIC_NAME_WORDS (profile.toml [discovery]
# generic_name_words); falls back to these defaults when unconfigured.
_DEFAULT_GENERIC_NAME_WORDS = {
    "medical", "center", "centre", "health", "healthcare", "regional",
    "group", "services", "systems", "system", "technology", "technologies",
    "imaging", "solutions", "associates", "partners", "care", "clinic",
    "hospital", "labs", "laboratories", "company", "corporation", "global",
    "national", "american", "international", "the", "and", "inc", "llc",
}
_GENERIC_NAME_WORDS = getattr(config, "DISCOVERY_GENERIC_NAME_WORDS", None) or _DEFAULT_GENERIC_NAME_WORDS


def _host_matches_name(url, name):
    """True if the result's host plausibly belongs to the company itself
    (a distinctive name token appears in the host) — the guard that keeps a
    self-hosted 'custom' board from resolving to a third-party jobs site."""
    host = re.sub(r"^https?://", "", url.lower()).split("/", 1)[0].replace("www.", "")
    hostslug = name_key(host)
    joined = name_key(name)
    tokens = {joined} | {w for w in re.findall(r"[a-z0-9]+", name.lower())
                         if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}
    return any(len(t) >= 4 and t in hostslug for t in tokens)


def _slug_matches_name(slug, name):
    """True if a web-searched ATS slug/tenant plausibly belongs to the
    company — guards against the dork surfacing an unrelated board (e.g.
    'Novamed' -> the 'nc' NC-government Workday tenant)."""
    s = slug[0] if isinstance(slug, tuple) else slug   # workday tenant, else slug
    s = name_key(str(s or ""))
    if len(s) < 3:
        return False
    tokens = {name_key(name)}
    tokens |= {w for w in re.findall(r"[a-z0-9]+", name.lower())
               if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}
    return any(len(t) >= 3 and (s in t or t in s) for t in tokens)


def _websearch_board(name, max_results=8):
    """Find a company's board via web search when domain-guessing fails
    (gov/org domains, acronyms, or product-named domains — e.g. 'Core Sound
    Imaging' -> corestudycast.com). Returns the sniff_ats result shape, or
    None.

    Two improvements over a plain '"<name>" careers' search, which is
    dominated by LinkedIn/Indeed and rarely surfaces the real board:
      1. an ATS-dork query first, so a direct Workday/Greenhouse/iCIMS board
         link surfaces in the results;
      2. aggregators are skipped and self-hosted *custom* boards accepted,
         not just JSON-API ATSes.
    """
    from src.ats.fetchers.company import custom_board_listing_url

    def _search(query):
        out = []
        for r in ddg.search(query, max_results=max_results):
            u = r.get("href") or r.get("url")
            if u and not _is_aggregator(u):
                out.append(u)
        return out

    def _resolve(urls):
        # Pass 1: ATS coordinates already visible in a result URL
        # (myworkdayjobs.com / boards.greenhouse.io / *.icims.com links).
        # The slug must match the name — a bare board link from search has no
        # page context, so an unrelated board (nc.wd108 for "Novamed") is
        # otherwise indistinguishable from a real hit.
        for u in urls:
            hit = detect("", u)
            if hit and hit[0] in ("fetchable", "semi") and _slug_matches_name(hit[2], name):
                return pack(hit[1], hit[2], u)
        # Pass 2: fetch the top real (non-aggregator) results and sniff for
        # an embedded ATS or a self-hosted board with genuine job links.
        for u in urls[:5]:
            try:
                r = SESSION.get(u, timeout=config.PROBE_TIMEOUT, headers=HEADERS,
                                allow_redirects=True)
                if r.status_code != 200 or len(r.text) < 300:
                    continue
            except Exception:
                continue
            own = _host_matches_name(r.url, name)
            hit = detect(r.text, r.url)
            # Trust an embedded ATS when its slug matches the name OR it was
            # embedded on the company's own careers page (own-domain link).
            # An own-page Workday embed can still be a parent conglomerate's
            # shared board (seqirus.com links to CSL's 'csl' tenant), which
            # would attribute every sibling company's jobs to this one —
            # same guard as the sniffer.
            if hit and hit[0] in ("fetchable", "semi") and (own or _slug_matches_name(hit[2], name)):
                if not (hit[1] == "workday" and _foreign_board(name, hit[2])):
                    return pack(hit[1], hit[2], r.url)
            # Custom self-hosted board: only on the company's OWN domain —
            # otherwise a third-party jobs site with ≥3 listings
            # (healthecareers, dotmed, expertini, …) resolves as the board.
            if own:
                listing = custom_board_listing_url(r.url, r.text)
                if listing:
                    return {"ats": "custom", "careers_url": listing}
        return None

    # Dork for a direct ATS board first (cheap win, avoids the second query
    # when it lands); fall back to a general careers search only if it misses.
    ats_hint = ("myworkdayjobs OR greenhouse OR lever OR ashbyhq OR icims "
                "OR smartrecruiters OR bamboohr OR workday")
    seen = set()
    for query in (f'"{name}" jobs ({ats_hint})', f'"{name}" careers'):
        fresh = [u for u in _search(query) if u not in seen]
        seen.update(fresh)
        hit = _resolve(fresh)
        if hit:
            return hit
    return None
