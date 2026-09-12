# RevenueOS marketing site

Two plain HTML files, no build step, no framework, no external assets except system
fonts (the CDN/font policy of the rest of the repo doesn't apply here — this is just
static HTML+CSS):

- `index.html` — the proposition, the TODAY brief example, the connect → analyse →
  opportunity → approve → execute → measure → outcome loop, an install section, and an
  honest "what's inside" credit to the vendored upstream projects (see `../NOTICE.md`).
- `pricing.html` — the four tiers (Community, Pro $99, Business $299, Agency $999), a
  full feature-comparison table, and the "how the licence key gets to your install" steps.

## How it's served

**Option A — from the panel (recommended for a single self-hosted install).**
`revenueos serve` serves this directory at `/site/` (`panel.py` maps `GET /site/*` to
`website/*`, falling back to `index.html` for `/site/` and `/site`). On that same host,
`/billing/checkout?tier=<tier>` and `/billing/webhook` are live (delegated to
`src/revenueos/billing.py:billing_http`), so every checkout button on this site
(`<a href="/billing/checkout?tier=pro">`) works unmodified — the links are relative to
whatever host is running the panel.

**Option B — any static host, for the vendor's own marketing domain.**
Copy `index.html` and `pricing.html` (as-is; there's nothing to build) to any static
host — S3+CloudFront, Netlify, GitHub Pages, nginx, etc. Because the checkout links are
relative (`/billing/checkout?tier=pro`, not an absolute URL), you must either:

1. Reverse-proxy `/billing/*` on that host through to wherever the vendor's billing
   backend runs `revenueos serve` (or an equivalent process that mounts `billing_http`), or
2. Edit the four `/billing/checkout?tier=...` hrefs in `index.html` and `pricing.html` to
   point at the vendor's billing host directly, e.g.
   `https://billing.revenueos.example/billing/checkout?tier=pro`.

Either way, the billing host needs `STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO`,
`STRIPE_PRICE_BUSINESS`, `STRIPE_PRICE_AGENCY`, `STRIPE_WEBHOOK_SECRET`, and
`REVENUEOS_LICENSE_SECRET` set (see `src/revenueos/billing.py`'s module docstring), plus
`REVENUEOS_BILLING_SUCCESS_URL` / `REVENUEOS_BILLING_CANCEL_URL` for where Stripe should
bounce the customer back to after checkout.

## Editing

Everything is inline in the two `.html` files (a `<style>` block each, duplicated on
purpose — there's no shared stylesheet to keep in sync, and no build step to update it
through). Update pricing, feature lists, or copy directly in the HTML; there is nothing
generated from `billing.py`'s `TIER_FEATURES`/`TIER_PRICES` — if those change, update this
site by hand to match.
