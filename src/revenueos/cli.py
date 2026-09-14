"""`revenueos` — the one command the customer touches.

    revenueos init                 answer the questionnaire (or --answers file.json)
    revenueos today                the brief
    revenueos run <worker|all>     run a department worker now (--json for the orchestrator)
    revenueos approve|execute|ignore <id>
    revenueos skills search <q>    the unified catalogue
    revenueos serve                the control panel on http://127.0.0.1:8791
    revenueos doctor               what is connected, what is missing
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .context import QUESTIONS, BusinessContext
from .llm import LLM, chain_status, maybe_llm
from .paths import Workspace, is_workspace
from .registry import build_registry, load_registry, search_skills
from .store import Store
from .today import build_brief
from .workers import all_workers, execute_action, refused, run_worker


def _boot(args: argparse.Namespace) -> tuple[Workspace, Store, BusinessContext]:
    root = getattr(args, "root", None)
    if root and not is_workspace(Path(root)):
        # an explicit --root must be a workspace; silently falling back to ~/.revenueos would
        # write the customer's answers and leads somewhere they did not point at
        raise SystemExit(f"{root} is not a RevenueOS workspace (no company-context/). Create one: revenueos workspace new {root}")
    ws = Workspace.locate(Path(root) if root else None)
    return ws, Store(ws.db), BusinessContext.load(ws)


# ── commands ────────────────────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> int:
    ws, store, ctx = _boot(args)
    answers: dict[str, str] = {}
    if getattr(args, "from_url", None):
        from .onboard import derive_answers

        answers, facts = derive_answers(args.from_url, None if args.no_llm else maybe_llm())
        if not answers:
            print(f"could not read {args.from_url}: {facts.get('error')}", file=sys.stderr)
            return 2
        print(f"Read {facts['url']}: {len(facts.get('pages', {})) + 1} page(s). Answers written from the site" + (" and a model" if not args.no_llm and maybe_llm() else "") + "; correct any of them on the Business page.")
    elif args.answers:
        answers = json.loads(Path(args.answers).read_text())
    else:
        print("Connect your business. Answer what you can; blank keeps the template text.\n")
        for key, prompt, _ in QUESTIONS:
            try:
                val = input(f"{prompt}: ").strip()
            except EOFError:
                val = ""
            if val:
                answers[key] = val
    if not answers:
        print("no answers given", file=sys.stderr)
        return 2
    ctx.onboard(answers)
    errors = ctx.validate()
    build_registry(ws)
    if answers.get("objective"):
        from .objectives import ensure_objective

        oid = ensure_objective(store, answers["objective"])
        print(f"Objective [{oid}] set: {answers['objective']}" if oid else "Objective already set (unchanged).")
    print(f"Onboarded {ctx.company_name}. Config: {ws.config.relative_to(ws.root)}; canon: company-context/.")
    if errors:
        print("company-context validation:", *errors, sep="\n  ")
        return 1
    print("company-context validates (canon gate). Now: `revenueos run all` then `revenueos today`.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """`revenueos demo https://yoursite` — one command, no setup: read the site, list what is costing it
    customers, say which fixes RevenueOS would deploy once connected. Nothing is stored."""
    import tempfile

    from .onboard import derive_answers
    from .paths import Workspace as _W
    from .paths import new_workspace
    from .workers.seo import crawl_findings, deployable_fix, homepage_signals, signal_findings

    answers, facts = derive_answers(args.url, None if args.no_llm else maybe_llm())
    if not answers:
        print(f"could not read {args.url}: {facts.get('error')}", file=sys.stderr)
        return 2
    site = facts["url"]
    sig = homepage_signals(site)
    findings, crawl = crawl_findings(site)
    findings += signal_findings(site, sig)
    name = answers.get("company_name") or facts["host"]
    print(f"RevenueOS demo — {name} ({site})\n")
    pages = len(crawl.get("pages", [])) if crawl.get("ok") else 0
    tags = [n for n, k in (("Meta Pixel", "meta_pixel"), ("Google Ads tag", "google_ads_tag"), ("GA4", "ga4")) if sig.get(k)]
    print(f"  {pages} pages crawled · ad/analytics tags: {', '.join(tags) or 'none'} · booking link: {'yes' if sig.get('booking_url') else 'no'} · phones: {', '.join(sig.get('phones') or []) or 'none seen'}\n")
    if not findings:
        print("  Nothing wrong that a crawler can see. Connect the site and RevenueOS keeps checking, every day, for free.")
    tmpdir = Path(tempfile.mkdtemp(prefix="revenueos-demo-"))
    tmp = _W(new_workspace(tmpdir / "ws", Workspace.locate().root))
    from .context import BusinessContext as _B

    _B.load(tmp).onboard({"company_name": name, "website": site})
    ctx = _B.load(tmp)
    for i, f in enumerate(findings, 1):
        fix = deployable_fix(f, ctx, sig)
        print(f"  {i}. {f['kind'].replace('_', ' ').upper()} — {f['url']}\n     {f['why']}")
        print("     → RevenueOS deploys this fix itself once the site is connected (git or WordPress), then re-checks it." if fix
              else "     → RevenueOS writes the fix as a deliverable you approve, then re-checks the page.")
    print(f"\n  {len(findings)} finding(s). Everything above runs free, every day, once connected:\n"
          f"     pip install revenueos && revenueos init --from {site} && revenueos serve\n"
          "  You pay only when you agree with a measured result.")
    shutil.rmtree(tmpdir, ignore_errors=True)  # the demo stores nothing
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    _ws, _store, ctx = _boot(args)
    errors = ctx.validate()
    print("\n".join(errors) if errors else "company-context OK")
    return 1 if errors else 0


def cmd_today(args: argparse.Namespace) -> int:
    ws, store, ctx = _boot(args)
    brief = build_brief(store, ws)
    if args.json:
        print(json.dumps({"objective": brief.objective, "counts": brief.counts, "funnel": brief.funnel, "pipeline_value": brief.pipeline_value,
                          "actions": brief.actions, "approved": brief.approved, "metrics": brief.metrics, "messages": brief.messages,
                          "offer": brief.offer},
                         indent=2, default=str))
    else:
        print(brief.render_text(ctx.company_name))
    return 0


def cmd_offer(args: argparse.Namespace) -> int:
    """Where this workspace stands on the pay-on-result clock, and what is drafted about it."""
    from .convert import offer_line, offer_state

    ws, store, ctx = _boot(args)
    st = offer_state(ws, store, ctx)
    pending = [a for a in store.list_actions("pending", limit=500)
               if (a.get("context") or {}).get("kind") in ("offer", "license_delivery")]
    if args.json:
        print(json.dumps({"offer": st, "actions": pending}, indent=2, default=str))
        return 0
    print(f"OFFER — {ctx.company_name}\n")
    print(f"state: {st['state']}   ({st['reason']})")
    if st["first_result_at"]:
        print(f"first measured result: {st['first_result_at'][:10]}"
              + (f" · {st['days_left']} free day(s) left" if st["days_left"] is not None else ""))
    print(f"Pro: ${st['price']}/month · payment link: {st['payment_link'] or 'not configured (billing.payment_link or REVENUEOS_PAYMENT_LINK_PRO)'}")
    line = offer_line(st)
    if line:
        print(f"\n{line}")
    if pending:
        print(f"\nwaiting for your decision ({len(pending)}):")
        for a in pending:
            print(f"[{a['id']:>4}] {a['title'][:80]}")
        print("\nrevenueos approve <id> && revenueos execute <id>")
    else:
        print("\nnothing drafted (the heartbeat drafts the offer once a result is measured; "
              "`revenueos run billing` drafts a licence delivery for a paying customer).")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ws, store, ctx = _boot(args)
    llm = None if args.no_llm else maybe_llm()
    names = list(all_workers()) if args.worker == "all" else [args.worker]
    workflow = os.environ.get("REVENUEOS_WORKFLOW", "manual")
    results = {n: run_worker(n, ws, store, ctx, llm, workflow) for n in names}
    ok = all(r.ok for r in results.values())
    if args.json:
        if len(names) == 1:
            print(json.dumps(results[names[0]].as_json()))
        else:
            print(json.dumps({"ok": ok, "summary": "; ".join(f"{n}: {r.summary or r.error}" for n, r in results.items()),
                              "actions_created": sum(r.actions_created for r in results.values())}))
    else:
        for n, r in results.items():
            print(f"{'ok  ' if r.ok else 'FAIL'} {n:<10} {r.summary or r.error}")
    return 0 if ok else 1


def _decide(args: argparse.Namespace, verb: str) -> int:
    ws, store, ctx = _boot(args)
    action = store.get_action(args.id)
    if not action:
        print(f"no action {args.id}", file=sys.stderr)
        return 2
    if verb == "ignore":
        store.set_action_status(args.id, "ignored")
        print(f"ignored [{args.id}] {action['title']}")
        return 0
    if verb == "approve":
        store.set_action_status(args.id, "approved")
        if action["context"].get("draft_id"):
            store.set_draft_approval(action["context"]["draft_id"], "approved")
        print(f"approved [{args.id}] {action['title']} — run `revenueos execute {args.id}` to act")
        return 0
    llm = maybe_llm()
    try:
        outcome = execute_action(ws, store, ctx, llm, action)
        if refused(outcome):
            print(f"not executed [{args.id}] {action['title']}\n  {outcome}")
            return 1
        store.set_action_status(args.id, "executed")
        print(f"executed [{args.id}] {action['title']}\n  {outcome}")
        return 0
    except Exception as exc:
        store.set_action_status(args.id, "failed")
        print(f"failed [{args.id}] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def cmd_intake(args: argparse.Namespace) -> int:
    """Read one business document now and say what was found. Extraction is entirely local."""
    from .intake import read_document
    from .workers.intake import _slug

    ws, store, _ctx = _boot(args)
    doc = read_document(args.path)
    if args.json:
        print(json.dumps(doc.as_json(), indent=2))
        return 0 if doc.ok else 2
    print(doc.path.name)
    if not doc.ok:
        print(f"  could not read it: {doc.error}")
        return 2
    size = f"{doc.size_bytes / 1024:.0f} KB" if doc.size_bytes < 1048576 else f"{doc.size_bytes / 1048576:.1f} MB"
    unit = f", {doc.pages} {doc.unit}{'s' if doc.pages != 1 else ''}" if doc.pages is not None else ""
    print(f"  format     {doc.format}{unit}, {size} on disk")
    for key in ("title", "author", "created", "subject"):
        if doc.metadata.get(key):
            print(f"  {key:<10} {doc.metadata[key][:90]}")
    print(f"  text       {doc.chars:,} characters" + (" (truncated)" if doc.truncated else ""))
    for t in doc.tables:
        print(f"  table      {t.name}: {len(t.rows)} row(s) × {len(t.rows[0]) if t.rows else 0} column(s)")
    for note in doc.notes:
        print(f"  note       {note}")
    stored = ""
    if not args.dry_run:
        out = ws.documents / f"{doc.sha256[:8]}-{_slug(doc.path.stem)}.md"
        out.write_text(doc.as_markdown(), encoding="utf-8")
        stored = str(out.relative_to(ws.root))
        store.record_document(path=str(doc.path), sha256=doc.sha256, format=doc.format, pages=doc.pages,
                              chars=doc.chars, title=doc.title, text_path=stored, error=doc.error)
        print(f"  stored     {stored} (sha256 {doc.sha256[:12]})")
    preview = [ln for ln in doc.text.splitlines() if ln.strip()][: args.lines]
    if preview:
        print(f"\n  ── what it says (first {len(preview)} non-blank lines) ──")
        for ln in preview:
            print("  " + ln[:120])
    return 0


def cmd_documents(args: argparse.Namespace) -> int:
    _ws, store, _ctx = _boot(args)
    rows = store.list_documents(limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No documents read yet. Drop a PDF, Word file, spreadsheet or deck into data/inbox/documents/ "
              "and run `revenueos run intake`, or read one now with `revenueos intake <file>`.")
        return 0
    print(f"{'READ':<11} {'FORMAT':<7} {'UNITS':>6} {'CHARS':>9}  TITLE")
    for r in rows:
        units = str(r["pages"]) if r["pages"] is not None else "—"
        print(f"{(r['extracted_at'] or '')[:10]:<11} {r['format']:<7} {units:>6} {r['chars']:>9,}  {(r['title'] or '')[:54]}")
        if r["error"]:
            print(f"{'':<11} could not be read: {r['error'][:100]}")
        elif r["summary"]:
            print(f"{'':<11} {r['summary'][:110]}")
    print(f"\n{len(rows)} document(s).")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    ws, _store, _ctx = _boot(args)
    if args.sub == "index":
        reg = build_registry(ws)
        print(json.dumps(reg["counts"], indent=2))
        return 0
    reg = load_registry(ws)
    if args.sub == "list":
        rows = [s for s in reg["skills"] if not args.source or s["source"] == args.source]
        for s in rows[: args.limit]:
            print(f"{s['source']:<20} {s['slug']:<44} {'[code]' if s['has_scripts'] else '      '} {s['description'][:70]}")
        print(f"\n{len(rows)} skills ({reg['counts']['skills_with_code']} ship code). Sources: {reg['counts']['by_source']}")
        return 0
    if args.sub == "search":
        for s in search_skills(ws, args.query, limit=args.limit, source=args.source):
            print(f"{s['source']}/{s['slug']}\n    {s['description'][:160]}")
        return 0
    if args.sub == "show":
        source, slug = args.query.split("/", 1)
        for s in reg["skills"]:
            if s["source"] == source and s["slug"] == slug:
                print((ws.root / s["path"]).read_text(encoding="utf-8", errors="replace"))
                return 0
        print("not found", file=sys.stderr)
        return 2
    return 2


def cmd_tools(args: argparse.Namespace) -> int:
    ws, _store, _ctx = _boot(args)
    reg = load_registry(ws)
    for t in reg["tools"]:
        configured = all(os.environ.get(v) for v in t["env_vars"]) if t["env_vars"] else True
        print(f"{'✓' if configured else ' '} {t['slug']:<28} {' '.join(t['env_vars'])}")
    print(f"\n{len(reg['tools'])} connectors (node tools/clis/<name>.js --help). ✓ = credential present in the environment.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ws, store, ctx = _boot(args)
    reg = load_registry(ws)
    checks = [
        ("workspace", str(ws.root), True),
        ("onboarded", ctx.company_name, ctx.is_onboarded()),
        ("company-context valid", "canon gate", not ctx.validate()),
        ("Anthropic credential", "ANTHROPIC_API_KEY / ant auth login", LLM.available()),
        ("skills indexed", f"{reg['counts']['skills']} skills, {reg['counts']['tools']} connectors", reg["counts"]["skills"] > 0),
        ("node (connectors + orchestrator)", shutil.which("node") or "missing", bool(shutil.which("node"))),
        ("orchestrator deps", "orchestrator/node_modules", (ws.root / "orchestrator" / "node_modules").exists()),
        ("automations", str(ws.automations.relative_to(ws.root)), ws.automations.exists()),
        ("openoutreach (GPL, process boundary)", shutil.which("openoutreach") or "not installed", bool(shutil.which("openoutreach"))),
        ("SMTP", "smtp.host in revenueos.yaml + SMTP_PASSWORD", bool((ctx.config.get("smtp") or {}).get("host") and os.environ.get("SMTP_PASSWORD"))),
        ("ad exports", f"{len(list(ws.exports.glob('ads-*.csv')))} file(s) in data/exports/", True),
        ("pending actions", str(sum(store.counts_by_type('pending').values())), True),
    ]
    for name, detail, ok in checks:
        print(f"{'✓' if ok else '✗'} {name:<36} {detail}")
    chain = chain_status()
    print("\nmodel routing — tried in this order, failing over on rate limits, outages and timeouts:")
    for pos, p in enumerate(chain, 1):
        print(f"{'✓' if p['ready'] else '✗'} {pos}. {p['name']:<33} {p['model']} — {p['detail']}")
    if not chain:
        print("  none (REVENUEOS_LLM=off, or no credential): workers run without a model.")
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    _ws, store, ctx = _boot(args)
    brief = build_brief(store)
    if args.json:
        print(json.dumps({"summary": brief.summary, "results": brief.results}, indent=2, default=str))
        return 0
    s = brief.summary
    print(f"RESULTS — {ctx.company_name}\n")
    print(f"actions: {s['found']} found · {s['executed']} executed · {s['measured']} measured · {s.get('produced', 0)} produced (not published)")
    print(f"{s['emails_sent']} emails sent · {s['replies']} replies · {s['bounces']} bounces · {s['booked']} booked · ${s['pipeline_value']:,.0f} pipeline\n")
    for line in brief.result_lines(limit=100):
        print(line)
    if not brief.results:
        print("No executed actions yet. Approve and execute something from `revenueos today`.")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    """The next highest-value actions, ranked from measured history (see today.rank_next)."""
    from .today import rank_next

    _ws, store, ctx = _boot(args)
    ranked = rank_next(store, limit=args.limit)
    if args.json:
        print(json.dumps(ranked, indent=2, default=str))
        return 0
    print(f"NEXT — {ctx.company_name}\n")
    if not ranked:
        print("Nothing pending. Run `revenueos run all`.")
        return 0
    for r in ranked:
        hist = f"{r['executed_of_type']} executed, win rate {r['win_rate']:.0%}" if r["executed_of_type"] else "no history yet"
        print(f"{r['score']:>6.2f} [{r['id']:>4}] {r['action_type']:<19} {r['title'][:70]}   ({hist})")
    print(f"\nApprove and execute the top one: revenueos approve {ranked[0]['id']} && revenueos execute {ranked[0]['id']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .panel import serve

    ws, store, ctx = _boot(args)
    return serve(ws, store, ctx, host=args.host, port=args.port)


def cmd_orchestrator(args: argparse.Namespace) -> int:
    ws, store, _ctx = _boot(args)
    if not args.once:  # free until a measured result the business agreed with, plus a grace period; then Pro
        try:
            from .billing import pay_on_result

            verdict = pay_on_result(ws, store)
        except ImportError:
            verdict = {"allowed": True, "reason": "community build"}
        if not verdict["allowed"]:
            print(verdict["reason"], file=sys.stderr)
            return 3
        print(f"continuous operation: {verdict['reason']}", flush=True)
    orch = ws.root / "orchestrator"
    if not (orch / "node_modules").exists():
        subprocess.run(["npm", "install", "--silent"], cwd=orch, check=True)
    env = {**os.environ, "REVENUEOS_ROOT": str(ws.root)}
    cmd = ["npx", "tsx", "src/worker/index.ts"] + (["--once", args.once] if args.once else [])
    return subprocess.run(cmd, cwd=orch, env=env, check=False).returncode


def cmd_workspace(args: argparse.Namespace) -> int:
    """Create a fresh customer workspace that shares this install's catalogue (skills, tools,
    orchestrator) but has its own canon, config, database and logs."""
    from .paths import new_workspace

    try:
        dest = new_workspace(Path(args.dir), Workspace.locate().root)
    except FileExistsError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"workspace ready: {dest}\n  revenueos --root {dest} init   (or REVENUEOS_ROOT={dest})")
    return 0


def cmd_connections(args: argparse.Namespace) -> int:
    from .connections import ConnectionStore, describe_all

    ws, _store, _ctx = _boot(args)
    cs = ConnectionStore(ws)
    rows = describe_all(cs)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"CONNECTIONS — {ws.root}  (secrets {'sealed' if cs.sealed() else 'plain, file mode 600; set REVENUEOS_TOKEN_KEY to seal'})")
    for r in rows:
        state = f"connected as {r['account']}" + (" · changes allowed" if r["allow_write"] else " · read-only") if r["connected"] else "not connected"
        print(f"  {r['name']:<13} {r['label']:<48} {state}")
        if not r["connected"]:
            print(f"  {'':<13} how: {r['how']}" + (f"   missing: {'; '.join(r['missing'])}" if r["missing"] else ""))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    from .connections import ConnectionStore, github_site, google, meta, stripe_conn, wordpress

    ws, _store, _ctx = _boot(args)
    cs = ConnectionStore(ws)
    name = args.provider
    try:
        if name == "stripe":
            conn = stripe_conn.connect(cs, args.key)
        elif name == "github_site":
            if not args.repo:
                print("--repo owner/name is required", file=sys.stderr)
                return 2
            conn = github_site.connect(cs, args.repo, args.path or "", args.branch or "main", (_ctx.config.get("sender") or {}).get("email"))
        elif name == "wordpress":
            if not (args.site and args.user and args.app_password):
                print("--site, --user and --app-password are required", file=sys.stderr)
                return 2
            conn = wordpress.connect(cs, args.site, args.user, args.app_password)
        elif name == "google":
            conn = google.connect(cs, args.scopes.split(",") if args.scopes else None)
        elif name == "meta":
            conn = meta.connect(cs, args.scopes.split(",") if args.scopes else None)
        else:
            print(f"unknown provider {name!r}", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"not connected: {exc}", file=sys.stderr)
        return 1
    if args.allow_changes:
        cs.set_allow_write(name, True)
    print(f"connected {name} as {conn.account} · scopes {', '.join(conn.scopes)} · changes {'allowed' if args.allow_changes else 'not allowed (read-only until you turn it on)'}")
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    from .connections import ConnectionStore

    ws, _store, _ctx = _boot(args)
    print("disconnected" if ConnectionStore(ws).remove(args.provider) else "was not connected")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    from .tenants import Accounts

    host = Path(args.root).expanduser().resolve() if args.root else Workspace.locate().root
    acc = Accounts(host)
    if args.verb == "add":
        try:
            rec = acc.add(args.email, args.password, template=host)
        except (ValueError, FileExistsError) as e:
            print(e, file=sys.stderr)
            return 2
        print(f"account {rec['email']} → workspace {rec['workspace']}")
        return 0
    for r in acc.list():
        print(f"  {r['email']:<36} {r['role']:<6} {r['workspace']}")
    if not acc.list():
        print("no accounts (single-tenant mode); add one: revenueos accounts add <email> <password>")
    return 0


def _objective_args(args: argparse.Namespace) -> int:
    """`objective add "<title>"` / `objective show <id>` share one positional; split it here so
    cmd_objective can be called directly from tests with a plain namespace."""
    args.title = args.target if args.verb == "add" else None
    args.id = None
    if args.verb not in ("add", "list"):
        try:
            args.id = int(args.target) if args.target is not None else None
        except ValueError:
            print(f"not an objective id: {args.target!r}", file=sys.stderr)
            return 2
    return cmd_objective(args)


def cmd_objective(args: argparse.Namespace) -> int:
    """The one thing the business is trying to achieve, and the trail of what happened towards it."""
    from .objectives import HOW_TO_ADD, ensure_objective
    from .store import OBJECTIVE_EVENT_KINDS

    _ws, store, _ctx = _boot(args)
    verb = args.verb
    if verb == "add":
        oid = ensure_objective(store, args.title or "", strategy=args.strategy)
        if oid is None:
            print("an active objective with that title already exists", file=sys.stderr)
            return 1
        print(f"objective [{oid}] {args.title}")
        return 0
    if verb == "list":
        rows = store.list_objectives()
        if not rows:
            print(HOW_TO_ADD)
            return 0
        for o in rows:
            hb = store.latest_heartbeat(o["id"])
            print(f"[{o['id']:>3}] {o['status']:<7} {o['title']}")
            if o.get("next_action"):
                print(f"        next: {o['next_action']}")
            if hb:
                print(f"        heartbeat {hb['ts'][:16].replace('T', ' ')}: {hb['text'][:120]}")
        return 0
    if args.id is None:
        print(f"revenueos objective {verb} <id>", file=sys.stderr)
        return 2
    o = store.get_objective(args.id)
    if not o:
        print(f"no objective {args.id}", file=sys.stderr)
        return 2
    if verb == "show":
        print(f"[{o['id']}] {o['title']}  ({o['status']})")
        print(f"strategy: {o.get('strategy') or '— not set —'}")
        print(f"next:     {o.get('next_action') or '— the heartbeat sets this —'}\n")
        events = store.list_objective_events(o["id"], limit=15)
        if not events:
            print("no events yet. `revenueos run heartbeat` writes the first one.")
        for e in events:
            print(f"{e['ts'][:16].replace('T', ' ')}  {e['kind']:<9} {e['text']}")
        return 0
    if verb in ("pause", "resume", "done"):
        store.set_objective_status(o["id"], {"pause": "paused", "resume": "active", "done": "done"}[verb])
        print(f"objective [{o['id']}] is now {store.get_objective(o['id'])['status']}")
        return 0
    if verb == "set":
        if args.strategy is None and args.next is None:
            print("nothing to set: pass --strategy and/or --next", file=sys.stderr)
            return 2
        store.update_objective(o["id"], strategy=args.strategy, next_action=args.next)
        print(f"objective [{o['id']}] updated")
        return 0
    if verb == "note":
        if args.kind not in OBJECTIVE_EVENT_KINDS or args.kind == "heartbeat":
            print(f"--kind must be one of {', '.join(k for k in OBJECTIVE_EVENT_KINDS if k != 'heartbeat')}", file=sys.stderr)
            return 2
        if not args.text:
            print("nothing to record: pass the note text", file=sys.stderr)
            return 2
        store.add_objective_event(o["id"], args.kind, args.text, {"source": "cli"})
        print(f"recorded {args.kind} on objective [{o['id']}]")
        return 0
    return 2


def cmd_messages(args: argparse.Namespace) -> int:
    """What RevenueOS has to say to a human, and nothing else."""
    _ws, store, _ctx = _boot(args)
    rows = store.list_messages(args.to, unread_only=args.unread, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no messages" + (" unread" if args.unread else ""))
        return 0
    for m in rows:
        mark = " " if m.get("read_at") else "•"
        print(f"{mark} [{m['id']:>3}] {m['ts'][:16].replace('T', ' ')}  {m['from_agent']} → {m['to_agent']}: {m['subject']}")
        for line in (m.get("body") or "").strip().splitlines():
            print(f"        {line}")
    if args.mark_read:
        for m in rows:
            store.mark_read(m["id"])
        print(f"\nmarked {len(rows)} message(s) read")
    return 0


def _render_role_result(result, indent: str = "") -> None:
    mark = "ok  " if result.ok else "FAIL"
    print(f"{indent}{mark} {result.role} #{result.run_id if result.run_id is not None else '-'}")
    if not result.ok:
        print(f"{indent}     {result.error}")
        if result.raw:
            print(f"{indent}     raw: {result.raw.strip()[:300]}")
        return
    out = result.output or {}
    print(f"{indent}     {out.get('summary', '').strip()}")
    for f in (out.get("findings") or [])[:12]:
        if isinstance(f, dict):
            ev = f.get("evidence")
            ev = "; ".join(str(x) for x in ev) if isinstance(ev, list) else str(ev or "")
            print(f"{indent}     • {str(f.get('claim', '')).strip()}   [{ev.strip()}]")
    for c in (out.get("cannot_determine") or [])[:6]:
        print(f"{indent}     ? cannot determine: {c}")
    for s in out.get("skipped_proposals") or []:
        print(f"{indent}     – proposal skipped: {s}")
    if result.proposed_action_ids:
        ids = ", ".join(str(i) for i in result.proposed_action_ids)
        print(f"{indent}     proposed {len(result.proposed_action_ids)} action(s) [{ids}] — they wait for approval in `revenueos today`")
    for sub in result.subresults:
        _render_role_result(sub, indent + "    ")


def cmd_agent(args: argparse.Namespace) -> int:
    """Roles — specialised subagents (research, marketing, sales, measurement) that can call each
    other. They propose actions; only Execute ever acts."""
    from .roles import ROLES, run_role

    ws, store, ctx = _boot(args)
    rest = list(args.rest)
    if args.sub == "run":
        if len(rest) < 2:
            print('usage: revenueos agent run <role> "<task>"', file=sys.stderr)
            return 2
        role, task = rest[0], " ".join(rest[1:])
        if role not in ROLES:
            print(f"unknown role {role!r}; known: {', '.join(ROLES)}", file=sys.stderr)
            return 2
        result = run_role(role, task, ws, store, ctx, None if args.no_llm else maybe_llm())
        if args.json:
            print(json.dumps(result.as_json(), indent=2, default=str))
        else:
            _render_role_result(result)
        return 0 if result.ok else 1
    if args.sub == "runs":
        rows = store.list_agent_runs(role=args.role, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
            return 0
        for r in rows:
            depth = "  " * int(r["depth"] or 0)
            print(f"[{r['id']:>4}] {'ok  ' if r['ok'] else 'FAIL'} {r['started_at']}  {depth}{r['role']:<12} {r['task'][:70]}")
        if not rows:
            print("no role runs yet — try: revenueos agent run research \"...\"")
        return 0
    if args.sub == "show":
        if not rest:
            print("usage: revenueos agent show <id>", file=sys.stderr)
            return 2
        row = store.get_agent_run(int(rest[0]))
        if not row:
            print(f"no role run {rest[0]}", file=sys.stderr)
            return 2
        print(json.dumps(row, indent=2, default=str))
        return 0
    return 2


def cmd_learn(args: argparse.Namespace) -> int:
    """Turn measured outcomes into lessons, or record one by hand. Lessons ride in every prompt."""
    from .learning import add_lesson, lessons_from_outcomes, recent_lessons

    ws, store, _ctx = _boot(args)
    if args.sub == "add":
        missing = [f for f in ("title", "attempt", "result", "evidence") if not getattr(args, f, None)]
        if missing:
            print(f"missing --{' --'.join(missing)}", file=sys.stderr)
            return 2
        add_lesson(ws, args.title, attempt=args.attempt, result=args.result, evidence=args.evidence,
                   why=args.why or "stated by the operator", lesson=args.lesson or "pending analysis",
                   apply_when=args.apply_when or "")
        print("recorded in learning-loop/LESSONS.md (newest first); it is injected into every prompt for 60 days.")
        return 0
    written = lessons_from_outcomes(ws, store, None if args.no_llm else maybe_llm())
    newest = recent_lessons(ws).splitlines()
    title = newest[0].lstrip("# ").strip() if newest else ""
    print(f"{written} new lesson(s) from measured outcomes." if written else
          "0 new lessons — every measured outcome already has one (or nothing has been measured yet).")
    if title:
        print(f"newest: {title}")
    return 0


def cmd_lessons(args: argparse.Namespace) -> int:
    from .learning import recent_lessons

    ws, _store, _ctx = _boot(args)
    text = recent_lessons(ws, days=args.days, max_chars=200000)
    print(text if text else f"no lessons in the last {args.days} days — run `revenueos learn` after something is measured.")
    return 0


def cmd_refine(args: argparse.Namespace) -> int:
    """One small, evidence-backed edit to a role spec — or the snapshot restored."""
    from .learning import RefinementRefused, list_snapshots, refine, rollback

    ws, _store, _ctx = _boot(args)
    if args.rollback is not None:
        try:
            snap = rollback(ws, args.role, args.rollback or None)
        except (FileNotFoundError, ValueError) as exc:
            print(exc, file=sys.stderr)
            if list_snapshots(ws, args.role):
                print("available:", *[p.name for p in list_snapshots(ws, args.role)], sep="\n  ", file=sys.stderr)
            return 2
        print(f"{args.role} spec restored from {snap.name}")
        return 0
    if not (args.evidence and args.change):
        print("--evidence and --change are both required (a refinement without evidence is a guess)", file=sys.stderr)
        return 2
    try:
        ref = refine(ws, args.role, args.evidence, args.change, None if args.no_llm else maybe_llm())
    except RefinementRefused as exc:
        print(exc, file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"{ref.role} spec refined ({ref.diff_lines} changed line(s), {ref.mode}).\n"
          f"  snapshot: learning-loop/snapshots/{ref.snapshot.name}   undo: revenueos refine {ref.role} --rollback")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    _ws, _store, ctx = _boot(args)
    ctx.add_correction(args.title, args.context, args.correction, args.apply_when, source="cli")
    print("recorded in learning-loop/CORRECTIONS.md (newest first); it is injected into every worker prompt for 30 days.")
    return 0


# ── parser ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="revenueos", description="Connect your business. Turn RevenueOS on.")
    p.add_argument("--root", help="workspace root (default: auto-detect / $REVENUEOS_ROOT)")
    p.add_argument("--version", action="version", version=f"revenueos {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="the onboarding questionnaire — or `--from https://yoursite` to fill it from the website")
    s.add_argument("--from", dest="from_url", help="derive the answers from this website (connect once)")
    s.add_argument("--no-llm", action="store_true", help="facts only, no model")
    s.add_argument("--answers", help="JSON file of answers (non-interactive)")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("validate", help="run the company-context gate").set_defaults(fn=cmd_validate)
    s = sub.add_parser("demo", help="one command on any website: what is costing it customers, and what RevenueOS would fix")
    s.add_argument("url")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("today", help="the daily brief")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_today)

    s = sub.add_parser("run", help="run a worker now")
    s.add_argument("worker", choices=[*all_workers().keys(), "all"])
    s.add_argument("--json", action="store_true", help="one JSON line (used by the orchestrator)")
    s.add_argument("--no-llm", action="store_true", help="deterministic paths only")
    s.set_defaults(fn=cmd_run)

    for verb in ("approve", "execute", "ignore"):
        s = sub.add_parser(verb, help=f"{verb} an action from TODAY")
        s.add_argument("id", type=int)
        s.set_defaults(fn=lambda a, v=verb: _decide(a, v))

    s = sub.add_parser("intake", help="read one business document (PDF, Word, Excel, PowerPoint, CSV, RTF) and say what is in it")
    s.add_argument("path")
    s.add_argument("--json", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="print what was found without recording the document")
    s.add_argument("--lines", type=int, default=20, help="how many lines of the extracted text to show")
    s.set_defaults(fn=cmd_intake)

    s = sub.add_parser("documents", help="the documents the business has handed over and what was read from them")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_documents)

    s = sub.add_parser("skills", help="the unified skill catalogue")
    s.add_argument("sub", choices=["index", "list", "search", "show"])
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--source")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_skills)
    sub.add_parser("tools", help="connector CLIs and their credentials").set_defaults(fn=cmd_tools)
    sub.add_parser("doctor", help="what is connected, what is missing").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("results", help="what RevenueOS did and what measurable result occurred")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_results)

    s = sub.add_parser("offer", help="the pay-on-result clock and what is drafted about it")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_offer)

    s = sub.add_parser("next", help="the next highest-value actions, ranked from measured results")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_next)

    s = sub.add_parser("serve", help="the control panel")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8791)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("orchestrator", help="start the always-on scheduler (Kairos worker)")
    s.add_argument("--once", help="run one automation by name and exit")
    s.set_defaults(fn=cmd_orchestrator)

    s = sub.add_parser("workspace", help="create a fresh customer workspace sharing this install's catalogue")
    s.add_argument("sub", choices=["new"])
    s.add_argument("dir")
    s.set_defaults(fn=cmd_workspace)

    s = sub.add_parser("connections", help="what is connected and what each connection may do")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_connections)
    s = sub.add_parser("connect", help="connect an account: stripe | google | meta | github_site | wordpress")
    s.add_argument("provider")
    s.add_argument("--key", help="stripe: secret or restricted key")
    s.add_argument("--repo", help="github_site: owner/name")
    s.add_argument("--path", help="github_site: site directory inside the repo")
    s.add_argument("--branch", help="github_site: branch (main)")
    s.add_argument("--site", help="wordpress: site url")
    s.add_argument("--user", help="wordpress: user")
    s.add_argument("--app-password", help="wordpress: application password")
    s.add_argument("--scopes", help="google/meta: comma-separated scope names")
    s.add_argument("--allow-changes", action="store_true", help="let executors change things through this connection")
    s.set_defaults(fn=cmd_connect)
    s = sub.add_parser("disconnect", help="remove a connection and its tokens")
    s.add_argument("provider")
    s.set_defaults(fn=cmd_disconnect)
    s = sub.add_parser("accounts", help="hosted mode: customer accounts, one workspace each")
    s.add_argument("verb", choices=["add", "list"])
    s.add_argument("email", nargs="?")
    s.add_argument("password", nargs="?")
    s.set_defaults(fn=cmd_accounts)
    s = sub.add_parser("objective", help="the revenue objective RevenueOS is working towards, and its trail")
    s.add_argument("verb", choices=["add", "list", "show", "set", "note", "pause", "resume", "done"])
    s.add_argument("target", nargs="?", help="the objective title (add) or its id (everything else)")
    s.add_argument("text", nargs="?", help="note text (note)")
    s.add_argument("--strategy", help="how the business intends to reach it")
    s.add_argument("--next", help="what to do next (normally written by the heartbeat)")
    s.add_argument("--kind", default="evidence", help="note kind: evidence|action|result|failure|lesson|next")
    s.set_defaults(fn=_objective_args)

    s = sub.add_parser("messages", help="what RevenueOS has to say to you")
    s.add_argument("--to", default="operator")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--mark-read", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_messages)

    s = sub.add_parser("agent", help="run a specialised role (research | marketing | sales | measurement)")
    s.add_argument("sub", choices=["run", "runs", "show"])
    s.add_argument("rest", nargs="*", help='run: <role> "<task>"   ·   show: <id>')
    s.add_argument("--role", help="runs: filter by role")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_agent)

    s = sub.add_parser("learn", help="turn measured outcomes into lessons (learning-loop/LESSONS.md)")
    s.add_argument("sub", nargs="?", choices=["add"], default=None)
    s.add_argument("--title")
    s.add_argument("--attempt")
    s.add_argument("--result")
    s.add_argument("--evidence")
    s.add_argument("--why")
    s.add_argument("--lesson")
    s.add_argument("--apply-when", dest="apply_when")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("lessons", help="what RevenueOS learned from measured results")
    s.add_argument("--days", type=int, default=60)
    s.set_defaults(fn=cmd_lessons)

    s = sub.add_parser("refine", help="one small, evidence-backed edit to a role spec (snapshotted, reversible)")
    s.add_argument("role")
    s.add_argument("--evidence", help="what was observed that justifies the change")
    s.add_argument("--change", help="the change, in one line")
    s.add_argument("--rollback", nargs="?", const="", default=None, help="restore the latest (or a named) snapshot")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_refine)

    s = sub.add_parser("correct", help="record a permanent correction")
    s.add_argument("title")
    s.add_argument("--context", required=True)
    s.add_argument("--correction", required=True)
    s.add_argument("--apply-when", required=True)
    s.set_defaults(fn=cmd_correct)
    try:
        from .billing import register as register_billing

        register_billing(sub)
    except ImportError:
        pass
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
