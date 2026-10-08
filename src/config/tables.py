"""The data tables shipped as .toml beside this module, loaded once.

Each table's rationale is the comment header of its .toml file. The build
bundles them by globbing src/config/*.toml (build_app.py).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TypedDict, cast


def load_table[T](name: str) -> T:
    """The parsed src/config/<name>.toml, typed as the caller's annotation says.

    >>> sorted(load_table("places"))
    ['codes', 'regions', 'segment_names']
    """
    return cast(T, tomllib.loads((Path(__file__).parent / f"{name}.toml").read_text("utf-8")))


class NihSbirSpec(TypedDict):
    url: str
    activity_codes: list[str]
    page: int
    max_offset: int
    include_fields: list[str]
    blurb_field: str


class OpenFdaSpec(TypedDict):
    url: str
    limit: int
    count: str
    search: str
    specialty_search: str


class Registries(TypedDict):
    nih_sbir: NihSbirSpec
    openfda_devices: OpenFdaSpec


NON_US_PLACES: dict[str, list[str]] = load_table("places")
JUNK_NAME_WORDS: dict[str, list[str]] = load_table("junk_names")
REGISTRIES: Registries = load_table("registries")
DEFAULT_TRACKS: dict[str, dict[str, str | float | bool]] = load_table("default_tracks")
