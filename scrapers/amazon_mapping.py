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

import re

from bs4 import BeautifulSoup

from models.order import OrderItem

RETAILER = "Amazon"
_BASE = "https://www.amazon.com"
ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")
_ORDER_DETAILS_ID_RE = re.compile(r"order-details\?orderID=(\d{3}-\d{7}-\d{7})", re.IGNORECASE)
_ENDING_IN_RE = re.compile(r"ending in\s+(\d{4})", re.IGNORECASE)
_SHIPMENT_ID_RE = re.compile(r"shipmentId=([A-Za-z0-9]+)")
_ASIN_RE = re.compile(r"asin=([A-Z0-9]{10})|/dp/([A-Z0-9]{10})")
_MONEY_RE = re.compile(r"-?\$\s*([\d,]+\.\d{2})")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # Amazon also shows abbreviated months in ETAs ("Arriving Aug 14").
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}

# Digital line markers (best-effort — no digital fixture captured yet; the agent fallback also skips
# digital). A shipment whose status/text is clearly a digital delivery has no physical package.
_DIGITAL_MARKERS = ("digital delivery", "ready to redeem", "redeem your", "gift card claim")


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
            if node.select_one("a[href*='ship-track'], [data-component='shipmentConnections']"):
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
        track = wrapper.select_one("a[href*='ship-track']")
        hrefs = " ".join(a.get("href", "") for a in wrapper.select("a[href]"))
        m = _SHIPMENT_ID_RE.search(hrefs)
        targets.append({
            "shipment": f"Shipment {i + 1}",
            "shipmentId": m.group(1) if m else "",
            "tracking_url": _abs_url(track.get("href")) if track else "",
            "status": _status_from_text(status_el.get_text(" ", strip=True)),
        })
    return targets


def parse_shipment_targets(order_details_html: str) -> list[dict]:
    region = _order_region(BeautifulSoup(order_details_html or "", "html.parser"))
    return _shipment_targets(region)


def _is_digital_shipment(status_text: str) -> bool:
    low = status_text.lower()
    return any(marker in low for marker in _DIGITAL_MARKERS)


def build_order_items(
    order_details_html: str,
    profile_label: str = "",
    known_open_ids: frozenset[str] | set[str] = frozenset(),
    tracking_by_shipment: dict[str, str] | None = None,
    today: str | None = None,
) -> list[OrderItem]:
    """Ledger rows for ONE order-details page. `tracking_by_shipment` maps a shipment's number
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
        if _is_digital_shipment(status_text):
            continue
        shipment = f"Shipment {i + 1}"
        status = _status_from_text(status_text)
        cancelled = status == "cancelled"

        track = wrapper.select_one("a[href*='ship-track']")
        tracking_url = _abs_url(track.get("href")) if track else ""
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
                    status=status,
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
    # cancelled flows through so its rows go terminal). Same rule as Best Buy / Costco.
    if rows and all(r.status == "cancelled" for r in rows) and order_id not in (known_open_ids or set()):
        return []
    return rows
