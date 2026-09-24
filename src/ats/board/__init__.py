"""The board engine: every ATS board platform, read from its `config.BOARDS`
spec by one engine.

    engine.py   `Board`, the listing -> row -> detail loop; `BOARDS`,
                `board_for(ats)`, `board_for_url(url)`
    spec.py     the spec schema: the models a spec parses into (`parse`)
    fields.py   the field grammar a spec names values in
    decode.py   response bodies as data (the decoders)
    pager.py    the listing walk, page by page
    custom.py   the careers-page reader behind the self-hosted spec
    jsonld.py   schema.org JobPosting blocks off a page
    company.py  a store row's board pulled, sampled and its postings
                hydrated (`fetch_company`, `hydrate_description`)
    closure.py  one stored posting's open/closed verdict (`probe_job_open`)

Outside this package and config/boards.py, a platform is named in src/
only where tests/test_boards_spec.py's NAMED_PLATFORMS allows.
"""

from .engine import BOARDS, board_for, board_for_url  # noqa: F401
