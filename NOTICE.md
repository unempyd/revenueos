# Third-party notice

RevenueOS includes software from the following projects. `VENDOR.json` records, for every
vendored file, the upstream repository, the exact commit, and the licence; the licence
texts themselves are in `THIRD_PARTY_LICENSES/`. Nothing is vendored unless its licence is
MIT or Apache-2.0 — `scripts/vendor.py` refuses to copy anything else.

## Vendored (MIT / Apache-2.0)

| Upstream project | Licence | Used as |
|---|---|---|
| claude-ads (AgriciDaniel) | MIT | The ads-audit engine: adapters, scoring, and reporting for ad-platform exports, plus 34 ads skills and 25 audit agent definitions |
| kairos (kevinbadi) | MIT | The orchestrator: its cron parser, run journal, activity log, and status API (the always-on worker loop is a port of it; the process runner is new) |
| pulse-cmo (aruntemme) | MIT | Site crawling, Hacker News discovery, the relevance gate, and the tool registry those run on |
| ai-sales-agent (Meshpilot-AGI) | MIT | The lead / draft / send / unsubscribe data model (translated to SQLite), the recipe model for outreach hooks, and the CASL-compliant renderer |
| markster-os (markster-public) | MIT | The company-context canon schema and its validator, the learning-loop correction format, methodology, and playbooks, plus 34 skills |
| seo (marketingskills) | MIT | The Authority Mark domain-comparison script and project-probe scripts, plus 23 SEO skills |
| ai-cmo-operator (guerrilla2799) | MIT | The CORRECTIONS.md standing-memory format, plus 11 operator skills |
| talon (mailgun) | Apache-2.0 | Reply-quotation stripping used by the inbox worker |
| Marketing-Agent-OS (iamwaqargulzar; bundles four MIT/Apache upstreams — see its own notice) | Apache-2.0 | 236 normalised skills and their catalog |
| openclaudia-skills (OpenClaudia), marketing-skills (scayver), marketingskills (coreyhaines31), marketing-skills (kostja94), ai-marketing-skills (ericosiu), saas-marketing-agents (shalintripathi), growth-marketing-os (growthack88), office-skills (claude-office-skills) | MIT | Skill and agent libraries, the zero-dependency connector CLIs, and integration guides |

Modifications to vendored files are limited to the `PATCHES` list in `scripts/vendor.py`
(import relocation, workspace-path fixes, and removal of an upstream author's hardcoded
sign-off and demo URLs from the outreach renderer) and the removal of one upstream
project's phone-home telemetry preamble from 22 skill files (recorded in `VENDOR.json`
under `telemetry_stripped_from`).

## Deliberately not vendored

| Project | Reason | How RevenueOS relates to it |
|---|---|---|
| OpenOutreach (eracle) | GPL-3.0-or-later — copyleft, not compatible with vendoring into this codebase | Runs as an independent, separately-licensed process or container; RevenueOS's discover worker reads only its JSON output on stdout, across a process boundary |
| ads-skills (adkit) | Proprietary licence forbids bundling into other skill libraries | Not used |
| OpenCRM (arkitekt-ai) | No LICENSE file in the repository (its README claims MIT, but that isn't a licence grant) | Reference only |
| ai-os-skills (kevinbadi) | No LICENSE file | Reference only |
| marketing-skills (erron-ai) | No LICENSE file | Its MIT-licensed superset already comes in via the coreyhaines31 / scayver sources above |
| business-skills (astDeniss) | No LICENSE file | Reference only |

If any of the unlicensed projects above publish a licence, they can be added the same way
every other source was: a row in `upstream/MANIFEST.tsv` and a mapping in
`scripts/vendor.py`'s `VENDOR_MAP` — nothing else changes.

## How provenance is recorded

Every vendored file's origin is tracked at three levels, so a licence question can always
be answered exactly: `VENDOR.json` maps each destination path to its source repository,
commit, and licence; `upstream/MANIFEST.tsv` lists every upstream clone with its commit
hash, the date it was pinned, and its verified licence; and `THIRD_PARTY_LICENSES/`
carries the full licence text for every vendored source, one file per project.

## Source repositories

Exact upstream repositories and pinned commits for every included component (from `VENDOR.json`):

- `AgriciDaniel/claude-ads` — MIT — commit `ac2164493391`
- `aruntemme/pulse-cmo` — MIT — commit `04626391e581`
- `Meshpilot-AGI/ai-sales-agent` — MIT — commit `e87c79471cdc`
- `markster-public/markster-os` — MIT — commit `b1da50054715`
- `marketingskills/seo` — MIT — commit `b25b48c2d300`
- `kevinbadi/kairos` — MIT — commit `d1fd28850236`
- `guerrilla2799/ai-cmo-operator` — MIT — commit `6ce2d4e68e3f`
- `iamwaqargulzar/Marketing-Agent-OS` — Apache — commit `adb39acf84ce`
- `OpenClaudia/openclaudia-skills` — MIT — commit `10f3fd809031`
- `scayver/marketing-skills` — MIT — commit `18243d7675fa`
- `coreyhaines31/marketingskills` — MIT — commit `5b2c0007766c`
- `kostja94/marketing-skills` — MIT — commit `70987bad4ebe`
- `ericosiu/ai-marketing-skills` — MIT — commit `09694f033dea`
- `shalintripathi/saas-marketing-agents` — MIT — commit `95440b5757af`
- `growthack88/growth-marketing-os` — MIT — commit `db41a8e2e9fa`
- `claude-office-skills/skills` — MIT — commit `9c4c7d5cd281`
- `mailgun/talon` — Apache — commit `ee497a79c648`
