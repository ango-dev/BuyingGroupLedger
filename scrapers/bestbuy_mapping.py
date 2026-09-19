"""Pure mapping from Best Buy's self-service order payload to ledger `OrderItem` rows.

The payload is one order's JSON from `GET /profile/ss/api/v1/orders/<BBY01-…>` (the "ss" order model),
captured live — see reference-bestbuy-order-api and scripts/bestbuy_capture.py. Kept free of
any network/auth dependency so the shaping (the part most likely to drift when Best Buy changes its
schema) is fully unit-testable offline against a captured fixture, exactly like costco_mapping.

Row model (keyed on Order ID + Order Date + Item Name + Shipment, like every retailer):

- One ledger row per (physical fulfillment group) x (distinct SKU in that group). Best Buy's own
  `groups.fulfillmentGroups` already defines the shipments natively — a group is one shipment, and its
  `groupId` is assigned at order time (a qty-N line is pre-split into N groups even before it ships), so
  unlike Costco we never have to guess box membership. Items in one group share a tracking number.
- SHIPMENTS are numbered 1..N across the PHYSICAL (non-digital) groups only, ordered by `groupId` — so
  a single-shipment order is "Shipment 1" and a split reads "Shipment 1 / 2 / …". Digital groups
  (grouping.type "email"/"download") don't consume a number.
- QUANTITY: the ss-api unit-splits a line (each item is quantity 1, the same SKU repeated), so we group
  a shipment's items by SKU and SUM their quantities.
- DIGITAL lines are dropped. The reliable signal is `item.fulfillment.type` in {email, download, …} —
  NOT `item.type`, which reports "physicalSku" even for Discord/Xbox/Norton digital codes.
- CANCELLED: a group with `grouping.type == "canceled"` (or an item whose quantity dropped to 0). A
  brand-new fully-cancelled order is ignored at discovery; a recorded (open) order that has since been
  cancelled is emitted as `cancelled` so its rows go terminal — same rule as Costco.

`build_order_items` is the entry point; each element of `order_payloads` is one order's full ss-api
response (the object with a top-level `order` key).
"""

from models.order import OrderItem, shipment_label

RETAILER = "Best Buy"

# Order-details deep link, derivable from the order id alone (no extra field needed).
ORDER_DETAILS_URL = "https://www.bestbuy.com/profile/ss/orders/order-details/{}/view"

# item.fulfillment.type values that mean "no physical package to track" -> digital, dropped.
_DIGITAL_FULFILLMENT_TYPES = {"email", "download", "digital", "sms", "edelivery"}


class PayloadShapeError(ValueError):
    """A payload that parsed as an order but not as one this mapping understands.

    Returning [] here would be indistinguishable from "nothing new" -- the silent failure the
    dossier exists for. `payload` rides along so the caller can attach
    the document that failed to the dossier.
    """

    def __init__(self, message: str, payload=None):
        super().__init__(message)
        self.payload = payload


def _order_url(order_id: str) -> str:
    return ORDER_DETAILS_URL.format(order_id) if order_id else ""


def _num(value) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    n = _num(value)
    return int(n) if n is not None else 0


def _date(value) -> str:
    """Keep just YYYY-MM-DD from either a plain date ('2026-08-07') or an ISO datetime
    ('2026-08-10T10:44:24-05:00')."""
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return ""


def _is_gift_tender(payment: dict) -> bool:
    ptype = (payment.get("type") or "").strip().lower()
    ctype = (payment.get("creditCardType") or "").strip().lower()
    return "gift" in ptype or "gift" in ctype


def _card_last4(payments: list) -> str:
    """Last 4 of the real payment card. Skip gift cards / non-card tenders (no reusable last-4).

    A card KEYED at checkout (cardEntryType "KEYED", live on BBY03-809900000009) has no
    `creditCardNumber` field at all — its number rides `displayCreditCardNumber` (bare last-4)
    instead, so that is the fallback."""
    for payment in payments or []:
        if _is_gift_tender(payment):
            continue
        number = (payment.get("creditCardNumber") or payment.get("displayCreditCardNumber") or "").strip()
        if number:
            return number[-4:]
    return ""


def _gift_card_total(payments: list) -> float | None:
    """What the order's gift-card tenders paid, summed.

    `totalNetAmount` is the one amount field every captured tender reports as its net paid figure —
    an in-flight order's credit tender shows `amount`/`chargedAmount` 0 with `totalNetAmount` at the
    full charge, a completed one shows all three, and a refund goes negative there. (All five
    on-disk captures agree; a live gift-tender confirmation is still pending — the user holds a
    known gift-card BBY order for it.)

    A tender list WITHOUT a gift tender is a real 0.0 — every order carries its tenders, so their presence without a
    gift one IS the detection. None only when the payments list itself is missing/empty (a shape
    we've never seen; a blank never overwrites the ledger).
    """
    if not payments:
        return None
    amounts = [_num(p.get("totalNetAmount")) for p in payments if _is_gift_tender(p)]
    amounts = [a for a in amounts if a is not None]
    return round(sum(amounts), 2) if amounts else 0.0


def _format_address(address: dict | None) -> str:
    if not address:
        return ""
    name = " ".join(
        p.strip() for p in (address.get("firstName"), address.get("lastName")) if p and p.strip()
    )
    street = ", ".join(
        p.strip() for p in (address.get("addressLine1"), address.get("addressLine2")) if p and p.strip()
    )
    region = " ".join(
        p for p in ((address.get("state") or "").strip(), (address.get("postalCode") or "").strip()) if p
    )
    locality = ", ".join(p for p in ((address.get("city") or "").strip(), region) if p)
    return ", ".join(p for p in (name, street, locality) if p)


def _addresses_by_id(order: dict) -> dict:
    """Map addressId -> address, preferring the 'shipping' record when an id appears as both billing
    and shipping (Best Buy repeats one id under both types)."""
    out: dict[str, dict] = {}
    for address in (order.get("user") or {}).get("addresses") or []:
        aid = address.get("id")
        if not aid:
            continue
        if aid not in out or (address.get("type") or "").lower() == "shipping":
            out[aid] = address
    return out


def _is_digital(item: dict) -> bool:
    ftype = ((item.get("fulfillment") or {}).get("type") or "").strip().lower()
    return ftype in _DIGITAL_FULFILLMENT_TYPES


def _group_tracking(items: list[dict]) -> dict:
    """The shipment's tracking object — the first item in the group that carries a tracking number
    (items in one group share it). Returns {} when nothing has shipped yet."""
    for item in items:
        tracking = (item.get("fulfillment") or {}).get("tracking") or {}
        if (tracking.get("trackingNumber") or "").strip():
            return tracking
    return {}


def _status(group_type: str, tracking: dict) -> str:
    """Status of one shipment. 'shipped' requires a tracking number (the OrderItem invariant enforces
    this too); delivered is read from the carrier's delivered date / DELIVERED milestone."""
    if group_type == "canceled":
        return "cancelled"
    milestone = (tracking.get("milestoneCode") or "").strip().upper()
    status_desc = (tracking.get("statusDesc") or "").strip().lower()
    if tracking.get("carrierDeliveredDate") or milestone == "DELIVERED" or "delivered" in status_desc:
        return "delivered"
    if (tracking.get("trackingNumber") or "").strip():
        return "shipped"
    return "ordered"


def _delivery_date(status: str, tracking: dict, fulfillment: dict) -> str:
    if status == "delivered":
        return _date(tracking.get("carrierDeliveredDate")) or _date(fulfillment.get("maxArrivalDate"))
    if status == "shipped":
        return _date(fulfillment.get("maxArrivalDate")) or _date(fulfillment.get("minArrivalDate"))
    return ""


def build_order_items(
    order_payloads: list[dict],
    profile_label: str = "",
    known_open_ids: frozenset[str] | set[str] = frozenset(),
) -> list[OrderItem]:
    items: list[OrderItem] = []
    for payload in order_payloads or []:
        items.extend(_build_one_order(payload, profile_label, known_open_ids))
    return items


def _build_one_order(payload: dict, profile_label: str, known_open_ids) -> list[OrderItem]:
    order = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(order, dict):
        raise PayloadShapeError("ss-api payload has no `order` object -- shape changed?", payload)
    order_id = str(order.get("userOrderId") or "").strip()
    if not order_id:
        raise PayloadShapeError("ss-api order has no `userOrderId` -- shape changed?", payload)
    # An order ALWAYS lists its items and its fulfillment groups -- even a cancelled or digital-only
    # one. A missing key can only mean the payload's shape moved; an empty `fulfillmentGroups` list
    # on a just-placed order is left alone (it simply yields no rows yet).
    if not order.get("items"):
        raise PayloadShapeError(f"order {order_id}: ss-api payload lists no `items` -- shape changed?", payload)
    if not isinstance(order.get("groups"), dict) or "fulfillmentGroups" not in order["groups"]:
        raise PayloadShapeError(f"order {order_id}: ss-api payload has no `groups.fulfillmentGroups` -- shape changed?", payload)

    order_date = _date(order.get("created"))
    card_last4 = _card_last4(order.get("payments"))
    shipping_total = _num((order.get("price") or {}).get("shippingTotal"))
    # Both ORDER-LEVEL like shipping: repeated on every row, prorated at sync, netted by the COGS
    # formula (gift card subtracted, tax added). `totalSalesTax` sits beside shippingTotal in
    # `order.price` on every capture; absent (None) leaves the cell alone, 0.0 is a real zero.
    sales_tax_total = _num((order.get("price") or {}).get("totalSalesTax"))
    gift_card_total = _gift_card_total(order.get("payments"))
    addr_by_id = _addresses_by_id(order)
    items_by_id = {it.get("id"): it for it in order.get("items") or [] if it.get("id")}

    # Physical shipments = fulfillment groups that hold at least one non-digital item. Number them
    # 1..N by groupId (digital-only groups are skipped and don't consume a shipment number).
    physical_groups: list[tuple[int, str, list[dict], str]] = []
    for group in (order.get("groups") or {}).get("fulfillmentGroups") or []:
        gtype = (group.get("grouping") or {}).get("type", "").strip().lower()
        members = [items_by_id.get(m.get("itemKey")) for m in group.get("members") or []]
        members = [m for m in members if m]
        physical = [m for m in members if not _is_digital(m)]
        if physical:
            # The raw groupId string rides along as the row's Package ID: assigned at order time,
            # a per-order ordinal ("1", "2", ...), so it is unique only WITHIN the order — which is
            # all ledger_sync's (Order ID, Package ID) index needs.
            physical_groups.append((_int(group.get("groupId")), gtype, physical,
                                    str(group.get("groupId") or "").strip()))
    physical_groups.sort(key=lambda t: t[0])

    rows: list[OrderItem] = []
    for index, (_group_id, gtype, physical_items, package_id) in enumerate(physical_groups):
        shipment = shipment_label(index + 1)
        cancelled = gtype == "canceled"

        # One row per distinct SKU in the shipment; same SKU repeated = summed quantity.
        by_sku: dict[str, dict] = {}
        sku_order: list[str] = []
        for item in physical_items:
            sku = str(item.get("sku") or item.get("id") or "")
            group = by_sku.get(sku)
            if group is None:
                group = {"items": [], "name": (item.get("itemDesc") or "").strip(),
                         "unit_price": _num((item.get("price") or {}).get("unitCurrentPrice"))}
                by_sku[sku] = group
                sku_order.append(sku)
            group["items"].append(item)

        for sku in sku_order:
            sku_items = by_sku[sku]["items"]
            tracking = _group_tracking(sku_items)
            status = _status(gtype, tracking)
            fulfillment = sku_items[0].get("fulfillment") or {}
            quantity = sum(_int(it.get("quantity")) for it in sku_items)
            rows.append(
                OrderItem(
                    retailer=RETAILER,
                    profile_label=profile_label,
                    order_id=order_id,
                    order_date=order_date,
                    status=status,
                    order_url=_order_url(order_id),
                    tracking_number=(tracking.get("trackingNumber") or "").strip(),
                    tracking_url=(tracking.get("trackingUrl") or "").strip(),
                    delivery_date=_delivery_date(status, tracking, fulfillment),
                    delivery_address=_format_address(addr_by_id.get(fulfillment.get("addressId"))),
                    item_name=by_sku[sku]["name"],
                    # A cancelled line's quantity has dropped to 0; send None so the blank-preserving
                    # upsert keeps whatever quantity was originally recorded rather than zeroing it.
                    quantity=None if cancelled else (quantity or None),
                    cost_per_item=by_sku[sku]["unit_price"],
                    # Shipping is order-level; repeat it on EVERY shipment row (same value), matching
                    # the agent path's convention so the two writers agree on this field.
                    shipping=shipping_total,
                    gift_card=gift_card_total,
                    rewards_used=0.0,  # no rewards programme on this retailer yet: a real 0, not unknown
                    sales_tax=sales_tax_total,
                    card_last4=card_last4,
                    shipment=shipment,
                    package_id=package_id,
                )
            )

    # Brand-new fully-cancelled order -> ignore at discovery. A recorded (open) order that has since
    # been cancelled flows through so its rows flip to cancelled and go terminal.
    if rows and all(r.status == "cancelled" for r in rows) and order_id not in known_open_ids:
        return []
    return rows
