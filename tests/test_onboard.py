"""Connect once: answers derived from the website; the demo command; pay-on-result."""
from __future__ import annotations

import httpx

from revenueos import onboard
from revenueos.billing import pay_on_result
from revenueos.cli import main

HOME = """<html lang="en"><head><title>Best Hair Salon in Adelaide | Glow Salon</title>
<meta name="description" content="Glow Salon, the city's balayage and extensions specialists."><meta property="og:site_name" content="Glow Salon">
<script>fbq('init','700642230440253');</script></head>
<body><h1>Adelaide Hair Experts | Blonde &amp; Extensions</h1><a href="/booking">Book online</a><a href="/about">About</a>
<p>Call +61 8 5550 0100</p><a href="https://instagram.com/glow">Instagram</a><a href="mailto:hello@glow.example">email</a></body></html>"""
ABOUT = "<html><head><title>About</title></head><body><h2>Our team</h2><p>Two salons, north and south of the river, since 2012.</p></body></html>"


def _client():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/about":
            return httpx.Response(200, text=ABOUT, headers={"content-type": "text/html"})
        if req.url.path in ("/", ""):
            return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})
        return httpx.Response(404, text="no", headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_read_site_and_heuristic_answers():
    facts = onboard.read_site("https://glow.example", client=_client())
    assert facts["ok"] and facts["site_name"] == "Glow Salon" and facts["phones"] == ["+61 8 5550 0100"] and facts["booking_url"] == "/booking"
    assert "https://glow.example/about" in facts["pages"] and "Two salons" in facts["pages"]["https://glow.example/about"]
    a = onboard.heuristic_answers(facts)
    assert a["company_name"] == "Glow Salon" and a["website"].startswith("https://glow.example")
    assert a["what_we_do"].endswith("[derived from the website]") and "balayage" in a["what_we_do"]
    assert a["channels"] == "website, meta ads, instagram" and a["booking_url"] == "https://glow.example/booking"
    assert a["sender_email"] == "hello@glow.example"


def test_derive_answers_layers_the_model_and_survives_its_failure():
    class FakeLLM:
        def ask(self, system, user, max_tokens=4096):
            assert "Glow Salon" in user
            return '{"primary_audience": "Women in Adelaide who want colour work [hypothesis]", "pain_points": "", "category": "hair salon"}'

    answers, facts = onboard.derive_answers("https://glow.example", FakeLLM(), client=_client())
    assert answers["primary_audience"].endswith("[hypothesis]") and answers["category"] == "hair salon" and "pain_points" not in answers
    assert answers["company_name"] == "Glow Salon"

    class BrokenLLM:
        def ask(self, *a, **k):
            raise RuntimeError("no model")

    answers, facts = onboard.derive_answers("https://glow.example", BrokenLLM(), client=_client())
    assert answers["company_name"] == "Glow Salon" and "no model" in facts["llm_error"]
    bad, facts = onboard.derive_answers("https://down.example", None, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    assert bad == {} and facts["error"] == "http 503"


def test_init_from_url_and_demo(workspace, capsys, monkeypatch):
    orig = onboard.read_site
    monkeypatch.setattr(onboard, "read_site", lambda url, client=None, max_pages=5: orig(url, client=_client()))
    assert main(["--root", str(workspace.root), "init", "--from", "https://glow.example", "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "Read https://glow.example" in out and "validates" in out
    from revenueos.context import BusinessContext

    ctx = BusinessContext.load(workspace)
    assert ctx.company_name == "Glow Salon" and ctx.website.startswith("https://glow.example") and "meta ads" in ctx.channels

    from revenueos.workers import seo

    monkeypatch.setattr(seo, "homepage_signals", lambda site, timeout=15.0: seo.parse_signals(HOME, "https://glow.example/"))

    async def fake_crawl(url, max_pages=10):
        import json as _j
        return _j.dumps({"ok": True, "sitemap_found": True, "pages": [{"url": "https://glow.example/", "meta": {"title": "Best Hair Salon", "description": "d" * 40}, "excerpt": "x" * 400}]})

    monkeypatch.setattr(seo, "crawl_website", fake_crawl)
    monkeypatch.setattr(seo, "authority_gap", lambda *a, **k: None)
    assert main(["--root", str(workspace.root), "demo", "https://glow.example", "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "RevenueOS demo — Glow Salon" in out and "PHONE NOT TAPPABLE" in out and "NO LOCAL SCHEMA" in out
    assert "deploys this fix itself once the site is connected" in out and "pay only when you agree with a measured result" in out
    assert "revenueos init --from https://glow.example" in out


def test_pay_on_result_is_free_until_an_agreed_result_then_a_grace_period(workspace, store, monkeypatch):
    monkeypatch.delenv("REVENUEOS_LICENSE_SECRET", raising=False)
    v = pay_on_result(workspace, store)
    assert v["allowed"] and v["reason"] == "free until your first measured result" and v["tier"] == "community"
    a = store.create_action("seo_opportunity", "SEO: no canonical — https://g.example/", "x", context={"executor": "site_deploy"})
    store.set_action_status(a, "executed")
    store.record_outcome(a, "measured", metric="canonical_present", before_value=0.0, after_value=1.0)
    v = pay_on_result(workspace, store)
    assert v["allowed"] and v["days_left"] == 14 and "free for 14 more day(s)" in v["reason"]
    # 15 days later
    import sqlite3

    c = sqlite3.connect(workspace.db)
    c.execute("UPDATE outcomes SET measured_at='2026-08-01T00:00:00+00:00'")
    c.commit()
    v = pay_on_result(workspace, store)
    assert not v["allowed"] and "needs Pro" in v["reason"] and "stay free" in v["reason"]
    assert main(["--root", str(workspace.root), "orchestrator"]) == 3
