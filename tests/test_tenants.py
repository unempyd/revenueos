"""Hosted mode: accounts with their own workspaces, sessions, and the Connections / Spend pages over real HTTP."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import httpx
import pytest

from revenueos import panel
from revenueos.connections import Connection, ConnectionStore, stripe_conn
from revenueos.context import BusinessContext
from revenueos.store import Store
from revenueos.tenants import Accounts, make_session, read_session


def test_accounts_add_verify_and_workspace(workspace):
    acc = Accounts(workspace.root)
    assert not acc.enabled()
    rec = acc.add("Owner@Salon.Example", "correct horse battery", template=workspace.root)
    assert rec["email"] == "owner@salon.example" and (workspace.root / "tenants" / "owner-salon-example" / "company-context").is_dir()
    assert acc.enabled() and acc.verify("owner@salon.example", "correct horse battery")
    assert acc.verify("owner@salon.example", "wrong") is None and acc.verify("nobody@x.example", "x") is None
    with pytest.raises(ValueError):
        acc.add("owner@salon.example", "correct horse battery")
    with pytest.raises(ValueError):
        acc.add("short@x.example", "short")
    ws = acc.workspace_for("owner@salon.example")
    assert ws and ws.root.name == "owner-salon-example"
    secret = b"s"
    tok = make_session("owner@salon.example", secret)
    assert read_session(tok, secret) == "owner@salon.example" and read_session(tok + "x", secret) is None


@pytest.fixture
def hosted(workspace, monkeypatch):
    monkeypatch.delenv("REVENUEOS_PANEL_PASSWORD", raising=False)
    acc = Accounts(workspace.root)
    acc.add("a@one.example", "password-one!", template=workspace.root)
    acc.add("b@two.example", "password-two!", template=workspace.root)
    ws = acc.workspace_for("a@one.example")
    handler = panel.make_handler(ws, Store(ws.db), BusinessContext.load(ws), password=None, accounts=acc)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, acc
    srv.shutdown()


def _req(srv, method, path, data=None, cookie=None):
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    headers = {"Cookie": cookie} if cookie else {}
    with httpx.Client(base_url=base, follow_redirects=False) as c:
        r = c.request(method, path, content=data, headers={**headers, **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
    return r.status_code, dict(r.headers), r.text


def test_hosted_login_binds_each_account_to_its_own_workspace(hosted, monkeypatch):
    srv, acc = hosted
    status, _h, body = _req(srv, "GET", "/")
    assert status == 401 and 'name="email"' in body
    status, h, _ = _req(srv, "POST", "/login", urlencode({"email": "a@one.example", "password": "password-one!"}))
    assert status == 303 and "rs=" in h["set-cookie"]
    cookie_a = h["set-cookie"].split(";")[0]
    status, h, _ = _req(srv, "POST", "/login", urlencode({"email": "b@two.example", "password": "password-two!"}))
    cookie_b = h["set-cookie"].split(";")[0]
    # onboard A only; B stays unconnected
    answers = {"company_name": "One Co", "website": "https://one.example", "category": "salon", "what_we_do": "hair", "primary_audience": "locals",
               "pain_points": "no-shows", "core_offer": "cuts", "differentiators": "fast", "channels": "website", "tone": "warm",
               "sender_name": "One", "sender_email": "hi@one.example"}
    status, h, _ = _req(srv, "POST", "/onboard", urlencode(answers), cookie=cookie_a)
    assert status == 303 and "Connected" in h["location"]
    _s, _h, page_a = _req(srv, "GET", "/", cookie=cookie_a)
    _s, _h, page_b = _req(srv, "GET", "/", cookie=cookie_b)
    assert "One Co" in page_a and "One Co" not in page_b and "Not connected yet" in page_b
    assert BusinessContext.load(acc.workspace_for("a@one.example")).company_name == "One Co"
    assert BusinessContext.load(acc.workspace_for("b@two.example")).company_name != "One Co"
    # wrong password
    status, _h, body = _req(srv, "POST", "/login", urlencode({"email": "a@one.example", "password": "nope"}))
    assert status == 401 and "Wrong email or password" in body


def test_connections_page_connect_toggle_and_spend(hosted, monkeypatch):
    srv, acc = hosted
    _s, h, _ = _req(srv, "POST", "/login", urlencode({"email": "a@one.example", "password": "password-one!"}))
    cookie = h["set-cookie"].split(";")[0]
    _s, _h, page = _req(srv, "GET", "/connections", cookie=cookie)
    assert "Stripe" in page and "not connected" in page and "Google (Search Console" in page and 'action="/connections/stripe"' in page

    def fake_connect(cs, key=None, client=None):
        return cs.put(Connection(provider="stripe", account="acct_t", scopes=["read", "write"], secrets={"secret_key": key}, meta={"business": "One Co", "livemode": False}))

    monkeypatch.setattr(stripe_conn, "connect", fake_connect)
    status, h, _ = _req(srv, "POST", "/connections/stripe", urlencode({"key": "sk_test_1"}), cookie=cookie)
    assert status == 303 and "Stripe%20connected%20as%20acct_t" in h["location"]
    ws = acc.workspace_for("a@one.example")
    assert ConnectionStore(ws).get("stripe").secrets["secret_key"] == "sk_test_1" and not ConnectionStore(ws).get("stripe").allow_write
    _s, _h, page = _req(srv, "GET", "/connections", cookie=cookie)
    assert "connected · acct_t · read-only" in page and "Allow changes" in page
    status, h, _ = _req(srv, "POST", "/connections/stripe/write", urlencode({"allow": "on"}), cookie=cookie)
    assert ConnectionStore(ws).get("stripe").allow_write
    # tenant B sees nothing of it
    _s, h, _ = _req(srv, "POST", "/login", urlencode({"email": "b@two.example", "password": "password-two!"}))
    cookie_b = h["set-cookie"].split(";")[0]
    _s, _h, page_b = _req(srv, "GET", "/connections", cookie=cookie_b)
    assert "acct_t" not in page_b
    _s, _h, spend = _req(srv, "GET", "/spend", cookie=cookie)
    assert "REVENUE" in spend and "PIPELINE" in spend and "Read Stripe now" in spend
    _s, _h, api = _req(srv, "GET", "/api/connections", cookie=cookie)
    assert '"name": "stripe"' in api and '"connected": true' in api
    status, h, _ = _req(srv, "POST", "/connections/stripe/disconnect", "", cookie=cookie)
    assert ConnectionStore(ws).get("stripe") is None
