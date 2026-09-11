"""BFMR's combined-package emails: recognise one, resolve it against the ledger, build the reply.

When BFMR receives a combined Best Buy box (several orders, or several packages of one order,
under tracking numbers that collided with another earner's), they email the account asking for
the units' SERIAL NUMBERS and the Best Buy receipt as a PDF — and the email itself says "please
reply to this email", so the reply IS the sanctioned channel. Their API has no serials or
document-upload endpoint (spec re-checked 2026-09-11), which is why this exists as mail handling
rather than another client call.

Everything here is PURE — bytes/rows in, dataclasses/MIME out — so the whole flow is offline-
testable. respond_bfmr.py owns the IMAP/SMTP/sheet edges. NOTHING from any real BFMR email is
hardcoded here: recognition is the sender domain plus a generic
combined-package pattern, and the tracking number is read out of whatever email arrives.
"""

import logging
import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr

from buying_groups.bfmr import bfmr_spellings
from models.order import RETIRED_STATUSES

log = logging.getLogger(__name__)

#: A tracking number as BFMR quotes it: Best Buy's are 10-22 digits, optionally carrying the
#: single letter BFMR itself appends to de-duplicate a combined carton (see bfmr.bfmr_spellings).
_TRACKING_TOKEN = re.compile(r"\b(\d{10,22}[A-Z]?)\b")

#: What makes a BFMR mail a combined-package request, spelled generically: the word "combined"
#: near "package"/"order". Subject observed live: "Action Needed: Combined Best Buy Package <n>";
#: body observed live: "your package as part of a combined Best Buy order". Never keyed on
#: "Action Needed" — this repo's OWN alerts use that phrase and land in the same inbox.
_COMBINED_REQUEST = re.compile(r"combined[^.\n]{0,80}\b(package|order)", re.IGNORECASE)

_HTML_TAG = re.compile(r"<[^>]+>")

#: How a hand-given serial list (--serials) is split into units: commas are the convention
#: ("SN1, SN2"), but semicolons/whitespace shouldn't block a reply.
_SERIAL_SPLIT = re.compile(r"[,;\s]+")


@dataclass
class CombinedPackageRequest:
    """One BFMR combined-package email, reduced to what the reply needs."""

    uid: str
    message_id: str
    references: str
    reply_to: str  # where the reply goes: Reply-To, falling back to From
    from_addr: str
    subject: str
    tracking_number: str | None  # None = recognised as a request, but the number was ambiguous


@dataclass
class OrderInBox:
    """One retailer order's share of the combined box."""

    order_id: str
    order_date: str
    receipt_url: str  # the row's Receipt Link cell; "" when no receipt exists yet
    profile_label: str = ""  # which browser profile owns the order — the serial fetch needs it
    # The LEDGER's bare tracking number for this order's rows in the box — the key the fetched
    # per-package serials are looked up under (Best Buy's site shows the bare number too).
    tracking_number: str = ""
    # The order's OTHER tracking numbers on the sheet (a 2+1 split has one). Empty means the
    # whole order is this one package, which is when unattributed serials are safe to use.
    other_tracking: set = field(default_factory=set)
    rows: list[int] = field(default_factory=list)
    # (item name, quantity) per matched ledger row; serials are fetched live per ORDER, not
    # stored on rows — there is no serial column
    items: list[tuple[str, int]] = field(default_factory=list)

    def quantity(self) -> int:
        """Units of this order in the box = how many serials BFMR expects for it."""
        return sum(qty for _name, qty in self.items)


@dataclass
class BoxResolution:
    """What the ledger knows about the requested box, and everything still in the way."""

    tracking_number: str
    orders: dict[str, OrderInBox] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # empty = the reply can be sent


def _body_text(msg) -> str:
    """The message's text, preferring text/plain, falling back to tag-stripped HTML."""
    plain, html = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        (plain if ctype == "text/plain" else html).append(content)
    if plain:
        return "\n".join(plain)
    return _HTML_TAG.sub(" ", "\n".join(html))


def parse_request(raw: bytes, uid: str, sender_domains: list[str]) -> CombinedPackageRequest | None:
    """Recognise a BFMR combined-package request, or return None for everything else.

    Defense in depth on the sender: the IMAP search already scopes to these domains, but this
    re-checks the parsed From address so a fake mailbox in a test — or a forwarded copy — can't
    slip an unrelated mail through. Recognition is content-generic; see module docstring.
    """
    msg = message_from_bytes(raw, policy=policy.default)
    _, from_addr = parseaddr(str(msg.get("From", "")))
    domain = from_addr.rsplit("@", 1)[-1].lower() if "@" in from_addr else ""
    if domain not in {d.strip().lower() for d in sender_domains if d.strip()}:
        return None

    # A message that is itself a REPLY is never a fresh request. Proven necessary live
    # (2026-09-11): BFMR's Zendesk acknowledged the first auto-reply with a ticket receipt whose
    # subject still said "Combined Best Buy Package …" — answering THAT would loop with their
    # ticket system, and their own footer warns each extra reply sends the ticket to the back of
    # the queue. Their genuine requests arrive with no In-Reply-To.
    if msg.get("In-Reply-To") or msg.get("References"):
        log.info("Ignoring a BFMR conversation reply (not a fresh request): %r",
                 str(msg.get("Subject", ""))[:80])
        return None

    subject = str(msg.get("Subject", ""))
    body = _body_text(msg)
    if not (_COMBINED_REQUEST.search(subject) or _COMBINED_REQUEST.search(body)):
        return None

    # The subject's number wins when present; otherwise the body must name exactly ONE candidate
    # — with several, guessing which box they meant could attach the wrong receipt, so the
    # ambiguity is surfaced (tracking_number=None -> needs_manual) rather than resolved by luck.
    tracking = None
    in_subject = _TRACKING_TOKEN.findall(subject)
    if in_subject:
        tracking = in_subject[0]
    else:
        candidates = list(dict.fromkeys(_TRACKING_TOKEN.findall(body)))
        if len(candidates) == 1:
            tracking = candidates[0]
        elif candidates:
            log.warning("BFMR combined-package email names %d tracking-shaped numbers: %s",
                        len(candidates), candidates)

    _, reply_addr = parseaddr(str(msg.get("Reply-To", "")))
    return CombinedPackageRequest(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip(),
        references=str(msg.get("References", "")).strip(),
        reply_to=reply_addr or from_addr,
        from_addr=from_addr,
        subject=subject,
        tracking_number=tracking,
    )


def split_serials(cell: str) -> list[str]:
    return [s for s in _SERIAL_SPLIT.split(cell.strip()) if s]


def resolve_box(request: CombinedPackageRequest, header: list[str],
                data_rows: list[list]) -> BoxResolution:
    """Pure: which ledger rows are in the requested box, and what still blocks the reply.

    A ledger tracking number T claims the email's number E iff E is one of T's BFMR spellings —
    the bare number or the single letter BFMR appends to de-duplicate a carton. One-directional
    on purpose (extend ours, never strip theirs): see bfmr.bfmr_spellings.

    Every gap lands in `missing` as a sentence a human can act on; ANY gap blocks the reply,
    because a partial answer (serials for one order but not the other) would just move the manual
    work into a support thread. Fail loudly, never guess.
    """
    resolution = BoxResolution(tracking_number=request.tracking_number or "")
    if not request.tracking_number:
        resolution.missing.append(
            "the email did not name exactly one tracking number — read it and handle by hand")
        return resolution

    idx = {name: header.index(name) for name in (
        "Order ID", "Order Date", "Item Name", "Quantity", "Tracking Number", "Status",
        "Receipt Link", "Retailer", "Profile",
    )}

    def cell(row: list, name: str) -> str:
        i = idx[name]
        return str(row[i]).strip() if i < len(row) else ""

    email_number = request.tracking_number
    for offset, row in enumerate(data_rows):
        row_number = offset + 2  # row 1 is the header
        order_id = cell(row, "Order ID")
        tracking = cell(row, "Tracking Number")
        if not order_id or not tracking:
            continue
        if cell(row, "Status").lower() in RETIRED_STATUSES:
            continue  # a superseded row's number is dead; its live twin carries the box now
        if email_number not in bfmr_spellings(tracking):
            continue

        retailer = cell(row, "Retailer")
        if retailer and retailer.lower() != "best buy":
            # Combined packages are a Best Buy phenomenon; a hit on another retailer's row means
            # the number matched the wrong thing, and replying from it would attach the wrong
            # receipt. Surface it rather than filter it silently.
            resolution.missing.append(
                f"row {row_number} ({order_id}): tracking matched a {retailer} row — combined "
                f"packages are Best Buy only; check the number")
            continue

        order = resolution.orders.setdefault(order_id, OrderInBox(
            order_id=order_id,
            order_date=cell(row, "Order Date"),
            receipt_url=cell(row, "Receipt Link"),
            profile_label=cell(row, "Profile"),
            tracking_number=tracking,
        ))
        order.rows.append(row_number)
        if not order.receipt_url:  # any row of the order may carry the link
            order.receipt_url = cell(row, "Receipt Link")
        if not order.profile_label:
            order.profile_label = cell(row, "Profile")

        item_name = cell(row, "Item Name")
        qty_text = cell(row, "Quantity")
        try:
            quantity = int(float(qty_text))
        except (TypeError, ValueError):
            quantity = 0
            resolution.missing.append(
                f"row {row_number} ({order_id}): Quantity {qty_text!r} is not a number — the "
                f"serial count can't be checked")
        order.items.append((item_name, quantity))

    if not resolution.orders:
        resolution.missing.append(
            f"no ledger row carries tracking {email_number} under any BFMR spelling — "
            f"is the box on the sheet?")

    # Second pass: each matched order's OTHER live tracking numbers (a 2+1 split has one).
    # attribute_serials needs this to know whether unattributed serials could belong elsewhere.
    for row in data_rows:
        order_id = cell(row, "Order ID")
        order = resolution.orders.get(order_id)
        if order is None or cell(row, "Status").lower() in RETIRED_STATUSES:
            continue
        tracking = cell(row, "Tracking Number")
        if tracking and tracking != order.tracking_number:
            order.other_tracking.add(tracking)

    for order in resolution.orders.values():
        if not order.receipt_url:
            resolution.missing.append(
                f"order {order.order_id}: Receipt Link is blank — run "
                f"`python -m scripts.backfill_receipts --apply` to capture the receipt first")
        elif not order.receipt_url.lower().endswith(".pdf"):
            resolution.missing.append(
                f"order {order.order_id}: the stored receipt is not a PDF "
                f"({order.receipt_url.rsplit('.', 1)[-1]}) — BFMR asks for PDF; re-capture it")

    return resolution


#: The key the serial fetch uses for serials it could not tie to a package (same "" convention
#: as scrapers.bestbuy_serials.UNATTRIBUTED — spelled here too so this module stays pure and
#: import-free of the scraper stack).
UNATTRIBUTED = ""


def attribute_serials(box: BoxResolution,
                      fetched: dict[str, dict[str, list[str]]],
                      ) -> tuple[dict[str, list[str]], list[str]]:
    """Which serials go in THIS box's reply, per order — and every reason none can. Pure.

    `fetched` is per order {tracking number: [serials], "" (UNATTRIBUTED): [...]} from
    scrapers.bestbuy_serials. An order can ship as several packages and a combined box holds
    only some of them, so the rule is strict:

      1. Serials ATTRIBUTED to the box's own tracking number are used — count must equal the
         box's units for that order.
      2. UNATTRIBUTED serials are used only when the order has NO other tracking number on the
         sheet (the whole order is this package) and the count matches.
      3. Anything else — wrong count, or loose serials on a multi-package order — blocks with
         a gap naming exactly what was found. Never a guess.
    """
    serials_of_order: dict[str, list[str]] = {}
    gaps: list[str] = []
    for order in box.orders.values():
        per_tracking = fetched.get(order.order_id) or {}
        expected = order.quantity()
        attributed = per_tracking.get(order.tracking_number) or []
        loose = per_tracking.get(UNATTRIBUTED) or []

        if attributed:
            if len(attributed) == expected or not expected:
                serials_of_order[order.order_id] = attributed
            else:
                gaps.append(
                    f"order {order.order_id}: {len(attributed)} serial(s) attributed to package "
                    f"{order.tracking_number} but the box holds {expected} unit(s) — check the "
                    f"ledger rows")
        elif loose and not order.other_tracking:
            if len(loose) == expected or not expected:
                serials_of_order[order.order_id] = loose
            else:
                gaps.append(
                    f"order {order.order_id}: found {len(loose)} serial(s) for {expected} "
                    f"unit(s) in the box — refusing to guess which belong to it")
        elif loose:
            others = ", ".join(sorted(order.other_tracking))
            gaps.append(
                f"order {order.order_id}: found {len(loose)} serial(s) but the site did not tie "
                f"them to a package, and the order also shipped under {others} — refusing to "
                f"guess which are in {order.tracking_number}; use --serials")
        else:
            gaps.append(
                f"order {order.order_id}: no serial numbers found on the Best Buy order pages — "
                f"read them off the site/app and re-run with --serials, or run "
                f"`python -m scripts.bestbuy_serial_probe` to pin where they render")
    return serials_of_order, gaps


def build_reply(request: CombinedPackageRequest, box: BoxResolution,
                pdf_of_order: dict[str, bytes], serials_of_order: dict[str, list[str]],
                from_addr: str, cc: str = "") -> EmailMessage:
    """The reply itself: serials in the body, one receipt PDF attached per order, threaded.

    THE RECEIPT LINK NEVER APPEARS ANYWHERE IN THE MESSAGE — it embeds the bucket-level PAR
    secret, and a mail leaves the machine. Bytes only. tests pin the invariant.
    """
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = request.reply_to
    if cc:
        msg["Cc"] = cc
    subject = request.subject.strip()
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if request.message_id:
        msg["In-Reply-To"] = request.message_id
        msg["References"] = f"{request.references} {request.message_id}".strip()

    lines = [
        "Hello,",
        "",
        f"Here are the details for combined Best Buy package {box.tracking_number}:",
        "",
    ]
    # Receipt first, then the serial block — "Serials:" with one bare serial per line (the
    # user's chosen format, 2026-09-11).
    for order in box.orders.values():
        lines.append(f"Order {order.order_id}:")
        for item_name, quantity in order.items:
            lines.append(f"{item_name} (qty {quantity})")
        lines.append(f"Receipt: attached as {order.order_id}.pdf")
        lines.append("")
        lines.append("Serials:")
        lines.extend(serials_of_order.get(order.order_id) or [])
        lines.append("")
    lines.append("Thank you!")
    msg.set_content("\n".join(lines))

    for order_id, pdf in pdf_of_order.items():
        msg.add_attachment(pdf, maintype="application", subtype="pdf",
                           filename=f"{order_id}.pdf")
    return msg
