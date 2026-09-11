import logging
import smtplib
import ssl
from email.mime.text import MIMEText

import requests

from config.settings import settings

log = logging.getLogger(__name__)


def send_email(subject: str, body: str) -> None:
    if not settings.gmail_address or not settings.gmail_app_password or not settings.alert_email_to:
        log.warning("Email alert not configured, skipping: %s", subject)
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.gmail_address
    msg["To"] = settings.alert_email_to

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(settings.gmail_address, settings.gmail_app_password)
        server.sendmail(settings.gmail_address, [settings.alert_email_to], msg.as_string())


def send_message(msg, recipients: list[str]) -> None:
    """Send a PREBUILT MIME message — attachments, threading headers and all.

    `send_email` above deliberately stays a two-string function for alerts; this exists for the
    one caller that needs a real message (respond_bfmr.py replies with PDF attachments and
    In-Reply-To headers). Same account, same SMTP_SSL boundary — and conftest.py's
    _block_real_alerts patches THIS name for every test, which is why the mechanics live in
    _smtp_send below: the boundary test exercises them while this name stays safely stubbed.
    """
    _smtp_send(msg, recipients)


def _smtp_send(msg, recipients: list[str]) -> None:
    if not settings.gmail_address or not settings.gmail_app_password:
        raise RuntimeError("Gmail is not configured (alerts.gmail_address / gmail_app_password).")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(settings.gmail_address, settings.gmail_app_password)
        server.sendmail(settings.gmail_address, recipients, msg.as_string())


def send_discord(message: str) -> None:
    if not settings.discord_webhook_url:
        log.warning("Discord webhook not configured, skipping alert")
        return

    response = requests.post(settings.discord_webhook_url, json={"content": message}, timeout=10)
    response.raise_for_status()


def alert(subject: str, message: str) -> None:
    """Fire both alert channels independently so one failing doesn't suppress the other."""
    # The BODY goes to the log too, not just the subject: it carries the failure dossier links and
    # the classified sign-in verdicts, and an alert that never arrived (email down, webhook
    # rotated) would otherwise leave no local record of either. run.log is where a reader looks.
    log.info("ALERT: %s\n%s", subject, message)
    try:
        send_email(subject, message)
    except Exception:
        log.exception("Failed to send email alert: %s", subject)

    try:
        send_discord(f"**{subject}**\n{message}")
    except Exception:
        log.exception("Failed to send Discord alert: %s", subject)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) >= 3:
        # `python -m alerts.notifier "<subject>" "<body>"` -- for shell callers such as
        # docker/healthcheck.sh, so a dead scheduler reaches the same channels as everything else.
        alert(sys.argv[1], " ".join(sys.argv[2:]))
    else:
        alert("BuyingGroupLedger test alert", "If you see this by email and/or Discord, notifier.py is working.")
