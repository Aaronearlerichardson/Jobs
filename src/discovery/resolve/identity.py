"""Is this board really this company's? The identity guards every
discovery path applies to a detection before trusting it.

Two failure shapes, both seen in real runs: a candidate URL built from a
truncated or generic domain token reached an unrelated company's site
(`_risky_token_in_url` / `_corroborates`), and a careers page linked a
parent conglomerate's shared Workday tenant (`_tenant_affinity` /
`_foreign_board`). Shared by the sniffer, the Workday probes and the
web-search resolver, so it depends on nothing in this package.
"""

import re
import sys

from src.match.names import domain_tokens, risky_domain_tokens


# ─── Truncated-domain corroboration ───────────────────────────────────────
#
# candidate_urls tries every domain TOKEN (full, suffix-stripped, bare
# first word) across every path/TLD combo, all fetched concurrently, and
# takes the first hit in priority order. If the precise (full) token's
# domain times out while an ambiguous truncated token's domain answers —
# "galaxydiagnostics.com" dead, "galaxy.com" live — that unrelated
# company's board wins outright ("Galaxy Diagnostics" -> a 57-job board at
# the fintech Galaxy.com, zero of them local, misreported as a live
# no-local-jobs board instead of the wrong company it actually is). A hit
# reached only through a risky token (names.risky_domain_tokens) must
# corroborate against the page content before it's trusted.

def _risky_token_in_url(url, name):
    """The risky domain token (names.risky_domain_tokens) `url`'s host
    was built from, or "" if the host isn't one of those — including when
    it's ALSO reachable via a safe (full/suffix-stripped) token, since the
    full token containing the risky one as a substring
    ("galaxydiagnostics" contains "galaxy") must not itself count as risky.

    >>> _risky_token_in_url("https://www.galaxy.com/careers", "Galaxy Diagnostics")
    'galaxy'
    >>> _risky_token_in_url("https://www.galaxydiagnostics.com/careers", "Galaxy Diagnostics")
    ''
    >>> _risky_token_in_url("https://www.unitedtherapeutics.com/careers", "United Therapeutics")
    ''
    """
    risky = risky_domain_tokens(name)
    if not risky:
        return ""
    host = re.sub(r"^https?://", "", url.lower()).split("/", 1)[0]
    safe = [t for t in domain_tokens(name) if t not in risky]
    if any(s and s in host for s in safe):
        return ""
    for t in risky:
        if t and t in host:
            return t
    return ""


def _corroborates(text, name, skip_token=""):
    """True if `text` actually mentions `name` beyond the (possibly
    generic/truncated) domain token that reached it — the check a
    risky-token hit (see _risky_token_in_url) must pass before it's
    trusted. Requires a distinctive word (>=4 letters) from `name`, other
    than `skip_token`, to appear in the text.

    >>> _corroborates("Careers at Galaxy Diagnostics", "Galaxy Diagnostics", "galaxy")
    True
    >>> _corroborates("Galaxy Digital hires blockchain engineers",
    ...                "Galaxy Diagnostics", "galaxy")
    False
    >>> _corroborates("", "Galaxy Diagnostics", "galaxy")
    False

    A name with nothing left to check (every word is the skipped token, or
    too short) doesn't block the hit — there's no more precision to ask
    for:

    >>> _corroborates("anything", "Q2", "q2")
    True
    """
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(w) >= 4 and w != skip_token]
    if not words:
        return True
    blob = (text or "").lower()
    return any(w in blob for w in words)


# Tokens that appear in tenant/site strings for structural reasons and say
# nothing about WHOSE board it is.
_BOARD_GENERIC = {"jobs", "job", "careers", "career", "external", "site",
                  "portal", "search", "global", "en", "us", "www", "com"}
_NAME_GENERIC = {"inc", "llc", "ltd", "plc", "corp", "corporation", "co",
                 "the", "and", "of", "gmbh", "ag", "sa"}

# (name, tenant) pairs whose foreign-board verdict was already printed this
# process — the verdicts themselves are cached in src.claude.
_FOREIGN_ANNOUNCED = set()


def _tenant_affinity(name, triple):
    """True if a sniffed Workday (tenant, pod, site) shares an identity
    token with the company name — tenant OR site, either direction, or a
    4+-char shared prefix (tenants abbreviate: 'vhr-unither').

    >>> _tenant_affinity("KBI Biopharma", ("jsrglobal", 1, "KBI_Biopharma"))
    True
    >>> _tenant_affinity("Bioventus", ("osv-bioventus", 501, "External"))
    True
    >>> _tenant_affinity("United Therapeutics", ("vhr-unither", 5, "External"))
    True

    No affinity does NOT mean wrong — Merck & Co. really posts on tenant
    'msd' — it means "cannot be confirmed from the strings alone", which is
    what routes the hit to _foreign_board's LLM check:

    >>> _tenant_affinity("Genedata", ("danaher", 1, "DanaherJobs"))
    False
    >>> _tenant_affinity("Merck & Co.", ("msd", 5, "SearchJobs"))
    False
    """
    tenant, _, site = triple
    board = f"{tenant} {re.sub(r'([a-z])([A-Z])', r'\\1 \\2', str(site))}"
    board_toks = [t for t in re.findall(r"[a-z0-9]+", board.lower())
                  if len(t) >= 3 and t not in _BOARD_GENERIC]
    name_words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
                  if w not in _NAME_GENERIC]
    squashed_name = "".join(name_words)
    squashed_board = "".join(board_toks)
    for bt in board_toks:
        if bt in squashed_name:
            return True
    for nw in name_words:
        if len(nw) >= 3 and nw in squashed_board:
            return True
    for bt in board_toks:
        for nw in name_words:
            if len(bt) >= 5 and len(nw) >= 5 and bt[:4] == nw[:4]:
                return True
    return False


def _foreign_board(name, triple):
    """True when a sniffed Workday triple should NOT be attributed to
    `name`: the strings share no identity token AND the LLM judges the
    tenant to be another employer's (typically a parent conglomerate's
    shared board).

    Notes:
        Genedata's careers page legitimately links to Danaher's
        danaher/DanaherJobs board — but confirming that board AS Genedata
        made the daily crawl ingest every Danaher opco's local job under
        Genedata's name (2026-08-28 discover session, nc=28). The string
        check alone can't reject it: Merck & Co. really does post on
        tenant 'msd', so a bare mismatch must stay (that's also the
        offline behavior — with no API key the verdict is unknown and the
        hit is kept, flagged in the log for a human glance).
    """
    if _tenant_affinity(name, triple):
        return False
    from src.claude.api import board_is_own
    own = board_is_own(name, triple[0], triple[2])
    # Announce each (name, board) verdict ONCE — the sniff scans many
    # candidate URLs that embed the same board link, and the 2026-08-28
    # discover log repeated the same skip line 3x per company. Single write,
    # not print(): this runs on sniff worker threads, and print()'s separate
    # text/newline writes let another thread splice its line into this one.
    key = (name, triple[0])
    if own is False:
        if key not in _FOREIGN_ANNOUNCED:
            _FOREIGN_ANNOUNCED.add(key)
            sys.stdout.write(
                f"    [!] {name}: sniffed Workday board {triple[0]}/"
                f"{triple[2]} belongs to another employer (parent/shared "
                f"board) - skipped\n")
        return True
    if own is None and key not in _FOREIGN_ANNOUNCED:
        _FOREIGN_ANNOUNCED.add(key)
        sys.stdout.write(
            f"    [?] {name}: Workday tenant {triple[0]!r} shares no token "
            f"with the name and can't be verified offline - keeping; worth "
            f"a human glance\n")
    return False


# --------------------------------------------------------------------------- #
#  Fetching a company's candidate pages, with the identity check applied       #
# --------------------------------------------------------------------------- #
#
# Four places scanned a company's candidate URLs looking for something --
# an ATS signature, a Workday triple, a self-hosted board -- and each one
# opened with the same six lines: build the candidate list, fetch it
# through the per-run memo, walk it in priority order, skip the URLs that
# did not answer, and skip the ones reached only through a risky domain
# token that the page does not corroborate.
#
# They had already come apart. probe_workday built and scanned its own
# list with neither the corroboration check nor the foreign-board check,
# so a hit the sniffer rejected was accepted there; the docstring saying
# "Same guards on both paths now" is the repair, made by hand, that this
# generator makes structural.


def corroborated(url, name, text):
    """False when `url` reaches `name`'s page only through a risky domain
    token and the page does nothing to back that up.

    A truncated or generic guess ("galaxy.com" for "Galaxy Diagnostics")
    can land on a live site belonging to somebody else entirely, and does
    exactly that when the precise domain times out. The page then has to
    corroborate the company name before anything read off it is trusted.
    A URL built from a safe token needs no corroboration.
    """
    risky = _risky_token_in_url(url, name)
    return not risky or _corroborates(text, name, risky)


def candidate_responses(name, careers_url="", **kw):
    """`name`'s candidate URLs paired with what each one answered, in
    candidate-priority order. `None` where a URL did not answer at all.
    `kw` goes to `candidate_urls` (patterns, cap).

    The raw pass, for the one caller that needs to tell "nothing answered"
    from "everything that answered was somebody else" --
    sniffer.diagnose_no_board exists to name exactly that difference, so
    it cannot use the filtered walk below. It reached into fetchpool for
    the fetch itself before, which made the sniffer import its own package
    and put a cycle between resolve/__init__ and resolve/sniffer.

    Imported here rather than at module level so there is ONE place to
    stub the fetch in tests (tests/test_parsers.py patches
    fetchpool._fetch_all and every caller follows).
    """
    from .fetchpool import _fetch_all, candidate_urls
    urls = candidate_urls(name, careers_url, **kw)
    if not urls:
        return []
    responses = _fetch_all(urls)
    return [(u, responses.get(u)) for u in urls]


def candidate_pages(name, careers_url="", **kw):
    """Yield the responses from `name`'s candidate URLs, best first, with
    the ones that did not answer and the ones that do not corroborate
    already dropped. `kw` goes to `candidate_urls` (patterns, cap).

    Priority order is the candidate list's, not completion order: the
    fetch runs in parallel but the walk does not, because the first hit
    wins and the precise domain must beat the generic guess.
    """
    for url, r in candidate_responses(name, careers_url, **kw):
        if r is not None and corroborated(url, name, r.text):
            yield r
