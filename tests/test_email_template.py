"""The findings email is the only thing most people will ever see of this product.

These tests hold the properties that make it safe to send to a stranger: the claims in the HTML
and the text part are the same claims, the message identifies who sent it and how to stop it, the
one link points at the domain the mail came from, and nothing quotes a tracking identifier back at
the recipient. The last one exists because the previous version did exactly that.
"""
from __future__ import annotations

import smtplib

import pytest

from revenueos.email_template import Finding, Sender, findings_email, findings_text
from revenueos.workers import outreach

BRAND = Sender(company="RevenueOS", email="hello@revenueos.com.au",
               site="revenueos.com.au")

FINDINGS = [
    Finding(title="Nobody on a phone can tap your number",
            cost="It is typed as text, not a link.",
            evidence='<li class="phone">0438 187 373</li>  tel: links on the page: 0'),
    Finding(title="Google is guessing your hours and address",
            cost="A search nearby is answered from structured markup.",
            evidence="JSON-LD on page: WebPage, WebSite. LocalBusiness: absent."),
]

ARGS = dict(business="EFM Health Clubs", page="efm.net.au/club/bowden", findings=FINDINGS,
            sender=BRAND)


def test_evidence_quoting_a_tracking_identifier_is_refused():
    """Reading someone's pixel id out of their page source and mailing it to them reads as
    surveillance, and it tells them nothing they can act on."""
    for snooped in ("Meta Pixel 994925497566169 fires on load",
                    "Google Ads AW-1005268197 is installed",
                    "GTM-ABC1234 loads three tags",
                    "Analytics G-XY12345678 is present"):
        bad = Finding(title="t", cost="c", evidence=snooped)
        with pytest.raises(ValueError, match="surveillance"):
            findings_email(business="X", page="x.example", findings=[bad], sender=BRAND)
        with pytest.raises(ValueError, match="surveillance"):
            findings_text(business="X", page="x.example", findings=[bad], sender=BRAND)


def test_a_phone_number_is_not_mistaken_for_a_tracking_identifier():
    assert "0438 187 373" in findings_email(**ARGS)


def test_the_text_part_makes_every_claim_the_html_makes():
    """A text part that is a stub telling the reader to open a browser is the shape of a message
    with something to hide, and filters score it that way."""
    html, text = findings_email(**ARGS), findings_text(**ARGS)
    for f in FINDINGS:
        assert f.title in html and f.title in text
        assert f.cost in html and f.cost in text
        assert f.evidence in text


def test_the_copy_passes_the_send_paths_dash_rule():
    assert outreach.dash_in(findings_text(**ARGS)) is None
    assert outreach.dash_in(FINDINGS[0].title) is None


def test_there_is_one_web_link_and_it_is_the_sending_domain():
    """Alignment between the From domain and the only link in the body is the cheapest honesty
    signal available, and the one a phishing message cannot reproduce."""
    html = findings_email(**ARGS)
    assert html.count("href=") == 1
    assert 'href="https://revenueos.com.au"' in html


def test_it_carries_no_tracking_pixel_and_no_style_block():
    html = findings_email(**ARGS)
    assert "<style" not in html
    assert html.count("<img") == 1  # the wordmark, and it is decorative
    assert 'width="1"' not in html


def test_the_message_says_who_sent_it_and_how_to_stop_it():
    for rendered in (findings_email(**ARGS), findings_text(**ARGS)):
        assert "RevenueOS" in rendered
        assert "hello@revenueos.com.au" in rendered
        assert "stop" in rendered


def test_no_sender_location_can_reach_the_message():
    """The owner asked three times for the location out of outbound mail, and it came back twice
    because a caller passed it. There is no field to pass any more, which is the only version of
    this fix that holds. Australia does not require a postal address in commercial email; that is
    the American rule, and the Act's actual ask is identification and a contact that stays valid.
    """
    assert not hasattr(BRAND, "place")
    with pytest.raises(TypeError):
        Sender(company="RevenueOS", place="Adelaide, South Australia")
    for rendered in (findings_email(**ARGS), findings_text(**ARGS)):
        assert "Adelaide" not in rendered
        assert "South Australia" not in rendered


def test_a_brand_writes_as_we_and_a_named_person_writes_as_i():
    brand = findings_text(**ARGS)
    assert "reply and we will send the fixes" in brand
    assert "you will not hear from us again" in brand

    named = findings_text(**{**ARGS, "sender": Sender(company="RevenueOS",
                                                      person="Brian")})
    assert "reply and I will send the fixes" in named
    assert "I am Brian and I build RevenueOS" in named


def test_an_absent_company_number_renders_no_company_number():
    """A wrong ABN is worse than an absent one, so nothing is filled in with a plausible default."""
    assert "ABN" not in findings_email(**ARGS)
    with_abn = Sender(company="RevenueOS", abn="12 345 678 901")
    assert "ABN 12 345 678 901" in findings_email(**{**ARGS, "sender": with_abn})


def test_an_email_with_no_findings_is_refused():
    for render in (findings_email, findings_text):
        with pytest.raises(ValueError, match="nothing to say"):
            render(business="X", page="x.example", findings=[], sender=BRAND)


def test_send_smtp_sends_multipart_alternative_with_the_text_part_first(monkeypatch):
    """The HTML used to be unreachable: nothing called it and the send path only ever set a text
    body, so the designed email could not leave the building."""
    sent: list = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    cfg = {"smtp": {"host": "smtp.example", "port": 587, "user": "hello@revenueos.com.au"},
           "sender": {"name": "RevenueOS", "email": "hello@revenueos.com.au"}}

    outreach.send_smtp(cfg, "someone@example.com", FINDINGS[0].title,
                       findings_text(**ARGS), html=findings_email(**ARGS))

    assert len(sent) == 1
    msg = sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_without_html_it_stays_a_plain_text_message(monkeypatch):
    sent: list = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    cfg = {"smtp": {"host": "smtp.example", "user": "hello@revenueos.com.au"},
           "sender": {"email": "hello@revenueos.com.au"}}

    outreach.send_smtp(cfg, "someone@example.com", "A subject", "A body with no dashes in it.")
    assert sent[0].get_content_type() == "text/plain"
