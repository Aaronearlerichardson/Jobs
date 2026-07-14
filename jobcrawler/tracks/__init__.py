"""Job-search tracks.

A *track* is a self-contained search posture built on the shared machinery
(fetchers, discovery, store, filters, scorers, parallel fetch). Each track
supplies its own keyword focus (applied by mutating config's live lists via
``apply_to_config``), its own gates, its own ranking, and a tagged digest.

    remote-neural  — REMOTE roles anchored on neural signals (BCI/EEG/...)
                     with a high technical bar and clinical mission.
    local-tech     — LOCAL (Triangle/NC) roles with a genuine technical bar
"""

from . import local_tech, remote_neural

TRACKS = {
    "remote-neural": remote_neural,
    "local-tech":    local_tech,
}

__all__ = ["TRACKS", "local_tech", "remote_neural"]
