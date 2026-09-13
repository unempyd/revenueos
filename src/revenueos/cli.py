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
from .llm import LLM, maybe_llm
from .paths import Workspace, is_workspace
from .registry import build_registry, load_registry, search_skills
from .store import Store
from .today import build_brief
from .workers import all_workers, execute_action, run_worker


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
    ws, _store, ctx = _boot(args)
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
    _ws, store, ctx = _boot(args)
    brief = build_brief(store)
    if args.json:
        print(json.dumps({"counts": brief.counts, "funnel": brief.funnel, "pipeline_value": brief.pipeline_value, "actions": brief.actions, "approved": brief.approved, "metrics": brief.metrics}, indent=2, default=str))
    else:
        print(brief.render_text(ctx.company_name))
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
        store.set_action_status(args.id, "executed")
        print(f"executed [{args.id}] {action['title']}\n  {outcome}")
        return 0
    except Exception as exc:
        store.set_action_status(args.id, "failed")
        print(f"failed [{args.id}] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


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
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    _ws, store, ctx = _boot(args)
    brief = build_brief(store)
    if args.json:
        print(json.dumps({"summary": brief.summary, "results": brief.results}, indent=2, default=str))
        return 0
    s = brief.summary
    print(f"RESULTS — {ctx.company_name}\n")
    print(f"{s['found']} opportunities found · {s['executed']} executed · {s['measured']} measured")
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
