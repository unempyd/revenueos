#!/usr/bin/env bash
# Post-deploy smoke test for the RevenueOS panel.
#
#   deploy/smoke.sh <base-url> [panel-password]
#   deploy/smoke.sh https://revenueos.example.com
#   deploy/smoke.sh https://revenueos.example.com "$REVENUEOS_PANEL_PASSWORD"
#   REVENUEOS_PANEL_PASSWORD=... deploy/smoke.sh http://127.0.0.1:8791
#
# Exits 0 if the panel is up and answering; non-zero otherwise.
#
#   GET /health     — always unauthenticated (src/revenueos/panel.py keeps
#                      it that way for load balancers). Must be 200 JSON
#                      with "ok": true.
#   GET /api/today  — authenticated once REVENUEOS_PANEL_PASSWORD is set on
#                      the server (everything but /health, /site/, /billing/
#                      is behind the panel's session cookie in production).
#                      If a password is given here, this script logs in via
#                      POST /login first and checks /api/today for real.
#                      If no password is given and the server returns 401,
#                      that is treated as a WARN, not a failure: it proves
#                      the panel is up and correctly enforcing auth, not
#                      that it's broken. Any other non-200 (5xx, connection
#                      failure, timeout, garbage body) is still a hard fail.
set -euo pipefail

base="${1:-}"
password="${2:-${REVENUEOS_PANEL_PASSWORD:-}}"
if [ -z "$base" ]; then
  echo "usage: $0 <base-url> [panel-password]" >&2
  exit 2
fi
base="${base%/}"

fail=0
cookiejar="$(mktemp)"
trap 'rm -f "$cookiejar"' EXIT

curl_get() {
  # $1 path -> prints "HTTPCODE\nBODY"
  local url="${base}$1"
  local tmp code
  tmp="$(mktemp)"
  if ! code=$(curl -sS -b "$cookiejar" -o "$tmp" -w '%{http_code}' --max-time 10 "$url" 2>/dev/null); then
    code="000"
  fi
  printf '%s\n' "$code"
  cat "$tmp"
  rm -f "$tmp"
}

# ── /health — always unauthenticated, always a hard requirement ──
result="$(curl_get /health)"
code="$(printf '%s' "$result" | head -1)"
body="$(printf '%s' "$result" | tail -n +2)"
if [ "$code" = "200" ] && printf '%s' "$body" | grep -q '"ok": true'; then
  echo "ok   health: ${base}/health -> 200"
else
  echo "FAIL health: ${base}/health -> ${code}" >&2
  echo "  body: ${body:0:300}" >&2
  fail=1
fi

# ── optional login, so /api/today can be checked authenticated ──
if [ -n "$password" ]; then
  if ! login_code=$(curl -sS -c "$cookiejar" -o /dev/null -w '%{http_code}' --max-time 10 \
    --data-urlencode "password=${password}" "${base}/login" 2>/dev/null); then
    login_code="000"
  fi
  if [ "$login_code" != "303" ] && [ "$login_code" != "200" ]; then
    echo "FAIL login: POST ${base}/login -> ${login_code} (bad panel password?)" >&2
    fail=1
  fi
fi

# ── /api/today ──
result="$(curl_get /api/today)"
code="$(printf '%s' "$result" | head -1)"
body="$(printf '%s' "$result" | tail -n +2)"
if [ "$code" = "200" ] && printf '%s' "$body" | grep -q '"counts"'; then
  echo "ok   today: ${base}/api/today -> 200"
elif [ "$code" = "401" ] && [ -z "$password" ]; then
  echo "warn today: ${base}/api/today -> 401 (no password given — panel is up and auth is enforced, not checked further)"
else
  echo "FAIL today: ${base}/api/today -> ${code}" >&2
  echo "  body: ${body:0:300}" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "smoke test FAILED against ${base}" >&2
  exit 1
fi
echo "smoke test passed against ${base}"
