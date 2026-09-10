"""The job crawler's package tree.

Entry points stay at the repository root (run_scraper.py, discover.py,
harvest.py, webapp.py, capture.py, crawler.py); everything they drive
lives here, grouped by what it does rather than by when it was written:

    config/     profile.toml, tracks, paths, secrets, policy  (leaf)
    src/tags.py     the company scope-tag vocabulary              (leaf)
    store/      the SQLite store: schema, roster, jobs, ranking
    claude/     the Claude API, resume-fit scoring, mission scoring
    match/      the free gates: titles, keywords, geography, remote, names
    digest/     the ranked digest: rendering and delivery
    ats/        everything applicant-tracking-system specific --
                signatures, the fetcher registry, one fetcher per ATS,
                and the search-engine dork that finds new boards
    net/        HTTP, robots, the fetch pool, DuckDuckGo, small helpers
    crawl/      the three passes over boards: crawl, harvest, triage
    discovery/  finding companies worth crawling in the first place
    ops/        every operation a front end can run, in one registry
    web/        the Flask UI
"""
