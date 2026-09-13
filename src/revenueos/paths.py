"""Workspace layout. One RevenueOS install == one workspace directory (this repo, or a
copy of it) — the same single-workspace model the vendored Kairos worker uses."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent / "bundle"
HOME_WORKSPACE = Path(os.environ.get("REVENUEOS_HOME", "~/.revenueos")).expanduser()
WORKSPACE_DIRS = ("company-context", "learning-loop", "methodology", "playbooks", "skills", "agents", "tools",
                  "capabilities", "website", "orchestrator")


def _is_workspace(p: Path) -> bool:
    return (p / "company-context" / "manifest.json").exists() and (p / "skills").is_dir()


def materialise(dest: Path, source: Path = BUNDLE) -> Path:
    """Create a workspace at `dest` from the template shipped inside the wheel (or a checkout)."""
    import shutil

    dest.mkdir(parents=True, exist_ok=True)
    for name in WORKSPACE_DIRS:
        src = source / name
        if src.is_dir() and not (dest / name).exists():
            shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".DS_Store"))
    for name in ("VENDOR.json", "NOTICE.md"):
        if (source / name).is_file() and not (dest / name).exists():
            shutil.copy2(source / name, dest / name)
    (dest / "data").mkdir(exist_ok=True)
    if (source / "data" / "automations.json").is_file() and not (dest / "data" / "automations.json").exists():
        shutil.copy2(source / "data" / "automations.json", dest / "data" / "automations.json")
    return dest


def is_workspace(path: Path) -> bool:
    """Public form of the marker test used by find_root (an explicit --root must pass it)."""
    return _is_workspace(path.expanduser().resolve())


def find_root(start: Path | None = None) -> Path:
    """Resolve the workspace: $REVENUEOS_ROOT, else walk up from cwd, else the source checkout
    (editable installs / tests), else ~/.revenueos created from the bundled template."""
    env = os.environ.get("REVENUEOS_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if _is_workspace(candidate):
            return candidate
    checkout = Path(__file__).resolve().parents[2]
    if _is_workspace(checkout):
        return checkout
    if _is_workspace(HOME_WORKSPACE):
        return HOME_WORKSPACE
    if BUNDLE.is_dir():
        return materialise(HOME_WORKSPACE)
    return checkout


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def locate(cls, start: Path | None = None) -> Workspace:
        return cls(find_root(start))

    # Human-edited files (stay files forever)
    @property
    def company_context(self) -> Path:
        return self.root / "company-context"

    @property
    def learning_loop(self) -> Path:
        return self.root / "learning-loop"

    @property
    def corrections(self) -> Path:
        return self.learning_loop / "CORRECTIONS.md"

    @property
    def config(self) -> Path:
        """revenueos.yaml at the root; inside containers it lives on the data volume instead
        (a bind mount of a not-yet-existing file would create a directory, so we never mount it)."""
        root_cfg = self.root / "revenueos.yaml"
        if root_cfg.is_dir() or os.environ.get("REVENUEOS_CONFIG_IN_DATA"):
            return self.root / "data" / "revenueos.yaml"
        return root_cfg

    @property
    def skills(self) -> Path:
        return self.root / "skills"

    @property
    def agents(self) -> Path:
        return self.root / "agents"

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    # Machine state
    @property
    def data(self) -> Path:
        p = self.root / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db(self) -> Path:
        return self.data / "revenueos.db"

    @property
    def exports(self) -> Path:
        p = self.data / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def outputs(self) -> Path:
        p = self.data / "outputs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def reports(self) -> Path:
        p = self.data / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def automations(self) -> Path:
        return self.data / "automations.json"

    @property
    def registry_json(self) -> Path:
        return self.skills / "registry.json"

    @property
    def logs(self) -> Path:
        p = self.root / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p


def new_workspace(dest: Path, source: Path | None = None) -> Path:
    """A fresh customer workspace that shares an install's catalogue (skills, tools, orchestrator, website)
    by symlink but has its own canon, config, database and logs. `source` defaults to the located workspace."""
    import shutil

    src = source or find_root()
    dest = Path(dest).expanduser().resolve()
    if (dest / "company-context").exists():
        raise FileExistsError(f"{dest} already looks like a workspace")
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("company-context", "learning-loop"):
        shutil.copytree(src / name, dest / name)
    if (src / "VENDOR.json").is_file():
        shutil.copy2(src / "VENDOR.json", dest / "VENDOR.json")
    (dest / "data").mkdir(exist_ok=True)
    if (src / "data" / "automations.json").is_file():
        shutil.copy2(src / "data" / "automations.json", dest / "data" / "automations.json")
    for name in ("skills", "agents", "tools", "methodology", "playbooks", "website", "orchestrator"):
        if (src / name).exists() and not (dest / name).exists():
            os.symlink(src / name, dest / name)
    return dest
