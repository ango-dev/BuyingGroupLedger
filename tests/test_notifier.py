"""alerts/notifier.py -- the alert body must reach run.log, not only the channels."""

import logging

from alerts import notifier


def test_the_body_is_logged_even_when_no_channel_is_configured(monkeypatch, caplog):
    monkeypatch.setattr(notifier, "send_email", lambda s, b: None)
    monkeypatch.setattr(notifier, "send_discord", lambda m: None)
    with caplog.at_level(logging.INFO, logger="alerts.notifier"):
        notifier.alert("Amazon [p]: deterministic path failed", "Failure dossier: https://par/x/report.md
  page_1.html: https://par/x/page_1.html")
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
