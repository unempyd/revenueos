"""Homepage signal extraction: the shapes that produced false claims on real sites on 2026-09-13."""
from revenueos.workers.seo import parse_signals


def phones(html: str) -> list[str]:
    return parse_signals(html, "https://x.test")["phones"]


def test_hash_fragments_are_not_phone_numbers():
    # a font-file hash and a proxy hash on balenseskin.com.au read as "0369840597" and "085215316"
    html = ('<link href="/cdn/fonts/cardo/cardo_n4.8d7bdd0369840597cbb62dc8a447619701d8d34a.woff2">'
            '<script>"/proxy/f1f32296fc6867327ea8482d8755477b085215316e2475f5ac20c798ef47152b/cdn"</script>')
    assert phones(html) == []


def test_uuid_slices_are_not_phone_numbers():
    # a Shopify extension id on littleloveco.net.au read as "05801-3517-74"
    html = '<div data-id="01a05801-3517-74cc-ad1b-9e2f"></div> Call (08) 8363 7443'
    assert phones(html) == ["(08) 8363 7443"]


def test_real_numbers_next_to_labels_still_pass():
    assert phones("Phone:0438187373") == ["0438187373"]
    assert phones("Call us on 0438 187 373 today.") == ["0438 187 373"]
    assert phones("<p>+61 8 8123 4567</p>") == ["+61 8 8123 4567"]


def test_facebook_links_are_not_booking_links():
    assert parse_signals('<a href="https://www.facebook.com/littlelovecoflorist/">fb</a>', "https://x.test")["booking_url"] is None
    assert parse_signals('<link href="/plugins/custom-facebook-feed/x.css">', "https://x.test")["booking_url"] is None
    assert parse_signals('<a href="/book-now">Book</a>', "https://x.test")["booking_url"] == "/book-now"
    assert parse_signals('<a href="https://x.com/booking/1">b</a>', "https://x.test")["booking_url"] == "https://x.com/booking/1"
