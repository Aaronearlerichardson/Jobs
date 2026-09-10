"""The digest: the ranked report, rendered and delivered.

`render` holds it; the public functions are re-exported here so callers
say `digest.write_ranked_digest(...)`.
"""

from .render import (                                        # noqa: F401
    APPLY_BAND_LIMIT, age_tag, apply_band_rows, new_ranked_rows,
    write_ranked_digest, send_ranked_digest, toast,
    write_matches_digest, send_matches_digest,
)
