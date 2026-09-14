

def test_a_dash_in_outbound_copy_is_refused_at_send_time():
    """Asked for three times, shipped anyway three times, most recently opening all five drafts.
    A habit that fails repeatedly becomes a check."""
    import pytest

    from revenueos.workers.outreach import dash_in, send_smtp

    assert dash_in("Hi — someone who needs flowers") == "em dash"
    assert dash_in("pages 3–4") == "en dash"
    assert dash_in("the number - not a link") == "spaced hyphen"
    assert dash_in("the number is plain text, not a tap-to-call link") is None, "hyphenated words are words"
    assert dash_in("Quick one about the Alberton page.") is None

    cfg = {"smtp": {"host": "smtp.example.com", "user": "a@example.com"},
           "sender": {"email": "a@example.com", "name": "Brian at RevenueOS"}}
    with pytest.raises(RuntimeError, match="em dash"):
        send_smtp(cfg, "b@example.com", "subject", "Hi — there")
    with pytest.raises(RuntimeError, match="em dash"):
        send_smtp(cfg, "b@example.com", "a — subject", "body")
