# Board health

_6/6 platforms healthy · checked 2026-08-22 07:03 UTC · [how this works](tools/check_boards.py)_

One request per platform against a public sample board, with the keyword filter widened so the number reflects the BOARD rather than any particular search profile.

| | Platform | Sample board | Postings | Detail |
|---|---|---|---:|---|
| ✅ | `ashby` | Vanta | 103 | ok |
| ✅ | `bamboohr` | EMS Biomedical | 100 | ok |
| ✅ | `greenhouse` | Databricks | 818 | ok |
| ✅ | `kula` | Precision Neuroscience | 13 | ok |
| ✅ | `lever` | Veeva | 872 | ok |
| ✅ | `rippling` | Blackrock Neurotech | 3 | ok |

**✅ ok** — endpoint alive, response parsed, postings returned.  
**⚠️ degraded** — reachable and parsed, but fewer postings than expected: an empty board, or a silent shape change worth checking.  
**🚧 blocked** — rate-limited or challenged (commonly a CI runner's IP). Says nothing about the parser.  
**❌ broken** — 4xx/5xx or an exception: a real failure.
