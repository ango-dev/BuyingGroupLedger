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


def send_discord(message: str) -> None:
    if not settings.discord_webhook_url:
        log.warning("Discord webhook not configured, skipping alert")
        return

    response = requests.post(settings.discord_webhook_url, json={"content": message}, timeout=10)
    response.raise_for_status()


def alert(subject: str, message: str) -> None:
    """Fire both alert channels independently so one failing doesn't suppress the other."""
    try:
        send_email(subject, message)
    except Exception:
        log.exception("Failed to send email alert: %s", subject)

    try:
        send_discord(f"**{subject}**\n{message}")
    except Exception:
        log.exception("Failed to send Discord alert: %s", subject)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    alert("BuyingGroupLedger test alert", "If you see this by email and/or Discord, notifier.py is working.")
