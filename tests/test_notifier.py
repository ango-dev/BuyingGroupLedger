"""alerts/notifier.py -- the alert body must reach run.log, not only the channels."""

import logging

import pytest

from alerts import notifier


def test_the_body_is_logged_even_when_no_channel_is_configured(monkeypatch, caplog):
    monkeypatch.setattr(notifier, "send_email", lambda s, b: None)
    monkeypatch.setattr(notifier, "send_discord", lambda m: None)
    with caplog.at_level(logging.INFO, logger="alerts.notifier"):
        notifier.alert("Amazon [p]: deterministic path failed", "Failure dossier: https://par/x/report.md\n  page_1.html: https://par/x/page_1.html")
    assert "ALERT: Amazon [p]: deterministic path failed" in caplog.text
    assert "https://par/x/page_1.html" in caplog.text


def test_a_failing_channel_still_leaves_the_log_line(monkeypatch, caplog):
    def boom(*a):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(notifier, "send_email", boom)
    monkeypatch.setattr(notifier, "send_discord", boom)
    with caplog.at_level(logging.INFO, logger="alerts.notifier"):
        notifier.alert("subject", "the links")
    assert "ALERT: subject" in caplog.text and "the links" in caplog.text


def test_send_message_sends_the_prebuilt_mime_through_smtp_ssl(monkeypatch):
    """send_message must ship the message VERBATIM (attachments, threading headers) from the
    configured account to exactly the recipients given -- it is the boundary respond_bfmr
    replies through, and conftest stubs it for every other test."""
    import dataclasses
    from email.message import EmailMessage

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None):
            sent["endpoint"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            sent["login"] = user

        def sendmail(self, from_addr, recipients, body):
            sent["from"], sent["recipients"], sent["body"] = from_addr, recipients, body

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        notifier.settings, gmail_address="me@example.com", gmail_app_password="pw"))

    msg = EmailMessage()
    msg["Subject"] = "Re: Combined Package"
    msg["In-Reply-To"] = "<original@example.com>"
    msg.set_content("serials inside")
    notifier._smtp_send(msg, ["support@example.com", "audit@example.com"])

    assert sent["endpoint"] == ("smtp.gmail.com", 465)
    assert sent["login"] == "me@example.com"
    assert sent["recipients"] == ["support@example.com", "audit@example.com"]
    assert "In-Reply-To: <original@example.com>" in sent["body"]


def test_each_channel_has_its_own_switch(monkeypatch):
    """a channel
    that is OFF sends nothing even when it is fully configured."""
    import dataclasses

    monkeypatch.undo()  # conftest stubs send_email / send_discord for every test; this one needs the real ones

    def boom(*a, **k):
        raise AssertionError("the channel is off; nothing may be sent")

    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        notifier.settings, gmail_alerts_enabled=False, gmail_address="a@example.com",
        gmail_app_password="pw", alert_email_to="a@example.com",
        discord_alerts_enabled=False, discord_webhook_url="https://discord.example/hook"))
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", boom)
    monkeypatch.setattr(notifier.requests, "post", boom)
    notifier.send_email("subject", "body")
    notifier.send_discord("message")
    # switched back on, the same configuration reaches the channel
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        notifier.settings, discord_alerts_enabled=True))
    with pytest.raises(AssertionError, match="nothing may be sent"):
        notifier.send_discord("message")
