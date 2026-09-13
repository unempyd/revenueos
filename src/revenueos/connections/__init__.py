"""Connections: the accounts a business authorises once, the tokens that prove it, and what each may do.

    data/connections.json   one record per provider (0600; secrets Fernet-encrypted when REVENUEOS_TOKEN_KEY is set)

Permission model, in order:
  1. a connection carries the scopes the provider granted (read-only scopes cannot write, whatever we ask);
  2. the owner flips `allow_write` per connection — off by default — so a connected account is read-only
     until a human says otherwise;
  3. every write still arrives as an action a human approves on TODAY; the executor checks (1) and (2)
     at run time and refuses with a plain sentence otherwise.

Providers register in PROVIDERS with `describe()` (what it reads, what it can change, what it needs) so
the panel's Connections page and `revenueos connections` are generated from one source of truth.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..paths import Workspace


class PermissionDenied(RuntimeError):
    """A write was asked of a connection that may only read."""


@dataclass
class Connection:
    provider: str
    account: str                       # the identity on the other side (email, account id, repo, site url)
    scopes: list[str] = field(default_factory=list)
    allow_write: bool = False          # owner's switch; off until flipped
    secrets: dict[str, Any] = field(default_factory=dict)  # tokens, keys — encrypted at rest when a key is set
    meta: dict[str, Any] = field(default_factory=dict)     # non-secret provider details (account name, currency, ids)
    connected_at: str = ""
    updated_at: str = ""

    def can(self, verb: str) -> bool:
        if verb == "read":
            return True
        return bool(self.allow_write)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["secrets"] = {k: "•••" for k in self.secrets}
        return d


# ── secrets at rest ─────────────────────────────────────────────────────────
def _fernet():
    key = os.environ.get("REVENUEOS_TOKEN_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover
        return None
    raw = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
    return Fernet(raw)


def _seal(secrets: dict[str, Any]) -> dict[str, Any]:
    f = _fernet()
    if f is None:
        return {"__plain__": secrets}
    return {"__sealed__": f.encrypt(json.dumps(secrets).encode()).decode()}


def _open(blob: dict[str, Any]) -> dict[str, Any]:
    if "__sealed__" in blob:
        f = _fernet()
        if f is None:
            raise RuntimeError("connections.json holds sealed secrets but REVENUEOS_TOKEN_KEY is not set")
        return json.loads(f.decrypt(blob["__sealed__"].encode()).decode())
    return dict(blob.get("__plain__") or {})


class ConnectionStore:
    def __init__(self, ws: Workspace):
        self.path: Path = ws.data / "connections.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def get(self, provider: str) -> Connection | None:
        rec = self._load().get(provider)
        if not rec:
            return None
        rec = dict(rec)
        rec["secrets"] = _open(rec.get("secrets") or {})
        return Connection(**rec)

    def all(self) -> list[Connection]:
        out = []
        for name in sorted(self._load()):
            c = self.get(name)
            if c:
                out.append(c)
        return out

    def put(self, conn: Connection) -> Connection:
        data = self._load()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        conn.updated_at = now
        conn.connected_at = conn.connected_at or now
        rec = asdict(conn)
        rec["secrets"] = _seal(conn.secrets)
        data[conn.provider] = rec
        self._save(data)
        return conn

    def set_allow_write(self, provider: str, allow: bool) -> Connection:
        conn = self.get(provider)
        if not conn:
            raise KeyError(provider)
        conn.allow_write = bool(allow)
        return self.put(conn)

    def remove(self, provider: str) -> bool:
        data = self._load()
        if provider not in data:
            return False
        del data[provider]
        self._save(data)
        return True

    def sealed(self) -> bool:
        return _fernet() is not None


def require(store: ConnectionStore, provider: str, verb: str = "read") -> Connection:
    """The connection, or a plain refusal the executor can return to the human."""
    conn = store.get(provider)
    if conn is None:
        raise PermissionDenied(f"{provider} is not connected — open Connections and connect it first")
    if verb == "write" and not conn.can("write"):
        raise PermissionDenied(f"{provider} is connected read-only — turn on 'allow changes' for it on the Connections page")
    return conn


# ── provider registry ───────────────────────────────────────────────────────
def providers() -> dict[str, Any]:
    from . import calendar_ics, github_site, google, meta, stripe_conn, wordpress

    mods = [stripe_conn, google, meta, github_site, wordpress, calendar_ics]
    return {m.NAME: m for m in mods}


def describe_all(store: ConnectionStore) -> list[dict[str, Any]]:
    out = []
    for name, mod in providers().items():
        d = dict(mod.describe())
        conn = store.get(name)
        d.update({"name": name, "connected": conn is not None, "account": conn.account if conn else None,
                  "allow_write": bool(conn and conn.allow_write), "scopes": conn.scopes if conn else [],
                  "ready": mod.ready(), "missing": mod.missing()})
        out.append(d)
    return out
