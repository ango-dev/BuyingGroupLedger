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
              shipment_id: str | None = None, track: bool = True) -> str:
    # One shipmentId PER CARD by default, as on a real page (76 captured tokens, one per package):
    # two cards sharing an id is the double-render shape _collapse_same_package_cards exists for,
    # so a test wanting that passes the same shipment_id explicitly.
    shipment_id = shipment_id or f"SHIP{index}"
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
             earn: str = "", gift_card: str = "", subtotal: str = "", tax: str = "$0.00") -> str:
    # Amazon prints the paying card's earn line under the payment method, in its own pmts-* <li>.
    earn_html = (
        '<ul class="pmts-payments-instrument-list">'
        '<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
        f'<span class="a-list-item">{earn}</span></li></ul>'
    ) if earn else ""
    # The "Gift Card Amount" line only renders when a gift card actually paid part of the order.
    gift_html = f"Gift Card Amount: -{gift_card}\n" if gift_card else ""
    # The tax line renders on every real summary (usually $0.00); pass tax="" to model a summary
    # that failed to parse it, which must come back None rather than a hard 0.
    tax_html = f"Estimated tax to be collected: {tax}\n" if tax else ""
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
        f"{tax_html}{gift_html}Grand Total: $10.00</div>"
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


def test_moved_up_delivery_ignores_the_previously_expected_date():
    """A moved-up delivery renders the STALE estimate under the live one -- "Now arriving Monday" in
    the h4, "Previously expected September 15" in a second row (live capture 2026-09-11, order
    111-9990013-9990013 on profile-charlie; the Business twin's case is 111-9990012-9990012). The
    explicit-date scan used to prefer the stale date over the bare weekday, so the sheet kept the
    old estimate while Amazon said next Monday. 2026-09-11 is a Friday."""
    oid = "111-9990013-9990013"
    card = ('<h4 class="a-color-base od-status-message"><span>Now arriving Monday</span></h4>'
            '<div class="a-row od-status-message"><span>Previously expected September 15</span></div>')
    html = _details(oid, "September 7, 2026",
                    [_shipment(oid, 0, card, [_item("Apple 2026 MacBook Air 13-inch", "$1,499.00")])])
    rows = build_order_items(html, today="2026-09-11")
    assert rows[0].delivery_date == "2026-09-14"   # next Monday, not the stale September 15


def test_moved_up_delivery_with_an_explicit_new_date_takes_the_new_one():
    oid = "111-9990012-9990012"
    card = ("<h4><span>Now arriving October 3</span></h4>"
            "<div><span>Previously expected October 19</span></div>")
    html = _details(oid, "September 4, 2026",
                    [_shipment(oid, 0, card, [_item("Thing", "$5.00")])])
    assert build_order_items(html, today="2026-09-08")[0].delivery_date == "2026-10-03"


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


# --- gift card + sales tax ----------------------------------------------------------------------
# A gift card earns 0% cashback. The mapping used to scale the cost basis down to card spend and
# throw the amount away; the netting now lives in the sheet's COGS formula, so
# the mapping emits the GROSS cost plus the order-level Gift Card / Sales Tax amounts on every row
# (prorated cost-weighted at sync, exactly like shipping). test_profit_formula proves the algebra
# matches the old scaling to the cent.
def test_gift_card_is_emitted_and_cost_stays_gross():
    rows = build_order_items(_one_item_order(gift_card="$40.00"))
    assert rows[0].cost_per_item == 100.00
    assert rows[0].total_cost == 100.00
    assert rows[0].gift_card == 40.00


def test_a_gift_card_covering_the_grand_total_is_emitted_in_full():
    """The real …2175042-8952239 case: a $14.04 gift card against a $12.85 order carrying $1.19 of
    tax. The old scaling had to CAP the reduction at the pre-tax basis; with Sales Tax recorded the
    formula computes (12.85 - 14.04 + 1.19) * (1 - rate) = 0 exactly, no cap needed."""
    html = _details(OID, "August 11, 2026",
                    [_shipment(OID, 0, "Delivered August 12", [_item("Gummies", "$12.85", qty=1)])],
                    gift_card="$14.04", tax="$1.19")
    rows = build_order_items(html)
    assert rows[0].total_cost == 12.85
    assert rows[0].gift_card == 14.04
    assert rows[0].sales_tax == 1.19


def test_gift_card_and_tax_are_order_level_on_every_row():
    # The same contract as shipping: every row carries the order TOTAL; ledger_sync prorates.
    html = _details(OID, "August 12, 2026", [
        _shipment(OID, 0, "Delivered August 13", [_item("Big", "$60.00", qty=1)], shipment_id="S1"),
        _shipment(OID, 1, "Delivered August 14", [_item("Small", "$40.00", qty=1)], shipment_id="S2"),
    ], gift_card="$50.00", tax="$8.00")
    rows = build_order_items(html)
    assert [r.total_cost for r in rows] == [60.00, 40.00]
    assert [r.gift_card for r in rows] == [50.00, 50.00]
    assert [r.sales_tax for r in rows] == [8.00, 8.00]


def test_a_parsed_summary_without_the_lines_is_a_real_zero():
    """A detected 0 is a VALUE: a summary we parsed that shows no tax line and
    no gift-card line fills both cells with 0.00 -- "checked, none", not "unknown". Only a missing
    summary altogether stays None so a blank never overwrites the sheet."""
    from scrapers.amazon_mapping import _gift_card_amount, _sales_tax_amount

    rows = build_order_items(_one_item_order(tax=""))
    assert rows[0].sales_tax == 0.0
    assert rows[0].gift_card == 0.0
    assert _sales_tax_amount(None) is None
    assert _gift_card_amount(None) is None


def test_no_gift_card_line_is_a_detected_zero():
    rows = build_order_items(_one_item_order())
    assert rows[0].cost_per_item == 100.00
    assert rows[0].total_cost == 100.00
    assert rows[0].gift_card == 0.0
    assert rows[0].sales_tax == 0.0  # the builder's default $0.00 tax line


def test_netting_can_be_switched_off():
    # Toggle off -> the amount is simply not emitted, so COGS uses the full sticker cost.
    rows = build_order_items(_one_item_order(gift_card="$40.00"), net_gift_cards=False)
    assert rows[0].cost_per_item == 100.00
    assert rows[0].total_cost == 100.00
    assert rows[0].gift_card is None


def test_summary_lines_are_read_when_label_and_amount_are_separate_elements():
    """The live page puts each summary label and its amount in their own span, so the text comes back
    as 'Gift Card Amount:\\n-$14.04' — the builder above happens to emit them on one line, and both
    shapes must parse. Same for the earn line's real pmts-* markup."""
    from bs4 import BeautifulSoup

    from scrapers.amazon_mapping import (_gift_card_amount, _order_region, _promo_cashback_rate,
                                         _sales_tax_amount)

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
    assert _sales_tax_amount(region.select_one("[data-component='orderSummary']")) == 1.19
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


def test_gift_card_rides_the_collapsed_row_with_its_gross_cost():
    """The sync's proration divides by the Total Cost basis, so the duplicated card still has to be
    collapsed before the row leaves here — the survivor carries the gross cost and the gc amount."""
    html = _two_cards("$100.00", price="$100.00", qty=1, gift_card="$40.00")
    rows = build_order_items(html, tracking_by_shipment={"1": "T1", "2": "T2"})

    assert len(rows) == 1
    assert rows[0].total_cost == 100.0, "gross, not netted — the COGS formula subtracts the gc"
    assert rows[0].gift_card == 40.0


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
    assert r.delivery_date == "2026-08-01", "a gift card is delivered the day it is bought"


# --- split-quantity lines (one line rendered as several blocks in ONE shipment card) ------------
def test_a_quantity_split_across_blocks_in_one_shipment_sums_into_one_row():
    """Amazon sometimes splits one line's quantity into several item blocks inside ONE shipment
    card — and the qty-1 block carries NO qty badge at all. Live on 113-9990028-9990028:
    4 Apple Watches rendered as a qty-3 block plus a badgeless block. Two rows under the same
    upsert key collapse downstream, silently dropping the +1 — so the mapping must sum them."""
    html = _details(
        "113-9990028-9990028", "July 22, 2026",
        [_shipment("113-9990028-9990028", 0, "Delivered July 27",
                   [_item("Apple Watch S11", "$299.00", qty=3),
                    _item("Apple Watch S11", "$299.00")])],  # badgeless = qty 1
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
    """Twin of the Amazon Business test — 114-9990033-9990033's shape (two qty-3 badged blocks,
    subtotal rendered blank on the live page)."""
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
# The payment-method list of a points order, fixture-shaped.
_POINTS_INSTRUMENT = (
    '<ul class="pmts-payments-instrument-list">'
    '<li class="a-spacing-micro pmts-payments-instrument-detail-box-paystationpaymentmethod">'
    '<span class="a-list-item">Prime Business Card ending in 1234</span></li>'
    '<li class="pmts-payments-instrument-supplemental-box-paystationpaymentmethod">'
    '<span class="a-list-item">5% back</span></li>'
    '<li class="a-spacing-micro pmts-payments-instrument-detail-box-paystationpaymentmethod">'
    '<span class="a-list-item">Amazon point</span></li></ul>'
)


def _transactions(*entries):
    """The related-transactions page, fixture-shaped: one block per line item,
    each holding the label, the amount and a link naming the order."""
    blocks = "".join(
        '<div class="a-section a-spacing-base apx-transactions-line-item-component-container">'
        '<div class="a-row"><div class="a-column a-span9">'
        f'<span class="a-size-base a-text-bold">{label}</span></div>'
        '<div class="a-column a-span3 a-text-right a-span-last">'
        f'<span class="a-size-base-plus a-text-bold">{amount}</span></div></div>'
        '<div class="a-section"><a class="a-link-normal" '
        f'href="https://www.amazon.com/gp/css/summary/edit.html?orderID={oid}">Order #{oid}</a></div>'
        "</div>"
        for label, amount, oid in entries
    )
    return f'<html><body><div id="a-page">{blocks}</div></body></html>'


def test_a_spent_cash_back_balance_is_rewards_used_not_a_gift_card():
    """ "Prime for Young Adults cash back: -$15.98" paid the
    whole order. It lands in Rewards Used, while
    the Gift Card column keeps the real gift-card line alone. The bare label ALSO sits in the
    payment list (no colon, no amount) and must not count twice."""
    oid = "111-9990008-9990008"
    html = _details(oid, "September 7, 2026",
                    [_shipment(oid, 0, "Arriving tomorrow", [_item("Object Permanence Box", "$15.98")])],
                    gift_card="$4.02", subtotal="$15.98")
    html = html.replace("Gift Card Amount: -$4.02\n",
                        "Gift Card Amount: -$4.02\nPrime for Young Adults cash back: -$11.96\n")
    html = html.replace("Payment method Visa ending in 1234",
                        "Payment method Visa ending in 1234 Prime for Young Adults cash back")
    rows = build_order_items(html)
    assert rows[0].gift_card == 4.02           # the gift-card line only
    assert rows[0].rewards_used == 11.96       # the cash back, its own column
    assert rows[0].total_cost == 15.98         # the GROSS cost stays


def test_the_cash_back_label_alone_is_not_a_tender():
    from bs4 import BeautifulSoup

    from scrapers.amazon_mapping import _cash_back_used

    summary = BeautifulSoup(
        '<div data-component="orderSummary">Payment method Visa ending in 0301\n'
        "Prime for Young Adults cash back\nItem(s) Subtotal: $73.77\nGrand Total: $73.77</div>",
        "html.parser").div
    assert _cash_back_used(summary) == 0.0
    assert _cash_back_used(None) is None


def test_several_cash_back_lines_sum_and_split_spans_parse():
    from bs4 import BeautifulSoup

    from scrapers.amazon_mapping import _cash_back_used

    summary = BeautifulSoup(
        '<div data-component="orderSummary">'
        "<span>Prime for Young Adults cash back:</span><span>-$10.00</span>"
        "<span>Prime cash back:</span><span>-$2.50</span>"
        "<span>Grand Total:</span><span>$0.00</span></div>", "html.parser").div
    assert _cash_back_used(summary) == 12.50


def test_amazon_points_are_detected_from_the_payment_list_only():
    from scrapers.amazon_mapping import order_uses_points

    oid = "111-9990010-9990010"
    plain = _details(oid, "September 4, 2026", [_shipment(oid, 0, "Shipped", [_item("Book", "$48.28")])])
    assert order_uses_points(plain) is False
    assert order_uses_points(plain.replace("Payment method Visa ending in 1234",
                                           "Payment method " + _POINTS_INSTRUMENT)) is True
    # Marketing text elsewhere on the page never counts.
    assert order_uses_points(plain.replace("</body>", "<div>Shop with Amazon points!</div></body>")) is False


def test_points_used_reads_only_this_orders_lines_from_the_transactions_page():
    from scrapers.amazon_mapping import points_used_from_transactions

    oid, other = "111-9990010-9990010", "111-9999999-9999999"
    page = _transactions(("Amazon Points used", "-$48.28", oid),
                         ("Amazon Points used", "-$5.00", other),                 # another order
                         ("Prime Business Card ending in 0315", "-$10.00", oid))  # a card charge
    assert points_used_from_transactions(page, oid) == 48.28
    assert points_used_from_transactions(page, other) == 5.00
    # Two postings for one order sum (a multi-shipment order charges in pieces).
    two = _transactions(("Amazon Points used", "-$30.00", oid), ("Amazon Points used", "-$18.28", oid))
    assert points_used_from_transactions(two, oid) == 48.28
    # Nothing posted yet / shape changed / no amount -> unknown, never 0.
    assert points_used_from_transactions(_transactions(), oid) is None
    assert points_used_from_transactions("<html>It looks like we couldn't find the transaction</html>",
                                         oid) is None
    assert points_used_from_transactions(_transactions(("Amazon Points used", "pending", oid)), oid) is None


def test_points_land_in_rewards_used_and_an_unknown_amount_leaves_it_blank():
    """the order page showed the full $48.28 Grand Total with
    "Amazon point" listed as a tender — every dollar was points. The API client reads the amount off
    the rewards ledger / transactions page and hands it in; without it the cell must be BLANK, not a
    false 0. The Gift Card column is untouched by points."""
    oid = "111-9990010-9990010"
    html = _details(oid, "September 4, 2026", [_shipment(oid, 0, "Shipped", [_item("Book", "$48.28")])],
                    gift_card="$0.72", subtotal="$48.28")
    html = html.replace("Payment method Visa ending in 1234", "Payment method " + _POINTS_INSTRUMENT)
    row = build_order_items(html, points_used=47.56)[0]
    assert (row.gift_card, row.rewards_used) == (0.72, 47.56)
    row = build_order_items(html)[0]
    assert (row.gift_card, row.rewards_used) == (0.72, None)                   # amount unknown
    row = build_order_items(html, points_used=47.56, net_gift_cards=False)[0]
    assert (row.gift_card, row.rewards_used) == (None, None)
    # An order that paid by card alone ignores a stray points figure.
    plain = _details(oid, "September 4, 2026", [_shipment(oid, 0, "Shipped", [_item("Book", "$48.28")])])
    row = build_order_items(plain, points_used=47.56)[0]
    assert (row.gift_card, row.rewards_used) == (0.0, 0.0)


def test_the_non_card_tender_selectors_are_declared_for_the_audit():
    from scrapers.amazon_mapping import SELECTORS

    assert SELECTORS["payment_instrument"] == ".pmts-payments-instrument-detail-box-paystationpaymentmethod"
    assert SELECTORS["transactions_line_item"] == ".apx-transactions-line-item-component-container"


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
    import scrapers.amazon_mapping as M
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
# Package ID (column 33, 2026-09-09): the card's shipmentId rides onto every row of the card, and two
# cards sharing one id in a single parse are the re-label double render — collapsed to one.
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


def test_a_card_with_no_links_has_a_blank_package_id():
    html = _details(PKG_OID, "August 12, 2026",
                    [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00")], track=False)])
    assert build_order_items(html)[0].package_id == ""


def test_two_identical_cards_sharing_a_shipment_id_collapse_to_the_tracked_one(monkeypatch):
    """The 2026-08-22 double render: the superseded card and its replacement, one shipmentId. The
    card whose pt page gave a number survives, as Shipment 1, and the cost is booked once."""
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


def test_same_id_cards_prefer_the_last_when_neither_has_a_number(monkeypatch):
    """Amazon appends the re-issue, so with no number to prefer the LAST card is the live one."""
    _no_alerts(monkeypatch)
    first = _shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00", qty=3)], shipment_id="SAME")
    second = _shipment(PKG_OID, 1, "Arriving Tuesday", [_item("iPad", "$949.00", qty=3)], shipment_id="SAME")
    rows = build_order_items(_details(PKG_OID, "August 12, 2026", [first, second]))
    only_second = build_order_items(_details(PKG_OID, "August 12, 2026", [second]))
    assert len(rows) == 1 and rows[0].shipment == "1"
    assert rows[0].delivery_date == only_second[0].delivery_date != build_order_items(
        _details(PKG_OID, "August 12, 2026", [first]))[0].delivery_date


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
    """The subtotal is a veto: if the order really is worth two of these boxes, two same-id cards
    are not a duplicate this code understands, and collapsing would lose a paid-for unit."""
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


def test_same_id_identical_cards_collapse_when_the_subtotal_is_unreadable(monkeypatch):
    """Where the subtotal guard is blind, the id is the only protection -- and it works."""
    calls = _no_alerts(monkeypatch)
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00", qty=3)], shipment_id="SAME"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("iPad", "$949.00", qty=3)], shipment_id="SAME")],
    )
    rows = build_order_items(html)
    assert [(r.shipment, r.quantity) for r in rows] == [("1", 3)]
    assert len(calls) == 1


def test_same_id_cards_with_different_contents_are_left_alone(monkeypatch):
    """Not a duplicate we understand: two cards, one id, different items. Left for the subtotal guard."""
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


def test_collapse_renumbers_by_card_so_a_multi_sku_card_keeps_one_number(monkeypatch):
    _no_alerts(monkeypatch)
    html = _details(
        PKG_OID, "August 12, 2026",
        [_shipment(PKG_OID, 0, "Arriving Monday", [_item("iPad", "$949.00")], shipment_id="DUP"),
         _shipment(PKG_OID, 1, "Arriving Monday", [_item("iPad", "$949.00")], shipment_id="DUP"),
         _shipment(PKG_OID, 2, "Arriving Monday", [_item("Case", "$20.00"), _item("Pencil", "$99.00")],
                   shipment_id="OTHER")],
    )
    rows = build_order_items(html, tracking_by_shipment={"1": "TBA1", "3": "TBA3"})
    assert [(r.shipment, r.item_name, r.tracking_number) for r in rows] == [
        ("1", "iPad", "TBA1"), ("2", "Case", "TBA3"), ("2", "Pencil", "TBA3"),
    ]
