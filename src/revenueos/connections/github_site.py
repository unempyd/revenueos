"""A website that lives in a git repository (GitHub Pages, Netlify, Vercel from git): the 'deploy the fix'
executor edits the file, commits and pushes, and the host publishes it. The connection is the repo plus
a token that can push (the `gh` CLI's token, or GITHUB_TOKEN).

Fixes are applied as small, reversible HTML edits to <head>: a canonical link, a JSON-LD block, a title
or meta description. Anything else is out of scope for an automatic executor and stays a deliverable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import Connection, ConnectionStore, require

NAME = "github_site"


def describe() -> dict[str, Any]:
    return {
        "label": "Website in a git repo (GitHub Pages, Netlify, Vercel)",
        "reads": "the site's HTML files",
        "writes": "small <head> fixes (canonical, JSON-LD schema, title, meta description) committed and pushed (needs 'allow changes')",
        "needs": "the repo (owner/name), the path of the site inside it, and a token that can push (gh auth or GITHUB_TOKEN)",
        "how": "revenueos connect github_site --repo owner/name --path website",
    }


def _token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def ready() -> bool:
    return _token() is not None and shutil.which("git") is not None


def missing() -> list[str]:
    out = []
    if _token() is None:
        out.append("GITHUB_TOKEN or `gh auth login`")
    if shutil.which("git") is None:
        out.append("git")
    return out


def connect(store: ConnectionStore, repo: str, path: str = "", branch: str = "main", author_email: str | None = None) -> Connection:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("repo must be owner/name")
    tok = _token()
    if not tok:
        raise RuntimeError("no GitHub token: run `gh auth login` or set GITHUB_TOKEN")
    conn = Connection(provider=NAME, account=repo, scopes=["read", "write"], secrets={"token": tok},
                      meta={"path": path.strip("/"), "branch": branch, "author_email": author_email})
    return store.put(conn)


# ── HTML head edits ───────────────────────────────────────────────────────────
def apply_head_fix(html: str, fix: dict[str, Any]) -> tuple[str, bool]:
    """Return (new_html, changed). fix kinds: canonical {url}, jsonld {data}, title {text}, description {text}."""
    kind = fix["kind"]
    if kind == "canonical":
        tag = f'<link rel="canonical" href="{fix["url"]}">'
        if re.search(r'<link[^>]+rel=["\']canonical["\']', html, re.I):
            return html, False
    elif kind == "jsonld":
        tag = '<script type="application/ld+json">' + json.dumps(fix["data"], ensure_ascii=False) + "</script>"
        t = str(fix["data"].get("@type", ""))
        if t and re.search(r'"@type"\s*:\s*"' + re.escape(t) + '"', html):
            return html, False
    elif kind == "title":
        new, n = re.subn(r"<title[^>]*>.*?</title>", f"<title>{fix['text']}</title>", html, count=1, flags=re.I | re.S)
        return (new, n > 0 and new != html)
    elif kind == "description":
        pattern = r'<meta\s+name=["\']description["\']\s+content=["\'][^"\']*["\']\s*/?>'
        tag = f'<meta name="description" content="{fix["text"].replace(chr(34), "&quot;")}">'
        if re.search(pattern, html, re.I):
            new = re.sub(pattern, tag, html, count=1, flags=re.I)
            return new, new != html
    else:
        raise ValueError(f"unknown fix kind {kind!r}")
    m = re.search(r"</head>", html, re.I)
    if not m:
        return html, False
    return html[: m.start()] + tag + "\n" + html[m.start():], True


def deploy_fix(store: ConnectionStore, *, file: str, fix: dict[str, Any], message: str, dry_run: bool = False) -> dict[str, Any]:
    """Clone shallow, edit one file, commit, push. Returns what changed and the commit."""
    conn = require(store, NAME, "write")
    repo, tok = conn.account, conn.secrets["token"]
    branch = conn.meta.get("branch") or "main"
    rel = "/".join(p for p in (conn.meta.get("path", ""), file) if p)
    work = Path(tempfile.mkdtemp(prefix="revenueos-site-"))
    try:
        url = f"https://x-access-token:{tok}@github.com/{repo}.git"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", branch, url, str(work)], check=True, capture_output=True)
        target = work / rel
        if not target.exists():
            raise FileNotFoundError(f"{rel} is not in {repo}@{branch}")
        before = target.read_text(encoding="utf-8")
        after, changed = apply_head_fix(before, fix)
        if not changed:
            return {"changed": False, "file": rel, "note": "already in place"}
        if dry_run:
            return {"changed": True, "file": rel, "dry_run": True, "diff_chars": len(after) - len(before)}
        target.write_text(after, encoding="utf-8")
        author = conn.meta.get("author_email") or ("revenueos@" + "users.noreply.github.com")  # the connection's identity, never a hard-coded address
        env = {**os.environ, "GIT_AUTHOR_NAME": "RevenueOS", "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_NAME": "RevenueOS", "GIT_COMMITTER_EMAIL": author}
        subprocess.run(["git", "-C", str(work), "add", rel], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "commit", "--quiet", "-m", message], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(work), "push", "--quiet", "origin", branch], check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", str(work), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        return {"changed": True, "file": rel, "commit": sha, "repo": repo, "branch": branch}
    finally:
        shutil.rmtree(work, ignore_errors=True)
