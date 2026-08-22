"""Offline tests for the pure Amazon Business HTML parsing in scrapers/amazon_business_mapping.py.

Amazon Business has no order JSON endpoint, so the deterministic path parses server-rendered HTML. These
tests use SYNTHETIC HTML mirroring the real business structure captured live (scripts/amazon_capture.py
--out .amazon_business_capture, 2026-08-11); no real order HTML (PII) is committed. The business
order-details `#orderDetails` data-component tree is identical to consumer Amazon; the one shape
difference is DISCOVERY — business order cards are `a-box`/`a-box-group` with an "Order placed <date>"
row, NOT consumer's `.order-card` — so the order-history builder here reflects that.
"""

from scrapers.amazon_business_mapping import (
    build_order_items,
    discover_orders,
    parse_shipment_targets,
)

BASE = "https://www.amazon.com"


# --- HTML builders mirroring the real business structure ----------------------------------------
def _item(title: str, price: str, qty: int | None = None, asin: str = "B000000001") -> str:
    qty_html = f'<div class="od-item-view-qty"><span>{qty}</span></div>' if qty is not None else ""
    return (
        '<div class="a-fixed-left-grid a-spacing-base">'
        f'<a href="/dp/{asin}">img</a>'
        f'<div data-component="itemTitle">{title}</div>'
        f"{qty_html}"
        f'<div data-component="unitPrice">{price} {price}</div>'
        "</div>"
    )


def _shipment(order_id: str, index: int, status_text: str, items: list[str],
              shipment_id: str = "SHIP", track: bool = True) -> str:
    track_html = ""
    if track:
        # Business "Track package" is still a consumer /gp/your-account/ship-track link (a separate
        # /your-orders/pop "View your item" link may sit alongside it, which the ship-track selector
        # correctly ignores).
        href = (f"/gp/your-account/ship-track?itemId=abc{index}&orderId={order_id}&shipmentId={shipment_id}"
                f"&ref_=ppx_hzod_shipconns_dt_b_track_package_{index}&noPtRedirect=1")
        pop = (f'<a href="/your-orders/pop?orderId={order_id}&shipmentId={shipment_id}'
               f'&packageId=1&ref_=ppx_hzod_itemconns_dt_b_pop_{index}_0&noPopRedirect=1">View your item</a>')
        track_html = (f'<div data-component="shipmentConnections">{pop}'
                      f'<a href="{href}">Track package</a></div>')
    return (
        '<div class="shipment-block">'
        f'<div class="a-row"><div data-component="shipmentStatus">{status_text}</div></div>'
        f'<div data-component="purchasedItems">{"".join(items)}</div>'
        f"{track_html}"
        "</div>"
    )


def _details(order_id: str, order_date: str, shipments: list[str], card: str = "1234",
             shipping: str = "$0.00", address: str = "Test Buyer\n123 Main St\nSampletown, CA 90000",
             extra_chrome: str = "", gift_card: str = "", subtotal: str = "") -> str:
    # The "Gift Card Amount" line only renders when a gift card actually paid part of the order.
    gift_html = f"Gift Card Amount: -{gift_card}\n" if gift_card else ""
    # Real pages always carry a subtotal; tests that don't care omit it so the reconciliation guard
    # (which only fires when the cards are worth MORE than the order) stays out of the way.
    subtotal_line = f"Item(s) Subtotal: {subtotal}\n" if subtotal else ""
    return (
        '<html><body><div id="orderDetails">'
        f'<div data-component="orderDate">{order_date}</div>'
        f'<div data-component="orderId">Order # {order_id}</div>'
        f'<div data-component="shippingAddress">{address}</div>'
        f"{extra_chrome}"
        f'<div>Payment method Prime Business Card ending in {card} 5% back</div>'
        f'<div data-component="orderSummary">{subtotal_line}Shipping &amp; Handling: {shipping}\n'
        f"Total before tax: $10.00\nEstimated tax to be collected: $0.87\n{gift_html}Grand Total: $10.87</div>"
        f'<div data-component="shipments">{"".join(shipments)}</div>'
        "</div>"
        # A recommendations carousel OUTSIDE #orderDetails must be ignored.
        '<div id="rhf"><div data-component="itemTitle">RECOMMENDED JUNK</div>'
        '<div data-component="unitPrice">$999.00</div></div>'
        "</body></html>"
    )


def _order_card(oid: str, placed: str, total: str = "$10.87", ship_to: str = "Test Buyer") -> str:
    """A business order-history card: an a-box-group whose row reads 'Order placed <date>' and which
    links to the order-details page (NOT a consumer `.order-card`)."""
    return (
        '<div class="a-box-group a-spacing-top-base"><div class="a-box a-color-offset-background">'
        '<div class="a-box-inner">'
        f'<div class="a-row">Order placed {placed} Total {total} Ship to {ship_to}</div>'
        '<div class="a-row"><ul class="a-unordered-list a-nostyle a-vertical">'
        f'<li class="a-list-item">Order # {oid}</li>'
        f'<li class="a-list-item"><a href="/your-orders/order-details?orderID={oid}&ref=ab_ppx_yo_d">'
        "View order details</a></li></ul></div>"
        "</div></div></div>"
    )


def _history(*cards: str) -> str:
    return f"<html><body><div class='your-orders-content'>{''.join(cards)}</div></body></html>"


# --- discovery ----------------------------------------------------------------------------------
def test_discovery_reads_order_placed_dates_from_business_cards():
    html = _history(
        _order_card("112-9990001-9990001", "August 7, 2026"),
        _order_card("113-9990002-9990002", "July 26, 2026"),
    )
    assert discover_orders(html) == {
        "112-9990001-9990001": "2026-08-07",
        "113-9990002-9990002": "2026-07-26",
    }


def test_discovery_scopes_to_order_details_links_only():
    html = _history(
        _order_card("112-9990001-9990001", "August 7, 2026"),
        # A recommendation widget with a bogus order-number-looking id and NO order-details link.
        '<div class="p13n-carousel"><a href="/dp/B0XYZ?ref=000-0000000-8675309">buy again</a></div>',
    )
    assert discover_orders(html) == {"112-9990001-9990001": "2026-08-07"}


def test_discovery_fallback_to_links_when_no_order_placed_text():
    # A bare order-details link with no enclosing "Order placed" card -> id with blank date.
    html = '<a href="/your-orders/order-details?orderID=111-2223334-5556667">x</a>'
    assert discover_orders(html) == {"111-2223334-5556667": ""}


# --- single shipment / core fields --------------------------------------------------------------
def test_single_shipment_fields_and_total_cost():
    html = _details(
        "111-2223334-5556667", "July 26, 2026",
        [_shipment("111-2223334-5556667", 0, "Delivered July 28", [_item("Widget", "$17.85")])],
        card="0301", shipping="$0.00",
    )
    rows = build_order_items(html, "profile-alpha", today="2026-08-10")
    assert len(rows) == 1
    r = rows[0]
    assert (r.retailer, r.order_id, r.order_date) == ("Amazon Business", "111-2223334-5556667", "2026-07-26")
    assert r.shipment == "1"
    assert r.status == "delivered"
    assert r.item_name == "Widget"
    assert r.quantity == 1
    assert r.cost_per_item == 17.85
    assert r.total_cost == 17.85  # computed = qty * cost
    assert r.shipping == 0.0
    assert r.card_last4 == "0301"
    assert r.delivery_date == "2026-07-28"
    assert "Sampletown, CA 90000" in r.delivery_address
    assert r.order_url == f"{BASE}/gp/css/order-details?orderID=111-2223334-5556667"
    # The "Track package" ship-track link is captured, NOT the /your-orders/pop "View your item" link.
    assert "ship-track" in r.tracking_url
    assert "your-orders/pop" not in r.tracking_url


def test_quantity_badge_parsed_and_defaults_to_one():
    html = _details(
        "111-2223334-5556667", "July 26, 2026",
        [_shipment("111-2223334-5556667", 0, "Delivered July 27",
                   [_item("Watch", "$299.00", qty=3), _item("Band", "$9.99")])],
    )
    rows = build_order_items(html, today="2026-08-10")
    by_name = {r.item_name: r for r in rows}
    assert by_name["Watch"].quantity == 3
    assert by_name["Watch"].total_cost == 897.0
    assert by_name["Band"].quantity == 1


# --- multi-shipment split -----------------------------------------------------------------------
def test_multi_shipment_split_numbers_top_to_bottom():
    oid = "113-9990003-9990003"
    html = _details(
        oid, "July 22, 2026",
        [
            _shipment(oid, 0, "Delivered July 27", [_item("Watch Rose", "$299.00", qty=3)], shipment_id="AAA"),
            _shipment(oid, 1, "Delivered July 27", [_item("Watch Silver", "$299.00")], shipment_id="BBB"),
        ],
    )
    rows = build_order_items(html, today="2026-08-10")
    assert [r.shipment for r in rows] == ["1", "2"]
    targets = parse_shipment_targets(html)
    assert [t["shipmentId"] for t in targets] == ["AAA", "BBB"]
    assert all("ship-track" in t["tracking_url"] for t in targets)


# --- shipped-requires-tracking + tracking injection ---------------------------------------------
def test_arriving_without_tracking_stays_ordered():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 9, 2026",
                    [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")])])
    rows = build_order_items(html, today="2026-08-10")
    assert rows[0].status == "ordered"
    assert rows[0].tracking_number == ""


def test_tracking_by_shipment_number_promotes_to_shipped():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 9, 2026",
                    [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")], shipment_id="ZZZ")])
    rows = build_order_items(html, today="2026-08-10", tracking_by_shipment={"1": "TBA123"})
    assert rows[0].status == "shipped"
    assert rows[0].tracking_number == "TBA123"


def test_tracking_by_shipment_id_also_works():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 9, 2026",
                    [_shipment(oid, 0, "Arriving Monday", [_item("Thing", "$5.00")], shipment_id="ZZZ")])
    rows = build_order_items(html, today="2026-08-10", tracking_by_shipment={"ZZZ": "TBA999"})
    assert rows[0].status == "shipped"
    assert rows[0].tracking_number == "TBA999"


# --- digital / cancelled / returns --------------------------------------------------------------
def test_digital_shipment_is_skipped():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 1, 2026", [
        _shipment(oid, 0, "Delivered July 28", [_item("Physical", "$10.00")]),
        _shipment(oid, 1, "Digital Delivery Ready to Redeem", [_item("Gift Card", "$25.00")], track=False),
    ])
    rows = build_order_items(html, today="2026-08-10")
    assert [r.item_name for r in rows] == ["Physical"]


def test_brand_new_fully_cancelled_order_is_dropped():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 9, 2026",
                    [_shipment(oid, 0, "Cancelled", [_item("Thing", "$5.00")], track=False)])
    assert build_order_items(html, today="2026-08-10") == []


def test_cancelled_order_kept_when_already_open():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 9, 2026",
                    [_shipment(oid, 0, "Cancelled", [_item("Thing", "$5.00")], track=False)])
    rows = build_order_items(html, today="2026-08-10", known_open_ids=frozenset({oid}))
    assert len(rows) == 1
    assert rows[0].status == "cancelled"
    assert rows[0].quantity is None  # cancelled -> None so the upsert keeps the recorded quantity


def test_return_started_treated_as_delivered():
    oid = "111-2223334-5556667"
    html = _details(oid, "July 26, 2026",
                    [_shipment(oid, 0, "Return started Your refund will be processed", [_item("Thing", "$5.00")])])
    rows = build_order_items(html, today="2026-08-10")
    assert rows[0].status == "delivered"


# --- business-specific chrome -------------------------------------------------------------------
def test_business_chrome_is_ignored():
    """Extra Amazon Business chrome (PO number, cost center, requisitioner) must not corrupt parsing:
    the item/qty/cost/card come through unchanged."""
    oid = "114-9990004-9990004"
    chrome = ('<div data-component="poNumber">PO number: PO-2026-4471</div>'
              '<div>Cost center: Engineering</div><div>Placed by Test Buyer</div>'
              '<div>Tax exemption applied</div>')
    html = _details(
        oid, "August 11, 2026",
        [_shipment(oid, 0, "Arriving Friday", [_item("Nintendo Switch 2 System", "$449.00", qty=3)])],
        card="0315", extra_chrome=chrome,
    )
    rows = build_order_items(html, today="2026-08-11", tracking_by_shipment={"1": "TBA55"})
    assert len(rows) == 1
    r = rows[0]
    assert r.retailer == "Amazon Business"
    assert r.item_name == "Nintendo Switch 2 System"
    assert r.quantity == 3
    assert r.cost_per_item == 449.0
    assert r.total_cost == 1347.0
    assert r.card_last4 == "0315"
    assert r.status == "shipped"  # tracking number present
    assert "PO-2026-4471" not in (r.item_name or "")


# --- delivery-date parsing ----------------------------------------------------------------------
def test_delivered_today_uses_run_date():
    oid = "112-9990001-9990001"
    html = _details(oid, "August 7, 2026",
                    [_shipment(oid, 0, "Delivered today", [_item("AirPods", "$189.99")])])
    rows = build_order_items(html, today="2026-08-10")
    assert rows[0].delivery_date == "2026-08-10"


def test_bare_weekday_arrival_resolves_to_next_occurrence():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Arriving Friday", [_item("Switch 2", "$449.00", qty=4)])])
    rows = build_order_items(html, today="2026-08-11")  # 2026-08-11 is a Tuesday
    assert rows[0].delivery_date == "2026-08-14"


def test_abbreviated_month_estimate_parsed():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Arriving Aug 14", [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-08-11")[0].delivery_date == "2026-08-14"


def test_no_date_in_status_leaves_delivery_date_blank():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Preparing for shipment", [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-08-11")[0].delivery_date == ""


# --- gift-card netting ---------------------------------------------------------------------------
# Business shares the consumer rule: a gift card earns 0% cashback, so cost is scaled down to what the
# CARD actually paid. (The consumer path ALSO reads a promo cashback rate; Business deliberately does
# not — test_business_never_reads_a_promo_rate pins that.)
BIZ_OID = "111-2223334-5556667"


def _one_item_order(**kwargs) -> str:
    return _details(BIZ_OID, "August 12, 2026",
                    [_shipment(BIZ_OID, 0, "Delivered August 13",
                               [_item("Thing", "$100.00", qty=1)])],
                    **kwargs)


def test_gift_card_reduces_cost_to_what_the_card_paid():
    rows = build_order_items(_one_item_order(gift_card="$40.00"))
    assert rows[0].cost_per_item == 60.00
    assert rows[0].total_cost == 60.00


def test_gift_card_larger_than_the_basis_floors_cost_at_zero():
    html = _details(BIZ_OID, "August 11, 2026",
                    [_shipment(BIZ_OID, 0, "Delivered August 12", [_item("Gummies", "$12.85", qty=1)])],
                    gift_card="$14.04")
    rows = build_order_items(html)
    assert rows[0].cost_per_item == 0.00
    assert rows[0].total_cost == 0.00


def test_gift_card_prorates_across_shipments_by_cost():
    html = _details(BIZ_OID, "August 12, 2026", [
        _shipment(BIZ_OID, 0, "Delivered August 13", [_item("Big", "$60.00", qty=1)], shipment_id="S1"),
        _shipment(BIZ_OID, 1, "Delivered August 14", [_item("Small", "$40.00", qty=1)], shipment_id="S2"),
    ], gift_card="$50.00")
    rows = build_order_items(html)
    assert [r.total_cost for r in rows] == [30.00, 20.00]


def test_no_gift_card_line_leaves_cost_untouched():
    rows = build_order_items(_one_item_order())
    assert rows[0].cost_per_item == 100.00


def test_netting_can_be_switched_off():
    rows = build_order_items(_one_item_order(gift_card="$40.00"), net_gift_cards=False)
    assert rows[0].cost_per_item == 100.00


def test_business_never_reads_a_promo_rate():
    """Promo cashback is Amazon-consumer only by decision; Business must leave it unset even when an
    earn line with an 'extra N%' is on the page, so tag_cards has nothing to add."""
    html = _one_item_order(
        extra_chrome='<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
                     "<span>Earn 5% back plus an extra 1% back on select items</span></li>",
    )
    assert build_order_items(html)[0]._promo_cashback_rate is None


# --- re-tracked shipment / subtotal reconciliation (twin of the consumer guard) --------------------
RID = "111-9990021-9990021"


def _biz_two_cards(subtotal: str, price: str = "$949.00", qty: int = 3, cards: int = 2) -> str:
    ships = [
        _shipment(RID, i, "Arriving Monday", [_item("iPad Pro", price, qty=qty)], shipment_id="S%d" % i)
        for i in range(cards)
    ]
    return _details(RID, "August 12, 2026", ships, subtotal=subtotal)


def test_a_repeated_shipment_card_is_collapsed_to_one_row():
    """The real case this came from: order 111-9990021-9990021 booked $5,694 for a $2,847 order."""
    rows = build_order_items(_biz_two_cards("$2,847.00"),
                             tracking_by_shipment={"1": "TBA-DEAD", "2": "TBA-LIVE"})

    assert len(rows) == 1
    assert rows[0].total_cost == 2847.0
    assert rows[0].shipment == "1"
    assert rows[0].tracking_number == "TBA-LIVE"


def test_a_genuine_split_is_left_alone():
    rows = build_order_items(_biz_two_cards("$200.00", price="$100.00", qty=1),
                             tracking_by_shipment={"1": "T1", "2": "T2"})

    assert [r.shipment for r in rows] == ["1", "2"]
    assert sum(r.total_cost for r in rows) == 200.0
