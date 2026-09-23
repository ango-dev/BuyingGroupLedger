import logging
import smtplib
import ssl
from email.mime.text import MIMEText

import requests

from config.settings import settings

log = logging.getLogger(__name__)


def send_email(subject: str, body: str) -> None:
    if not settings.gmail_alerts_enabled:
        log.info("Gmail alerts are off (alerts.gmail_enabled); not emailing: %s", subject)
        return
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


def send_message(msg, recipients: list[str], account: tuple[str, str] | None = None) -> None:
    """Send a PREBUILT MIME message — attachments, threading headers and all.

    `send_email` above deliberately stays a two-string function for alerts; this exists for the
    one caller that needs a real message (respond_bfmr.py replies with PDF attachments and
    In-Reply-To headers). `account` is an (address, app password) pair for a caller whose
    feature may use its own mailbox (settings.bfmr_reply_account()); None = the alerts account.
    Same SMTP_SSL boundary either way — and conftest.py's _block_real_alerts patches THIS name
    for every test, which is why the mechanics live in _smtp_send below: the boundary test
    exercises them while this name stays safely stubbed.
    """
    _smtp_send(msg, recipients, account)


def _smtp_send(msg, recipients: list[str], account: tuple[str, str] | None = None) -> None:
    address, password = account or (settings.gmail_address, settings.gmail_app_password)
    if not address or not password:
        raise RuntimeError("Gmail is not configured (alerts.gmail_address / gmail_app_password).")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(address, password)
        server.sendmail(address, recipients, msg.as_string())


#: Embed colours, the dashboard's own (style.css --gold / --badge / --edge-pass / the accent).
_ACTION, _FAILURE, _RECOVERED, _INFO = 0xE0A800, 0xD64545, 0x2E9E5B, 0x4361B2
_FAILURE_WORDS = ("failed", "not recorded", "unhealthy", "unreadable", "unavailable", "rejected", "could not")
DISCORD_DESCRIPTION_MAX = 4000  # Discord's limits: 4,096 for the description,
DISCORD_FIELD_MAX = 1024        # 1,024 for a field's value,
DISCORD_TITLE_MAX = 256         # 256 for the title
#: The notification text: Discord's
#: @everyone, allowed explicitly below so the ping actually rings.
DISCORD_PING = "@everyone"
_KIND_LABEL = {"alert": "Alert", "health": "Health check", "cap": "Spend limit", "backup": "Backup"}


def _severity(subject: str) -> tuple[str, int]:
    text = subject.lower()
    if text.startswith("action needed"):
        return "Action needed", _ACTION
    if "healthy again" in text:
        return "Recovered", _RECOVERED
    if any(word in text for word in _FAILURE_WORDS):
        return "Failure", _FAILURE
    return "Notice", _INFO


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _sections(body: str) -> dict:
    """compose()'s body taken apart again: {"what", "do", "items", "more", "open", "dossier", "rest"}."""
    out = {"what": [], "do": "", "items": [], "more": "", "open": "", "dossier": "", "rest": []}
    for para in [p.strip() for p in str(body or "").split("\n\n") if p.strip()]:
        lines = para.split("\n")
        if para.startswith("Do: "):
            out["do"] = para[4:].strip()
        elif para.startswith("Open: "):
            out["open"] = para[6:].strip()
        elif para.startswith("Failure dossier: "):
            out["dossier"] = para[len("Failure dossier: "):].strip()
        elif all(line.startswith(("• ", "…and ")) for line in lines):
            out["items"] += [line[2:] for line in lines if line.startswith("• ")]
            out["more"] = next((line for line in lines if line.startswith("…and ")), out["more"])
        elif not out["what"] and not out["items"] and not out["do"]:
            out["what"].append(para)
        else:
            out["rest"].append(para)
    return out


def _items_field(items: list[str], more: str) -> str:
    """The items as one field value inside Discord's 1,024 characters: whole lines only, the rest
    counted."""
    lines, used = [], 0
    for i, item in enumerate(items):
        line = "• " + item
        if used + len(line) + 1 > DISCORD_FIELD_MAX - 40:
            left = len(items) - i
            lines.append(f"…and {left} more" + (f" ({more.split('(', 1)[1]}" if "(" in more else ""))
            return "\n".join(lines)
        lines.append(line)
        used += len(line) + 1
    if more:
        lines.append(more)
    return "\n".join(lines)


def discord_payload(subject: str, body: str = "", *, kind: str = "alert", when=None) -> dict:
    """The webhook's JSON for one alert. The message text is @everyone -- the ping
    and all the notification shows. The embed: the subject as its title, what happened as its
    description, then fields -- severity and type side by side, What to do, the items (counted in
    the field's name), the failure dossier and the dashboard link -- a colour for how urgent it is,
    the time and the app's name. A paragraph compose() did not shape stays in the description."""
    from datetime import datetime, timezone

    subject = " ".join(str(subject or "").split())
    severity, colour = _severity(subject)
    parts = _sections(body)
    description = "\n\n".join(parts["what"] + parts["rest"]) or subject
    fields = [
        {"name": "Severity", "value": severity, "inline": True},
        {"name": "Type", "value": _KIND_LABEL.get(kind, kind.title() or "Alert"), "inline": True},
    ]
    if parts["do"]:
        fields.append({"name": "What to do", "value": _cut(parts["do"], DISCORD_FIELD_MAX), "inline": False})
    if parts["items"]:
        total = len(parts["items"])
        if parts["more"]:
            digits = "".join(c for c in parts["more"].split("more")[0] if c.isdigit())
            total += int(digits) if digits else 0
        fields.append({"name": f"Details ({total})", "value": _items_field(parts["items"], parts["more"]), "inline": False})
    if parts["dossier"]:
        fields.append({"name": "Failure dossier", "value": _cut(parts["dossier"], DISCORD_FIELD_MAX), "inline": False})
    embed = {
        "title": _cut(subject, DISCORD_TITLE_MAX),
        "description": _cut(description, DISCORD_DESCRIPTION_MAX),
        "color": colour,
        "fields": fields,
        "timestamp": (when or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "footer": {"text": "Buying Group Ledger"},
    }
    if parts["open"].startswith(("http://", "https://")):
        embed["url"] = parts["open"]  # the title links to the dashboard page
        fields.append({"name": "Open", "value": parts["open"], "inline": False})
    return {"content": DISCORD_PING, "embeds": [embed], "allowed_mentions": {"parse": ["everyone"]}}


def send_discord(subject: str, body: str = "", *, kind: str = "alert") -> None:
    if not settings.discord_alerts_enabled:
        log.info("Discord alerts are off (alerts.discord_enabled); not posting.")
        return
    if not settings.discord_webhook_url:
        log.warning("Discord webhook not configured, skipping alert")
        return

    response = requests.post(settings.discord_webhook_url, json=discord_payload(subject, body, kind=kind), timeout=10)
    response.raise_for_status()


#: Items one alert lists; the rest are counted, and the whole list goes to run.log.
MAX_ITEMS = 10


def compose(what: str, *, do: str = "", items=(), link: str = "") -> str:
    """Every alert body has ONE shape:
    what happened in a sentence or two, one "Do:" line when there is something to do, the items
    (at most MAX_ITEMS, then a count), and the dashboard page when `web.public_url` is set and a
    `link` path is given. WHY a thing happens is explained in the code and the docs, not in the
    message a phone shows at 3am.

        compose("BFMR refused these tracking numbers.", do="Re-type them.", items=["1Z...: bad"])
    """
    parts = [" ".join(str(what).split())]
    if do:
        parts.append("Do: " + " ".join(str(do).split()))
    lines = [" ".join(str(item).split()) for item in items if str(item).strip()]
    if lines:
        shown = [f"• {line}" for line in lines[:MAX_ITEMS]]
        if len(lines) > MAX_ITEMS:
            shown.append(f"…and {len(lines) - MAX_ITEMS} more (the full list is in logs/run.log)")
            log.info("Alert items, all %d:\n%s", len(lines), "\n".join(lines))
        parts.append("\n".join(shown))
    public = str(getattr(settings, "web_public_url", "") or "").rstrip("/")
    if link and public:
        parts.append(f"Open: {public}{link}")
    return "\n\n".join(parts)


def alert(subject: str, message: str, *, kind: str = "alert") -> None:
    """Fire both alert channels independently so one failing doesn't suppress the other.
    `kind` is the activity-log type the alert is recorded under: "alert" for the app's own,
    "health" for the container healthcheck's (docker/healthcheck.sh sets ALERT_KIND=health), so
    the Activity page can show or hide them apart."""
    # The BODY goes to the log too, not just the subject: it carries the failure dossier links and
    # the classified sign-in verdicts, and an alert that never arrived (email down, webhook
    # rotated) would otherwise leave no local record of either. run.log is where a reader looks.
    log.info("ALERT: %s\n%s", subject, message)
    try:
        from diagnostics import activity  # lazy: alerts is imported everywhere

        activity.record(kind if kind in activity.KINDS else "alert", subject, {"message": message})
    except Exception:  # noqa: BLE001 -- the activity log must never stop an alert
        log.warning("Could not record the alert in the activity log", exc_info=True)
    try:
        send_email(subject, message)
    except Exception:
        log.exception("Failed to send email alert: %s", subject)

    try:
        send_discord(subject, message, kind=kind)
    except Exception:
        log.exception("Failed to send Discord alert: %s", subject)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) >= 3:
        # `python -m alerts.notifier "<subject>" "<body>"` -- for shell callers such as
        # docker/healthcheck.sh, so a dead scheduler reaches the same channels as everything else.
        # ALERT_KIND=health in the environment files it under the Activity page's health type.
        import os

        alert(sys.argv[1], " ".join(sys.argv[2:]), kind=os.environ.get("ALERT_KIND") or "alert")
    else:
        alert("BuyingGroupLedger test alert", "If you see this by email and/or Discord, notifier.py is working.")
