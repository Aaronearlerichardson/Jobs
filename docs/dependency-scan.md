# Dependency scan: should any hand-written code give way to a package?

2026-09-30. For each domain the code base implements itself, which PyPI
packages claim the same job, what would they delete, what would they cost,
and what does a measurement say. The scan found one package worth adopting
(`protego`) and one bug that needs no package (unescaped HTML in the
digest). Everything else is rejected, most of it with a number.

## How it works, and what it cannot do

- **Domains** are the places the code does a job a library also does (HTML
  parsing, robots.txt, the Anthropic client, path reading, ...), with the
  functions that do it and their line counts (measured by AST).
- **Candidates** per domain are curated by hand. PyPI has no search API, so
  the scan checks the packages named in its table; it cannot discover ones
  nobody listed. Add a name to the table and rerun.
- **Gates** read PyPI's JSON API for each candidate: a release within 24
  months, `requires-python` admitting 3.12 to 3.14, wheels (pure Python, or
  binary for Windows, macOS and Linux, since the project ships Nuitka
  builds on all three), a permissive licence (copyleft is refused for a
  shipped binary), and the packages pip would newly install next to the
  environment the repo already runs in.
- **Measurements** decide what the gates cannot: speed on the repo's own
  fixtures, and parity of behaviour against the repo's own tests or a
  differential corpus.
- The scripts are in the session scratchpad, not the repo. Say the word and
  they go in as one `tools/depscan.py`.

## The BeautifulSoup test

BeautifulSoup is a candidate for HTML parsing and selection, and the scan
rejects it on performance. Parse a page and find every `<a href>`, the
anchor counts agreeing across all parsers:

| parser | 27 real fixtures (63 KB) | 499 KB listing, 1050 anchors | 2 MB listing, 4305 anchors |
|---|---|---|---|
| **current: lxml via `parse_markup`** | 1.3 ms | 8.7 ms | 44.7 ms |
| selectolax (Lexbor) | 3.4 ms (x2.6) | 8.4 ms (x1.0) | 36.2 ms (x0.8) |
| bs4 + lxml | 18.9 ms (**x14**) | 152 ms (**x17**) | 695 ms (**x16**) |
| bs4 + html.parser | 24.7 ms (**x19**) | 226 ms (**x26**) | 925 ms (**x21**) |
| bs4 + html5lib | 50.9 ms (x39) | 436 ms (x50) | 1838 ms (x41) |

The speed is the smaller half of the problem. The crawler parses in worker
threads (`asyncio.to_thread`), and bs4 builds its tree in Python, holding the
GIL. Twenty 499 KB pages through four worker threads, with a 1 ms ticker on
the event loop (two runs each):

| parser | wall time | worst gap the loop went unserved |
|---|---|---|
| current: lxml | 0.17 to 0.19 s | 12 to 15 ms |
| bs4 + lxml | 15.5 to 16.9 s (**about 90x**) | 90 to 125 ms |
| bs4 + html.parser | 4.8 to 4.9 s | 196 to 217 ms (p99 120 to 144 ms) |

The repo's test harness fails a test in which a callback holds the loop
for 100 ms (`conftest.loop_blocks`); a 200 ms gap is well past the budget
the async code lives by. It would also delete nothing: lxml is already the parser, and
`parse_markup`, `xpath` and `css` (101 lines) would remain as the bs4 layer.
The code also rejects soupsieve's `:-soup-contains` on purpose
(`ats/board/spec.py`), and its `text_from_html` docstring records that eight of nine
fetchers once used bs4's `get_text` before they were unified.

### Second test: was bs4 used badly, and does using it well close the gap?

The first table used bs4 the plain way. The objection is that the old code
did not use it competently, so this reads the history and then repeats the
measurement with bs4 configured as its documentation recommends.

**What the old code did.** `git show 0c10887^:src/net/util.py` (the commit
that removed bs4) shows `BeautifulSoup(markup, "xml" if xml else "lxml")`: the
fast tree builder was already chosen, so `html.parser` was not the problem.
It passed no `parse_only`. An earlier commit, `3e160fe`, had used
`SoupStrainer("a")` on the board-detection path and measured 1.6 ms against
2.9 ms for `html.parser`, but that compared bs4 with bs4 and never with lxml.
The removal commit gives no timing.

**The same work, five ways.** bs4 4.15.0 on lxml, `parse_only` wherever the
task allows it, on the 27 fixtures, a 499 KB page and a 2 MB page. "Same
output" means the result equals the lxml result on every page.

| task (real call site) | bs4, full tree | bs4, `parse_only` | same output |
|---|---|---|---|
| T1 every `<a href>` with its text (board detection) | x8.6 / x10.2 / x11.9 | x4.5 / x4.3 / x4.1 | yes |
| T2 anchors that sit in a nav element or a nav-named container (`find_job_links`) | x9.3 / x9.9 / x11.0 | x8.2 / x4.5 / x4.6 | **no with `parse_only`**: it drops the ancestors the test reads |
| T3 `ld+json` script bodies (`parse_jsonld`) | x16.8 / x14.2 / x18.3 | x5.5 / x5.0 / x5.1 | yes |
| T4 whole-page text (`text_from_html`) | x9.8 / x19.4 / x21.8 | no strainer possible | text differs on some fixtures |
| T5 several reads per card (the spec engine's shape) | x6.3 / x9.2 / x4.9 | no strainer possible | yes |

(Each cell is fixtures / 499 KB / 2 MB, as a multiple of the current lxml time.)

Parsing is where the time goes, not querying. On the 499 KB page lxml parses
in 8.7 ms; bs4 takes 124 ms for the full tree and 58.5 ms with `parse_only`;
finding the anchors in an already-built bs4 tree takes 5.2 ms. A better query
cannot recover it, and `parse_only` still calls Python for every element, so
it removes tree building but not the callbacks. Where the work needs
surrounding elements (T2, T4, T5) no strainer applies.

The event-loop test again, 20 pages of 499 KB through 4 worker threads, T1
work, two runs each: lxml took 0.29 to 0.31 s; bs4 with `parse_only` took
10.8 to 11.1 s (about 36 times) and bs4 on the full tree took 15.0 to
15.5 s (about 50 times). The worst loop gap was 41 to 42 ms for lxml, 59 to
65 ms with `parse_only` and 133 to 150 ms for the full tree. (The first
table's 12 to 15 ms for lxml came from a quieter run on the same shared
machine; the order of the three is the same in both.)

**Verdict unchanged.** The old code chose the right builder, and bs4 used
well is still x4 to x5 slower where a strainer applies and x5 to x22 where it
does not. It would also mean rewriting the XPath queries (`xpath(` is called 13 times
outside doctests), which bs4 does not support. The only place the cost would not matter is
`page_capture`, which parses a page the user saved by hand, but keeping a
second tree API for it would cost more than it removes. To reopen this, the
number to beat is T1 at x1.5 on the 499 KB page with output equal on every
fixture.

## Verdicts

"Deletable" is an estimate of lines a package would remove, not the domain's
size.

| domain (lines by hand) | candidates checked | deletable | verdict |
|---|---|---|---|
| robots.txt (116) | **protego** | about 116 | **adopted**, see below |
| HTML parse (101) | beautifulsoup4, soupsieve, selectolax, parsel | 0 | reject: bs4 x14 to x26 slower and stalls the loop; selectolax is no faster and has no XPath (17 `xpath` references); parsel wraps lxml |
| HTML to text (65) | html2text, inscriptis, trafilatura, markdownify | 0 | reject: html2text is GPL-3; inscriptis is x5 to x6 slower and differs on 19 of 27 fixtures (it adds bullets); trafilatura adds 11 packages |
| Anthropic client (207) | anthropic | about 25 | reject: `import anthropic` costs 972 ms (aiohttp 226, pydantic 51), 8 new packages besides itself, 18 MB for the SDK alone, to replace a 20-line retry ladder; it would bypass `net.http.send`, the choke point robots, logging and the `serve` test fake share |
| JSON-LD (176) | extruct, pyld | about 40 | reject: extruct adds 10 packages (last release 23 months ago); pyld is a JSON-LD processor, not a script-tag extractor |
| path reading (50 of 604) | jmespath, jsonpath-ng, glom | about 50 | reject: `[]` keeps a `None` for a missing item where JMESPath drops it (misaligns parallel lists), x7 to x19 slower per read |
| company names (53) | cleanco, rapidfuzz | 0 | reject: cleanco strips 8 of the 20 suffix words; the other 12 ("Therapeutics", "Labs", ...) are deliberate domain words |
| dates (36) | python-dateutil, dateparser | 0 | reject: the input is Workday's "Posted 30+ Days Ago" and epoch milliseconds; dateparser adds 4 packages |
| bounded fan-out (128) | anyio, aiometer, aiostream | 0 | reject (by design, not measured): the pass budget, abandonment callback and per-key serialisation are this code's own semantics, so an anyio-based version would keep the same wrapper; aiostream is GPL-3 |
| breaker, limiter (55) | tenacity, backoff, aiolimiter, pybreaker, purgatory | 0 | reject (by design, not measured): per-host state lives in `runstate`; nothing to net out at this size |
| migrations (77) | yoyo-migrations, sqlite-utils | 0 | reject (by design, not measured): yoyo adds 4 packages for a 77-line runner whose real content is the SQL files |
| scheduling (65) | apscheduler, croniter | 0 | reject (by design, not measured): the schedule math is small and has a doctest |
| digest render (569) | jinja2 (already shipped), markdown-it-py, mistune | n/a | see the bug below: no new package needed |
| registrable domain (0) | tldextract | 0 | nothing to replace; adopt only if a public-suffix need appears |

## Adopt: protego for robots.txt

`net/robots.py` parses robots.txt and matches rules by hand (`parse_groups`,
`_pattern_to_re`, `_match_group`, `_Group`), and still uses the standard
library's `RobotFileParser` for crawl-delay and sitemaps. `protego` (Scrapy's
parser, BSD-3, pure Python, 0 new packages, released 0.3 months ago) does all
of it.

A differential test over 6,000 random robots.txt files, three user agents
and 18 paths (324,000 decisions):

| files | decisions | disagreements |
|---|---|---|
| well-formed: no repeated agent, no overlapping tokens | 121,554 | **0** |
| a `User-agent` appears in more than one group | 181,170 | 6.8% |
| one agent name is a substring of another (`bot` in `googlebot`) | 21,276 | 10.3% |

So the core rules (longest match, `*`, `$`, empty `Disallow`) agree exactly,
and both disagreements are where the repo's matcher departs from RFC 9309:
it keeps only the first `*` group (§2.2.1 says matching groups are merged),
and it matches agents by substring where the RFC matches the product token.
protego is x3 slower on parse plus 18 decisions (46 against 16 ms per 500
files), which is 0.005 ms a file for something read once per host an hour.

## Adopted

`protego` 0.7.0 replaced the hand-written matcher and `RobotFileParser` in
`net/robots.py`: 169 lines out and 106 in, most of them doctests. Two
conditions were set for it:

- **No further packages.** Its wheel lists no `Requires-Dist`; a clean venv
  that installs it holds `Protego` and nothing else. Pure Python, BSD-3,
  `py.typed`, Python 3.10 and up.
- **Robots.txt stays optional.** The switch is not the parser's: `[policy]
  respect_robots = false` (and `robots_exempt_hosts` per host) sits above
  it. Off, `allowed` is True and no robots.txt is fetched or waited on;
  `TestRespectRobots` runs both settings through `net.http.send` and fails
  when either check in `robots.py` is removed.

A second differential, end to end (`allowed`, `crawl_delay` and `sitemaps`
for 6,000 random files and three user agents, the matcher as it was against
the matcher as it is), found what the swap changes. Every difference traces
to one of these; none was left unexplained:

| what changes | share of the decisions where it applies |
|---|---|
| a user agent named in two groups: the groups now merge (RFC 9309 s2.2.1); before, only the first counted | 5.6% of decisions in those files |
| an agent name must start a word of the user agent: `bot` no longer matches `googlebot` | 5.8% |
| a Crawl-delay line ends its group, so the next `User-agent` starts another; before it joined the last one | 0.56% of decisions in files with a Crawl-delay |
| a `$` counts toward a rule's length: `Disallow: /x$` now beats `Allow: /x` for `/x` | 36 of 89,140 (0.04%) in the rest |
| Crawl-delay: fractional values (`0.5`) are honored (stdlib read only whole numbers); the delay comes from the same group as the rules, not the file's first matching entry | 1,291 of 6,750 comparisons in files with a `0.5`, malformed or negative delay |
| sitemaps | 0 differences |

The first two are the ones already found in the first run. On the well-formed
files the two agreed on every decision. The crawler identifies itself with a
Chrome user agent, so a group naming `chrome` or `mozilla` governs it, as
before.

## Bug found on the way: the digest never escapes anything

`digest/render.py` builds the HTML email by f-string and has no escaping
anywhere (`grep -n escape src/digest/*.py` finds nothing). Ordinary text
from job boards is enough to break it:

    title "R&D Engineer <Senior> - AI/ML", url "...?id=7&src=x'y"
    html: <a href='...?id=7&src=x'y'>R&D Engineer <Senior> - AI/ML</a>
    cell: <td>Acme & Sons <Labs></td><td>5 < 6</td>

The apostrophe ends the `href` early, `<Senior>` becomes a tag and swallows
text in a mail client, and text from third-party pages goes into an HTML
message unescaped. The fix is the standard library's `html.escape` on the
HTML half, with matching escaping on the markdown half, not a package.
Jinja2 is already shipped through Flask and would also do it, at the price
of moving 569 lines of builders into templates.

## Watch list

- **`html5lib`** (already a dependency): last release 75 months ago. It is
  the fallback when lxml fails, and x32 to x37 slower than lxml.
- **`requests`**: kept on purpose as the model layer (redirects, cookies,
  headers) while `aiohttp` does the I/O, so the repo already borrows a
  dependency where it could have hand-rolled. It reaches into
  `requests.sessions.SessionRedirectMixin` and `merge_setting`, which are
  not public API, with an open upper bound in `requirements.txt`.
