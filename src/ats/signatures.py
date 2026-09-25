"""ATS signatures: recognise a hosted applicant-tracking board from a URL or
a page body, and read its handle off the match.

Pure text layer over the specs' `detect` entries (config.BOARDS, read by
`Board.detect`), no network and no imports from src/discovery/ or
src/crawl/, so both the discovery paths (careers-page sniffer, Workday
probes, ATS dorking, web-search resolution) and the crawl-side consumers
share one definition of "what is a board".
"""

import html

from .board import BOARDS, board_for

# The captured slug of a subdomain or path segment can be structural rather
# than a board: the vendor's own site (www.bamboohr.com, help.applytojob.com),
# or an embed/asset path (boards.greenhouse.io/embed/job_board?for=<real
# slug>, boards.greenhouse.io/js). Never a board the crawl can fetch. One
# list for every consumer -- the sniffer's page detection and the dork's URL
# harvest used to keep different halves of it.
BAD_SLUGS = frozenset({
    "www", "help", "support", "blog", "app", "careers", "jobs", "secure",
    "embed", "job_board", "js", "boards", "job-boards", "search", "api",
})


def _board_part(part):
    """Whether a fetchable detection's first part can name a board."""
    return len(part) >= 2 and part.lower() not in BAD_SLUGS


def _lead_part(part):
    return len(part) >= 2


def detect(text, final_url="", leads=True, only=None):
    """Scan text + final URL (HTML entities decoded) for an ATS signature.

    Returns (kind, ats, slug) or None: kind "fetchable" for a spec with a
    listing, else "lead"; the slug a tuple where the handle spans several
    store columns (Workday's tenant, pod and site), else a string. The
    specs are tried in config.BOARDS order, each over its `detect` entries
    (`Board.detect`); a fetchable detection's first part is at least two
    characters and not in BAD_SLUGS, a lead's at least two. `leads=False`
    answers only with a board the crawl can fetch; `only` names the one
    platform to look for.

    >>> detect("", "https://boards.greenhouse.io/acmebio/jobs/1")
    ('fetchable', 'greenhouse', 'acmebio')
    >>> detect("", "https://acme.wd5.myworkdayjobs.com/en-US/External")
    ('fetchable', 'workday', ('acme', 5, 'External'))
    >>> detect("<a href='https://acme.icims.com/jobs'>Jobs</a>")
    ('fetchable', 'icims', 'acme')
    >>> detect('{"widgetApiEndpoint":"https://careers.acme.org/widgets"}')
    ('fetchable', 'phenom', 'careers.acme.org')
    >>> detect("", "https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/page/"
    ...            "JobsHomePage?csk.JobBoard=EXTERNAL&csk.HROrganization=42")
    ('fetchable', 'infor', 'css-acme-prd.inforcloudsuite.com|42')
    >>> detect("", "https://apply.workable.com/acme-aps/j/D68529D654/")
    ('fetchable', 'workable', 'acme-aps')
    >>> detect("via acme.eightfold.ai portal")
    ('lead', 'eightfold', 'acme.eightfold.ai')
    >>> detect("via acme.eightfold.ai portal", leads=False) is None
    True

    Workday's CXS API URL (the tenant twice) is read first; an API or
    asset segment in the site slot is not a board:

    >>> detect("", "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Jobs/jobs")[2]
    ('acme', 5, 'Jobs')
    >>> detect("", "https://acme.wd5.myworkdayjobs.com/wday/authgwy") is None
    True

    A URL that names a POSTING but not the board it belongs to names no
    board at all -- Workable's slug-less short link is the shape:

    >>> detect("", "https://apply.workable.com/j/D68529D654") is None
    True

    A vendor's own site or an embed path is not a board (BAD_SLUGS):

    >>> detect("", "https://www.bamboohr.com/") is None
    True
    >>> detect("", "https://boards.greenhouse.io/embed/job_board/js?for=acme") is None
    True
    >>> detect("", "https://example.com/careers") is None
    True
    """
    blob = html.unescape(f"{final_url}\n{text}")
    for b in (BOARDS.get(only),) if only else BOARDS.values():
        if b is None or not (b.fetchable or leads):
            continue
        slug = b.detect(blob, _board_part if b.fetchable else _lead_part)
        if slug:
            return ("fetchable" if b.fetchable else "lead"), b.name, slug
    return None


def pack(ats, slug, careers_url):
    """A detection -> the coordinate dict every resolver consumes.

    A handle spanning several store columns (Workday's (tenant, pod, site))
    travels as a tuple under `triple`; every other platform has one slug:

    >>> pack("greenhouse", "acme", "https://acme.com/careers")["slug"]
    'acme'
    >>> pack("workday", ("acme", 5, "External"), "")["triple"]
    ('acme', 5, 'External')

    A spec whose `detect` rebuilds the board's URL (keyed on `careers_url`,
    src.store.board_key) gets it from the slug or the page, so whichever
    page of the tenant carried the signature, the board comes out the same:

    >>> pack("peopleadmin", "unc",
    ...      "https://unc.peopleadmin.com/postings/search?x=1")["careers_url"]
    'https://unc.peopleadmin.com'
    >>> pack("successfactors", "performancemanager4",
    ...      "https://careers.acme.org/search/?q=eng")["careers_url"]
    'https://careers.acme.org'

    Nothing else is rewritten, including a PeopleAdmin tenant on its own
    hostname, which never matches the signature and reaches the store by
    hand instead:

    >>> pack("custom", None, "https://jobs.ncsu.edu/")["careers_url"]
    'https://jobs.ncsu.edu/'
    """
    b = board_for(ats)
    rebuilt = b.careers_url(slug, careers_url) if b and slug else None
    out = {"ats": ats, "careers_url": rebuilt or careers_url}
    out["triple" if b and b.multi_column else "slug"] = slug
    return out
