"""Name in, crawlable board out -- or the reason there isn't one.

The top of this package: `resolve_or_miss` is the single entry point for
"attempt a company, and record the outcome either way", and everything
else here is what it is built from.

    resolve_board_sniff_first   sniff the company's OWN careers page
                                first, slug-probe second, web search
                                third, and VALIDATE every hit with a
                                live fetch
    classify_miss               when none of that worked, which of the
                                store's MISS_REASONS codes explains it
    _validate_board             the live fetch that rejects a slug guess
                                landing on an empty or nonexistent board

Order matters and is the point: a probe-first resolver guessed slugs from
the name before looking at the company's own site, and false-positived
onto same-named but unrelated boards ("Oxford Biomedica" -> a different
Oxford Workday tenant, "Raya Health" -> the Raya dating app on Lever).

Store-free, like the rest of this package. `classify_miss` returns the
store's reason CODES as strings; persisting them is the caller's job
(src/discovery/local_sourcing.py, paste_ingest.py), which is why this
lives here and they live one level up.
"""

from src import config
from src.ats import coords
from src.ats.board import board_for
from src.ats.signatures import detect, pack

from .identity import _foreign_board
from .probes import probe_company
from .websearch_board import _websearch_board


def _validate_board(comp):
    """Fetch a resolved board and return (total, nc) live job counts. A board
    that returns zero jobs is treated as dead/wrong by the caller — this is
    what rejects a slug-guess that resolves to an empty or nonexistent board."""
    from src.ats.board import company as company_fetch
    try:
        allj = company_fetch.fetch_company(comp, None)
    except Exception:
        return 0, 0
    if not allj:
        return 0, 0
    try:
        nc = sum(1 for j in allj
                 if company_fetch.NC_RE.search(j.get("location", "") or ""))
    except Exception:
        nc = 0
    return len(allj), nc


def _url_board(name, careers_url):
    """(ats, handle, careers_url) of the fetchable board `careers_url`
    itself names (`signatures.detect` on the URL), or None; a Workday
    tenant that is another employer's (`identity._foreign_board`, as in
    every other resolver step) names none."""
    hit = detect("", careers_url, leads=False) if careers_url else None
    if not hit or hit[1] == "workday" and _foreign_board(name, hit[2]):
        return None
    return hit[1], hit[2], pack(hit[1], hit[2], careers_url)["careers_url"]


def resolve_board_sniff_first(name, careers_url="", websearch=True):
    """Resolve a company NAME -> crawlable board, careers-page SNIFF FIRST,
    slug-probe only as a fallback, and VALIDATE every hit with a live fetch.

    The only resolver there is. It replaced a probe-first one that guessed
    slugs from the name before looking at the company's own site, which
    false-positived onto same-named but unrelated boards ('Oxford
    Biomedica' -> a different Oxford Workday tenant; 'Raya Health' -> the Raya
    dating app on Lever). Sniffing the company's OWN careers page can't
    collide that way, so it goes first; a probe-only hit is tagged
    ``via='probe'`` so the caller can flag it for a human sanity-check.

    ``websearch=False`` drops step 3 for a BULK pass. The search backend
    rate-limits hard (see src.net.ddg and local_sourcing._websearch_pass,
    which caps it for the same reason), so a directory sweep of hundreds of
    names would spend most of its wall clock inside its backoff.

    A careers_url on a vendor's host is read first: the board it names
    itself (``via='sniff'``).

    Returns {name, ats, slug, careers_url, count, nc, via} or None. ``slug`` is
    a (tenant, pod, site) triple for Workday, the GUID/slug otherwise, None for
    a custom self-hosted board."""
    from .sniffer import sniff_ats

    def _mk(ats, slug, curl, via):
        total, nc = _validate_board(coords.columns(ats, slug, curl))
        if total <= 0:
            return None
        return {"name": name, "ats": ats, "slug": slug, "careers_url": curl,
                "count": total, "nc": nc, "via": via}

    # 0) A careers_url on a vendor's host names its board outright; the
    # sniff never fetches one (fetchpool.candidate_urls).
    u = _url_board(name, careers_url)
    hit = _mk(*u, "sniff") if u else None
    if hit:
        return hit

    # 1) Authoritative: detect the ATS embedded on the company's own careers page.
    # A `custom` sniff hit is held back rather than returned outright: a
    # marketing/careers page with no real ATS embedded still classifies as
    # `custom`, and a handful of scraped page fragments is enough for
    # _validate_board's total > 0 to pass (Pfizer/Sanofi/AstraZeneca/Syngenta/
    # Novozymes all resolved this way, each with a single-digit `total` that
    # was never their real Workday board). Only a `custom` hit that already
    # carries LOCAL jobs (nc > 0) is a genuine self-hosted board worth taking
    # immediately; an nc == 0 custom hit is kept as a last-resort fallback so
    # steps 2/3 get a chance to find the real ATS first.
    fallback = None
    s = sniff_ats(name, careers_url or "")
    if s:
        hit = _mk(s["ats"], s.get("triple", s.get("slug")), s.get("careers_url"), "sniff")
        if hit:
            if s["ats"] != config.CAREERS_PAGE_ATS or hit["nc"] > 0:
                return hit
            fallback = hit

    # 2) Fallback: name-guessed slug/Workday probe (collision risk -> validated).
    p = probe_company(name, try_workday=True)
    if p:
        hit = _mk(p["ats"], p["slug"], p.get("careers_url"), "probe")
        if hit:
            return hit

    # 3) Web-search fallback: find the careers page for names whose domain the
    #    sniffer can't guess (acronyms, hyphenated or product-named domains --
    #    'OXB' -> oxb.com, 'United Imaging - North America' -> united-imaging.com,
    #    'Core Sound Imaging' -> studycast). _websearch_board already validates
    #    slug/own-domain against the name, so it's not collision-flagged.
    #    Best-effort: degrades to a miss when the search backend is rate-limited.
    w = _websearch_board(name) if websearch else None
    if w:
        hit = _mk(w["ats"], w.get("triple", w.get("slug")), w.get("careers_url"), "websearch")
        if hit:
            return hit

    # Nothing better than the weak custom sniff turned up: it beats a miss.
    return fallback


def classify_miss(name, careers_url=""):
    """Second look at a name that would not resolve: which src.store
    MISS_REASONS code explains it.

    Re-sniffs the careers page for detections resolve_board_sniff_first
    discards — an ATS we can RECOGNIZE but not fetch (Taleo, Eightfold,
    Dayforce, ...) is a very different problem from a company we could find
    nothing for, and the two were previously indistinguishable.

    A bare "no-board-found" is itself four different problems (nothing
    resolves, the domain is dead, a careers page exists with no known ATS,
    or a candidate resolved to someone else's site) — sniffer.diagnose_no_board
    tells them apart, appended as the ':'-qualifier a rerun's miss_counts
    already knows how to aggregate past (see src.store.miss_family). A
    careers_url naming a board itself is that board, dead.

    Notes:
        Costs one extra careers-page sniff (plus diagnose_no_board's own,
        on the no-board-found path), so it is called only on the failure
        path and only by the on-demand resolvers — never per candidate in
        a full discover_local pass.
    """
    from .sniffer import diagnose_no_board, sniff_careers_ats
    u = _url_board(name, careers_url)
    if u:
        return f"board-dead:{u[0]}"
    try:
        lead = sniff_careers_ats(name, careers_url or "")
    except Exception as e:
        return f"fetch-error:{type(e).__name__}"
    if not lead:
        try:
            sub = diagnose_no_board(name, careers_url or "")
        except Exception:
            sub = ""
        return f"no-board-found:{sub}" if sub else "no-board-found"
    ats = lead.get("ats") or "?"
    if ats in config.BOARDS and not board_for(ats):
        return f"ats-unsupported:{ats}"
    return f"board-dead:{ats}"


def resolve_or_miss(name, careers_url=""):
    """Resolve a company NAME to a crawlable board, or say why it failed.

    Returns ``(hit, reason)``. A hit with no reason is usable; a reason with
    no hit is a failed resolution (see classify_miss); a hit WITH a reason is
    a live, readable board that simply has no openings in your [locality]
    (``no-local-jobs``) — worth keeping, not worth crawling today.

    Notes:
        The single entry point for "attempt a company, and record the
        outcome either way". Callers persist the reason with
        src.store.record_miss so a rerun can skip, retry or report it.
    """
    try:
        hit = resolve_board_sniff_first(name, careers_url or "")
    except Exception as e:
        return None, f"fetch-error:{type(e).__name__}"
    if not hit:
        return None, classify_miss(name, careers_url)
    if not hit.get("nc"):
        return hit, "no-local-jobs"
    return hit, None


def resolved(fut, name):
    """`resolve_or_miss`'s (hit, reason) off a completed future, with a
    RAISE turned into a miss reason of the same shape.

    `resolve_or_miss` already converts the exceptions it can see, but the
    future itself can still fail -- a worker that dies in the pool, a
    cancelled task. Its three consumers (resolve_leads, add_names,
    reresolve_misses) each wrote this out, and the third had already
    dropped the report line, so a resolution that blew up during a
    reresolve became a miss with nothing in the log to say why.
    """
    try:
        return fut.result()
    except Exception as e:          # noqa: BLE001 - the reason IS the result
        print(f"    [!] {name}: {type(e).__name__}: {e}")
        return None, f"fetch-error:{type(e).__name__}"
