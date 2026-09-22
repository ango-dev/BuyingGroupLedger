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

import diagnostics

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
# Amazon's rebuilt payment widget (a server-rendered Next.js "ViewPurchase" block, first seen live
# 2026-09-11 on order 111-9990024-9990024, THIS retailer) renders the card as separate spans —
# name / "••••" / "0315" — with NO "ending in" text anywhere, so the last 4 must be read from this
# span instead. It is an A/B rollout mid-flight: the same day, profile-bravo's pages still carried
# the old pmts-* list, and the widget also rewrote OLD orders' pages — both shapes stay supported.
_CARD_LAST4_SELECTOR = "[data-testid='payment-instrument-number']"
# The widget's instrument rows. Only the paying card carries a number span; the points instrument
# is renamed ("Amazon point" → "Prime Business Rewards", live on 111-9990010-9990010,
# THIS retailer) and a spent cash-back balance renders as its own numberless row — that one is
# priced by the order-summary line, which survived the widget, and must NOT gate a
# transactions-page read.
_WIDGET_INSTRUMENT_SELECTOR = "[data-testid='payment-instrument']"
_WIDGET_INSTRUMENT_NAME_SELECTOR = "[data-testid='payment-instrument-name']"
_WIDGET_POINTS_NAME_RE = re.compile(r"\b(?:points?|rewards)\b", re.IGNORECASE)
# Since ~2026-09-21 each widget row also carries a FEATURE DETAIL: on the paying card it is the
# earn line the old pmts-* supplemental box used to hold ("Earn 5% back (cap applies) plus an
# extra 1% back on select items" on the Prime Business Card, live on 111-9990023-9990023;
# "$1,071.98 (Earns 5% back)" on the consumer Prime Visa, live on 111-9990016-9990016), and on a
# spent cash-back balance it is the amount applied ("$126.02 applied"). The promo rate is read
# from the card row's detail, and the applied amount stands in for the order-summary line only
# when that line is absent.
_WIDGET_FEATURE_SELECTOR = "[data-testid='payment-instrument-feature-detail']"
_APPLIED_AMOUNT_RE = re.compile(r"(-?\$\s*[\d,]+\.\d{2})\s+applied", re.IGNORECASE)
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
# The payment-method list's instrument rows ("Prime Business Card ending in 0315", "Amazon point",
# "Prime for Young Adults cash back") and the related-transactions page's line items — both used by
# the non-card-tender rules declared next to _GIFT_CARD_RE below.
_PAYMENT_INSTRUMENT_SELECTOR = ".pmts-payments-instrument-detail-box-paystationpaymentmethod"
_TRANSACTION_LINE_SELECTOR = ".apx-transactions-line-item-component-container"
_REWARDS_ENTRY_SELECTOR = '[data-testid="points-history-list-entry"]'

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
    "item_merchant": "[data-component='orderedMerchant']",
    "card_earn_line": _EARN_LINE_SELECTOR,
    "payment_instrument": _PAYMENT_INSTRUMENT_SELECTOR,
    "payment_card_last4": _CARD_LAST4_SELECTOR,
    "payment_instrument_widget": _WIDGET_INSTRUMENT_SELECTOR,
    "payment_instrument_widget_name": _WIDGET_INSTRUMENT_NAME_SELECTOR,
    "payment_instrument_widget_feature": _WIDGET_FEATURE_SELECTOR,
    # Matches only on the related-transactions page (TRANSACTIONS_URL), so it audits at 0 on an
    # order-details snapshot — the same way the pt-page selectors do.
    "transactions_line_item": _TRANSACTION_LINE_SELECTOR,
    # Matches only on the Business Prime Rewards ledger (REWARDS_URL).
    "rewards_history_entry": _REWARDS_ENTRY_SELECTOR,
}
# Order-summary line that only renders when a gift card actually paid part of the order. Twin of the
# consumer rule in scrapers/amazon_mapping.py — kept duplicated because this module is deliberately a
# standalone copy of the Amazon trio.
_GIFT_CARD_RE = re.compile(r"Gift Card Amount:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# The two other NON-CARD tenders, twins of scrapers/amazon_mapping.py (see the notes there): a spent
# cash-back balance is a summary line ("Prime for Young Adults cash back: -$15.98"; colon + amount
# required, the bare label also sits in the payment list inside this element), while Amazon POINTS
# — the Prime Business card's rewards, live on 111-9990010-9990010 — only show as an
# "Amazon point" payment instrument, with the amount on the related-transactions page.
_CASH_BACK_USED_RE = re.compile(r"([^\n:]*cash back):\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
_POINTS_INSTRUMENT_RE = re.compile(r"^\s*Amazon\s+points?\s*$", re.IGNORECASE)
TRANSACTIONS_URL = "https://www.amazon.com/cpe/yourpayments/transactions?transactionTag={}"
_POINTS_USED_RE = re.compile(r"Amazon\s+points\s+used", re.IGNORECASE)
# THE LEDGER: Business Prime Rewards keeps its own points history, one entry per
# order — "Redeeming points - <order> ... -1370", "Earning points - <order> ... +1699" — at 100 points
# to the dollar, under a "since you joined" filter, so ONE page load prices every points order of
# the run at once. It also knows a redemption the moment the order is placed, where the
# related-transactions page shows nothing until the points post (live: 111-9990012-9990012, still
# `ordered`, ledger -1370 = $13.70 of a $15.48 order, transactions page empty). A redemption can be
# PARTIAL, which is why the amount is read and never assumed to be the whole order.
REWARDS_URL = "https://www.amazon.com/businessprime/rewards"
_REWARDS_POINTS_RE = re.compile(r"([+-]\d[\d,]*)\s*$")
_REWARDS_REDEEMING_RE = re.compile(r"\bRedeeming points\b", re.IGNORECASE)
_POINTS_PER_DOLLAR = 100
# The tax line renders on every order summary, usually as $0.00 (the resale certificate).
_TAX_RE = re.compile(r"Estimated tax to be collected:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# What the order is actually worth — the ceiling its shipment cards may not exceed.
_SUBTOTAL_RE = re.compile(r"Item\(s\) Subtotal:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# What the buyer owed — the number the non-card tenders must cover for a card-less order to be
# legitimate (see missing_card_reason).
_GRAND_TOTAL_RE = re.compile(r"Grand Total:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# Amazon's own payment-area error state: the whole order summary rendered as just "Payment method / Unable to display payment
# details at the moment." — an Amazon-side transient; the same page rendered fully on a later read.
_PAYMENT_ERROR_RE = re.compile(r"Unable to display payment details", re.IGNORECASE)
# A shipping promo is its own discount line under Shipping & Handling ("Shipping & Handling: $2.99"
# then "Free Shipping: -$2.99", live on order 111-9990025-9990025, the consumer twin) —
# the Shipping column records what was PAID, so the discount nets against the charge.
_FREE_SHIPPING_RE = re.compile(r"Free Shipping:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# The page's own net of subtotal + shipping − promo discounts — the exact frame the non-card
# tenders can consume (see _cash_back_consumed).
_TOTAL_BEFORE_TAX_RE = re.compile(r"Total before tax:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # Amazon also shows abbreviated months in ETAs ("Arriving Aug 14").
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}

# A MOVED-UP delivery renders the stale estimate UNDER the live one — "Now arriving Monday" in
# the h4, "Previously expected October 19" in a second .od-status-message row. Both land in one status text, and the explicit "<Month> <Day>" scan
# below used to prefer the STALE date over the bare weekday, so the ledger kept October while
# Amazon said next week. Everything from "previously" onward is history, never the answer.
_PREVIOUS_ETA_RE = re.compile(r"(?<![A-Za-z])previously(?![A-Za-z]).*", re.IGNORECASE | re.DOTALL)

# Digital line markers, matched against a SHIPMENT'S STATUS TEXT. A shipment whose status says it was
# delivered electronically has no physical package, so it is never a reimbursable order line.
# "balance is added to your account" is what Amazon prints for a Gift Card Balance Reload — captured
# live as "Applied Gift Card balance is added to your account." after one reached the ledger
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


#: Where each capture-mandatory cell is read from (models.order.CAPTURE_*): the dossier's
#: "could not be read" problems name these so a report says which source stopped matching.
FIELD_SOURCES: dict[str, str] = {
    "order_id": SELECTORS["order_id"],
    "order_date": SELECTORS["order_date"],
    "item_name": SELECTORS["item_title"],
    "quantity": SELECTORS["item_quantity"] + " (absent on a qty-1 line, present with the number otherwise)",
    "cost_per_item": SELECTORS["item_unit_price"],
    "delivery_address": SELECTORS["shipping_address"],
    "card_last4": "'ending in NNNN' in #orderDetails, else " + _PAYMENT_INSTRUMENT_SELECTOR,
}


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
    '… - <Month> <Day>' range — the first date is taken; a trailing 'Previously expected <date>'
    is discarded, see _PREVIOUS_ETA_RE). This keeps the deterministic path's
    delivery_date identical to the agent's, which records the ETA while ordered/shipped and the real
    date once delivered; a later re-check simply overwrites the estimate (a non-blank value overwrites
    in ledger_sync._merge_row), so a delayed ETA updates and the actual date lands on delivery.

    Year is inferred from `order_date` (rolling to next year when the arrival month is before the order
    month, e.g. ordered Dec / arriving Jan). Returns '' when no date can be read (e.g. 'Preparing for
    shipment', 'Not yet shipped')."""
    from datetime import date, timedelta

    # Drop the "Previously expected <date>" tail first: on a moved-up delivery it is the only
    # explicit date on the card, and it is the wrong one.
    status_text = _PREVIOUS_ETA_RE.sub("", status_text)
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
    # never rolls up, so every scheduled run re-reads it for nothing.
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
    texts = [el.get_text(" ", strip=True) for el in region.select(_EARN_LINE_SELECTOR)]
    # The rebuilt widget: the same line sits in the paying card's feature detail (2026-09-21).
    # Only the row that carries the card number -- a cash-back row's detail is an amount applied,
    # and a points row has no earn line to offer.
    for inst in region.select(_WIDGET_INSTRUMENT_SELECTOR):
        if inst.select_one(_CARD_LAST4_SELECTOR) is None:
            continue
        texts.extend(fd.get_text(" ", strip=True) for fd in inst.select(_WIDGET_FEATURE_SELECTOR))
    for text in texts:
        m = _EXTRA_PCT_RE.search(text)
        if not m:
            continue
        try:
            rate = float(m.group(1)) / 100
        except ValueError:
            continue
        if 0 < rate <= 1:
            return rate
    return None


def _cash_back_applied_in_widget(region) -> float | None:
    """The cash-back balance the rebuilt widget says was applied ("$126.02 applied" on a row named
    "... cash back"), as a POSITIVE number; None when no such row carries an amount.

    A fallback for the order-summary line, never a second source: `rewards_used_amount` reads the
    summary first (it survived the widget on every capture so far) and asks here only when the
    summary shows no cash-back line at all."""
    total, found = 0.0, False
    for inst in region.select(_WIDGET_INSTRUMENT_SELECTOR):
        if inst.select_one(_CARD_LAST4_SELECTOR) is not None:
            continue
        name_el = inst.select_one(_WIDGET_INSTRUMENT_NAME_SELECTOR)
        name = name_el.get_text(" ", strip=True) if name_el else ""
        if _CASH_BACK_TENDER_HINT not in name.lower():
            continue
        for fd in inst.select(_WIDGET_FEATURE_SELECTOR):
            m = _APPLIED_AMOUNT_RE.search(fd.get_text(" ", strip=True))
            if m:
                total += abs(_num(m.group(1)) or 0.0)
                found = True
    return round(total, 2) if found else None


def _summary_parsed(summary_el) -> bool:
    """False for a summary STUB — present in the DOM but without even a total.

    Amazon rendered the order
    summary as nothing but "Payment method / Unable to display payment details at the moment.",
    and the 0-vs-None rule below turned that into real $0 Gift Card / Rewards Used / Sales Tax
    cells. A summary that cannot show a Grand Total or an Item(s) Subtotal did not parse: its
    amounts are UNKNOWN, so the caller treats the element as absent and every amount stays blank
    to fill on the open order's next re-read (the same page rendered fully an hour later).
    """
    if summary_el is None:
        return False
    text = summary_el.get_text("\n", strip=True)
    return bool(_GRAND_TOTAL_RE.search(text) or _SUBTOTAL_RE.search(text))


def _gift_card_amount(summary_el) -> float | None:
    """"Gift Card Amount: -$14.04" from the order summary, as a POSITIVE number.

    A parsed summary WITHOUT the line is a real 0.0, not a blank — the line only
    renders when a gift card paid part of the order, so its absence from a parsed summary IS the
    detection. None only when there is no summary at all. Twin of amazon_mapping._gift_card_amount.
    """
    if summary_el is None:
        return None
    m = _GIFT_CARD_RE.search(summary_el.get_text("\n", strip=True))
    return abs(_num(m.group(1)) or 0.0) if m else 0.0


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
def _cash_back_used(summary_el) -> float | None:
    """A cash-back balance spent on the order ("Prime for Young Adults cash back: -$15.98"), as a
    POSITIVE number; several such lines sum. Same 0-vs-None rule as _gift_card_amount. Twin of the consumer rule."""
    if summary_el is None:
        return None
    total = 0.0
    for _label, amount in _CASH_BACK_USED_RE.findall(summary_el.get_text("\n", strip=True)):
        total += abs(_num(amount) or 0.0)
    return round(total, 2)


def _cash_back_consumed(summary_el, applied: float | None = None) -> float | None:
    """`_cash_back_used`, capped at what the order can actually have consumed.

    Live (orders 111-9990007-9990007 and 111-9990025-9990025, one checkout split in
    two, the consumer twin): each order's summary line showed the whole BALANCE ("Prime for Young
    Adults cash back: -$89.10"), so the ledger held $89.10 PER ORDER instead of $89.10 split
    across them. The line is the balance applied at checkout, not the amount spent. What an order
    consumed is what the card was not charged: "Total before tax" (the page's own net of subtotal
    + shipping − promo discounts, e.g. Free Shipping) + tax − gift card − Grand Total; when that
    line is absent (synthetic fixtures), subtotal + shipping stand in. The cap only applies when
    the pieces parse, and a healthy line equals the cap anyway — proven on the 09-05/09-07
    orders."""
    used = _cash_back_used(summary_el)
    if not used and applied is not None:
        used = applied  # the widget's "$X applied" when the summary has no cash-back line
    if not used:
        return used
    text = summary_el.get_text("\n", strip=True)
    tax_m = _TAX_RE.search(text)
    grand_m = _GRAND_TOTAL_RE.search(text)
    if not (tax_m and grand_m):
        return used
    tbt_m = _TOTAL_BEFORE_TAX_RE.search(text)
    if tbt_m:
        base = _num(tbt_m.group(1)) or 0.0
    else:
        sub_m = _SUBTOTAL_RE.search(text)
        ship_m = re.search(r"Shipping\s*&\s*Handling:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", text, re.IGNORECASE)
        if not (sub_m and ship_m):
            return used
        base = (_num(sub_m.group(1)) or 0.0) + (_num(ship_m.group(1)) or 0.0)
    frame = round(base + (_num(tax_m.group(1)) or 0.0) - (_gift_card_amount(summary_el) or 0.0)
                  - abs(_num(grand_m.group(1)) or 0.0), 2)
    if frame < 0:
        return used
    return min(used, frame)


def uses_points(region) -> bool:
    """True when the payment-method list names Amazon points as one of the order's tenders.

    Both shapes are checked: the old pmts-* instrument list ("Amazon point"), and the rebuilt
    widget, where the points tender is a numberless instrument row whose name says points/rewards
    ("Prime Business Rewards"). The no-number-span guard is what keeps a card NAMED "rewards"
    (e.g. "Amazon Rewards Visa") from counting, and a cash-back row is its own tender.
    """
    if any(_POINTS_INSTRUMENT_RE.match(el.get_text(" ", strip=True) or "")
           for el in region.select(_PAYMENT_INSTRUMENT_SELECTOR)):
        return True
    for inst in region.select(_WIDGET_INSTRUMENT_SELECTOR):
        if inst.select_one(_CARD_LAST4_SELECTOR) is not None:
            continue
        name_el = inst.select_one(_WIDGET_INSTRUMENT_NAME_SELECTOR)
        name = name_el.get_text(" ", strip=True) if name_el else ""
        if "cash back" in name.lower():
            continue
        if _WIDGET_POINTS_NAME_RE.search(name):
            return True
    return False


def order_uses_points(order_details_html: str) -> bool:
    """`uses_points` straight off the page HTML — what the API client asks before spending a page
    load on the related-transactions page. False for every order that paid by card alone."""
    return uses_points(_order_region(BeautifulSoup(order_details_html or "", "html.parser")))


def missing_card_reason(order_details_html: str) -> str | None:
    """Why a parsed order with NO readable card is suspicious — or None when the page explains it.

    An unmatched card is exactly how the rebuilt payment widget silently blanked Card Last 4 for a
    run: the parse "succeeded", so no
    dossier was written and nothing alerted until the ledger audit. The API client calls this after
    a successful build whose rows all carry a blank card and, given a reason, files a dossier
    problem WITH the page attached while still RECORDING the rows — one unreadable field must
    alert, not drop reimbursement money (the blank fills itself once the parser is fixed).

    A card-less order is legitimate — and stays quiet — when every shipment is cancelled (what such
    a page renders for payment is unobserved, so no guessing), when points are a tender (their
    amount lives off-page), or when the gift-card + cash-back lines cover the Grand Total.
    An unreadable Grand Total does NOT excuse it: that is a summary-shape change of its own.
    """
    soup = BeautifulSoup(order_details_html or "", "html.parser")
    region = _order_region(soup)
    region_text = region.get_text(" ", strip=True)
    if _ENDING_IN_RE.search(region_text) or _card_last4_from_widget(region):
        return None
    statuses = [_status_from_text(el.get_text(" ", strip=True))
                for el in region.select("[data-component='shipmentStatus']")]
    if statuses and all(s == "cancelled" for s in statuses):
        return None
    if uses_points(region):
        return None
    if _PAYMENT_ERROR_RE.search(region_text):
        return (
            "Amazon's payment area failed to render ('Unable to display payment details at the "
            "moment') — an Amazon-side transient ("
            "the same page rendered fully on a later read); every amount was left blank to fill "
            "on the open order's next re-read"
        )
    summary_el = region.select_one("[data-component='orderSummary']")
    gift = _gift_card_amount(summary_el) or 0.0
    cash = _cash_back_used(summary_el) or _cash_back_applied_in_widget(region) or 0.0
    m = _GRAND_TOTAL_RE.search(summary_el.get_text("\n", strip=True)) if summary_el else None
    total = _num(m.group(1)) if m else None
    if total is not None and gift + cash >= total - 0.005:
        return None
    return (
        "no card last-4 on the page ('ending in NNNN' and the payment-instrument-number span "
        f"both missing), and the non-card tenders (gift card ${gift:.2f} + cash back ${cash:.2f}) "
        "do not cover the Grand Total "
        f"({f'${total:.2f}' if total is not None else 'unreadable'}) — payment shape changed again?"
    )


# The wordings that mark a NON-CARD instrument row as a tender the money columns can price:
# points/rewards go through Rewards Used, these two through Gift Card. A row matching none of the
# known kinds is what unknown_tender_reason exists for.
_CASH_BACK_TENDER_HINT = "cash back"
_GIFT_CARD_TENDER_HINT = "gift card"


def unknown_tender_reason(order_details_html: str) -> str | None:
    """A payment instrument the parser cannot classify — or None when every tender is a known kind.

    The known kinds are the ones the money columns can price: the paying card (a last-4 in either
    shape), Amazon points/rewards, a spent cash-back balance, and a gift-card balance. Anything
    else in the payment list — a renamed points row, a reworded gift-card tender, a tender type
    never seen before — would otherwise become a silent wrong 0 in Gift Card / Rewards Used, so it
    must alert instead. A benign new wording costs one false alarm and
    one hint added beside the ones above; a card-only order has nothing here to fire on.
    """
    soup = BeautifulSoup(order_details_html or "", "html.parser")
    region = _order_region(soup)
    unknown: list[str] = []
    for el in region.select(_PAYMENT_INSTRUMENT_SELECTOR):
        text = el.get_text(" ", strip=True)
        low = text.lower()
        if not text or _ENDING_IN_RE.search(text) or _POINTS_INSTRUMENT_RE.match(text) \
                or _CASH_BACK_TENDER_HINT in low or _GIFT_CARD_TENDER_HINT in low:
            continue
        unknown.append(text)
    for inst in region.select(_WIDGET_INSTRUMENT_SELECTOR):
        if inst.select_one(_CARD_LAST4_SELECTOR) is not None:
            continue  # the paying card (a malformed number is missing_card_reason's job)
        name_el = inst.select_one(_WIDGET_INSTRUMENT_NAME_SELECTOR)
        name = name_el.get_text(" ", strip=True) if name_el else inst.get_text(" ", strip=True)
        low = name.lower()
        if not name or _WIDGET_POINTS_NAME_RE.search(name) \
                or _CASH_BACK_TENDER_HINT in low or _GIFT_CARD_TENDER_HINT in low:
            continue
        unknown.append(name)
    if not unknown:
        return None
    names = ", ".join(f"'{u[:80]}'" for u in dict.fromkeys(unknown))
    return (f"unrecognized payment instrument(s) {names} — a tender the parser cannot price; "
            "if it paid part of the order, Gift Card / Rewards Used and COGS are wrong for it")


def points_used_from_transactions(transactions_html: str, order_id: str) -> float | None:
    """Amazon points spent on `order_id`, read from its related-transactions page, as a POSITIVE
    number. Each line item there is one block holding a label ("Amazon Points used"), an amount
    ("-$48.28") and a link to the order; only blocks naming THIS order count, and several sum.
    None when no such block exists — the page is empty until the points post, or its shape changed
    — so the caller leaves the cell blank and says so rather than writing a false 0."""
    soup = BeautifulSoup(transactions_html or "", "html.parser")
    total, found = 0.0, False
    for block in soup.select(_TRANSACTION_LINE_SELECTOR):
        text = block.get_text(" ", strip=True)
        if order_id not in text or not _POINTS_USED_RE.search(text):
            continue
        amount = _num(text)
        if amount is None:
            continue
        total += abs(amount)
        found = True
    return round(total, 2) if found else None


def points_redeemed_by_order(rewards_html: str) -> dict[str, float]:
    """{order_id: dollars paid with points} from the Business Prime Rewards ledger page.

    Only "Redeeming points" entries count; earning / refund / expiry entries are other kinds. Several
    redemptions against one order sum. An order absent from the result did not redeem points as far
    as the ledger shows — the API client then falls back to the related-transactions page, and only
    after THAT fails does it raise a dossier problem."""
    soup = BeautifulSoup(rewards_html or "", "html.parser")
    out: dict[str, float] = {}
    for entry in soup.select(_REWARDS_ENTRY_SELECTOR):
        text = entry.get_text(" ", strip=True)  # "2026/09/04 Redeeming points - 111-... items... -4828"
        if not _REWARDS_REDEEMING_RE.search(text):
            continue
        oid_m = ORDER_ID_RE.search(text)
        pts_m = _REWARDS_POINTS_RE.search(text)
        if not oid_m or not pts_m:
            continue
        points = abs(int(pts_m.group(1).replace(",", "")))
        out[oid_m.group(0)] = round(out.get(oid_m.group(0), 0.0) + points / _POINTS_PER_DOLLAR, 2)
    return out


def rewards_used_amount(summary_el, region, points_used: float | None, order_id: str = "") -> float | None:
    """A spent cash-back balance + Amazon points, for the Rewards Used column — kept IN the cost,
    out of the cashback basis. None when the summary did not parse or points were used but their
    amount is unknown — a blank never overwrites, a false 0 would. Twin of
    amazon_mapping.rewards_used_amount."""
    if summary_el is None:
        return None
    total = _cash_back_consumed(summary_el, _cash_back_applied_in_widget(region)) or 0.0
    if uses_points(region):
        if points_used is None:
            log.warning("Amazon Business order %s was paid partly with Amazon points but the amount is "
                        "unknown — Rewards Used left blank rather than understated.", order_id)
            return None
        total += points_used
    return round(total, 2)


def _sales_tax_amount(summary_el) -> float | None:
    """"Estimated tax to be collected: $0.87" from the order summary.

    Same 0-vs-None rule as _gift_card_amount: a parsed summary without the tax
    line reads as a real $0.00; only a missing summary comes back None so a blank never overwrites
    a figure on the ledger. Twin of scrapers/amazon_mapping._sales_tax_amount.
    """
    if summary_el is None:
        return None
    m = _TAX_RE.search(summary_el.get_text("\n", strip=True))
    return (_num(m.group(1)) or 0.0) if m else 0.0


def _order_subtotal(summary_el) -> float | None:
    """The order summary's "Item(s) Subtotal" — what the order is actually worth."""
    if summary_el is None:
        return None
    m = _SUBTOTAL_RE.search(summary_el.get_text("\n", strip=True))
    return _num(m.group(1)) if m else None


def _sum_split_quantity_lines(rows: list[OrderItem]) -> list[OrderItem]:
    """Twin of scrapers/amazon_mapping._sum_split_quantity_lines — keep the two in step.

    Amazon sometimes SPLITS one line's quantity into several item blocks inside ONE shipment card,
    the qty-1 block carrying no `.od-item-view-qty` badge at all. Live on
    113-9990028-9990028 (this retailer): 4 Apple Watches rendered 3 + badgeless-1, parsed as two
    rows under the same upsert key, and the collapse dropped the +1. Same-item rows in one
    shipment sum; different shipments are a genuine split, untouched. Runs after
    _reconcile_against_subtotal; skips '*' and cancelled (None) quantities.
    """
    merged: dict[tuple, OrderItem] = {}
    out: list[OrderItem] = []
    for row in rows:
        key = (row.shipment, row.item_name, row.cost_per_item, row.status)
        first = merged.get(key)
        if first is None or not isinstance(row.quantity, int) or not isinstance(first.quantity, int):
            merged.setdefault(key, row)
            out.append(row)
            continue
        first.quantity += row.quantity
        if first.cost_per_item is not None:
            first.total_cost = round(first.quantity * first.cost_per_item, 2)
    return out


def _disambiguate_same_named_lines(rows: list[OrderItem]) -> list[OrderItem]:
    """Twin of scrapers/amazon_mapping._disambiguate_same_named_lines -- keep the two in step.

    Two DISTINCT lines can share a title inside ONE shipment: the same product bought from two
    sellers at two prices, boxed together. Live on 114-9990029-9990029 (Amazon
    Business): one shipment card, two iPad Air blocks -- "Sold by: Amazon" at $626.29 and
    "Sold by: Amazon.com" at $649.00 -- one tracking number. _sum_split_quantity_lines rightly left
    them apart (different prices are not one split line), so both rows reached ledger_sync under
    the SAME upsert key (order + date + item + shipment) and its collapse silently kept one: the
    $649 iPad vanished from the ledger while the box physically held it.

    The upsert key has no other column, so the NAME has to carry the difference. The cheapest line
    (then seller, then page order) keeps its plain name -- an already-recorded row stays matched
    and a re-read cannot swap the rows' prices -- and every other line is suffixed with its seller
    and unit price. Only a collision is renamed: an ordinary order's names never change.
    """
    groups: dict[tuple, list[OrderItem]] = {}
    for row in rows:
        groups.setdefault((row.shipment, row.item_name), []).append(row)
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(
            enumerate(members),
            key=lambda pair: (pair[1].cost_per_item if pair[1].cost_per_item is not None
                              else float("inf"), pair[1]._seller, pair[0]),
        )
        seen = {ordered[0][1].item_name}
        for position, (_, row) in enumerate(ordered[1:], start=2):
            seller = f"Sold by {row._seller}" if row._seller else f"line {position}"
            price = f" @ ${row.cost_per_item:,.2f}" if row.cost_per_item is not None else ""
            name = f"{row.item_name} ({seller}{price})"
            if name in seen:  # same seller AND price twice can only be a split, already summed
                name = f"{row.item_name} ({seller}{price}, line {position})"
            seen.add(name)
            row.item_name = name
    return rows


def _refuse_shared_keys(rows: list[OrderItem], order_id: str) -> None:
    """Twin of scrapers/amazon_mapping._refuse_shared_keys -- keep the two in step: after every
    known same-key shape is handled, two rows of one order sharing (shipment, item name) is an
    unknown page shape, and raising puts the page in the dossier instead of losing a row's money."""
    seen: set[tuple] = set()
    for row in rows:
        key = (row.shipment, row.item_name)
        if key in seen:
            raise OrderPageShapeError(
                f"order {order_id}: two lines share the ledger key (shipment {row.shipment}, "
                f"{row.item_name[:60]!r}) after every known shape was handled — an unknown page "
                f"shape; nothing recorded for this order"
            )
        seen.add(key)


def _collapse_same_package_cards(rows: list[OrderItem], subtotal: float | None,
                                 order_id: str) -> list[OrderItem]:
    """Twin of scrapers/amazon_mapping._collapse_same_package_cards — keep the two in step.

    Two shipment cards of ONE parse carrying the SAME shipmentId are one package — keep one. The
    structural twin of _reconcile_against_subtotal below (which runs after this and stays as the
    backstop). On the re-labelled order of 2026-08-22 (111-9990021-9990021, this retailer) the
    ledger's two duplicate rows both carried the NEW shipmentId in their Tracking Link, which is what
    the double render most likely looked like. A healthy page never repeats a shipmentId across cards,
    so this only ever fires on a duplicate — and needs no `Item(s) Subtotal` line to do so.

    Only IDENTICAL CARDS collapse: two DIFFERENT cards (distinct Shipment labels) sharing one id whose
    item blocks match line for line on (item, quantity, unit price). Two identical blocks INSIDE one
    card are the split-quantity shape (_sum_split_quantity_lines) and are never touched here; two
    same-id cards with different contents are an unknown shape and are left for the subtotal guard.
    Prefer the card that carries a tracking number, tie-break to the LAST (Amazon appends the
    re-issue). Survivors are renumbered 1..N by card. Logged and alerted, never silent (CLAUDE.md).

    THE SUBTOTAL IS A VETO. When `Item(s) Subtotal` is readable and the cards do NOT exceed it, the
    order really contains that much and two same-id cards are not a duplicate this code
    understands -- they are left alone (collapsing would under-count a paid-for unit). So this
    adds protection only where the subtotal guard is blind (no subtotal line) and can never
    remove a unit the order paid for.
    """
    if subtotal is not None and round(sum(r.total_cost or 0.0 for r in rows), 2) <= subtotal + 0.01:
        return rows
    cards: dict[tuple[str, str], list[OrderItem]] = {}  # (package id, shipment label) -> its rows
    for row in rows:
        if row.package_id:
            cards.setdefault((row.package_id, row.shipment), []).append(row)
    by_id: dict[str, list[tuple[str, list[OrderItem]]]] = {}
    for (pid, label), card_rows in cards.items():
        by_id.setdefault(pid, []).append((label, card_rows))
    dropped: set[int] = set()
    for card_list in by_id.values():
        if len(card_list) < 2:
            continue
        by_sig: dict[tuple, list[list[OrderItem]]] = {}
        for _label, card_rows in card_list:
            sig = tuple(sorted((r.item_name, r.quantity, r.cost_per_item) for r in card_rows))
            by_sig.setdefault(sig, []).append(card_rows)
        for dupes in by_sig.values():
            if len(dupes) < 2:
                continue
            keep = dupes[0]
            for card_rows in dupes[1:]:
                if any(r.tracking_number for r in card_rows) >= any(r.tracking_number for r in keep):
                    keep = card_rows
            for card_rows in dupes:
                if card_rows is not keep:
                    dropped.update(id(r) for r in card_rows)
    if not dropped:
        return rows

    survivors = [r for r in rows if id(r) not in dropped]
    # Renumber by CARD, not by row: a multi-SKU card keeps one Shipment number across its rows.
    renumbered: dict[str, str] = {}
    for row in survivors:
        if row.shipment not in renumbered:
            renumbered[row.shipment] = shipment_label(len(renumbered) + 1)
        row.shipment = renumbered[row.shipment]

    ids = sorted({r.package_id for r in rows if id(r) in dropped})
    log.warning(
        "%s: order-details rendered the same package twice (shipmentId %s) — kept %d of %d row(s).",
        order_id, ", ".join(ids), len(survivors), len(rows),
    )
    from alerts.notifier import alert  # local: keeps this module importable without the alert stack

    alert(
        f"{RETAILER} {order_id}: a package was rendered twice",
        f"Two shipment cards on the order-details page carried the same shipmentId "
        f"({', '.join(ids)}) with identical items — a delayed package re-issued a tracking number "
        f"while its old card was still on the page. {len(rows) - len(survivors)} duplicate row(s) "
        f"dropped; the card carrying a tracking number was kept.\n\n"
        f"If a row for the superseded tracking number is already on the ledger, mark it superseded "
        f"(the row stays, its money is blanked) with:\n"
        f"  python -m scripts.fix_superseded_shipments --order {order_id}",
    )
    return survivors


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
    audit_ledger.unresolved_split_quantity exists to surface.
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
        # quietly keep the inflated figure already on the ledger.
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
        f"If a row for the superseded tracking number is already on the ledger, mark it superseded "
        f"(the row stays, its money is blanked) with:\n"
        f"  python -m scripts.fix_superseded_shipments --order {order_id}",
    )
    return survivors


def _card_last4_from_widget(region) -> str:
    """The paying card's last 4 from the rebuilt payment widget, "" when it isn't on the page.

    The prefix span holds the mask dots and a gift-card instrument row has no number span at all,
    so only an exactly-4-digit span counts.
    """
    for el in region.select(_CARD_LAST4_SELECTOR):
        digits = el.get_text(strip=True)
        if re.fullmatch(r"\d{4}", digits):
            return digits
    return ""


def build_order_items(
    order_details_html: str,
    profile_label: str = "",
    known_open_ids: frozenset[str] | set[str] = frozenset(),
    tracking_by_shipment: dict[str, str] | None = None,
    today: str | None = None,
    net_gift_cards: bool = True,
    keep_digital_last4s: frozenset[str] = frozenset(),
    points_used: float | None = None,
) -> list[OrderItem]:
    """Ledger rows for ONE business order-details page. `tracking_by_shipment` maps a shipment's number
    ('Shipment 1') OR its Amazon shipmentId to a tracking number read from the pt page; absent leaves
    tracking blank (the shipment then stays `ordered` until the number is read). `points_used` is
    the Amazon-points amount the API client read off the related-transactions page, for an order
    whose payment list names points; it lands in `rewards_used` (see rewards_used_amount)."""
    tracking_by_shipment = tracking_by_shipment or {}
    today = today or __import__("datetime").date.today().isoformat()
    soup = BeautifulSoup(order_details_html or "", "html.parser")
    region = _order_region(soup)
    region_text = region.get_text(" ", strip=True)

    order_id = _order_id(region, order_details_html or "")
    if not order_id:
        # No order id anywhere on the document: not an order-details page (a sign-in bounce, an
        # error page) or the id moved. Returning [] here recorded nothing and said nothing.
        raise OrderPageShapeError(
            f"order-details page carries no order id ({SELECTORS['order_id']} matched nothing and "
            f"no order-details link names one) -- not an order page, or the shape changed?"
        )
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
    if not order_date:
        # Order Date is part of the upsert key: a blank would file every row under a NEW key
        # (a duplicate of the real row) -- record nothing, loudly, instead.
        raise OrderPageShapeError(
            f"order {order_id}: order date could not be read ({SELECTORS['order_date']} matched "
            f"nothing, or held no 'Month D, YYYY') -- shape changed?"
        )

    card_m = _ENDING_IN_RE.search(region_text)
    card_last4 = card_m.group(1) if card_m else _card_last4_from_widget(region)

    summary_el = region.select_one("[data-component='orderSummary']")
    if not _summary_parsed(summary_el):
        summary_el = None  # a stub is a missing summary: amounts unknown, never a fake 0
        # ...and a dossier problem (2026-09-19): the amounts stay blank to fill on a re-read, but
        # a summary that stopped rendering or parsing is a shape change worth the page.
        # EXCEPT when every shipment is cancelled: Amazon renders the
        # chargeSummary EMPTY -- no Grand Total exists, and there is nothing to fill -- the same
        # rule missing_card_reason keeps. One live card among cancelled ones still reports.
        statuses = [_status_from_text(el.get_text(" ", strip=True))
                    for el in region.select("[data-component='shipmentStatus']")]
        if not (statuses and all(s == "cancelled" for s in statuses)):
            diagnostics.problem(
                f"order {order_id}: the order summary could not be read ({SELECTORS['order_summary']} "
                f"matched nothing, or showed no Grand Total / Item(s) Subtotal) -- Shipping, Sales "
                f"Tax, Gift Card and Rewards Used recorded blank this run")
    shipping = None
    if summary_el:
        st = summary_el.get_text("\n", strip=True)
        sm = re.search(r"Shipping\s*&\s*Handling:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", st, re.IGNORECASE)
        shipping = _num(sm.group(1)) if sm else None
        if shipping is not None:
            fm = _FREE_SHIPPING_RE.search(st)
            if fm:
                shipping = round(max(0.0, shipping - abs(_num(fm.group(1)) or 0.0)), 2)
    # Both ORDER-LEVEL like shipping: repeated on every row, prorated cost-weighted at sync, and
    # netted by the COGS formula (gift card subtracted — a tender the card never spent, so it earns
    # no cashback; tax added). This replaced _net_gift_card's silent cost-scaling (2026-08-30):
    # same algebra, but Total Cost now stays the GROSS number the order page shows and the amount is
    # visible on the ledger. The toggle keeps its old name and meaning — netting off means the
    # gift-card amount is simply not emitted, so COGS uses the full sticker cost.
    gift_card = (_gift_card_amount(summary_el) if net_gift_cards else None)
    # Rewards SPENT (cash-back balance + points) are their own column: full cost kept, no cashback
    # on them — see rewards_used_amount. Same toggle.
    rewards_used = (rewards_used_amount(summary_el, region, points_used, order_id) if net_gift_cards else None)
    sales_tax = _sales_tax_amount(summary_el)

    addr_el = region.select_one("[data-component='shippingAddress']")
    delivery_address = _format_address(addr_el.get_text("\n", strip=True)) if addr_el else ""

    status_cards = region.select("[data-component='shipmentStatus']")
    if not status_cards:
        # Every captured order page carries at least one shipment card (21 of 21, digital orders
        # included); titles with no card means the card selector moved, and the loop below
        # would build zero rows in silence.
        raise OrderPageShapeError(
            f"order {order_id}: order-details page lists items but no shipment cards "
            f"({SELECTORS['shipment_status']} matched nothing) -- shape changed?"
        )
    rows: list[OrderItem] = []
    for i, status_el in enumerate(status_cards):
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
            # A kept gift card completes the moment the balance lands: mark it `paid` with a real
            # $0 payout, $0 insurance and the order date as its payout date. No
            # buying group is ever involved -- the income arrives through the order the balance
            # funds -- so "delivered, awaiting payout" was the wrong reading: it kept the row in the
            # unpaid straddle and the tax report's "not yet paid out" line forever. `paid` is
            # terminal, so the order also closes instead of being re-read every run.
            item_status = "paid" if kept_gift_card else status
            # ...and tag it as DELIBERATELY unrouted. It is a real cost funding inventory, but it will
            # never be submitted to a buying group and will never be paid out on its own — the income
            # arrives through the order the balance pays for, whose COGS drops by this amount via its
            # Gift Card cell. Left blank it would instead read as an ordinary order still awaiting
            # payment, which is what audit_ledger's cogs_inputs_complete counts as a year-boundary
            # straddle. config.warehouses.tag_and_filter_personal preserves this tag.
            item_group = GIFT_CARD if kept_gift_card else ""
            # The quantity element is ABSENT on a qty-1 line and present with the number otherwise
            # (5 titles / 0 elements on 113-9990002's capture; 2 / 1 on 113-9990003's), so absent
            # means 1. Present but without a digit is a reader failure: None, which the client's
            # capture gate reports as a dossier problem (it used to default to 1 in silence --
            # the 2026-09-04 under-count class).
            qty_el = container.select_one(".od-item-view-qty")
            if qty_el is None:
                quantity = 1
            else:
                qm = re.search(r"\d+", qty_el.get_text())
                quantity = int(qm.group(0)) if qm else None
            price_el = container.select_one("[data-component='unitPrice']")
            cost_per_item = _num(price_el.get_text(" ", strip=True)) if price_el else None
            seller_el = container.select_one("[data-component='orderedMerchant']")
            seller = re.sub(r"^\s*sold by:?\s*", "", seller_el.get_text(" ", strip=True),
                            flags=re.IGNORECASE).strip() if seller_el else ""

            rows.append(
                OrderItem(
                    retailer=RETAILER,
                    profile_label=profile_label,
                    order_id=order_id,
                    order_date=order_date,
                    status=item_status,
                    buying_group=item_group,
                    insurance=0.0 if kept_gift_card else None,
                    payout_amount=0.0 if kept_gift_card else None,
                    payout_date=order_date if kept_gift_card else "",
                    # ...and "delivered" the day it was bought: the balance lands at once.
                    delivery_date=order_date if kept_gift_card else delivery_date,
                    order_url=f"{_BASE}/gp/css/order-details?orderID={order_id}",
                    tracking_number=tracking_number,
                    tracking_url=tracking_url,
                    delivery_address=delivery_address,
                    item_name=item_name,
                    quantity=None if cancelled else quantity,
                    cost_per_item=cost_per_item,
                    shipping=shipping,
                    gift_card=gift_card,
                    rewards_used=rewards_used,
                    sales_tax=sales_tax,
                    card_last4=card_last4,
                    shipment=shipment,
                    package_id=shipment_id,
                )
            )
            rows[-1]._seller = seller

    # Brand-new fully-cancelled order -> ignore at discovery (a recorded/open order that has since been
    # cancelled flows through so its rows go terminal). Same rule as Amazon / Best Buy / Costco.
    if rows and all(r.status == "cancelled" for r in rows) and order_id not in (known_open_ids or set()):
        return []

    # A duplicated shipment card inflates the Total Cost basis the sync's cost-weighted proration
    # (and the COGS netting that reads it) divides over, so the collapse still has to run.
    subtotal = _order_subtotal(summary_el)
    rows = _collapse_same_package_cards(rows, subtotal, order_id)
    rows = _reconcile_against_subtotal(rows, subtotal, order_id)
    rows = _sum_split_quantity_lines(rows)
    rows = _disambiguate_same_named_lines(rows)
    _refuse_shared_keys(rows, order_id)
    # Rides to config.cards.tag_cards, which folds it into cashback_rate (see OrderItem). The
    # AMAZON_PROMO_CASHBACK_ENABLED toggle already gates BOTH Amazons at the tag_cards call site.
    promo = _promo_cashback_rate(region)
    for row in rows:
        row.promo_rate = promo
    return rows
