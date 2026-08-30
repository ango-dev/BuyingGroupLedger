"""Offline tests for the pure Amazon HTML parsing in scrapers/amazon_mapping.py (no browser).

Amazon has no order JSON endpoint, so the deterministic path parses server-rendered HTML — the part
most likely to drift when Amazon changes its markup. These tests use SYNTHETIC HTML that mirrors the
real order-details `data-component` tree captured live (scripts/amazon_capture.py); no real order HTML
(PII) is committed. The live browser mechanics are proven by a real run instead.
"""

import pytest

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
             earn: str = "", gift_card: str = "", subtotal: str = "") -> str:
    # Amazon prints the paying card's earn line under the payment method, in its own pmts-* <li>.
    earn_html = (
        '<ul class="pmts-payments-instrument-list">'
        '<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
        f'<span class="a-list-item">{earn}</span></li></ul>'
    ) if earn else ""
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
        f'<div>Payment method Visa ending in {card}{earn_html}</div>'
        f'<div data-component="orderSummary">{subtotal_line}Shipping &amp; Handling: {shipping}\n'
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


@pytest.mark.parametrize("earn", [
    # The bare wording REAL non-promo orders carry (seen on four captured 0315 orders) — the case
    # that matters most, since it is what distinguishes "no bonus this order" from a bonus.
    "5% back",
    "Earn 5% back at Amazon.com",
    "Earn 5% back (cap applies)",
])
def test_earn_line_without_an_extra_is_not_a_promo(earn):
    # The base rate is cards.json's job — an earn line with no "extra N%" must not become a bonus.
    assert build_order_items(_one_item_order(earn=earn))[0]._promo_cashback_rate is None


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


# --- re-tracked shipment / subtotal reconciliation ------------------------------------------------
# Amazon re-issues a new tracking number for the same package when it is delayed, and can render the
# package TWICE while that is in flight. Shipments are numbered by DOM position, so the second card
# lands as a brand-new row carrying the full cost again. The guard: an order's cards may not be worth
# more than the order's own subtotal.
RID = "111-9990021-9990021"


def _two_cards(subtotal: str, price: str = "$949.00", qty: int = 3, cards: int = 2, **kw) -> str:
    ships = [
        _shipment(RID, i, "Arriving Monday", [_item("iPad Pro", price, qty=qty)], shipment_id="S%d" % i)
        for i in range(cards)
    ]
    return _details(RID, "August 12, 2026", ships, subtotal=subtotal, **kw)


def test_a_repeated_shipment_card_is_collapsed_to_one_row():
    """The live case: two cards of 3 iPads against a $2,847 order would book $5,694."""
    html = _two_cards("$2,847.00")
    rows = build_order_items(html, tracking_by_shipment={"1": "TBA-DEAD", "2": "TBA-LIVE"})

    assert len(rows) == 1, "one physical shipment must produce one row"
    assert rows[0].total_cost == 2847.0
    assert rows[0].shipment == "1", "the survivor is renumbered so future scrapes match it"
    assert rows[0].tracking_number == "TBA-LIVE", "the re-issued label wins, not the dead one"


def test_a_genuine_split_is_left_alone():
    """The critical no-false-positive case: real boxes still sum to the subtotal."""
    html = _two_cards("$200.00", price="$100.00", qty=1)
    rows = build_order_items(html, tracking_by_shipment={"1": "T1", "2": "T2"})

    assert [r.shipment for r in rows] == ["1", "2"]
    assert sum(r.total_cost for r in rows) == 200.0


def test_no_subtotal_on_the_page_leaves_rows_untouched():
    html = _two_cards("")  # subtotal omitted -> nothing to reconcile against
    assert len(build_order_items(html)) == 2


def test_an_unresolvable_duplicate_is_marked_rather_than_guessed():
    """A qty-6 order split 3+3 where one box was re-tracked renders 3/3/3. Collapsing gets to 3, which
    still isn't 6 — so the quantity is left explicitly unresolved instead of booking a wrong number."""
    html = _two_cards("$5,694.00", cards=3)
    rows = build_order_items(html, tracking_by_shipment={"1": "A", "2": "B", "3": "C"})

    assert len(rows) == 1
    assert rows[0].quantity == "*", "'*' is the existing convention audit_sheet.unresolved_split_quantity reads"
    assert rows[0].total_cost is None, "no cost is better than a wrong cost"


def test_gift_card_netting_sees_the_corrected_basis():
    """Netting divides by the cost basis, so a duplicated card would make the reduction too gentle."""
    html = _two_cards("$100.00", price="$100.00", qty=1, gift_card="$40.00")
    rows = build_order_items(html, tracking_by_shipment={"1": "T1", "2": "T2"})

    assert len(rows) == 1
    assert rows[0].total_cost == 60.0, "100 - 40, not the 80 an inflated 200 basis would give"


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


def test_a_kept_gift_card_row_is_tagged_as_deliberately_unrouted():
    """It funds inventory, so its cost belongs on the ledger — but it will never be submitted to a
    buying group and never paid out on its own. Left blank it reads as an ordinary order still
    awaiting payment, which audit_sheet's cogs_inputs_complete would count as a year-boundary
    straddle for the life of the row."""
    from config.warehouses import GIFT_CARD, is_deliberately_unrouted, tag_and_filter_personal
    from models.warehouse import Jig, Warehouse

    html = _gift_card_order("Applied Gift Card balance is added to your account.",
                            "Amazon Gift Card Balance Reload")
    rows = build_order_items(html, keep_digital_last4s=BOOSTED)

    assert len(rows) == 1
    assert rows[0].buying_group == GIFT_CARD

    # And the tag must SURVIVE the classifier, which otherwise overwrites buying_group from the
    # address — a gift card has none, so it would be blanked back to "".
    kept, dropped, _unclassified = tag_and_filter_personal(
        rows, [Warehouse(buying_group="BFMR", jigs=[Jig(zip="10001")])]
    )
    assert len(kept) == 1 and dropped == 0
    assert kept[0].buying_group == GIFT_CARD
    assert is_deliberately_unrouted(kept[0].buying_group)


# --- an order page with NO item titles is a shape change, not "no orders" ------------------------
class TestZeroItemTitlesIsAShapeChange:
    """with `itemTitle` renamed, three order-details pages parsed to zero rows and the
    run logged 'nothing new in the lookback window' — no dossier, no alert. An order always lists its
    items, so zero title elements can only mean the selector stopped matching."""

    def test_a_page_without_item_titles_raises(self):
        from scrapers.amazon_mapping import OrderPageShapeError
        html = _details("111-1234567-1234567", "August 1, 2026", [_shipment("111-1234567-1234567", 1, "Delivered August 3", [
            '<div class="a-fixed-left-grid"><div data-component="itemTitleRENAMED">Widget</div></div>'])])
        with pytest.raises(OrderPageShapeError, match="no item titles"):
            build_order_items(html)

    def test_an_all_digital_order_still_has_titles_and_simply_yields_nothing(self):
        html = _details("111-2234567-1234567", "August 1, 2026", [_shipment("111-2234567-1234567", 1, "Digital order", [
            _item("Amazon.com eGift Card", "$50.00")])])
        assert build_order_items(html) == []

    def test_a_page_that_is_not_an_order_at_all_is_still_an_empty_result(self):
        assert build_order_items("<html><body>Sign in</body></html>") == []


def test_a_kept_gift_card_is_paid_with_a_zero_payout_dated_on_the_order():
    """User rule 2026-08-30: no buying group is ever involved, so the payout side is a real $0."""
    html = _details("111-1234567-1234567", "August 1, 2026",
                    [_shipment("111-1234567-1234567", 1, "Applied Gift Card balance is added to your account",
                               [_item("Amazon.com eGift Card", "$500.00")], track=False)], card="0315")
    rows = build_order_items(html, keep_digital_last4s=frozenset({"0315"}))
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "paid" and r.payout_amount == 0.0 and r.insurance == 0.0 and r.payout_date == "2026-08-01"
