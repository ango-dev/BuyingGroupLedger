"""Offline tests for the pure Amazon Business HTML parsing in scrapers/amazon_business_mapping.py.

Amazon Business has no order JSON endpoint, so the deterministic path parses server-rendered HTML. These
tests use SYNTHETIC HTML mirroring the real business structure captured live (scripts/amazon_capture.py
--out .amazon_business_capture, 2026-08-11); no real order HTML (PII) is committed. The business
order-details `#orderDetails` data-component tree is identical to consumer Amazon; the one shape
difference is DISCOVERY — business order cards are `a-box`/`a-box-group` with an "Order placed <date>"
row, NOT consumer's `.order-card` — so the order-history builder here reflects that.
"""

import pytest
from scrapers.amazon_business_mapping import (
    build_order_items,
    discover_orders,
    parse_shipment_targets,
)

BASE = "https://www.amazon.com"


# --- HTML builders mirroring the real business structure ----------------------------------------
def _item(title: str, price: str, qty: int | None = None, asin: str = "B000000001",
          seller: str = "") -> str:
    qty_html = f'<div class="od-item-view-qty"><span>{qty}</span></div>' if qty is not None else ""
    seller_html = f'<div data-component="orderedMerchant">Sold by: {seller}</div>' if seller else ""
    return (
        '<div class="a-fixed-left-grid a-spacing-base">'
        f'<a href="/dp/{asin}">img</a>'
        f'<div data-component="itemTitle">{title}</div>'
        f"{seller_html}"
        f"{qty_html}"
        f'<div data-component="unitPrice">{price} {price}</div>'
        "</div>"
    )


def _shipment(order_id: str, index: int, status_text: str, items: list[str],
              shipment_id: str | None = None, track: bool = True, pop_only: bool = False) -> str:
    # One shipmentId PER CARD by default, as on a real page (76 captured tokens, one per package):
    # two cards sharing an id is the double-render shape _collapse_same_package_cards exists for,
    # so a test wanting that passes the same shipment_id explicitly.
    shipment_id = shipment_id or f"SHIP{index}"
    track_html = ""
    if pop_only:
        # An order whose "Track package" link has EXPIRED (Amazon removes it once an order is old
        # enough, user 2026-08-23): only the "View your item" pop link is left, so there is no
        # tracking page to hop to. Recent orders always still have the ship-track link.
        track_html = (
            '<div data-component="shipmentConnections">'
            f'<a href="/your-orders/pop?orderId={order_id}&shipmentId={shipment_id}'
            f'&packageId=1&ref_=ppx_hzod_itemconns_dt_b_pop_{index}_0&noPopRedirect=1">View your item</a>'
            "</div>"
        )
    elif track:
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


# --- self-receipt ("Mark as received") --------------------------------------------------------------
# Amazon Business lets the buyer confirm receipt, and the card then reads "All items received <date>"
# + "N/M items marked as received. Updated by: <name>" instead of "Delivered <date>". Modelled on the
# real captured order 111-9990019 (Apple Watch x5 to a BFMR warehouse, a pallet). Before this was
# handled the card fell through to `ordered`, leaving a long-since-received order permanently open and
# — while open with no tracking number — re-read by the PAID agent on every scheduled run.
# The fixtures use pop_only because that order was old enough that its track link had been removed.
def test_fully_received_order_is_delivered():
    oid = "111-9990019-9990019"
    html = _details(
        oid, "April 25, 2026",
        [_shipment(oid, 0,
                   "All items received April 28 5/5 items marked as received. Updated by: Test Buyer",
                   [_item("Apple Watch Series 11", "$329.99", qty=5)],
                   shipment_id="Pk91KJl0p", pop_only=True)],
        card="4345",
    )
    rows = build_order_items(html, "profile-alpha", today="2026-08-23")
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "delivered", "a fully-received order is terminal, not still 'ordered'"
    assert r.delivery_date == "2026-04-28"
    assert r.quantity == 5
    assert r.total_cost == 1649.95
    assert r.card_last4 == "4345"
    # The track link has expired off this old order, so nothing can drive the pt tracking-number hop.
    assert r.tracking_url == ""
    assert r.tracking_number == ""


def test_partially_received_order_stays_open():
    """3 of 5 received means the rest of the order is still outstanding — keep tracking it."""
    oid = "111-9990019-9990019"
    html = _details(
        oid, "April 25, 2026",
        [_shipment(oid, 0, "3/5 items marked as received. Updated by: Test Buyer",
                   [_item("Apple Watch Series 11", "$329.99", qty=5)], pop_only=True)],
    )
    rows = build_order_items(html, "profile-alpha", today="2026-08-23")
    assert rows[0].status == "ordered", "a partial receipt must not close the order"


def test_received_order_with_expired_link_is_not_a_hop_candidate():
    """parse_shipment_targets drives the pt hop; a delivered order whose track link has expired
    offers it nothing, so the client skips it instead of opening a browser for a page that is gone."""
    oid = "111-9990019-9990019"
    html = _details(
        oid, "April 25, 2026",
        [_shipment(oid, 0, "All items received April 28 5/5 items marked as received",
                   [_item("Apple Watch Series 11", "$329.99", qty=5)], pop_only=True)],
    )
    targets = parse_shipment_targets(html)
    assert len(targets) == 1
    assert targets[0]["status"] == "delivered"
    assert targets[0]["tracking_url"] == ""


def test_received_bare_weekday_resolves_backwards_not_forwards():
    """A receipt is a PAST event, so a bare weekday must resolve to the most recent occurrence."""
    oid = "111-9990019-9990019"
    html = _details(
        oid, "August 17, 2026",
        [_shipment(oid, 0, "All items received Monday 2/2 items marked as received",
                   [_item("Thing", "$5.00", qty=2)], pop_only=True)],
    )
    # From Sunday 2026-08-23 the most recent Monday is 2026-08-17.
    assert build_order_items(html, today="2026-08-23")[0].delivery_date == "2026-08-17"


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


def test_moved_up_delivery_ignores_the_previously_expected_date():
    """A moved-up delivery renders the STALE estimate under the live one -- "Now arriving Monday" in
    the h4, "Previously expected October 19" in a second row. The explicit-date scan used to prefer October 19 over the bare weekday,
    so the ledger kept October while Amazon said next week. 2026-09-08 is a Tuesday."""
    oid = "111-9990012-9990012"
    card = ('<h4 class="a-color-base od-status-message"><span>Now arriving Monday</span></h4>'
            '<div class="a-row od-status-message"><span>Previously expected October 19</span></div>')
    html = _details(oid, "September 4, 2026",
                    [_shipment(oid, 0, card, [_item("Hot Wheels Monster Trucks", "$7.74", qty=2)])])
    rows = build_order_items(html, today="2026-09-08")
    assert rows[0].delivery_date == "2026-09-14"   # next Monday, not the stale October 19


def test_moved_up_delivery_with_an_explicit_new_date_takes_the_new_one():
    oid = "111-9990012-9990012"
    card = ("<h4><span>Now arriving October 3</span></h4>"
            "<div><span>Previously expected October 19</span></div>")
    html = _details(oid, "September 4, 2026",
                    [_shipment(oid, 0, card, [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-09-08")[0].delivery_date == "2026-10-03"


def test_no_date_in_status_leaves_delivery_date_blank():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Preparing for shipment", [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-08-11")[0].delivery_date == ""


# --- gift card + sales tax ----------------------------------------------------------------------
# Business shares the consumer rule: the mapping emits the GROSS cost plus the order-level Gift
# Card / Sales Tax amounts on every row; the COGS formula nets them. The
# builder's summary always carries "Estimated tax to be collected: $0.87", the real Business shape.
BIZ_OID = "111-2223334-5556667"


def _one_item_order(**kwargs) -> str:
    return _details(BIZ_OID, "August 12, 2026",
                    [_shipment(BIZ_OID, 0, "Delivered August 13",
                               [_item("Thing", "$100.00", qty=1)])],
                    **kwargs)


def test_gift_card_is_emitted_and_cost_stays_gross():
    rows = build_order_items(_one_item_order(gift_card="$40.00"))
    assert rows[0].cost_per_item == 100.00
    assert rows[0].total_cost == 100.00
    assert rows[0].gift_card == 40.00


def test_the_business_tax_line_is_read():
    assert build_order_items(_one_item_order())[0].sales_tax == 0.87


def test_gift_card_and_tax_are_order_level_on_every_row():
    html = _details(BIZ_OID, "August 12, 2026", [
        _shipment(BIZ_OID, 0, "Delivered August 13", [_item("Big", "$60.00", qty=1)], shipment_id="S1"),
        _shipment(BIZ_OID, 1, "Delivered August 14", [_item("Small", "$40.00", qty=1)], shipment_id="S2"),
    ], gift_card="$50.00")
    rows = build_order_items(html)
    assert [r.total_cost for r in rows] == [60.00, 40.00]
    assert [r.gift_card for r in rows] == [50.00, 50.00]
    assert [r.sales_tax for r in rows] == [0.87, 0.87]


def test_no_gift_card_line_is_a_detected_zero():
    # A parsed summary without the line is "checked, none" -- a real 0.0.
    rows = build_order_items(_one_item_order())
    assert rows[0].cost_per_item == 100.00
    assert rows[0].gift_card == 0.0


def test_netting_can_be_switched_off():
    rows = build_order_items(_one_item_order(gift_card="$40.00"), net_gift_cards=False)
    assert rows[0].cost_per_item == 100.00
    assert rows[0].gift_card is None


def test_business_reads_the_promo_rate_off_the_earn_line():
    """Business DOES carry the promo. This test used to assert the opposite -- promo cashback was left
    out of this module on the assumption that the offer was consumer-only. Verified live on
    order 111-9990009-9990009 (card 0315): the Business order-details page renders the identical
    element with identical wording, so Business orders had been silently losing the bonus percent."""
    html = _one_item_order(
        extra_chrome='<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
                     "<span>Earn 5% back (cap applies) plus an extra 1% back on select items</span></li>",
    )
    assert build_order_items(html)[0]._promo_cashback_rate == 0.01


def test_business_earn_line_without_an_extra_is_not_a_promo():
    """A plain earn line is the card's BASE rate, which is cards.json's job -- reading it here would
    double-count the base rate on top of itself."""
    html = _one_item_order(
        extra_chrome='<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
                     "<span>Earn 5% back (cap applies)</span></li>",
    )
    assert build_order_items(html)[0]._promo_cashback_rate is None


def test_business_no_earn_line_means_no_promo():
    assert build_order_items(_one_item_order())[0]._promo_cashback_rate is None


def test_business_promo_outside_order_details_is_ignored():
    """Scoped to the payment element inside the order region, so marketing copy in a recommendations
    rail ("extra 5% off!") can never be mistaken for this order's promo."""
    html = _one_item_order().replace(
        "</body>",
        '<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
        "<span>Earn 2% back plus an extra 9% back</span></li></body>",
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


# --- digital lines must never reach the ledger ----------------------------------------------------
# An Amazon Gift Card Balance Reload reached the ledger on 2026-08-24 and booked $40.35 of cost against
# an order that can never ship. Its status card reads "Applied Gift Card balance is added to your
# account." — captured live — which none of the original markers matched.
DIG = "114-9990031-9990031"


def test_a_gift_card_reload_is_skipped_on_its_status_text():
    html = _details(DIG, "August 23, 2026", [
        _shipment(DIG, 0, "Applied Gift Card balance is added to your account.",
                  [_item("Amazon Gift Card Balance Reload", "$40.35", qty=1)], track=False),
    ])
    assert build_order_items(html) == []


def test_a_digital_item_is_skipped_even_when_the_status_is_unremarkable():
    """Second net: the item name alone is enough when the status card is worded some new way."""
    html = _details(DIG, "August 23, 2026", [
        _shipment(DIG, 0, "Delivered August 23",
                  [_item("Amazon Gift Card Balance Reload", "$40.35", qty=1)]),
    ])
    assert build_order_items(html) == []


def test_a_PHYSICAL_gift_card_is_still_recorded():
    """The false positive that would cost real money: a gift card in a greeting card SHIPS, has real
    tracking, and is a reimbursable line. A bare "gift card" marker would silently drop it."""
    html = _details(DIG, "August 23, 2026", [
        _shipment(DIG, 0, "Delivered August 25",
                  [_item("Amazon.com Gift Card in a Greeting Card", "$50.00", qty=1)]),
    ])
    rows = build_order_items(html, tracking_by_shipment={"1": "TBA1"})

    assert len(rows) == 1
    assert rows[0].item_name == "Amazon.com Gift Card in a Greeting Card"


def test_a_digital_line_does_not_consume_a_shipment_number_from_a_physical_one():
    """A mixed order still numbers the physical shipment 1 — the digital card is dropped whole."""
    html = _details(DIG, "August 23, 2026", [
        _shipment(DIG, 0, "Applied Gift Card balance is added to your account.",
                  [_item("Amazon Gift Card Balance Reload", "$40.35", qty=1)], track=False),
        _shipment(DIG, 1, "Delivered August 25", [_item("Widget", "$20.00", qty=1)], shipment_id="S2"),
    ])
    rows = build_order_items(html, tracking_by_shipment={"2": "TBA2"})

    assert [r.item_name for r in rows] == ["Widget"]


# --- a gift card bought on a RESELLING card is kept for bookkeeping -------------------------------
# User rule 2026-08-24: a card with an explicit Amazon rate in cards.json is a reselling card, so a
# gift card bought on it is funding inventory and its cost must land on the ledger (the balance later
# pays for an order whose cost the scraper nets down). On any other card it is personal spending.
BOOSTED = frozenset({"0315"})


def _gift_card_order(status_text, item, card="0315"):
    return _details(DIG, "August 23, 2026",
                    [_shipment(DIG, 0, status_text, [_item(item, "$40.35", qty=1)], track=False)],
                    card=card)


def test_a_reload_on_a_reselling_card_is_kept_and_marked_paid():
    html = _gift_card_order("Applied Gift Card balance is added to your account.",
                            "Amazon Gift Card Balance Reload")
    rows = build_order_items(html, keep_digital_last4s=BOOSTED)

    assert len(rows) == 1
    assert rows[0].total_cost == 40.35
    assert rows[0].status == "paid", "paid with a $0 payout: terminal, and never awaiting a buying group"


def test_the_same_reload_on_any_other_card_is_still_skipped():
    html = _gift_card_order("Applied Gift Card balance is added to your account.",
                            "Amazon Gift Card Balance Reload", card="9585")
    assert build_order_items(html, keep_digital_last4s=BOOSTED) == []


def test_an_ordinary_gift_card_purchase_is_kept_on_a_reselling_card():
    """Not just reloads — a normal digital gift card counts too."""
    html = _gift_card_order("Ready to Redeem", "Amazon.com Gift Card - Email Delivery")
    rows = build_order_items(html, keep_digital_last4s=BOOSTED)

    assert len(rows) == 1
    assert rows[0].status == "paid"


def test_an_ordinary_gift_card_purchase_on_a_personal_card_is_skipped():
    html = _gift_card_order("Ready to Redeem", "Amazon.com Gift Card - Email Delivery", card="4335")
    assert build_order_items(html, keep_digital_last4s=BOOSTED) == []


def test_a_non_gift_card_digital_line_is_dropped_even_on_a_reselling_card():
    """No card makes an eBook reimbursable — the exception is gift cards only."""
    html = _gift_card_order("Digital delivery", "Some Kindle Book")
    assert build_order_items(html, keep_digital_last4s=BOOSTED) == []


def test_without_a_boosted_set_every_gift_card_is_skipped():
    """The default is the safe one: no cards.json entry -> nothing is kept."""
    html = _gift_card_order("Applied Gift Card balance is added to your account.",
                            "Amazon Gift Card Balance Reload")
    assert build_order_items(html) == []


# --- the "Track package" link has TWO shapes -------------------------------------------------------
# Amazon moved from /gp/your-account/ship-track to /progress-tracker/package. Matching only the old one silently produced NO tracking url -> no pt-page
# visit -> no tracking number -> _shipped_requires_tracking downgraded a shipped package to 'ordered'.
TRK = "112-9990026-9990026"


def _shipment_with_links(status_text, item_html, links: str) -> str:
    return (
        '<div class="shipment-block">'
        f'<div class="a-row"><div data-component="shipmentStatus">{status_text}</div></div>'
        f'<div data-component="purchasedItems">{item_html}</div>'
        f'<div data-component="shipmentConnections">{links}</div>'
        "</div>"
    )


def _details_with(links: str) -> str:
    return _details(TRK, "August 18, 2026",
                    [_shipment_with_links("Arriving today", _item("iPad Air", "$559.00", qty=1), links)])


def test_a_progress_tracker_link_is_captured():
    html = _details_with(
        f'<a href="/progress-tracker/package?orderId={TRK}&_encoding=UTF8&shipmentId=NWf4QBM12'
        '&packageIndex=0">Track package</a>'
    )
    targets = parse_shipment_targets(html)

    assert "progress-tracker/package" in targets[0]["tracking_url"]
    assert targets[0]["shipmentId"] == "NWf4QBM12"


def test_the_old_ship_track_link_still_works():
    html = _details_with(
        f'<a href="/gp/your-account/ship-track?orderId={TRK}&shipmentId=OLD123">Track package</a>'
    )
    assert "ship-track" in parse_shipment_targets(html)[0]["tracking_url"]


def test_the_cancel_items_sibling_is_not_mistaken_for_a_tracking_link():
    """"Cancel items" lives in the SAME shipmentConnections block and is also under
    /progress-tracker/package/ — matching it would send the tracking read to a cancellation page."""
    html = _details_with(
        f'<a href="/progress-tracker/package/preship/cancel-items?orderID={TRK}">Cancel items</a>'
    )
    assert parse_shipment_targets(html)[0]["tracking_url"] == ""


def test_the_real_block_picks_the_track_link_over_its_siblings():
    """The live block carries Track package, Cancel items and a product-review link together."""
    html = _details_with(
        f'<a href="/progress-tracker/package?itemId=abc&orderId={TRK}">Track package</a>'
        f'<a href="/progress-tracker/package/preship/cancel-items?orderID={TRK}">Cancel items</a>'
        '<a href="/review/review-your-purchases?asins=B0GQVPX1PJ">Write a product review</a>'
    )
    url = parse_shipment_targets(html)[0]["tracking_url"]

    assert "progress-tracker/package?" in url and "cancel" not in url


def test_a_page_without_item_titles_raises_on_business_too():
    from scrapers.amazon_mapping import OrderPageShapeError
    from scrapers.amazon_business_mapping import build_order_items as build_ab
    html = ('<html><body><div id="orderDetails"><div data-component="orderDate">August 1, 2026</div>'
            '<div data-component="orderId">Order # 111-9234567-1234567</div>'
            '<div data-component="shipments"><div data-component="shipmentStatus">Delivered</div></div>'
            '</div></body></html>')
    with pytest.raises(OrderPageShapeError, match="no item titles"):
        build_ab(html)


def test_a_kept_gift_card_is_paid_with_a_zero_payout_dated_on_the_order():
    """User rule 2026-08-30: no buying group is ever involved, so the payout side is a real $0."""
    html = _details("111-1234567-1234567", "August 1, 2026",
                    [_shipment("111-1234567-1234567", 1, "Applied Gift Card balance is added to your account",
                               [_item("Amazon.com eGift Card", "$500.00")], track=False)], card="0315")
    rows = build_order_items(html, keep_digital_last4s=frozenset({"0315"}))
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "paid" and r.payout_amount == 0.0 and r.insurance == 0.0 and r.payout_date == "2026-08-01"
    assert r.delivery_date == "2026-08-01", "a gift card is delivered the day it is bought"


# --- split-quantity lines (twin of test_amazon_mapping's; this retailer is where it hit live) ---
def test_a_quantity_split_across_blocks_in_one_shipment_sums_into_one_row():
    """113-9990028-9990028 live: 4 Apple Watches rendered in one shipment card as a
    qty-3 block plus a badgeless (qty-1) block; the ledger recorded 3 and dropped the +1."""
    html = _details(
        "113-9990028-9990028", "July 22, 2026",
        [_shipment("113-9990028-9990028", 0, "Delivered July 27",
                   [_item("Apple Watch S11", "$299.00", qty=3),
                    _item("Apple Watch S11", "$299.00")])],
        subtotal="$1,196.00",
    )
    rows = build_order_items(html)
    assert len(rows) == 1
    assert rows[0].quantity == 4
    assert rows[0].total_cost == 1196.0


def test_the_same_item_in_two_shipments_is_a_genuine_split_not_a_sum():
    html = _details(
        "111-2223334-5556667", "July 26, 2026",
        [_shipment("111-2223334-5556667", 0, "Delivered July 28",
                   [_item("Widget", "$10.00", qty=2)], shipment_id="A"),
         _shipment("111-2223334-5556667", 1, "Delivered July 29",
                   [_item("Widget", "$10.00", qty=1)], shipment_id="B")],
        subtotal="$30.00",
    )
    rows = build_order_items(html)
    assert [(r.shipment, r.quantity) for r in rows] == [("1", 2), ("2", 1)]


def test_two_identical_badged_blocks_in_one_shipment_also_sum():
    """Second live shape (114-9990033-9990033, 2026-09-04): 6 MacBooks as TWO qty-3 badged blocks
    in one card — and the real page's summary rendered its subtotal lazily (blank), so the
    reconcile guard must not be what this relies on."""
    html = _details(
        "114-9990033-9990033", "September 1, 2026",
        [_shipment("114-9990033-9990033", 0, "Arriving September 11",
                   [_item("MacBook Air 13", "$1,259.99", qty=3),
                    _item("MacBook Air 13", "$1,259.99", qty=3)], track=False)],
    )
    rows = build_order_items(html)
    assert len(rows) == 1
    assert rows[0].quantity == 6
    assert rows[0].total_cost == 7559.94


# --- non-card tenders: a spent cash-back balance + Amazon points (2026-09-07) ---------------------
def test_business_non_card_tenders_cash_back_and_points():
    """Twin of the consumer tests, 111-9990010-9990010 (Business) paid its whole
    $48.28 in Amazon points and the order page never said how much — only the related-transactions
    page does. Both the cash-back summary line and the points fold into the Gift Card column."""
    from tests.test_amazon_mapping import _POINTS_INSTRUMENT, _transactions
    from scrapers.amazon_business_mapping import (SELECTORS, order_uses_points,
                                                  points_used_from_transactions)

    oid = "111-9990010-9990010"
    base = _details(oid, "September 4, 2026", [_shipment(oid, 0, "Shipped", [_item("Book", "$48.28")])],
                    gift_card="$0.72", subtotal="$48.28")
    cash_back = base.replace("Gift Card Amount: -$0.72\n",
                             "Gift Card Amount: -$0.72\nPrime for Young Adults cash back: -$5.00\n")
    assert (build_order_items(cash_back)[0].gift_card, build_order_items(cash_back)[0].rewards_used) == (0.72, 5.0)

    points = base.replace("Payment method Prime Business Card ending in 1234 5% back",
                          "Payment method " + _POINTS_INSTRUMENT)
    assert order_uses_points(points) is True and order_uses_points(base) is False
    assert build_order_items(points, points_used=47.56)[0].rewards_used == 47.56
    assert build_order_items(points, points_used=47.56)[0].gift_card == 0.72
    assert build_order_items(points)[0].rewards_used is None            # amount unknown -> blank
    assert build_order_items(base, points_used=47.56)[0].rewards_used == 0.0  # no points tender -> ignored

    page = _transactions(("Amazon Points used", "-$48.28", oid),
                         ("Amazon Points used", "-$1.00", "111-0000000-0000000"))
    assert points_used_from_transactions(page, oid) == 48.28
    assert points_used_from_transactions(_transactions(), oid) is None
    assert {"payment_instrument", "transactions_line_item"} <= set(SELECTORS)


def _rewards_ledger(*entries):
    """The Business Prime Rewards history, fixture-shaped: one list entry per
    posting — kind, the order link, the items, and the signed points at the end."""
    blocks = "".join(
        '<div data-testid="points-history-list-entry" class="space-y-[10px]">'
        '<div class="flex justify-between"><div class="w-3/4"><div class="mb-[10px]"><p>2026/09/04</p></div>'
        f'<div class="space-y-0"><p class="mb-0"><span class="font-bold">{kind}</span><span> - </span>'
        f'<a href="/gp/aw/ya?oid={oid}&amp;ac=od&amp;ref_=abr_op_d_ol">{oid}</a></p>'
        '<div class="flex w-full flex-row"><p class="overflow-hidden">2x Paint by Sticker Kids</p>'
        '<p class="whitespace-nowrap"><div> and 6 more</div></p></div></div></div>'
        '<div class="w-1/4"><div class="space-y-0 text-right mb-[5px]"><button type="button" '
        'aria-label="See order detail"></button></div>'
        f'<div class="space-y-0 text-right font-bold"><p>{points}</p></div></div></div></div>'
        for kind, oid, points in entries
    )
    return f'<html><body><section id="points-history">{blocks}</section></body></html>'


def test_the_rewards_ledger_prices_every_redemption_at_100_points_per_dollar():
    """-4828 on 111-9990010-9990010 ($48.28, the whole order) and -1370 on
    111-9990012-9990012 ($13.70 of a $15.48 order — a PARTIAL redemption). Earning entries are
    other kinds and never count; two redemptions against one order sum."""
    from scrapers.amazon_business_mapping import SELECTORS, points_redeemed_by_order

    page = _rewards_ledger(("Redeeming points", "111-9990010-9990010", "-4828"),
                           ("Redeeming points", "111-9990012-9990012", "-1370"),
                           ("Earning points", "111-9990014-9990014", "+1699"),
                           ("Redeeming points", "111-9990012-9990012", "-100"),
                           ("Refunding points", "114-9990032-9990032", "+7,532"))
    assert points_redeemed_by_order(page) == {"111-9990010-9990010": 48.28,
                                              "111-9990012-9990012": 14.70}
    assert points_redeemed_by_order("<html>nothing</html>") == {}
    assert SELECTORS["rewards_history_entry"] == '[data-testid="points-history-list-entry"]'


# --- two sellers, one box (114-9990029-9990029, 2026-09-08) ---------------------------------------
def test_the_same_item_from_two_sellers_in_one_shipment_keeps_both_lines():
    """One shipment card, two blocks with the SAME title at different prices (two sellers), one
    tracking number. They are two real lines, not a split quantity, so they must not sum -- but
    under one upsert key the ledger's collapse silently dropped the $649 iPad. The cheaper line
    keeps its plain name (an existing row stays matched); the other is suffixed seller + price."""
    html = _details(
        "114-9990029-9990029", "September 7, 2026",
        [_shipment("114-9990029-9990029", 0, "Arriving tomorrow",
                   [_item("Apple iPad Air 11-inch (M4)", "$626.29", seller="Amazon"),
                    _item("Apple iPad Air 11-inch (M4)", "$649.00", seller="Amazon.com")])],
        subtotal="$1,275.29",
    )
    rows = build_order_items(html)
    assert [(r.item_name, r.quantity, r.cost_per_item) for r in rows] == [
        ("Apple iPad Air 11-inch (M4)", 1, 626.29),
        ("Apple iPad Air 11-inch (M4) (Sold by Amazon.com @ $649.00)", 1, 649.0),
    ]
    assert {r.shipment for r in rows} == {"1"}
    assert round(sum(r.total_cost for r in rows), 2) == 1275.29


def test_the_cheaper_line_keeps_the_plain_name_whatever_the_page_order():
    """Stable across re-reads: if Amazon lists the dearer seller first, the names must not swap."""
    html = _details(
        "114-9990029-9990029", "September 7, 2026",
        [_shipment("114-9990029-9990029", 0, "Arriving tomorrow",
                   [_item("iPad", "$649.00", seller="Amazon.com"),
                    _item("iPad", "$626.29", seller="Amazon")])],
    )
    names = {r.cost_per_item: r.item_name for r in build_order_items(html)}
    assert names[626.29] == "iPad"
    assert names[649.0] == "iPad (Sold by Amazon.com @ $649.00)"


def test_same_seller_same_price_blocks_still_sum_not_suffix():
    """The split-quantity case is untouched: identical price blocks are one line, summed."""
    html = _details(
        "114-0000000-0000001", "September 7, 2026",
        [_shipment("114-0000000-0000001", 0, "Arriving tomorrow",
                   [_item("MacBook", "$1,259.99", qty=3, seller="Amazon.com"),
                    _item("MacBook", "$1,259.99", qty=3, seller="Amazon.com")])],
    )
    rows = build_order_items(html)
    assert [(r.item_name, r.quantity) for r in rows] == [("MacBook", 6)]


def test_a_residual_same_key_collision_fails_loudly_instead_of_merging(monkeypatch):
    """The tripwire behind the three known same-key shapes: with the sum and the seller/price
    disambiguation neutralised, two same-titled blocks in one shipment must raise -- so the page
    lands in a failure dossier -- rather than reach the ledger's collapse and lose a row's money."""
    import scrapers.amazon_business_mapping as M
    from scrapers.amazon_mapping import OrderPageShapeError

    monkeypatch.setattr(M, "_sum_split_quantity_lines", lambda rows: rows)
    monkeypatch.setattr(M, "_disambiguate_same_named_lines", lambda rows: rows)
    html = _details(
        "114-9990029-9990029", "September 7, 2026",
        [_shipment("114-9990029-9990029", 0, "Arriving tomorrow",
                   [_item("iPad Air", "$626.29"), _item("iPad Air", "$649.00")])],
    )
    with pytest.raises(OrderPageShapeError, match="share the ledger key"):
        build_order_items(html)


# --------------------------------------------------------------------------------------------------
# Package ID (column 33, 2026-09-09) — twin of the consumer tests, plus the expired-link case that
# only Business has a fixture for.
# --------------------------------------------------------------------------------------------------

PKG_OID = "111-9990021-9990021"


def _no_alerts(monkeypatch):
    calls = []
    monkeypatch.setattr("alerts.notifier.alert", lambda subject, body: calls.append((subject, body)))
    return calls


def test_package_id_is_the_cards_shipment_id_on_every_row():
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Delivered August 14", [_item("iPad", "$949.00", qty=3)], shipment_id="NWfgPHR2F"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("Case", "$20.00"), _item("Pencil", "$99.00")],
                   shipment_id="NxWmqLBj2")],
        subtotal="$2,966.00",
    )
    rows = build_order_items(html)
    assert [(r.shipment, r.item_name, r.package_id) for r in rows] == [
        ("1", "iPad", "NWfgPHR2F"), ("2", "Case", "NxWmqLBj2"), ("2", "Pencil", "NxWmqLBj2"),
    ]


def test_an_expired_track_link_still_yields_the_package_id_from_the_pop_link():
    """An OLD order: the ship-track link is gone, only "View your item" remains —
    no tracking page to hop to, but the shipmentId is still on the page."""
    html = _details(PKG_OID, "April 20, 2026",
                    [_shipment(PKG_OID, 0, "Delivered April 28", [_item("Watch", "$329.99", qty=5)],
                               shipment_id="Pk91KJl0p", pop_only=True)])
    row = build_order_items(html)[0]
    assert row.tracking_url == "" and row.package_id == "Pk91KJl0p"


def test_a_card_with_no_links_has_a_blank_package_id():
    html = _details(PKG_OID, "August 12, 2026",
                    [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00")], track=False)])
    assert build_order_items(html)[0].package_id == ""


def test_two_identical_cards_sharing_a_shipment_id_collapse_to_the_tracked_one(monkeypatch):
    """The 2026-08-22 double render on THIS retailer (111-9990021-9990021): both duplicate rows on
    the ledger carried NxWmqLBj2. The card whose pt page gave a number survives as Shipment 1."""
    calls = _no_alerts(monkeypatch)
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00", qty=3)], shipment_id="NxWmqLBj2"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("iPad", "$949.00", qty=3)], shipment_id="NxWmqLBj2")],
        subtotal="$2,847.00",
    )
    rows = build_order_items(html, tracking_by_shipment={"2": "TBA999000000009"})
    assert [(r.shipment, r.quantity, r.tracking_number, r.package_id) for r in rows] == [
        ("1", 3, "TBA999000000009", "NxWmqLBj2"),
    ]
    assert sum(r.total_cost for r in rows) == 2847.0
    assert len(calls) == 1 and "rendered twice" in calls[0][0]


def test_distinct_shipment_ids_never_collapse_a_genuine_split(monkeypatch):
    calls = _no_alerts(monkeypatch)
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00", qty=1)], shipment_id="AAA"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("iPad", "$949.00", qty=1)], shipment_id="BBB")],
        subtotal="$1,898.00",
    )
    rows = build_order_items(html)
    assert [(r.shipment, r.package_id) for r in rows] == [("1", "AAA"), ("2", "BBB")]
    assert calls == []


def test_same_id_identical_cards_that_fit_the_subtotal_are_not_collapsed(monkeypatch):
    calls = _no_alerts(monkeypatch)
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00", qty=1)], shipment_id="SAME"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("iPad", "$949.00", qty=1)], shipment_id="SAME")],
        subtotal="$1,898.00",
    )
    rows = build_order_items(html)
    assert [(r.shipment, r.quantity) for r in rows] == [("1", 1), ("2", 1)]
    assert calls == []


def test_same_id_cards_with_different_contents_are_left_alone(monkeypatch):
    calls = _no_alerts(monkeypatch)
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00")], shipment_id="SAME"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("Case", "$20.00")], shipment_id="SAME")],
        subtotal="$969.00",
    )
    rows = build_order_items(html)
    assert [(r.shipment, r.item_name) for r in rows] == [("1", "iPad"), ("2", "Case")]
    assert calls == []


# --- the rebuilt payment widget ------
# Amazon replaced the pmts-* payment-method list with a server-rendered Next.js "ViewPurchase"
# widget: the card renders as three spans (name / mask dots / last 4) and the words "ending in"
# are gone from the page, so _ENDING_IN_RE alone left Card Last 4 blank on the ledger. Twin of the
# consumer test; the widget was captured on this Business order.
def _widget(*rows: str) -> str:
    return (
        '<div data-component="viewPaymentPlanSummaryWidget"><div id="__next">'
        '<div aria-label="payment method list">' + "".join(
            f'<div aria-label="payment method">{r}</div>' for r in rows
        ) + "</div></div></div>"
    )


def _widget_card(name: str = "Prime Business Card", last4: str = "0315") -> str:
    return (
        '<div data-testid="payment-instrument">'
        f'<span data-testid="payment-instrument-name">{name}</span>'
        '<span data-testid="payment-instrument-prefix">\u2022\u2022\u2022\u2022</span>'
        f'<span data-testid="payment-instrument-number">{last4}</span></div>'
    )


# The numberless tender rows, as captured live: the points instrument
# (111-9990010-9990010, THIS retailer) and a spent cash-back balance (111-9990008-9990008).
_WIDGET_POINTS_ROW = ('<div data-testid="payment-instrument">'
                      '<span data-testid="payment-instrument-name">Prime Business Rewards</span></div>')
_WIDGET_CASH_BACK_ROW = ('<div data-testid="payment-instrument">'
                         '<span data-testid="payment-instrument-name">Prime Young Adults cash back</span>'
                         "<span>$15.98 applied</span></div>")

_NEW_PAYMENT_WIDGET = _widget(_widget_card())


def test_rebuilt_payment_widget_yields_card_last4():
    html = _details(
        "111-9990024-9990024", "September 11, 2026",
        [_shipment("111-9990024-9990024", 0, "Arriving September 20",
                   [_item("MacBook Air", "$1,249.00", qty=3)])],
    ).replace("<div>Payment method Prime Business Card ending in 1234 5% back</div>",
              _NEW_PAYMENT_WIDGET)
    assert "ending in" not in html
    rows = build_order_items(html, today="2026-09-11")
    assert rows[0].card_last4 == "0315"


def test_rebuilt_payment_widget_without_a_number_span_leaves_the_card_blank():
    html = _details(
        "111-9990024-9990024", "September 11, 2026",
        [_shipment("111-9990024-9990024", 0, "Arriving September 20",
                   [_item("MacBook Air", "$1,249.00")])],
    ).replace(
        "<div>Payment method Prime Business Card ending in 1234 5% back</div>",
        # A tender with no card digits (e.g. a gift-card instrument) renders name-only.
        '<div data-testid="payment-instrument">'
        '<span data-testid="payment-instrument-name">Amazon Gift Card</span></div>',
    )
    rows = build_order_items(html, today="2026-09-11")
    assert rows[0].card_last4 == ""


def test_rebuilt_widget_points_instrument_gates_the_transactions_read():
    from scrapers.amazon_business_mapping import order_uses_points
    html = _details(
        "111-9990010-9990010", "September 7, 2026",
        [_shipment("111-9990010-9990010", 0, "Delivered September 9", [_item("Filament", "$48.28")])],
    ).replace("<div>Payment method Prime Business Card ending in 1234 5% back</div>",
              _widget(_widget_card(), _WIDGET_POINTS_ROW))
    assert order_uses_points(html) is True
    # The amount still comes from the rewards ledger / transactions page; unknown stays blank.
    assert build_order_items(html, points_used=48.28)[0].rewards_used == 48.28
    assert build_order_items(html)[0].rewards_used is None


def test_rebuilt_widget_cash_back_row_is_not_a_points_tender():
    from scrapers.amazon_business_mapping import order_uses_points
    html = _details(
        "111-9990008-9990008", "September 5, 2026",
        [_shipment("111-9990008-9990008", 0, "Delivered September 7", [_item("Charger", "$15.98")])],
    ).replace("<div>Payment method Prime Business Card ending in 1234 5% back</div>",
              _widget(_widget_card("Prime Visa", "4345"), _WIDGET_CASH_BACK_ROW))
    assert order_uses_points(html) is False


def test_a_card_merely_named_rewards_is_not_a_points_tender():
    from scrapers.amazon_business_mapping import order_uses_points
    html = _details(
        "111-2223334-5556667", "September 7, 2026",
        [_shipment("111-2223334-5556667", 0, "Arriving Monday", [_item("Widget", "$10.00")])],
    ).replace("<div>Payment method Prime Business Card ending in 1234 5% back</div>",
              _widget(_widget_card("Amazon Rewards Visa", "1234")))
    assert order_uses_points(html) is False
    assert build_order_items(html)[0].card_last4 == "1234"


# --- missing_card_reason: the loud tripwire for a silently blank card ----------------------------
_AB_PAY_DIV = "<div>Payment method Prime Business Card ending in 1234 5% back</div>"


def test_blank_card_with_no_explanation_returns_a_reason():
    from scrapers.amazon_business_mapping import missing_card_reason
    html = _details(
        "111-9990024-9990024", "September 11, 2026",
        [_shipment("111-9990024-9990024", 0, "Arriving September 20", [_item("MacBook", "$1,249.00")])],
    ).replace(_AB_PAY_DIV, "")
    reason = missing_card_reason(html)
    assert reason and "no card last-4" in reason and "$10.87" in reason


def test_a_readable_card_in_either_shape_is_quiet():
    from scrapers.amazon_business_mapping import missing_card_reason
    shipments = [_shipment("111-2223334-5556667", 0, "Arriving Monday", [_item("Widget", "$10.00")])]
    old = _details("111-2223334-5556667", "September 11, 2026", shipments)
    new = old.replace(_AB_PAY_DIV, _widget(_widget_card()))
    assert missing_card_reason(old) is None
    assert missing_card_reason(new) is None


def test_points_gift_card_cash_back_and_cancelled_orders_are_legitimately_cardless():
    from scrapers.amazon_business_mapping import missing_card_reason
    oid = "111-2223334-5556667"
    shipments = [_shipment(oid, 0, "Arriving Monday", [_item("Widget", "$10.00")])]
    base = _details(oid, "September 11, 2026", shipments)

    points = base.replace(_AB_PAY_DIV, _widget(_WIDGET_POINTS_ROW))
    assert missing_card_reason(points) is None

    covered = _details(oid, "September 11, 2026", shipments, gift_card="$10.87").replace(_AB_PAY_DIV, "")
    assert missing_card_reason(covered) is None

    partial = _details(oid, "September 11, 2026", shipments, gift_card="$4.00").replace(_AB_PAY_DIV, "")
    assert missing_card_reason(partial) is not None

    cash = base.replace(_AB_PAY_DIV, "") \
        .replace("Grand Total: $10.87", "Prime for Young Adults cash back: -$10.87\nGrand Total: $10.87")
    assert missing_card_reason(cash) is None

    cancelled = _details(oid, "September 11, 2026",
                         [_shipment(oid, 0, "Cancelled", [_item("Widget", "$10.00")])]) \
        .replace(_AB_PAY_DIV, "")
    assert missing_card_reason(cancelled) is None


# --- unknown_tender_reason: any instrument the parser can't classify alerts ----------------------
def test_known_tenders_in_both_shapes_are_quiet():
    from scrapers.amazon_business_mapping import unknown_tender_reason
    oid = "111-2223334-5556667"
    shipments = [_shipment(oid, 0, "Arriving Monday", [_item("Widget", "$10.00")])]
    old = _details(oid, "September 12, 2026", shipments).replace(
        _AB_PAY_DIV,
        '<ul><li class="pmts-payments-instrument-detail-box-paystationpaymentmethod">Mastercard ending in 0315</li>'
        '<li class="pmts-payments-instrument-detail-box-paystationpaymentmethod">Amazon point</li>'
        '<li class="pmts-payments-instrument-detail-box-paystationpaymentmethod">Amazon gift card balance</li>'
        '<li class="pmts-payments-instrument-detail-box-paystationpaymentmethod">Prime for Young Adults cash back</li></ul>')
    new = _details(oid, "September 12, 2026", shipments).replace(
        _AB_PAY_DIV, _widget(_widget_card(), _WIDGET_POINTS_ROW, _WIDGET_CASH_BACK_ROW))
    assert unknown_tender_reason(old) is None
    assert unknown_tender_reason(new) is None
    # A plain card-only order has nothing to fire on.
    assert unknown_tender_reason(_details(oid, "September 12, 2026", shipments)) is None


def test_an_unclassifiable_instrument_alerts_in_either_shape():
    from scrapers.amazon_business_mapping import unknown_tender_reason
    oid = "111-2223334-5556667"
    shipments = [_shipment(oid, 0, "Arriving Monday", [_item("Widget", "$10.00")])]
    base = _details(oid, "September 12, 2026", shipments)
    old = base.replace(
        _AB_PAY_DIV,
        '<div>Payment method Prime Business Card ending in 1234'
        '<li class="pmts-payments-instrument-detail-box-paystationpaymentmethod">Pay by Invoice</li></div>')
    new = base.replace(
        _AB_PAY_DIV,
        _widget(_widget_card(),
                '<div data-testid="payment-instrument">'
                '<span data-testid="payment-instrument-name">Prime Flex Balance</span></div>'))
    for html, expect in ((old, "Pay by Invoice"), (new, "Prime Flex Balance")):
        reason = unknown_tender_reason(html)
        assert reason and expect in reason and "cannot price" in reason


# --- the payment-area error stub + the over-applied cash-back balance ------------------------------
_SUMMARY_STUB = ('<div data-component="orderSummary">Payment method\n'
                 "Unable to display payment details at the moment.</div>")


def _with_stub_summary(html: str) -> str:
    import re as _re
    return _re.sub(r'<div data-component="orderSummary">.*?</div>', _SUMMARY_STUB, html, flags=_re.S)


def test_a_summary_stub_leaves_every_amount_blank_never_a_fake_zero():
    oid = "111-9990007-9990007"
    html = _with_stub_summary(_details(
        oid, "September 14, 2026",
        [_shipment(oid, 0, "Arriving Wednesday", [_item("Busy Board", "$15.98")])],
    ))
    r = build_order_items(html, "profile-charlie", today="2026-09-14")[0]
    assert (r.gift_card, r.rewards_used, r.sales_tax, r.shipping) == (None, None, None, None)


def test_the_payment_error_state_gets_its_own_precise_reason():
    from scrapers.amazon_business_mapping import missing_card_reason
    oid = "111-9990007-9990007"
    html = _with_stub_summary(_details(
        oid, "September 14, 2026",
        [_shipment(oid, 0, "Arriving Wednesday", [_item("Busy Board", "$15.98")])],
    )).replace(_AB_PAY_DIV, "")
    reason = missing_card_reason(html)
    assert reason and "failed to render" in reason and "transient" in reason
    assert "shape changed" not in reason


def test_cash_back_applied_beyond_the_order_total_is_clamped_to_what_it_consumed():
    oid = "111-9990007-9990007"
    html = _details(
        oid, "September 14, 2026",
        [_shipment(oid, 0, "Arriving Wednesday", [_item("Busy Board", "$15.98"),
                                                  _item("Suction Kupz", "$19.95")])],
        subtotal="$35.93",
    ).replace("Total before tax: $10.00", "Total before tax: $35.93") \
     .replace("Estimated tax to be collected: $0.87", "Estimated tax to be collected: $0.00") \
     .replace("Grand Total: $10.87",
              "Prime for Young Adults cash back: -$89.10\nGrand Total: $0.00")
    r = build_order_items(html, "profile-charlie", today="2026-09-14")[0]
    assert r.rewards_used == 35.93


def test_free_shipping_discount_nets_the_shipping_charge():
    oid = "111-9990025-9990025"
    html = _details(
        oid, "September 14, 2026",
        [_shipment(oid, 0, "Arriving Monday", [_item("Speaker", "$16.99")])],
        shipping="$2.99",
    ).replace("Shipping &amp; Handling: $2.99",
              "Shipping &amp; Handling: $2.99\nFree Shipping: -$2.99")
    assert build_order_items(html, today="2026-09-14")[0].shipping == 0.0


def test_cash_back_frame_uses_the_pages_total_before_tax():
    oid = "111-9990025-9990025"
    html = _details(
        oid, "September 14, 2026",
        [_shipment(oid, 0, "Arriving Monday", [_item("Book", "$16.19"), _item("Speaker", "$16.99"),
                                               _item("Light Bar", "$19.99")])],
        shipping="$2.99", subtotal="$53.17",
    ).replace("Shipping &amp; Handling: $2.99",
              "Shipping &amp; Handling: $2.99\nFree Shipping: -$2.99") \
     .replace("Total before tax: $10.00", "Total before tax: $53.17") \
     .replace("Estimated tax to be collected: $0.87", "Estimated tax to be collected: $0.00") \
     .replace("Grand Total: $10.87",
              "Prime for Young Adults cash back: -$89.10\nGrand Total: $0.00")
    r = build_order_items(html, today="2026-09-14")[0]
    assert r.rewards_used == 53.17
    assert r.shipping == 0.0

from scrapers.amazon_business_mapping import FIELD_SOURCES, SELECTORS  # noqa: E402  (capture-gate tests)
from scrapers.amazon_mapping import OrderPageShapeError  # noqa: E402


# --- the capture gate + shape checks (2026-09-19) ----------------------------------------------
class TestWhatACaptureMustRead:
    """A cell that used to read and now does not is a shape change: identity cells raise (nothing
    recorded, dossier), the rest come back None for the client's capture gate to report -- never a
    quiet blank and never a quiet default."""

    OID = "111-2223334-5556667"

    def _page(self, **kw):
        return _details(self.OID, "August 11, 2026",
                        [_shipment(self.OID, 0, "Arriving Friday", [_item("Switch 2", "$449.00", qty=4)])], **kw)

    def test_a_missing_quantity_element_is_one_unit(self):
        html = _details(self.OID, "August 11, 2026",
                        [_shipment(self.OID, 0, "Arriving Friday", [_item("Switch 2", "$449.00")])])
        assert build_order_items(html, today="2026-08-11")[0].quantity == 1

    def test_a_quantity_element_with_no_digit_is_unread_not_one(self):
        html = self._page().replace('<div class="od-item-view-qty"><span>4</span></div>',
                                    '<div class="od-item-view-qty"><span></span></div>')
        row = build_order_items(html, today="2026-08-11")[0]
        assert row.quantity is None and row.total_cost is None

    def test_a_missing_unit_price_is_a_blank_cost_not_a_dropped_row(self):
        html = self._page().replace('data-component="unitPrice"', 'data-component="unitPriceRENAMED"')
        row = build_order_items(html, today="2026-08-11")[0]
        assert row.item_name == "Switch 2" and row.cost_per_item is None

    def test_no_order_date_is_a_shape_error(self):
        html = self._page().replace('<div data-component="orderDate">August 11, 2026</div>', "")
        with pytest.raises(OrderPageShapeError, match="order date could not be read"):
            build_order_items(html, today="2026-08-11")

    def test_no_order_id_anywhere_is_a_shape_error_not_an_empty_result(self):
        html = self._page().replace(self.OID, "")
        with pytest.raises(OrderPageShapeError, match="no order id"):
            build_order_items(html, today="2026-08-11")

    def test_items_with_no_shipment_cards_is_a_shape_error_not_zero_rows(self):
        html = self._page().replace('data-component="shipmentStatus"', 'data-component="shipmentStatusRENAMED"')
        with pytest.raises(OrderPageShapeError, match="no shipment cards"):
            build_order_items(html, today="2026-08-11")

    def test_an_unreadable_order_summary_is_a_dossier_problem_and_blank_amounts(self, tmp_path):
        import diagnostics
        html = self._page().replace('data-component="orderSummary"', 'data-component="orderSummaryRENAMED"')
        with diagnostics.collecting("amazon", "p", root=tmp_path) as d:
            row = build_order_items(html, today="2026-08-11")[0]
        assert row.shipping is None and row.sales_tax is None
        assert d.problems and "order summary could not be read" in d.problems[0] and self.OID in d.problems[0]

    def test_field_sources_cover_every_capture_field_with_declared_selectors(self):
        from models.order import CAPTURE_IDENTITY_FIELDS, CAPTURE_MANDATORY_FIELDS
        assert set(FIELD_SOURCES) == set(CAPTURE_IDENTITY_FIELDS + CAPTURE_MANDATORY_FIELDS)
        for field in ("order_id", "order_date", "item_name", "cost_per_item", "delivery_address"):
            assert FIELD_SOURCES[field] in SELECTORS.values()
