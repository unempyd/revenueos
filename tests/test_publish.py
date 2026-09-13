"""Content deliverables reach an audience: WordPress create_post/get_post against the real request
shape, the publish_post executor (refusals, and a real publish through a MockTransport), content.py
offering a separate Publish action only when WordPress is writable, and measure.py telling produced
from published."""
from __future__ import annotations

import httpx
import pytest

from revenueos.connections import ConnectionStore, PermissionDenied, wordpress
from revenueos.workers import execute_action
from revenueos.workers.content import execute_content
from revenueos.workers.executors import markdown_to_html
from revenueos.workers.measure import measure_publish


def _mock(routes):
    """routes: {(method, path_prefix): (status, json)}"""
    def handler(req: httpx.Request) -> httpx.Response:
        for (m, pfx), (status, payload) in routes.items():
            if req.method == m and req.url.path.startswith(pfx):
                return httpx.Response(status, json=payload if not callable(payload) else payload(req))
        return httpx.Response(404, json={"error": f"no route {req.method} {req.url.path}"})
    return httpx.MockTransport(handler)


class FakeLLM:
    model = "fake"

    def complete_sync(self, messages, *, temperature=None, max_tokens=4096):
        return ("## What to publish\n\nA short paragraph about no-shows.\n\n"
                "- rebooking is automatic\n- reminders go out by SMS\n\n"
                "Read more about **Acme Recall** [here](https://acme-scheduling.example/recall).")


def _connect_wordpress(cs: ConnectionStore, *, allow_write: bool) -> None:
    client = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("GET", "/wp-json/wp/v2/users/me"): (200, {"id": 1, "slug": "admin", "capabilities": {"edit_pages": True}}),
    }))
    wordpress.connect(cs, "https://wp.example", "admin", "abcd efgh", client=client)
    if allow_write:
        cs.set_allow_write("wordpress", True)


# ── wordpress.py: create_post / get_post ──────────────────────────────────────
def test_wordpress_create_post(workspace):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=True)
    client = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("POST", "/wp-json/wp/v2/posts"): (201, lambda req: {"id": 9, "link": "https://wp.example/2026/09/hi/", "status": "publish"}),
    }))
    res = wordpress.create_post(cs, "Hi", "<p>Hi</p>", status="publish", client=client)
    assert res == {"id": 9, "link": "https://wp.example/2026/09/hi/", "status": "publish"}


def test_wordpress_create_post_requires_allow_write(workspace):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=False)
    with pytest.raises(PermissionDenied):
        wordpress.create_post(cs, "Hi", "<p>Hi</p>", client=httpx.Client(transport=_mock({})))


def test_wordpress_get_post(workspace):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=True)
    client = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("GET", "/wp-json/wp/v2/posts/9"): (200, {"id": 9, "link": "https://wp.example/2026/09/hi/", "status": "publish",
                                                  "title": {"raw": "Hi"}, "content": {"raw": "<p>Hi</p>"}}),
    }))
    res = wordpress.get_post(cs, 9, client=client)
    assert res["title"] == "Hi" and res["status"] == "publish"


def test_wordpress_create_post_surfaces_401_and_403_as_sentences(workspace):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=True)
    unauthorized = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("POST", "/wp-json/wp/v2/posts"): (401, {"code": "incorrect_password"}),
    }))
    with pytest.raises(RuntimeError, match="refused the credentials"):
        wordpress.create_post(cs, "Hi", "<p>Hi</p>", client=unauthorized)
    forbidden = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("POST", "/wp-json/wp/v2/posts"): (403, {"code": "rest_cannot_create"}),
    }))
    with pytest.raises(RuntimeError, match="not allowed to create posts"):
        wordpress.create_post(cs, "Hi", "<p>Hi</p>", client=forbidden)


# ── the markdown → HTML converter ─────────────────────────────────────────────
def test_markdown_to_html_covers_headings_lists_links_bold():
    html = markdown_to_html("## Title\n\nA **bold** [link](https://x.example).\n\n- one\n- two\n")
    assert "<h2>Title</h2>" in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://x.example">link</a>' in html
    assert "<ul><li>one</li><li>two</li></ul>" in html


# ── the publish_post executor ──────────────────────────────────────────────────
def test_publish_post_refuses_without_connection(workspace, store, onboarded):
    (workspace.outputs / "20260913-x-1.md").write_text("# T\n\nbody\n", encoding="utf-8")
    aid = store.create_action("content_opportunity", "Publish: T", "x",
                              context={"executor": "publish_post", "deliverable": "data/outputs/20260913-x-1.md"})
    out = execute_action(workspace, store, onboarded, None, store.get_action(aid))
    assert out.startswith("not published: ") and "wordpress" in out


def test_publish_post_refuses_when_read_only(workspace, store, onboarded):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=False)
    (workspace.outputs / "20260913-x-2.md").write_text("# T\n\nbody\n", encoding="utf-8")
    aid = store.create_action("content_opportunity", "Publish: T", "x",
                              context={"executor": "publish_post", "deliverable": "data/outputs/20260913-x-2.md"})
    out = execute_action(workspace, store, onboarded, None, store.get_action(aid))
    assert out.startswith("not published: ") and "read-only" in out


def test_publish_post_refuses_without_deliverable(workspace, store, onboarded):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=True)
    aid = store.create_action("content_opportunity", "Publish: T", "x",
                              context={"executor": "publish_post", "deliverable": "data/outputs/does-not-exist.md"})
    out = execute_action(workspace, store, onboarded, None, store.get_action(aid))
    assert out.startswith("not published: ") and "missing" in out
    bid = store.create_action("content_opportunity", "Publish: T2", "x", context={"executor": "publish_post"})
    out2 = execute_action(workspace, store, onboarded, None, store.get_action(bid))
    assert out2.startswith("not published: ") and "no deliverable" in out2


def test_publish_post_publishes_and_records_url(workspace, store, onboarded, monkeypatch):
    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=True)
    mock_client = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("POST", "/wp-json/wp/v2/posts"): (201, {"id": 42, "link": "https://wp.example/2026/09/hello/", "status": "publish"}),
    }))
    monkeypatch.setattr(wordpress, "_client", lambda conn, client: mock_client)
    (workspace.outputs / "20260913-x-3.md").write_text("# Hello No-Shows\n\nSome **bold** text.\n\n- item one\n- item two\n",
                                                        encoding="utf-8")
    aid = store.create_action("content_opportunity", "Publish: Hello No-Shows", "x",
                              context={"executor": "publish_post", "deliverable": "data/outputs/20260913-x-3.md"})
    out = execute_action(workspace, store, onboarded, None, store.get_action(aid))
    assert not out.startswith("not published")
    assert "https://wp.example/2026/09/hello/" in out
    store.set_action_status(aid, "executed")
    updated = store.get_action(aid)
    assert updated["context"]["published_url"] == "https://wp.example/2026/09/hello/"
    assert updated["context"]["post_id"] == 42
    assert updated["context"]["published_at"]


# ── content.py: the Publish action is offered only when WordPress is writable ─
def test_content_offers_publish_action_only_when_wordpress_writable(workspace, store, onboarded):
    aid = store.create_action("content_opportunity", "SEO: a blog post", "x",
                              context={"executor": "run_skill", "skill": "operations/seo-blog-writer", "channel": "blog",
                                       "skill_input": "Write a short post."})
    execute_content(workspace, store, onboarded, FakeLLM(), store.get_action(aid))
    assert store.get_action(store.list_actions("pending", "content_opportunity")[0]["id"])
    publish_actions = [a for a in store.list_actions("pending", "content_opportunity") if a["context"].get("executor") == "publish_post"]
    assert publish_actions == []  # no wordpress connection: nothing changes

    cs = ConnectionStore(workspace)
    _connect_wordpress(cs, allow_write=True)
    bid = store.create_action("content_opportunity", "SEO: another blog post", "x",
                              context={"executor": "run_skill", "skill": "operations/seo-blog-writer", "channel": "blog",
                                       "skill_input": "Write a short post."})
    out = execute_content(workspace, store, onboarded, FakeLLM(), store.get_action(bid))
    assert "deliverable written to" in out
    publish_actions = [a for a in store.list_actions("pending", "content_opportunity") if a["context"].get("executor") == "publish_post"]
    assert len(publish_actions) == 1
    p = publish_actions[0]
    assert p["title"] == "Publish: SEO: another blog post"
    assert p["context"]["source_action_id"] == bid
    assert p["dedupe_key"] == f"publish:{bid}"
    # the original action itself now records where its deliverable landed
    assert store.get_action(bid)["context"]["deliverable"] == p["context"]["deliverable"]


# ── measure.py: produced vs. published ────────────────────────────────────────
def test_measure_publish_pending_before_url(workspace):
    action = {"id": 1, "context": {}}
    status, data = measure_publish(workspace, action)
    assert status == "pending"


def test_measure_publish_measured_on_200_with_title(workspace, monkeypatch):
    (workspace.outputs / "20260913-x-4.md").write_text("# Hello No-Shows\n\nbody\n", encoding="utf-8")
    action = {"id": 2, "context": {"published_url": "https://wp.example/p/", "deliverable": "data/outputs/20260913-x-4.md"}}
    import revenueos.workers.measure as measure_mod

    monkeypatch.setattr(measure_mod, "_check_published", lambda url, title: (True, ""))
    status, data = measure_publish(workspace, action)
    assert status == "measured" and data["metric"] == "published" and data["after_value"] == 1.0


def test_measure_publish_no_effect_on_404(workspace, monkeypatch):
    (workspace.outputs / "20260913-x-5.md").write_text("# Hello No-Shows\n\nbody\n", encoding="utf-8")
    action = {"id": 3, "context": {"published_url": "https://wp.example/p/", "deliverable": "data/outputs/20260913-x-5.md"}}
    import revenueos.workers.measure as measure_mod

    monkeypatch.setattr(measure_mod, "_check_published", lambda url, title: (False, "publication URL returned HTTP 404"))
    status, data = measure_publish(workspace, action)
    assert status == "no_effect"


def test_measure_publish_pending_on_network_failure(workspace, monkeypatch):
    action = {"id": 4, "context": {"published_url": "https://wp.example/p/"}}
    import revenueos.workers.measure as measure_mod

    monkeypatch.setattr(measure_mod, "_check_published", lambda url, title: (False, "publication URL unreachable (ConnectError)"))
    status, data = measure_publish(workspace, action)
    assert status == "pending"
