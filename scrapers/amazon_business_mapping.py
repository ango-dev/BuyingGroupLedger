"""Pure mapping from Amazon Business's order pages (HTML) to ledger `OrderItem` rows.

STANDALONE — a deliberate sibling of `scrapers/amazon_mapping.py`, NOT an import/subclass of it. Amazon (consumer) was just live-validated and must not regress, so Amazon
Business gets its own copy that can diverge freely. Accepted cost: the two parsers duplicate logic, so
a future shared fix must be applied in both.

Live capture (2026-08-11, `scripts/amazon_capture.py --out .amazon_business_capture` +
`scripts/amazon_business_paginate_probe.py`, profile-alpha) established that Amazon Business runs on
amazon.com and — for a logged-in business account — serves the SAME order-details `#orderDetails`
`data-component` tree as consumer Amazon. Running the consumer parser on real business fixtures produced
byte-correct rows (status, qty, cost, card, address, delivery date) on 3 orders incl. an in-transit one
shipped to a BFMR warehouse. So `build_order_items` / `parse_shipment_targets` and all the field logic
carry over VERBATIM. It is Path B — server-rendered HTML, NO order JSON endpoint (every JSON response
in the capture was noise: the `biz-companion` assistant, recommendation carousels, map sprites).

TWO business-only differences the capture surfaced (everything else is identical to consumer):

1. DISCOVERY card shape. The business order-history does NOT use consumer's `.order-card`/`.js-order-
   card` (that selector matched only a test placeholder). Each order is an `a-box`/`a-box-group` whose
   row reads "Order placed <Month D, YYYY> · Total … · Ship to …". So `discover_orders` here scopes to
   `order-details?orderID=` links (still the right junk-free scoping) and walks UP from each link to the
   enclosing card to read its "Order placed" date, instead of selecting a card class.

2. PAGINATION (handled in `scrapers/amazon_business_api.py`, not here). Business paginates client-side
   (hash routes `#pagination/N/` + a POST `/ab/your-orders/orderHistory` fragment), NOT via consumer's
   `?timeFilter=&startIndex=` URL params — so the API client click-throughs the pagination control
   rather than navigating startIndex URLs.

The "Track package" link on business order-details is still the consumer `/gp/your-account/ship-track…`
(`a[href*='ship-track']`); a separate `/your-orders/pop` "View your item" link exists but the ship-track
selector already ignores it, so the pt-page tracking-number hop (reusing the pt selectors) carries over.
CAVEAT: that link EXPIRES and is removed from the page once an order is old enough,
leaving only the pop link. Orders inside a normal lookback window always have it, so this only shows up
when an OLD order is re-read (a backfill, or a wide LOOKBACK_DAYS) — and a blank tracking_url can never
erase a number already recorded, because `ledger_sync._merge_row` never lets a blank overwrite.

Row model (keyed on Order ID + Order Date + Item Name + Shipment, like every retailer): one row per
(physical shipment x distinct item); shipments numbered 1..N top-to-bottom (single included); qty
preserved; digital items skipped; cost_per_item = the unit price charged (total_cost is computed).
"""

import logging
import re

from bs4 import BeautifulSoup

from config.warehouses import GIFT_CARD
from scrapers.amazon_mapping import OrderPageShapeError
from models.order import OrderItem, shipment_label

log = logging.getLogger(__name__)

RETAILER = "Amazon Business"
_BASE = "https://www.amazon.com"
ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")
_ORDER_DETAILS_ID_RE = re.compile(r"order-details\?orderID=(\d{3}-\d{7}-\d{7})", re.IGNORECASE)
_ORDER_PLACED_RE = re.compile(r"Order placed\s+([A-Za-z]+ \d{1,2}, \d{4})", re.IGNORECASE)
_ENDING_IN_RE = re.compile(r"ending in\s+(\d{4})", re.IGNORECASE)
_SHIPMENT_ID_RE = re.compile(r"shipmentId=([A-Za-z0-9]+)")
_ASIN_RE = re.compile(r"asin=([A-Z0-9]{10})|/dp/([A-Z0-9]{10})")
_MONEY_RE = re.compile(r"-?\$\s*([\d,]+\.\d{2})")
# Amazon Business RECEIVING CONFIRMATION: the buyer clicks "Mark as received" and the status card
# then reads "All items received <date>" + "N/M items marked as received. Updated by: <name>" instead
# of the usual "Delivered <date>" (live capture 2026-08-23, order 111-9990019, a pallet). It is the
# completion signal for orders Amazon never gets a carrier delivery scan for, so nothing else marks
# them done.
_RECEIVED_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+items?\s+marked as received", re.IGNORECASE)
# The paying card's earn line, printed under the payment method: "Earn 5% back (cap applies) plus an
# extra 1% back on select items". Only the EXTRA is read -- the base rate is cards.json's job.
# ORIGINALLY NOT PORTED HERE, on the assumption that the promo was a consumer-only offer. That was
# WRONG: verified live on order 111-9990009-9990009 (profile-alpha, card 0315) -- a
# Business order-details page carries the identical element, with identical wording, inside the same
# `#orderDetails` region, so Business orders were silently losing the bonus percent.
_EXTRA_PCT_RE = re.compile(r"extra\s+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
_EARN_LINE_SELECTOR = ".pmts-payments-instrument-supplemental-box-paystationpaymentmethod"

#: Every selector this parser depends on, by name — audited by the failure dossier against the
#: captured page (see scrapers/amazon_mapping.SELECTORS for the rationale). Business discovery is
#: link-scoped rather than card-scoped, which is the one entry that differs from consumer Amazon.
#: What proves the Business order-history page RENDERED, orders or not. Both captured pages
#: (.amazon_business_capture/order_history_*.html) carry the time-filter select and the page's own
#: anti-CSRF token input. An empty account renders these with no order links — a legitimate zero.
HISTORY_RENDERED_SELECTOR = "select[name='timeFilter'], #ab-your-orders-anticsrf-token"

SELECTORS: dict[str, str] = {
    "history_container": HISTORY_RENDERED_SELECTOR,
    "history_order_link": "a[href*='order-details?orderID=']",
    "any_link": "a[href]",
    "details_root": "#orderDetails",
    "order_id": "[data-component='orderId']",
    "order_date": "[data-component='orderDate']",
    "order_summary": "[data-component='orderSummary']",
    "shipping_address": "[data-component='shippingAddress']",
    "shipment_status": "[data-component='shipmentStatus']",
    "purchased_items": "[data-component='purchasedItems']",
    "shipment_connections": "[data-component='shipmentConnections']",
    "item_title": "[data-component='itemTitle']",
    "item_quantity": ".od-item-view-qty",
    "item_unit_price": "[data-component='unitPrice']",
    "card_earn_line": _EARN_LINE_SELECTOR,
}
# Order-summary line that only renders when a gift card actually paid part of the order. Twin of the
# consumer rule in scrapers/amazon_mapping.py — kept duplicated because this module is deliberately a
# standalone copy of the Amazon trio.
_GIFT_CARD_RE = re.compile(r"Gift Card Amount:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# What the order is actually worth — the ceiling its shipment cards may not exceed.
_SUBTOTAL_RE = re.compile(r"Item\(s\) Subtotal:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # Amazon also shows abbreviated months in ETAs ("Arriving Aug 14").
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}

# Digital line markers, matched against a SHIPMENT'S STATUS TEXT. A shipment whose status says it was
# delivered electronically has no physical package, so it is never a reimbursable order line.
# "balance is added to your account" is what Amazon prints for a Gift Card Balance Reload — captured
# live as "Applied Gift Card balance is added to your account." after one reached the sheet
# and booked $40.35 of cost against an order that can never ship.
_DIGITAL_MARKERS = ("digital delivery", "ready to redeem", "redeem your", "gift card claim",
                    "balance is added to your account")

# Matched against an ITEM NAME, as a second net for the case where the status card is worded in a way
# we have not seen. Kept DELIBERATELY NARROW — these are literal product names that cannot describe a
# shippable good. Do NOT add a bare "gift card": a physical gift card arrives in a box, has real
# tracking, and dropping it would silently lose a reimbursable line, which is the worse failure
# (CLAUDE.md: a missed order is missed reimbursement money).
_DIGITAL_ITEM_MARKERS = ("gift card balance reload", "egift card", "e-gift card")

# Asked ONLY of a line already known to be digital, to decide keep-vs-skip. Because it is gated behind
# that, it can be broad without endangering a physical product: a "Gift Card Holder Box" SHIPS, so its
# status is an ordinary "Delivered <date>", it is never digital, and it is never tested against this.
_GIFT_CARD_HINTS = ("gift card", "egift", "e-gift", "balance reload")


# --- small pure helpers -------------------------------------------------------------------------
def _num(text) -> float | None:
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_full_date(text: str) -> str:
    """'August 7, 2026' -> '2026-08-07' ('' if not parseable)."""
    if not text:
        return ""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if not m:
        return ""
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return ""
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"


def _parse_status_date(status_text: str, order_date: str, today: str) -> str:
    """Best-effort YYYY-MM-DD from a shipment status card. Works for BOTH the actual delivery date
    ('Delivered <Month> <Day>' / 'Delivered today|yesterday') and the estimated arrival of a
    not-yet-delivered shipment ('Arriving <Month> <Day>', 'Now arriving <Month> <Day>', a
    '… - <Month> <Day>' range — the first date is taken). This keeps the deterministic path's
    delivery_date identical to the agent's, which records the ETA while ordered/shipped and the real
    date once delivered; a later re-check simply overwrites the estimate (a non-blank value overwrites
    in ledger_sync._merge_row), so a delayed ETA updates and the actual date lands on delivery.

    Year is inferred from `order_date` (rolling to next year when the arrival month is before the order
    month, e.g. ordered Dec / arriving Jan). Returns '' when no date can be read (e.g. 'Preparing for
    shipment', 'Not yet shipped')."""
    from datetime import date, timedelta

    low = status_text.lower()
    # A self-receipt "…items marked as received" card is a PAST event just like "Delivered", so a
    # bare weekday on it must resolve backwards, not to the next occurrence.
    is_delivered = low.lstrip().startswith(("delivered", "all items received")) or "marked as received" in low

    def _infer_year(month: int):
        if order_date and len(order_date) >= 7 and order_date[:4].isdigit():
            year = int(order_date[:4])
            if month < int(order_date[5:7]):  # spills into the next year (ordered Dec, arriving Jan)
                year += 1
            return year
        return int(today[:4]) if today and today[:4].isdigit() else None

    # 1. Explicit "<Month> <Day>" (full or abbreviated month) — most precise, prefer it. Scan every
    #    word+number pair so a leading weekday ("Friday, August 14") doesn't block the real month.
    for m in re.finditer(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\b", status_text):
        month = _MONTHS.get(m.group(1).lower().rstrip("."))
        if month is None:
            continue
        year = _infer_year(month)
        if year is None:
            return ""
        try:
            return date(year, month, int(m.group(2))).isoformat()
        except ValueError:
            return ""

    # Relative forms need the run date as a reference.
    try:
        t = date.fromisoformat(today) if today else None
    except ValueError:
        t = None
    if t is None:
        return ""

    # 2. today / tomorrow / yesterday.
    if "today" in low:
        return t.isoformat()
    if "tomorrow" in low:
        return (t + timedelta(days=1)).isoformat()
    if "yesterday" in low:
        return (t - timedelta(days=1)).isoformat()

    # 3. A bare weekday ("Arriving Friday" / "Delivered Tuesday") — Amazon's near-term form. Resolve to
    #    the NEXT occurrence for an arrival (future) or the MOST RECENT for a delivered date (past).
    for name, wd in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            delta = (t.weekday() - wd) % 7 if is_delivered else (wd - t.weekday()) % 7
            return (t - timedelta(days=delta)).isoformat() if is_delivered else (t + timedelta(days=delta)).isoformat()
    return ""


def _status_from_text(status_text: str) -> str:
    """Map a shipment's status header text to a ledger status. 'shipped' is provisional — the
    `_shipped_requires_tracking` invariant downgrades it to 'ordered' when no tracking number is
    present (Amazon shows an 'Arriving …' estimate before a package actually ships)."""
    low = status_text.strip().lower()

    # Self-receipt ("Mark as received"), checked FIRST because none of the keyword rules below match
    # its wording. "5/5 items marked as received" is terminal; a PARTIAL receipt ("3/5") is not — the
    # rest of the order is still outstanding, so it stays open and keeps being tracked. Without this
    # the card fell through to `ordered`, leaving a long-since-received order permanently open: it
    # never rolls up, and while open with no tracking number it is `needs_agent`, so the PAID agent
    # re-reads it on every scheduled run.
    received = _RECEIVED_COUNT_RE.search(status_text)
    if received:
        return "delivered" if int(received.group(1)) == int(received.group(2)) else "ordered"
    if low.startswith("all items received"):
        return "delivered"

    if low.startswith("delivered") or low.startswith("return") or "refund" in low:
        # A returned/refunded item was delivered first; keep it terminal so it stops re-checking.
        return "delivered"
    if "cancel" in low:
        return "cancelled"
    if low.startswith(("arriving", "out for delivery", "shipped", "now arriving", "arrives")):
        return "shipped"
    return "ordered"


def _abs_url(href: str) -> str:
    if not href:
        return ""
    return href if href.startswith("http") else _BASE + href


def _format_address(text: str) -> str:
    """Collapse a shipping-address block's text into one comma-joined line."""
    parts = [p.strip() for p in re.split(r"[\n,]", text or "") if p.strip()]
    # Drop a leading label if the block includes one.
    parts = [p for p in parts if p.lower() not in ("ship to", "shipping address")]
    return ", ".join(dict.fromkeys(parts))  # de-dupe while preserving order


# --- discovery (STEP 1) -------------------------------------------------------------------------
def history_rendered(order_history_html: str) -> bool:
    """Did the order-history page render its container at all? See amazon_mapping.history_rendered."""
    soup = BeautifulSoup(order_history_html or "", "html.parser")
    return soup.select_one(HISTORY_RENDERED_SELECTOR) is not None


def discover_orders(order_history_html: str) -> dict[str, str]:
    """{order_id: order_date(YYYY-MM-DD or '')} for every REAL order on a business order-history page
    (or a paginated fragment).

    Scoped to `order-details?orderID=` links (the account's genuine orders) so recommendation/"Buy
    again" widget ids are excluded. Unlike consumer Amazon, the business order card is NOT `.order-card`
    — each order is an `a-box`/`a-box-group` — so for the date we walk UP from each order-details link
    to the nearest ancestor that carries the card's "Order placed <Month D, YYYY>" text.
    """
    soup = BeautifulSoup(order_history_html or "", "html.parser")
    result: dict[str, str] = {}

    for a in soup.select("a[href*='order-details?orderID=']"):
        m = _ORDER_DETAILS_ID_RE.search(a.get("href", ""))
        if not m:
            continue
        oid = m.group(1)
        date = ""
        node = a
        for _ in range(9):  # walk up to the enclosing order card
            node = node.parent
            if node is None:
                break
            dm = _ORDER_PLACED_RE.search(node.get_text(" ", strip=True))
            if dm:
                date = _parse_full_date(dm.group(1))
                break
        if oid not in result or (date and not result[oid]):
            result[oid] = date

    if not result:
        # Fallback: scope to order-details links anywhere; else nothing (never a bare junk-prone regex).
        for oid in dict.fromkeys(_ORDER_DETAILS_ID_RE.findall(order_history_html or "")):
            result[oid] = ""
    return result


# --- order-details parsing --------------------------------------------------------------------
def _order_region(soup: BeautifulSoup):
    return soup.select_one("#orderDetails") or soup


def _order_id(region, html: str) -> str:
    el = region.select_one("[data-component='orderId']")
    if el:
        m = ORDER_ID_RE.search(el.get_text(" ", strip=True))
        if m:
            return m.group(0)
    m = ORDER_ID_RE.search(html)
    return m.group(0) if m else ""


# The "Track package" link. Amazon serves TWO shapes and BOTH must be accepted:
#   /gp/your-account/ship-track?...   the long-standing one
#   /progress-tracker/package?...     seen live on an order arriving that day
# The page they land on is the same pt page either way, so read_tracking_page needs no change — a
# progress-tracker link was live-verified to yield TBA999000000008 through the existing selectors.
# The trailing "?" on the progress-tracker form is LOAD-BEARING: it keeps the sibling
# /progress-tracker/package/preship/cancel-items link — a "Cancel items" button sitting in the very
# same shipmentConnections block — from being mistaken for a tracking link.
_TRACK_HREF_RE = re.compile(r"ship-track|progress-tracker/package\?", re.IGNORECASE)


def _track_href(node) -> str:
    """The first real tracking link inside `node`, or "". See _TRACK_HREF_RE for the two shapes."""
    for anchor in node.select("a[href]"):
        href = anchor.get("href") or ""
        if "cancel" in href.lower():
            continue
        if _TRACK_HREF_RE.search(href):
            return href
    return ""
def _shipment_wrapper(status_el):
    """The block that holds one shipment: the nearest ancestor of a `shipmentStatus` that also contains
    its `purchasedItems` and its connections (the 'Track package' link / shipmentConnections)."""
    node = status_el
    fallback = None
    for _ in range(8):
        node = node.parent
        if node is None:
            break
        if node.select_one("[data-component='purchasedItems']"):
            fallback = fallback or node
            if _track_href(node) or node.select_one("[data-component='shipmentConnections']"):
                return node
    return fallback


def _item_container(title_el):
    """The per-item block (`.a-fixed-left-grid.a-spacing-base` holding one item's title/qty/price)."""
    return title_el.find_parent("div", class_="a-fixed-left-grid") or title_el.parent


def _shipment_targets(region) -> list[dict]:
    """Per-shipment {shipment, shipmentId, tracking_url, status} in top-to-bottom order — used by the
    API client to fetch each shipment's tracking number from its pt page."""
    targets = []
    for i, status_el in enumerate(region.select("[data-component='shipmentStatus']")):
        wrapper = _shipment_wrapper(status_el)
        if wrapper is None:
            continue
        track_href = _track_href(wrapper)
        hrefs = " ".join(a.get("href", "") for a in wrapper.select("a[href]"))
        m = _SHIPMENT_ID_RE.search(hrefs)
        targets.append({
            "shipment": shipment_label(i + 1),
            "shipmentId": m.group(1) if m else "",
            "tracking_url": _abs_url(track_href) if track_href else "",
            "status": _status_from_text(status_el.get_text(" ", strip=True)),
        })
    return targets


def parse_shipment_targets(order_details_html: str) -> list[dict]:
    region = _order_region(BeautifulSoup(order_details_html or "", "html.parser"))
    return _shipment_targets(region)


def _is_digital_item(item_name: str) -> bool:
    """Second net for a digital line whose SHIPMENT status we don't recognise — see the marker list."""
    low = (item_name or "").lower()
    return any(marker in low for marker in _DIGITAL_ITEM_MARKERS)
def _is_digital_shipment(status_text: str) -> bool:
    low = status_text.lower()
    return any(marker in low for marker in _DIGITAL_MARKERS)


def _promo_cashback_rate(region) -> float | None:
    """The BONUS rate Amazon advertises under the payment method, as a decimal fraction.

    Twin of scrapers/amazon_mapping._promo_cashback_rate -- duplicated, not imported, because this
    module is deliberately a standalone copy of the Amazon trio.

    Amazon prints the paying card's earn line there ("Earn 5% back (cap applies) plus an extra 1% back
    on select items"). Only the "extra N%" is taken; the card's base rate stays in cards.json, and
    config.cards.tag_cards adds the two together. Returns None when there is no such line (most cards).

    Scoped to the payment element rather than the page text so unrelated marketing ("extra 5% off!")
    in a recommendations rail can never be mistaken for this order's promo.
    """
    for el in region.select(_EARN_LINE_SELECTOR):
        m = _EXTRA_PCT_RE.search(el.get_text(" ", strip=True))
        if not m:
            continue
        try:
            rate = float(m.group(1)) / 100
        except ValueError:
            continue
        if 0 < rate <= 1:
            return rate
    return None


def _gift_card_amount(summary_el) -> float | None:
    """"Gift Card Amount: -$14.04" from the order summary, as a POSITIVE number; None when absent."""
    if summary_el is None:
        return None
    m = _GIFT_CARD_RE.search(summary_el.get_text("\n", strip=True))
    amount = _num(m.group(1)) if m else None
    return abs(amount) if amount is not None else None


def _is_gift_card_line(status_text: str, item_name: str) -> bool:
    """Is this digital line a gift card (a reload OR an ordinary gift-card purchase)?"""
    blob = f"{status_text} {item_name}".lower()
    return any(hint in blob for hint in _GIFT_CARD_HINTS)


def _skip_digital(status_text: str, item_name: str, card_last4: str,
                  keep_last4s: frozenset[str]) -> bool:
    """Should this line be dropped as digital?

    Digital lines have no package and are never reimbursable, so they are dropped — WITH ONE EXCEPTION:
    a gift card bought on a card that carries an explicit Amazon rate in cards.json. You only give a
    card a per-retailer rate on purpose, so that card is a reselling card and the gift card is funding
    inventory. Its cost has to land on the ledger, because the balance later pays for an order whose
    cost the scraper nets down (the gift-card accounting rule, the design notes) — without this row the
    profit would be overstated by the gift-card amount. The same purchase on any other card, or on a
    card missing from cards.json, is personal spending and stays out.

    A non-gift-card digital line (a Kindle book, a membership) is dropped on EVERY card: no card makes
    an eBook reimbursable.
    """
    if not (_is_digital_shipment(status_text) or _is_digital_item(item_name)):
        return False
    return not (_is_gift_card_line(status_text, item_name) and card_last4 in keep_last4s)
def _order_subtotal(summary_el) -> float | None:
    """The order summary's "Item(s) Subtotal" — what the order is actually worth."""
    if summary_el is None:
        return None
    m = _SUBTOTAL_RE.search(summary_el.get_text("\n", strip=True))
    return _num(m.group(1)) if m else None


def _reconcile_against_subtotal(rows: list[OrderItem], subtotal: float | None,
                                order_id: str) -> list[OrderItem]:
    """Twin of scrapers/amazon_mapping._reconcile_against_subtotal — keep the two in step.

    Drop shipment cards that would make an order cost MORE than the order is worth.

    Amazon re-issues a NEW TRACKING NUMBER FOR THE SAME PHYSICAL SHIPMENT when one is delayed, and
    while that is in flight the order-details page can render the package TWICE — the superseded card
    and its replacement. Each card carries the whole line, and shipments are numbered by DOM position,
    so the second card lands as a brand-new "Shipment 2" row holding the full quantity and cost again.
    Seen live on a 3-iPad order: two cards against a $2,847 order booked $5,694, which halves the
    reported profit and doubles the cashback basis.

    The invariant that separates that from a GENUINE split: a real split's boxes still sum to the order
    subtotal (three boxes of a qty-3 line are one unit each), while a duplicated card pushes the sum
    ABOVE it. So this can only fire when the page claims more value than the order contains — a
    legitimate multi-box order never trips it.

    Nothing is corrected silently (CLAUDE.md: a cheap path must not guess): the collapse is logged and
    alerted. And if the survivors STILL do not reconcile — e.g. a qty-6 order split 3+3 where one box
    was re-tracked, giving 3/3/3 — quantities are left UNRESOLVED ("*" with a blank cost) rather than
    guessed, the same convention ledger_sync's undisclosed-split net uses and which
    audit_sheet.unresolved_split_quantity exists to surface.
    """
    if subtotal is None or not rows:
        return rows
    before = round(sum(r.total_cost or 0.0 for r in rows), 2)
    if before <= subtotal + 0.01:
        return rows  # the normal path, including every genuine split

    # Keep ONE row per identical (item, quantity, unit price) card. Prefer a card carrying a tracking
    # number, then the LAST such card: Amazon appends the re-issued package, and on the observed order
    # the live number was on the second card while the first held a dead label.
    keep: dict[tuple, OrderItem] = {}
    for row in rows:
        sig = (row.item_name, row.quantity, row.cost_per_item)
        current = keep.get(sig)
        if current is None or bool(row.tracking_number) >= bool(current.tracking_number):
            keep[sig] = row
    survivor_ids = {id(r) for r in keep.values()}
    survivors = [r for r in rows if id(r) in survivor_ids]
    for index, row in enumerate(survivors):
        row.shipment = shipment_label(index + 1)

    after = round(sum(r.total_cost or 0.0 for r in survivors), 2)
    resolved = abs(after - subtotal) <= 0.01
    if not resolved:
        # Can't tell which box holds what. Record it as explicitly unresolved rather than book a number
        # that is wrong: "*" is deliberately not blank, because _merge_row preserves a blank and would
        # quietly keep the inflated figure already on the sheet.
        for row in survivors:
            row.quantity = "*"
            row.total_cost = None

    log.warning(
        "%s: order-details showed %d shipment card(s) worth $%.2f against a $%.2f subtotal — a "
        "re-tracked package rendered twice. Kept %d card(s) worth $%.2f.%s",
        order_id, len(rows), before, subtotal, len(survivors), after,
        "" if resolved else " Still short of the subtotal, so quantities are left unresolved ('*').",
    )
    from alerts.notifier import alert  # local: keeps this module importable without the alert stack

    tail = "" if resolved else (
        "\n\nThe surviving cards still do not match the subtotal, so their quantities are recorded "
        "as '*' and need setting by hand."
    )
    alert(
        f"Amazon Business {order_id}: a shipment was recorded twice",
        f"The order-details page rendered {len(rows)} shipment card(s) totalling ${before:.2f} for an "
        f"order whose subtotal is ${subtotal:.2f} — the hallmark of a delayed package that was "
        f"re-issued a new tracking number while the old card was still on the page.\n\n"
        f"{len(survivors)} card(s) worth ${after:.2f} were kept.{tail}\n\n"
        f"If a row for the superseded tracking number is already on the sheet, clear it with:\n"
        f"  python -m scripts.fix_superseded_shipments --order {order_id}",
    )
    return survivors


def _net_gift_card(rows: list[OrderItem], gift_card: float | None) -> None:
    """Scale an order's cost basis down to what the CARD actually paid. Mutates rows in place.

    A gift card earns 0% cashback, so the recorded cost — and the cashback it drives — must come from
    the card-paid portion only. The sheet computes cashback as (Total Cost + Shipping) * rate, so
    shrinking the basis is all it takes: no second rate, no extra column, no formula change.

    The reduction is CAPPED at the pre-tax item+shipping basis, because Amazon applies a gift card to
    the tax too and this ledger records no tax anywhere — the excess is dropped rather than pushing
    cost negative. Scaling by cost IS cost-weighted proration (the ledger_sync._reprorate_shipping
    rule). total_cost is recomputed by hand: the model's validator only runs at construction.

    Twin of scrapers/amazon_mapping._net_gift_card — keep the two in step.
    """
    if not gift_card or gift_card <= 0 or not rows:
        return
    items_total = sum(r.total_cost or 0.0 for r in rows)
    # shipping is the ORDER-level total repeated on every row, so any row carries it.
    ship = rows[0].shipping or 0.0
    basis = items_total + ship
    if basis <= 0:
        return
    factor = max(0.0, basis - gift_card) / basis
    for r in rows:
        if r.cost_per_item is not None:
            r.cost_per_item = round(r.cost_per_item * factor, 2)
            if r.quantity is not None:
                r.total_cost = round(r.quantity * r.cost_per_item, 2)
        if r.shipping is not None:
            r.shipping = round(r.shipping * factor, 2)


def build_order_items(
    order_details_html: str,
    profile_label: str = "",
    known_open_ids: frozenset[str] | set[str] = frozenset(),
    tracking_by_shipment: dict[str, str] | None = None,
    today: str | None = None,
    net_gift_cards: bool = True,
    keep_digital_last4s: frozenset[str] = frozenset(),
) -> list[OrderItem]:
    """Ledger rows for ONE business order-details page. `tracking_by_shipment` maps a shipment's number
    ('Shipment 1') OR its Amazon shipmentId to a tracking number read from the pt page; absent leaves
    tracking blank (the shipment then stays `ordered` until the number is read)."""
    tracking_by_shipment = tracking_by_shipment or {}
    today = today or __import__("datetime").date.today().isoformat()
    soup = BeautifulSoup(order_details_html or "", "html.parser")
    region = _order_region(soup)
    region_text = region.get_text(" ", strip=True)

    order_id = _order_id(region, order_details_html or "")
    if not order_id:
        return []
    if not region.select(SELECTORS["item_title"]):
        # An order ALWAYS lists its items — even a fully cancelled or all-digital one. Zero title
        # elements means the selector no longer matches, and returning [] here is exactly the silent
        # failure the dossier exists for
        # ledger row(s) ... nothing new in the lookback window" and no alert at all.
        raise OrderPageShapeError(
            f"order {order_id}: order-details page has no item titles "
            f"({SELECTORS['item_title']} matched nothing) — shape changed?"
        )

    date_el = region.select_one("[data-component='orderDate']")
    order_date = _parse_full_date(date_el.get_text(" ", strip=True) if date_el else "")

    card_m = _ENDING_IN_RE.search(region_text)
    card_last4 = card_m.group(1) if card_m else ""

    summary_el = region.select_one("[data-component='orderSummary']")
    shipping = None
    if summary_el:
        st = summary_el.get_text("\n", strip=True)
        sm = re.search(r"Shipping\s*&\s*Handling:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", st, re.IGNORECASE)
        shipping = _num(sm.group(1)) if sm else None

    addr_el = region.select_one("[data-component='shippingAddress']")
    delivery_address = _format_address(addr_el.get_text("\n", strip=True)) if addr_el else ""

    rows: list[OrderItem] = []
    for i, status_el in enumerate(region.select("[data-component='shipmentStatus']")):
        wrapper = _shipment_wrapper(status_el)
        if wrapper is None:
            continue
        status_text = status_el.get_text(" ", strip=True)
        # Digital and not paid on a reselling card -> never wanted. When it IS such a
        # card, defer to the per-item check below, which can see the item name.
        if _is_digital_shipment(status_text) and card_last4 not in keep_digital_last4s:
            continue
        shipment = shipment_label(i + 1)
        status = _status_from_text(status_text)
        cancelled = status == "cancelled"

        track_href = _track_href(wrapper)
        tracking_url = _abs_url(track_href) if track_href else ""
        hrefs = " ".join(a.get("href", "") for a in wrapper.select("a[href]"))
        sid_m = _SHIPMENT_ID_RE.search(hrefs)
        shipment_id = sid_m.group(1) if sid_m else ""
        tracking_number = (
            tracking_by_shipment.get(shipment)
            or tracking_by_shipment.get(shipment_id)
            or ""
        )

        # delivery_date = the actual date once delivered, else the estimated arrival while
        # ordered/shipped (matches the agent). '' for cancelled or when no date is shown.
        delivery_date = "" if status == "cancelled" else _parse_status_date(status_text, order_date, today)

        for title_el in wrapper.select("[data-component='itemTitle']"):
            container = _item_container(title_el)
            item_name = title_el.get_text(" ", strip=True)
            if not item_name:
                continue
            if _skip_digital(status_text, item_name, card_last4, keep_digital_last4s):
                continue
            kept_gift_card = (
                _is_gift_card_line(status_text, item_name)
                and (_is_digital_shipment(status_text) or _is_digital_item(item_name))
            )
            # A kept gift card completes the moment the balance lands: mark it terminal so
            # the order closes instead of sitting in the open list being re-read forever.
            item_status = "delivered" if kept_gift_card else status
            # ...and tag it as DELIBERATELY unrouted. It is a real cost funding inventory, but it will
            # never be submitted to a buying group and will never be paid out on its own — the income
            # arrives through the order the balance pays for, whose cost `_net_gift_card` reduces by
            # this amount. Left blank it would instead read as an ordinary order still awaiting
            # payment, which is what audit_sheet's cogs_inputs_complete counts as a year-boundary
            # straddle. config.warehouses.tag_and_filter_personal preserves this tag.
            item_group = GIFT_CARD if kept_gift_card else ""
            qty_el = container.select_one(".od-item-view-qty")
            quantity = None
            if qty_el:
                qm = re.search(r"\d+", qty_el.get_text())
                quantity = int(qm.group(0)) if qm else None
            price_el = container.select_one("[data-component='unitPrice']")
            cost_per_item = _num(price_el.get_text(" ", strip=True)) if price_el else None

            rows.append(
                OrderItem(
                    retailer=RETAILER,
                    profile_label=profile_label,
                    order_id=order_id,
                    order_date=order_date,
                    status=item_status,
                    buying_group=item_group,
                    order_url=f"{_BASE}/gp/css/order-details?orderID={order_id}",
                    tracking_number=tracking_number,
                    tracking_url=tracking_url,
                    delivery_date=delivery_date,
                    delivery_address=delivery_address,
                    item_name=item_name,
                    quantity=None if cancelled else (quantity or 1),
                    cost_per_item=cost_per_item,
                    shipping=shipping,
                    card_last4=card_last4,
                    shipment=shipment,
                )
            )

    # Brand-new fully-cancelled order -> ignore at discovery (a recorded/open order that has since been
    # cancelled flows through so its rows go terminal). Same rule as Amazon / Best Buy / Costco.
    if rows and all(r.status == "cancelled" for r in rows) and order_id not in (known_open_ids or set()):
        return []

    # Before netting: the gift-card reduction divides by this same basis, so it must
    # not see a basis inflated by a duplicated shipment card.
    rows = _reconcile_against_subtotal(rows, _order_subtotal(summary_el), order_id)
    if net_gift_cards:
        _net_gift_card(rows, _gift_card_amount(summary_el))
    # Rides to config.cards.tag_cards, which folds it into cashback_rate (see OrderItem). The
    # AMAZON_PROMO_CASHBACK_ENABLED toggle already gates BOTH Amazons at the tag_cards call site.
    promo = _promo_cashback_rate(region)
    for row in rows:
        row._promo_cashback_rate = promo
    return rows
