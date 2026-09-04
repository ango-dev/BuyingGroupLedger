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
              shipment_id: str = "SHIP", track: bool = True, pop_only: bool = False) -> str:
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
# An Amazon Gift Card Balance Reload reached the sheet on 2026-08-24 and booked $40.35 of cost against
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
    qty-3 block plus a badgeless (qty-1) block; the sheet recorded 3 and dropped the +1."""
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
