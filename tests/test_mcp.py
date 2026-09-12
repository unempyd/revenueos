"""revenueos-mcp: tool/resource registration and a few calls through the server's real
call path (FastMCP's in-process `call_tool`/`read_resource`), over the same `workspace` /
`onboarded` fixtures every other worker test uses. No network, no LLM (REVENUEOS_LLM=off).
"""
from __future__ import annotations

import pytest

TOOL_NAMES = {
    "revenueos_today",
    "revenueos_results",
    "revenueos_run",
    "revenueos_seo_audit",
    "revenueos_ads_audit",
    "revenueos_discover_leads",
    "revenueos_draft_followups",
    "revenueos_approve",
    "revenueos_execute",
    "revenueos_ignore",
    "revenueos_measure",
    "revenueos_search_skills",
}


def _structured(result):
    """FastMCP's call_tool returns (content_blocks, structured_result); pull out the payload,
    unwrapping the {"result": ...} envelope FastMCP uses for non-object return types."""
    _blocks, structured = result
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


@pytest.fixture
def server(workspace):
    """Import after `workspace` has set REVENUEOS_ROOT / REVENUEOS_LLM=off, exactly like the
    CLI and panel tests do — the module resolves the workspace lazily inside each tool call,
    but importing early would be equally safe."""
    from revenueos.mcp_server import mcp

    return mcp


async def test_tools_registered(server):
    tools = await server.list_tools()
    assert {t.name for t in tools} == TOOL_NAMES
    for t in tools:
        assert t.description, f"{t.name} has no description (it becomes the marketplace listing)"


async def test_resources_registered(server):
    resources = await server.list_resources()
    assert {str(r.uri) for r in resources} == {"revenueos://today", "revenueos://results"}


async def test_today_empty_workspace(server, onboarded):
    result = await server.call_tool("revenueos_today", {})
    data = _structured(result)
    assert data == {"counts": {}, "pipeline_value": 0.0, "actions": [], "metrics": {}}


async def test_results_empty_workspace(server, onboarded):
    result = await server.call_tool("revenueos_results", {})
    data = _structured(result)
    assert data["results"] == []
    assert data["summary"]["found"] == 0


async def test_run_content_creates_actions(server, onboarded, store):
    result = await server.call_tool("revenueos_run", {"worker": "content"})
    data = _structured(result)
    assert data["ok"] is True
    assert data["actions_created"] > 0
    assert store.counts_by_type()["content_opportunity"] == data["actions_created"]


async def test_today_reflects_content_run(server, onboarded, store):
    await server.call_tool("revenueos_run", {"worker": "content"})
    result = await server.call_tool("revenueos_today", {})
    data = _structured(result)
    assert data["counts"]["content_opportunity"] > 0
    assert len(data["actions"]) == data["counts"]["content_opportunity"]


async def test_approve_execute_ignore_round_trip(server, onboarded, store):
    await server.call_tool("revenueos_run", {"worker": "content"})
    pending = store.list_actions("pending", "content_opportunity")
    approve_id, ignore_id = pending[0]["id"], pending[1]["id"]

    approved = _structured(await server.call_tool("revenueos_approve", {"action_id": approve_id}))
    assert approved == {"ok": True, "action_id": approve_id, "title": pending[0]["title"], "status": "approved"}
    assert store.get_action(approve_id)["status"] == "approved"

    # no LLM in this test env: execute_content refuses to run a skill without a credential,
    # but the tool must still report ok and move the action to 'executed'.
    executed = _structured(await server.call_tool("revenueos_execute", {"action_id": approve_id}))
    assert executed["ok"] is True
    assert store.get_action(approve_id)["status"] == "executed"

    ignored = _structured(await server.call_tool("revenueos_ignore", {"action_id": ignore_id}))
    assert ignored == {"ok": True, "action_id": ignore_id, "title": pending[1]["title"], "status": "ignored"}
    assert store.get_action(ignore_id)["status"] == "ignored"

    missing = _structured(await server.call_tool("revenueos_ignore", {"action_id": 999999}))
    assert missing == {"ok": False, "error": "no action 999999"}


async def test_search_skills_cold_email(server, workspace):
    result = await server.call_tool("revenueos_search_skills", {"query": "cold email", "limit": 5})
    hits = _structured(result)
    assert isinstance(hits, list) and hits
    assert all("cold" in h["description"].lower() or "cold" in h["name"].lower() or "email" in h["description"].lower() for h in hits)


async def test_today_resource_renders_text(server, onboarded):
    await server.call_tool("revenueos_run", {"worker": "content"})
    contents = await server.read_resource("revenueos://today")
    text = contents[0].content
    assert "TODAY — Acme Scheduling" in text
    assert "content_opportunity" in text


async def test_results_resource_renders_text(server, onboarded):
    contents = await server.read_resource("revenueos://results")
    text = contents[0].content
    assert text.startswith("RESULTS — Acme Scheduling")
    assert "0 opportunities found" in text


def test_underlying_functions_are_plain_python(workspace, onboarded, store):
    """Belt and suspenders: the tool bodies must also work as ordinary functions, independent
    of FastMCP's call path (in case a future SDK changes call_tool's shape again)."""
    from revenueos.mcp_server import revenueos_search_skills, revenueos_today

    brief = revenueos_today()
    assert brief["counts"] == {}
    hits = revenueos_search_skills("seo content brief", limit=3)
    assert isinstance(hits, list)
    assert all(isinstance(h, dict) and "slug" in h for h in hits)
