"""The OpenStreetMap + homepage-evidence lead source: only observed facts, then the gate."""
from __future__ import annotations

from revenueos.workers import discover, run_worker, sources


def test_osm_source_stages_only_sites_with_ad_tags_and_a_business_email(monkeypatch):
    monkeypatch.setattr(sources, "fetch_osm", lambda bbox, kinds=None, limit=600, timeout=150.0: [
        {"osm_id": "n1", "name": "Glow Salon", "website": "https://glow.example", "email": "", "phone": "", "kind": "hairdresser", "suburb": "Northtown"},
        {"osm_id": "n2", "name": "Quiet Cafe", "website": "https://quiet.example", "email": "", "phone": "", "kind": "cafe", "suburb": "Norwood"},
        {"osm_id": "n3", "name": "Dead Site", "website": "https://dead.example", "email": "", "phone": "", "kind": "dentist", "suburb": ""},
        {"osm_id": "n4", "name": "Gmail Only", "website": "https://gm.example", "email": "owner@gmail.com", "phone": "", "kind": "florist", "suburb": ""},
    ])

    async def fake_enrich_all(urls, concurrency=12):
        sig_ads = {"meta_pixel": True, "google_ads_tag": False, "ga4": True, "booking_link": True, "tel_link": False, "phone_text": True, "local_schema": False}
        sig_none = {**sig_ads, "meta_pixel": False, "ga4": False}
        by = {
            "https://glow.example": {"ok": True, "url": "https://glow.example/", "domain": "glow.example", "signals": sig_ads, "business_email": "hello@glow.example", "freemail": [], "title": "Glow"},
            "https://quiet.example": {"ok": True, "url": "https://quiet.example/", "domain": "quiet.example", "signals": sig_none, "business_email": "info@quiet.example", "freemail": [], "title": "Quiet"},
            "https://dead.example": {"ok": False, "error": "ConnectError"},
            "https://gm.example": {"ok": True, "url": "https://gm.example/", "domain": "gm.example", "signals": sig_ads, "business_email": None, "freemail": ["owner@gmail.com"], "title": "GM"},
        }
        return [by[u] for u in urls]

    monkeypatch.setattr(sources, "_enrich_all", fake_enrich_all)
    rows, stats = sources.osm_leads({"bbox": [-35.05, 138.45, -34.75, 138.75], "area": "Adelaide"})
    assert stats == {"candidates": 4, "reachable": 3, "with_ad_tag": 2, "with_email": 2, "staged": 1}
    assert [r["company"] for r in rows] == ["Glow Salon"]
    r = rows[0]
    assert r["email"] == "hello@glow.example" and r["lead_id"] == "osm:n1"
    assert r["reason"] == ("hairdresser in Northtown (OpenStreetMap); ad/analytics tags on the site: Meta Pixel, GA4; online booking link; "
                           "business email published on the site: hello@glow.example; phone shown but not tappable; no LocalBusiness schema")


def test_discover_runs_the_osm_source_from_config(workspace, store, onboarded, monkeypatch):
    onboarded.config["discover"] = {"osm": {"bbox": [-35.05, 138.45, -34.75, 138.75], "area": "Adelaide"}}
    monkeypatch.setattr(sources, "osm_leads", lambda cfg: ([{
        "email": "hello@glow.example", "first_name": "", "last_name": "", "company": "Glow Salon", "title": "",
        "website": "https://glow.example/", "linkedin_url": "", "reason": "hairdresser in Northtown (OpenStreetMap); ad/analytics tags on the site: Meta Pixel",
        "lead_id": "osm:n1", "_source": "osm", "_signals": {"meta_pixel": True}}], {"candidates": 1, "reachable": 1, "with_ad_tag": 1, "with_email": 1, "staged": 1}))
    r = run_worker("discover", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 1 and r.details["osm"]["staged"] == 1, r
    lead = store.list_leads()[0]
    assert lead["source"] == "osm" and lead["status"] == "scored" and "Meta Pixel" in lead["reason"]
    assert '"meta_pixel": true' in lead["notes"]
    assert store.lead_funnel() == {"found": 1, "contactable": 1, "qualified": 1}


def test_overpass_query_shape():
    q = sources.overpass_query((-35.05, 138.45, -34.75, 138.75), ('shop="hairdresser"',), 10)
    assert q.startswith("[out:json][timeout:90][bbox:-35.05,138.45,-34.75,138.75];(") and 'nwr[shop="hairdresser"]["website"];' in q and q.endswith("out tags 10;")
    assert discover.openoutreach_command is not None
