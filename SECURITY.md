# Security policy

## Reporting a vulnerability

Please report suspected security issues to **hello@revenueos.com.au**.
Do not open a public issue for a suspected vulnerability.

Include, if you can:

- A description of the issue and its potential impact.
- Steps to reproduce it, or a proof of concept.
- The version or commit you tested against.

We read that mailbox and will reply once we have looked at the report.

## Disclosure timeline

We ask for 90 days from the initial report before any public disclosure, to give us time to
investigate, fix and release a patch. We are happy to credit reporters in the release notes
for a fix unless you would prefer to stay anonymous.

## Supported versions

RevenueOS is under active development. Security fixes are made against the latest
released version; if you are running an older version, please upgrade before reporting an
issue that may already be fixed.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases | No — please upgrade first |

## Scope

This policy covers the RevenueOS codebase in this repository: the CLI, the control panel,
the orchestrator, billing/licensing, and the capability packs shipped here. It does not
cover third-party services RevenueOS can be configured to connect to (your mailbox
provider, ad platforms, Stripe, or an optional external lead-generation service) — please
report issues in those services to their own maintainers.
