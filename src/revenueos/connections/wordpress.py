"""WordPress (the most common small-business CMS): read pages, and apply the same small <head> fixes the
git-site executor applies — through the REST API with an Application Password the owner creates in
Users → Profile. Canonical and JSON-LD go into the page content's first block when the theme offers no
head hook, which every crawler still reads.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import Connection, ConnectionStore, require
from .github_site import apply_head_fix

NAME = "wordpress"


def describe() -> dict[str, Any]:
    return {
        "label": "WordPress site",
        "reads": "pages and posts",
        "writes": "small fixes to a page (canonical, JSON-LD schema, title, meta description) via the REST API (needs 'allow changes')",
        "needs": "the site URL, a WordPress user and an Application Password (Users → Profile → Application Passwords)",
        "how": "revenueos connect wordpress --site https://example.com --user admin --app-password 'xxxx xxxx …'",
    }


def ready() -> bool:
    return True


def missing() -> list[str]:
    return []


def _client(conn: Connection, client: httpx.Client | None) -> httpx.Client:
    if client is not None:
        return client
    return httpx.Client(base_url=conn.account.rstrip("/") + "/wp-json/wp/v2", auth=(conn.secrets["user"], conn.secrets["app_password"]), timeout=30)


def connect(store: ConnectionStore, site: str, user: str, app_password: str, client: httpx.Client | None = None) -> Connection:
    c = client or httpx.Client(base_url=site.rstrip("/") + "/wp-json/wp/v2", auth=(user, app_password), timeout=30)
    r = c.get("/users/me", params={"context": "edit"})
    if r.status_code != 200:
        raise RuntimeError(f"WordPress refused the credentials: {r.status_code} {r.text[:120]}")
    me = r.json()
    caps = me.get("capabilities") or {}
    scopes = ["read"] + (["write"] if caps.get("edit_pages") or caps.get("edit_posts") else [])
    conn = Connection(provider=NAME, account=site.rstrip("/"), scopes=scopes, secrets={"user": user, "app_password": app_password},
                      meta={"user": me.get("slug") or user, "wp_user_id": me.get("id")})
    return store.put(conn)


def pages(store: ConnectionStore, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    conn = require(store, NAME)
    c = _client(conn, client)
    r = c.get("/pages", params={"per_page": 50, "context": "edit"})
    if r.status_code != 200:
        raise RuntimeError(f"WordPress pages: {r.status_code}")
    return [{"id": p["id"], "link": p.get("link"), "title": (p.get("title") or {}).get("raw") or (p.get("title") or {}).get("rendered"),
             "content": (p.get("content") or {}).get("raw") or ""} for p in r.json()]


def apply_fix(store: ConnectionStore, page_id: int, fix: dict[str, Any], client: httpx.Client | None = None) -> dict[str, Any]:
    """Update one page. For canonical/jsonld the tag is prepended to the content (themes without a head hook);
    title updates the page title; description is stored as excerpt."""
    conn = require(store, NAME, "write")
    if "write" not in conn.scopes:
        raise RuntimeError("this WordPress user cannot edit pages")
    c = _client(conn, client)
    r = c.get(f"/pages/{page_id}", params={"context": "edit"})
    if r.status_code != 200:
        raise RuntimeError(f"WordPress page {page_id}: {r.status_code}")
    p = r.json()
    payload: dict[str, Any] = {}
    if fix["kind"] == "title":
        payload["title"] = fix["text"]
    elif fix["kind"] == "description":
        payload["excerpt"] = fix["text"]
    else:
        raw = (p.get("content") or {}).get("raw") or ""
        new, changed = apply_head_fix("<head></head>" + raw, fix)
        if not changed:
            return {"changed": False, "page": page_id, "note": "already in place"}
        payload["content"] = new.replace("<head>", "", 1).replace("</head>", "", 1)
    u = c.post(f"/pages/{page_id}", json=payload)
    if u.status_code != 200:
        raise RuntimeError(f"WordPress update: {u.status_code} {u.text[:200]}")
    return {"changed": True, "page": page_id, "link": u.json().get("link")}
