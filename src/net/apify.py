"""Apify's API: run one actor and read what it found (APIFY_API_KEY).

An actor is a scraper published on Apify; each run is billed to the key's
account (a start fee, its results, compute), so every run names the most
it may charge, and one still running at its budget is aborted.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from pydantic import BaseModel, Field, ValidationError

from src import config
from src.net import http
from src.net.util import JSON

# A run in any other status is still going.
_ENDED = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
# Seconds Apify holds a run request open waiting for the run to end (its maximum).
_WAIT_S = 60


class _Run(BaseModel):
    id: str
    status: str
    dataset: str = Field("", alias="defaultDatasetId")
    message: str | None = Field(None, alias="statusMessage")
    usd: float | None = Field(None, alias="usageTotalUsd")


class _RunReply(BaseModel):
    data: _Run


async def _call(method: str, path: str, *, body: JSON = None,
                params: Mapping[str, str | int | float] | None = None) -> tuple[int | None, JSON]:
    """(status, payload) of one API call; the payload is None on any error."""
    status, data, err = await http.request_json(
        method, f"{config.APIFY_API}/{path}", json=body, params=params,
        headers={"Authorization": f"Bearer {config.APIFY_API_KEY}"}, timeout=_WAIT_S + 30)
    return status, None if err else data


def _run(data: JSON) -> _Run | None:
    try:
        return _RunReply.model_validate(data).data
    except ValidationError:
        return None


async def run_actor(actor: str, run_input: Mapping[str, JSON], *, max_charge_usd: float,
                    budget_s: float = 900) -> list[Mapping[str, JSON]] | None:
    """The dataset items of one run of `actor` ("user/name") on `run_input`,
    or None, said in one line: no key, a refused start (a bad key, no
    credit), a run that did not succeed, one still running after
    `budget_s` (aborted), or results that could not be read. The run may
    charge at most `max_charge_usd`; what it cost is printed.
    """
    if not config.require_creds("Apify", "https://console.apify.com/sign-up",
                                APIFY_API_KEY=config.APIFY_API_KEY):
        return None
    deadline = time.monotonic() + budget_s
    status, data = await _call("POST", f"acts/{actor.replace('/', '~')}/runs", body=run_input,
                               params={"waitForFinish": _WAIT_S, "maxTotalChargeUsd": max_charge_usd})
    run = _run(data)
    if run is None:
        print(f"  [!] {actor}: the run did not start (HTTP {status})")
        return None
    while run.status not in _ENDED and time.monotonic() < deadline:
        _status, data = await _call("GET", f"actor-runs/{run.id}", params={"waitForFinish": _WAIT_S})
        run = _run(data) or run
    if run.status not in _ENDED:
        await _call("POST", f"actor-runs/{run.id}/abort")
        print(f"  [!] {actor}: still running after {budget_s:.0f}s; aborted")
        return None
    cost = f", ${run.usd:.3f}" if run.usd is not None else ""
    if run.status != "SUCCEEDED":
        print(f"  [!] {actor}: run {run.status}{cost}: {run.message or ''}")
        return None
    _status, items = await _call("GET", f"datasets/{run.dataset}/items",
                                 params={"clean": "true", "format": "json"})
    if not isinstance(items, list):
        print(f"  [!] {actor}: the run's results could not be read{cost}")
        return None
    rows = [i for i in items if isinstance(i, Mapping)]
    print(f"  {actor}: {len(rows)} item(s){cost}")
    return rows
