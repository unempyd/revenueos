"""OAuth 2.0 authorisation-code flow with PKCE, a loopback callback, and token refresh.

The customer clicks Connect, a browser opens the provider's consent page, the provider redirects to
http://127.0.0.1:<port>/callback on this machine (or to the hosted panel's /connections/<provider>/callback),
the code is exchanged for tokens, and the tokens go into the ConnectionStore. Nothing is stored before
the provider says yes. Refresh happens on demand; a refreshed token is written back.

Provider endpoints and the client credentials (an OAuth client the owner registers once with the
provider) come from environment variables named in PROVIDERS below.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import httpx

PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID", "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "extra_auth": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "scopes": {
            "analytics.read": "https://www.googleapis.com/auth/analytics.readonly",
            "searchconsole.read": "https://www.googleapis.com/auth/webmasters.readonly",
            "ads.read": "https://www.googleapis.com/auth/adwords",   # Google Ads has one scope for read and write
            "ads.write": "https://www.googleapis.com/auth/adwords",
            "calendar.write": "https://www.googleapis.com/auth/calendar.events",
            "identity": "openid email",
        },
    },
    "meta": {
        "auth": "https://www.facebook.com/v21.0/dialog/oauth",
        "token": "https://graph.facebook.com/v21.0/oauth/access_token",
        "client_id_env": "META_APP_ID", "client_secret_env": "META_APP_SECRET",
        "extra_auth": {},
        "scopes": {"ads.read": "ads_read", "ads.write": "ads_management", "identity": "public_profile"},
        "pkce": False,
    },
}


def client_credentials(provider: str) -> tuple[str | None, str | None]:
    cfg = PROVIDERS[provider]
    return os.environ.get(cfg["client_id_env"]), os.environ.get(cfg["client_secret_env"])


def scope_string(provider: str, wanted: list[str]) -> str:
    table = PROVIDERS[provider]["scopes"]
    parts: list[str] = []
    for w in wanted:
        for s in table.get(w, w).split():
            if s not in parts:
                parts.append(s)
    return " ".join(parts)


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(provider: str, redirect_uri: str, wanted: list[str], state: str, verifier_challenge: str | None) -> str:
    cfg = PROVIDERS[provider]
    client_id, _ = client_credentials(provider)
    params = {"client_id": client_id or "", "redirect_uri": redirect_uri, "response_type": "code",
              "scope": scope_string(provider, wanted), "state": state, **cfg.get("extra_auth", {})}
    if verifier_challenge:
        params.update({"code_challenge": verifier_challenge, "code_challenge_method": "S256"})
    return cfg["auth"] + "?" + urllib.parse.urlencode(params)


def exchange_code(provider: str, code: str, redirect_uri: str, verifier: str | None, client: httpx.Client | None = None) -> dict[str, Any]:
    cfg = PROVIDERS[provider]
    client_id, client_secret = client_credentials(provider)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": client_id or "",
            "client_secret": client_secret or ""}
    if verifier:
        data["code_verifier"] = verifier
    c = client or httpx.Client(timeout=30)
    r = c.post(cfg["token"], data=data)
    if r.status_code != 200:
        raise RuntimeError(f"{provider} token exchange failed: {r.status_code} {r.text[:200]}")
    tok = r.json()
    tok["obtained_at"] = int(time.time())
    return tok


def refresh(provider: str, tokens: dict[str, Any], client: httpx.Client | None = None) -> dict[str, Any]:
    cfg = PROVIDERS[provider]
    client_id, client_secret = client_credentials(provider)
    if not tokens.get("refresh_token"):
        raise RuntimeError(f"{provider}: no refresh token; reconnect")
    c = client or httpx.Client(timeout=30)
    r = c.post(cfg["token"], data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                   "client_id": client_id or "", "client_secret": client_secret or ""})
    if r.status_code != 200:
        raise RuntimeError(f"{provider} refresh failed: {r.status_code} {r.text[:200]}")
    new = r.json()
    new.setdefault("refresh_token", tokens["refresh_token"])
    new["obtained_at"] = int(time.time())
    return new


def access_token(provider: str, tokens: dict[str, Any], client: httpx.Client | None = None) -> tuple[str, dict[str, Any]]:
    """A valid access token, refreshing when within 60 s of expiry. Returns (token, possibly-updated tokens)."""
    exp = int(tokens.get("obtained_at", 0)) + int(tokens.get("expires_in", 3600))
    if time.time() > exp - 60 and tokens.get("refresh_token"):
        tokens = refresh(provider, tokens, client)
    return tokens["access_token"], tokens


# ── loopback flow for a CLI / local panel ────────────────────────────────────
class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.result = {k: v[0] for k, v in q.items()}
        body = b"<h2>Connected. You can close this tab and return to RevenueOS.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


def loopback_flow(provider: str, wanted: list[str], *, open_browser: bool = True, timeout: float = 300.0,
                  port: int = 0, client: httpx.Client | None = None) -> dict[str, Any]:
    """Run the whole flow on this machine: start a callback listener, open the consent page, wait, exchange."""
    cfg = PROVIDERS[provider]
    client_id, _ = client_credentials(provider)
    if not client_id:
        raise RuntimeError(f"{provider}: set {cfg['client_id_env']} and {cfg['client_secret_env']} (an OAuth client you register with the provider once)")
    srv = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    redirect_uri = f"http://127.0.0.1:{srv.server_port}/callback"
    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce() if cfg.get("pkce", True) else (None, None)
    url = authorize_url(provider, redirect_uri, wanted, state, challenge)
    _Catcher.result = {}
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    if open_browser:
        webbrowser.open(url)
    print(f"Open this in a browser to connect {provider}:\n{url}", flush=True)
    t.join(timeout)
    srv.server_close()
    res = _Catcher.result
    if not res:
        raise TimeoutError(f"{provider}: no callback within {timeout:.0f}s")
    if res.get("state") != state:
        raise RuntimeError(f"{provider}: state mismatch; refusing the callback")
    if "error" in res:
        raise RuntimeError(f"{provider}: {res.get('error')} {res.get('error_description', '')}".strip())
    return exchange_code(provider, res["code"], redirect_uri, verifier, client)
