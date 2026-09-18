"""Board coordinates as the store's columns.

`signatures.detect` reads an ATS and a handle off a URL or a page; the
resolvers in src/discovery/ return the same pair plus a careers URL. Every
path that then writes, probes or de-duplicates that board has to spell the
handle out as store columns -- and because Workday's handle is a (tenant,
pod, site) triple while every other platform has one slug, that spelling
is a seven-line conditional. It was written out at seven call sites across
three modules, each with its own small variation: one forgot the careers
URL, one built the Workday branch without it, one only filled the columns
it happened to need.

One function now. Callers say what they resolved; this says what the
store calls it.
"""

from src.ats import signatures
from src.match.names import SLUG_NAME_SOURCE, name_is_own_slug

#: The columns a board's identity occupies in `companies` (core.store).
BOARD_COLUMNS = ("ats", "slug", "wd_tenant", "wd_pod", "wd_site",
                 "careers_url")


def columns(ats, slug=None, careers_url=None, name=None, **extra):
    """One board's coordinates as store company columns.

    Most platforms carry a single slug:

    >>> columns("greenhouse", "acmebio")["slug"]
    'acmebio'
    >>> columns("greenhouse", "acmebio")["wd_tenant"] is None
    True

    Workday's handle is a (tenant, pod, site) triple, and it goes in the
    three `wd_` columns -- never in `slug`, which must stay NULL or the row
    carries two contradictory identities:

    >>> wd = columns("workday", ("acme", 5, "External"))
    >>> wd["slug"] is None, wd["wd_tenant"], wd["wd_pod"], wd["wd_site"]
    (True, 'acme', 5, 'External')

    A self-hosted board has no handle at all; its identity is the URL
    (core.store.board_key), and the same is true of a hosted PeopleAdmin
    tenant:

    >>> c = columns("custom", None, "https://jobs.ncsu.edu/")
    >>> c["slug"] is None, c["careers_url"]
    (True, 'https://jobs.ncsu.edu/')

    `name` and any further keyword go straight through, so a caller
    building an upsert row writes one dict rather than merging two:

    >>> row = columns("lever", "acme", name="Acme", active=1, source="manual")
    >>> row["name"], row["active"], row["source"]
    ('Acme', 1, 'manual')
    """
    is_wd = ats == "workday"
    out = {
        "ats": ats,
        "slug": None if is_wd else (slug or None),
        "wd_tenant": slug[0] if is_wd else None,
        "wd_pod": slug[1] if is_wd else None,
        "wd_site": slug[2] if is_wd else None,
        "careers_url": careers_url,
    }
    if name is not None:
        out["name"] = name
    out.update(extra)
    return out


def board_slug(company):
    """The one string that names this board on its own host, independent of
    which coordinate column carries it: Workday's `wd_tenant`, or the
    ordinary `slug` otherwise. '' for a careers_url-keyed board (custom,
    successfactors, peopleadmin, wpjson, CAPTURE_ATS) that has neither.

    >>> board_slug({"ats": "workday", "wd_tenant": "aah", "slug": None})
    'aah'
    >>> board_slug({"ats": "lever", "slug": "dominos"})
    'dominos'
    >>> board_slug({"ats": "custom", "slug": None, "wd_tenant": None,
    ...             "careers_url": "https://x.org/careers"})
    ''
    """
    return company.get("wd_tenant") or company.get("slug") or ""


def slug_named(company):
    """True when a roster row is named after nothing but its own board
    slug/tenant AND was named from that slug in the first place -- the one
    rule behind the HARVEST SUMMARY's "still named after their own
    slug/tenant" tally (src.crawl.harvest.run) and the op that repairs
    those rows (src.ops.maintenance.rename_slug_boards).

    >>> slug_named({"name": "Aah", "ats": "workday", "wd_tenant": "aah",
    ...             "source": "ats_dork"})
    True
    >>> slug_named({"name": "Precision for Medicine", "ats": "greenhouse",
    ...             "slug": "pfm", "source": "ats_dork"})
    False

    Both halves are load-bearing. `name_is_own_slug` alone also matches a
    company legitimately named after a single word that happens to equal
    its slug, so a row a human (or local_sourcing) named for real is not
    a candidate:

    >>> slug_named({"name": "Ceribell", "ats": "greenhouse",
    ...             "slug": "ceribell", "source": "local_sourcing"})
    False

    Notes:
        On 2026-09-17 name_is_own_slug alone matched 321 of 579
        harvestable rows, most of them correctly named; restricted to the
        rows src.discovery.dork titled from the slug itself
        (names.SLUG_NAME_SOURCE) it matched 183, which is the list worth
        renaming. See names.name_is_own_slug for what the name half can
        and cannot tell.
    """
    return (company.get("source") == SLUG_NAME_SOURCE
            and name_is_own_slug(company.get("name"), board_slug(company)))


def wd_handle(company, url):
    """The reverse trip: a stored company row plus one job URL back into the
    (tenant, pod, site, path) handle `fetchers.workday.cxs_detail` takes.
    None when the row isn't a Workday board, or the URL isn't a posting.

    The roster's site wins over the URL's, because the URL's segment is
    whatever locale-shaped thing happened to sit in that slot; the URL is
    only asked for the path, which the roster cannot know:

    >>> c = {"ats": "workday", "wd_tenant": "acme", "wd_pod": 5,
    ...      "wd_site": "External"}
    >>> wd_handle(c, "https://acme.wd5.myworkdayjobs.com/Careers/job/RTP/Eng_R1")
    ('acme', 5, 'External', '/job/RTP/Eng_R1')

    A row whose triple is incomplete has no handle -- half a triple builds a
    CXS URL that 404s, and the caller's fallback (fetch the rendered page)
    is the correct answer instead:

    >>> wd_handle({"ats": "workday", "wd_tenant": "acme"},
    ...           "https://acme.wd5.myworkdayjobs.com/X/job/y") is None
    True
    >>> wd_handle({"ats": "greenhouse", "slug": "acme"}, "https://x/job/y") is None
    True
    """
    if company.get("ats") != "workday":
        return None
    if not (company.get("wd_tenant") and company.get("wd_pod")):
        return None
    found = signatures.workday_job_path(url)
    if not found:
        return None
    site_from_url, path = found
    return (company["wd_tenant"], company["wd_pod"],
            company.get("wd_site") or site_from_url, path)


def from_hit(hit, name=None, **extra):
    """The same, from a resolver's hit dict ({ats, slug, careers_url, ...}).

    >>> hit = {"ats": "workday", "slug": ("acme", 5, "Ext"),
    ...        "careers_url": "https://acme.com/careers", "nc": 3}
    >>> row = from_hit(hit, name="Acme")
    >>> row["name"], row["wd_pod"], row["careers_url"]
    ('Acme', 5, 'https://acme.com/careers')

    Only the coordinates are read; the rest of the hit (counts, provenance)
    is the caller's to carry, so nothing leaks into the row by accident:

    >>> "nc" in row
    False
    """
    return columns(hit["ats"], hit.get("slug"), hit.get("careers_url"),
                   name=name, **extra)
