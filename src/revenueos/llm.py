"""The one LLM client in RevenueOS — a routing layer, not a single provider.

Underneath every worker, role and executor sits an ordered *chain* of providers.  A call is
tried against the first available provider; if it fails for a reason that is about capacity
rather than about the request (rate limit, quota, overload, timeout, connection error, 5xx)
the call moves down the chain automatically.  A bad request, an authentication failure or a
refusal is a real answer about this request and is never retried elsewhere — retrying those
somewhere else only hides the misconfiguration.  The business never sees any of this: the
public surface (`LLM`, `maybe_llm()`, `.ask()`, `.complete_sync()`, `.complete()`) is unchanged.

Providers:
  * `anthropic`         — the official Anthropic SDK (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
                          an `ant auth login` profile).
  * `claude-cli`        — the signed-in Claude Code CLI (`claude -p`), no API key required.
  * `openai-compatible` — any OpenAI-compatible endpoint the operator points RevenueOS at with
                          REVENUEOS_LLM_BASE_URL + REVENUEOS_LLM_MODEL (+ REVENUEOS_LLM_API_KEY):
                          a local router, a self-hosted model, a gateway. No product assumed.

REVENUEOS_LLM pins the chain: `off` disables everything, a single name forces one provider,
a comma-separated list (`anthropic,claude-cli`) fixes the order.  Unset = auto-detect.
REVENUEOS_LLM_RETRIES (attempts per provider, default 2), REVENUEOS_LLM_BUDGET (total wall-clock
seconds for one call across the whole chain, default 1200) bound the work so a worker cannot hang.

Observability: every attempt records an `llm_call` metric (provider, model, latency, token counts
where the provider reports them, outcome, exception class).  **Prompt and response text are never
logged, never recorded and never stored** — not in metrics, not in error messages we persist; only
the provider's own transport errors carry text, and those stay in the raised exception.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = os.environ.get("REVENUEOS_MODEL", "claude-opus-5")
DEFAULT_EFFORT = os.environ.get("REVENUEOS_EFFORT", "medium")
CLI_TIMEOUT = int(os.environ.get("REVENUEOS_LLM_TIMEOUT", "900"))

PROVIDERS = ("anthropic", "claude-cli", "openai-compatible")
_NO_PROVIDER = ("no LLM: set ANTHROPIC_API_KEY, run `ant auth login`, sign in to Claude Code, "
                "or point REVENUEOS_LLM_BASE_URL at an OpenAI-compatible endpoint")


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMUnavailable(RuntimeError):
    pass


class LLMRefused(RuntimeError):
    pass


@dataclass
class Reply:
    """One provider's answer. Token counts are None when the provider does not report them."""
    text: str
    provider: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None


def _split(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    convo = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return ("\n\n".join(system_parts) or None), convo


def _text_of(response: Any) -> str:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise LLMRefused(getattr(details, "explanation", None) or "request refused by safety classifier")
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()


# ── detection ───────────────────────────────────────────────────────────────────────────────
def _anthropic_ready() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    profile_dir = os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(profile_dir) and any(True for _ in os.scandir(profile_dir))


def _cli_ready() -> bool:
    return bool(shutil.which("claude"))


def _openai_config() -> tuple[str, str, str] | None:
    base = (os.environ.get("REVENUEOS_LLM_BASE_URL") or "").rstrip("/")
    model = os.environ.get("REVENUEOS_LLM_MODEL") or ""
    if not base or not model:
        return None
    return base, model, os.environ.get("REVENUEOS_LLM_API_KEY") or ""


def detect_chain() -> list[str]:
    """The ordered providers to try. REVENUEOS_LLM pins it (a name, a comma-separated list, or
    `off`); unset auto-detects: first-party first, operator endpoint last."""
    forced = (os.environ.get("REVENUEOS_LLM") or "").strip()
    if forced:
        names = [n.strip() for n in forced.split(",") if n.strip()]
        if "off" in names:
            return []
        return [n for n in names if n in PROVIDERS]
    chain = []
    if _anthropic_ready():
        chain.append("anthropic")
    if _cli_ready():
        chain.append("claude-cli")
    if _openai_config():
        chain.append("openai-compatible")
    return chain


def detect_provider() -> str | None:
    """The first provider in the chain — the historical single-provider answer."""
    chain = detect_chain()
    return chain[0] if chain else None


def _reachable(base: str, timeout: float = 1.5) -> bool:
    parsed = urlparse(base)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not parsed.hostname:
        return False
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def chain_status() -> list[dict[str, Any]]:
    """What `revenueos doctor` prints: the chain in order and whether each end is reachable.
    Only local/TCP checks — no model is called, nothing is spent."""
    out = []
    for name in detect_chain():
        if name == "anthropic":
            ready = _anthropic_ready()
            detail = "ANTHROPIC_API_KEY / ant auth login" + ("" if ready else " (missing)")
            model = os.environ.get("REVENUEOS_MODEL", DEFAULT_MODEL)
        elif name == "claude-cli":
            ready = _cli_ready()
            detail = shutil.which("claude") or "claude not on PATH"
            model = os.environ.get("REVENUEOS_CLI_MODEL", "signed-in default")
        else:
            cfg = _openai_config()
            ready = bool(cfg) and _reachable(cfg[0])
            detail = (cfg[0] if cfg else "REVENUEOS_LLM_BASE_URL unset") + ("" if ready else " (unreachable)")
            model = cfg[1] if cfg else "?"
        out.append({"name": name, "model": model, "detail": detail, "ready": ready})
    return out


# ── failure classification (what may be failed over, and what must not be) ───────────────────
_FATAL_TYPES = ("badrequest", "authentication", "permissiondenied", "notfound", "invalidrequest",
                "unprocessable", "llmrefused")
_TRANSIENT_TYPES = ("ratelimit", "overload", "timeout", "connect", "internalserver", "serviceunavailable",
                    "apistatus", "remoteprotocol", "readerror", "writeerror", "poolerror", "oserror")
_FATAL_TEXT = ("invalid api key", "invalid x-api-key", "authentication", "unauthorized", "not logged in",
               "please run /login", "permission denied", "invalid_request_error", "invalid request",
               "model not found", "does not exist", "credit balance")
_TRANSIENT_TEXT = ("rate limit", "rate_limit", "429", "session limit", "usage limit", "quota", "overloaded",
                   "capacity", "timeout", "timed out", "connection", "connect error", "temporarily",
                   "try again", "server error", "service unavailable", "temporarily unavailable")
# 5xx only when the text reads like a status, so a "max_tokens 500" never looks like an outage.
_TRANSIENT_CODES = ("500", "502", "503", "504", "529")


def _status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


_UNUSABLE_TYPES = ("authentication", "permissiondenied")
_UNUSABLE_TEXT = ("invalid api key", "invalid x-api-key", "authentication", "unauthorized",
                  "not logged in", "please run /login", "permission denied", "credit balance")


def classify_failure(exc: BaseException) -> str:
    """One of three, and the distinction matters for an unattended run.

    "transient"  capacity: rate limit, overload, timeout, outage. Retry, then fail over.
    "unusable"   THIS PROVIDER cannot serve anyone: a stale key, a revoked token, no credit. The
                 request is fine, so fail over — but record it distinctly, because a provider that
                 is broken rather than busy needs the operator's attention even when the run
                 succeeds elsewhere. A stale key must never take down a workspace that has a
                 second working provider.
    "fatal"      THE REQUEST is wrong, or was answered: a bad request, an unknown model, a refusal.
                 Every provider would reject it identically, so stop and surface it.

    Order matters: an HTTP status is the most reliable signal, then the exception class, then the
    message. Anything unrecognised is fatal on purpose — a new bug must not look like an outage."""
    if isinstance(exc, LLMRefused):
        return "fatal"
    status = _status_of(exc)
    if status is not None:
        if status in (408, 409, 425, 429) or status >= 500:
            return "transient"
        if status in (401, 402, 403):
            # this provider's credential is stale, revoked or out of credit: it can serve nobody,
            # but the request itself is fine, so the chain continues past it.
            return "unusable"
        return "fatal"
    if isinstance(exc, FileNotFoundError):
        # the provider's binary is not installed here: skip to the next one rather than abort the
        # chain — a provider that is absent is not a wrong request.
        return "transient"
    name = type(exc).__name__.lower()
    if any(k in name for k in _UNUSABLE_TYPES):
        return "unusable"
    if any(k in name for k in _FATAL_TYPES):
        return "fatal"
    if isinstance(exc, (TimeoutError, ConnectionError, subprocess.TimeoutExpired, socket.timeout)):
        return "transient"
    if any(k in name for k in _TRANSIENT_TYPES):
        return "transient"
    text = str(exc).lower()
    if any(k in text for k in _UNUSABLE_TEXT):
        return "unusable"
    if any(k in text for k in _FATAL_TEXT):
        return "fatal"
    if any(k in text for k in _TRANSIENT_TEXT):
        return "transient"
    if any(k in text for k in ("error", "status", "http")) and any(c in text for c in _TRANSIENT_CODES):
        return "transient"
    return "fatal"


# ── observability (never prompt or response text) ────────────────────────────────────────────
def _metric_store() -> Any | None:
    """The workspace store, if one already exists. Best effort: metrics must never break a call,
    and we never create a workspace or a database just to record one."""
    if os.environ.get("REVENUEOS_LLM_METRICS") == "0":
        return None
    try:
        from .paths import Workspace
        from .store import Store

        db = Workspace.locate().db
        return Store(db) if db.exists() else None
    except Exception:
        return None


def record_call(provider: str, model: str, ms: float, outcome: str, *, attempt: int = 1,
                tokens_in: int | None = None, tokens_out: int | None = None,
                error_type: str | None = None) -> None:
    """Record one attempt as an `llm_call` metric (value = latency in ms).

    Deliberately takes no prompt, no response and no provider error message: nothing derived from
    the conversation is ever written to the database. Only the provider name, the model id, the
    latency, the token counts the provider itself reported, the outcome and the exception class."""
    store = _metric_store()
    if store is None:
        return
    dims: dict[str, Any] = {"provider": provider, "model": model, "outcome": outcome, "attempt": attempt}
    if tokens_in is not None:
        dims["tokens_in"] = tokens_in
    if tokens_out is not None:
        dims["tokens_out"] = tokens_out
    if error_type:
        dims["error_type"] = error_type
    try:
        store.record_metric("llm_call", round(ms, 1), **dims)
    except Exception:
        pass


def _retries() -> int:
    try:
        return max(1, int(os.environ.get("REVENUEOS_LLM_RETRIES", "2")))
    except ValueError:
        return 2


def _budget() -> float:
    try:
        return max(0.0, float(os.environ.get("REVENUEOS_LLM_BUDGET", "1200")))
    except ValueError:
        return 1200.0


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, provider: str | None = "auto") -> None:
        self.model = model
        self.effort = effort
        if provider == "auto":
            self.chain = detect_chain()
        elif provider is None:
            self.chain = []
        else:
            self.chain = [n.strip() for n in str(provider).split(",") if n.strip() and n.strip() != "off"]
        self._sync = None

    @property
    def provider(self) -> str | None:
        """The provider a call starts with. Kept for callers that only ever asked "which model?"."""
        return self.chain[0] if self.chain else None

    @staticmethod
    def available() -> bool:
        return bool(detect_chain())

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

    def _anthropic(self, messages: list[Message], max_tokens: int) -> Reply:
        if self._sync is None:
            import anthropic

            self._sync = anthropic.Anthropic()
        response = self._sync.messages.create(**self._kwargs(messages, max_tokens))
        usage = getattr(response, "usage", None)
        return Reply(_text_of(response), "anthropic", self.model,
                     getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))

    # ── claude code CLI ──
    def _cli(self, messages: list[Message], max_tokens: int) -> Reply:
        system, convo = _split(messages)
        prompt = "\n\n".join(f"[{m['role']}]\n{m['content']}" if len(convo) > 1 else m["content"] for m in convo)
        # Skills are written for an agent with tools; this run has none, so say so up front —
        # otherwise the model attempts a tool call, hits --max-turns 1 and returns nothing.
        no_tools = ("\n\nRUNTIME NOTE: this run has no tools. Do not read files, run commands, search the web or ask for "
                    "input; produce the complete deliverable now from the context provided.")
        cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", ""]
        cli_model = os.environ.get("REVENUEOS_CLI_MODEL")
        if cli_model:
            cmd += ["--model", cli_model]
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
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return Reply(result[: max_tokens * 6], "claude-cli", cli_model or "claude-cli",
                     usage.get("input_tokens"), usage.get("output_tokens"))

    # ── any OpenAI-compatible endpoint the operator configured ──
    def _openai(self, messages: list[Message], max_tokens: int) -> Reply:
        import httpx

        cfg = _openai_config()
        if cfg is None:
            raise LLMUnavailable("openai-compatible endpoint not configured (REVENUEOS_LLM_BASE_URL + REVENUEOS_LLM_MODEL)")
        base, model, key = cfg
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        timeout = float(os.environ.get("REVENUEOS_LLM_HTTP_TIMEOUT", "300"))
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        if (choice.get("finish_reason") or "") == "content_filter":
            raise LLMRefused("request refused by the endpoint's content filter")
        text = str((choice.get("message") or {}).get("content") or "").strip()
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return Reply(text, "openai-compatible", data.get("model") or model,
                     usage.get("prompt_tokens"), usage.get("completion_tokens"))

    def _invoke(self, name: str, messages: list[Message], max_tokens: int) -> Reply:
        if name == "claude-cli":
            return self._cli(messages, max_tokens)
        if name == "openai-compatible":
            return self._openai(messages, max_tokens)
        if name == "anthropic":
            return self._anthropic(messages, max_tokens)
        raise LLMUnavailable(f"unknown provider {name!r}; known: {', '.join(PROVIDERS)}")

    def _model_label(self, name: str) -> str:
        if name == "claude-cli":
            return os.environ.get("REVENUEOS_CLI_MODEL") or "claude-cli"
        if name == "openai-compatible":
            cfg = _openai_config()
            return cfg[1] if cfg else "unconfigured"
        return self.model

    # ── the chain ──
    def _run_chain(self, messages: list[Message], max_tokens: int) -> Reply:
        """Try each provider in order; fail over only on capacity failures; stop on the first
        fatal one; give up when the wall-clock budget is gone. Raises LLMUnavailable naming
        every provider and why it did not answer."""
        if not self.chain:
            raise LLMUnavailable(_NO_PROVIDER)
        started = time.monotonic()
        budget, attempts_per_provider = _budget(), _retries()
        tried: list[str] = []

        def give_up(index: int, note: str) -> LLMUnavailable:
            rest = [f"{n}: not tried ({note})" for n in self.chain[index:]]
            return LLMUnavailable("no model completed the request — " + "; ".join(tried + rest))

        for index, name in enumerate(self.chain):
            spent = time.monotonic() - started
            if index > 0 and spent >= budget:
                raise give_up(index, f"wall-clock budget {budget:.0f}s exhausted after {spent:.0f}s")
            reason = "no answer"
            for attempt in range(1, attempts_per_provider + 1):
                call_started = time.monotonic()
                try:
                    reply = self._invoke(name, messages, max_tokens)
                except LLMRefused:
                    record_call(name, self._model_label(name), (time.monotonic() - call_started) * 1000,
                                "refused", attempt=attempt, error_type="LLMRefused")
                    raise  # a refusal is an answer about this request; another provider must not launder it
                except Exception as exc:
                    kind = classify_failure(exc)
                    record_call(name, self._model_label(name), (time.monotonic() - call_started) * 1000,
                                kind, attempt=attempt, error_type=type(exc).__name__)
                    reason = f"{type(exc).__name__}: {str(exc)[:200]}"
                    if kind == "fatal":
                        # a bad request, an unknown model or a refusal: every provider would answer
                        # the same way, so stop rather than hide it behind another one.
                        tried.append(f"{name}: {reason} (not retried: the request, not the provider)")
                        raise give_up(index + 1, "previous failure was not transient")
                    if kind == "unusable":
                        # a stale key or a revoked token: this provider can serve nobody, but the
                        # request is fine. Move on rather than take the workspace down, and keep the
                        # reason in the trail so a run that succeeds elsewhere still reports it.
                        tried.append(f"{name}: {reason} (provider unusable: fix or remove its credential)")
                        break
                    if attempt < attempts_per_provider:
                        if time.monotonic() - started >= budget:
                            tried.append(f"{name}: {reason}")
                            raise give_up(index + 1, f"wall-clock budget {budget:.0f}s exhausted")
                        time.sleep(min(8.0, 1.0 * 2 ** (attempt - 1)))
                    continue
                record_call(reply.provider, reply.model, (time.monotonic() - call_started) * 1000, "ok",
                            attempt=attempt, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out)
                return reply
            tried.append(f"{name}: {reason}")
        raise LLMUnavailable("no model completed the request — " + "; ".join(tried))

    def complete_sync(self, messages: list[Message], *, temperature: float | None = None, max_tokens: int = 4096) -> str:
        return self._run_chain(messages, max_tokens).text

    async def complete(self, messages: list[Message], *, temperature: float | None = None, max_tokens: int = 4096) -> str:
        return await asyncio.to_thread(lambda: self._run_chain(messages, max_tokens).text)

    def ask(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        return self.complete_sync([Message("system", system), Message("user", user)], max_tokens=max_tokens)


def maybe_llm() -> LLM | None:
    """An LLM if any provider exists, else None. Workers branch on this."""
    return LLM() if LLM.available() else None
