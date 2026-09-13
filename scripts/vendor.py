#!/usr/bin/env python3
"""The RevenueOS assembly map, made executable.

Copies the pieces RevenueOS reuses from the pinned clones in upstream/ into the
product tree.  Idempotent: every destination listed in VENDOR_MAP is deleted and
re-created from upstream on each run, so edits belong in the map (or in a
PATCHES entry), never in the vendored files themselves.

    scripts/fetch-upstream.sh   # once, after a fresh clone of RevenueOS
    python3 scripts/vendor.py   # re-assemble

Licence gate: only MIT / Apache-2.0 upstreams are copied.  The four unlicensed
repositories (OpenCRM, ai-os-skills, erron-ai, business-skills), the
proprietary AdKit pack and the GPL-3.0 OpenOutreach are deliberately absent from
the map; see NOTICE.md.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UP = ROOT / "upstream"
VENDOR = ROOT / "src" / "revenueos" / "vendor"

ALLOWED_LICENSES = {"MIT", "Apache"}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "node_modules", ".git")

# (upstream dir, source path, destination path, mode)
#   mode "tree"  – copy a directory
#   mode "file"  – copy a single file
#   mode "glob"  – copy every directory matching `src` (a glob) under `dst`/<basename>
VENDOR_MAP: list[tuple[str, str, str, str]] = [
    # ── Measurement brain: claude-ads deterministic core (stdlib only) ──────────
    ("AgriciDaniel__claude-ads", "claude_ads_core", "src/revenueos/vendor/claude_ads_core", "tree"),
    ("AgriciDaniel__claude-ads", "control-plane/manifests", "src/revenueos/vendor/claude_ads_data/control-plane/manifests", "tree"),
    ("AgriciDaniel__claude-ads", "skills/*", "skills/ads", "glob"),
    ("AgriciDaniel__claude-ads", "ads", "skills/ads/ads", "tree"),
    ("AgriciDaniel__claude-ads", "agents", "agents/ads", "tree"),
    # ── Market monitoring: pulse-cmo crawl / HN discovery / relevance gate ──────
    ("aruntemme__pulse-cmo", "src/pulse/tools/registry.py", "src/revenueos/vendor/pulse/registry.py", "file"),
    ("aruntemme__pulse-cmo", "src/pulse/tools/crawl.py", "src/revenueos/vendor/pulse/crawl.py", "file"),
    ("aruntemme__pulse-cmo", "src/pulse/tools/discovery.py", "src/revenueos/vendor/pulse/discovery.py", "file"),
    ("aruntemme__pulse-cmo", "src/pulse/relevance.py", "src/revenueos/vendor/pulse/relevance.py", "file"),
    ("aruntemme__pulse-cmo", "src/pulse/text.py", "src/revenueos/vendor/pulse/text.py", "file"),
    ("aruntemme__pulse-cmo", "src/pulse/product_brain.py", "src/revenueos/vendor/pulse/product_brain.py", "file"),
    # ── Sales pipeline: ai-sales-agent data model, approval state machine, renderer
    ("Meshpilot-AGI__ai-sales-agent", "src/sales_agent/db/models.py", "src/revenueos/vendor/sales_agent/models.py", "file"),
    ("Meshpilot-AGI__ai-sales-agent", "src/sales_agent/agent/recipes_stub.py", "src/revenueos/vendor/sales_agent/recipes.py", "file"),
    ("Meshpilot-AGI__ai-sales-agent", "src/sales_agent/mail/render.py", "src/revenueos/vendor/sales_agent/render.py", "file"),
    ("Meshpilot-AGI__ai-sales-agent", "src/sales_agent/mail/templates", "src/revenueos/vendor/sales_agent/templates", "tree"),
    ("Meshpilot-AGI__ai-sales-agent", "migrations/0001_init_schema.sql", "src/revenueos/vendor/sales_agent/postgres_schema.sql", "file"),
    # ── Business context + gates: markster-os schema, validator, learning loop ──
    ("markster-public__markster-os", "tools/validate_markster_os.py", "src/revenueos/vendor/markster/validate_markster_os.py", "file"),
    ("markster-public__markster-os", "company-context", "company-context", "tree"),
    # overlay, not tree: learning-loop/ also holds RevenueOS-owned state that must survive a
    # re-vendor — CORRECTIONS.md history, LESSONS.md, REFINEMENTS.md, roles/ and snapshots/.
    ("markster-public__markster-os", "learning-loop", "learning-loop", "overlay"),
    ("markster-public__markster-os", "methodology", "methodology", "tree"),
    ("markster-public__markster-os", "playbooks", "playbooks", "tree"),
    ("markster-public__markster-os", "skills/*", "skills/gtm", "glob"),
    # ── SEO operator ─────────────────────────────────────────────────────────────
    ("marketingskills__seo", "skills/seo-growth-loop/scripts/probe_project.py", "src/revenueos/vendor/seo/probe_project.py", "file"),
    ("marketingskills__seo", "skills/authority-mark-keyword-difficulty/scripts/authority_mark.py", "src/revenueos/vendor/seo/authority_mark.py", "file"),
    ("marketingskills__seo", "skills/*", "skills/seo", "glob"),
    # ── Orchestration: Kairos worker (cron, JSONL run journal, activity log, status API)
    ("kevinbadi__kairos", "src/worker/schedule.ts", "orchestrator/src/worker/schedule.ts", "file"),
    ("kevinbadi__kairos", "src/worker/automations.ts", "orchestrator/src/worker/automations.ts", "file"),
    ("kevinbadi__kairos", "src/worker/server.ts", "orchestrator/src/worker/server.ts", "file"),
    ("kevinbadi__kairos", "src/storage/store.ts", "orchestrator/src/storage/store.ts", "file"),
    ("kevinbadi__kairos", "src/storage/jsonlStore.ts", "orchestrator/src/storage/jsonlStore.ts", "file"),
    ("kevinbadi__kairos", "src/util/activityLog.ts", "orchestrator/src/util/activityLog.ts", "file"),
    ("kevinbadi__kairos", "tests/storage.test.ts", "orchestrator/tests/storage.test.ts", "file"),
    ("kevinbadi__kairos", "tests/activityMerge.test.ts", "orchestrator/tests/activityMerge.test.ts", "file"),
    # ── Persistent CMO loop: corrections memory + operator skills (markdown) ────
    ("guerrilla2799__ai-cmo-operator", "CORRECTIONS-template.md", "learning-loop/CORRECTIONS.md", "file"),
    ("guerrilla2799__ai-cmo-operator", "skills/*.md", "skills/cmo", "mdglob"),
    # ── Skill libraries ──────────────────────────────────────────────────────────
    ("iamwaqargulzar__Marketing-Agent-OS", "skills/*", "skills/core", "glob"),
    ("iamwaqargulzar__Marketing-Agent-OS", "catalog.json", "skills/core/catalog.json", "file"),
    ("OpenClaudia__openclaudia-skills", "skills/*", "skills/creative", "glob"),
    ("scayver__marketing-skills", "skills/*", "skills/operations", "glob"),
    ("scayver__marketing-skills", "docs/MARKETING_OS_MANIFEST.json", "skills/operations/MANIFEST.json", "file"),
    ("coreyhaines31__marketingskills", "skills/*", "skills/growth", "glob"),
    ("kostja94__marketing-skills", "skills", "skills/playbooks", "tree"),
    ("ericosiu__ai-marketing-skills", "*/SKILL.md", "skills/pipelines", "skilldirs"),
    ("shalintripathi__saas-marketing-agents", "plugins/saas-marketing/skills/*", "skills/agency", "glob"),
    ("shalintripathi__saas-marketing-agents",
     "abm,analytics,client-ops,comms,content,design,developer-marketing,email,events,growth,paid-media,partnerships,product-marketing,project-management,sales,seo,social",
     "agents/agency", "dirs"),
    ("growthack88__growth-marketing-os", "skills/*", "skills/growth-lab", "glob"),
    ("claude-office-skills__skills", "lead-research", "skills/office/lead-research", "tree"),
    ("claude-office-skills__skills", "lead-qualification", "skills/office/lead-qualification", "tree"),
    ("claude-office-skills__skills", "content-writer", "skills/office/content-writer", "tree"),
    ("claude-office-skills__skills", "brand-guidelines", "skills/office/brand-guidelines", "tree"),
    ("claude-office-skills__skills", "ads-copywriter", "skills/office/ads-copywriter", "tree"),
    ("claude-office-skills__skills", "competitive-analysis", "skills/office/competitive-analysis", "tree"),
    ("claude-office-skills__skills", "company-research", "skills/office/company-research", "tree"),
    # ── Inbound email: mailgun/talon reply-quotation stripping (second research pass) ──
    ("mailgun__talon", "talon/quotations.py", "src/revenueos/vendor/talon/quotations.py", "file"),
    ("mailgun__talon", "talon/html_quotations.py", "src/revenueos/vendor/talon/html_quotations.py", "file"),
    ("mailgun__talon", "talon/utils.py", "src/revenueos/vendor/talon/utils.py", "file"),
    ("mailgun__talon", "talon/constants.py", "src/revenueos/vendor/talon/constants.py", "file"),
    # ── Tool connectors: zero-dependency Node CLIs + integration guides ─────────
    ("scayver__marketing-skills", "tools/clis", "tools/clis", "tree"),
    ("scayver__marketing-skills", "tools/integrations", "tools/integrations", "tree"),
    ("coreyhaines31__marketingskills", "tools/clis", "tools/clis", "overlay"),
    ("coreyhaines31__marketingskills", "tools/integrations", "tools/integrations", "overlay"),
    ("coreyhaines31__marketingskills", "tools/REGISTRY.md", "tools/REGISTRY.md", "file"),
]

# Post-copy patches: (destination path, regex, replacement, why).  Kept tiny on purpose.
PATCHES: list[tuple[str, str, str, str]] = [
    ("src/revenueos/vendor/pulse/relevance.py", r"from \.llm import LLM, Message", "from revenueos.llm import LLM, Message",
     "RevenueOS supplies the LLM client (Anthropic SDK) instead of pulse's OpenAI-compatible one"),
    ("orchestrator/src/worker/automations.ts", r"join\(workspaceRoot, 'kairos', 'automations\.json'\)", "join(workspaceRoot, 'data', 'automations.json')",
     "RevenueOS workspace layout: data/automations.json"),
    ("orchestrator/src/storage/jsonlStore.ts", r"join\(workspaceRoot, 'kairos', 'content', 'items\.jsonl'\)", "join(workspaceRoot, 'data', 'content.jsonl')",
     "RevenueOS workspace layout"),
    ("src/revenueos/vendor/pulse/discovery.py", r"from \.\.relevance import", "from .relevance import",
     "flattened package: relevance.py sits beside discovery.py"),
    ("src/revenueos/vendor/sales_agent/render.py", r"from sales_agent\.db\.models import", "from .models import",
     "package relocation"),
    ("src/revenueos/vendor/sales_agent/render.py", r'SENDER_NAME = "[^"]+"',
     'class _Sender:\n    def __str__(self) -> str:\n        return settings.casl_sender_name\n    __format__ = lambda self, spec: settings.casl_sender_name  # noqa: E731\n\n\nSENDER_NAME = _Sender()',
     "upstream hardcoded the original author\'s name; RevenueOS reads the sender from revenueos.yaml"),
    ("src/revenueos/vendor/sales_agent/render.py", r'DEMO_URL = "https://example-brand\.com"\nLANDING_URL = "https://landing\.example\.com"',
     'DEMO_URL = ""\nLANDING_URL = ""',
     "upstream demo/landing placeholders removed; booking_url comes from revenueos.yaml"),
    ("src/revenueos/vendor/sales_agent/render.py", r"from sales_agent\.config import settings\n", "from .settings import settings\n",
     "settings object provided by revenueos.vendor.sales_agent.settings"),
    ("src/revenueos/vendor/sales_agent/render.py",
     r'f"\{settings\.casl_sender_name\} · \{settings\.casl_sender_address\}\\n"',
     'f"{settings.casl_sender_name}" + (f" · {settings.casl_sender_address}" if settings.casl_sender_address else "") + "\\n"',
     "no dangling separator when the sender has no postal address"),
    ("src/revenueos/vendor/sales_agent/render.py", r'        f"— \{SENDER_NAME\}\\n"\n        f"\\n"\n', "",
     "RevenueOS drafts carry their own signature block; the vendored sign-off would duplicate it"),
]

PATCHES += [
    ("src/revenueos/vendor/pulse/product_brain.py",
     r"from \.llm import LLM, Message\nfrom \.store import ActionStore\nfrom \.strategy_core import gather_evidence, render_evidence\n",
     "from revenueos.llm import LLM, Message  # RevenueOS: store/strategy_core imports removed; only brain_context_block is used\nActionStore = gather_evidence = render_evidence = None  # noqa: N816\n",
     "RevenueOS uses brain_context_block only; pulse's project store and strategy engine are not vendored"),
    ("src/revenueos/vendor/talon/quotations.py", r"from talon import html_quotations", "from . import html_quotations", "package relocation"),
    ("src/revenueos/vendor/talon/quotations.py", r"from talon\.utils import", "from .utils import", "package relocation"),
    ("src/revenueos/vendor/talon/html_quotations.py", r"from talon\.utils import", "from .utils import", "package relocation"),
    ("src/revenueos/vendor/talon/utils.py", r"from talon\.constants import", "from .constants import", "package relocation"),
]

# Skill files whose telemetry preamble phones home; removed on copy (see NOTICE.md).
TELEMETRY_BLOCK = re.compile(
    r"(?ms)^```(?:bash|sh)?\n(?:[^\n]*\n)*?python3 telemetry/telemetry_init\.py[^\n]*\n```\n+"
    r"(?:> \*\*Privacy:\*\*[^\n]*\n+)?"
)


def load_manifest() -> dict[str, dict[str, str]]:
    with (UP / "MANIFEST.tsv").open() as fh:
        return {row["dir"]: row for row in csv.DictReader(fh, delimiter="\t")}


def rm(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def copy_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=IGNORE, dirs_exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sanitize_markdown(root: Path) -> list[str]:
    """Drop upstream authors' donation lines from every vendored skill/agent file (see revenueos.sanitize)."""
    sys.path.insert(0, str(ROOT / "src"))
    from revenueos.sanitize import strip_solicitations  # noqa: E402

    changed = []
    for base in ("skills", "agents", "capabilities"):
        for path in (root / base).rglob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            new, n = strip_solicitations(text)
            if n:
                path.write_text(new, encoding="utf-8")
                changed.append(f"{path.relative_to(root)} (-{n})")
    return changed


def strip_telemetry(skill_md: Path) -> bool:
    text = skill_md.read_text(encoding="utf-8")
    new = TELEMETRY_BLOCK.sub("", text)
    if new != text:
        skill_md.write_text(new, encoding="utf-8")
        return True
    return False


def main() -> int:
    if "--sanitize" in sys.argv:
        changed = sanitize_markdown(ROOT)
        print(f"sanitised {len(changed)} file(s)")
        for c in changed:
            print("  ", c)
        return 0
    manifest = load_manifest()
    record: dict[str, list[dict[str, str]]] = {}
    destinations_cleared: set[str] = set()

    for upstream, src_spec, dst_spec, mode in VENDOR_MAP:
        meta = manifest[upstream]
        if meta["license"] not in ALLOWED_LICENSES:
            sys.exit(f"refusing to vendor {upstream}: license {meta['license']!r} is not in {sorted(ALLOWED_LICENSES)}")
        base = UP / upstream
        if not base.exists():
            sys.exit(f"{base} missing — run scripts/fetch-upstream.sh first")
        dst = ROOT / dst_spec
        if mode != "overlay" and dst_spec not in destinations_cleared:
            rm(dst)
            destinations_cleared.add(dst_spec)

        if mode == "tree":
            copy_tree(base / src_spec, dst)
        elif mode == "overlay":
            copy_tree(base / src_spec, dst)  # dirs_exist_ok: later entries win
        elif mode == "file":
            copy_file(base / src_spec, dst)
        elif mode == "glob":
            for d in sorted(base.glob(src_spec)):
                if d.is_dir() and not d.name.startswith("."):
                    copy_tree(d, dst / d.name)
        elif mode == "mdglob":
            for f in sorted(base.glob(src_spec)):
                if f.name.upper() in {"INDEX.MD", "README.MD", "OTHER-SKILLS.MD"}:
                    continue
                copy_file(f, dst / f.stem / "SKILL.md")
        elif mode == "dirs":
            for name in src_spec.split(","):
                copy_tree(base / name, dst / name)
        elif mode == "skilldirs":
            for f in sorted(base.glob(src_spec)):
                copy_tree(f.parent, dst / f.parent.name)
        else:
            sys.exit(f"unknown mode {mode}")

        record.setdefault(upstream, []).append({"src": src_spec, "dst": dst_spec, "mode": mode})

    for rel, pattern, repl, _why in PATCHES:
        p = ROOT / rel
        text = p.read_text(encoding="utf-8")
        new = re.sub(pattern, repl, text)
        if new == text:
            sys.exit(f"patch did not apply: {rel} /{pattern}/")
        p.write_text(new, encoding="utf-8")

    stripped = [str(p.relative_to(ROOT)) for p in (ROOT / "skills" / "pipelines").rglob("SKILL.md") if strip_telemetry(p)]
    sanitised = sanitize_markdown(ROOT)
    print(f"sanitised {len(sanitised)} vendored file(s) (donation lines removed)")
    for junk in ["skills/pipelines/telemetry", "skills/pipelines/security", "skills/pipelines/eval"]:
        rm(ROOT / junk)

    # Third-party licence texts travel with the code.
    lic_dir = ROOT / "THIRD_PARTY_LICENSES"
    rm(lic_dir)
    lic_dir.mkdir()
    for upstream in record:
        for cand in (UP / upstream).iterdir():
            if cand.is_file() and cand.name.lower().startswith("licen"):
                copy_file(cand, lic_dir / f"{upstream}{cand.suffix or '.txt'}")
                break

    out = {
        "generated_by": "scripts/vendor.py",
        "sources": {
            u: {"repo": manifest[u]["repo"], "commit": manifest[u]["commit"], "license": manifest[u]["license"], "entries": entries}
            for u, entries in record.items()
        },
        "patches": [{"file": f, "pattern": p, "replacement": r, "why": w} for f, p, r, w in PATCHES],
        "telemetry_stripped_from": stripped,
        "excluded": {
            "adkit__ads-skills": "proprietary AdKit licence forbids bundling into other skill libraries",
            "eracle__OpenOutreach": "GPL-3.0; used only across a process boundary (services/openoutreach)",
            "arkitekt-ai__OpenCRM": "no LICENSE file (README claims MIT); reference only",
            "kevinbadi__ai-os-skills": "no LICENSE file; reference only",
            "erron-ai__marketing-skills": "no LICENSE file; the MIT superset of its CLIs is vendored from coreyhaines31/scayver",
            "astDeniss__business-skills": "no LICENSE file; reference only",
            "kevinbadi__kairos templates/": "CreatorOS social-media playbooks; not applicable",
        },
    }
    (ROOT / "VENDOR.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"vendored {sum(len(v) for v in record.values())} entries from {len(record)} upstreams; "
          f"stripped telemetry from {len(stripped)} skill files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
