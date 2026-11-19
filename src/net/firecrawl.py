"""Firecrawl's search API: the web search behind DuckDuckGo (FIRECRAWL_API_KEY).

`ddg.search` asks it what DuckDuckGo missed. A search is billed in credits
(2 per 10 results), so a run spends at most [discovery].firecrawl_credits,
and a key refused as bad or out of credit is not asked again that run.
The run's spend is printed as it ends.

Notes:
    DuckDuckGo throttles discovery's searches: on 2026-10-05 the search
    library got a 403, 429, 202 or timeout from every engine on almost
    every query, and resolved 6 of 15 names in 108 s.
"""

from __future__ import annotations

import logging
import math

from pydantic import BaseModel, Field, ValidationError

from src import config, runstate
from src.net import http

_log = logging.getLogger(__name__)


class WebResult(BaseModel):
    """One web result of a search."""
    url: str
    title: str = ""
    description: str = ""


class _Data(BaseModel):
    web: list[WebResult] = []


class _Reply(BaseModel):
    data: _Data = Field(default_factory=_Data)
    credits_used: int | None = Field(None, alias="creditsUsed")


class _Spend:
    """What a run's searches have cost, and why it stopped asking."""

    def __init__(self) -> None:
        self.searches = 0
        self.credits = 0
        self.stopped = ""        # why the run asks no more; "" while it may


_SPEND = runstate.per_run(_Spend)


def cost(limit: int) -> int:
    """The credits a search for `limit` results is billed.

    >>> cost(1), cost(10), cost(11), cost(25)
    (2, 2, 4, 6)
    """
    return 2 * math.ceil(limit / 10)


def _report(spend: _Spend) -> None:
    print(f"  [firecrawl] {spend.searches} search(es), {spend.credits} credit(s) this run"
          + (f"; stopped: {spend.stopped}" if spend.stopped else ""))


def _stop(spend: _Spend, why: str) -> None:
    if not spend.stopped:
        spend.stopped = why
        print(f"  [!] firecrawl: {why}; DuckDuckGo alone for the rest of this run")


def usable(limit: int = 10) -> bool:
    """Whether a search for `limit` results may be asked now: a key is set,
    none was refused, and it fits the run's credits."""
    spend = _SPEND()
    return bool(config.FIRECRAWL_API_KEY and not spend.stopped
                and spend.credits + cost(limit) <= config.DISCOVERY_FIRECRAWL_CREDITS)


async def search(query: str, limit: int = 10) -> list[WebResult] | None:
    """Firecrawl's web results for `query`, at most `limit` (100 at most);
    None when it was not asked (`usable`) or did not answer.

    One search at a time per config.FIRECRAWL_GAP_S. A 401, 402 or 403
    stops the run's searches; a timeout or 5xx skips this one.
    """
    limit, spend, cap = min(limit, 100), _SPEND(), config.DISCOVERY_FIRECRAWL_CREDITS
    if not usable(limit):
        if config.FIRECRAWL_API_KEY and cap and not spend.stopped:
            _stop(spend, f"this run's {cap} credits are spent")
        return None
    url = f"{config.FIRECRAWL_API}/search"
    await http.LIMITER.wait(url, config.FIRECRAWL_GAP_S)
    status, data, err = await http.request_json(
        "POST", url, json={"query": query, "limit": limit, "timeout": 20000},
        headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"}, timeout=30)
    if status in (401, 402, 403):
        _stop(spend, {401: "the key was refused", 402: "out of credits"}.get(
            status, f"HTTP {status}"))
        return None
    if err:
        _log.debug("firecrawl search failed (%s): %s", err, query)
        return None
    try:
        reply = _Reply.model_validate(data)
    except ValidationError as e:
        _log.debug("firecrawl reply unreadable: %s", e)
        return None
    if not spend.searches:
        runstate.at_exit(lambda: _report(spend))
    spend.searches += 1
    spend.credits += reply.credits_used if reply.credits_used is not None else cost(limit)
    return reply.data.web
