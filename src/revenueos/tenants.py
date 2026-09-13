"""Multi-tenant hosting: accounts, sessions and one workspace per account.

    <host root>/accounts.json      {email: {password_hash, salt, workspace, role, created_at}}
    <host root>/tenants/<slug>/    a full workspace (created with the same template `workspace new` uses)

The panel binds every request to the signed-in account's workspace. With no accounts file the panel
behaves as before (single workspace, optional panel password). Passwords: PBKDF2-HMAC-SHA256, 200k rounds.
Sessions: HMAC-signed cookie carrying the account email and an expiry; the secret is the panel's.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import Workspace

ROUNDS = 200_000


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ROUNDS).hex()


def slugify(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")[:60]


class Accounts:
    def __init__(self, host_root: Path):
        self.root = host_root
        self.path = host_root / "accounts.json"

    def enabled(self) -> bool:
        return self.path.exists()

    def _load(self) -> dict[str, dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def list(self) -> list[dict[str, Any]]:
        return [{"email": e, "workspace": r["workspace"], "role": r.get("role", "owner"), "created_at": r.get("created_at")}
                for e, r in sorted(self._load().items())]

    def add(self, email: str, password: str, *, role: str = "owner", template: Path | None = None) -> dict[str, Any]:
        email = email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("a valid email is required")
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        data = self._load()
        if email in data:
            raise ValueError("account exists")
        slug = slugify(email)
        ws_dir = self.root / "tenants" / slug
        from .paths import new_workspace  # local import: paths is imported by everything

        new_workspace(ws_dir, template)
        salt = secrets.token_hex(16)
        data[email] = {"password_hash": _hash(password, salt), "salt": salt, "workspace": str(ws_dir), "role": role,
                       "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
        self._save(data)
        return {"email": email, "workspace": str(ws_dir), "role": role}

    def verify(self, email: str, password: str) -> dict[str, Any] | None:
        rec = self._load().get(email.strip().lower())
        if not rec:
            _hash(password, "00" * 16)  # constant time-ish
            return None
        return rec if hmac.compare_digest(_hash(password, rec["salt"]), rec["password_hash"]) else None

    def workspace_for(self, email: str) -> Workspace | None:
        rec = self._load().get(email.strip().lower())
        return Workspace(Path(rec["workspace"])) if rec else None


# ── sessions ──────────────────────────────────────────────────────────────────
def make_session(email: str, secret: bytes, hours: int = 12) -> str:
    exp = str(int(time.time()) + hours * 3600)
    payload = f"{email}|{exp}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def read_session(token: str | None, secret: bytes) -> str | None:
    if not token or token.count("|") != 2:
        return None
    email, exp, sig = token.split("|")
    good = hmac.new(secret, f"{email}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig) or not exp.isdigit() or int(exp) < time.time():
        return None
    return email
