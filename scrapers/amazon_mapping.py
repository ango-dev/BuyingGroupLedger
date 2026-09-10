"""Pure mapping from Amazon's server-rendered order pages (HTML) to ledger `OrderItem` rows.

Amazon has NO order/shipment/cost JSON endpoint (confirmed by live capture 2026-08-10, see
scripts/amazon_capture.py / reference-amazon-order-html): its order pages are server-rendered HTML.
So — unlike Best Buy (ss-api JSON) and Costco (GraphQL) — the deterministic Amazon path PARSES HTML.
This module is that parser, kept free of any network/auth dependency so it is fully unit-testable
offline against captured fixtures (the fragile part when Amazon changes its markup).

Two entry points, both pure:

- `discover_orders(order_history_html)` -> {order_id: order_date} for STEP 1 discovery. IMPORTANT:
  it scopes to `order-details?orderID=` links only — a bare NNN-NNNNNNN-NNNNNNN regex over the page
  picks up JUNK ids from "Buy again"/recommendation widgets whose order-details pages 404.
- `build_order_items(order_details_html, …)` -> ledger rows for ONE order. The modern order-details
  page carries a clean `data-component` tree inside `#orderDetails`: per-shipment `shipmentStatus` +
  `purchasedItems` (per-item `itemTitle`/`unitPrice`, `.od-item-view-qty` when qty>1) + a "Track
  package" `ship-track` link, plus order-level `orderDate`/`orderId`/`shippingAddress`/`orderSummary`
  and a "ending in NNNN" card. `#orderDetails` scoping excludes the recommendation carousels below.

Amazon's tracking NUMBER is not on order-details — it lives on the package-tracking ("pt") page. The
API client fetches it per shipment (reusing the scraper's validated `read_tracking_page`) and passes
it here as `tracking_by_shipment` so the `_shipped_requires_tracking` invariant can promote a shipment
to `shipped`. With no number a shipment stays `ordered` (self-heals once the number is read).

Row model (keyed on Order ID + Order Date + Item Name + Shipment, like every retailer): one row per
(physical shipment x distinct item); shipments numbered 1..N top-to-bottom (single included); qty
preserved; digital items skipped; cost_per_item = the unit price shown (total_cost is computed).
"""

import logging
import re

from bs4 import BeautifulSoup

from config.warehouses import GIFT_CARD
from models.order import OrderItem, shipment_label

log = logging.getLogger(__name__)

RETAILER = "Amazon"
_BASE = "https://www.amazon.com"
ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")
_ORDER_DETAILS_ID_RE = re.compile(r"order-details\?orderID=(\d{3}-\d{7}-\d{7})", re.IGNORECASE)
_ENDING_IN_RE = re.compile(r"ending in\s+(\d{4})", re.IGNORECASE)
_SHIPMENT_ID_RE = re.compile(r"shipmentId=([A-Za-z0-9]+)")
_ASIN_RE = re.compile(r"asin=([A-Z0-9]{10})|/dp/([A-Z0-9]{10})")
_MONEY_RE = re.compile(r"-?\$\s*([\d,]+\.\d{2})")
# The bonus half of the card's earn line, e.g. "Earn 5% back (cap applies) plus an extra 1% back on
# select items" / "Earns 5% back and extra 1% on items using Amazon Day delivery." Only the EXTRA is
# read — the base rate is cards.json's job.
_EXTRA_PCT_RE = re.compile(r"extra\s+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
_EARN_LINE_SELECTOR = ".pmts-payments-instrument-supplemental-box-paystationpaymentmethod"
# The payment-method list's instrument rows ("Prime Business Card ending in 0315", "Amazon point",
# "Prime for Young Adults cash back") and the related-transactions page's line items — both used by
# the non-card-tender rules declared next to _GIFT_CARD_RE below.
_PAYMENT_INSTRUMENT_SELECTOR = ".pmts-payments-instrument-detail-box-paystationpaymentmethod"
_TRANSACTION_LINE_SELECTOR = ".apx-transactions-line-item-component-container"

#: Every selector this parser depends on, by name — the failure dossier's selector audit runs these
#: against the captured page so a report can say WHICH one stopped matching. The parser itself keeps
#: using its literals below; tests/test_diagnostics.py asserts each of those literals is listed here.
#: What proves the order-history page RENDERED, independent of whether it holds any orders. Both
#: captured consumer pages (.amazon_capture/order_history_*.html) carry the time-filter control
#: exactly once. An account with no orders in the window renders this and zero cards — that is a
#: legitimate empty, not a shape change; the container missing is the shape change.
HISTORY_RENDERED_SELECTOR = "select[name='timeFilter'], #time-filter"

SELECTORS: dict[str, str] = {
    "history_container": HISTORY_RENDERED_SELECTOR,
    "history_order_card": ".order-card, .js-order-card",
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
    # Matches only on the related-transactions page (TRANSACTIONS_URL), so it audits at 0 on an
    # order-details snapshot — the same way the pt-page selectors do.
    "transactions_line_item": _TRANSACTION_LINE_SELECTOR,
}
# Order-summary line that only renders when a gift card actually paid part of the order.
_GIFT_CARD_RE = re.compile(r"Gift Card Amount:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# A cash-back BALANCE spent on the order renders as its own summary line, exactly like a gift card:
# "Prime for Young Adults cash back: -$15.98". The colon AND an amount are required: the same label also appears, without either, in the
# payment-method list, which sits inside this summary element.
_CASH_BACK_USED_RE = re.compile(r"([^\n:]*cash back):\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
# Amazon POINTS (the Prime Business card's rewards) are the one non-card tender the order page does
# NOT price: the payment-method list gains an "Amazon point" instrument and the summary still shows
# the full Grand Total. The amount lives ONLY on the related-transactions
# page, as "Amazon Points used  -$48.28" — see points_used_from_transactions().
_POINTS_INSTRUMENT_RE = re.compile(r"^\s*Amazon\s+points?\s*$", re.IGNORECASE)
TRANSACTIONS_URL = "https://www.amazon.com/cpe/yourpayments/transactions?transactionTag={}"
_POINTS_USED_RE = re.compile(r"Amazon\s+points\s+used", re.IGNORECASE)
# The tax line renders on every order summary, usually as $0.00 (the resale certificate).
_TAX_RE = re.compile(r"Estimated tax to be collected:?\s*\n?\s*(-?\$\s*[\d,]+\.\d{2})", re.IGNORECASE)
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
class OrderPageShapeError(ValueError):
    """An order-details page parsed as an order but not as one this parser understands."""


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
    is_delivered = low.lstrip().startswith("delivered")

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
    """Did the order-history page render its container (time filter) at all?

    Paired with an empty `discover_orders` this separates "no orders" (return nothing, quietly)
    from "the markup changed" (fail loudly with a dossier). Only the container counts — an empty
    card selector is exactly what an empty account looks like.
    """
    soup = BeautifulSoup(order_history_html or "", "html.parser")
    return soup.select_one(HISTORY_RENDERED_SELECTOR) is not None


def discover_orders(order_history_html: str) -> dict[str, str]:
    """{order_id: order_date(YYYY-MM-DD or '')} for every REAL order on the order-history page.

    Scoped to `order-details?orderID=` links (the account's genuine orders) so recommendation/"Buy
    again" widget ids are excluded. Date is read best-effort from each order card's 'Order placed …'.
    """
    soup = BeautifulSoup(order_history_html or "", "html.parser")
    result: dict[str, str] = {}

    cards = soup.select(".order-card, .js-order-card")
    for card in cards:
        hrefs = " ".join(a.get("href", "") for a in card.select("a[href]"))
        m = _ORDER_DETAILS_ID_RE.search(hrefs)
        if not m:
            continue
        oid = m.group(1)
        date = _parse_full_date(card.get_text(" ", strip=True))
        if oid not in result or (date and not result[oid]):
            result[oid] = date

    if not result:
        # Fallback: scope to order-details links anywhere; else nothing (never the bare junk-prone regex).
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
def _promo_cashback_rate(region) -> float | None:
    """The BONUS rate Amazon advertises under the payment method, as a decimal fraction.

    Amazon prints the paying card's earn line there ("Earn 5% back (cap applies) plus an extra 1% back
    on select items"). Only the "extra N%" is taken; the card's base rate stays in cards.json, and
    config.cards.tag_cards adds the two together. Returns None when there is no such line (most cards).

    Scoped to the payment element rather than the page text so unrelated marketing ("extra 5% off!")
    in a recommendations rail can never be mistaken for this order's promo.

    Caveat worth knowing: two orders on the same card have been seen carrying byte-identical text, so
    this may be card-level marketing rather than proof the delay promo was taken. That is why
    AMAZON_PROMO_CASHBACK_ENABLED exists.
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
    """"Gift Card Amount: -$14.04" from the order summary, as a POSITIVE number.

    A parsed summary WITHOUT the line is a real 0.0, not a blank — the line only renders when a gift card
    actually paid part of the order, so its absence from a summary we did parse IS the detection.
    None only when there is no summary at all (the page didn't parse; a blank never overwrites).
    """
    if summary_el is None:
        return None
    m = _GIFT_CARD_RE.search(summary_el.get_text("\n", strip=True))
    return abs(_num(m.group(1)) or 0.0) if m else 0.0


def _cash_back_used(summary_el) -> float | None:
    """A cash-back balance spent on the order ("Prime for Young Adults cash back: -$15.98"), as a
    POSITIVE number; several such lines sum. Same 0-vs-None rule as _gift_card_amount."""
    if summary_el is None:
        return None
    total = 0.0
    for _label, amount in _CASH_BACK_USED_RE.findall(summary_el.get_text("\n", strip=True)):
        total += abs(_num(amount) or 0.0)
    return round(total, 2)


def uses_points(region) -> bool:
    """True when the payment-method list names Amazon points as one of the order's tenders."""
    return any(_POINTS_INSTRUMENT_RE.match(el.get_text(" ", strip=True) or "")
               for el in region.select(_PAYMENT_INSTRUMENT_SELECTOR))


def order_uses_points(order_details_html: str) -> bool:
    """`uses_points` straight off the page HTML — what the API client asks before spending a page
    load on the related-transactions page. False for every order that paid by card alone."""
    return uses_points(_order_region(BeautifulSoup(order_details_html or "", "html.parser")))


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


def rewards_used_amount(summary_el, region, points_used: float | None, order_id: str = "") -> float | None:
    """Amazon REWARDS spent on the order — a cash-back balance + Amazon points — for the Rewards Used
    column. NOT a gift card: the user nets every Amazon reward out of COGS at
    year end outside the sheet, so the order keeps its FULL cost here and only the cashback basis
    shrinks (the card earns nothing on dollars it never paid). The COGS formula does that.

    None ("unknown": a blank, which never overwrites) when the summary did not parse, or when the
    page says points were used but their amount could not be read — a 0 there would be a lie, and
    the API client raises a dossier problem for that case so the run says so out loud.
    """
    if summary_el is None:
        return None
    total = _cash_back_used(summary_el) or 0.0
    if uses_points(region):
        if points_used is None:
            log.warning("Amazon order %s was paid partly with Amazon points but the amount is unknown "
                        "— Rewards Used left blank rather than understated.", order_id)
            return None
        total += points_used
    return round(total, 2)


def _sales_tax_amount(summary_el) -> float | None:
    """"Estimated tax to be collected: $1.19" from the order summary.

    Same 0-vs-None rule as _gift_card_amount: a parsed summary without the tax line reads as a real
    $0.00, and only a missing summary comes back None so a blank never
    overwrites a figure on the sheet.
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
    """Amazon sometimes SPLITS one line's quantity into several item blocks inside ONE shipment
    card — and the qty-1 block carries no `.od-item-view-qty` badge at all. Live on
    113-9990028-9990028: 4 Apple Watches rendered as a qty-3 block plus a badgeless block, so the
    order parsed as two rows (3 and 1) under the SAME upsert key, and ledger_sync's collapse kept
    one and silently dropped the +1.

    Two same-item rows in the SAME shipment can only be that split, so they sum: the upsert key has
    no column that could ever tell them apart. Same item across DIFFERENT shipments is a genuine
    split and is untouched. Runs AFTER _reconcile_against_subtotal so a duplicated (re-tracked)
    card has already been dropped rather than summed, and skips anything that helper left
    unresolved ('*') or cancelled (quantity None).
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
    """Two DISTINCT lines can share a title inside ONE shipment: the same product bought from two
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


def _reconcile_against_subtotal(rows: list[OrderItem], subtotal: float | None,
                                order_id: str) -> list[OrderItem]:
    """Drop shipment cards that would make an order cost MORE than the order is worth.

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
        f"Amazon {order_id}: a shipment was recorded twice",
        f"The order-details page rendered {len(rows)} shipment card(s) totalling ${before:.2f} for an "
        f"order whose subtotal is ${subtotal:.2f} — the hallmark of a delayed package that was "
        f"re-issued a new tracking number while the old card was still on the page.\n\n"
        f"{len(survivors)} card(s) worth ${after:.2f} were kept.{tail}\n\n"
        f"If a row for the superseded tracking number is already on the sheet, mark it superseded "
        f"(the row stays, its money is blanked) with:\n"
        f"  python -m scripts.fix_superseded_shipments --order {order_id}",
    )
    return survivors


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
    """Ledger rows for ONE order-details page. `tracking_by_shipment` maps a shipment's number
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
    # Both ORDER-LEVEL like shipping: repeated on every row, prorated cost-weighted at sync, and
    # netted by the COGS formula (gift card subtracted — a tender the card never spent, so it earns
    # no cashback; tax added). This replaced _net_gift_card's silent cost-scaling (2026-08-30):
    # same algebra, but Total Cost now stays the GROSS number the order page shows and the amount is
    # visible on the sheet. The toggle keeps its old name and meaning — netting off means the amount
    # is simply not emitted, so COGS uses the full sticker cost.
    gift_card = (_gift_card_amount(summary_el) if net_gift_cards else None)
    # Rewards SPENT (a Prime cash-back balance + Amazon points, 2026-09-07/08) are their OWN column,
    # not a gift card: the cost stays full and only the cashback basis shrinks — see
    # rewards_used_amount. Gated by the same toggle.
    rewards_used = (rewards_used_amount(summary_el, region, points_used, order_id) if net_gift_cards else None)
    sales_tax = _sales_tax_amount(summary_el)

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
                    quantity=None if cancelled else (quantity or 1),
                    cost_per_item=cost_per_item,
                    shipping=shipping,
                    gift_card=gift_card,
                    rewards_used=rewards_used,
                    sales_tax=sales_tax,
                    card_last4=card_last4,
                    shipment=shipment,
                )
            )
            rows[-1]._seller = seller

    # Brand-new fully-cancelled order -> ignore at discovery (a recorded/open order that has since been
    # cancelled flows through so its rows go terminal). Same rule as Best Buy / Costco.
    if rows and all(r.status == "cancelled" for r in rows) and order_id not in (known_open_ids or set()):
        return []

    # A duplicated shipment card inflates the Total Cost basis the sync's cost-weighted proration
    # (and the COGS netting that reads it) divides over, so the collapse still has to run.
    rows = _reconcile_against_subtotal(rows, _order_subtotal(summary_el), order_id)
    rows = _sum_split_quantity_lines(rows)
    rows = _disambiguate_same_named_lines(rows)
    # Rides to config.cards.tag_cards, which folds it into cashback_rate (see OrderItem).
    promo = _promo_cashback_rate(region)
    for row in rows:
        row._promo_cashback_rate = promo
    return rows
