"""The profile.toml schema: one pydantic model per section, and every
profile default.

`parse` validates a whole profile once, at load; `problems` returns what
is wrong with one as path-qualified lines (the Settings tab's validator).
Every table rejects keys it does not declare, so a misspelled key is an
error rather than a silent default.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (AfterValidator, AliasChoices, BaseModel, BeforeValidator,
                      ConfigDict, Field, ValidationError, ValidationInfo,
                      field_validator, model_validator)

from src import tags


class ProfileError(ValueError):
    """A profile that does not match the schema. `lines` holds one
    'path: problem' entry per bad key."""

    def __init__(self, source, lines):
        self.lines = lines
        super().__init__(f"{source} does not match the profile schema:\n"
                         + "\n".join(f"  {ln}" for ln in lines))


class _Table(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _filled(v):
    if not v.strip():
        raise ValueError("must not be blank")
    return v.strip()


def _regex(v):
    try:
        re.compile(v)
    except re.error as e:
        raise ValueError(f"not a valid regex ({e})") from None
    return v


def _false_is_none(v):
    return None if v is False else v


def _table_only(v):
    if not isinstance(v, dict):
        raise ValueError("unknown key")
    return v


Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Count = Annotated[int, Field(ge=0)]
Filled = Annotated[str, AfterValidator(_filled)]
Regex = Annotated[str, AfterValidator(_regex)]


# --- [keywords] / [exclude] -------------------------------------------------
# Both mix fixed keys with per-track sub-tables ([keywords.<track id>]).
# Any key that is not a declared field must be such a table.

class TrackKeywords(_Table):
    core: list[str] = []
    domain: list[str] = []
    skill: list[str] = []


class Keywords(TrackKeywords):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, Annotated[TrackKeywords,
                                            BeforeValidator(_table_only)]]


class TrackExclude(_Table):
    role_phrases: list[str] = []
    title_tokens: list[str] = []
    defense_strong: list[str] = []
    defense_weak: list[str] = []
    nonclinical: list[str] = []
    clinical_titles: list[str] = []
    clinical_markers: list[str] = []


class Exclude(_Table):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, Annotated[TrackExclude,
                                            BeforeValidator(_table_only)]]
    phrases: list[str] = []
    title_phrases: list[str] = []
    title_exempt_phrases: list[str] = []
    boilerplate_phrases: list[Regex] = []


class Locations(_Table):
    onsite: list[str] = []
    remote: list[str] = []
    accept_remote: bool = False
    exclude: list[str] = []
    remote_tokens: list[str] = []
    remote_phrases: list[str] = []
    hard_negations: list[str] = []
    us_markers: list[str] = []
    non_us_regions: list[str] = []


# --- [tracks.<id>] ----------------------------------------------------------

#: The default technical-title gate: a posting whose TITLE doesn't match
#: this never costs an API call. Broad and field-neutral on purpose; narrow
#: or widen it per track with `tech_title_regex`.
DEFAULT_TECH_TITLE_REGEX = (
    r"\b("
    r"engineer|engineering|developer|develop|software|programmer|programming|"
    r"architect|devops|sre|reliability|infrastructure|platform|security|"
    r"data|database|analyst|analytics|quantitative|"
    r"scientist|science|sciences|scientific|research|researcher|"
    r"ml|machine learning|deep learning|ai|algorithm|algorithms|modeling|"
    r"simulation|computational|"
    r"informatic\w*|bioinformatic\w*|statistic\w*|biostatistic\w*|"
    r"epidemiolog\w*|"
    r"firmware|hardware|embedded|robotics|systems|automation|technologist|"
    r"quality|validation|verification|qa|test|r&d|python"
    r")\b"
)


class TrackSources(_Table):
    store: bool = True
    priority_companies: bool = False
    aggregators: bool = False
    websearch: bool = False
    location_scoped: bool = True


class Methodology(_Table):
    """A track's crawl methodology. The class defaults are the "local"
    engine's; ENGINE_DEFAULTS holds each engine's bundle, and a track
    table overrides any key it sets (see src/config/tracks.py for what
    each key does)."""
    keyword_mode: Literal["extend", "replace"] = "extend"
    accept_remote: bool = False
    sources: TrackSources = Field(default_factory=TrackSources)
    store_tag: Annotated[str | None,
                         AfterValidator(lambda v: tags.canonical(v) or None)
                         ] = None
    require_core_anchor: bool = False
    geo_gate: bool = True
    # TOML has no null: `false` switches the admission off.
    remote_mission_floor: Annotated[Unit | None,
                                    BeforeValidator(_false_is_none)] = 0.85
    verify_top: Count = 15
    verify_floor: Unit = 0.25
    cost_guard: Count = 0
    email: bool = False
    digest_min_fit: Unit = 0.4
    notify: bool = False
    exclude_gate: bool = True
    dormant_after: Annotated[int, Field(ge=1)] = 4
    dormant_days: Annotated[int, Field(ge=1)] = 7
    tech_title_regex: Regex = DEFAULT_TECH_TITLE_REGEX


ENGINE_DEFAULTS = {
    # A location-scoped crawl of the companies in your store: asks each
    # board for YOUR region, so it stays cheap on huge employers.
    "local": Methodology(),
    # A location-AGNOSTIC sweep: whole boards, aggregator feeds and web
    # search, gated hard on CORE keywords.
    "sweep": Methodology(
        keyword_mode="replace", accept_remote=True,
        sources=TrackSources(priority_companies=True, aggregators=True,
                             websearch=True, location_scoped=False),
        store_tag=tags.SWEEP, require_core_anchor=True, geo_gate=False,
        verify_top=0, cost_guard=300, exclude_gate=False),
}

#: Retired engine name -> current one, so an older profile keeps working.
ENGINE_ALIASES = {"neural": "sweep"}


def _engine(v):
    v = ENGINE_ALIASES.get(v, v)
    if v not in ENGINE_DEFAULTS:
        raise ValueError("unknown engine; expected "
                         + " or ".join(ENGINE_DEFAULTS))
    return v


class Track(Methodology):
    """One [tracks.<id>] table, resolved: every methodology key it leaves
    out is filled from its engine's defaults, and a blank string means the
    key is unset (src.config.tracks._build_ui_tracks' doctest shows both).
    `db`, when present, must name a file."""
    label: str = ""                   # "" -> the track id
    db: Filled | None = None          # None -> "<id>.db"
    track: str = ""                   # "" -> the id, "_" -> "-"
    engine: Annotated[str, AfterValidator(_engine)] = "local"
    rank_by: Literal["fit", "combined"] = "fit"
    min_mission: Unit | None = None
    min_fit_default: Unit = 0.0
    willing_to_move_default: bool = False
    remote_requires_watch: bool = False
    default: bool = False

    @model_validator(mode="before")
    @classmethod
    def _blank_is_unset(cls, data):
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items()
                if k == "db" or not (isinstance(v, str) and not v.strip())}

    @model_validator(mode="after")
    def _engine_fills_the_rest(self):
        eng = ENGINE_DEFAULTS[self.engine]
        fill = {n: getattr(eng, n) for n in Methodology.model_fields
                if n not in self.model_fields_set}
        if "sources" in self.model_fields_set:
            mine = self.sources
            fill["sources"] = eng.sources.model_copy(
                update=mine.model_dump(include=mine.model_fields_set))
        return self.model_copy(update=fill)


#: The built-in pair used when a profile has no [tracks] section.
DEFAULT_TRACKS = {
    "local": {
        "label": "Local", "db": "jobs.db", "track": "local", "engine": "local",
        "rank_by": "fit", "min_mission": 0.2, "min_fit_default": 0.0,
        "willing_to_move_default": False, "remote_requires_watch": True,
        "default": True,
    },
    "remote": {
        "label": "Remote", "db": "jobs.db", "track": "remote",
        "engine": "sweep", "rank_by": "fit", "min_fit_default": 0.5,
        "willing_to_move_default": True, "remote_requires_watch": False,
        "default": False,
    },
}


# --- [policy] ----------------------------------------------------------------

class Policy(_Table):
    multi_division: list[str] = []
    multi_division_mission_floor: Unit = 0.6
    watch_division_titles: list[str] = []
    harvest_offmission_hours: Annotated[float, Field(ge=0)] = 168.0
    board_max_rows: Annotated[int, Field(ge=1)] = 3000
    respect_robots: bool = True
    robots_exempt_hosts: list[str] = []
    search_dns_fallback: list[str] = ["1.1.1.1", "8.8.8.8"]
    robots_connect_timeout: Annotated[float, Field(gt=0)] = 3.0
    robots_read_timeout: Annotated[float, Field(gt=0)] = 10.0
    browser_channels: list[str] = ["", "chrome", "msedge"]


# --- [candidate] / [mission] / [locality] ----------------------------------

class Candidate(_Table):
    summary: str = ""
    strengths: list[str] = []
    fit_caps: list[str] = []
    avoid: str = ""
    resume: str = ""


def _ordered(band):
    if band[0] > band[1]:
        raise ValueError("must be [lo, hi] with lo <= hi")
    return band


class MissionTier(_Table):
    name: Filled
    desc: str = ""
    band: Annotated[list[Unit], Field(min_length=2, max_length=2),
                    AfterValidator(_ordered)] = [0.0, 1.0]
    active: bool = True


class Mission(_Table):
    tiers: list[MissionTier] = []
    bullseye_regex: Regex = ""
    bullseye_tier: str = ""

    @model_validator(mode="after")
    def _bullseye_names_a_tier(self):
        names = {t.name for t in self.tiers}
        if names and self.bullseye_tier.strip() and (
                self.bullseye_tier.strip() not in names):
            raise ValueError("bullseye_tier names no tier in `tiers`")
        return self


class Locality(_Table):
    name: str = "local"
    word_tokens: list[str] = []
    substrings: list[str] = []
    state_suffix: list[str] = []

    @field_validator("name")
    @classmethod
    def _blank_is_local(cls, v):
        return v.strip() or "local"


# --- [sources] ---------------------------------------------------------------

class Discourse(_Table):
    label: str = ""
    url: Filled
    category_id: int = 0


class WebSearch(_Table):
    label: str = ""
    query: Filled
    max_results: Annotated[int, Field(ge=1)] = 12


class RssFeed(_Table):
    label: str = ""
    url: Filled
    location: str = "Remote"


def _default_rss():
    wwr = "https://weworkremotely.com/categories"
    return [RssFeed(label="WeWorkRemotely - Programming",
                    url=f"{wwr}/remote-programming-jobs.rss"),
            RssFeed(label="WeWorkRemotely - All Other",
                    url=f"{wwr}/all-other-remote-jobs.rss"),
            RssFeed(label="Jobicy - All Remote",
                    url="https://jobicy.com/?feed=job_feed")]


class Usajobs(_Table):
    enabled: bool = False
    keyword: str = ""
    location: str = ""
    radius: Count = 50
    # Absent -> the fetcher's technical set; [] -> every series.
    series: list[str] | None = None
    results_per_page: Annotated[int, Field(ge=1)] = 250


class Getro(_Table):
    enabled: bool = False
    boards: list[str] = []
    max_details: Count = 150


class Sources(_Table):
    remoteok: bool = True
    remotive: bool = True
    remotive_category: str = ""
    hnhiring: bool = True
    hnhiring_max_threads: Count = 2
    discourse: list[Discourse] = []
    websearch: list[WebSearch] = []
    # Absent -> these general remote-job feeds; [] -> no RSS at all.
    rss: list[RssFeed] = Field(default_factory=_default_rss)
    usajobs: Usajobs = Field(default_factory=Usajobs)
    getro: Getro = Field(default_factory=Getro)


# --- [discovery] -------------------------------------------------------------

class SeedCompany(_Table):
    name: Filled
    notes: str = ""


class PriorityCompany(_Table):
    name: Filled
    ats: Filled
    slug: Filled


class Discovery(_Table):
    # A bare name, or a { name, notes } table.
    seed_companies: list[Annotated[SeedCompany, BeforeValidator(
        lambda v: {"name": v} if isinstance(v, str) else v)]] = []
    seed_triggers: list[str] = []
    # Big employers worth the slow careers-page scan (`discovery.scan`);
    # "workday_majors" is its old name.
    scan_majors: list[str] = Field([], validation_alias=AliasChoices("scan_majors",
                                                                     "workday_majors"))
    directory_urls: list[str] = []
    name_search_queries: list[str] = []
    brainstorm_names: Count = 50
    name_blocklist: list[str] = []
    websearch_cap: Count = 20
    aggregator_hosts: list[str] = []
    generic_name_words: list[str] = []
    priority_companies: list[PriorityCompany] = []


# --- [fit] -------------------------------------------------------------------
# A profile's weights / gate_penalty tables MERGE over these: a key it
# leaves out keeps its default, and a key not declared here is an error.

class FitWeights(_Table):
    domain: Unit = 0.25
    function: Unit = 0.34
    stack: Unit = 0.33
    seniority: Unit = 0.08


class GatePenalty(_Table):
    geo: Unit = 0.20
    embedded: Unit = 0.40
    level: Unit = 0.35
    phd: Unit = 0.45
    management: Unit = 0.35
    clearance: Unit = 0.45


class LadderRung(_Table):
    score: Unit
    terms: list[str]


class Fit(_Table):
    weights: FitWeights = Field(default_factory=FitWeights)
    gate_penalty: GatePenalty = Field(default_factory=GatePenalty)
    domain_ladder: list[LadderRung] = []
    stack_core: list[str] = []
    stack_anti: list[str] = []
    region_terms: list[str] = []
    disposition_examples: Count = 3
    clearance_verbs: list[str] = []
    clearance_qualifiers: list[str] = []


# --- the whole profile -------------------------------------------------------

def _builtin_tracks():
    return {tid: Track.model_validate(t) for tid, t in DEFAULT_TRACKS.items()}


class Profile(_Table):
    # `tracks` first: the per-track [keywords.<id>] / [exclude.<id>] checks
    # below read the validated track ids.
    tracks: dict[str, Track] = Field(default_factory=_builtin_tracks)
    keywords: Keywords = Field(default_factory=Keywords)
    exclude: Exclude = Field(default_factory=Exclude)
    locations: Locations = Field(default_factory=Locations)
    policy: Policy = Field(default_factory=Policy)
    candidate: Candidate = Field(default_factory=Candidate)
    mission: Mission = Field(default_factory=Mission)
    locality: Locality = Field(default_factory=Locality)
    sources: Sources = Field(default_factory=Sources)
    discovery: Discovery = Field(default_factory=Discovery)
    fit: Fit = Field(default_factory=Fit)

    @field_validator("tracks", mode="before")
    @classmethod
    def _empty_is_builtin(cls, v):
        return v or DEFAULT_TRACKS

    @field_validator("keywords", "exclude")
    @classmethod
    def _tables_name_tracks(cls, v, info: ValidationInfo):
        ids = info.data.get("tracks", v.model_extra)
        stray = sorted(k for k in v.model_extra if k not in ids)
        if stray:
            raise ValueError(", ".join(f"[{info.field_name}.{k}]"
                                       for k in stray)
                             + " names no [tracks.*] table")
        return v


_PHRASES = {"missing": "required", "extra_forbidden": "unknown key",
            "model_type": "must be a table", "dict_type": "must be a table"}


def _line(err):
    path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                   for p in err["loc"]).lstrip(".")
    ctx = err.get("ctx") or {}
    msg = _PHRASES.get(err["type"]) or str(ctx.get("error") or err["msg"])
    return f"{path}: {msg}" if path else msg


def parse(raw, source="profile"):
    """`raw` (a parsed profile.toml) as a validated Profile, or ProfileError
    listing every bad key path (the lines `problems` returns)."""
    try:
        return Profile.model_validate(raw)
    except ValidationError as e:
        raise ProfileError(source, [_line(x) for x in e.errors(
            include_url=False, include_input=False)]) from None


def problems(raw):
    """What is wrong with `raw`, one 'path: problem' line per bad key; []
    when it is a valid profile. A line names the key, never its value: a
    profile holds personal data.

    >>> problems({"keywords": {"core": ["neuro"]}})
    []
    >>> for line in problems({
    ...         "tracks": {"t": {"engine": "rocket"}},
    ...         "keywords": {"core": "neuro", "t": {"skill": ["c"]}},
    ...         "mission": {"tiers": [{"desc": "no name"}]},
    ...         "sources": {"rss": [{"label": "no url"}]},
    ...         "fit": {"weights": {"domain": 0.5, "vibes": 0.1}}}):
    ...     print(line)
    tracks.t.engine: unknown engine; expected local or sweep
    keywords.core: Input should be a valid list
    mission.tiers[0].name: required
    sources.rss[0].url: required
    fit.weights.vibes: unknown key

    Weights merge over the defaults rather than replacing them:

    >>> parse({"fit": {"weights": {"domain": 0.5}}}).fit.weights.model_dump()
    {'domain': 0.5, 'function': 0.34, 'stack': 0.33, 'seniority': 0.08}
    """
    try:
        parse(raw)
    except ProfileError as e:
        return e.lines
    return []
