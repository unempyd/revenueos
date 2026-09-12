"""Unified skill / agent / tool registry over the vendored libraries.

The 16 upstream libraries use six on-disk layouts and five frontmatter dialects; the only
universal keys are `name` and `description`.  This indexer normalises them to one record
(slug from the directory name, source = first path component under skills/) and writes
skills/registry.json.  Marketing-Agent-OS's catalog.json and Marketing OS's MANIFEST.json
are folded in for domains/layers when present.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import Workspace

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
ENV_RE = re.compile(r"process\.env\.([A-Z0-9_]+)")
EXCLUDE_DIRS = {"upstreams", "node_modules", ".git", "__pycache__"}


@dataclass
class SkillRecord:
    slug: str
    source: str
    path: str  # relative to workspace root
    name: str
    description: str
    domain: str | None = None
    version: str | None = None
    license: str | None = None
    has_scripts: bool = False
    script_files: list[str] = field(default_factory=list)
    frontmatter_keys: list[str] = field(default_factory=list)


@dataclass
class AgentRecord:
    slug: str
    source: str
    path: str
    name: str
    description: str


@dataclass
class ToolRecord:
    slug: str
    path: str
    env_vars: list[str]
    guide: str | None


def parse_frontmatter(text: str) -> dict[str, Any]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_heading(text: str) -> str | None:
    m = re.search(r"(?m)^#\s+(.+)$", text)
    return m.group(1).strip() if m else None


def _first_paragraph(text: str) -> str:
    body = FRONTMATTER.sub("", text)
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if p and not p.startswith(("#", "```", "<", "|", "-", "*", ">")):
            return re.sub(r"\s+", " ", p)[:400]
    return ""


def _license_map(root: Path) -> dict[str, str]:
    vj = root / "VENDOR.json"
    if not vj.exists():  # a workspace copy without the assembly record: fall back to the package's own
        vj = Path(__file__).resolve().parents[2] / "VENDOR.json"
    if not vj.exists():
        return {}
    data = json.loads(vj.read_text())
    out: dict[str, str] = {}
    for meta in data.get("sources", {}).values():
        for entry in meta.get("entries", []):
            dst = entry["dst"]
            if dst.startswith("skills/") or dst.startswith("agents/"):
                out[dst.split("/")[1]] = meta["license"]
    return out


def _domain_hints(root: Path) -> dict[str, str]:
    hints: dict[str, str] = {}
    cat = root / "skills" / "core" / "catalog.json"
    if cat.exists():
        for s in json.loads(cat.read_text()).get("skills", []):
            if s.get("name") and s.get("domain"):
                hints[f"core/{s['name']}"] = s["domain"]
    man = root / "skills" / "operations" / "MANIFEST.json"
    if man.exists():
        for layer in json.loads(man.read_text()).get("layers", []):
            for s in layer.get("skills", []):
                hints[f"operations/{s}"] = layer.get("id", "")
    return hints


def iter_skill_files(skills_dir: Path) -> Iterable[Path]:
    for p in sorted(skills_dir.rglob("SKILL.md")):
        if any(part in EXCLUDE_DIRS for part in p.relative_to(skills_dir).parts):
            continue
        yield p


def index_skills(ws: Workspace) -> list[SkillRecord]:
    licenses = _license_map(ws.root)
    domains = _domain_hints(ws.root)
    records: list[SkillRecord] = []
    for skill_md in iter_skill_files(ws.skills):
        rel = skill_md.relative_to(ws.skills)
        source = rel.parts[0]
        skill_dir = skill_md.parent
        slug = skill_dir.name if skill_dir != ws.skills / source else source
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        scripts = [
            str(f.relative_to(skill_dir))
            for f in skill_dir.rglob("*")
            if f.is_file() and f.suffix in {".py", ".js", ".mjs", ".sh", ".ts"} and "node_modules" not in f.parts
        ]
        name = str(fm.get("name") or _first_heading(text) or slug)
        desc = str(fm.get("description") or _first_paragraph(text)).strip()
        records.append(
            SkillRecord(
                slug=slug,
                source=source,
                path=str(skill_md.relative_to(ws.root)),
                name=name,
                description=re.sub(r"\s+", " ", desc)[:600],
                domain=domains.get(f"{source}/{slug}") or meta.get("domain") or fm.get("category"),
                version=str(meta.get("version") or fm.get("version") or "") or None,
                license=licenses.get(source),
                has_scripts=bool(scripts),
                script_files=scripts[:20],
                frontmatter_keys=sorted(fm.keys()),
            )
        )
    return records


def index_agents(ws: Workspace) -> list[AgentRecord]:
    out: list[AgentRecord] = []
    if not ws.agents.exists():
        return out
    for md in sorted(ws.agents.rglob("*.md")):
        if md.name.upper() in {"README.MD", "INDEX.MD"}:
            continue
        rel = md.relative_to(ws.agents)
        text = md.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        out.append(
            AgentRecord(
                slug=md.stem,
                source=rel.parts[0],
                path=str(md.relative_to(ws.root)),
                name=str(fm.get("name") or fm.get("display_name") or _first_heading(text) or md.stem).strip('"'),
                description=re.sub(r"\s+", " ", str(fm.get("description") or _first_paragraph(text)))[:400],
            )
        )
    return out


def index_tools(ws: Workspace) -> list[ToolRecord]:
    clis = ws.tools / "clis"
    guides = ws.tools / "integrations"
    out: list[ToolRecord] = []
    if not clis.exists():
        return out
    for js in sorted(clis.glob("*.js")):
        text = js.read_text(encoding="utf-8", errors="replace")
        env = sorted(set(ENV_RE.findall(text)))
        guide = guides / f"{js.stem}.md"
        out.append(ToolRecord(slug=js.stem, path=str(js.relative_to(ws.root)), env_vars=env,
                              guide=str(guide.relative_to(ws.root)) if guide.exists() else None))
    return out


def build_registry(ws: Workspace) -> dict[str, Any]:
    skills = index_skills(ws)
    agents = index_agents(ws)
    tools = index_tools(ws)
    reg = {
        "skills": [asdict(s) for s in skills],
        "agents": [asdict(a) for a in agents],
        "tools": [asdict(t) for t in tools],
        "counts": {
            "skills": len(skills),
            "skills_with_code": sum(1 for s in skills if s.has_scripts),
            "agents": len(agents),
            "tools": len(tools),
            "by_source": _count(s.source for s in skills),
        },
    }
    ws.registry_json.write_text(json.dumps(reg, indent=1) + "\n", encoding="utf-8")
    return reg


def load_registry(ws: Workspace) -> dict[str, Any]:
    if not ws.registry_json.exists():
        return build_registry(ws)
    return json.loads(ws.registry_json.read_text(encoding="utf-8"))


def search_skills(ws: Workspace, query: str, limit: int = 20, source: str | None = None) -> list[dict[str, Any]]:
    """Cheap lexical ranking: slug match > name match > description term hits."""
    terms = [t for t in re.split(r"\W+", query.lower()) if t]
    scored: list[tuple[int, dict[str, Any]]] = []
    for s in load_registry(ws)["skills"]:
        if source and s["source"] != source:
            continue
        hay_slug, hay_name, hay_desc = s["slug"].lower(), s["name"].lower(), s["description"].lower()
        score = 0
        for t in terms:
            if t in hay_slug:
                score += 5
            if t in hay_name:
                score += 3
            score += hay_desc.count(t)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1]["slug"]))
    return [s for _, s in scored[:limit]]


def skill_text(ws: Workspace, source: str, slug: str) -> str:
    for s in load_registry(ws)["skills"]:
        if s["source"] == source and s["slug"] == slug:
            return (ws.root / s["path"]).read_text(encoding="utf-8", errors="replace")
    raise KeyError(f"{source}/{slug}")


def _count(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return dict(sorted(out.items()))
