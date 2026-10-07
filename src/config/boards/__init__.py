"""Every per-platform fact about an ATS board, as data.

`BOARDS[ats]` says how a store row names a board, how its listing is read
and mapped to rows, how one posting is read back, and how a posting's
closure is judged. The engine that reads it is `src.ats.board` (its
default the models in `src.ats.board.spec`, where each key's meaning and
default are declared); outside them, no module in `src/` names a platform
(tests/test_boards_spec.py).

The literal is JSON-compatible on purpose (str, int, float, bool, None,
list, dict; regexes as strings): tests/test_invariants.py pins
`json.loads(json.dumps(BOARDS)) == BOARDS`, so moving it to a JSON file
later is mechanical.

The host lists below it are derived from the specs' `detect` entries, so
the layers under the engine (the store, the careers-page reader, the
closure prober, page capture) know every vendor host without naming one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.rows import JSON, dig
from src.rows import HandleColumn

from .greenhouse import SPEC as GREENHOUSE
from .lever import SPEC as LEVER
from .ashby import SPEC as ASHBY
from .bamboohr import SPEC as BAMBOOHR
from .rippling import SPEC as RIPPLING
from .hibob import SPEC as HIBOB
from .workable import SPEC as WORKABLE
from .paylocity import SPEC as PAYLOCITY
from .ultipro import SPEC as ULTIPRO
from .adp import SPEC as ADP
from .smartrecruiters import SPEC as SMARTRECRUITERS
from .workday import SPEC as WORKDAY
from .infor import SPEC as INFOR
from .jazzhr import SPEC as JAZZHR
from .jobvite import SPEC as JOBVITE
from .kula import SPEC as KULA
from .successfactors import SPEC as SUCCESSFACTORS
from .jibe import SPEC as JIBE
from .icims import SPEC as ICIMS
from .peopleadmin import SPEC as PEOPLEADMIN
from .custom import SPEC as CUSTOM
from .wpjson import SPEC as WPJSON
from .breezy import SPEC as BREEZY
from .recruitee import SPEC as RECRUITEE
from .pinpoint import SPEC as PINPOINT
from .phenom import SPEC as PHENOM
from .oracle import SPEC as ORACLE
from .eightfold import SPEC as EIGHTFOLD
from .dayforce import SPEC as DAYFORCE
from .cornerstone import SPEC as CORNERSTONE
from .recruiterbox import SPEC as RECRUITERBOX
from .teamtailor import SPEC as TEAMTAILOR
from .gem import SPEC as GEM
from .taleo import SPEC as TALEO
from .taleo_enterprise import SPEC as TALEO_ENTERPRISE
from .avature import SPEC as AVATURE
from .ukg import SPEC as UKG
from .paycom import SPEC as PAYCOM
from .gohire import SPEC as GOHIRE
from .polymer import SPEC as POLYMER
from .gusto import SPEC as GUSTO
from .amazon import SPEC as AMAZON
from .joincom import SPEC as JOINCOM
from .personio import SPEC as PERSONIO
from .manatal import SPEC as MANATAL

BOARDS: dict[str, dict[str, JSON]] = {
    "greenhouse": GREENHOUSE,
    "lever": LEVER,
    "ashby": ASHBY,
    "bamboohr": BAMBOOHR,
    "rippling": RIPPLING,
    "hibob": HIBOB,
    "workable": WORKABLE,
    "paylocity": PAYLOCITY,
    "ultipro": ULTIPRO,
    "adp": ADP,
    "smartrecruiters": SMARTRECRUITERS,
    "workday": WORKDAY,
    "infor": INFOR,
    "jazzhr": JAZZHR,
    "jobvite": JOBVITE,
    "kula": KULA,
    "successfactors": SUCCESSFACTORS,
    "jibe": JIBE,
    "icims": ICIMS,
    "peopleadmin": PEOPLEADMIN,
    "custom": CUSTOM,
    "wpjson": WPJSON,
    "breezy": BREEZY,
    "recruitee": RECRUITEE,
    "pinpoint": PINPOINT,
    "phenom": PHENOM,
    "oracle": ORACLE,
    "eightfold": EIGHTFOLD,
    "dayforce": DAYFORCE,
    "cornerstone": CORNERSTONE,
    "recruiterbox": RECRUITERBOX,
    "teamtailor": TEAMTAILOR,
    "gem": GEM,
    "taleo": TALEO,
    "taleo_enterprise": TALEO_ENTERPRISE,
    "avature": AVATURE,
    "ukg": UKG,
    "paycom": PAYCOM,
    "gohire": GOHIRE,
    "polymer": POLYMER,
    "gusto": GUSTO,
    "amazon": AMAZON,
    "joincom": JOINCOM,
    "personio": PERSONIO,
    "manatal": MANATAL,
}

#: The spec that reads a careers page itself, no ATS signature on it: the
#: sniffer's last resort, a board named by its URL alone.
CAREERS_PAGE_ATS = "custom"

#: The store columns naming a board whose spec sets no `handle.columns`:
#: the default of src.ats.board.spec.Handle, and the store's.
DEFAULT_HANDLE_COLUMNS: tuple[HandleColumn, ...] = ("slug",)

#: Job aggregators: hosts listing other employers' postings. A careers page
#: never names one as its own board, and they bot-gate anonymous reads, so
#: a probe there says nothing.
AGGREGATOR_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.", "ziprecruiter.com",
                    "simplyhired.com", "monster.com", "dice.com", "builtin.com")


def _hosts(fetchable: bool) -> tuple[str, ...]:
    """The vendor hosts the specs' `detect` entries claim, in spec order;
    only a fetchable spec's (one with a `listing`) when `fetchable`."""
    hosts: list[JSON] = []
    for s in BOARDS.values():
        dets = s.get("detect")
        if (s.get("listing") or not fetchable) and isinstance(dets, list):
            hosts += [dig(d, "host") for d in dets]
    return tuple(dict.fromkeys(h for h in hosts if isinstance(h, str)))


#: The vendor hosts of the platforms the engine fetches.
FETCHABLE_HOSTS = _hosts(fetchable=True)
#: Hosts shared by many employers: a page there names a board or a listing,
#: never the company that owns it, so it is no company's own careers page
#: or website: the aggregators, every ATS vendor host (fetchable or lead),
#: and Google's, which serve its search and many employers' Sites pages.
SHARED_HOSTS = AGGREGATOR_HOSTS + _hosts(fetchable=False) + ("google.com",)


def hosts_re(hosts: Iterable[str]) -> re.Pattern[str]:
    """A case-blind regex finding any of `hosts` (literal fragments) in a
    URL or host."""
    return re.compile("|".join(map(re.escape, hosts)), re.I)
