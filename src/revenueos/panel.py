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
from .connections import ConnectionStore, describe_all, oauth
from .context import QUESTIONS, BusinessContext
from .llm import maybe_llm
from .paths import Workspace
from .store import Store
from .tenants import Accounts, make_session, read_session
from .today import build_brief
from .workers import execute_action, refused, run_worker

try:  # commercial plumbing is optional in the Community build
    from .billing import billing_http, load_license
except ImportError:  # pragma: no cover
    billing_http = None  # type: ignore[assignment]
    load_license = None  # type: ignore[assignment]

STYLE = """
/* One design language with website/design.css: the site is a black product stage
   with no light mode, so the dashboard a customer opens after installing is the
   same stage. The OS preference does not flip it — a customer who clicks through
   from the site must not land in a white app. Light is still available, but only
   when a person asks for it by name (data-theme="light" on the root). */
:root{
  color-scheme:dark;
  --bg:#000000; --surface:#1d1d1f; --surface-2:#111113; --canvas:#000000;
  --ink:#f5f5f7; --ink-2:#a1a1a6; --ink-3:#86868b;
  --line:#424245; --line-soft:#2c2c2e;
  --accent:#0071e3; --accent-ink:#ffffff;   /* the site's single filled CTA colour */
  --focus:#2997ff;                          /* halo: rings and edges, never a fill */
  --orange:#f56900;                         /* eyebrows only, as on the site */
  --ok:#30d158; --pend:#ffd60a; --bad:#ff453a;
  --chrome:rgba(29,29,31,.72);
  --r-card:28px; --r-btn:36px; --r-pill:980px; --r-field:10px;
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px; --sp-6:24px; --sp-8:32px; --sp-10:40px;
  --ease:cubic-bezier(.32,.72,0,1);
  --fast:140ms; --base:280ms;
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#ffffff; --surface:#ffffff; --surface-2:#f5f5f7; --canvas:#f5f5f7;
  --ink:#1d1d1f; --ink-2:#6e6e73; --ink-3:#86868b;
  --line:#d2d2d7; --line-soft:#e8e8ed;
  --accent:#0071e3; --accent-ink:#ffffff;
  --focus:#0071e3;
  --orange:#f56900;
  --ok:#00845a; --pend:#8a6d00; --bad:#b00020;
  --chrome:rgba(255,255,255,.72);
}

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
/* the same mark the site wears, inheriting the text colour: frost here, ink under
   data-theme="light". The 8-element cut, because the full one turns to mush this small. */
nav .brand{display:inline-flex;align-items:center;margin-right:var(--sp-3);color:var(--ink)}
nav .brand svg{width:19px;height:19px;display:block}
nav a{
  color:var(--ink-2); text-decoration:none; font-size:14px; font-weight:500; letter-spacing:-.01em;
  padding:7px 12px; border-radius:var(--r-pill); margin:0;
  transition:color var(--fast) var(--ease),background var(--fast) var(--ease);
}
nav a:hover{color:var(--ink);background:var(--surface-2)}
nav a:active{transform:scale(.96)}

main,.wrap{width:100%}

/* Content links. Only nav links were styled, so a source URL inside a row fell back
   to the browser's own blue-on-black (and visited purple), the one element on the
   page not speaking the product's language. Halo blue, as on the site; the fill blue
   stays reserved for a button. */
.row a,.t a,p a,td a{
  color:var(--focus); text-decoration:none;
  border-bottom:1px solid color-mix(in srgb,var(--focus) 40%,transparent);
  overflow-wrap:anywhere;
  transition:border-color var(--fast) var(--ease);
}
.row a:visited,.t a:visited,p a:visited,td a:visited{color:var(--focus)}
.row a:hover,.t a:hover,p a:hover,td a:hover{border-bottom-color:var(--focus)}
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
/* The same card carrying prose, not a column of figures: `pre` is right for the
   numbers, where alignment is load-bearing, and wrong for a sentence — it pushed
   the objective line off the card's right edge instead of wrapping. */
.brief.note{
  white-space:pre-wrap; overflow-wrap:anywhere;
  font:400 15px/1.6 var(--sans,-apple-system,BlinkMacSystemFont,system-ui,sans-serif);
  letter-spacing:-.016em;
}
.brief.note b{font-weight:600}
.brief.note small{display:block;margin-top:var(--sp-2);color:var(--ink-3);font-size:13px}

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
.type{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--orange)}

/* ── controls: feedback on press, not release ── */
form{display:inline-flex;margin:0 var(--sp-2) var(--sp-2) 0}
.chip{margin-right:10px}
.agents{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px} .agentchip{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--ink-2)} .agentchip b{font-weight:600;color:var(--ink)} .agentchip.ok{border-color:var(--ok)} .agentchip.bad{border-color:#b00020}
.stepper{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 var(--sp-5)} .step{padding:6px 14px;border-radius:var(--r-pill);border:1px solid var(--line);color:var(--ink-3);font-size:13px} .step.done{color:var(--ok);border-color:var(--ok)} .step.on{color:var(--focus);border-color:var(--focus)} .step.ok{color:var(--ok);border-color:var(--ok)} .step.bad{color:var(--bad);border-color:var(--bad)} #feed{max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word} a.cta{display:inline-block;padding:10px 18px;border-radius:var(--r-btn);background:var(--accent);color:var(--accent-ink);text-decoration:none;font-weight:600}
form.stack{display:block;flex:none;width:100%;margin:0 0 var(--sp-6)} form.stack input,form.stack textarea{width:100%;box-sizing:border-box} form.once{padding:var(--sp-5);border:1px solid var(--line-soft);border-radius:var(--r-card);background:var(--surface)}
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
  outline:2px solid var(--focus); outline-offset:2px;
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
input:focus,textarea:focus{border-color:var(--focus);outline:none}

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
<nav><span class="brand" aria-hidden="true"><svg viewBox="0 0 100 100" fill="currentColor"><path d="M48.77 84.24 L48.77 84.77 C48.77 86.73 47.95 88.56 46.62 90 C45.75 90.94 45.27 92.24 45.43 93.66 C45.67 95.76 47.37 97.46 49.47 97.7 C52.25 98 54.59 95.84 54.59 93.12 C54.59 91.85 54.09 90.7 53.25 89.88 C51.91 88.54 51.23 86.65 51.23 84.75 L51.23 84.22 C51.23 81.87 52.42 79.71 54.31 78.31 C56.21 76.92 57.41 74.65 57.3 72.09 C57.14 68.38 54.11 65.33 50.4 65.13 C46.19 64.9 42.7 68.26 42.7 72.42 C42.7 74.84 43.86 76.97 45.66 78.3 C47.57 79.71 48.77 81.87 48.77 84.24 ZM48.77 84.24"/><path d="M24.92 73.34 L24.55 73.72 C23.17 75.11 21.28 75.82 19.32 75.9 C18.04 75.95 16.78 76.53 15.89 77.64 C14.58 79.29 14.59 81.7 15.9 83.35 C17.65 85.53 20.84 85.66 22.76 83.74 C23.66 82.84 24.1 81.67 24.1 80.49 C24.1 78.59 24.95 76.78 26.29 75.44 L26.67 75.06 C28.33 73.39 30.71 72.71 33.03 73.08 C35.35 73.43 37.82 72.67 39.54 70.79 C42.04 68.04 42.07 63.75 39.59 60.99 C36.77 57.84 31.92 57.74 28.97 60.69 C27.28 62.4 26.59 64.73 26.92 66.94 C27.28 69.29 26.59 71.67 24.92 73.34 ZM24.92 73.34"/><path d="M15.75 48.77 L15.22 48.77 C13.27 48.77 11.44 47.95 10 46.62 C9.06 45.75 7.76 45.27 6.34 45.43 C4.24 45.67 2.54 47.37 2.3 49.47 C2 52.25 4.16 54.61 6.87 54.61 C8.15 54.61 9.3 54.09 10.12 53.25 C11.48 51.91 13.35 51.23 15.25 51.23 L15.78 51.23 C18.13 51.23 20.29 52.42 21.69 54.31 C23.08 56.21 25.35 57.41 27.91 57.3 C31.62 57.14 34.67 54.11 34.87 50.42 C35.1 46.19 31.74 42.69 27.58 42.69 C25.16 42.69 23.03 43.86 21.7 45.66 C20.29 47.57 18.13 48.77 15.75 48.77 ZM15.75 48.77"/><path d="M26.66 24.92 L26.28 24.55 C24.89 23.17 24.18 21.28 24.1 19.32 C24.05 18.04 23.47 16.78 22.36 15.89 C20.7 14.58 18.3 14.59 16.65 15.9 C14.46 17.65 14.34 20.84 16.26 22.76 C17.16 23.66 18.33 24.1 19.51 24.1 C21.41 24.1 23.22 24.95 24.56 26.29 L24.94 26.67 C26.61 28.33 27.29 30.71 26.92 33.03 C26.57 35.35 27.33 37.8 29.21 39.54 C31.96 42.04 36.25 42.07 39.01 39.59 C42.16 36.77 42.26 31.93 39.31 28.97 C37.6 27.28 35.27 26.59 33.06 26.92 C30.71 27.28 28.33 26.59 26.66 24.92 ZM26.66 24.92"/><path d="M51.23 15.76 L51.23 15.22 C51.23 13.27 52.05 11.44 53.38 10 C54.25 9.06 54.73 7.76 54.57 6.34 C54.33 4.24 52.63 2.54 50.53 2.3 C47.75 2 45.39 4.16 45.39 6.88 C45.39 8.15 45.91 9.3 46.75 10.12 C48.09 11.46 48.77 13.35 48.77 15.25 L48.77 15.78 C48.77 18.13 47.58 20.29 45.69 21.69 C43.79 23.08 42.59 25.35 42.7 27.91 C42.86 31.62 45.88 34.67 49.58 34.87 C53.81 35.1 57.3 31.74 57.3 27.58 C57.3 25.16 56.14 23.03 54.34 21.7 C52.43 20.29 51.23 18.13 51.23 15.76 ZM51.23 15.76"/><path d="M75.08 26.65 L75.45 26.28 C76.83 24.89 78.72 24.18 80.68 24.1 C81.96 24.05 83.22 23.47 84.11 22.36 C85.42 20.7 85.41 18.3 84.1 16.65 C82.35 14.46 79.16 14.34 77.24 16.26 C76.34 17.16 75.9 18.33 75.9 19.51 C75.9 21.41 75.05 23.22 73.71 24.56 L73.33 24.94 C71.67 26.61 69.29 27.29 66.97 26.92 C64.65 26.57 62.18 27.33 60.46 29.21 C57.96 31.96 57.93 36.25 60.41 39.01 C63.23 42.16 68.07 42.26 71.03 39.31 C72.72 37.6 73.41 35.26 73.08 33.06 C72.72 30.71 73.41 28.33 75.08 26.65 ZM75.08 26.65"/><path d="M84.24 51.23 L84.77 51.23 C86.73 51.23 88.56 52.05 90 53.38 C90.94 54.25 92.24 54.73 93.66 54.57 C95.76 54.33 97.46 52.63 97.7 50.53 C98 47.75 95.84 45.39 93.12 45.39 C91.85 45.39 90.7 45.91 89.88 46.75 C88.52 48.09 86.65 48.77 84.75 48.77 L84.22 48.77 C81.87 48.77 79.71 47.58 78.31 45.69 C76.92 43.79 74.65 42.59 72.09 42.7 C68.38 42.86 65.33 45.89 65.13 49.58 C64.9 53.81 68.26 57.3 72.42 57.3 C74.84 57.3 76.97 56.14 78.3 54.34 C79.71 52.43 81.87 51.23 84.24 51.23 ZM84.24 51.23"/><path d="M73.34 75.08 L73.72 75.45 C75.11 76.83 75.82 78.72 75.9 80.68 C75.95 81.96 76.53 83.22 77.64 84.1 C79.29 85.42 81.7 85.41 83.35 84.1 C85.54 82.35 85.66 79.16 83.74 77.24 C82.84 76.34 81.67 75.9 80.49 75.9 C78.59 75.9 76.78 75.05 75.44 73.71 L75.06 73.33 C73.39 71.67 72.71 69.29 73.08 66.97 C73.43 64.65 72.67 62.18 70.79 60.46 C68.04 57.96 63.75 57.93 60.99 60.41 C57.84 63.23 57.74 68.07 60.69 71.03 C62.4 72.72 64.73 73.41 66.94 73.08 C69.29 72.72 71.67 73.41 73.34 75.08 ZM73.34 75.08"/></svg></span><a href="/">TODAY</a><a href="/results">RESULTS</a><a href="/connections">Connections</a><a href="/spend">Spend</a><a href="/onboard">Business</a><a href="/site/">Site</a>{nav_extra}</nav>
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

LIVE = """<div class="stepper" id="stepper">{steps}</div>
<div class="brief" id="feed">Starting…</div>
<p id="done" hidden><a class="cta" href="/">Open TODAY →</a></p>
<script>
(function(){{
  var es = new EventSource("/events/run?workers={workers}");
  var feed = document.getElementById("feed"); feed.textContent = "";
  var rows = {{}};
  function line(t){{ var d = document.createElement("div"); d.textContent = t; feed.appendChild(d); feed.scrollTop = feed.scrollHeight; return d; }}
  function mark(step, cls){{ var el = document.querySelector('[data-step="'+step+'"]'); if (el) {{ el.className = "step " + cls; }} }}
  es.onmessage = function(ev){{
    var e = JSON.parse(ev.data);
    if (e.state === "running") {{ rows[e.worker] = line("▸ " + e.label + " — running"); mark(e.step, "on"); }}
    else if (e.state === "done") {{ var r = rows[e.worker] || line(""); r.textContent = (e.ok ? "✓ " : "✗ ") + e.label + " — " + e.summary + (e.actions ? "  (+" + e.actions + " new)" : ""); mark(e.step, e.ok ? "ok" : "bad"); }}
    else if (e.state === "detail") {{
      var sig = e.signals || {{}};
      var names = {{meta_pixel: "Meta Pixel", google_ads_tag: "Google Ads tag", ga4: "GA4", booking_link: "Booking link", tel_link: "Tappable phone", local_schema: "LocalBusiness schema", canonical: "Canonical"}};
      line("   pages crawled: " + e.pages.length + (e.pages.length ? "  (" + e.pages.slice(0, 6).map(function(u){{ return u.replace(/^https?:\/\/(www\.)?/, ""); }}).join(", ") + (e.pages.length > 6 ? ", …" : "") + ")" : ""));
      var seen = Object.keys(sig).filter(function(k){{ return sig[k]; }}).map(function(k){{ return names[k] || k; }});
      line("   on the homepage: " + (seen.length ? seen.join(" · ") : "no ad tags, booking link, schema or canonical seen") + (e.phones.length ? " · phones: " + e.phones.join(", ") : ""));
      (e.passed || []).forEach(function(t){{ line("   ✓ " + t); }});
      (e.findings || []).forEach(function(f){{ line("   ✗ " + f.kind.replace(/_/g, " ") + " — " + f.url); }});
    }}
    else if (e.state === "finished") {{ line(""); line("Done: " + e.new + " new opportunit" + (e.new === 1 ? "y" : "ies") + " waiting for your decision."); document.getElementById("done").hidden = false; es.close(); }}
  }};
  es.onerror = function(){{ es.close(); }};
}})();
</script>"""

STEPS = (("connect", "Connect"), ("discover", "Discover"), ("analyse", "Analyse"), ("approve", "Approve"), ("execute", "Execute"), ("measure", "Measure"))
WORKER_STEP = {"discover": "discover", "outreach": "discover", "inbox": "discover", "seo": "analyse", "ads-audit": "analyse", "ads-live": "analyse",
               "analytics": "analyse", "content": "analyse", "monitor": "analyse", "billing": "analyse", "growth": "analyse", "measure": "measure",
               "heartbeat": "measure"}
WORKER_LABEL = {"discover": "Finding prospects", "outreach": "Drafting emails", "inbox": "Reading the mailbox", "seo": "Reading your website",
                "ads-audit": "Auditing ad exports", "ads-live": "Reading your ad accounts", "analytics": "Reading Search Console / GA4",
                "content": "Planning content", "monitor": "Scanning conversations", "billing": "Reading Stripe", "growth": "Recording external numbers",
                "measure": "Re-checking earlier actions", "heartbeat": "Deciding what to do next"}
ALL_WORKERS = ["discover", "outreach", "inbox", "seo", "ads-audit", "ads-live", "analytics", "billing", "content", "monitor", "measure", "heartbeat"]

LOGIN = """<form method="post" action="/login"><label>Panel password</label><input type="password" name="password" autofocus>
<p><button class="x">Sign in</button></p></form>"""
LOGIN_ACCOUNTS = """<form method="post" action="/login"><label>Email</label><input type="email" name="email" autofocus>
<label>Password</label><input type="password" name="password"><p><button class="x">Sign in</button></p></form>"""

_OAUTH_STATES: dict[str, dict[str, str]] = {}

CONN_ROW = """<div class="row"><div class="t"><span class="type">{state}</span><b>{label}</b><small>Reads: {reads}<br>Changes: {writes}<br>Needs: {needs}</small>{missing}</div>{forms}</div>"""


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


def make_handler(ws0: Workspace, store0: Store, ctx0: BusinessContext, *, password: str | None, accounts: Accounts | None = None):
    multi = accounts is not None and accounts.enabled()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"RevenueOS/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ── tenant binding: every request resolves to one workspace ──
        def _cookie(self, name: str) -> str | None:
            cookie = self.headers.get("Cookie", "")
            return next((c.split("=", 1)[1].strip() for c in cookie.split(";") if c.strip().startswith(name + "=")), None)

        def _tenant(self):
            cached = getattr(self, "_tenant_cache", None)
            if cached is not None:
                return cached or None
            if multi:
                email = read_session(self._cookie("rs"), _session_secret())
                tws = accounts.workspace_for(email) if email else None
                self._tenant_cache = (tws, Store(tws.db), BusinessContext.load(tws), email) if tws else ()
            else:
                self._tenant_cache = (ws0, store0, ctx0, None)
            return self._tenant_cache or None

        @property
        def ws(self) -> Workspace:
            t = self._tenant()
            return t[0] if t else ws0

        @property
        def store(self) -> Store:
            t = self._tenant()
            return t[1] if t else store0

        @property
        def ctx(self) -> BusinessContext:
            t = self._tenant()
            return t[2] if t else ctx0

        @property
        def account_email(self) -> str | None:
            t = self._tenant()
            return t[3] if t else None

        @property
        def website_dir(self):
            return self.ws.root / "website"

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
                    from .billing import pay_on_result

                    v = pay_on_result(self.ws, self.store)
                    lic = f'<span class="none">{html.escape(v["reason"] if v["tier"] == "community" else "licence: " + v["tier"])}</span>'
                except Exception:
                    lic = ""
            try:
                runs = self.store.list_runs(limit=1)
                last = runs[0] if runs else None
                n_conn = len(ConnectionStore(self.ws).all())
                chip = (f'<span class="none chip">{n_conn} connection{"s" if n_conn != 1 else ""} · '
                        f'last run {html.escape((last or {}).get("finished_at") or (last or {}).get("started_at") or "never")[:16].replace("T", " ")}'
                        f'{" · " + html.escape(str((last or {}).get("status"))) if last else ""}</span>')
            except Exception:
                chip = ""
            return PAGE.format(title=html.escape(title), sub=html.escape(sub), body=body, style=STYLE, nav_extra=chip + lic,
                               msg=f'<div class="msg">{html.escape(msg)}</div>' if msg else "")

        def _authed(self) -> bool:
            if multi:
                return self._tenant() is not None
            if not password:
                return True
            token = self._cookie("rs")
            return token_ok(token)

        def _login_page(self, sub: str, status: int = 401) -> None:
            self._send(self._page("Sign in", sub, LOGIN_ACCOUNTS if multi else LOGIN), status)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        # ── GET ──
        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path == "/health":
                self._send(json.dumps({"ok": True, "service": "revenueos-panel", "version": __version__, "onboarded": self.ctx.is_onboarded()}),
                           ctype="application/json")
                return
            if path.startswith("/site/") or path == "/site":
                self._static(path[len("/site/"):] or "index.html")
                return
            if path in ("/sitemap.xml", "/robots.txt"):  # crawlers look for these at the host root
                self._static(path[1:])
                return
            if billing_http is not None and path.startswith("/billing/"):
                res = billing_http(self.path, "GET", b"", dict(self.headers), self.ws)
                if res:
                    status, ctype, body = res
                    if status in (301, 302, 303):
                        self._redirect(body)
                    else:
                        self._send(body, status, ctype)
                    return
            if path.startswith("/connections/") and path.endswith("/callback"):
                self._oauth_callback(path.split("/")[2], parse_qs(url.query))
                return
            if not self._authed():
                self._login_page("RevenueOS control panel")
                return
            msg = parse_qs(url.query).get("msg", [""])[0]
            if path == "/api/today":
                b = build_brief(self.store, self.ws)
                self._send(json.dumps({"objective": b.objective, "counts": b.counts, "funnel": b.funnel, "pipeline_value": b.pipeline_value,
                                       "actions": b.actions, "approved": b.approved, "results": b.results, "summary": b.summary,
                                       "messages": b.messages, "offer": b.offer}, default=str), ctype="application/json")
            elif path == "/results":
                self._send(self._page("RESULTS", f"{self.ctx.company_name} — what RevenueOS did and what happened", self._results_html(), msg))
            elif path == "/onboard":
                self._send(self._page("Connect your business", "One questionnaire. Blank answers keep the current text.", self._onboard_form(), msg))
            elif path == "/run-live":
                workers = parse_qs(url.query).get("workers", ["all"])[0]
                steps = "".join(f'<span class="step {"done" if k == "connect" and self.ctx.is_onboarded() else ""}" data-step="{k}">{v}</span>' for k, v in STEPS)
                self._send(self._page("Watch it work", f"{self.ctx.company_name}: every check, as it runs", LIVE.format(steps=steps, workers=html.escape(workers)), msg))
            elif path == "/events/run":
                self._stream_run(parse_qs(url.query).get("workers", ["all"])[0])
            elif path == "/connections":
                self._send(self._page("Connections", "Accounts you authorise once. Read-only until you allow changes.", self._connections_html(), msg))
            elif path == "/spend":
                self._send(self._page("Spend", "Real numbers from the connected accounts", self._spend_html(), msg))
            elif path == "/api/connections":
                self._send(json.dumps(describe_all(ConnectionStore(self.ws)), default=str), ctype="application/json")
            elif path == "/":
                self._send(self._page("TODAY", self.ctx.company_name if self.ctx.is_onboarded() else "Not connected yet — open Business",
                                      self._today_html(), msg))
            else:
                self._send("not found", 404)

        def _static(self, rel: str) -> None:
            base = self.website_dir.resolve()
            target = (self.website_dir / rel).resolve()
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
            b = build_brief(self.store, self.ws)
            rows = "".join(
                ROW.format(
                    id=a["id"], atype=html.escape(a["action_type"].replace("_", " ")), title=html.escape(a["title"]),
                    content=html.escape((a["content"] or "")[:400]),
                    link=f'<br><a href="{html.escape(a["source_url"])}" target="_blank" rel="noopener">{html.escape(a["source_url"])}</a>' if a.get("source_url") else "",
                )
                for a in b.actions[:100]
            )
            run_form = ('<form method="get" action="/run-live"><input type="hidden" name="workers" value="all"><button class="x">Run everything now and watch</button></form> '
                        '<form method="post" action="/run/measure"><button>Measure results</button></form> '
                        + (f'<form method="post" action="/approve-all"><button>Approve all {len(b.actions)}</button></form>' if b.actions else ""))
            approved_rows = "".join(
                ROW_APPROVED.format(
                    id=a["id"], atype=html.escape(a["action_type"].replace("_", " ")), title=html.escape(a["title"]),
                    content=html.escape((a["content"] or "")[:400]),
                    link=f'<br><a href="{html.escape(a["source_url"])}" target="_blank" rel="noopener">{html.escape(a["source_url"])}</a>' if a.get("source_url") else "",
                )
                for a in b.approved[:100]
            )
            latest: dict[str, dict] = {}
            for r in self.store.list_runs(limit=300):
                latest.setdefault(r["kind"], r)
            strip = "".join(
                f'<span class="agentchip {"ok" if r.get("status") == "done" else "bad"}"><b>{html.escape(WORKER_LABEL.get(k, k))}</b> '
                f'{html.escape((r.get("finished_at") or r.get("started_at") or "")[:16].replace("T", " "))}</span>'
                for k, r in sorted(latest.items()))
            agents_html = f'<div class="agents"><span class="type">Agents · last run</span>{strip}</div>' if strip else ""
            approved_html = (f"<h2>Approved, waiting to run ({len(b.approved)})</h2>"
                             "<p class='sub'>You said yes. Nothing happens until Execute; Execute sends the email or runs the skill and the result lands in RESULTS.</p>"
                             + approved_rows) if b.approved else ""
            ol = b.offer_line()
            offer_html = f'<div class="brief note"><span class="type">Pro</span>\n{html.escape(ol)}</div>' if ol else ""
            return (self._objective_html(b) + self._messages_html(b)
                    + f'<div class="brief">{html.escape(chr(10).join(b.lines()))}</div>{offer_html}{agents_html}{run_form}'
                    "<p class='sub'>Read-only until you approve. Every line below is something observed about this business; nothing sends, publishes, changes a site or spends until you press Approve and then Execute.</p>"
                    '<h2>Approve / Execute / Ignore</h2>'
                    + (rows or "<p>Nothing pending.</p>")
                    + "<p class='sub'>Approve = mark as wanted. Execute = do it now (send / run the skill). Ignore = drop it.</p>"
                    + approved_html)

        def _objective_html(self, b) -> str:
            """What all of this is for — the objective, the next action, and the last heartbeat."""
            from .objectives import HOW_TO_ADD

            o = b.objective
            if not o:
                return f"<div class='brief note'><span class='type'>Objective</span>\n{html.escape(HOW_TO_ADD)}</div>"
            parts = [f"<span class='type'>Objective · {html.escape(o['status'])}</span>", f"<b>{html.escape(o['title'])}</b>"]
            parts.append("Next: " + html.escape(o.get("next_action") or "not decided yet — the heartbeat sets this every 30 minutes"))
            if o.get("heartbeat"):
                when = html.escape((o.get("heartbeat_at") or "")[:16].replace("T", " "))
                parts.append(f"<small>Last heartbeat {when} — {html.escape(o['heartbeat'])}</small>")
            else:
                parts.append("<small>No heartbeat yet — it runs every 30 minutes once the orchestrator is on.</small>")
            return "<div class='brief note'>" + "\n".join(parts) + "</div>"

        def _messages_html(self, b) -> str:
            if not b.messages:
                return ""
            rows = []
            for m in b.messages[:20]:
                first = (m.get("body") or "").strip().splitlines()
                rows.append(
                    '<div class="row"><div class="t"><span class="type">RevenueOS needs you</span>'
                    f"<b>{html.escape(m['subject'])}</b><small>{html.escape(first[0][:300]) if first else ''}</small></div>"
                    f'<form method="post" action="/messages/{m["id"]}/read"><button>Mark read</button></form></div>')
            return f"<h2>Inbox from RevenueOS ({len(b.messages)} unread)</h2>" + "".join(rows)

        def _results_html(self) -> str:
            b = build_brief(self.store)
            s = b.summary
            head = (f"<div class='brief'>actions: {s.get('found', 0)} found · {s.get('executed', 0)} executed · "
                    f"{s.get('measured', 0)} measured · {s.get('produced', 0)} produced (not published)\n"
                    f"{s.get('emails_sent', 0)} emails sent · {s.get('replies', 0)} replies · {s.get('bounces', 0)} bounces · {s.get('booked', 0)} booked\n"
                    f"${s.get('pipeline_value', 0):,.0f} pipeline</div>")
            ext = {k.removeprefix("growth_"): v for k, v in b.metrics.items() if k.startswith("growth_")}
            if ext:
                head += "<div class='brief'>EXTERNAL (real numbers, last growth run)\n" + "\n".join(f"{k:<28} {v:g}" for k, v in sorted(ext.items())) + "</div>"
            rows = []
            for a in b.results[:200]:
                o = a.get("outcome") or {}
                st = o.get("status") or "pending"
                cls = {"measured": "ok", "produced": "pend", "pending": "pend"}.get(st, "none")
                if o.get("metric") is not None and o.get("after_value") is not None:
                    what = f"{o['metric']}: {o.get('before_value') or 0:g} → {o['after_value']:g}" + (f" — {o['note']}" if o.get("note") else "")
                else:
                    what = o.get("note") or "awaiting measurement"
                rows.append(f"<tr><td>{a['id']}</td><td>{html.escape(a['action_type'].replace('_', ' '))}</td><td>{html.escape(a['title'][:80])}</td>"
                            f"<td>{html.escape(a.get('executed_at') or '')[:16]}</td><td class='{cls}'>{html.escape(st)}</td><td>{html.escape(what)}</td></tr>")
            table = ("<table><tr><th>#</th><th>What it found</th><th>What it did</th><th>When</th><th>Result</th><th>Measured</th></tr>"
                     + "".join(rows) + "</table>") if rows else "<p>No executed actions yet.</p>"
            return head + "<h2>Every executed action</h2>" + table

        def _stream_run(self, which: str) -> None:
            """Server-Sent Events: one line per worker start and finish, then a finished event. Runs the workers in
            this request thread (ThreadingHTTPServer gives each request its own) and flushes after every event."""
            names = ALL_WORKERS if which == "all" else [w for w in which.split(",") if w in WORKER_LABEL]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(obj: dict) -> None:
                try:
                    self.wfile.write(f"data: {json.dumps(obj, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    raise
            ws, store, ctx = self.ws, self.store, self.ctx
            llm = maybe_llm()
            new = 0
            try:
                for n in names:
                    emit({"state": "running", "worker": n, "label": WORKER_LABEL.get(n, n), "step": WORKER_STEP.get(n, "analyse")})
                    r = run_worker(n, ws, store, ctx, llm, "panel-live")
                    new += r.actions_created
                    emit({"state": "done", "worker": n, "label": WORKER_LABEL.get(n, n), "step": WORKER_STEP.get(n, "analyse"), "ok": r.ok,
                          "summary": (r.summary or r.error or "")[:300], "actions": r.actions_created})
                    if n == "seo" and r.details.get("urls") is not None:
                        d = r.details
                        emit({"state": "detail", "worker": n, "pages": d.get("urls", []), "signals": d.get("signals", {}), "phones": d.get("phones", []),
                              "findings": d.get("findings", []), "passed": d.get("passed", [])})
                emit({"state": "finished", "new": new})
            except (BrokenPipeError, ConnectionResetError):
                return

        def _connections_html(self) -> str:
            cs = ConnectionStore(self.ws)
            rows = []
            for d in describe_all(cs):
                name = d["name"]
                if d["connected"]:
                    state = f"connected · {html.escape(str(d['account']))} · {'changes allowed' if d['allow_write'] else 'read-only'}"
                    toggle = ("off" if d["allow_write"] else "on")
                    forms = (f'<form method="post" action="/connections/{name}/write"><input type="hidden" name="allow" value="{toggle}">'
                             f'<button class="{"" if d["allow_write"] else "x"}">{"Make read-only" if d["allow_write"] else "Allow changes"}</button></form>'
                             f'<form method="post" action="/connections/{name}/disconnect"><button>Disconnect</button></form>')
                else:
                    state = "not connected"
                    forms = self._connect_form(name, d)
                missing = f"<small>Missing: {html.escape('; '.join(d['missing']))}</small>" if d["missing"] and not d["connected"] else ""
                rows.append(CONN_ROW.format(state=state, label=html.escape(d["label"]), reads=html.escape(d["reads"]), writes=html.escape(d["writes"]),
                                            needs=html.escape(d["needs"]), missing=missing, forms=forms))
            sealed = "sealed with REVENUEOS_TOKEN_KEY" if cs.sealed() else "stored in data/connections.json (mode 600); set REVENUEOS_TOKEN_KEY to seal them"
            return (f"<p class='sub'>Tokens are {sealed}. A connection is read-only until you allow changes; every change still waits for your approval on TODAY.</p>"
                    + "".join(rows))

        def _connect_form(self, name: str, d: dict) -> str:
            a = f'/connections/{name}'
            if name == "stripe":
                return f'<form method="post" action="{a}"><input name="key" placeholder="sk_live_… or rk_…" required><button class="x">Connect</button></form>'
            if name == "github_site":
                return (f'<form method="post" action="{a}"><input name="repo" placeholder="owner/name" required><input name="path" placeholder="site dir (optional)">'
                        f'<input name="branch" placeholder="main"><button class="x">Connect</button></form>')
            if name == "wordpress":
                return (f'<form method="post" action="{a}"><input name="site" placeholder="https://example.com" required><input name="user" placeholder="user" required>'
                        f'<input name="app_password" placeholder="application password" required><button class="x">Connect</button></form>')
            if name in ("google", "meta"):
                ready = d.get("ready")
                return (f'<form method="post" action="{a}"><button class="x" {"" if ready else "disabled"}>Connect {html.escape(d["label"].split(" (")[0])}</button></form>'
                        + ("" if ready else "<small>set the OAuth client credentials first</small>"))
            return "<small>connected automatically with the mailbox</small>"

        def _connections_post(self, name: str, verb: str, form: dict) -> None:
            from .connections import github_site, stripe_conn, wordpress

            cs = ConnectionStore(self.ws)
            f = {k: v[0].strip() for k, v in form.items() if v}
            try:
                if verb == "write":
                    cs.set_allow_write(name, f.get("allow") == "on")
                    msg = f"{name}: changes {'allowed' if f.get('allow') == 'on' else 'no longer allowed'}"
                elif verb == "disconnect":
                    cs.remove(name)
                    msg = f"{name} disconnected"
                elif name == "stripe":
                    c = stripe_conn.connect(cs, f.get("key"))
                    msg = f"Stripe connected as {c.account} ({c.meta.get('business') or ''})"
                elif name == "github_site":
                    c = github_site.connect(cs, f.get("repo", ""), f.get("path", ""), f.get("branch") or "main")
                    msg = f"site repo connected: {c.account}"
                elif name == "wordpress":
                    c = wordpress.connect(cs, f.get("site", ""), f.get("user", ""), f.get("app_password", ""))
                    msg = f"WordPress connected: {c.account}"
                elif name in ("google", "meta"):
                    self._oauth_start(name)
                    return
                else:
                    msg = f"unknown provider {name}"
            except Exception as exc:
                msg = f"{name}: not connected — {exc}"
            self._redirect("/connections?msg=" + quote(msg[:400]))

        def _oauth_start(self, name: str) -> None:
            import secrets as _secrets

            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
            scheme = self.headers.get("X-Forwarded-Proto") or ("https" if not host.startswith(("localhost", "127.")) else "http")
            redirect_uri = f"{scheme}://{host}/connections/{name}/callback"
            state = _secrets.token_urlsafe(16)
            wanted = ["identity", "searchconsole.read", "analytics.read", "calendar.write"] if name == "google" else ["identity", "ads.read"]
            verifier, challenge = oauth._pkce() if oauth.PROVIDERS[name].get("pkce", True) else (None, None)
            _OAUTH_STATES[state] = {"provider": name, "verifier": verifier or "", "redirect_uri": redirect_uri, "ws": str(self.ws.root), "wanted": ",".join(wanted)}
            self._redirect(oauth.authorize_url(name, redirect_uri, wanted, state, challenge))

        def _oauth_callback(self, name: str, q: dict) -> None:
            from .connections import google, meta

            st = _OAUTH_STATES.pop((q.get("state") or [""])[0], None)
            if not st or st["provider"] != name:
                self._send("bad state", 400)
                return
            if "error" in q:
                self._redirect("/connections?msg=" + quote(f"{name}: {q['error'][0]}"))
                return
            tws = Workspace(__import__("pathlib").Path(st["ws"]))
            cs = ConnectionStore(tws)
            try:
                toks = oauth.exchange_code(name, q["code"][0], st["redirect_uri"], st["verifier"] or None)
                c = (google if name == "google" else meta).connect(cs, st["wanted"].split(","), tokens=toks)
                msg = f"{name} connected as {c.account}"
            except Exception as exc:
                msg = f"{name}: not connected — {exc}"
            self._redirect("/connections?msg=" + quote(msg[:400]))

        def _spend_html(self) -> str:
            m = self.store.latest_metrics()
            cs = ConnectionStore(self.ws)
            lines = []
            st = {k.removeprefix("stripe_"): v for k, v in m.items() if k.startswith("stripe_")}
            if st:
                lines.append("REVENUE (Stripe, last billing run)\n" + "\n".join(f"{k:<24} {v:g}" for k, v in sorted(st.items())))
            else:
                lines.append("REVENUE: Stripe not connected" if not cs.get("stripe") else "REVENUE: run the billing check")
            ads = {k: v for k, v in m.items() if k.endswith("_spend_28d")}
            lines.append(("AD SPEND (28 days)\n" + "\n".join(f"{k:<24} {v:g}" for k, v in sorted(ads.items()))) if ads else "AD SPEND: no ad account connected")
            b = build_brief(self.store)
            lines.append(f"PIPELINE  ${b.pipeline_value:,.0f} (deal values on open leads)")
            run = ('<form method="post" action="/run/billing"><button>Read Stripe now</button></form> '
                   '<form method="post" action="/run/ads-live"><button>Read ad accounts now</button></form> '
                   '<form method="post" action="/run/analytics"><button>Read Search Console / GA4 now</button></form>')
            return f'<div class="brief">{html.escape(chr(10).join(lines))}</div>{run}'

        def _onboard_form(self) -> str:
            once = ('<form method="post" action="/onboard" class="stack once"><label>Connect once: your website</label>'
                    '<input name="from_url" placeholder="https://yourbusiness.com" required>'
                    '<p><button class="x">Read my site and fill this in</button></p>'
                    "<p class='sub'>RevenueOS reads the site and writes the answers below; every inference is labelled. Correct anything afterwards.</p></form>")
            return once + self._onboard_form_full()

        def _onboard_form_full(self) -> str:
            cur = {"company_name": self.ctx.company_name if self.ctx.is_onboarded() else "", "website": self.ctx.website or "",
                   "competitors": ", ".join(self.ctx.competitors), "channels": ", ".join(self.ctx.channels),
                   "sender_name": (self.ctx.config.get("sender") or {}).get("name", ""), "sender_email": (self.ctx.config.get("sender") or {}).get("email", ""),
                   "booking_url": self.ctx.config.get("booking_url") or ""}
            fields = []
            for key, prompt, target in QUESTIONS:
                value = cur.get(key) or (self.ctx.section(*target) if target and self.ctx.is_onboarded() else "")
                if target and value and value.split()[0] in ("Write", "State", "List", "Describe", "Name", "Include", "Define", "Explain", "Replace"):
                    value = ""
                tag = "textarea" if target and key in ("pain_points", "differentiators", "primary_audience") else "input"
                fields.append(f"<label>{html.escape(prompt)}</label>" + (
                    f'<textarea name="{key}" rows="3">{html.escape(value)}</textarea>' if tag == "textarea"
                    else f'<input name="{key}" value="{html.escape(value)}">'))
            return '<form method="post" action="/onboard" class="stack">' + "".join(fields) + '<p><button class="x">Save and connect</button></p></form>'

        # ── POST ──
        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._body()
            if billing_http is not None and path.startswith("/billing/"):
                res = billing_http(self.path, "POST", body, dict(self.headers), self.ws)
                if res:
                    status, ctype, out = res
                    self._send(out, status, ctype)
                    return
            if path == "/login":
                form = parse_qs(body.decode("utf-8", "replace"))
                if multi:
                    email = form.get("email", [""])[0]
                    if accounts.verify(email, form.get("password", [""])[0]):
                        self._redirect("/", {"Set-Cookie": f"rs={make_session(email.strip().lower(), _session_secret())}; HttpOnly; SameSite=Lax; Path=/"})
                    else:
                        self._login_page("Wrong email or password")
                elif password and hmac.compare_digest(form.get("password", [""])[0], password):
                    self._redirect("/", {"Set-Cookie": f"rs={make_token()}; HttpOnly; SameSite=Lax; Path=/"})
                else:
                    self._login_page("Wrong password")
                return
            if path == "/logout":
                self._redirect("/", {"Set-Cookie": "rs=; Max-Age=0; Path=/"})
                return
            if not self._authed():
                self._send("unauthorized", 401)
                return
            if path == "/onboard":
                form = {k: v[0].strip() for k, v in parse_qs(body.decode("utf-8", "replace")).items() if v and v[0].strip()}
                if form.get("from_url"):
                    from .onboard import derive_answers

                    answers, facts = derive_answers(form["from_url"], maybe_llm())
                    if not answers:
                        self._redirect("/onboard?msg=" + quote(f"could not read {form['from_url']}: {facts.get('error')}"))
                        return
                    form = {**answers, **{k: v for k, v in form.items() if k != "from_url"}}
                if not form:
                    self._redirect("/onboard?msg=" + quote("nothing to save"))
                    return
                self.ctx.onboard(form)
                errors = self.ctx.validate()
                self._redirect("/?msg=" + quote(("Connected " + form.get("company_name", "")) if not errors else "Saved with validation errors: " + "; ".join(errors)[:300]))
                return
            parts = path.strip("/").split("/")
            if parts[0] == "connections" and len(parts) in (2, 3):
                self._connections_post(parts[1], parts[2] if len(parts) == 3 else "connect", parse_qs(body.decode("utf-8", "replace")))
                return
            if path == "/approve-all":
                n = 0
                for a in self.store.list_actions("pending"):
                    self.store.set_action_status(a["id"], "approved")
                    if a["context"].get("draft_id"):
                        self.store.set_draft_approval(a["context"]["draft_id"], "approved", by="panel")
                    n += 1
                self._redirect("/?msg=" + quote(f"Approved {n} action(s). Nothing runs until you press Execute on each, or Run everything and watch."))
                return
            if len(parts) == 2 and parts[0] == "run":
                names = ["discover", "outreach", "inbox", "seo", "ads-audit", "content", "monitor", "measure", "heartbeat"] if parts[1] == "all" else [parts[1]]
                llm = maybe_llm()
                results = [run_worker(n, self.ws, self.store, self.ctx, llm, "panel") for n in names]
                new = sum(r.actions_created for r in results)
                lines = [f"Ran {len(names)} check(s): {new} new opportunit{'y' if new == 1 else 'ies'}."]
                lines += [f"{n}: {r.summary or ('failed: ' + (r.error or ''))}" for n, r in zip(names, results, strict=True)]
                self._redirect("/?msg=" + quote("\n".join(lines)[:1200]))
                return
            if len(parts) == 3 and parts[0] == "messages" and parts[2] == "read" and parts[1].isdigit():
                self.store.mark_read(int(parts[1]))
                self._redirect("/?msg=" + quote("Message marked read."))
                return
            if len(parts) == 3 and parts[0] == "action" and parts[2] in ("approve", "execute", "ignore"):
                aid, verb = int(parts[1]), parts[2]
                action = self.store.get_action(aid)
                if not action:
                    self._send("no such action", 404)
                    return
                try:
                    if verb == "ignore":
                        self.store.set_action_status(aid, "ignored")
                        msg = f"Ignored: {action['title']}"
                    elif verb == "approve":
                        self.store.set_action_status(aid, "approved")
                        if action["context"].get("draft_id"):
                            self.store.set_draft_approval(action["context"]["draft_id"], "approved", by="panel")
                        msg = f"Approved: {action['title']}"
                    else:
                        outcome = execute_action(self.ws, self.store, self.ctx, maybe_llm(), action)
                        if refused(outcome):
                            msg = f"Not executed: {action['title']} — {outcome}"
                        else:
                            self.store.set_action_status(aid, "executed")
                            self.store.record_outcome(aid, "pending", note=f"executed: {outcome[:200]}")
                            msg = f"Executed: {action['title']} — {outcome}"
                except Exception as exc:
                    self.store.set_action_status(aid, "failed")
                    msg = f"Failed: {type(exc).__name__}: {exc}"
                self._redirect("/?msg=" + quote(msg[:900]))
                return
            self._send("not found", 404)

    return Handler


def serve(ws: Workspace, store: Store, ctx: BusinessContext, host: str = "127.0.0.1", port: int = 8791) -> int:
    password = os.environ.get("REVENUEOS_PANEL_PASSWORD") or None
    accounts = Accounts(ws.root)
    multi = accounts.enabled()
    if host not in ("127.0.0.1", "localhost", "::1") and not password and not multi:
        print("refusing to bind a non-loopback address without REVENUEOS_PANEL_PASSWORD or accounts", flush=True)
        return 2
    server = ThreadingHTTPServer((host, port), make_handler(ws, store, ctx, password=password, accounts=accounts if multi else None))
    mode = f"accounts ({len(accounts.list())} tenants)" if multi else ("password" if password else "none — localhost only")
    print(f"RevenueOS panel on http://{host}:{port}  (auth: {mode}; Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
