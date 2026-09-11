"""Central configuration for the job crawler.

Your search criteria live in profile.toml (see profile.example.toml for
the schema); secrets come from environment variables. This package turns
both into the names the rest of the code reads:

    src/config/secrets.py   env vars: API keys, model names            (leaf)
    src/config/paths.py     SCRIPT_DIR / APP_HOME / DATA_DIR, store     (leaf)
    src/config/profile.py   profile.toml loaded; keywords, locations,
                        candidate, résumé, fit, mission, locality,
                        discovery; `profile_section(name)`
    src/config/tracks.py    [tracks.*] tables + engine defaults -> UI_TRACKS
    src/config/policy.py    [policy] + HTTP timeouts / user agents
    src/config/sources.py   [sources]: forums, web search, aggregator feeds

Everything is re-exported here, so `import config; config.X` is the whole
API and callers never name a submodule. Read mutable settings
(ACCEPT_REMOTE, STORE_DB_PATH, the keyword lists) through the package
attribute at use time — src/crawl/runner.py and src/ops/background.py reassign them
while a track runs, and a from-bound copy goes stale.
"""

from .paths import (  # noqa: F401
    APP_NAME, _DB_NAMES, _platform_data_dir, _looks_like_install,
    _resolve_data_dir,
    SCRIPT_DIR, APP_HOME, DATA_DIR, STORE_DB_PATH, REPORT_DIR,
    MAX_DESC_CHARS,
)
from .policy import (  # noqa: F401
    USER_AGENT, PROBE_TIMEOUT, FETCH_TIMEOUT, BROWSER_UA,
    MULTI_DIVISION_COMPANIES, MULTI_DIVISION_MISSION_FLOOR, is_multi_division,
    ACTIVE_MISSION_TIERS, is_active_mission,
    RESPECT_ROBOTS, ROBOTS_EXEMPT_HOSTS, SEARCH_DNS_FALLBACK,
    ROBOTS_CONNECT_TIMEOUT, ROBOTS_READ_TIMEOUT, BROWSER_CHANNELS,
)
from .profile import (  # noqa: F401
    PROFILE_PATH, PROFILE_EXAMPLE_PATH, PROFILE_SOURCE,
    _load_profile, _PROFILE, profile_section,
    CORE_KEYWORDS, DOMAIN_KEYWORDS, SKILL_KEYWORDS, INCLUDE_KEYWORDS,
    keyword_snapshot, restore_keywords, widen_keywords,
    EXCLUDE_PHRASES, EXCLUDE_TITLE_PHRASES, EXCLUDE_BOILERPLATE_PHRASES,
    KEYWORDS_BY_TRACK, EXCLUDE_BY_TRACK,
    ACCEPT_REMOTE, LOCATION_EXCLUDE, LOCATION_INCLUDE,
    REMOTE_LOC_TOKENS, REMOTE_BODY_PHRASES, REMOTE_HARD_NEGATIONS,
    REMOTE_US_MARKERS, REMOTE_NON_US_REGIONS,
    CANDIDATE_SUMMARY, CANDIDATE_STRENGTHS, CANDIDATE_FIT_CAPS,
    CANDIDATE_AVOID,
    RESUME_SUFFIXES, RESUME_PATH,
    FIT_WEIGHTS, FIT_GATE_PENALTY, FIT_DOMAIN_LADDER, FIT_STACK_CORE,
    FIT_STACK_ANTI, FIT_REGION, FIT_DISPOSITION_EXAMPLES,
    FIT_CLEARANCE_VERBS, FIT_CLEARANCE_QUALIFIERS,
    MISSION_TIERS, MISSION_BULLSEYE_REGEX, MISSION_BULLSEYE_TIER,
    LOCALITY_NAME, LOCALITY_WORD_TOKENS, LOCALITY_SUBSTRINGS,
    LOCALITY_STATE_SUFFIX,
    DISCOVERY_SEED_COMPANIES, DISCOVERY_SEED_NAMES, DISCOVERY_SEED_TRIGGERS,
    DISCOVERY_WORKDAY_MAJORS, DISCOVERY_DIRECTORY_URLS,
    DISCOVERY_NAME_SEARCH_QUERIES, DISCOVERY_BRAINSTORM_NAMES,
    DISCOVERY_NAME_BLOCKLIST, DISCOVERY_WEBSEARCH_CAP,
    DISCOVERY_AGGREGATOR_HOSTS, DISCOVERY_GENERIC_NAME_WORDS,
    DISCOVERY_PRIORITY_COMPANIES,
)
from .secrets import (  # noqa: F401
    env,
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
    ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_VERIFY_MODEL,
    CAREERONESTOP_USER_ID, CAREERONESTOP_TOKEN,
    USAJOBS_API_KEY, USAJOBS_EMAIL,
)
from .sources import (  # noqa: F401
    DISCOURSE_BOARDS, WEBSEARCH_QUERIES,
    REMOTEOK_ENABLED, REMOTIVE_ENABLED, REMOTIVE_CATEGORY,
    HNHIRING_ENABLED, HNHIRING_MAX_THREADS,
    USAJOBS_ENABLED, USAJOBS_KEYWORD, USAJOBS_LOCATION, USAJOBS_RADIUS,
    USAJOBS_SERIES, USAJOBS_RESULTS_PER_PAGE,
    GETRO_ENABLED, GETRO_BOARDS, GETRO_MAX_DETAILS,
    _DEFAULT_RSS_FEEDS, RSS_FEEDS,
)
from .tracks import (  # noqa: F401
    _DEFAULT_TRACKS, _DEFAULT_TECH_TITLE_REGEX, _ENGINE_CRAWL_DEFAULTS,
    ENGINE_ALIASES, _mission_floor, _build_ui_tracks,
    UI_TRACKS, DEFAULT_TRACK, track_for_engine,
)
