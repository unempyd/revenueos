

def test_a_number_only_inside_a_script_is_not_a_number_the_page_shows():
    """Three cold emails were drafted telling real businesses their phone number was plain text on
    pages that display no phone number at all. In every case the number came from JSON the visitor
    never sees: Squarespace site config, and the telephone field of LocalBusiness JSON-LD."""
    from revenueos.workers.seo import parse_signals, signal_findings

    hidden = """<html><head>
      <script type="application/ld+json">{"@type":"LocalBusiness","telephone":"0419 319 334"}</script>
      <script>window.siteConfig = {"defaultTelephone":"0419 319 334"};</script>
      </head><body><h1>Studio</h1><p>Book a class.</p></body></html>"""
    sig = parse_signals(hidden, "https://example.com")
    assert sig["phones"] == ["0419 319 334"], "the extractor should still see it"
    assert sig["phones_visible"] == [], "but nothing renders it"
    assert sig["phones_script_only"] == ["0419 319 334"]
    assert "phone_not_tappable" not in [f["kind"] for f in signal_findings("https://example.com", sig)]

    shown = """<html><body><p>Call us on (08) 8448 3980</p></body></html>"""
    sig = parse_signals(shown, "https://example.com")
    assert sig["phones_visible"] == ["(08) 8448 3980"]
    findings = signal_findings("https://example.com", sig)
    assert "phone_not_tappable" in [f["kind"] for f in findings]
    claim = next(f for f in findings if f["kind"] == "phone_not_tappable")
    assert claim["before"]["phones"] == ["(08) 8448 3980"], "the claim must cite what a visitor sees"
