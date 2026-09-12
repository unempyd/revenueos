# RevenueOS MCP server

`revenueos-mcp` exposes RevenueOS Community over the [Model Context Protocol](https://modelcontextprotocol.io):
one connected business, run over stdio, for any MCP host — Claude Desktop, Claude Code,
or another agent framework.

## Run it

```bash
uv sync --extra mcp          # installs the mcp SDK alongside revenueos
uv run revenueos-mcp          # starts the server on stdio; point your host at this command
```

The server resolves its workspace exactly like the CLI: `REVENUEOS_ROOT`, or the current
directory walked upward looking for `company-context/` + `skills/`. Set `REVENUEOS_ROOT` (or
launch the server with that directory as `cwd`) to point it at an onboarded workspace —
create one with `revenueos workspace new <dir>` and `revenueos --root <dir> init` first.

Everything else the workers need (an LLM credential, SMTP/IMAP, ad exports) is read from
that workspace's `revenueos.yaml` and environment exactly as documented in the repository
`CLAUDE.md`; `REVENUEOS_DRY_RUN=1` is the safe default for outreach in Community.

## Claude Desktop configuration

Add this to Claude Desktop's `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "revenueos": {
      "command": "uv",
      "args": ["run", "revenueos-mcp"],
      "cwd": "/absolute/path/to/RevenueOS",
      "env": {
        "REVENUEOS_ROOT": "/absolute/path/to/your-workspace"
      }
    }
  }
}
```

Omit `REVENUEOS_ROOT` to use the RevenueOS repository itself as the workspace (fine for
trying it out); point it at a separate `revenueos workspace new` directory for a real
customer install.

## Claude Code

Installing the `revenueos` plugin (see `.claude-plugin/`) registers this same server
automatically:

```
/plugin marketplace add <this-repo-url>
/plugin install revenueos
```

Or add it directly with the CLI: `claude mcp add revenueos -- uv run revenueos-mcp`.

## Tools

| Tool | Capability | Does |
|---|---|---|
| `revenueos_today` | Revenue Monitor | The brief: pending actions, pipeline value, latest metrics |
| `revenueos_results` | Revenue Monitor | Executed actions and their measured outcomes |
| `revenueos_run(worker)` | any | Run one worker (`discover`, `outreach`, `inbox`, `seo`, `ads-audit`, `content`, `monitor`, `measure`, or `all`) |
| `revenueos_seo_audit(website?)` | SEO Auditor | Crawl the site, compare authority, queue fixes |
| `revenueos_ads_audit(csv_path, platform)` | Ads Auditor | Import an ad export, flag waste/pacing/concentration |
| `revenueos_discover_leads(csv_path?)` | Lead Discovery | Import leads / pull from an optional external prospecting service (see docs/integrations.md) |
| `revenueos_draft_followups` | Sales Follow-up | Draft first-touch cold emails (never sends) |
| `revenueos_approve(action_id)` | any | Approve a pending action |
| `revenueos_execute(action_id)` | any | Execute an approved action (the only tool that sends/publishes) |
| `revenueos_ignore(action_id)` | any | Dismiss a pending action |
| `revenueos_measure` | Revenue Monitor | Record measurable outcomes for executed actions |
| `revenueos_search_skills(query, limit?)` | Marketing Intelligence | Search the unified skill catalogue |

## Resources

| URI | Content |
|---|---|
| `revenueos://today` | TODAY, rendered as text |
| `revenueos://results` | RESULTS, rendered as text |

## Registry files

`mcp/server.json` is the [MCP registry](https://github.com/modelcontextprotocol/registry)
manifest for `io.github.unempyd/revenueos`; `mcp/smithery.yaml` is the equivalent manifest
for listing on [Smithery](https://smithery.ai). Both describe the same `revenueos-mcp`
console script installed by `pip install revenueos[mcp]` / `uv sync --extra mcp`.
