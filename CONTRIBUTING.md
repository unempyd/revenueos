# Contributing to RevenueOS

Thanks for looking at RevenueOS's code. This document covers how to set up a development
environment, the one rule that keeps vendored code maintainable, and what a change needs
before it can be merged.

## Development setup

```bash
scripts/fetch-upstream.sh                 # fetch the pinned upstream sources RevenueOS is assembled from
python3 scripts/vendor.py                 # assemble the product tree from them (idempotent)
uv sync --extra dev                       # Python 3.12+ environment with the `revenueos` CLI
uv run pytest -q                          # the Python test suite
uv run ruff check src tests               # lint (vendored code is excluded)
cd orchestrator && npm install && npm test && npm run typecheck   # orchestrator: vitest + tsc
```

Useful day to day:

```bash
uv run revenueos workspace new <dir>      # a throwaway workspace for manual testing
uv run revenueos init --answers a.json    # onboard it non-interactively
uv run revenueos run <worker|all>         # run a worker
uv run revenueos doctor                   # what's connected, what's missing
docker compose up orchestrator panel      # the containerised topology; see deploy/README.md
```

## The vendored-code rule

A large share of RevenueOS's capability is assembled from pinned upstream sources rather
than written from scratch — see `NOTICE.md` for the full account of what and why. That
means one hard rule:

**Never hand-edit a vendored file.** If a path was written by `scripts/vendor.py`, editing
it directly will be silently overwritten the next time the tree is reassembled, and it
breaks the guarantee that every vendored file traces back to a known upstream commit. If a
vendored file needs to behave differently, add an entry to the `PATCHES` list in
`scripts/vendor.py` instead, and re-run it — that keeps the change visible, reviewable, and
reproducible from source. If you need to integrate a new upstream source entirely, add a
row and a mapping the same way; never copy files in by hand.

Everything else — the CLI, the workers, the control panel, billing, the orchestrator, the
test suite — is regular RevenueOS code and is edited normally.

## Tests are required

A change to worker logic, the approval state machine, billing/licensing, or the panel
needs a test that exercises the real behaviour change — not a description of what should
happen. Network calls in tests are monkeypatched rather than hitting real services; look
at the existing test suite for the pattern used for crawling, search, and mailbox access.
`uv run pytest -q` and, for anything touching the orchestrator, `npm test` and
`npm run typecheck` must pass before a change is ready for review.

## Voice in customer-facing text

Anything a customer reads — the CLI's own output, the control panel, the website, and
everything under `docs/` — is written in RevenueOS's own terms: what it found, what it
did, what it measured. It does not name the specific upstream projects RevenueOS is
assembled from; that accounting lives entirely in `NOTICE.md` and
`THIRD_PARTY_LICENSES/`, which is where licence attribution belongs and where anyone
curious about provenance should look.
