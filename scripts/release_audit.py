#!/usr/bin/env python3
"""Public-release audit for RevenueOS.

Scans a directory tree (default: the repo root, or a path argument) and prints
findings grouped by category with file:line. Exits non-zero if any HIGH
finding is present.

Categories:
  SECRETS                       (HIGH)
  PRIVATE DATA                  (HIGH)
  INTERNAL ARTEFACTS            (HIGH)
  UPSTREAM NAMES IN CUSTOMER-FACING TEXT   (MEDIUM)
  DEAD CODE                     (LOW)
  LICENCE                       (HIGH)

Dependency-free: standard library only.

    python3 scripts/release_audit.py [path] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories we never walk into for content scanning (too large / not ours /
# would recreate noise from the audit's own past runs). Internal-artefact
# checks for some of these are handled separately by *looking for* them.
SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "upstream"}

TEXT_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".sh", ".txt", ".html", ".css", ".sql", ".env",
    ".example",
}

MAX_TEXT_BYTES = 5_000_000  # skip binaries / huge files for line-scans


@dataclass
class Finding:
    category: str
    severity: str  # HIGH | MEDIUM | LOW
    path: str
    line: int | None
    message: str

    def loc(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else self.path


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, category: str, severity: str, path: Path, line: int | None, message: str) -> None:
        self.findings.append(Finding(category, severity, _relpath(path), line, message))

    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")


def _relpath(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(SCAN_ROOT.resolve()))
    except ValueError:
        return str(p)


SCAN_ROOT = ROOT  # set in main()


# ───────────────────────── helpers ─────────────────────────────────────────


def iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def iter_dirs(root: Path):
    for p in root.rglob("*"):
        if not p.is_dir():
            continue
        if any(part in SKIP_DIR_NAMES for part in p.relative_to(root).parts[:-1]):
            continue
        yield p


def read_text_lines(p: Path) -> list[str] | None:
    try:
        if p.stat().st_size > MAX_TEXT_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def find_line(lines: list[str], match_start: int, text: str) -> int:
    """Given a char offset into `text` (the joined content), return 1-based line."""
    return text.count("\n", 0, match_start) + 1


# ───────────────────────── SECRETS ─────────────────────────────────────────

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("Stripe live secret key", re.compile(r"sk_live_[A-Za-z0-9]{10,}")),
    ("Stripe test secret key", re.compile(r"sk_test_[A-Za-z0-9]{10,}")),
    ("Stripe webhook secret", re.compile(r"whsec_[A-Za-z0-9]{10,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Generic api_key literal", re.compile(r'(?i)\bapi[_-]?key\b\s*[:=]\s*["\']([^"\']{8,})["\']')),
    ("Generic password literal", re.compile(r'(?i)\bpassword\b\s*[:=]\s*["\']([^"\'$]{8,})["\']')),
    ("PEM private key header", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
]

SQLITE_EXTS = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"}


def check_secrets(report: Report, root: Path) -> None:
    for p in iter_files(root):
        rel = _relpath(p)

        # .env files anywhere (but not .env.example, which is an allowed template)
        name = p.name
        if (name == ".env" or (name.startswith(".env.") and name != ".env.example")):
            report.add("SECRETS", "HIGH", p, None, f".env file present: {rel}")
            continue

        if p.suffix in SQLITE_EXTS:
            report.add("SECRETS", "HIGH", p, None, f"SQLite database file present: {rel}")
            continue

        if name == "revenueos.yaml":
            lines = read_text_lines(p) or []
            for i, line in enumerate(lines, start=1):
                if re.search(r"(?i)\b(sender|smtp)\b", line):
                    report.add("SECRETS", "HIGH", p, i, f"revenueos.yaml contains sender/smtp config: {line.strip()[:80]}")

        if p.suffix not in TEXT_EXTS:
            continue
        lines = read_text_lines(p)
        if lines is None:
            continue
        text = "\n".join(lines)
        for label, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line_no = find_line(lines, m.start(), text)
                report.add("SECRETS", _severity_for(_relpath(p)), p, line_no, f"{label} literal found")

    # data/license.json, data/licenses.jsonl explicitly, wherever nested
    for p in iter_files(root):
        rel = _relpath(p)
        if rel.endswith(("data/license.json", "data/licenses.jsonl")):
            report.add("SECRETS", "HIGH", p, None, f"licence store file present: {rel}")

    # logs/ directory anywhere
    for d in iter_dirs(root):
        if d.name == "logs":
            has_files = any(d.rglob("*"))
            if has_files:
                report.add("SECRETS", "HIGH", d, None, f"logs/ directory present with contents: {_relpath(d)}")


# ───────────────────────── PRIVATE DATA ────────────────────────────────────

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_SUBSTRINGS = ("noreply", "anthropic")
ALLOWED_EMAIL_DOMAIN_SUFFIXES = (".invalid", ".example")
ALLOWED_EMAIL_DOMAINS = {"example.com", "github.com"}  # github noreply@github.com etc.
ALLOWED_EMAILS = {"hello@revenueos.com.au"}  # the project's published contact (SECURITY.md); intentionally public

PATH_RE = re.compile(r"(?:/Users/|/home/)[^\s\"'`)>\]]+")

IDENTITY_STRINGS = ("briankeeny", "Brian Keeny")  # the GitHub handle appears in public repo URLs by design

# This audit script's own source necessarily contains, as detection literals,
# the very substrings/patterns it looks for (e.g. the IDENTITY_STRINGS tuple
# above, or the INTERNAL_STRING_NEEDLES below). Since scripts/release_audit.py
# ships as part of every public export (build_public.py includes it) and is
# then scanned by itself, exempt it from the content-based identity-string,
# path-regex and internal-reference-string checks — filename/secret-pattern
# checks still apply to it like any other file.
SELF_RELPATH = "scripts/release_audit.py"


def _is_license_exempt(rel_path: str) -> bool:
    if rel_path.startswith("THIRD_PARTY_LICENSES/") or rel_path == "THIRD_PARTY_LICENSES":
        return True
    base = Path(rel_path).name
    return base in {"LICENSE", "LICENSE.txt", "LICENSE.md"}


def _email_allowed(addr: str) -> bool:
    if addr.lower() in ALLOWED_EMAILS:
        return True
    lower = addr.lower()
    if any(s in lower for s in ALLOWED_EMAIL_SUBSTRINGS):
        return True
    domain = lower.rsplit("@", 1)[-1]
    if domain in ALLOWED_EMAIL_DOMAINS:
        return True
    return any(domain.endswith(suf) for suf in ALLOWED_EMAIL_DOMAIN_SUFFIXES)


VENDORED_CONTENT_DIRS = ("src/revenueos/vendor/", "skills/", "agents/", "tools/", "methodology/", "playbooks/",
                         "THIRD_PARTY_LICENSES/", "company-context/", "learning-loop/")


def _severity_for(rel: str, default: str = "HIGH") -> str:
    """Test fixtures and vendored third-party content are reported, but never block a release."""
    if rel.startswith("tests/") or rel.startswith(VENDORED_CONTENT_DIRS):
        return "LOW"
    return default


def check_private_data(report: Report, root: Path) -> None:
    for p in iter_files(root):
        rel = _relpath(p)
        if _is_license_exempt(rel):
            continue
        if rel.startswith(VENDORED_CONTENT_DIRS) and not rel.startswith("company-context/"):
            continue  # third-party content: attribution emails and upstream paths are not our private data
        if p.suffix not in TEXT_EXTS and p.suffix != "":
            continue
        lines = read_text_lines(p)
        if lines is None:
            continue
        text = "\n".join(lines)

        for m in EMAIL_RE.finditer(text):
            addr = m.group(0)
            if _email_allowed(addr):
                continue
            line_no = find_line(lines, m.start(), text)
            report.add("PRIVATE DATA", _severity_for(rel), p, line_no, f"email address: {addr}")

        if rel == SELF_RELPATH:
            continue

        for m in PATH_RE.finditer(text):
            line_no = find_line(lines, m.start(), text)
            report.add("PRIVATE DATA", _severity_for(rel), p, line_no, f"absolute local path: {m.group(0)}")

        for needle in IDENTITY_STRINGS:
            search_text = text.lower()
            search_needle = needle.lower()
            pos = 0
            while True:
                pos = search_text.find(search_needle, pos)
                if pos == -1:
                    break
                line_no = find_line(lines, pos, text)
                report.add("PRIVATE DATA", _severity_for(rel), p, line_no, f"identity string: {needle!r}")
                pos += len(search_needle)


# ───────────────────────── INTERNAL ARTEFACTS ──────────────────────────────

INTERNAL_STRING_NEEDLES = ("claude-501", "session_01", "scratchpad", "Fabel", "Operating Mandate")

INTERNAL_FILENAME_SUFFIXES = (".eml.done",)


FINDER_DUP_RE = re.compile(r".+ \d+(\.[A-Za-z0-9]+)?$")


def check_finder_duplicates(report: Report, root: Path) -> None:
    """macOS Finder copy artefacts ('name 2.md', 'dir 3') must never ship."""
    for p in iter_files(root):
        rel = _relpath(p)
        if FINDER_DUP_RE.match(p.name) and not p.name.startswith("."):
            report.add("INTERNAL ARTEFACTS", "HIGH", p, None, f"Finder duplicate artefact: {rel}")


def check_internal_artefacts(report: Report, root: Path) -> None:
    check_finder_duplicates(report, root)
    # RESEARCH.md
    research_md = root / "RESEARCH.md"
    if research_md.exists():
        report.add("INTERNAL ARTEFACTS", "HIGH", research_md, None, "RESEARCH.md present")

    # research/ directory
    research_dir = root / "research"
    if research_dir.is_dir():
        report.add("INTERNAL ARTEFACTS", "HIGH", research_dir, None, "research/ directory present")

    # upstream/*/ clones (MANIFEST.tsv allowed)
    upstream_dir = root / "upstream"
    if upstream_dir.is_dir():
        for child in upstream_dir.iterdir():
            if child.is_dir():
                report.add("INTERNAL ARTEFACTS", "HIGH", child, None, f"upstream clone present: {_relpath(child)}")

    # .claude/ settings
    claude_dir = root / ".claude"
    if claude_dir.is_dir():
        report.add("INTERNAL ARTEFACTS", "HIGH", claude_dir, None, ".claude/ settings directory present")

    # data/demo-workspace
    demo_ws = root / "data" / "demo-workspace"
    if demo_ws.exists():
        report.add("INTERNAL ARTEFACTS", "HIGH", demo_ws, None, "data/demo-workspace present")

    # directory-name based: __pycache__, .pytest_cache, .DS_Store (dirs, rare), and files
    for d in iter_dirs(root):
        if d.name == "__pycache__":
            report.add("INTERNAL ARTEFACTS", "HIGH", d, None, f"__pycache__ present: {_relpath(d)}")
        elif d.name == ".pytest_cache":
            report.add("INTERNAL ARTEFACTS", "HIGH", d, None, f".pytest_cache present: {_relpath(d)}")

    for p in iter_files(root):
        rel = _relpath(p)
        if p.name == ".DS_Store":
            report.add("INTERNAL ARTEFACTS", "HIGH", p, None, f".DS_Store present: {rel}")
        if any(p.name.endswith(suf) for suf in INTERNAL_FILENAME_SUFFIXES):
            report.add("INTERNAL ARTEFACTS", "HIGH", p, None, f"stray artefact file present: {rel}")

        if rel == SELF_RELPATH:
            continue

        if p.suffix not in TEXT_EXTS:
            continue
        lines = read_text_lines(p)
        if lines is None:
            continue
        text = "\n".join(lines)
        for needle in INTERNAL_STRING_NEEDLES:
            pos = 0
            while True:
                pos = text.find(needle, pos)
                if pos == -1:
                    break
                line_no = find_line(lines, pos, text)
                report.add("INTERNAL ARTEFACTS", _severity_for(rel), p, line_no, f"internal reference: {needle!r}")
                pos += len(needle)


# ────────────────── UPSTREAM NAMES IN CUSTOMER-FACING TEXT ─────────────────

UPSTREAM_NAME_TERMS = [
    "kairos", "pulse-cmo", "pulse cmo", "markster", "claude-ads", "claude ads",
    "openclaudia", "kostja", "single brain", "singlebrain", "marketing-agent-os",
    "ai-cmo-operator", "coreyhaines", "scayver", "erron", "adkit", "openoutreach",
    "talon", "ai-sales-agent", "meshpilot",
]

CUSTOMER_FACING_TARGETS = [
    "README.md", "website", "docs", "capabilities", "mcp", "CONTRIBUTING.md", "SECURITY.md",
]

VENDORED_EXEMPT_DIRS = {"src/revenueos/vendor", "skills", "agents", "tools"}
VENDORED_EXEMPT_FILES = {"NOTICE.md", "VENDOR.json", "upstream/MANIFEST.tsv", "scripts/vendor.py"}


def _is_exempt_from_upstream_scan(rel_path: str) -> bool:
    if rel_path.startswith("THIRD_PARTY_LICENSES/") or rel_path == "THIRD_PARTY_LICENSES":
        return True
    if rel_path in VENDORED_EXEMPT_FILES:
        return True
    for d in VENDORED_EXEMPT_DIRS:
        if rel_path == d or rel_path.startswith(d + "/"):
            return True
    return False


def check_upstream_names(report: Report, root: Path) -> None:
    targets: list[Path] = []
    for name in CUSTOMER_FACING_TARGETS:
        p = root / name
        if p.is_file():
            targets.append(p)
        elif p.is_dir():
            targets.extend(f for f in iter_files(p) if f.is_file())

    for p in targets:
        rel = _relpath(p)
        if _is_exempt_from_upstream_scan(rel):
            continue
        if p.suffix not in TEXT_EXTS:
            continue
        lines = read_text_lines(p)
        if lines is None:
            continue
        text = "\n".join(lines)
        lower_text = text.lower()
        for term in UPSTREAM_NAME_TERMS:
            if term == "openoutreach" and rel in ("docs/integrations.md", "NOTICE.md"):
                continue
            pos = 0
            while True:
                pos = lower_text.find(term, pos)
                if pos == -1:
                    break
                line_no = find_line(lines, pos, text)
                report.add(
                    "UPSTREAM NAMES IN CUSTOMER-FACING TEXT", "MEDIUM", p, line_no,
                    f"upstream project name in customer-facing text: {term!r}",
                )
                pos += len(term)


# ───────────────────────── DEAD CODE ───────────────────────────────────────

TOP_LEVEL_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)


def check_dead_code(report: Report, root: Path) -> None:
    src_dir = root / "src" / "revenueos"
    if not src_dir.is_dir():
        return

    py_files = [
        p for p in src_dir.rglob("*.py")
        if "vendor" not in p.relative_to(src_dir).parts
    ]
    if not py_files:
        return

    # Build haystack of all source under src/ and tests/ (excluding vendor)
    search_dirs = []
    top_src = root / "src"
    if top_src.is_dir():
        search_dirs.append(top_src)
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        search_dirs.append(tests_dir)

    haystack_files = []
    for d in search_dirs:
        for p in d.rglob("*.py"):
            if "vendor" in p.relative_to(root).parts:
                continue
            haystack_files.append(p)

    haystack_cache: dict[Path, str] = {}
    for p in haystack_files:
        try:
            haystack_cache[p] = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            haystack_cache[p] = ""

    for p in py_files:
        try:
            own_text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in TOP_LEVEL_DEF_RE.finditer(own_text):
            fn = m.group(1)
            if fn.startswith("_") or fn in ("main", "__init__", "__new__"):
                continue
            # count occurrences of the name across the whole haystack;
            # more than 1 means it is referenced somewhere besides its own def.
            occurrences = 0
            for text in haystack_cache.values():
                occurrences += len(re.findall(r"\b" + re.escape(fn) + r"\b", text))
            if occurrences <= 1:
                line_no = find_line(own_text.splitlines(), m.start(), own_text)
                report.add(
                    "DEAD CODE", "LOW", p, line_no,
                    f"top-level function {fn!r} appears unreferenced outside its own definition",
                )


# ───────────────────────── LICENCE ─────────────────────────────────────────


def check_licence(report: Report, root: Path) -> None:
    vendor_json = root / "VENDOR.json"
    notice_md = root / "NOTICE.md"
    license_file = root / "LICENSE"
    tpl_dir = root / "THIRD_PARTY_LICENSES"

    if not license_file.exists():
        report.add("LICENCE", "HIGH", root, None, "LICENSE file missing at repo root")

    if not vendor_json.exists():
        report.add("LICENCE", "HIGH", vendor_json, None, "VENDOR.json missing")
        return

    try:
        data = json.loads(vendor_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        report.add("LICENCE", "HIGH", vendor_json, None, f"VENDOR.json unreadable/invalid JSON: {e}")
        return

    sources = data.get("sources", {})
    notice_text = ""
    if notice_md.exists():
        notice_text = notice_md.read_text(encoding="utf-8", errors="ignore")
    else:
        report.add("LICENCE", "HIGH", notice_md, None, "NOTICE.md missing")

    for source_name, entry in sources.items():
        expected = tpl_dir / f"{source_name}.txt"
        if not expected.exists():
            report.add(
                "LICENCE", "HIGH", tpl_dir, None,
                f"missing THIRD_PARTY_LICENSES/{source_name}.txt for VENDOR.json source {source_name!r}",
            )
        if notice_md.exists():
            repo_name = entry.get("repo", source_name)
            # Accept either the VENDOR.json key or the "org/repo" string appearing in NOTICE.md
            if repo_name not in notice_text and source_name not in notice_text:
                report.add(
                    "LICENCE", "HIGH", notice_md, None,
                    f"NOTICE.md does not mention VENDOR.json source repo {repo_name!r}",
                )


# ───────────────────────── report rendering ────────────────────────────────

CATEGORY_ORDER = [
    "SECRETS",
    "PRIVATE DATA",
    "INTERNAL ARTEFACTS",
    "UPSTREAM NAMES IN CUSTOMER-FACING TEXT",
    "DEAD CODE",
    "LICENCE",
]


def render_markdown(report: Report, scan_root: Path) -> str:
    lines = [f"# RevenueOS public-release audit — {scan_root}", ""]
    by_cat: dict[str, list[Finding]] = {c: [] for c in CATEGORY_ORDER}
    for f in report.findings:
        by_cat.setdefault(f.category, []).append(f)

    total = len(report.findings)
    high = sum(1 for f in report.findings if f.severity == "HIGH")
    medium = sum(1 for f in report.findings if f.severity == "MEDIUM")
    low = sum(1 for f in report.findings if f.severity == "LOW")

    lines.append(f"**Total findings: {total}** — HIGH: {high}, MEDIUM: {medium}, LOW: {low}")
    lines.append("")

    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        lines.append(f"## {cat} ({len(items)})")
        if not items:
            lines.append("")
            lines.append("_none_")
            lines.append("")
            continue
        for f in items:
            lines.append(f"- **{f.severity}** `{f.loc()}` — {f.message}")
        lines.append("")

    if high:
        lines.append(f"**RESULT: FAIL — {high} HIGH finding(s).**")
    else:
        lines.append("**RESULT: PASS — no HIGH findings.**")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    payload = {
        "findings": [
            {
                "category": f.category,
                "severity": f.severity,
                "path": f.path,
                "line": f.line,
                "message": f.message,
            }
            for f in report.findings
        ],
        "counts": {
            "total": len(report.findings),
            "HIGH": sum(1 for f in report.findings if f.severity == "HIGH"),
            "MEDIUM": sum(1 for f in report.findings if f.severity == "MEDIUM"),
            "LOW": sum(1 for f in report.findings if f.severity == "LOW"),
            "by_category": {
                cat: sum(1 for f in report.findings if f.category == cat)
                for cat in CATEGORY_ORDER
            },
        },
    }
    return json.dumps(payload, indent=2)


def run_audit(scan_root: Path) -> Report:
    global SCAN_ROOT
    SCAN_ROOT = scan_root
    report = Report()
    check_secrets(report, scan_root)
    check_private_data(report, scan_root)
    check_internal_artefacts(report, scan_root)
    check_upstream_names(report, scan_root)
    check_dead_code(report, scan_root)
    check_licence(report, scan_root)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=str(ROOT), help="directory tree to scan (default: repo root)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = parser.parse_args(argv)

    scan_root = Path(args.path).resolve()
    if not scan_root.is_dir():
        print(f"error: not a directory: {scan_root}", file=sys.stderr)
        return 2

    report = run_audit(scan_root)

    if args.json:
        print(render_json(report))
    else:
        print(render_markdown(report, scan_root))

    return 1 if report.high_count() > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
