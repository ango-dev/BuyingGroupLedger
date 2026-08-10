"""Pure mapping from Costco's `getOrderDetails` GraphQL payload to ledger `OrderItem` rows.

Kept deliberately free of any network/auth dependency (no curl_cffi / jwt import) so the whole
mapping is unit-testable offline against a captured payload — the part most likely to drift when
Costco changes its schema is exactly this shaping, and it's provable without spending a live call.

Row model (matches how Amazon/Best Buy rows are keyed — Order ID + Order Date + Item Name + Shipment):

- One ledger row per (distinct SKU in the order) x (distinct package it shipped in). Costco's own
  order splits a line's quantity across several UPS packages, each with its own tracking number, and
  a buying group needs every tracking number — so each package is its own row.
- SKUs are grouped by `itemNumber`, not by the description: Costco truncates descriptions, so two
  genuinely different SKUs can share an identical (truncated) `itemDescription`. If we keyed rows on
  that shared name they'd collide on the upsert key and overwrite each other. We therefore append the
  item number to the name ("... (Item #1847785)") so each SKU's rows are unique within the order, and
  group by itemNumber so a SKU accidentally split across two order lines still numbers its packages in
  one sequence.
- Shipment labels are numbered PER PHYSICAL PACKAGE at the ORDER level: each distinct tracking number
  is a "Shipment N" (ordered by ship date, then tracking number), and every row that shipped in that
  package carries that number — so a 2-box order reads Shipment 1 / Shipment 2 even when the boxes hold
  different SKUs, and a single SKU split across two boxes gets one row per box. Not-yet-shipped lines
  share a trailing bucket. (Caveat: an order first seen while UNSHIPPED and later seen shipped-and-split
  can renumber, since the box assignment isn't known until ship time; Costco ships fast so this window
  is small, and the item-number suffix keeps same-box distinct SKUs from colliding regardless.)
- Digital / non-shippable lines are dropped (gift cards, e-delivery software, memberships, and fee
  lines) — they're never resold and carry no carrier tracking.

`build_order_items` is the entry point. `known_open_ids` are order numbers already recorded and still
open in the sheet; a brand-new fully-cancelled order (not in that set) is ignored at discovery, while
a recorded order that has since been cancelled is emitted as `cancelled` so its rows go terminal.
"""

from models.order import OrderItem

RETAILER = "Costco"


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
    """Costco timestamps are ISO like '2026-07-21T21:44:11.127'; keep just the YYYY-MM-DD date."""
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return ""


def _is_digital(line_item: dict) -> bool:
    """A line with no physical shipment to track: e-delivery software, memberships, and fee lines."""
    if line_item.get("isFeeItem"):
        return True
    if (line_item.get("carrierItemCategory") or "").strip().lower() == "digital":
        return True
    # "EDG" = Electronic Delivery / "Delivery via Email".
    if (line_item.get("orderedShipMethod") or "").strip().upper() == "EDG":
        return True
    return False


def _shipment_status(package: dict) -> str:
    """Status of one physical package. 'shipped' requires a tracking number (a shipped-but-untracked
    package reads as 'ordered', matching the ledger-wide invariant that shipped implies a number)."""
    if package.get("deliveredDate"):
        return "delivered"
    if (package.get("trackingNumber") or "").strip():
        return "shipped"
    return "ordered"


def _package_delivery_date(package: dict) -> str:
    if package.get("deliveredDate"):
        return _date(package.get("deliveredDate"))
    if (package.get("trackingNumber") or "").strip():
        return _date(package.get("estimatedArrivalDate"))
    return ""


def _card_last4(payments: list) -> str:
    """Last 4 of the real payment card — skip coupons and wallet/shop-card lines, which either have
    no card number or a coupon code in that field."""
    for payment in payments or []:
        ptype = (payment.get("paymentType") or "").strip().lower()
        number = (payment.get("cardNumber") or "").strip()
        if number and "coupon" not in ptype and "wallet" not in ptype and "shop card" not in ptype:
            return number[-4:]
    return ""


def _format_address(shipto: dict) -> str:
    name = " ".join(
        p.strip() for p in (shipto.get("firstName"), shipto.get("lastName")) if p and p.strip()
    )
    street = ", ".join(
        p.strip() for p in (shipto.get("line1"), shipto.get("line2"), shipto.get("line3")) if p and p.strip()
    )
    city = (shipto.get("city") or "").strip()
    region = " ".join(
        p for p in ((shipto.get("state") or "").strip(), (shipto.get("postalCode") or "").strip()) if p
    )
    locality = ", ".join(p for p in (city, region) if p)
    return ", ".join(p for p in (name, street, locality) if p)


def _distribute(total: int, buckets: int) -> list[int]:
    """Split `total` units across `buckets` package rows so the per-row quantities still sum to the
    line total (keeps the Total Cost column summing to the order total) when we can't tell exactly how
    many units rode in each package. Even split, remainder to the earliest rows."""
    if buckets <= 0:
        return []
    base, remainder = divmod(total, buckets)
    return [base + (1 if i < remainder else 0) for i in range(buckets)]


def _order_cancelled(line_item: dict) -> bool:
    status = line_item.get("itemStatus") or {}
    return bool(status.get("cancelled"))


def build_order_items(
    order_details: list[dict],
    profile_label: str = "",
    known_open_ids: frozenset[str] | set[str] = frozenset(),
) -> list[OrderItem]:
    items: list[OrderItem] = []
    for detail in order_details or []:
        items.extend(_build_one_order(detail, profile_label, known_open_ids))
    return items


def _build_one_order(detail: dict, profile_label: str, known_open_ids) -> list[OrderItem]:
    order_id = str(detail.get("orderNumber") or "").strip()
    if not order_id:
        return []
    order_date = _date(detail.get("orderPlacedDate"))
    card_last4 = _card_last4(detail.get("orderPayment"))

    # Group physical lines by SKU (itemNumber), preserving first-seen order for stable numbering.
    groups: dict[str, dict] = {}
    order_keys: list[str] = []
    for shipto in detail.get("shipToAddress") or []:
        address = _format_address(shipto)
        for line_item in shipto.get("orderLineItems") or []:
            if _is_digital(line_item):
                continue
            item_number = str(line_item.get("itemNumber") or "").strip()
            description = (line_item.get("itemDescription") or "").strip()
            key = item_number or description
            group = groups.get(key)
            if group is None:
                group = {
                    "item_number": item_number,
                    "description": description,
                    "unit_price": _num(line_item.get("price")),
                    "shipping": 0.0,
                    "quantity": 0,
                    "packages": [],
                    "package_keys": set(),
                    "address": address,
                    "cancelled": False,
                }
                groups[key] = group
                order_keys.append(key)
            group["quantity"] += _int(line_item.get("quantity"))
            group["shipping"] += _num(line_item.get("shippingChargeAmount")) or 0.0
            if _order_cancelled(line_item):
                group["cancelled"] = True
            for package in line_item.get("shipment") or []:
                tracking = (package.get("trackingNumber") or "").strip()
                pkg_key = tracking or (package.get("packageNumber") or "")
                if pkg_key and pkg_key in group["package_keys"]:
                    continue
                if pkg_key:
                    group["package_keys"].add(pkg_key)
                group["packages"].append(
                    {
                        "tracking_number": tracking,
                        "tracking_url": (package.get("trackingSiteUrl") or "").strip(),
                        "status": _shipment_status(package),
                        "delivery_date": _package_delivery_date(package),
                        "shipped_date": package.get("shippedDate") or "",
                        "address": address,
                    }
                )

    # Number shipments at the ORDER level, one number per distinct physical package (tracking
    # number), so items boxed together share a number and items in different boxes get different
    # numbers (a 2-box order reads Shipment 1 / Shipment 2, not Shipment 1 twice). Ordered by ship
    # date then tracking number so a package keeps its number across runs.
    packages_by_tracking: dict[str, dict] = {}
    for key in order_keys:
        for pkg in groups[key]["packages"]:
            trk = pkg["tracking_number"]
            if trk and trk not in packages_by_tracking:
                packages_by_tracking[trk] = pkg
    ordered = sorted(packages_by_tracking, key=lambda t: (packages_by_tracking[t]["shipped_date"] or "", t))
    shipment_number = {trk: i + 1 for i, trk in enumerate(ordered)}
    # Not-yet-shipped lines share a trailing bucket (its own number after the shipped packages; just
    # "Shipment 1" when nothing in the order has shipped).
    unshipped_shipment = len(ordered) + 1

    rows = [
        row
        for key in order_keys
        for row in _rows_for_group(
            groups[key], order_id, order_date, card_last4, profile_label,
            shipment_number, unshipped_shipment,
        )
    ]

    # Brand-new fully-cancelled order → ignore at discovery. A recorded (open) order that has since
    # been cancelled still flows through so its rows flip to cancelled and go terminal.
    if rows and all(r.status == "cancelled" for r in rows) and order_id not in known_open_ids:
        return []
    return rows


def _rows_for_group(
    group, order_id, order_date, card_last4, profile_label, shipment_number, unshipped_shipment,
) -> list[OrderItem]:
    name = group["description"]
    if group["item_number"]:
        name = f"{name} (Item #{group['item_number']})".strip()
    unit_price = group["unit_price"]
    shipping = round(group["shipping"], 2) if group["shipping"] else None
    # Only packages with a real tracking number count as shipped; anything else is "not shipped yet".
    packages = [p for p in group["packages"] if p["tracking_number"]]

    if not packages:
        status = "cancelled" if group["cancelled"] else "ordered"
        return [
            OrderItem(
                retailer=RETAILER,
                profile_label=profile_label,
                order_id=order_id,
                order_date=order_date,
                status=status,
                delivery_address=group["address"],
                item_name=name,
                quantity=group["quantity"] or None,
                cost_per_item=unit_price,
                shipping=shipping,
                card_last4=card_last4,
                shipment=f"Shipment {unshipped_shipment}",
            )
        ]

    packages.sort(key=lambda p: shipment_number[p["tracking_number"]])
    quantities = _distribute(group["quantity"], len(packages))
    rows = []
    for i, package in enumerate(packages):
        rows.append(
            OrderItem(
                retailer=RETAILER,
                profile_label=profile_label,
                order_id=order_id,
                order_date=order_date,
                status=package["status"],
                tracking_number=package["tracking_number"],
                tracking_url=package["tracking_url"],
                delivery_date=package["delivery_date"],
                delivery_address=package["address"],
                item_name=name,
                quantity=quantities[i] or None,
                cost_per_item=unit_price,
                # Line shipping belongs to the order once, not once per split package.
                shipping=shipping if i == 0 else None,
                card_last4=card_last4,
                shipment=f"Shipment {shipment_number[package['tracking_number']]}",
            )
        )
    return rows
