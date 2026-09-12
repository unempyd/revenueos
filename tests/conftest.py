"""A throwaway workspace per test: real company-context/learning-loop copies, the real
skill catalogue linked in, a fresh SQLite file. Nothing touches the repo's own state."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

ANSWERS = {
    "company_name": "Acme Scheduling",
    "website": "https://acme-scheduling.example",
    "category": "appointment scheduling software for dental clinics",
    "what_we_do": "We fill empty dental chairs by automating appointment reminders and rebooking.",
    "primary_audience": "Practice managers at 2–10 chair dental clinics in the US",
    "pain_points": "- no-shows eat 15% of chair time\n- front desk spends hours on reminder calls",
    "core_offer": "Acme Recall — $149/month per location, 30-day free trial",
    "differentiators": "- two-way SMS rebooking, not one-way reminders\n- installs in an afternoon, no PMS migration",
    "competitors": "weave.com, nexhealth.com",
    "channels": "cold email, seo, linkedin",
    "tone": "clear, direct, warm",
    "sender_name": "Sam Rivera",
    "sender_email": "sam@acme-scheduling.example",
    "booking_url": "https://cal.example/acme/15min",
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copytree(REPO / "company-context", root / "company-context")
    shutil.copytree(REPO / "learning-loop", root / "learning-loop")
    for linked in ("skills", "agents", "tools", "orchestrator"):
        if (REPO / linked).exists():
            os.symlink(REPO / linked, root / linked)
    shutil.copy2(REPO / "VENDOR.json", root / "VENDOR.json")
    (root / "data").mkdir()
    monkeypatch.setenv("REVENUEOS_ROOT", str(root))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("REVENUEOS_LLM", "off")
    from revenueos.paths import Workspace

    ws = Workspace(root)
    # the registry is expensive to rebuild; reuse the repo's if present
    if (REPO / "skills" / "registry.json").exists():
        pass  # symlinked skills/ already contains registry.json
    return ws


@pytest.fixture
def onboarded(workspace):
    from revenueos.context import BusinessContext
    from revenueos.vendor.sales_agent.settings import settings

    ctx = BusinessContext.load(workspace)
    ctx.onboard(ANSWERS)
    settings.reload()
    return ctx


@pytest.fixture
def store(workspace):
    from revenueos.store import Store

    return Store(workspace.db)


@pytest.fixture
def answers_file(tmp_path: Path) -> Path:
    p = tmp_path / "answers.json"
    p.write_text(json.dumps(ANSWERS))
    return p
