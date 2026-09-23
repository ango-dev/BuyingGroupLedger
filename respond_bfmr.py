"""Answer BFMR's combined-package emails with the box's serial numbers and receipt PDFs.

BFMR receives a combined Best Buy box and emails asking for the units' serial numbers and the
Best Buy receipt in PDF — and their email says "please reply to this email", so a reply is the
sanctioned channel (their API has no serials or upload endpoint). This module reads the request
mailbox over IMAP — BY DEFAULT the alerts Gmail account (same app password), or its own account
when buying_groups.bfmr.combined_package_gmail_address/_app_password are BOTH set
(settings.bfmr_reply_account() resolves the choice once, so reading and replying can never use
different mailboxes) — matches each request to the ledger rows sharing its tracking number, PULLS THE SERIALS OFF THE BEST BUY SITE for the
box's order ids at that moment (scrapers/bestbuy_serials.py — there is no serial column; only
Best Buy has the combined-package issue), and replies with the orders' captured receipts
attached.

FAIL LOUDLY, NEVER GUESS: any gap — no matching rows, a non-Best-Buy match, serials the site
doesn't show (or the wrong count for the box), a missing or non-PDF receipt — blocks that reply
entirely and raises ONE "ACTION NEEDED" alert naming exactly what's missing. The email stays
unanswered, so the next run retries once the gap is filled.

IDEMPOTENCY IS THE MAILBOX'S OWN `\\Answered` FLAG (searching `UNANSWERED`), which a manual
reply from the phone also sets — exactly the right skip. `.state.json` keeps a backstop record
per Message-ID for the one bad ordering (sent, then the flag write failed): that message is
skipped with an alert instead of being answered twice.

DRY RUN BY DEFAULT — reads the live inbox and the ledger, prints the reply it would send, sends nothing,
flags nothing, writes nothing, and does NOT open a browser (the serial fetch spends a CDP fee,
so it runs only on --apply; pass --serials to preview the full body):
    python -m respond_bfmr

Apply for real:
    python -m respond_bfmr --apply --limit 1                 # supervised first send
    python -m respond_bfmr --tracking 529900000011           # only this box (any BFMR spelling)
    python -m respond_bfmr --apply --serials "SN1,SN2"       # skip the live fetch; refused
                                                             # unless exactly ONE order matches

`run()` is also called from main.py (behind BFMR_COMBINED_PACKAGE_AUTOREPLY_ENABLED, default off) so a
scheduled run answers inside the same run lock, after the scrape has refreshed the ledger.
"""

import argparse
import logging
import urllib.request
from datetime import datetime, timezone

from ledger_db.worksheet import ValueRenderOption

from alerts import notifier
from alerts.notifier import alert, compose
from buying_groups.bfmr import bfmr_spellings
from buying_groups.bfmr_email import (
    attribute_serials, build_reply, parse_request, resolve_box, split_serials,
)
from config.loader import load_state, save_state
from config.settings import settings
from ledger.sync import HEADER, _get_worksheet

log = logging.getLogger("respond_bfmr")

#: How far back the inbox scan reaches. A module constant, not a setting: an unanswered request
#: older than this has been deliberately ignored, and resurfacing it forever helps nobody.
LOOKBACK_DAYS = 30

_STATE_KEY = "bfmr_email_replies"


def _alert(apply: bool, subject: str, message: str) -> None:
    """Alert only on a real run — same reasoning as sync_tracking._alert: a dry run is a person
    already reading this output."""
    if apply:
        alert(subject, message)
    else:
        log.warning("%s: %s (no alert sent — dry run)", subject, message)


def _fetch_pdf(url: str) -> bytes:
    """The receipt bytes: a dashboard-relative Receipt Link (`/receipts/...`, receipts/store.py)
    is read straight off the disk; anything else is fetched over HTTP (a link typed by hand)."""
    from receipts import store

    local = store.path_for_link(url)
    if local is not None:
        return local.read_bytes()
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def _fetch_serials_live(profile_label: str,
                        order_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    """The production serial source: one CDP session on the profile that owns the orders,
    returning serials PER PACKAGE ({order: {tracking: [...], "": unattributed}}). Spends a
    Browser-Use fee, which is why only --apply reaches it."""
    from config.profiles import load_profiles  # local: config/browser stacks only when fetching
    from scrapers.bestbuy_serials import fetch_serials

    profile = next((p for p in load_profiles() if p.label == profile_label), None)
    if profile is None or not profile.profile_id:
        raise RuntimeError(
            f"no profile {profile_label!r} with a profile_id — cannot open the Best Buy "
            f"session to read serials")
    return fetch_serials(profile, order_ids)


def _sender_domains() -> list[str]:
    return [d.strip() for d in settings.bfmr_combined_package_sender_domains.split(",") if d.strip()]


def _preview(msg, serials_pending: bool) -> str:
    """What a dry run shows instead of sending: every header that matters, the body, and each
    attachment's name and size — enough to approve the send without trusting it blind."""
    lines = [f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Subject", "In-Reply-To", "References")
             if msg[h]]
    lines.append("")
    lines.append(msg.get_body(("plain",)).get_content())
    for part in msg.iter_attachments():
        lines.append(f"[attachment: {part.get_filename()}, "
                     f"{len(part.get_payload(decode=True))} bytes]")
    if serials_pending:
        lines.append("[serials not shown: the live Best Buy fetch spends a CDP fee and runs "
                     "only on --apply — pass --serials to preview them]")
    return "\n".join(lines)


def run(apply: bool = False, limit: int | None = None, tracking: str | None = None,
        serials_override: str | None = None, mailbox=None, fetch_pdf=None,
        fetch_serials=None, sheet_values: list[list] | None = None) -> dict:
    """One pass over the unanswered BFMR requests. Returns outcome buckets.

    `mailbox`, `fetch_pdf`, `fetch_serials` and `sheet_values` are injection points for tests;
    production uses the real IMAP mailbox, the object-store URL fetch, the live Best Buy serial
    fetch, and a ledger read.
    """
    outcome: dict[str, list] = {"replied": [], "skipped": [], "needs_manual": [], "failed": []}
    fetch_pdf = fetch_pdf or _fetch_pdf
    fetch_serials = fetch_serials or _fetch_serials_live

    # The mailbox choice (alerts account, or the feature's own) is resolved ONCE, up front —
    # a half-configured pair raises here, loudly, before anything is read or sent.
    reply_address, reply_password = settings.bfmr_reply_account()

    if mailbox is None:
        from alerts.inbox import Mailbox  # local: imaplib/SSL only when actually reading mail
        mailbox = Mailbox(address=reply_address, password=reply_password)

    domains = _sender_domains()
    requests = []
    for uid in mailbox.search_unanswered(domains, since_days=LOOKBACK_DAYS):
        request = parse_request(mailbox.fetch(uid), uid, domains)
        if request is None:
            continue
        if tracking and (request.tracking_number is None
                         or request.tracking_number not in bfmr_spellings(tracking)):
            continue
        requests.append(request)

    if not requests:
        log.info("No unanswered BFMR combined-package emails (last %dd).", LOOKBACK_DAYS)
        return outcome

    if limit is not None:
        requests = requests[:limit]
    log.info("%d unanswered BFMR combined-package request(s): %s", len(requests),
             ", ".join(r.tracking_number or "?" for r in requests))

    if serials_override and len(requests) != 1:
        raise SystemExit(
            f"--serials would apply one serial list to {len(requests)} different emails — "
            f"narrow with --tracking first."
        )

    state = load_state()
    replied_state = dict(state.get(_STATE_KEY) or {})

    header, data_rows = _sheet(sheet_values)

    for request in requests:
        label = request.tracking_number or request.subject
        # The backstop: sent before, but the \Answered flag never landed. Never send twice.
        if request.message_id and request.message_id in replied_state:
            log.warning("Already replied to %s (state record) but the message is still "
                        "UNANSWERED — re-flagging, not re-sending.", label)
            outcome["skipped"].append((label, "already replied per .state.json"))
            _alert(apply, f"BFMR combined package {label}: reply already sent",
                   compose("The reply went out earlier, but marking the email as answered failed; nothing was re-sent.",
                           do="Nothing, if the reply is in Gmail's Sent folder."))
            if apply:
                _try_mark_answered(mailbox, request, label)
            continue

        # Structural gaps first — no browser fee is spent on a box that can't be answered anyway.
        resolution = resolve_box(request, header, data_rows)
        if resolution.missing:
            _needs_manual(outcome, apply, label, resolution.missing)
            continue

        # Serials: the hand-given list, or the live Best Buy fetch (apply only — it costs).
        # Fetched serials come back PER PACKAGE ({tracking: [...]}); attribute_serials picks the
        # ones belonging to THIS box and blocks on any ambiguity.
        serials_of_order: dict[str, list[str]] | None = None
        if serials_override:
            if len(resolution.orders) != 1:
                raise SystemExit(
                    f"--serials would apply one serial list to {len(resolution.orders)} "
                    f"order(s) in box {label} — it only works for a single-order box.")
            (order,) = resolution.orders.values()
            fetched = {order.order_id: {order.tracking_number: split_serials(serials_override)}}
            serials_of_order, gaps = attribute_serials(resolution, fetched)
            if gaps:
                _needs_manual(outcome, apply, label, gaps)
                continue
        elif apply:
            try:
                fetched = _fetch_serials_for_box(resolution, fetch_serials)
            except Exception as exc:
                log.exception("Serial fetch for %s failed", label)
                outcome["failed"].append((label, str(exc)))
                _alert(apply, f"BFMR combined package {label}: auto-reply failed",
                       compose(f"Reading serials from Best Buy failed: {exc}. The email stays unanswered and is "
                               "retried next run.", do="If it repeats, see logs/run.log."))
                continue
            serials_of_order, gaps = attribute_serials(resolution, fetched)
            if gaps:
                _needs_manual(outcome, apply, label, gaps)
                continue

        try:
            pdf_of_order = {}
            for order in resolution.orders.values():
                pdf = fetch_pdf(order.receipt_url)
                if not pdf.startswith(b"%PDF"):
                    raise ValueError(f"receipt for {order.order_id} is not a PDF "
                                     f"({len(pdf)} bytes)")
                pdf_of_order[order.order_id] = pdf
            msg = build_reply(request, resolution, pdf_of_order, serials_of_order or {},
                              from_addr=reply_address,
                              cc=settings.bfmr_combined_package_reply_cc)
        except Exception as exc:
            log.exception("Building the reply for %s failed", label)
            outcome["failed"].append((label, str(exc)))
            _alert(apply, f"BFMR combined package {label}: auto-reply failed",
                   compose(f"{exc}. The email stays unanswered and is retried next run.",
                           do="If it repeats, see logs/run.log."))
            continue

        if not apply:
            print(f"\n--- DRY RUN: would reply for combined package {label} ---")
            print(_preview(msg, serials_pending=serials_of_order is None))
            outcome["skipped"].append((label, "dry run"))
            continue

        try:
            recipients = [request.reply_to] + (
                [settings.bfmr_combined_package_reply_cc] if settings.bfmr_combined_package_reply_cc else [])
            # Via the module, not a bound name, so conftest's network-boundary patch covers it.
            notifier.send_message(msg, recipients, account=(reply_address, reply_password))
        except Exception as exc:
            log.exception("Sending the reply for %s failed", label)
            outcome["failed"].append((label, str(exc)))
            _alert(apply, f"BFMR combined package {label}: auto-reply failed",
                   compose(f"Sending the reply failed: {exc}. The email stays unanswered and is retried next run.",
                           do="If it repeats, see logs/run.log."))
            continue

        log.info("Replied for combined package %s (%d order(s), %d attachment(s)).",
                 label, len(resolution.orders), len(pdf_of_order))
        outcome["replied"].append(label)
        # Send succeeded: record it BEFORE the flag write, so a flag failure can never cause a
        # second send — the state check above catches it next run.
        if request.message_id:
            replied_state[request.message_id] = {
                "tracking": resolution.tracking_number,
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            state[_STATE_KEY] = replied_state
            save_state(state)
        _try_mark_answered(mailbox, request, label)

    return outcome


def _needs_manual(outcome: dict, apply: bool, label: str, gaps: list[str]) -> None:
    detail = "\n".join(f"  - {g}" for g in gaps)
    log.warning("Cannot auto-reply for %s:\n%s", label, detail)
    outcome["needs_manual"].append((label, gaps))
    _alert(apply,
           f"Action needed: BFMR combined package {label} — cannot auto-reply",
           compose("BFMR asked for serial numbers and a PDF receipt, and the reply is blocked by the items "
                   "below. The email is retried on the next run.",
                   do=f"Fix them, then dry-run: python -m respond_bfmr --tracking {label}",
                   items=gaps))


def _fetch_serials_for_box(resolution, fetch_serials) -> dict[str, list[str]]:
    """One serial fetch per profile that owns orders in the box (normally exactly one)."""
    by_profile: dict[str, list[str]] = {}
    for order in resolution.orders.values():
        by_profile.setdefault(order.profile_label, []).append(order.order_id)
    serials: dict[str, list[str]] = {}
    for profile_label, order_ids in by_profile.items():
        serials.update(fetch_serials(profile_label, order_ids))
    return serials


def _try_mark_answered(mailbox, request, label: str) -> None:
    try:
        mailbox.mark_answered(request.uid)
    except Exception:
        log.exception("Could not flag %s as answered; the .state.json record prevents a "
                      "double reply.", label)


def _sheet(sheet_values: list[list] | None) -> tuple[list[str], list[list]]:
    """Header + data rows, from the injected grid or the ledger. Same header guard as
    sync_tracking: a drifted header must never be read positionally."""
    if sheet_values is None:
        worksheet = _get_worksheet()
        sheet_values = worksheet.get_values(value_render_option=ValueRenderOption.unformatted)
    if not sheet_values:
        raise SystemExit("Ledger is empty — nothing to resolve against.")
    header = [str(c) for c in sheet_values[0]]
    if header != list(HEADER):
        raise SystemExit(
            "The ledger's header doesn't match the current schema (the file migrates its own columns "
            f"on open, so this should not happen).\n  ledger:   {header}\n  expected: {list(HEADER)}"
        )
    return header, sheet_values[1:]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Send replies, flag the emails, and allow the live Best Buy serial "
                             "fetch (default: dry run — no send, no flag, no browser fee)")
    parser.add_argument("--limit", type=int,
                        help="Only handle the first N requests — use for first validation")
    parser.add_argument("--tracking",
                        help="Only the request for this tracking number (any BFMR spelling)")
    parser.add_argument("--serials",
                        help="Comma-separated serials, skipping the live fetch — refused "
                             "unless exactly one order matches the box")
    args = parser.parse_args()

    run(apply=args.apply, limit=args.limit, tracking=args.tracking,
        serials_override=args.serials)
    if not args.apply:
        print("\nDry run only — nothing sent, nothing flagged, nothing written. "
              "Re-run with --apply to act.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
