# Installing the RevenueOS MCP server

For an agent (Cline, Claude Desktop, or another MCP host) setting this server up
automatically. `mcp/README.md` has the full reference; this file is the minimum needed
to get `revenueos-mcp` running with no local clone.

## 1. Run it

```bash
uvx --from "revenueos[mcp]" revenueos-mcp
```

`uvx` (ships with [uv](https://docs.astral.sh/uv/)) fetches `revenueos` from PyPI into an
isolated environment and starts the server over stdio. Verified from a clean directory
with no cache and no `ANTHROPIC_API_KEY` set: it downloads, installs its dependencies,
and stays running, waiting on stdin, exactly as an MCP host expects.

No repository clone, no `uv sync`, no build step. If `uv` is not installed:
`pip install "revenueos[mcp]"` then run `revenueos-mcp` the same way.

## 2. Point it at a workspace

With no configuration, the server manages the RevenueOS repository's own directory —
fine for trying the tools out, not for a real business. To connect it to a real
workspace, set one environment variable:

```json
{
  "command": "uvx",
  "args": ["--from", "revenueos[mcp]", "revenueos-mcp"],
  "env": {
    "REVENUEOS_ROOT": "/absolute/path/to/a/revenueos/workspace"
  }
}
```

Create that workspace first, outside this install, with:

```bash
uvx revenueos workspace new /absolute/path/to/a/revenueos/workspace
uvx revenueos --root /absolute/path/to/a/revenueos/workspace init
```

`init` is interactive without `--answers <file>`; both commands verified from a clean
environment on 2026-09-14.

## 3. What the tools need to do anything

The tools that read a connected account (Stripe, Google, Meta) or send anything need
that account connected first (`revenueos connect <provider>`, run outside the MCP host)
and, for content or ads work, a model credential (`ANTHROPIC_API_KEY`) in the same
environment the server runs in. Without either, the read-only and deterministic tools
(the crawl, ad-export checks, lead qualification) still work; the rest say so instead of
failing silently. See `docs/security-and-approval.md` for what each tool can and cannot
do without your approval.

## What this file does not claim

These instructions were verified by running the exact commands above from a clean
directory with an empty cache. They were **not** verified inside Cline itself — this
project has no way to drive Cline's own agent loop in an unattended environment, so
whether Cline specifically can complete this setup unattended, end to end, from this
file alone, is untested. Say so rather than assume it.
