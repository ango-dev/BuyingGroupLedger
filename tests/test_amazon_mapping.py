"""Offline tests for the pure Amazon HTML parsing in scrapers/amazon_mapping.py (no browser).

Amazon has no order JSON endpoint, so the deterministic path parses server-rendered HTML — the part
most likely to drift when Amazon changes its markup. These tests use SYNTHETIC HTML that mirrors the
real order-details `data-component` tree captured live (scripts/amazon_capture.py); no real order HTML
(PII) is committed. The live browser mechanics are proven by a real run instead.
"""

from scrapers.amazon_mapping import (
    build_order_items,
    discover_orders,
    parse_shipment_targets,
)

BASE = "https://www.amazon.com"


# --- HTML builders mirroring the real structure -------------------------------------------------
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
        href = (f"/gp/your-account/ship-track?orderId={order_id}&shipmentId={shipment_id}"
                f"&packageIndex=0&ref_=ppx_hzod_shipconns_dt_b_track_package_{index}&noPtRedirect=1")
        track_html = f'<div data-component="shipmentConnections"><a href="{href}">Track package</a></div>'
    return (
        '<div class="shipment-block">'
        f'<div class="a-row"><div data-component="shipmentStatus">{status_text}</div></div>'
        f'<div data-component="purchasedItems">{"".join(items)}</div>'
        f"{track_html}"
        "</div>"
    )


def _details(order_id: str, order_date: str, shipments: list[str], card: str = "1234",
             shipping: str = "$0.00", address: str = "Test Buyer\n123 Main St\nSampletown, CA 90000",
             earn: str = "", gift_card: str = "") -> str:
    # Amazon prints the paying card's earn line under the payment method, in its own pmts-* <li>.
    earn_html = (
        '<ul class="pmts-payments-instrument-list">'
        '<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
        f'<span class="a-list-item">{earn}</span></li></ul>'
    ) if earn else ""
    # The "Gift Card Amount" line only renders when a gift card actually paid part of the order.
    gift_html = f"Gift Card Amount: -{gift_card}\n" if gift_card else ""
    return (
        '<html><body><div id="orderDetails">'
        f'<div data-component="orderDate">{order_date}</div>'
        f'<div data-component="orderId">Order # {order_id}</div>'
        f'<div data-component="shippingAddress">{address}</div>'
        f'<div>Payment method Visa ending in {card}{earn_html}</div>'
        f'<div data-component="orderSummary">Item(s) Subtotal: $10.00\nShipping &amp; Handling: {shipping}\n'
        f"{gift_html}Grand Total: $10.00</div>"
        f'<div data-component="shipments">{"".join(shipments)}</div>'
        "</div>"
        # A recommendations carousel OUTSIDE #orderDetails must be ignored.
        '<div id="rhf"><div data-component="itemTitle">RECOMMENDED JUNK</div>'
        '<div data-component="unitPrice">$999.00</div></div>'
        "</body></html>"
    )


# --- discovery ----------------------------------------------------------------------------------
def test_discovery_scopes_to_order_details_links_only():
    html = (
        '<div class="order-card"><span>Order placed August 7, 2026</span>'
        '<a href="/gp/css/order-details?orderID=112-9990001-9990001">Details</a></div>'
        '<div class="order-card"><span>Order placed July 26, 2026</span>'
        '<a href="/your-orders/order-details?orderID=113-9990002-9990002">Details</a></div>'
        # A recommendation widget with a bogus order-number-looking id and NO order-details link.
        '<div class="p13n-carousel"><a href="/dp/B0XYZ?ref=000-0000000-8675309">buy again</a></div>'
    )
    assert discover_orders(html) == {
        "112-9990001-9990001": "2026-08-07",
        "113-9990002-9990002": "2026-07-26",
    }


def test_discovery_fallback_to_links_when_no_cards():
    html = '<a href="/gp/css/order-details?orderID=111-2223334-5556667">x</a>'
    assert discover_orders(html) == {"111-2223334-5556667": ""}


# --- single shipment / core fields --------------------------------------------------------------
def test_single_shipment_fields_and_total_cost():
    html = _details(
        "111-2223334-5556667", "July 26, 2026",
        [_shipment("111-2223334-5556667", 0, "Delivered July 28", [_item("Widget", "$17.85")])],
        card="0301", shipping="$0.00",
    )
    rows = build_order_items(html, "profile-bravo", today="2026-08-10")
    assert len(rows) == 1
    r = rows[0]
    assert (r.retailer, r.order_id, r.order_date) == ("Amazon", "111-2223334-5556667", "2026-07-26")
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
    # 'Arriving' maps to shipped, but with no tracking number the invariant downgrades to 'ordered'.
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


def test_delivered_today_uses_run_date():
    oid = "112-9990001-9990001"
    html = _details(oid, "August 7, 2026",
                    [_shipment(oid, 0, "Delivered today", [_item("AirPods", "$189.99")])])
    rows = build_order_items(html, today="2026-08-10")
    assert rows[0].delivery_date == "2026-08-10"


def test_arriving_estimate_fills_delivery_date_for_open_order():
    """An ordered/shipped order records its estimated arrival date (matching the agent), so both paths
    write the same delivery_date; a later re-check overwrites it with the actual delivered date."""
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Arriving Thursday, August 14", [_item("Switch 2", "$449.00", qty=4)])])
    rows = build_order_items(html, today="2026-08-11")
    assert rows[0].status == "ordered"          # no tracking number yet
    assert rows[0].delivery_date == "2026-08-14"  # ETA captured, not left blank


def test_bare_weekday_arrival_resolves_to_next_occurrence():
    """Amazon often shows a bare weekday ETA ("Arriving Friday") with no date — resolve it to the next
    occurrence so the API matches the agent (which does the same). 2026-08-11 is a Tuesday."""
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Arriving Friday", [_item("Switch 2", "$449.00", qty=4)])])
    rows = build_order_items(html, today="2026-08-11")
    assert rows[0].delivery_date == "2026-08-14"  # next Friday on/after Tue Aug 11


def test_abbreviated_month_estimate_parsed():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Arriving Aug 14", [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-08-11")[0].delivery_date == "2026-08-14"


def test_delivered_weekday_resolves_to_past_occurrence():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 7, 2026",
                    [_shipment(oid, 0, "Delivered Monday", [_item("Thing", "$5.00")])])
    # From Tue 2026-08-11, the most recent Monday is 2026-08-10.
    assert build_order_items(html, today="2026-08-11")[0].delivery_date == "2026-08-10"


def test_no_date_in_status_leaves_delivery_date_blank():
    oid = "111-2223334-5556667"
    html = _details(oid, "August 11, 2026",
                    [_shipment(oid, 0, "Preparing for shipment", [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-08-11")[0].delivery_date == ""


def test_year_rollover_when_delivered_month_before_order_month():
    oid = "111-2223334-5556667"
    html = _details(oid, "December 30, 2025",
                    [_shipment(oid, 0, "Delivered January 2", [_item("Thing", "$5.00")])])
    rows = build_order_items(html, today="2026-01-05")
    assert rows[0].delivery_date == "2026-01-02"


# --- promo cashback ------------------------------------------------------------------------------
# The order page advertises the paying card's earn line; only the "extra N%" half is read, and
# config.cards.tag_cards adds it to the card's own cards.json rate. Wordings below are the real ones
# captured live from two different cards.
OID = "111-2223334-5556667"


def _one_item_order(**kwargs) -> str:
    return _details(OID, "August 12, 2026",
                    [_shipment(OID, 0, "Delivered August 13", [_item("Thing", "$100.00", qty=1)])],
                    **kwargs)


def test_promo_extra_percent_is_parsed():
    html = _one_item_order(earn="Earn 5% back (cap applies) plus an extra 1% back on select items")
    assert build_order_items(html)[0]._promo_cashback_rate == 0.01


def test_promo_amazon_day_wording_is_parsed():
    html = _one_item_order(earn="Earns 5% back and extra 1% on items using Amazon Day delivery.")
    assert build_order_items(html)[0]._promo_cashback_rate == 0.01


def test_earn_line_without_an_extra_is_not_a_promo():
    # The base rate is cards.json's job — "Earn 5% back" alone must not become a bonus.
    html = _one_item_order(earn="Earn 5% back at Amazon.com")
    assert build_order_items(html)[0]._promo_cashback_rate is None


def test_no_earn_line_means_no_promo():
    assert build_order_items(_one_item_order())[0]._promo_cashback_rate is None


def test_promo_outside_order_details_is_ignored():
    """A promo-shaped line in the recommendations rail must never be read as this order's promo."""
    html = _one_item_order().replace(
        '<div id="rhf">',
        '<div id="rhf"><li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
        "<span>Get an extra 9% back today</span></li>",
    )
    assert build_order_items(html)[0]._promo_cashback_rate is None


# --- gift-card netting ---------------------------------------------------------------------------
# A gift card earns 0% cashback, so the recorded cost is scaled down to what the CARD actually paid;
# the sheet's Cashback = (Total Cost + Shipping) * rate then charges the promo to card spend only.
def test_gift_card_reduces_cost_to_what_the_card_paid():
    rows = build_order_items(_one_item_order(gift_card="$40.00"))
    assert rows[0].cost_per_item == 60.00
    assert rows[0].total_cost == 60.00


def test_gift_card_larger_than_the_basis_floors_cost_at_zero():
    """The real …2175042-8952239 case: a $14.04 gift card against a $12.85 order (the rest covered
    $1.19 of tax, which this ledger does not record). Cost must floor at 0, never go negative."""
    html = _details(OID, "August 11, 2026",
                    [_shipment(OID, 0, "Delivered August 12", [_item("Gummies", "$12.85", qty=1)])],
                    gift_card="$14.04")
    rows = build_order_items(html)
    assert rows[0].cost_per_item == 0.00
    assert rows[0].total_cost == 0.00


def test_gift_card_scales_shipping_too():
    html = _details(OID, "August 12, 2026",
                    [_shipment(OID, 0, "Delivered August 13", [_item("Thing", "$100.00", qty=1)])],
                    shipping="$10.00", gift_card="$55.00")
    rows = build_order_items(html)
    # basis 110 - 55 = 55 left, so everything halves and the row still sums to card spend.
    assert rows[0].cost_per_item == 50.00
    assert rows[0].shipping == 5.00
    assert rows[0].total_cost + rows[0].shipping == 55.00


def test_gift_card_prorates_across_shipments_by_cost():
    html = _details(OID, "August 12, 2026", [
        _shipment(OID, 0, "Delivered August 13", [_item("Big", "$60.00", qty=1)], shipment_id="S1"),
        _shipment(OID, 1, "Delivered August 14", [_item("Small", "$40.00", qty=1)], shipment_id="S2"),
    ], gift_card="$50.00")
    rows = build_order_items(html)
    assert [r.total_cost for r in rows] == [30.00, 20.00]
    assert sum(r.total_cost for r in rows) == 50.00  # == basis 100 - gift card 50


def test_gift_card_respects_quantity():
    html = _details(OID, "August 12, 2026",
                    [_shipment(OID, 0, "Delivered August 13", [_item("Thing", "$50.00", qty=2)])],
                    gift_card="$25.00")
    rows = build_order_items(html)
    assert rows[0].cost_per_item == 37.50  # 100 basis, 75 left, halved per unit -> 37.50
    assert rows[0].total_cost == 75.00


def test_no_gift_card_line_leaves_cost_untouched():
    rows = build_order_items(_one_item_order())
    assert rows[0].cost_per_item == 100.00
    assert rows[0].total_cost == 100.00


def test_netting_can_be_switched_off():
    rows = build_order_items(_one_item_order(gift_card="$40.00"), net_gift_cards=False)
    assert rows[0].cost_per_item == 100.00
    assert rows[0].total_cost == 100.00


def test_summary_lines_are_read_when_label_and_amount_are_separate_elements():
    """The live page puts each summary label and its amount in their own span, so the text comes back
    as 'Gift Card Amount:\\n-$14.04' — the builder above happens to emit them on one line, and both
    shapes must parse. Same for the earn line's real pmts-* markup."""
    from bs4 import BeautifulSoup

    from scrapers.amazon_mapping import _gift_card_amount, _order_region, _promo_cashback_rate

    html = (
        '<div id="orderDetails"><div data-component="orderSummary">'
        "<span>Item(s) Subtotal:</span><span>$12.85</span>"
        "<span>Shipping &amp; Handling:</span><span>$0.00</span>"
        "<span>Estimated tax to be collected:</span><span>$1.19</span>"
        "<span>Gift Card Amount:</span><span>-$14.04</span>"
        "<span>Grand Total:</span><span>$0.00</span></div>"
        '<ul><li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
        '<span class="a-list-item">Earns 5% back and extra 1% on items using Amazon Day delivery.'
        "</span></li></ul></div>"
    )
    region = _order_region(BeautifulSoup(html, "html.parser"))

    assert _gift_card_amount(region.select_one("[data-component='orderSummary']")) == 14.04
    assert _promo_cashback_rate(region) == 0.01
