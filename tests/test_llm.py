import json
import subprocess

import pytest

from revenueos import llm as llm_mod
from revenueos.llm import LLM, LLMUnavailable, Message, detect_provider


def test_provider_detection(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("REVENUEOS_LLM", "off")
    assert detect_provider() is None and not LLM.available()
    monkeypatch.setenv("REVENUEOS_LLM", "claude-cli")
    assert detect_provider() == "claude-cli"
    monkeypatch.delenv("REVENUEOS_LLM")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert detect_provider() == "anthropic"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(llm_mod.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda n: "/usr/local/bin/claude" if n == "claude" else None)
    assert detect_provider() == "claude-cli"
    monkeypatch.setattr(llm_mod.shutil, "which", lambda n: None)
    assert detect_provider() is None


def test_claude_cli_provider_parses_print_json(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"], calls["input"] = cmd, kw["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": "  ok  ", "is_error": False}), stderr="")

    monkeypatch.setattr(llm_mod.subprocess, "run", fake_run)
    out = LLM(provider="claude-cli").complete_sync([Message("system", "be brief"), Message("user", "Say ok")])
    assert out == "ok"
    assert calls["cmd"][:3] == ["claude", "-p", "--output-format"] and "--system-prompt" in calls["cmd"]
    sys_arg = calls["cmd"][calls["cmd"].index("--system-prompt") + 1]
    assert sys_arg.startswith("be brief") and "no tools" in sys_arg  # skills are told tools are unavailable in -p runs
    assert calls["input"] == "Say ok"


def test_claude_cli_failure_raises(monkeypatch):
    monkeypatch.setattr(llm_mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in"))
    with pytest.raises(LLMUnavailable, match="not logged in"):
        LLM(provider="claude-cli").ask("s", "u")


def test_no_provider_raises():
    with pytest.raises(LLMUnavailable):
        LLM(provider=None).ask("s", "u")
