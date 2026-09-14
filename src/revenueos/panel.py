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
/* the owner's mark, as supplied, inheriting the text colour: frost here, ink under
   data-theme="light". */
nav .brand{display:inline-flex;align-items:center;margin-right:var(--sp-3);color:var(--ink)}
nav .brand svg{width:23px;height:23px;display:block}
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
<nav><span class="brand" aria-hidden="true"><svg viewBox="0 0 100 100" fill="currentColor"><g><path d="M49.03 77.06 L49.03 77.48 C49.03 79.03 48.38 80.48 47.33 81.62 C46.64 82.36 46.26 83.39 46.39 84.51 C46.58 86.17 47.92 87.51 49.58 87.7 C51.78 87.94 53.63 86.23 53.63 84.08 C53.63 83.08 53.23 82.17 52.57 81.52 C51.51 80.46 50.97 78.97 50.97 77.47 L50.97 77.05 C50.97 75.19 51.91 73.48 53.41 72.38 C54.91 71.28 55.86 69.48 55.77 67.46 C55.64 64.53 53.25 62.12 50.32 61.96 C46.99 61.78 44.23 64.43 44.23 67.72 C44.23 69.63 45.15 71.32 46.57 72.37 C48.08 73.48 49.03 75.19 49.03 77.06 ZM49.03 77.06"/><path d="M30.18 68.45 L29.88 68.75 C28.79 69.85 27.3 70.41 25.75 70.47 C24.74 70.51 23.74 70.97 23.04 71.85 C22 73.15 22.01 75.06 23.05 76.36 C24.43 78.08 26.95 78.19 28.47 76.67 C29.18 75.96 29.53 75.03 29.53 74.1 C29.53 72.6 30.2 71.17 31.26 70.11 L31.56 69.81 C32.87 68.49 34.75 67.95 36.59 68.24 C38.42 68.52 40.37 67.92 41.73 66.43 C43.71 64.26 43.73 60.87 41.77 58.69 C39.54 56.2 35.71 56.12 33.38 58.45 C32.04 59.8 31.5 61.64 31.76 63.39 C32.04 65.25 31.5 67.13 30.18 68.45 ZM30.18 68.45"/><path d="M22.93 49.03 L22.51 49.03 C20.97 49.03 19.52 48.38 18.38 47.33 C17.64 46.64 16.61 46.26 15.49 46.39 C13.83 46.58 12.49 47.92 12.3 49.58 C12.06 51.78 13.77 53.64 15.91 53.64 C16.92 53.64 17.83 53.23 18.48 52.57 C19.55 51.51 21.03 50.97 22.53 50.97 L22.95 50.97 C24.81 50.97 26.52 51.91 27.62 53.41 C28.72 54.91 30.52 55.86 32.54 55.77 C35.47 55.64 37.88 53.25 38.04 50.33 C38.22 46.99 35.57 44.22 32.28 44.22 C30.37 44.22 28.68 45.15 27.63 46.57 C26.52 48.08 24.81 49.03 22.93 49.03 ZM22.93 49.03"/><path d="M31.55 30.18 L31.25 29.88 C30.15 28.79 29.59 27.3 29.53 25.75 C29.49 24.74 29.03 23.74 28.15 23.04 C26.84 22 24.94 22.01 23.64 23.05 C21.91 24.43 21.81 26.95 23.33 28.47 C24.04 29.18 24.97 29.53 25.9 29.53 C27.4 29.53 28.83 30.2 29.89 31.26 L30.19 31.56 C31.51 32.87 32.05 34.75 31.76 36.59 C31.48 38.42 32.08 40.36 33.57 41.73 C35.74 43.71 39.13 43.73 41.31 41.77 C43.8 39.54 43.88 35.72 41.55 33.38 C40.2 32.04 38.36 31.5 36.61 31.76 C34.75 32.04 32.87 31.5 31.55 30.18 ZM31.55 30.18"/><path d="M50.97 22.94 L50.97 22.51 C50.97 20.97 51.62 19.52 52.67 18.38 C53.36 17.64 53.74 16.61 53.61 15.49 C53.42 13.83 52.08 12.49 50.42 12.3 C48.22 12.06 46.36 13.77 46.36 15.92 C46.36 16.92 46.77 17.83 47.43 18.48 C48.49 19.54 49.03 21.03 49.03 22.53 L49.03 22.95 C49.03 24.81 48.09 26.52 46.59 27.62 C45.09 28.72 44.14 30.52 44.23 32.54 C44.36 35.47 46.74 37.88 49.67 38.04 C53.01 38.22 55.77 35.57 55.77 32.28 C55.77 30.37 54.85 28.68 53.43 27.63 C51.92 26.52 50.97 24.81 50.97 22.94 ZM50.97 22.94"/><path d="M69.82 31.54 L70.12 31.25 C71.21 30.15 72.7 29.59 74.25 29.53 C75.26 29.49 76.26 29.03 76.96 28.15 C78 26.84 77.99 24.94 76.95 23.64 C75.57 21.91 73.05 21.81 71.53 23.33 C70.82 24.04 70.47 24.97 70.47 25.9 C70.47 27.4 69.8 28.83 68.74 29.89 L68.44 30.19 C67.13 31.51 65.25 32.05 63.41 31.76 C61.58 31.48 59.63 32.08 58.27 33.57 C56.29 35.74 56.27 39.13 58.23 41.31 C60.46 43.8 64.28 43.88 66.62 41.55 C67.96 40.2 68.5 38.35 68.24 36.61 C67.96 34.75 68.5 32.87 69.82 31.54 ZM69.82 31.54"/><path d="M77.06 50.97 L77.48 50.97 C79.03 50.97 80.48 51.62 81.62 52.67 C82.36 53.36 83.39 53.74 84.51 53.61 C86.17 53.42 87.51 52.08 87.7 50.42 C87.94 48.22 86.23 46.36 84.08 46.36 C83.08 46.36 82.17 46.77 81.52 47.43 C80.45 48.49 78.97 49.03 77.47 49.03 L77.05 49.03 C75.19 49.03 73.48 48.09 72.38 46.59 C71.28 45.09 69.48 44.14 67.46 44.23 C64.53 44.36 62.12 46.75 61.96 49.67 C61.78 53.01 64.43 55.77 67.72 55.77 C69.63 55.77 71.32 54.85 72.37 53.43 C73.48 51.92 75.19 50.97 77.06 50.97 ZM77.06 50.97"/><path d="M68.45 69.82 L68.75 70.12 C69.85 71.21 70.41 72.7 70.47 74.25 C70.51 75.26 70.97 76.26 71.85 76.95 C73.15 78 75.06 77.99 76.36 76.95 C78.09 75.57 78.19 73.05 76.67 71.53 C75.96 70.82 75.03 70.47 74.1 70.47 C72.6 70.47 71.17 69.8 70.11 68.74 L69.81 68.44 C68.49 67.13 67.95 65.25 68.24 63.41 C68.52 61.58 67.92 59.63 66.43 58.27 C64.26 56.29 60.87 56.27 58.69 58.23 C56.2 60.46 56.12 64.28 58.45 66.62 C59.8 67.96 61.64 68.5 63.39 68.24 C65.25 67.96 67.13 68.5 68.45 69.82 ZM68.45 69.82"/><path d="M14.88 34.14 L14.5 33.97 C13.08 33.36 12 32.19 11.37 30.77 C10.96 29.85 10.17 29.09 9.09 28.77 C7.49 28.28 5.73 28.99 4.9 30.44 C3.81 32.36 4.65 34.75 6.62 35.59 C7.54 35.99 8.53 35.97 9.4 35.63 C10.79 35.07 12.37 35.16 13.75 35.75 L14.13 35.92 C15.85 36.65 17.04 38.19 17.46 40.01 C17.88 41.82 19.16 43.4 21.05 44.12 C23.79 45.16 26.95 43.92 28.25 41.29 C29.74 38.3 28.39 34.71 25.37 33.41 C23.61 32.66 21.7 32.84 20.17 33.73 C18.55 34.68 16.61 34.88 14.88 34.14 ZM14.88 34.14"/><path d="M36.39 13.95 L36.23 13.56 C35.66 12.12 35.72 10.54 36.28 9.09 C36.64 8.15 36.62 7.05 36.09 6.06 C35.3 4.59 33.55 3.84 31.94 4.28 C29.81 4.87 28.72 7.14 29.51 9.14 C29.88 10.07 30.6 10.76 31.45 11.13 C32.83 11.72 33.88 12.9 34.44 14.29 L34.6 14.68 C35.29 16.41 35.04 18.35 34.06 19.93 C33.07 21.5 32.86 23.53 33.69 25.37 C34.89 28.04 38 29.4 40.78 28.46 C43.95 27.4 45.53 23.91 44.31 20.85 C43.61 19.08 42.12 17.85 40.41 17.41 C38.6 16.93 37.08 15.69 36.39 13.95 ZM36.39 13.95"/><path d="M65.86 14.88 L66.03 14.5 C66.64 13.07 67.81 12 69.23 11.37 C70.15 10.96 70.91 10.17 71.23 9.09 C71.71 7.49 71.01 5.73 69.56 4.9 C67.64 3.81 65.25 4.65 64.41 6.62 C64.01 7.54 64.03 8.53 64.37 9.4 C64.93 10.79 64.84 12.37 64.25 13.75 L64.08 14.13 C63.34 15.85 61.81 17.04 59.99 17.46 C58.18 17.88 56.6 19.16 55.88 21.05 C54.84 23.79 56.08 26.95 58.71 28.25 C61.7 29.74 65.29 28.39 66.59 25.37 C67.34 23.61 67.16 21.7 66.26 20.17 C65.32 18.55 65.12 16.61 65.86 14.88 ZM65.86 14.88"/><path d="M86.05 36.39 L86.44 36.23 C87.87 35.66 89.46 35.72 90.91 36.28 C91.85 36.65 92.95 36.62 93.94 36.09 C95.41 35.3 96.16 33.55 95.72 31.94 C95.13 29.81 92.85 28.72 90.86 29.51 C89.93 29.88 89.24 30.6 88.87 31.45 C88.28 32.83 87.1 33.88 85.7 34.44 L85.32 34.59 C83.59 35.29 81.65 35.04 80.07 34.06 C78.5 33.07 76.47 32.86 74.63 33.69 C71.96 34.89 70.6 38 71.54 40.78 C72.6 43.95 76.09 45.53 79.15 44.31 C80.92 43.61 82.14 42.12 82.59 40.41 C83.07 38.6 84.31 37.08 86.05 36.39 ZM86.05 36.39"/><path d="M85.12 65.86 L85.5 66.03 C86.92 66.64 88 67.81 88.63 69.23 C89.04 70.15 89.83 70.91 90.91 71.23 C92.51 71.72 94.27 71.01 95.1 69.56 C96.19 67.64 95.35 65.25 93.38 64.41 C92.46 64.01 91.46 64.02 90.6 64.37 C89.21 64.93 87.63 64.84 86.25 64.25 L85.86 64.08 C84.15 63.34 82.95 61.8 82.54 59.99 C82.12 58.18 80.84 56.6 78.95 55.88 C76.21 54.84 73.05 56.08 71.75 58.71 C70.26 61.7 71.6 65.29 74.63 66.59 C76.38 67.34 78.3 67.16 79.83 66.27 C81.45 65.32 83.39 65.12 85.12 65.86 ZM85.12 65.86"/><path d="M63.61 86.05 L63.77 86.44 C64.34 87.88 64.27 89.46 63.72 90.91 C63.35 91.85 63.38 92.95 63.91 93.94 C64.7 95.41 66.45 96.16 68.06 95.72 C70.19 95.13 71.28 92.86 70.49 90.86 C70.12 89.93 69.4 89.24 68.55 88.87 C67.17 88.28 66.12 87.1 65.56 85.71 L65.4 85.32 C64.71 83.58 64.95 81.65 65.94 80.07 C66.93 78.5 67.14 76.47 66.31 74.63 C65.11 71.96 62 70.6 59.22 71.53 C56.05 72.6 54.47 76.09 55.69 79.15 C56.39 80.92 57.88 82.15 59.59 82.59 C61.4 83.07 62.92 84.31 63.61 86.05 ZM63.61 86.05"/><path d="M34.14 85.11 L33.97 85.5 C33.36 86.92 32.19 88 30.77 88.63 C29.85 89.04 29.09 89.83 28.77 90.91 C28.28 92.51 28.99 94.27 30.44 95.1 C32.36 96.19 34.75 95.35 35.59 93.38 C35.99 92.46 35.97 91.46 35.63 90.6 C35.07 89.21 35.16 87.63 35.75 86.25 L35.92 85.86 C36.66 84.15 38.19 82.96 40.01 82.54 C41.82 82.12 43.4 80.84 44.12 78.95 C45.16 76.21 43.92 73.05 41.29 71.75 C38.3 70.26 34.71 71.61 33.41 74.63 C32.66 76.39 32.84 78.3 33.73 79.83 C34.68 81.45 34.88 83.39 34.14 85.11 ZM34.14 85.11"/><path d="M13.95 63.61 L13.56 63.77 C12.12 64.34 10.54 64.28 9.09 63.72 C8.15 63.35 7.05 63.38 6.06 63.91 C4.59 64.7 3.84 66.45 4.28 68.06 C4.87 70.19 7.14 71.28 9.14 70.49 C10.07 70.12 10.76 69.4 11.13 68.55 C11.72 67.16 12.9 66.12 14.29 65.56 L14.68 65.4 C16.41 64.71 18.35 64.95 19.93 65.94 C21.5 66.93 23.52 67.14 25.37 66.31 C28.04 65.11 29.4 62 28.46 59.22 C27.4 56.05 23.91 54.47 20.85 55.69 C19.08 56.39 17.85 57.88 17.41 59.59 C16.93 61.4 15.69 62.92 13.95 63.61 ZM13.95 63.61"/><path d="M20.94 17.4 C20.94 19.35 19.35 20.94 17.39 20.94 C15.44 20.94 13.85 19.35 13.85 17.4 C13.85 15.44 15.44 13.85 17.39 13.85 C19.35 13.85 20.94 15.44 20.94 17.4 ZM20.94 17.4"/><path d="M6.4 47.5 C7.78 48.88 7.78 51.12 6.4 52.51 C5.01 53.89 2.77 53.89 1.38 52.51 C0 51.12 0 48.88 1.38 47.5 C2.77 46.11 5.01 46.11 6.4 47.5 ZM6.4 47.5"/><path d="M17.4 79.06 C19.35 79.06 20.94 80.65 20.94 82.61 C20.94 84.56 19.35 86.15 17.4 86.15 C15.44 86.15 13.85 84.56 13.85 82.61 C13.85 80.65 15.44 79.06 17.4 79.06 ZM17.4 79.06"/><path d="M47.5 93.6 C48.88 92.22 51.12 92.22 52.51 93.6 C53.89 94.99 53.89 97.23 52.51 98.62 C51.12 100 48.88 100 47.5 98.62 C46.11 97.23 46.11 94.99 47.5 93.6 ZM47.5 93.6"/><path d="M79.06 82.6 C79.06 80.65 80.65 79.06 82.61 79.06 C84.56 79.06 86.15 80.65 86.15 82.6 C86.15 84.56 84.56 86.15 82.61 86.15 C80.65 86.15 79.06 84.56 79.06 82.6 ZM79.06 82.6"/><path d="M93.6 52.5 C92.22 51.12 92.22 48.88 93.6 47.49 C94.99 46.11 97.23 46.11 98.62 47.49 C100 48.88 100 51.12 98.62 52.5 C97.23 53.89 94.99 53.89 93.6 52.5 ZM93.6 52.5"/><path d="M82.6 20.94 C80.65 20.94 79.06 19.35 79.06 17.39 C79.06 15.44 80.65 13.85 82.6 13.85 C84.56 13.85 86.15 15.44 86.15 17.39 C86.15 19.35 84.56 20.94 82.6 20.94 ZM82.6 20.94"/><path d="M52.5 6.4 C51.12 7.78 48.88 7.78 47.49 6.4 C46.11 5.01 46.11 2.77 47.49 1.38 C48.88 0 51.12 0 52.5 1.38 C53.89 2.77 53.89 5.01 52.5 6.4 ZM52.5 6.4"/></g></svg></span><a href="/">TODAY</a><a href="/results">RESULTS</a><a href="/connections">Connections</a><a href="/spend">Spend</a><a href="/onboard">Business</a><a href="/site/">Site</a>{nav_extra}</nav>
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
