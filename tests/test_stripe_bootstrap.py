"""stripe_bootstrap against a fake Stripe API: idempotent product/price creation."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stripe_bootstrap.py"
spec = importlib.util.spec_from_file_location("stripe_bootstrap", SCRIPT)
sb = importlib.util.module_from_spec(spec)
sys.modules["stripe_bootstrap"] = sb
spec.loader.exec_module(sb)  # type: ignore[union-attr]


class FakeStripe:
    def __init__(self) -> None:
        self.products: list[dict] = []
        self.prices: list[dict] = []
        self.posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/products":
            return httpx.Response(200, json={"data": self.products})
        if request.method == "GET" and path == "/v1/prices":
            pid = request.url.params.get("product")
            return httpx.Response(200, json={"data": [p for p in self.prices if p["product"] == pid]})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.posts += 1
        if request.method == "POST" and path == "/v1/products":
            prod = {"id": f"prod_{len(self.products) + 1}", "name": form["name"], "active": True,
                    "metadata": {"revenueos_tier": form["metadata[revenueos_tier]"]}}
            self.products.append(prod)
            return httpx.Response(200, json=prod)
        if request.method == "POST" and path == "/v1/prices":
            price = {"id": f"price_{len(self.prices) + 1}", "product": form["product"], "unit_amount": int(form["unit_amount"]),
                     "currency": form["currency"], "recurring": {"interval": form["recurring[interval]"]}, "active": True}
            self.prices.append(price)
            return httpx.Response(200, json=price)
        return httpx.Response(404, json={"error": path})


def _client(fake: FakeStripe) -> httpx.Client:
    return httpx.Client(base_url="https://api.stripe.com/v1", transport=httpx.MockTransport(fake.handler))


def test_bootstrap_creates_then_reuses():
    fake = FakeStripe()
    env = sb.bootstrap("sk_test_x", client=_client(fake))
    assert set(env) == {"STRIPE_PRICE_PRO", "STRIPE_PRICE_BUSINESS", "STRIPE_PRICE_AGENCY"}
    assert [p["unit_amount"] for p in fake.prices] == [9900, 29900, 99900]
    assert fake.posts == 6
    again = sb.bootstrap("sk_test_x", client=_client(fake))
    assert again == env and fake.posts == 6  # idempotent: nothing created twice


def test_dry_run_and_missing_key(capsys, monkeypatch):
    assert sb.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "RevenueOS Pro" in out and "$99" in out
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert sb.main([]) == 2
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    assert sb.main(["--live"]) == 2


def test_price_env_lines_are_parseable(capsys, monkeypatch):
    fake = FakeStripe()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.setattr(sb, "_client", lambda key, client=None: _client(fake))
    assert sb.main([]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("STRIPE_PRICE_")]
    assert len(lines) == 3 and all("=" in l for l in lines)
    json.dumps(lines)  # plain strings, no secrets besides price ids
