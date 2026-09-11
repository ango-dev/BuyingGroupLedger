"""Read the Gmail inbox the alerts already send from — the account boundary for INBOUND mail.

The same 16-char app password `alerts/notifier.py` uses for SMTP works for IMAP
(imap.gmail.com:993), so reading mail needs no new credential and no OAuth of any kind — a
consumer @gmail.com account cannot be delegated to a service account anyway.

This module is deliberately thin: connection + search + fetch + one flag write. Everything that
understands a particular email's CONTENT (today: BFMR's combined-package requests, in
buying_groups/bfmr_email.py) stays out of here, so it can be tested pure while tests hand the
orchestrator a fake with these same four methods.

WHY `UNANSWERED` AND NOT `UNSEEN`: the user reads this inbox on a phone, so a request they merely
OPENED must still be answered — but one they already replied to by hand must not be answered twice.
The `\\Answered` flag is the mailbox's own record of "was this replied to", set by Gmail for a
manual reply and by `mark_answered` for ours, which makes it the one idempotency signal both paths
share. Searching is also scoped to the configured sender domains at the SERVER — never by subject,
because this repo's own alerts (subjects starting "ACTION NEEDED —") land in this very inbox and
would match any subject-side filter.
"""

import imaplib
import logging
from datetime import date, timedelta

from config.settings import settings

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


class Mailbox:
    """The live IMAP mailbox. Construct only when credentials exist; tests inject a fake."""

    def __init__(self, conn=None):
        if conn is None:
            if not settings.gmail_address or not settings.gmail_app_password:
                raise RuntimeError(
                    "Gmail is not configured (alerts.gmail_address / gmail_app_password) — "
                    "the inbox cannot be read."
                )
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            conn.login(settings.gmail_address, settings.gmail_app_password)
        self._conn = conn
        # readonly=False: mark_answered STOREs a flag. Nothing here ever deletes or moves mail.
        self._conn.select("INBOX")

    def search_unanswered(self, sender_domains: list[str], since_days: int = 30) -> list[str]:
        """UIDs of messages from the given domains with no reply yet, oldest first.

        One SEARCH per domain (IMAP `OR` nests awkwardly across N terms); duplicates collapse.
        `SINCE` bounds the scan so an old, deliberately ignored request doesn't resurface forever.
        """
        since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        uids: list[str] = []
        for domain in sender_domains:
            domain = domain.strip()
            if not domain:
                continue
            typ, data = self._conn.uid("SEARCH", None, "FROM", domain, "UNANSWERED", "SINCE", since)
            if typ != "OK":
                raise RuntimeError(f"IMAP search failed for domain {domain!r}: {typ}")
            for uid in (data[0] or b"").split():
                decoded = uid.decode()
                if decoded not in uids:
                    uids.append(decoded)
        return uids

    def fetch(self, uid: str) -> bytes:
        """The raw RFC822 message. BODY.PEEK so fetching never sets \\Seen — reading the inbox to
        DECIDE must not change what the user's own mail client shows as unread."""
        typ, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"IMAP fetch failed for uid {uid}: {typ}")
        return data[0][1]

    def mark_answered(self, uid: str) -> None:
        """Record that a reply was sent. Only called AFTER the SMTP send succeeded."""
        typ, _ = self._conn.uid("STORE", uid, "+FLAGS", "(\\Answered)")
        if typ != "OK":
            raise RuntimeError(f"IMAP store failed for uid {uid}: {typ}")

    def close(self) -> None:
        try:
            self._conn.close()
            self._conn.logout()
        except Exception:  # closing is best-effort; the work is already done
            log.debug("IMAP close/logout failed", exc_info=True)
