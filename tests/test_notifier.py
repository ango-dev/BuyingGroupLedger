"""alerts/notifier.py -- the alert body must reach run.log, not only the channels."""

import logging

import pytest

from alerts import notifier


def test_the_body_is_logged_even_when_no_channel_is_configured(monkeypatch, caplog):
    monkeypatch.setattr(notifier, "send_email", lambda s, b: None)
    monkeypatch.setattr(notifier, "send_discord", lambda *a, **k: None)
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


class TestDiscordEmbed:
    """then "for the plain text just make it @everybody, for the embed make it detailed but
    neat and organized"."""

    def test_the_text_pings_everyone_and_the_embed_is_organised(self, monkeypatch):
        import dataclasses
        from datetime import datetime, timezone

        monkeypatch.setattr(notifier, "settings", dataclasses.replace(notifier.settings, web_public_url="http://192.0.2.10:8765"))
        body = notifier.compose("These packages are not submitted.", do="Act on each.", items=["1Z1: bad", "1Z2: worse"],
                                link="/orders")
        payload = notifier.discord_payload("Action needed: BFMR — 2 package(s) could not be submitted", body,
                                           when=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc))
        assert payload["content"] == "@everyone" and payload["allowed_mentions"] == {"parse": ["everyone"]}
        embed = payload["embeds"][0]
        assert embed["title"] == "Action needed: BFMR — 2 package(s) could not be submitted"
        assert embed["description"] == "These packages are not submitted."
        assert embed["color"] == 0xE0A800 and embed["timestamp"] == "2026-09-22T12:00:00+00:00"
        assert embed["footer"] == {"text": "Buying Group Ledger"} and embed["url"] == "http://192.0.2.10:8765/orders"
        assert [(f["name"], f["inline"]) for f in embed["fields"]] == [
            ("Severity", True), ("Type", True), ("What to do", False), ("Details (2)", False), ("Open", False)]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Severity"] == "Action needed" and fields["Type"] == "Alert" and fields["What to do"] == "Act on each."
        assert fields["Details (2)"] == "• 1Z1: bad\n• 1Z2: worse"

    def test_a_dossier_a_long_list_and_the_colours(self):
        body = notifier.compose("Nothing was recorded.", items=[f"order {i}: " + "x" * 80 for i in range(14)]) \
            + "\n\nFailure dossier: logs/failures/costco_p_1\n(open it on the dashboard's Activity page)"
        embed = notifier.discord_payload("Costco [p]: scrape failed — not recorded this run", body, kind="alert")["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert embed["color"] == 0xD64545 and fields["Severity"] == "Failure"
        details = fields["Details (14)"]
        assert len(details) <= notifier.DISCORD_FIELD_MAX and "…and" in details.split("\n")[-1]
        assert fields["Failure dossier"].startswith("logs/failures/costco_p_1")
        health = notifier.discord_payload("Ledger container healthy again", "last run 5m ago.", kind="health")["embeds"][0]
        assert health["color"] == 0x2E9E5B and {f["name"]: f["value"] for f in health["fields"]}["Type"] == "Health check"
        plain = notifier.discord_payload("Only a subject")["embeds"][0]
        assert plain["description"] == "Only a subject" and plain["color"] == 0x4361B2 and "url" not in plain
        assert len(notifier.discord_payload("s", "x" * 5000)["embeds"][0]["description"]) == notifier.DISCORD_DESCRIPTION_MAX

    def test_the_webhook_is_posted_the_payload(self, monkeypatch):
        import dataclasses

        monkeypatch.undo()  # conftest stubs send_discord; this test needs the real one
        sent = {}

        class Response:
            def raise_for_status(self):
                return None

        def post(url, json, timeout):
            sent.update(url=url, json=json)
            return Response()

        monkeypatch.setattr(notifier, "settings", dataclasses.replace(
            notifier.settings, discord_alerts_enabled=True, discord_webhook_url="https://discord.example/hook"))
        monkeypatch.setattr(notifier.requests, "post", post)
        notifier.send_discord("Scheduled backup failed", "The backup could not be written.", kind="backup")
        assert sent["url"] == "https://discord.example/hook" and sent["json"]["content"] == "@everyone"
        embed = sent["json"]["embeds"][0]
        assert embed["title"] == "Scheduled backup failed" and embed["description"] == "The backup could not be written."
        assert {f["name"]: f["value"] for f in embed["fields"]}["Type"] == "Backup"


class TestCompose:
    """every body is what happened,
    one Do: line, the items (at most ten, then a count) and the dashboard page."""

    def test_the_shape(self, monkeypatch):
        import dataclasses

        from config import settings as cs

        monkeypatch.setattr(notifier, "settings", dataclasses.replace(cs.settings, web_public_url="http://192.0.2.10:8765/"))
        body = notifier.compose("BFMR refused   these\n tracking numbers.", do="Re-type them.",
                                items=[f"1Z{i}: bad" for i in range(13)] + ["  "], link="/orders")
        what, do, items, link = body.split("\n\n")
        assert what == "BFMR refused these tracking numbers." and do == "Do: Re-type them."
        lines = items.split("\n")
        assert lines[0] == "• 1Z0: bad" and len(lines) == 11
        assert lines[-1] == "…and 3 more (the full list is in logs/run.log)"
        assert link == "Open: http://192.0.2.10:8765/orders"

    def test_without_do_items_or_an_address_it_is_one_sentence(self, monkeypatch):
        import dataclasses

        from config import settings as cs

        monkeypatch.setattr(notifier, "settings", dataclasses.replace(cs.settings, web_public_url=""))
        assert notifier.compose("Nothing to do.", link="/orders") == "Nothing to do."
