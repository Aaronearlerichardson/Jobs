"""Paste-a-page ingest: employer names out of text copied from a page you
were already browsing, screened and queued for resolution.

Getting employers out of a site you are already browsing -- a LinkedIn
search, a Built In list, a news article -- comes down to harvesting NAMES.
The crawler never wants the source's job data: it resolves each name to the
employer's OWN board, which is fresher, complete, and carries a real apply
URL. So this surface can be dumb -- take whatever text was on the page and
let the existing probe -> validate -> score chain decide what was real.
Same contract name_sources.brainstorm_company_names runs under: a name that
isn't an employer simply fails to resolve.

Two steps, so requests are spent only on names a person ticked:
preview_names (parse and classify, no network) then add_names (resolve via
resolve.board.resolve_or_miss, score, queue for review).
"""

import re

from src import config

from src.match.names import junk_name_reason, name_key
from src.net.parallel import drain
from .local_sourcing import score_and_upsert
from .resolve.board import resolve_or_miss, resolved
from .name_sources import NAME_BLOCKLIST, _is_nav_noise


# Lines that are never a company name in a pasted results page.
_PASTE_NOISE_RE = re.compile(
    r"^(?:"
    r"promoted|easy apply|actively recruiting|be an early applicant|viewed|"
    r"applied|saved|save|dismiss|see all|show more|load more|next|previous|"
    r"remote|hybrid|on-?site|full-?time|part-?time|contract|internship|"
    r"\d*\s*(?:day|week|month|hour|minute)s?\s*ago|reposted.*|"
    r"[\d,]*\s*[km]?\s*(?:followers?|employees?|connections?|applicants?)|"
    r"over \d+ applicants|(?:page\s*)?\d* *of *\d+|see all.*|show all.*|"
    r"\$.*|"                     # any $-leading line: salary in every format
    # LinkedIn company/profile page stat lines ("11 results", "615 on
    # LinkedIn", "501-1000 employees", "2 year growth", "42Fair Match",
    # "4 connections work here", "75% have a Doctor of Philosophy")
    r"[\d,\-–]+\s*(?:results?|notifications?|on linkedin|"
    r"year growth|fair match|employees?)|"
    r"\d+\s*(?:company |school )?(?:alumni|connections?)\s+works? here.*|"
    r"\d+%.*|"
    r"401\(k\).*|"
    r"in the past \w+|"
    r".*(?:©|\(c\)|�)\s*\d{4}.*|.*\bcorporation\b\W*\d{4}"
    r")\W*$", re.I)

# A line that reads as a JOB TITLE rather than an employer. Results pages
# interleave the two, and a title resolves to nothing, so this saves the probe.
_TITLE_WORD_RE = re.compile(
    r"\b(?:engineer|scientist|developer|analyst|manager|director|specialist|"
    r"coordinator|associate|assistant|technician|architect|consultant|intern|"
    r"lead|head of|vp|president|officer|administrator|nurse|physician|"
    r"recruiter|designer|researcher|postdoc|fellow|programmer|"
    r"(?:bio)?statistician)\b",
    re.I)

# "Durham, NC" / "Durham, NC (Hybrid)" / "Raleigh-Durham-Chapel Hill Area" /
# "North Carolina, United States (Remote)" — the region after the comma may
# be several words, and a clipped paste can truncate the trailing "(Remote)"
# to "(R", so the closing paren is optional.
_LOCATION_LINE_RE = re.compile(
    r"^[A-Z][\w.'-]+(?:[ \-][\w.'-]+)*,\s*"
    r"(?:[A-Z]{2}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
    r"(?:\s*\([^)]*\)?)?$|.*\bArea$|.*\bMetropolitan\b", re.I)


# A results row whose job title is rendered twice, optionally with a badge
# wedged between the halves: "Signal Processing Engineer (Verified job)Signal
# Processing Engineer". LinkedIn emits this for every hit, and the employer is
# always the next line — so the doubled line is an unambiguous "company below"
# marker. `.{4,}?` is lazy so the shortest repeating half wins.
_DOUBLED_TITLE_RE = re.compile(r"^(.{4,}?)(?:\s*\([^)]{,24}\))?\1$")


# Words that stay lowercase in a real Title-Case name ("Bank of America",
# "University of North Carolina", "Bausch + Lomb") and so don't count as
# evidence either way when judging capitalisation.
_CASE_STOPWORDS = {
    "of", "and", "for", "the", "a", "an", "in", "at", "to", "on", "or",
    "&", "+", "de", "la", "le", "van", "von",
}


def _is_sentence_case(name):
    """True if a MULTI-WORD `name` reads as sentence-case UI prose (only the
    first significant word capitalised -- "Date posted", "Salary estimate")
    rather than a Title-Case company name (every significant word
    capitalised -- "Alpaca Health", "University of North Carolina").

    Single-token names are never flagged: a legitimately-lowercase name
    like "bioMerieux" or "nCino" has no second word to compare against, so
    there is no sentence-vs-title signal to read.

    >>> _is_sentence_case("Date posted")
    True
    >>> _is_sentence_case("Skip to main content")
    True

    A capitalised CONNECTOR first word still starts the shape — these two
    slipped through when the check skipped straight to the first
    significant word ("my" / "latest") and read its lowercase as
    "doesn't even start capitalised":

    >>> _is_sentence_case("In my network")
    True
    >>> _is_sentence_case("The latest hiring trend")
    True
    >>> _is_sentence_case("Alpaca Health")
    False
    >>> _is_sentence_case("University of North Carolina")
    False
    >>> _is_sentence_case("bioMerieux")
    False

    Notes:
        A name reduced to one significant word after stripping connector
        words ("Bank of X" with X itself lowercase, or a bare "X of") is
        left alone -- one word is not enough evidence to call sentence
        case, and a false reject here is a lost real company, not a
        dropped chrome line.
    """
    def _is_connector(w):
        # A word only counts as one of the fixed CASE_STOPWORDS after
        # stripping trailing punctuation, NOT the symbols that ARE
        # stopwords ("+", "&") -- stripping those first would zero the
        # token out and hide it from the membership check. A token with no
        # letters at all (a bare number, "+", "&") never carries a case
        # signal either way, so it's a connector too.
        core = w.lower().strip(".,’")
        return core in _CASE_STOPWORDS or not any(c.isalpha() for c in w)

    words = name.split()
    if len(words) < 2:
        return False
    significant = [w for w in words if not _is_connector(w)]
    if len(significant) < 2:
        return False

    def _starts_upper(w):
        core = w.lstrip("(\"'")
        return bool(core) and core[0].isalpha() and core[0].isupper()

    # Sentence case is defined by its shape: capitalised FIRST word, lower-
    # case rest. A phrase that doesn't even start capitalised ("bla bla")
    # isn't in that shape either way, so there's nothing to flag -- this
    # also keeps a completely-lowercase throwaway phrase from being read as
    # "sentence case" when it's really just not a name at all. The shape
    # starts at the LITERAL first word, connector or not: "In my network"
    # begins capitalised even though "In" carries no case signal itself.
    if not _starts_upper(words[0]):
        return False
    rest = significant[1:] if significant[0] == words[0] else significant
    if not rest:
        return False
    return any(not _starts_upper(w) for w in rest)


def _clean_candidate(raw, drop_titles=True):
    """One line -> a usable company name, or None.

    `drop_titles=False` for structurally-located names: the doubled-title
    marker already proved the line is an employer, and rejecting it for
    containing a word like "Science" or "Research" would lose real ones
    (Headwater Science, Vadum). The keyword screen is only needed when we
    are guessing from unstructured lines.
    """
    stripped = raw.strip()
    # Test noise BEFORE removing list markers: "2 days ago" and "1K followers"
    # only read as noise while they still carry their leading digits.
    if _PASTE_NOISE_RE.match(stripped):
        return None
    # Markdown export turns nav into "[Help Center](https://...)".
    if re.match(r"^\[[^\]]*\]\(", stripped):
        return None
    # Results pages render "Company · Location" and "Company • 1K followers".
    name = re.split(r"\s+[·•|]\s+", stripped)[0].strip()
    name = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", name).strip()
    if not (2 < len(name) <= 60):
        return None
    if _PASTE_NOISE_RE.match(name) or _is_nav_noise(name):
        return None
    if drop_titles and _TITLE_WORD_RE.search(name):
        return None
    if drop_titles and " / " in name:
        return None        # breadcrumb/CTA pair ("Employers / Post Job"), not a name
    if drop_titles and _is_sentence_case(name):
        return None        # sentence-case UI prose ("Date posted"), not Title Case
    if _LOCATION_LINE_RE.match(name):
        return None
    if not re.search(r"[A-Za-z]{2}", name):          # numbers / punctuation only
        return None
    if name.startswith(("http://", "https://", "www.")):
        return None
    return name


def _names_from_doubled_titles(lines):
    """Employers located by the repeated-title marker, in page order."""
    out = []
    for i, line in enumerate(lines):
        if not _DOUBLED_TITLE_RE.match(line.strip()):
            continue
        for nxt in lines[i + 1:i + 3]:               # skip a blank if present
            if nxt.strip():
                name = _clean_candidate(nxt, drop_titles=False)
                if name:
                    out.append(name)
                break
    return out


def parse_company_names(blob, limit=300):
    """Plausible employer names out of a pasted block of page text.

    Two passes. If the page repeats each job title — the shape every
    LinkedIn results row has — the employer is pinned by position and the
    surrounding chrome is never even considered. A real search page yields
    15 employers and no junk that way, against 117 lines for the filter.

    Otherwise fall back to filtering lines. Permissiveness is no longer
    cheap: a junk name that reaches the resolver costs a full sniff ->
    probe -> websearch chain, is PERSISTED as a miss row either way
    (add_names records every unresolved name), and a generic word can
    websearch-resolve to an unrelated real company's board (2026-08-28:
    "Biotech" landed on Dianthus Therapeutics' greenhouse board, ACTIVE).
    So page chrome, stat lines, sector labels and JD section headers are
    filtered out on sight; a dropped real name is still the rarer, cheaper
    mistake, so the filters key on shapes no employer name takes:

    >>> parse_company_names('''Home
    ... My Network
    ... Jobs
    ... Messaging
    ... Notifications
    ... For Business
    ... Create cover letter
    ... Learning
    ... People you can reach out to
    ... Senior Data Engineer
    ... Alpaca Health
    ... Durham, NC (Hybrid)
    ... IQVIA
    ... Durham, NC''')
    ['Alpaca Health', 'IQVIA']
    """
    if isinstance(blob, (list, tuple)):
        lines = [str(x) for x in blob]
    else:
        lines = re.split(r"[\r\n]+", str(blob or ""))

    structured = _names_from_doubled_titles(lines)
    # Two hits mean the marker is really this page's shape, not a coincidental
    # repeat in prose.
    candidates = structured if len(structured) >= 2 else [
        n for n in (_clean_candidate(ln) for ln in lines) if n]

    out, seen = [], set()
    for name in candidates:
        key = name_key(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
        if len(out) >= limit:
            break
    return out


def extract_names_llm(blob, limit=60):
    """Ask the model which employers a pasted page mentions.

    The regex path cannot tell "Fennec Pharmaceuticals" from a job title whose
    words it has never seen. One call fixes that for a messy paste. Returns []
    without an API key, so the caller falls back to the regex.
    """
    from src.claude.api import call_claude_json
    system = ("You extract EMPLOYER NAMES from text copied off a job-search or "
              "company-directory page. Return only organisations that could "
              "employ someone. Never return job titles, locations, dates, "
              "recruiter names, or UI labels. Return each company's plain name "
              "without taglines.")
    user = ('Return JSON {"companies": ["name", ...]} with at most '
            f'{limit} entries, in the order they appear.\n\n'
            f"---\n{str(blob or '')[:20000]}\n---")
    try:
        data = call_claude_json(system, user, max_tokens=2000)
    except Exception as e:
        print(f"    [!] name extraction failed ({type(e).__name__}: {e}); "
              f"falling back to the text parser")
        return []
    names = [str(x).strip() for x in (data or {}).get("companies", [])
             if str(x).strip()]
    return [n for n in names if 2 < len(n) <= 60][:limit]


# A name the store already has a BOARD for is not worth resolving again.
# Miss rows are excluded on purpose: they carry an `ats` when the board was
# found but rejected (no local jobs, dead board), and a re-paste of such a
# name should be allowed to try again.
_TRACKED_NAMES_SQL = ("SELECT name FROM companies "
                      "WHERE ats IS NOT NULL AND miss_reason IS NULL")


def _blocked_keys(conn):
    """Normalized name keys no path may add: the store's rejection blocklist
    (src.store.blocked_name_keys) union the profile's [discovery]
    name_blocklist."""
    from src.store import blocked_name_keys
    return blocked_name_keys(conn) | set(NAME_BLOCKLIST)


def screen_names(names):
    """Split `names` into (employer-shaped, [(name, reason)]) with
    core.names.junk_name_reason. Runs ahead of every resolution path, because a
    section heading or a category noun that reaches the resolver costs a
    careers-page sniff, two web searches and a mission call before it
    fails (2026-09-01/02 add-names and reresolve runs: "Required
    Qualifications", "Proficiency in SQL.", "Oncology", "99+ results").

    >>> screen_names(["Beacon Biosignals", "Required Qualifications", ""])
    (['Beacon Biosignals'], [('Required Qualifications', 'section-heading'), ('', 'empty')])
    """
    kept, junk = [], []
    for n in names:
        why = junk_name_reason(n)
        (junk if why else kept).append((n, why) if why else n)
    return kept, junk


def _name_state(key, tracked, blocked, missed):
    """Which review bucket a parsed name falls in, given the three key sets
    the store answers with.

    A name nothing on file knows about is the one worth spending requests on:

    >>> _name_state("acmebio", set(), set(), set())
    'new'

    Anything already answered for is not:

    >>> _name_state("acmebio", {"acmebio"}, set(), set())
    'tracked'
    >>> _name_state("oncology", set(), {"oncology"}, set())
    'blocked'
    >>> _name_state("acmebio", set(), set(), {"acmebio"})
    'missed'

    Blocked beats tracked beats missed, so a rejected name still reads as
    rejected when a stale row or miss stamp mentions it too:

    >>> _name_state("x", {"x"}, {"x"}, {"x"})
    'blocked'
    >>> _name_state("x", {"x"}, set(), {"x"})
    'tracked'
    """
    if key in blocked:
        return "blocked"
    if key in tracked:
        return "tracked"
    if key in missed:
        return "missed"
    return "new"


def preview_names(blob, use_llm=None):
    """A pasted page -> the list a person ticks through before anything is
    resolved: ``[{"name", "key", "state"}]``, one entry per distinct name, in
    the order they appear, with `state` from `_name_state`.

    Notes:
        Step one of the two-step paste flow, and the reason it exists: one
        pasted page produced 15 names that were never employers, and
        resolving them cost about a thousand HTTP requests before four of
        them landed on the roster with real boards. Nothing here resolves
        and nothing here writes -- add_names() takes the confirmed list.

        `use_llm=None` (the default) runs extract_names_llm whenever an API
        key is configured and falls back to parse_company_names when it
        returns nothing; True forces the model, False the regex parser.
    """
    from src.store import connect, recent_miss_names
    if use_llm is None:
        use_llm = config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY_HERE"
    names = extract_names_llm(blob) if use_llm else []
    if not names:
        names = parse_company_names(blob)
    conn = connect()
    try:
        tracked = {name_key(r["name"])
                   for r in conn.execute(_TRACKED_NAMES_SQL).fetchall()}
        blocked = _blocked_keys(conn)
        missed = {name_key(n)
                  for n in recent_miss_names(conn)}
    finally:
        conn.close()
    out, seen = [], set()
    for n in names:
        key = name_key(n)
        if not key or key in seen:
            continue
        seen.add(key)
        state = _name_state(key, tracked, blocked, missed)
        row = {"name": n, "key": key, "state": state}
        if state == "new":
            # Employer-shaped? A section heading or a category noun is
            # shown unticked with the reason, so the reviewer can still
            # override it, and is never resolved by default.
            why = junk_name_reason(n)
            if why:
                row.update(state="junk", why=why)
        out.append(row)
    return out


def add_names(names, use_llm=False, max_workers=6, include_missions=None):
    """Resolve company names to boards and queue the ones that verify.

    `names` is the list of names a person confirmed in the review step. A raw
    blob is still accepted -- preview_names() parses it and the `new` names go
    forward -- so the CLI and any single-step caller keep working.

    Uses resolve_board_sniff_first(), not the slug-probe-first resolver: a name
    a person pasted is exactly where a slug collision does the most damage
    (guessing 'sas' lands on an unrelated 5-job board while the real SAS
    Institute sits on iCIMS). Sniffing the company's own careers page cannot
    collide that way; a probe-only hit is reported for a human glance.

    Everything written lands in the review queue (src.store.mark_pending),
    never straight onto the roster.
    """
    from src.store import connect, record_miss

    if isinstance(names, (str, bytes)):
        names = [n["name"] for n in preview_names(names, use_llm=use_llm)
                 if n["state"] == "new"]
    else:
        names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        print("  no company names to resolve.")
        return []

    conn = connect()
    skip = ({name_key(r["name"])
             for r in conn.execute(_TRACKED_NAMES_SQL).fetchall()}
            | _blocked_keys(conn))
    fresh, junk = screen_names([n for n in names if name_key(n) not in skip])
    skipped = len(names) - len(fresh) - len(junk)
    for n, why in junk:
        # Recorded, not resolved: the miss keeps the paste a worklist, and
        # its 'junk-name' family is one no re-resolution pass retries.
        record_miss(conn, n, f"junk-name:{why}", source="paste")
        print(f"    [junk]  {n[:30]:30} {why} - not an employer name, skipped")
    print(f"  {len(names)} name(s) given"
          + (f", {skipped} already tracked or blocked" if skipped else "")
          + (f", {len(junk)} not employer names" if junk else "")
          + f" -> resolving {len(fresh)}...")
    if not fresh:
        return []

    written, unresolved = [], []

    def _stalled(n):
        record_miss(conn, n, "fetch-error:stalled", source="paste")
        unresolved.append((n, "fetch-error:stalled"))

    def _consume(fut, name):
        hit, reason = resolved(fut, name)
        if not hit:
            # A pasted name that resolves to nothing used to be printed
            # once and lost; keep it with a reason so the paste is a
            # worklist, not a one-shot.
            record_miss(conn, name, reason, source="paste")
            unresolved.append((name, reason))
            return
        result = score_and_upsert(conn, hit, source="paste",
                                  include_missions=include_missions)
        if not result:
            return
        row, active, pending = result
        written.append(hit)
        tier = row["mission_tier"]
        # resolve_board_sniff_first's `via` says HOW the board was found:
        # 'sniff' read it off the company's own careers page, 'probe' guessed
        # a slug from the name, 'websearch' only means some result URL
        # matched. The weakest two used to be corroborated (or written
        # inactive) here; the review queue is that check now, and it shows
        # the reviewer which one they are looking at.
        flag = {"probe": "  [slug-guess]",
                "websearch": "  [websearch match]"}.get(hit.get("via"), "")
        state = ("pending review" if pending
                 else "active" if active else "inactive")
        print(f"    [{'queue' if pending else ' ok  '}] {hit['name'][:30]:30} "
              f"{hit['ats']:12} {hit['nc']}/{hit['count']:<5} {str(tier):20} "
              f"{state}{flag}")

    drain(fresh, resolve_or_miss, _consume, _stalled,
          max_workers=max_workers)
    conn.commit()
    conn.close()
    if unresolved:
        print(f"\n  {len(unresolved)} name(s) did not resolve to a live board "
              f"(kept as misses — see the companies table's miss_reason):")
        print("    " + ", ".join(f"{n} [{r}]" for n, r in unresolved[:25])
              + (" ..." if len(unresolved) > 25 else ""))
    print(f"\n  {len(written)} compan(ies) queued for review.")
    return written
