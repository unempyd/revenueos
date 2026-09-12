"""The one LLM client in RevenueOS.

Two providers, picked automatically:
  * `anthropic`  — the official Anthropic SDK when ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
                   an `ant auth login` profile exists.
  * `claude-cli` — the logged-in Claude Code CLI (`claude -p`), the same path the vendored
                   Kairos worker uses for its Claude-plan mode.  Available wherever Claude Code
                   is installed and signed in; no API key required.
Force one with REVENUEOS_LLM=anthropic|claude-cli|off.

Exposes the tiny surface the vendored pulse-cmo relevance gate expects
(`await llm.complete([Message,...], temperature=, max_tokens=) -> str`) plus sync helpers.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = os.environ.get("REVENUEOS_MODEL", "claude-opus-5")
DEFAULT_EFFORT = os.environ.get("REVENUEOS_EFFORT", "medium")
CLI_TIMEOUT = int(os.environ.get("REVENUEOS_LLM_TIMEOUT", "900"))


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMUnavailable(RuntimeError):
    pass


class LLMRefused(RuntimeError):
    pass


def _split(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    convo = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return ("\n\n".join(system_parts) or None), convo


def _text_of(response: Any) -> str:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise LLMRefused(getattr(details, "explanation", None) or "request refused by safety classifier")
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()


def detect_provider() -> str | None:
    forced = os.environ.get("REVENUEOS_LLM")
    if forced == "off":
        return None
    if forced in ("anthropic", "claude-cli"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    profile_dir = os.path.expanduser("~/.config/anthropic")
    if os.path.isdir(profile_dir) and any(True for _ in os.scandir(profile_dir)):
        return "anthropic"
    if shutil.which("claude"):
        return "claude-cli"
    return None


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, provider: str | None = "auto") -> None:
        self.model = model
        self.effort = effort
        self.provider = detect_provider() if provider == "auto" else provider
        self._sync = None
        self._async = None

    @staticmethod
    def available() -> bool:
        return detect_provider() is not None

    # ── anthropic SDK ──
    def _kwargs(self, messages: list[Message], max_tokens: int) -> dict[str, Any]:
        system, convo = _split(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": convo,
            "output_config": {"effort": self.effort},
        }
        if system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        return kwargs

    # ── claude code CLI ──
    def _cli(self, messages: list[Message], max_tokens: int) -> str:
        system, convo = _split(messages)
        prompt = "\n\n".join(f"[{m['role']}]\n{m['content']}" if len(convo) > 1 else m["content"] for m in convo)
        # Skills are written for an agent with tools; this run has none, so say so up front —
        # otherwise the model attempts a tool call, hits --max-turns 1 and returns nothing.
        no_tools = ("\n\nRUNTIME NOTE: this run has no tools. Do not read files, run commands, search the web or ask for "
                    "input; produce the complete deliverable now from the context provided.")
        cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", ""]
        if os.environ.get("REVENUEOS_CLI_MODEL"):
            cmd += ["--model", os.environ["REVENUEOS_CLI_MODEL"]]
        cmd += ["--system-prompt", (system or "You are a careful assistant.") + no_tools]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT, check=False,
                              env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"})
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        result = str(data.get("result") or "").strip()
        if data.get("is_error"):
            raise LLMUnavailable(result[:400] or "claude -p reported an error")
        if data.get("stop_reason") == "tool_use" and not result:
            raise LLMUnavailable("claude -p stopped to call a tool although none are available; rephrase the task as a deliverable")
        if proc.returncode != 0 and not result:
            raise LLMUnavailable(f"claude -p failed ({proc.returncode}): {proc.stderr.strip()[-400:] or proc.stdout.strip()[-400:]}")
        return result[: max_tokens * 6]

    def complete_sync(self, messages: list[Message], *, temperature: float | None = None, max_tokens: int = 4096) -> str:
        if self.provider is None:
            raise LLMUnavailable("no LLM: set ANTHROPIC_API_KEY, run `ant auth login`, or sign in to Claude Code")
        if self.provider == "claude-cli":
            return self._cli(messages, max_tokens)
        if self._sync is None:
            import anthropic

            self._sync = anthropic.Anthropic()
        return _text_of(self._sync.messages.create(**self._kwargs(messages, max_tokens)))

    async def complete(self, messages: list[Message], *, temperature: float | None = None, max_tokens: int = 4096) -> str:
        if self.provider is None:
            raise LLMUnavailable("no LLM: set ANTHROPIC_API_KEY, run `ant auth login`, or sign in to Claude Code")
        if self.provider == "claude-cli":
            return await asyncio.to_thread(self._cli, messages, max_tokens)
        if self._async is None:
            import anthropic

            self._async = anthropic.AsyncAnthropic()
        return _text_of(await self._async.messages.create(**self._kwargs(messages, max_tokens)))

    def ask(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        return self.complete_sync([Message("system", system), Message("user", user)], max_tokens=max_tokens)


def maybe_llm() -> LLM | None:
    """An LLM if any provider exists, else None. Workers branch on this."""
    return LLM() if LLM.available() else None
