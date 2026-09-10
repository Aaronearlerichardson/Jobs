# [GOLDEN] Job Digest — 2026-09-10

## Your pipeline

Managed with `run_scraper.py --mark DISPOSITION JOB` (saved stays in the ranking; the rest live here).

| Disposition | When | Company | Title | Posting | Note |
|---|---|---|---|---|---|
| applied | 2026-09-02 | Acme | [Applied Role](https://acme.io/p1) | open | referral |
| interviewing | 2026-08-20 | Beta | [Closed Today](https://beta.io/p2) | CLOSED |  |
| applied | 2026-08-01 | Gamma | [Closed Before](https://gamma.io/p3) | CLOSED |  |

## Follow-ups due

Live applications whose follow-up date has arrived (set in the Pipeline tab).

| Due | Company | Title | Contact | Disposition |
|---|---|---|---|---|
| 2026-09-10 | Acme | [Chase f1](https://acme.io/f1) | Recruiter | applied |
| 2026-09-08 | Acme | [Chase f2](https://acme.io/f2) |  | applied |

## Apply band

Open local postings scored 0.40 to 0.70 that you have not decided on, best fit first. Interviews have come from this band rather than the top of the ranking, so this is where application volume goes.

| Fit | Age | Company | Title | Location |
|----:|----:|---------|-------|----------|
| 0.62 | 71d! | Acme | [Role r4](https://acme.io/r4) | Hometown, ZZ |
| 0.55 | NEW | Acme | [Role r2](https://acme.io/r2) | Hometown, ZZ |

## Watched companies — new postings this run

Flagged regardless of rank or geography (`run_scraper.py --watch NAME` manages the list).

- **Watched Co** — [Watched Role](https://w.co/1) — Hometown, ZZ *(scored)*
- **Listed Co** — [Listed Role](https://l.co/2) — ? *(listed only, outside local scope)*

**5 open job(s)** (closed, dismissed, and in-pipeline postings excluded), ranked by resume fit (combined = sqrt(resume-fit x company-mission), shown for reference). Age is days since the board's posting date (NEW = first seen today, ! = 45d+ stale, ? = date unknown).

| Fit | Combined | Age | Company | Mission | Title | Location | Why |
|----:|---------:|----:|---------|---------|-------|----------|-----|
| 0.91 | 0.80 | 6d | Acme | A | [Role r1](https://acme.io/r1) | Hometown, ZZ | reason |
| 0.55 | n/a | NEW | Acme | ? | [Role r2](https://acme.io/r2) | Hometown, ZZ | reason |
| n/a | n/a | NEW | Acme | ? | [Role r3](https://acme.io/r3) | Remote - US |  |
| 0.62 | n/a | 71d! | Acme | ? | [Role r4](https://acme.io/r4) | Hometown, ZZ | reason |
| 0.45 | n/a | NEW | Acme | ? | [Role r5](https://acme.io/r5) | Hometown, ZZ | reason |
