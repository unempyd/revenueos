#!/usr/bin/env bash
# One command after `fly auth login`: creates the billing/panel host on Fly.io, sets every
# secret from the local credential files, deploys, registers the Stripe webhook, and sets the
# webhook secret. Idempotent — re-run safely.
#
#   fly auth login                      # the only interactive step (browser)
#   deploy/fly-billing.sh               # everything else
#
# Reads: ~/.revenueos-stripe-key, ~/.revenueos-stripe-prices, ~/.revenueos-license-secret
set -euo pipefail
cd "$(dirname "$0")/.."
APP="${REVENUEOS_FLY_APP:-revenueos-billing}"
REGION="${REVENUEOS_FLY_REGION:-iad}"
HOST="https://${APP}.fly.dev"

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 2; }; }
need fly; need curl; need python3
fly auth whoami >/dev/null 2>&1 || { echo "run: fly auth login" >&2; exit 2; }

STRIPE_SECRET_KEY="$(tr -d '[:space:]' < ~/.revenueos-stripe-key)"
LICENSE_SECRET="$(tr -d '[:space:]' < ~/.revenueos-license-secret)"
# shellcheck disable=SC1090
source <(sed 's/^/export /' ~/.revenueos-stripe-prices)
PANEL_PASSWORD="${REVENUEOS_PANEL_PASSWORD:-$(openssl rand -hex 16)}"

fly apps list --json 2>/dev/null | grep -q "\"Name\": *\"$APP\"" || fly apps create "$APP"
fly volumes list --app "$APP" --json 2>/dev/null | grep -q revenueos_data || fly volumes create revenueos_data --size 1 --region "$REGION" --app "$APP" --yes

fly secrets set --app "$APP" --stage \
  REVENUEOS_PANEL_PASSWORD="$PANEL_PASSWORD" \
  REVENUEOS_LICENSE_SECRET="$LICENSE_SECRET" \
  STRIPE_SECRET_KEY="$STRIPE_SECRET_KEY" \
  STRIPE_PRICE_PRO="$STRIPE_PRICE_PRO" STRIPE_PRICE_BUSINESS="$STRIPE_PRICE_BUSINESS" STRIPE_PRICE_AGENCY="$STRIPE_PRICE_AGENCY" \
  REVENUEOS_BILLING_CANCEL_URL="https://unempyd.github.io/revenueos/pricing.html" >/dev/null

fly deploy --config deploy/fly.billing.toml --dockerfile Dockerfile --app "$APP" --yes

# Stripe webhook → this host (create once; reuse if present)
EXISTING="$(curl -s -u "$STRIPE_SECRET_KEY:" "https://api.stripe.com/v1/webhook_endpoints?limit=100" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(next((w['id'] for w in d.get('data',[]) if w['url']=='$HOST/billing/webhook'),''))")"
if [ -z "$EXISTING" ]; then
  WH="$(curl -s -u "$STRIPE_SECRET_KEY:" https://api.stripe.com/v1/webhook_endpoints \
    -d "url=$HOST/billing/webhook" -d "description=RevenueOS licence issuance" \
    -d "enabled_events[]=checkout.session.completed" -d "enabled_events[]=customer.subscription.created" \
    -d "enabled_events[]=customer.subscription.updated" -d "enabled_events[]=customer.subscription.deleted")"
  WEBHOOK_SECRET="$(printf '%s' "$WH" | python3 -c "import json,sys; print(json.load(sys.stdin)['secret'])")"
  fly secrets set --app "$APP" STRIPE_WEBHOOK_SECRET="$WEBHOOK_SECRET" >/dev/null
  echo "webhook created for $HOST/billing/webhook and STRIPE_WEBHOOK_SECRET set"
else
  echo "webhook already exists ($EXISTING); STRIPE_WEBHOOK_SECRET unchanged"
fi

echo "panel password: $PANEL_PASSWORD   (store it; it is not shown again)"
echo "checkout:       $HOST/billing/checkout?tier=pro"
curl -s -o /dev/null -w "health: %{http_code}\n" "$HOST/health"
curl -s -o /dev/null -w "checkout redirect: %{http_code}\n" "$HOST/billing/checkout?tier=pro"
