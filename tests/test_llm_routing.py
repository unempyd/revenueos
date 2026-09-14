"""The routing layer under the agent layer: an ordered provider chain that fails over on
capacity failures only, never on a bad request, an auth failure or a refusal.

Everything here uses fake provider callables and a fake `subprocess.run`, the same style as
tests/test_llm.py — no network, no model, no credentials.
"""
import json
import subprocess

import httpx
import pytest

from revenueos import llm as llm_mod
from revenueos.llm import (
    LLM,
    LLMRefused,
    LLMUnavailable,
    Message,
    classify_failure,
    detect_chain,
    record_call,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("REVENUEOS_LLM", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "REVENUEOS_LLM_BASE_URL",
                "REVENUEOS_LLM_MODEL", "REVENUEOS_LLM_API_KEY", "REVENUEOS_LLM_RETRIES", "REVENUEOS_LLM_BUDGET",
                "REVENUEOS_CLI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm_mod.os.path, "isdir", lambda p: False)          # no ~/.config/anthropic profile
    monkeypatch.setattr(llm_mod.shutil, "which", lambda n: None)            # no claude CLI
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)              # backoff without the wait
    monkeypatch.setattr(llm_mod, "record_call", lambda *a, **k: None)       # metrics tested separately
    yield


def _chain(llm, **providers):
    """Replace provider dispatch with fakes: {"anthropic": callable_or_exception, ...}."""
    calls = []

    def invoke(name, messages, max_tokens):
        calls.append(name)
        handler = providers[name]
        if isinstance(handler, BaseException):
            raise handler
        return handler(messages, max_tokens)

    llm._invoke = invoke
    return calls


def _reply(text, provider="fake"):
    return lambda messages, max_tokens: llm_mod.Reply(text, provider, "fake-model", 11, 7)


def _http_error(status):
    request = httpx.Request("POST", "http://127.0.0.1:1/chat/completions")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))


# ── chain composition ────────────────────────────────────────────────────────────────────────
def test_auto_detect_orders_first_party_then_operator_endpoint(monkeypatch):
    assert detect_chain() == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm_mod.shutil, "which", lambda n: "/usr/local/bin/claude" if n == "claude" else None)
    monkeypatch.setenv("REVENUEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("REVENUEOS_LLM_MODEL", "local-model")
    assert detect_chain() == ["anthropic", "claude-cli", "openai-compatible"]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert detect_chain() == ["claude-cli", "openai-compatible"]


def test_openai_compatible_needs_both_url_and_model(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    assert detect_chain() == []            # no model named: not a usable provider
    monkeypatch.setenv("REVENUEOS_LLM_MODEL", "local-model")
    assert detect_chain() == ["openai-compatible"]


def test_explicit_order_pins_the_chain(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "claude-cli,anthropic")
    assert detect_chain() == ["claude-cli", "anthropic"]                    # order as written, not detected
    assert LLM().chain == ["claude-cli", "anthropic"]
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic")
    assert detect_chain() == ["anthropic"] and LLM().provider == "anthropic"


def test_off_disables_everything_even_with_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REVENUEOS_LLM", "off")
    assert detect_chain() == [] and not LLM.available() and llm_mod.maybe_llm() is None
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,off")
    assert detect_chain() == []                                             # `off` anywhere wins
    with pytest.raises(LLMUnavailable):
        LLM().ask("s", "u")


# ── failure classification ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("exc,expected", [
    (_http_error(429), "transient"),
    (_http_error(500), "transient"),
    (_http_error(503), "transient"),
    (_http_error(400), "fatal"),
    (_http_error(401), "unusable"),   # a stale credential: skip this provider, do not stop the chain
    (_http_error(403), "unusable"),
    (TimeoutError("read timed out"), "transient"),
    (ConnectionRefusedError("[Errno 61] Connection refused"), "transient"),
    (subprocess.TimeoutExpired("claude", 900), "transient"),
    (LLMUnavailable("You've hit your session limit · resets 9:10pm"), "transient"),
    (LLMUnavailable("claude -p failed (1): not logged in"), "unusable"),
    (LLMUnavailable("invalid api key"), "unusable"),
    (LLMRefused("refused by safety classifier"), "fatal"),
    (FileNotFoundError(2, "No such file or directory: 'claude'"), "transient"),   # provider absent: skip it
    (ValueError("max_tokens 500 is not an integer"), "fatal"),              # a bug must not look like an outage
])
def test_classification(exc, expected):
    assert classify_failure(exc) == expected


# ── failover ─────────────────────────────────────────────────────────────────────────────────
def test_failover_on_rate_limit(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    monkeypatch.setenv("REVENUEOS_LLM_RETRIES", "1")
    llm = LLM()
    calls = _chain(llm, anthropic=_http_error(429), **{"claude-cli": _reply("second provider answered")})
    assert llm.ask("s", "u") == "second provider answered"
    assert calls == ["anthropic", "claude-cli"]


def test_failover_on_timeout_after_bounded_retry(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    monkeypatch.setenv("REVENUEOS_LLM_RETRIES", "3")
    llm = LLM()
    calls = _chain(llm, anthropic=TimeoutError("read timed out"), **{"claude-cli": _reply("ok")})
    assert llm.ask("s", "u") == "ok"
    assert calls == ["anthropic", "anthropic", "anthropic", "claude-cli"]   # 3 attempts, then move on


def test_no_failover_on_bad_request(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    llm = LLM()
    calls = _chain(llm, anthropic=_http_error(400), **{"claude-cli": _reply("must not be reached")})
    with pytest.raises(LLMUnavailable, match="the request, not the provider"):
        llm.ask("s", "u")
    assert calls == ["anthropic"]


def test_auth_failure_skips_that_provider_and_the_chain_continues(monkeypatch):
    """A stale key must not take down a workspace that has a working provider behind it.

    The provider is skipped without retrying it (retrying a bad credential is pointless), the
    next one serves the request, and the failure is still recorded so the operator learns that a
    credential needs fixing even though the run succeeded."""
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    llm = LLM()
    calls = _chain(llm, anthropic=_http_error(401), **{"claude-cli": _reply("served by the second provider")})
    assert llm.ask("s", "u") == "served by the second provider"
    assert calls == ["anthropic", "claude-cli"]  # tried once, not retried, then moved on


def test_unusable_provider_is_named_when_the_whole_chain_fails(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    llm = LLM()
    _chain(llm, anthropic=_http_error(401), **{"claude-cli": _http_error(500)})
    with pytest.raises(LLMUnavailable) as err:
        llm.ask("s", "u")
    assert "provider unusable" in str(err.value)
    assert "anthropic" in str(err.value) and "claude-cli" in str(err.value)


def test_refusal_is_an_answer_and_never_routed_elsewhere(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    llm = LLM()
    calls = _chain(llm, anthropic=LLMRefused("refused by safety classifier"),
                   **{"claude-cli": _reply("must not be reached")})
    with pytest.raises(LLMRefused):
        llm.ask("s", "u")
    assert calls == ["anthropic"]


def test_budget_stops_the_chain(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    monkeypatch.setenv("REVENUEOS_LLM_BUDGET", "0.05")
    clock = iter([0.0] + [i * 0.1 for i in range(1, 40)])                   # every read advances 100ms

    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: next(clock))
    llm = LLM()
    calls = _chain(llm, anthropic=_http_error(429), **{"claude-cli": _reply("too late")})
    with pytest.raises(LLMUnavailable, match="budget"):
        llm.ask("s", "u")
    assert calls == ["anthropic"]                                          # never got to the second provider


def test_unavailable_names_every_provider_and_its_reason(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli,openai-compatible")
    monkeypatch.setenv("REVENUEOS_LLM_RETRIES", "1")
    llm = LLM()
    _chain(llm,
           anthropic=_http_error(429),
           **{"claude-cli": LLMUnavailable("You've hit your session limit · resets 9:10pm"),
              "openai-compatible": ConnectionRefusedError("[Errno 61] Connection refused")})
    with pytest.raises(LLMUnavailable) as err:
        llm.ask("s", "u")
    message = str(err.value)
    for fragment in ("anthropic", "claude-cli", "openai-compatible", "session limit", "Connection refused"):
        assert fragment in message


# ── the real CLI provider still works under the chain ────────────────────────────────────────
def test_cli_provider_inside_a_chain(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM", "openai-compatible,claude-cli")
    monkeypatch.setenv("REVENUEOS_LLM_BASE_URL", "http://127.0.0.1:1/v1")   # nothing listens on port 1
    monkeypatch.setenv("REVENUEOS_LLM_MODEL", "unreachable-model")
    monkeypatch.setenv("REVENUEOS_LLM_RETRIES", "1")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
            {"result": "served by the CLI", "is_error": False,
             "usage": {"input_tokens": 120, "output_tokens": 9}}), stderr="")

    monkeypatch.setattr(llm_mod.subprocess, "run", fake_run)
    assert LLM().ask("s", "u") == "served by the CLI"


# ── observability ────────────────────────────────────────────────────────────────────────────
def test_metrics_record_route_and_never_prompt_text(monkeypatch, tmp_path):
    monkeypatch.undo()  # this test needs the real record_call
    for var in ("REVENUEOS_LLM_RETRIES", "REVENUEOS_LLM_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)

    recorded = []

    class FakeStore:
        def record_metric(self, name, value, **dims):
            recorded.append((name, value, dims))

    monkeypatch.setattr(llm_mod, "_metric_store", lambda: FakeStore())
    llm = LLM()
    _chain(llm, anthropic=_http_error(429), **{"claude-cli": _reply("answer text from the model")})
    secret = "PROMPT SECRET acme dental scheduling revenue"
    assert llm.ask("system prompt " + secret, "user prompt " + secret) == "answer text from the model"

    names = [(d["provider"], d["outcome"]) for _n, _v, d in recorded]
    assert ("anthropic", "transient") in names and ("fake", "ok") in names   # the route is visible
    ok = [d for _n, _v, d in recorded if d["outcome"] == "ok"][0]
    assert ok["tokens_in"] == 11 and ok["tokens_out"] == 7
    blob = json.dumps(recorded)
    assert "PROMPT SECRET" not in blob and "answer text" not in blob and "user prompt" not in blob


def test_record_call_is_silent_without_a_workspace_store(monkeypatch):
    monkeypatch.setattr(llm_mod, "_metric_store", lambda: None)
    record_call("anthropic", "m", 12.0, "ok")   # must not raise


def test_metrics_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("REVENUEOS_LLM_METRICS", "0")
    assert llm_mod._metric_store() is None


def test_complete_async_uses_the_chain(monkeypatch):
    import asyncio

    monkeypatch.setenv("REVENUEOS_LLM", "anthropic,claude-cli")
    monkeypatch.setenv("REVENUEOS_LLM_RETRIES", "1")
    llm = LLM()
    _chain(llm, anthropic=_http_error(503), **{"claude-cli": _reply("async answer")})
    out = asyncio.run(llm.complete([Message("system", "s"), Message("user", "u")], max_tokens=64))
    assert out == "async answer"


# ── a broken provider must not take down a workspace that has a working one ──────────
# The first spec for this said "never fail over on an auth failure". That conflated two
# different things: a wrong REQUEST (every provider rejects it identically) and an unusable
# PROVIDER (a stale key, a revoked token, no credit — the request is fine). For a system that
# runs unattended, a stale key that halts every run while a signed-in CLI sits second in the
# chain is the exact outage this routing layer exists to prevent.

def test_auth_failure_makes_the_provider_unusable_not_the_request_fatal():
    from revenueos.llm import classify_failure

    auth_type = type("AuthenticationError", (Exception,), {})
    assert classify_failure(auth_type("no")) == "unusable"
    assert classify_failure(Exception("Invalid API key provided")) == "unusable"
    assert classify_failure(Exception("your credit balance is too low")) == "unusable"
    # the request being wrong is still fatal: another provider would answer identically
    assert classify_failure(Exception("invalid_request_error: bad param")) == "fatal"
    assert classify_failure(Exception("model not found")) == "fatal"
    # capacity is still transient
    assert classify_failure(Exception("rate limit exceeded")) == "transient"
    assert classify_failure(Exception("You've hit your session limit · resets 9:10pm")) == "transient"
