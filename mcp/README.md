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

Every tool carries a `title` and MCP tool annotations; the hints below are what
`tools/list` returns. `R` = `readOnlyHint`, `D` = `destructiveHint`, `I` = `idempotentHint`,
`O` = `openWorldHint`.

| Tool | Capability | Does | Hints |
|---|---|---|---|
| `revenueos_today` | Revenue Monitor | The brief: pending actions, pipeline value, latest metrics | R·I |
| `revenueos_results` | Revenue Monitor | Executed actions and their measured outcomes | R·I |
| `revenueos_run(worker)` | any | Run one worker (`discover`, `outreach`, `inbox`, `seo`, `ads-audit`, `content`, `monitor`, `measure`, or `all`) | I·O |
| `revenueos_seo_audit(website?)` | SEO Auditor | Crawl the site, compare authority, queue fixes | I·O |
| `revenueos_ads_audit(csv_path, platform)` | Ads Auditor | Import an ad export, flag waste/pacing/concentration | I·O |
| `revenueos_discover_leads(csv_path?)` | Lead Discovery | Import leads / pull from an optional external prospecting service (see docs/integrations.md) | I·O |
| `revenueos_draft_followups` | Sales Follow-up | Draft first-touch cold emails (never sends) | I·O |
| `revenueos_approve(action_id)` | any | Approve a pending action | I |
| `revenueos_execute(action_id)` | any | Execute an approved action (the only tool that sends/publishes) | **D·O** |
| `revenueos_ignore(action_id)` | any | Dismiss a pending action | I |
| `revenueos_measure` | Revenue Monitor | Record measurable outcomes for executed actions | I·O |
| `revenueos_search_skills(query, limit?)` | Marketing Intelligence | Search the unified skill catalogue | R·I |
| `revenueos_lessons(days?)` | any | Dated lessons drawn from this workspace's own measured results | R·I |

## Resources

| URI | Content |
|---|---|
| `revenueos://today` | TODAY, rendered as text |
| `revenueos://results` | RESULTS, rendered as text |

## The `.mcpb` bundle

An [MCP Bundle](https://github.com/modelcontextprotocol/mcpb) (`.mcpb`) is a zip archive
holding a `manifest.json` that describes a local MCP server and how to run it. Claude
Desktop installs one on a double-click; it is also the artefact Smithery distributes for
stdio servers. `mcp/bundle/` holds the source, `scripts/build_mcpb.py` builds it.

```bash
python3 scripts/build_mcpb.py              # stage → mcpb validate → mcpb pack → dist/revenueos-<version>.mcpb
python3 scripts/build_mcpb.py --stage-only # write build/mcpb/ and stop, to inspect the manifest
```

The script reads the version from `pyproject.toml` and the tool list out of
`src/revenueos/mcp_server.py`, refuses to build against a `revenueos` version PyPI does not
have, normalises zip timestamps so a rebuild is byte-identical, and audits the finished
archive — it aborts if anything matching `data/`, `.venv/`, `company-context/`, a `.db`, a
key or a `.env` got in. The result is five files and ~90 KB.

### What it asks of the user

The bundle carries no RevenueOS code. It declares `server.type: "uv"` (MCPB manifest
version `0.4`), so at install time the host downloads uv, uv fetches a suitable Python, and
`revenueos[mcp]` is installed from PyPI into a venv beside the extension. That is the route
the specification prescribes for Python servers: bundling wheels instead is
[documented as unworkable](https://github.com/modelcontextprotocol/mcpb/blob/main/README.md)
— *"Limitation: Cannot portably bundle compiled dependencies (e.g., pydantic, which the MCP
Python SDK requires)"* — and RevenueOS needs pydantic, lxml, cryptography and selectolax.
So the extension is **not self-contained**: it needs network access on first run and takes a
few minutes to install. It does not need the user to have Python or uv already.

Three settings appear in Claude Desktop's extension UI, all optional:

| Setting | Maps to | If left blank |
|---|---|---|
| Workspace Directory | `REVENUEOS_ROOT` | RevenueOS creates `~/.revenueos` from its bundled template — un-onboarded, so the tools start and report nothing until `revenueos init` runs. `server/main.py` says so on stderr rather than failing obscurely |
| Anthropic API Key | `ANTHROPIC_API_KEY` | Deterministic workers still run; model-backed steps say they are unavailable instead of guessing |
| Dry run outreach | `REVENUEOS_DRY_RUN=1` | Defaults to on: approved outreach is written to `data/outputs/` instead of sent |

`server/main.py` exists to make that honest. MCPB substitutes `${user_config.KEY}` into the
environment, and a key the user never filled in can arrive as an empty string or as the
literal placeholder — which `revenueos.paths.find_root` would otherwise treat as a real
workspace path. The shim drops those, converts the MCPB boolean `"true"` into the `"1"` that
RevenueOS actually reads, and then calls `revenueos.mcp_server.main()`.

### Where it can go, and what is still missing

Both directories that take a local stdio server want this same file:

- **Anthropic's desktop-extension directory** — a
  [separate submission form](https://clau.de/desktop-extention-submission) from the
  Connectors Directory portal, which is remote-HTTPS-only.
- **Smithery** — `smithery mcp publish ./server.mcpb -n <org>/<server>`, per
  [smithery.ai/docs/build/publish](https://smithery.ai/docs/build/publish). There is no
  `smithery.yaml`; a config file in the repository is not what Smithery reads.

Neither has been submitted. Before either can be:

1. **Publish the matching `revenueos` release to PyPI.** The bundle pins the exact version
   it installs, so a bundle for a version PyPI does not have cannot install. The build
   script refuses to produce one without `--allow-unpublished`.
2. ~~**Tool annotations.**~~ **Done, 2026-09-14.** All 13 tools carry a `title` and a full
   `ToolAnnotations` set, via the `title=` / `annotations=` parameters of `@mcp.tool()` (mcp
   Python SDK 1.30.0; `FastMCP.list_tools` copies both onto the wire `Tool`). Verified over a
   real stdio session: `tools/list` returns `readOnlyHint`, `destructiveHint`,
   `idempotentHint` and `openWorldHint` on every tool. `revenueos_execute` is the only
   `destructiveHint: true` and the only `idempotentHint: false`; `revenueos_today`,
   `revenueos_results`, `revenueos_search_skills` and `revenueos_lessons` are the only
   `readOnlyHint: true` ones. The reasoning is a comment above `_boot()` in `mcp_server.py`.
3. ~~**A privacy policy.**~~ **Written and published, 2026-09-14** — at
   <https://unempyd.github.io/revenueos/privacy.html> (in `website/privacy.html`, in the
   sitemap and linked from every page footer) and as the `## Privacy` section of the
   repository `README.md`. **One step remains and it belongs to `scripts/build_mcpb.py`:**
   `build_manifest()` does not emit a `privacy_policies` key, so the field is still absent
   from the bundle. Add to the returned dict, next to `"homepage"`:

   ```python
   "privacy_policies": ["https://unempyd.github.io/revenueos/privacy.html"],
   ```

   Worth knowing while you are in there: `read_tools()` parses only the function name and
   docstring out of each `@mcp.tool()`, so the manifest's `tools` array still carries no
   `title` or annotations even though `tools/list` now does. MCPB 0.4 treats that array as
   display metadata and `tools_generated` is `false`, so it is not a blocker — but if the
   reviewer reads the manifest rather than starting the server, the annotations are invisible
   to them.
4. **Windows testing.** The manifest declares `darwin`, `win32` and `linux`; only macOS has
   been exercised. Anthropic asks for both macOS and Windows.

The bundle is unsigned. `mcpb sign --self-signed` succeeds and appends ~2 KB, but `mcpb
verify` and `mcpb info` on the result both report "Extension is not signed" (CLI 2.1.2), and
a self-signed certificate confers no trust anyway. Neither directory requires a signature.

## Registry files

`mcp/server.json` is the [MCP registry](https://github.com/modelcontextprotocol/registry)
manifest for `io.github.unempyd/revenueos`, describing the `revenueos-mcp` console script
installed by `pip install revenueos[mcp]` / `uv sync --extra mcp`.
