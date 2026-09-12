# RevenueOS Community capabilities

Six thin, RevenueOS-branded wrappers over the workers in `src/revenueos/workers/` — the
Community tier of the RevenueOS product. Each is an [Agent Skill](https://code.claude.com/docs/en/skills)
(a `SKILL.md`) that tells an agent exactly which `revenueos` command or MCP tool to call, what
input it needs, what it produces, and — for every capability that can act on the world — the
rule that nothing sends, publishes or spends until a human has approved it.

| Capability | Slug | Worker(s) |
|---|---|---|
| RevenueOS SEO Auditor | `seo-auditor` | `seo` (+ `measure`) |
| RevenueOS Ads Auditor | `ads-auditor` | `ads-audit` |
| RevenueOS Lead Discovery | `lead-discovery` | `discover` |
| RevenueOS Sales Follow-up | `sales-follow-up` | `outreach` + `inbox` |
| RevenueOS Marketing Intelligence | `marketing-intelligence` | `content` + `skills search` |
| RevenueOS Revenue Monitor | `revenue-monitor` | `monitor` + `today` / `results` |

All six operate on one connected RevenueOS workspace — a directory with `company-context/`
and `skills/` in it, created and onboarded with `revenueos workspace new` + `revenueos init`
(see the repository `README.md` and `CLAUDE.md`). Point `REVENUEOS_ROOT`, or your working
directory, at that workspace before invoking any capability.

**The approval rule is the same everywhere:** workers only read, analyse and queue
`actions` — they never send an email, publish content, spend ad budget, or change a live
campaign. A human decides with `revenueos approve <id>` then `revenueos execute <id>` (or the
matching MCP tools `revenueos_approve` / `revenueos_execute`) before anything happens in the
world, and even execution writes deliverables to `data/outputs/` rather than auto-publishing.

## Install

There are three ways to bring these capabilities into an agent session; all three point at
the same underlying `revenueos` CLI / MCP server, so pick whichever fits the host you use.

### 1. As Agent Skills (`npx skills add`)

```bash
npx skills add <this-repo-url> --skill seo-auditor
npx skills add <this-repo-url> --skill ads-auditor
npx skills add <this-repo-url> --skill lead-discovery
npx skills add <this-repo-url> --skill sales-follow-up
npx skills add <this-repo-url> --skill marketing-intelligence
npx skills add <this-repo-url> --skill revenue-monitor
```

Each installs the corresponding `capabilities/<slug>/SKILL.md` into your agent's skill
directory. The skill only tells the agent which commands to run — you still need the
`revenueos` CLI installed and a workspace onboarded (`uv sync && revenueos init`).

### 2. As a Claude Code plugin

Add this repository as a plugin marketplace and install `revenueos`:

```
/plugin marketplace add <this-repo-url>
/plugin install revenueos
```

This installs all six skills (from `.claude-plugin/plugin.json`'s `skills: ./capabilities`)
and registers the `revenueos-mcp` MCP server in the same step — see
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.

### 3. As a standalone MCP server

Any MCP host (Claude Desktop, Claude Code, another agent framework) can talk to RevenueOS
directly over `revenueos-mcp` without installing the skills at all — the tool descriptions
carry the same guidance the skills do. See `mcp/README.md` for the exact config.

```bash
uv sync --extra mcp
uv run revenueos-mcp
```
