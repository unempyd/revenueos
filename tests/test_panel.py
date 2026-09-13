"""The control surface, driven over real HTTP against a server thread."""
from __future__ import annotations

import html
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from revenueos.panel import make_handler, make_token, token_ok
from tests.conftest import ANSWERS
from tests.test_workers import LEADS_CSV


@pytest.fixture
def server(workspace, store, monkeypatch):
    from revenueos.context import BusinessContext

    monkeypatch.setenv("REVENUEOS_PANEL_PASSWORD", "hunter2")
    ctx = BusinessContext.load(workspace)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(workspace, store, ctx, password="hunter2"))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, ctx
    srv.shutdown()


def _req(srv, method, path, body=None, cookie=None, ctype="application/x-www-form-urlencoded"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Cookie": cookie} if cookie else {}
    if body is not None:
        headers["Content-Type"] = ctype
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read().decode()
    return r.status, dict(r.getheaders()), data


def test_health_is_public_and_pages_need_login(server):
    srv, _ = server
    status, _h, body = _req(srv, "GET", "/health")
    assert status == 200 and json.loads(body)["service"] == "revenueos-panel"
    status, _h, body = _req(srv, "GET", "/")
    assert status == 401 and "Panel password" in body
    status, headers, _ = _req(srv, "POST", "/login", urlencode({"password": "wrong"}))
    assert status == 401
    status, headers, _ = _req(srv, "POST", "/login", urlencode({"password": "hunter2"}))
    assert status == 303 and headers["Set-Cookie"].startswith("rs=")
    cookie = headers["Set-Cookie"].split(";")[0]
    assert token_ok(cookie[3:])
    status, _h, body = _req(srv, "GET", "/", cookie=cookie)
    assert status == 200 and "TODAY" in body and "Not connected yet" in body


def test_onboard_run_approve_execute_results_over_http(server, workspace, store, monkeypatch):
    srv, ctx = server
    cookie = "rs=" + make_token()
    status, headers, _ = _req(srv, "POST", "/onboard", urlencode(ANSWERS), cookie=cookie)
    assert status == 303 and "Connected" in headers["Location"]
    assert ctx.is_onboarded() and ctx.company_name == "Acme Scheduling"

    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    status, headers, _ = _req(srv, "POST", "/run/discover", "", cookie=cookie)
    assert status == 303 and "qualified" in headers["Location"] and "Ran" in headers["Location"]
    status, _h, body = _req(srv, "GET", "/", cookie=cookie)
    assert "2 qualified prospects found" in body and "BrightSmile Dental" in body

    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    _req(srv, "POST", "/run/outreach", "", cookie=cookie)
    follow = next(a for a in store.list_actions("pending", "follow_up"))
    status, headers, _ = _req(srv, "POST", f"/action/{follow['id']}/approve", "", cookie=cookie)
    # an approved action does not vanish: it stays on TODAY as "waiting to run" until executed or ignored
    _s, _h, page = _req(srv, "GET", "/", cookie=cookie)
    assert "Approved, waiting to run (1)" in page and html.escape(follow["title"][:40]) in page and "Execute now" in page
    assert status == 303 and "Approved" in headers["Location"]
    status, headers, _ = _req(srv, "POST", f"/action/{follow['id']}/execute", "", cookie=cookie)
    assert status == 303 and "Executed" in headers["Location"] and "dry-run" in headers["Location"]
    assert store.get_action(follow["id"])["status"] == "executed"
    assert store.latest_outcome(follow["id"])["status"] == "pending"

    _req(srv, "POST", "/run/measure", "", cookie=cookie)
    status, _h, body = _req(srv, "GET", "/results", cookie=cookie)
    assert status == 200 and "1 emails sent" in body and "no reply yet" in body
    status, _h, body = _req(srv, "GET", "/api/today", cookie=cookie)
    payload = json.loads(body)
    assert payload["summary"]["executed"] == 1 and payload["results"][0]["id"] == follow["id"]


def test_static_site_and_path_traversal(server, workspace):
    srv, _ = server
    site = workspace.root / "website"
    site.mkdir()
    (site / "index.html").write_text("<h1>RevenueOS</h1>")
    status, _h, body = _req(srv, "GET", "/site/")
    assert status == 200 and "RevenueOS" in body
    status, _h, _b = _req(srv, "GET", "/site/../pyproject.toml")
    assert status == 404


def test_watch_it_work_streams_each_worker(server, workspace, store):
    srv, ctx = server
    cookie = "rs=" + make_token()
    status, _h, page = _req(srv, "GET", "/run-live?workers=discover,measure", cookie=cookie)
    assert status == 200 and 'data-step="discover"' in page and "EventSource" in page
    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    status, headers, body = _req(srv, "GET", "/events/run?workers=discover,measure", cookie=cookie)
    assert status == 200 and {k.lower(): v for k, v in headers.items()}.get("content-type", "").startswith("text/event-stream")
    events = [json.loads(ln[6:]) for ln in body.splitlines() if ln.startswith("data: ")]
    assert [e["state"] for e in events] == ["running", "done", "running", "done", "finished"]
    assert events[1]["worker"] == "discover" and events[1]["actions"] == 2 and events[-1]["new"] == 2
    assert events[0]["label"] == "Finding prospects" and events[2]["step"] == "measure"


def test_status_chip_and_approve_all(server, workspace, store):
    srv, ctx = server
    cookie = "rs=" + make_token()
    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    _req(srv, "POST", "/run/discover", "", cookie=cookie)
    _s, _h, page = _req(srv, "GET", "/", cookie=cookie)
    assert "0 connections" in page and "last run" in page and "Approve all 2" in page
    status, h, _ = _req(srv, "POST", "/approve-all", "", cookie=cookie)
    assert status == 303 and "Approved%202" in h["Location"]
    assert store.list_actions("pending") == [] and len(store.list_actions("approved")) == 2
