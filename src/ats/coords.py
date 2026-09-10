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
