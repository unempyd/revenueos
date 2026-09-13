"""The control surface: TODAY, Approve / Execute / Ignore, RESULTS, onboarding, health.

Deliberately tiny (stdlib http.server, no build step), following the Kairos dashboard's
no-framework pattern.  Production rules:
  * REVENUEOS_PANEL_PASSWORD must be set to bind to anything but localhost; the login sets a
    signed cookie.  Without it the panel refuses non-loopback binds.
  * /health is unauthenticated (for load balancers); everything else needs the session.
  * /billing/* is delegated to billing.billing_http (Stripe webhook + checkout) when present.
  * /site/ serves the static marketing site from website/.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .context import QUESTIONS, BusinessContext
from .llm import maybe_llm
from .paths import Workspace
from .store import Store
from .today import build_brief
from .workers import execute_action, run_worker

try:  # commercial plumbing is optional in the Community build
    from .billing import billing_http, load_license
except ImportError:  # pragma: no cover
    billing_http = None  # type: ignore[assignment]
    load_license = None  # type: ignore[assignment]

STYLE = """
:root{
  color-scheme:light dark;
  --bg:#ffffff; --surface:#ffffff; --surface-2:#f5f5f7; --canvas:#f5f5f7;
  --ink:#1d1d1f; --ink-2:#6e6e73; --ink-3:#86868b;
  --line:#d2d2d7; --line-soft:#e8e8ed;
  --accent:#0066cc; --accent-ink:#ffffff;
  --ok:#00845a; --pend:#8a6d00;
  --chrome:rgba(255,255,255,.72);
  --r-card:28px; --r-btn:36px; --r-pill:980px; --r-field:10px;
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px; --sp-6:24px; --sp-8:32px; --sp-10:40px;
  --ease:cubic-bezier(.32,.72,0,1);
  --fast:140ms; --base:280ms;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#000000; --surface:#1d1d1f; --surface-2:#111113; --canvas:#000000;
  --ink:#f5f5f7; --ink-2:#a1a1a6; --ink-3:#86868b;
  --line:#424245; --line-soft:#2c2c2e;
  --accent:#2997ff; --accent-ink:#000000;
  --ok:#30d158; --pend:#ffd60a;
  --chrome:rgba(29,29,31,.72);
}}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--canvas); color:var(--ink);
  font:400 17px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,Segoe UI,Helvetica,Arial,sans-serif;
  letter-spacing:-.022em;
  max-width:880px; margin:0 auto;
  padding:0 max(var(--sp-5),env(safe-area-inset-left)) var(--sp-10);
  -webkit-font-smoothing:antialiased;
}

/* ── translucent chrome; content scrolls beneath ── */
nav{
  position:sticky; top:0; z-index:10;
  display:flex; gap:var(--sp-1); flex-wrap:wrap; align-items:center;
  margin:0 calc(-1 * var(--sp-5)) var(--sp-8);
  padding:var(--sp-3) var(--sp-5) calc(var(--sp-3) + env(safe-area-inset-top));
  background:var(--chrome);
  -webkit-backdrop-filter:saturate(180%) blur(20px); backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid transparent;
  animation:chrome linear both; animation-timeline:scroll(); animation-range:0 80px;
}
@keyframes chrome{to{border-bottom-color:var(--line-soft)}}
nav a{
  color:var(--ink-2); text-decoration:none; font-size:14px; font-weight:500; letter-spacing:-.01em;
  padding:7px 12px; border-radius:var(--r-pill); margin:0;
  transition:color var(--fast) var(--ease),background var(--fast) var(--ease);
}
nav a:hover{color:var(--ink);background:var(--surface-2)}
nav a:active{transform:scale(.96)}

main,.wrap{width:100%}
h1{
  margin:0 0 var(--sp-1);
  font-size:clamp(30px,5vw,40px); font-weight:700; line-height:1.08; letter-spacing:-.028em;
}
h2{margin:var(--sp-10) 0 var(--sp-3);font-size:21px;font-weight:600;line-height:1.19;letter-spacing:-.021em}
.sub{margin:0 0 var(--sp-8);color:var(--ink-3);font-size:17px;letter-spacing:-.022em}

/* ── the numbers block: alignment is load-bearing, keep it monospace ── */
.brief{
  margin:0 0 var(--sp-6);
  background:var(--surface); border:1px solid var(--line-soft); border-radius:var(--r-card);
  padding:var(--sp-6) var(--sp-8); white-space:pre; overflow-x:auto;
  font:500 16px/1.75 ui-monospace,"SF Mono",Menlo,monospace;
  font-variant-numeric:tabular-nums; font-feature-settings:"numr"; letter-spacing:0;
  animation:rise var(--base) var(--ease) both;
}

/* ── action rows ── */
.row{
  margin:0 0 var(--sp-2);
  display:flex; gap:var(--sp-3); align-items:center; flex-wrap:wrap;
  background:var(--surface); border:1px solid var(--line-soft); border-radius:var(--r-card);
  padding:var(--sp-4) var(--sp-5);
  transition:border-color var(--base) var(--ease),background var(--base) var(--ease);
  animation:rise var(--base) var(--ease) both;
}
.row:nth-child(1){animation-delay:20ms}.row:nth-child(2){animation-delay:40ms}
.row:nth-child(3){animation-delay:60ms}.row:nth-child(4){animation-delay:80ms}
.row:nth-child(5){animation-delay:100ms}.row:nth-child(n+6){animation-delay:120ms}
.row:hover{border-color:var(--line)}
.t{flex:1 1 min(100%,22rem); min-width:0}
.t b{display:block;font-size:17px;font-weight:600;line-height:1.29;letter-spacing:-.022em;margin:2px 0 4px}
.t small{display:block;color:var(--ink-3);font-size:14px;line-height:1.5;letter-spacing:-.016em;white-space:pre-wrap}
.type{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3)}

/* ── controls: feedback on press, not release ── */
form{display:inline-flex;margin:0 var(--sp-2) var(--sp-2) 0}
button{
  font:inherit; font-size:14px; font-weight:500; letter-spacing:-.016em;
  min-height:36px; padding:8px 16px;
  border:1px solid var(--line); background:var(--surface); color:var(--ink);
  border-radius:var(--r-btn); cursor:pointer;
  transition:transform var(--fast) var(--ease),background var(--fast) var(--ease),border-color var(--fast) var(--ease);
}
button:hover{background:var(--surface-2)}
button:active{transform:scale(.96)}
button.x{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
button.x:hover{filter:brightness(1.08)}
button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible{
  outline:2px solid var(--accent); outline-offset:2px;
}

.msg{
  margin:0 0 var(--sp-5);
  background:var(--surface); border:1px solid var(--line-soft); border-left:3px solid var(--ok);
  border-radius:var(--r-card); padding:var(--sp-4) var(--sp-5);
  font-size:15px; line-height:1.5; white-space:pre-line;
  animation:rise var(--base) var(--ease) both;
}

/* ── tables scroll rather than squash ── */
table{
  width:100%;border-collapse:collapse;
  background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--r-card);
  overflow:hidden;font-size:15px;
}
th{
  font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
  color:var(--ink-3);padding:var(--sp-3) var(--sp-4);text-align:left;
  border-bottom:1px solid var(--line-soft);
}
td{padding:var(--sp-3) var(--sp-4);text-align:left;vertical-align:top;border-bottom:1px solid var(--line-soft);letter-spacing:-.016em}
tr:last-child td{border-bottom:0}
.ok{color:var(--ok);font-weight:500}.pend{color:var(--pend);font-weight:500}.none{color:var(--ink-3)}

label{display:block;margin:var(--sp-5) 0 var(--sp-2);font-size:14px;font-weight:600;letter-spacing:-.016em}
input,textarea{
  display:block;width:100%;
  font:inherit;font-size:16px;padding:11px 14px;
  background:var(--surface);color:var(--ink);
  border:1px solid var(--line);border-radius:var(--r-field);
  transition:border-color var(--fast) var(--ease);
}
input:focus,textarea:focus{border-color:var(--accent);outline:none}

@keyframes rise{from{opacity:0;transform:translate3d(0,10px,0)}to{opacity:1;transform:none}}

@media (max-width:600px){
  body{font-size:16px;padding-bottom:var(--sp-8)}
  h1{font-size:28px}
  .brief{padding:var(--sp-5);font-size:14px}
  .row{padding:var(--sp-4)}
  button{min-height:44px;flex:1 1 auto;justify-content:center}
  form{flex:1 1 auto}
  table{display:block;overflow-x:auto;white-space:nowrap}
}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-delay:0ms!important;transition-duration:.01ms!important}
  .brief,.row,.msg{animation:none}
  button:active{transform:none}
}
@media (prefers-reduced-transparency:reduce){
  nav{background:var(--surface);-webkit-backdrop-filter:none;backdrop-filter:none;border-bottom:1px solid var(--line-soft)}
}
@media (prefers-contrast:more){
  :root{--line:#000;--line-soft:#555;--ink-3:#444}
  .row,.brief,table{border-width:1.5px}
}
"""

PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RevenueOS — {title}</title>
<style>{style}</style>
<nav><a href="/">TODAY</a><a href="/results">RESULTS</a><a href="/onboard">Business</a><a href="/site/">Site</a>{nav_extra}</nav>
<h1>{title}</h1><div class="sub">{sub}</div>
{msg}
{body}
"""

ROW = """<div class="row"><div class="t"><span class="type">{atype}</span><b>{title}</b><small>{content}</small>{link}</div>
<form method="post" action="/action/{id}/approve"><button>Approve</button></form>
<form method="post" action="/action/{id}/execute"><button class="x">Execute</button></form>
<form method="post" action="/action/{id}/ignore"><button>Ignore</button></form></div>"""

ROW_APPROVED = """<div class="row"><div class="t"><span class="type">{atype} · approved, waiting to run</span><b>{title}</b><small>{content}</small>{link}</div>
<form method="post" action="/action/{id}/execute"><button class="x">Execute now</button></form>
<form method="post" action="/action/{id}/ignore"><button>Ignore</button></form></div>"""

LOGIN = """<form method="post" action="/login"><label>Panel password</label><input type="password" name="password" autofocus>
<p><button class="x">Sign in</button></p></form>"""


def _session_secret() -> bytes:
    return (os.environ.get("REVENUEOS_PANEL_PASSWORD", "") + "|revenueos-session").encode()


def make_token() -> str:
    exp = str(int(time.time()) + 12 * 3600)
    sig = hmac.new(_session_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def token_ok(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(_session_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig) and exp.isdigit() and int(exp) > time.time()


def make_handler(ws: Workspace, store: Store, ctx: BusinessContext, *, password: str | None):
    website_dir = ws.root / "website"

    class Handler(BaseHTTPRequestHandler):
        server_version = f"RevenueOS/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ── plumbing ──
        def _send(self, body: str | bytes, status: int = 200, ctype: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _page(self, title: str, sub: str, body: str, msg: str = "") -> str:
            lic = ""
            if load_license is not None:
                try:
                    lic = f'<span class="none">licence: {load_license(ws).tier.value}</span>'
                except Exception:
                    lic = ""
            return PAGE.format(title=html.escape(title), sub=html.escape(sub), body=body, style=STYLE, nav_extra=lic,
                               msg=f'<div class="msg">{html.escape(msg)}</div>' if msg else "")

        def _authed(self) -> bool:
            if not password:
                return True
            cookie = self.headers.get("Cookie", "")
            token = next((c.split("=", 1)[1] for c in cookie.split(";") if c.strip().startswith("rs=")), None)
            return token_ok(token.strip() if token else None)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        # ── GET ──
        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path == "/health":
                self._send(json.dumps({"ok": True, "service": "revenueos-panel", "version": __version__, "onboarded": ctx.is_onboarded()}),
                           ctype="application/json")
                return
            if path.startswith("/site/") or path == "/site":
                self._static(path[len("/site/"):] or "index.html")
                return
            if path in ("/sitemap.xml", "/robots.txt"):  # crawlers look for these at the host root
                self._static(path[1:])
                return
            if billing_http is not None and path.startswith("/billing/"):
                res = billing_http(self.path, "GET", b"", dict(self.headers), ws)
                if res:
                    status, ctype, body = res
                    if status in (301, 302, 303):
                        self._redirect(body)
                    else:
                        self._send(body, status, ctype)
                    return
            if not self._authed():
                self._send(self._page("Sign in", "RevenueOS control panel", LOGIN), 401)
                return
            msg = parse_qs(url.query).get("msg", [""])[0]
            if path == "/api/today":
                b = build_brief(store)
                self._send(json.dumps({"counts": b.counts, "funnel": b.funnel, "pipeline_value": b.pipeline_value, "actions": b.actions, "approved": b.approved,
                                       "results": b.results, "summary": b.summary}, default=str), ctype="application/json")
            elif path == "/results":
                self._send(self._page("RESULTS", f"{ctx.company_name} — what RevenueOS did and what happened", self._results_html(), msg))
            elif path == "/onboard":
                self._send(self._page("Connect your business", "One questionnaire. Blank answers keep the current text.", self._onboard_form(), msg))
            elif path == "/":
                self._send(self._page("TODAY", ctx.company_name if ctx.is_onboarded() else "Not connected yet — open Business",
                                      self._today_html(), msg))
            else:
                self._send("not found", 404)

        def _static(self, rel: str) -> None:
            base = website_dir.resolve()
            target = (website_dir / rel).resolve()
            if not base.is_dir() or (target != base and base not in target.parents):
                self._send("not found", 404)
                return
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                self._send("not found", 404)
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(target.read_bytes(), 200, ctype)

        def _today_html(self) -> str:
            b = build_brief(store)
            rows = "".join(
                ROW.format(
                    id=a["id"], atype=html.escape(a["action_type"].replace("_", " ")), title=html.escape(a["title"]),
                    content=html.escape((a["content"] or "")[:400]),
                    link=f'<br><a href="{html.escape(a["source_url"])}" target="_blank" rel="noopener">{html.escape(a["source_url"])}</a>' if a.get("source_url") else "",
                )
                for a in b.actions[:100]
            )
            run_form = ('<form method="post" action="/run/all"><button>Run all workers now</button></form> '
                        '<form method="post" action="/run/measure"><button>Measure results</button></form>')
            approved_rows = "".join(
                ROW_APPROVED.format(
                    id=a["id"], atype=html.escape(a["action_type"].replace("_", " ")), title=html.escape(a["title"]),
                    content=html.escape((a["content"] or "")[:400]),
                    link=f'<br><a href="{html.escape(a["source_url"])}" target="_blank" rel="noopener">{html.escape(a["source_url"])}</a>' if a.get("source_url") else "",
                )
                for a in b.approved[:100]
            )
            approved_html = (f"<h2>Approved, waiting to run ({len(b.approved)})</h2>"
                             "<p class='sub'>You said yes. Nothing happens until Execute; Execute sends the email or runs the skill and the result lands in RESULTS.</p>"
                             + approved_rows) if b.approved else ""
            return (f'<div class="brief">{html.escape(chr(10).join(b.lines()))}</div>{run_form}<h2>Approve / Execute / Ignore</h2>'
                    + (rows or "<p>Nothing pending.</p>")
                    + "<p class='sub'>Approve = mark as wanted. Execute = do it now (send / run the skill). Ignore = drop it.</p>"
                    + approved_html)

        def _results_html(self) -> str:
            b = build_brief(store)
            s = b.summary
            head = (f"<div class='brief'>{s.get('found', 0)} opportunities found · {s.get('executed', 0)} executed · {s.get('measured', 0)} measured\n"
                    f"{s.get('emails_sent', 0)} emails sent · {s.get('replies', 0)} replies · {s.get('bounces', 0)} bounces · {s.get('booked', 0)} booked\n"
                    f"${s.get('pipeline_value', 0):,.0f} pipeline</div>")
            ext = {k.removeprefix("growth_"): v for k, v in b.metrics.items() if k.startswith("growth_")}
            if ext:
                head += "<div class='brief'>EXTERNAL (real numbers, last growth run)\n" + "\n".join(f"{k:<28} {v:g}" for k, v in sorted(ext.items())) + "</div>"
            rows = []
            for a in b.results[:200]:
                o = a.get("outcome") or {}
                st = o.get("status") or "pending"
                cls = {"measured": "ok", "pending": "pend"}.get(st, "none")
                if o.get("metric") is not None and o.get("after_value") is not None:
                    what = f"{o['metric']}: {o.get('before_value') or 0:g} → {o['after_value']:g}" + (f" — {o['note']}" if o.get("note") else "")
                else:
                    what = o.get("note") or "awaiting measurement"
                rows.append(f"<tr><td>{a['id']}</td><td>{html.escape(a['action_type'].replace('_', ' '))}</td><td>{html.escape(a['title'][:80])}</td>"
                            f"<td>{html.escape(a.get('executed_at') or '')[:16]}</td><td class='{cls}'>{html.escape(st)}</td><td>{html.escape(what)}</td></tr>")
            table = ("<table><tr><th>#</th><th>What it found</th><th>What it did</th><th>When</th><th>Result</th><th>Measured</th></tr>"
                     + "".join(rows) + "</table>") if rows else "<p>No executed actions yet.</p>"
            return head + "<h2>Every executed action</h2>" + table

        def _onboard_form(self) -> str:
            cur = {"company_name": ctx.company_name if ctx.is_onboarded() else "", "website": ctx.website or "",
                   "competitors": ", ".join(ctx.competitors), "channels": ", ".join(ctx.channels),
                   "sender_name": (ctx.config.get("sender") or {}).get("name", ""), "sender_email": (ctx.config.get("sender") or {}).get("email", ""),
                   "booking_url": ctx.config.get("booking_url") or ""}
            fields = []
            for key, prompt, target in QUESTIONS:
                value = cur.get(key) or (ctx.section(*target) if target and ctx.is_onboarded() else "")
                if target and value and value.split()[0] in ("Write", "State", "List", "Describe", "Name", "Include", "Define", "Explain", "Replace"):
                    value = ""
                tag = "textarea" if target and key in ("pain_points", "differentiators", "primary_audience") else "input"
                fields.append(f"<label>{html.escape(prompt)}</label>" + (
                    f'<textarea name="{key}" rows="3">{html.escape(value)}</textarea>' if tag == "textarea"
                    else f'<input name="{key}" value="{html.escape(value)}">'))
            return '<form method="post" action="/onboard">' + "".join(fields) + '<p><button class="x">Save and connect</button></p></form>'

        # ── POST ──
        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._body()
            if billing_http is not None and path.startswith("/billing/"):
                res = billing_http(self.path, "POST", body, dict(self.headers), ws)
                if res:
                    status, ctype, out = res
                    self._send(out, status, ctype)
                    return
            if path == "/login":
                form = parse_qs(body.decode("utf-8", "replace"))
                if password and hmac.compare_digest(form.get("password", [""])[0], password):
                    self._redirect("/", {"Set-Cookie": f"rs={make_token()}; HttpOnly; SameSite=Lax; Path=/"})
                else:
                    self._send(self._page("Sign in", "Wrong password", LOGIN), 401)
                return
            if not self._authed():
                self._send("unauthorized", 401)
                return
            if path == "/onboard":
                form = {k: v[0].strip() for k, v in parse_qs(body.decode("utf-8", "replace")).items() if v and v[0].strip()}
                if not form:
                    self._redirect("/onboard?msg=" + quote("nothing to save"))
                    return
                ctx.onboard(form)
                errors = ctx.validate()
                self._redirect("/?msg=" + quote(("Connected " + form.get("company_name", "")) if not errors else "Saved with validation errors: " + "; ".join(errors)[:300]))
                return
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "run":
                names = ["discover", "outreach", "inbox", "seo", "ads-audit", "content", "monitor", "measure"] if parts[1] == "all" else [parts[1]]
                llm = maybe_llm()
                results = [run_worker(n, ws, store, ctx, llm, "panel") for n in names]
                new = sum(r.actions_created for r in results)
                lines = [f"Ran {len(names)} check(s): {new} new opportunit{'y' if new == 1 else 'ies'}."]
                lines += [f"{n}: {r.summary or ('failed: ' + (r.error or ''))}" for n, r in zip(names, results, strict=True)]
                self._redirect("/?msg=" + quote("\n".join(lines)[:1200]))
                return
            if len(parts) == 3 and parts[0] == "action" and parts[2] in ("approve", "execute", "ignore"):
                aid, verb = int(parts[1]), parts[2]
                action = store.get_action(aid)
                if not action:
                    self._send("no such action", 404)
                    return
                try:
                    if verb == "ignore":
                        store.set_action_status(aid, "ignored")
                        msg = f"Ignored: {action['title']}"
                    elif verb == "approve":
                        store.set_action_status(aid, "approved")
                        if action["context"].get("draft_id"):
                            store.set_draft_approval(action["context"]["draft_id"], "approved", by="panel")
                        msg = f"Approved: {action['title']}"
                    else:
                        outcome = execute_action(ws, store, ctx, maybe_llm(), action)
                        store.set_action_status(aid, "executed")
                        store.record_outcome(aid, "pending", note=f"executed: {outcome[:200]}")
                        msg = f"Executed: {action['title']} — {outcome}"
                except Exception as exc:
                    store.set_action_status(aid, "failed")
                    msg = f"Failed: {type(exc).__name__}: {exc}"
                self._redirect("/?msg=" + quote(msg[:900]))
                return
            self._send("not found", 404)

    return Handler


def serve(ws: Workspace, store: Store, ctx: BusinessContext, host: str = "127.0.0.1", port: int = 8791) -> int:
    password = os.environ.get("REVENUEOS_PANEL_PASSWORD") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        print("refusing to bind a non-loopback address without REVENUEOS_PANEL_PASSWORD", flush=True)
        return 2
    server = ThreadingHTTPServer((host, port), make_handler(ws, store, ctx, password=password))
    print(f"RevenueOS panel on http://{host}:{port}  (auth: {'password' if password else 'none — localhost only'}; Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
