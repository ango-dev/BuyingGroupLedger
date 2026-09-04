"""Offline tests for the Costco GraphQL -> ledger-row mapping.

Runs against a captured `getOrderDetails` payload (tests/fixtures/costco_order_details.json), trimmed
to the same fields the live query requests. This is the layer most likely to drift when Costco changes
its schema, and it's fully provable without a live API call.
"""

import json
from pathlib import Path

import pytest

from scrapers.costco_mapping import build_order_items

FIXTURE = Path(__file__).parent / "fixtures" / "costco_order_details.json"


@pytest.fixture(scope="module")
def details() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _rows_for(items, order_id):
    return [it for it in items if it.order_id == order_id]


def _by_shipment(rows):
    return {r.shipment: r for r in rows}


def test_digital_fee_and_membership_lines_are_dropped(details):
    items = build_order_items(details, "profile-2")
    joined = " | ".join(it.item_name for it in items)
    for banned in ("Microsoft", "McAfee", "CA BEP", "Membership", "OFFICE 365"):
        assert banned not in joined, f"{banned!r} is non-shippable and must not produce a ledger row"


def test_single_shipment_delivered_order(details):
    rows = _rows_for(build_order_items(details, "profile-2"), "1399000006")
    assert len(rows) == 1
    row = rows[0]
    assert row.retailer == "Costco"
    assert row.profile_label == "profile-2"
    assert row.shipment == "1"
    assert row.status == "delivered"
    assert row.item_name.endswith("(Item #2045014)")
    assert row.quantity == 2
    assert row.cost_per_item == 699.99
    assert row.total_cost == 1399.98  # computed quantity * cost_per_item
    assert row.tracking_number == "1Z999TST0000000004"
    assert row.tracking_url.startswith("https://shipmenttracking.costco.com/")
    assert row.delivery_date == "2026-07-23"
    assert row.card_last4 == "1111"
    assert row.shipping == 0.0  # order-level shippingAndHandling (free shipping)
    assert "Testville" in row.delivery_address
    # Order link is the derivable order-details deep link (built from the order number).
    assert row.order_url == "https://www.costco.com/myaccount/#/app/4900eb1f-0c10-4bd9-99c3-c59e6c1ecebf/orderdetails/1399000006"


def test_shop_card_tender_is_the_gift_card_and_coupons_do_not_count(details):
    """Order 1399000006's tenders are Visa 659.99 + Wallet Shop Card 40.00 + Coupon 150.00 (the
    live-probed 1399000013 shape; `totalCharged` is the only valid amount field). The Shop Card is
    the order-level Gift Card; the Coupon must NOT count — Costco books a promo both as a Coupon
    tender AND as the line `discountAmount` already netted into cost_per_item, so counting it here
    would net the same money twice. Sales tax stays blank HERE because this trimmed fixture's
    tenders don't reconcile with its lines, and the derivation refuses to guess (see below)."""
    row = _rows_for(build_order_items(details, "p"), "1399000006")[0]
    assert row.gift_card == 40.0
    assert row.sales_tax is None
    assert row.cost_per_item == 699.99  # untouched by the shop card


def test_no_shop_card_tender_is_a_detected_zero(details):
    # Tenders present without a shop card IS the "no gift card" detection.
    for order in ("1399000004", "1399000005"):
        for row in _rows_for(build_order_items(details, "p"), order):
            assert row.gift_card == 0.0, order


class TestDerivedSalesTax:
    """The schema exposes no tax amount, so tax is DERIVED from the money equation
    `paid tenders = gross - discounts + shipping + tax` (coupon tenders excluded -- they duplicate
    the line discountAmount). The probe order 1399000013 reconciled this to exactly $0."""

    def _detail(self, payments, lines, shipping=0.0):
        return {"orderPayment": payments, "shippingAndHandling": shipping,
                "shipToAddress": [{"orderLineItems": lines}]}

    def test_the_probed_order_derives_to_zero(self):
        from scrapers.costco_mapping import _sales_tax_total

        detail = self._detail(
            [{"paymentType": "Visa", "totalCharged": 5419.88},
             {"paymentType": "Wallet Shop Card", "totalCharged": 40.0},
             {"paymentType": "Coupon", "totalCharged": 1800.10}],
            [{"price": 999.99, "quantity": 2, "discountAmount": 800.0},
             {"price": 699.99, "quantity": 2, "discountAmount": 0.0},
             {"price": 1149.99, "quantity": 2, "discountAmount": 600.0},
             {"price": 0.01, "quantity": 5, "discountAmount": 0.05},
             {"price": 749.99, "quantity": 2, "discountAmount": 400.0},
             {"price": 0.01, "quantity": 5, "discountAmount": 0.05}],
            shipping=59.96,
        )
        assert _sales_tax_total(detail) == 0.0

    def test_a_positive_residual_is_real_tax(self):
        from scrapers.costco_mapping import _sales_tax_total

        detail = self._detail([{"paymentType": "Visa", "totalCharged": 108.0}],
                              [{"price": 100.0, "quantity": 1, "discountAmount": 0.0}])
        assert _sales_tax_total(detail) == 8.0

    def test_a_negative_residual_refuses_rather_than_guesses(self):
        # A refund shrinking totalCharged (or a cancelled line still in the gross) breaks the
        # equation -- blank beats a number derived from broken inputs.
        from scrapers.costco_mapping import _sales_tax_total

        detail = self._detail([{"paymentType": "Visa", "totalCharged": 50.0}],
                              [{"price": 100.0, "quantity": 1, "discountAmount": 0.0}])
        assert _sales_tax_total(detail) is None

    def test_a_tender_with_no_readable_amount_refuses(self):
        from scrapers.costco_mapping import _sales_tax_total

        detail = self._detail([{"paymentType": "Visa"}],
                              [{"price": 100.0, "quantity": 1, "discountAmount": 0.0}])
        assert _sales_tax_total(detail) is None

    def test_coupon_tenders_never_count_toward_the_gift_card(self):
        from scrapers.costco_mapping import _gift_card_total

        payments = [{"paymentType": "Visa", "totalCharged": 100.0},
                    {"paymentType": "Wallet Shop Card", "totalCharged": 40.0},
                    {"paymentType": "Coupon", "totalCharged": 150.0}]
        assert _gift_card_total(payments) == 40.0


def test_order_url_is_populated_on_every_row(details):
    """The Order Link column should carry Costco's order-details deep link on every row, derived from
    the order number alone (no extra GraphQL field)."""
    items = build_order_items(details, "p", known_open_ids={"1399000001"})
    assert items
    for it in items:
        assert it.order_url.endswith(f"/orderdetails/{it.order_id}")
        assert it.order_url.startswith("https://www.costco.com/myaccount/")


def test_line_split_across_packages_becomes_numbered_shipments(details):
    rows = _rows_for(build_order_items(details, "p"), "1399000004")
    # Two physical packages for the one Dell SKU; the two e-delivery lines are dropped.
    assert len(rows) == 2
    by_shipment = _by_shipment(rows)
    assert set(by_shipment) == {"1", "2"}
    trackings = {r.tracking_number for r in rows}
    assert trackings == {"1Z999TST0000000002", "1Z999TST0000000003"}
    # Quantity split so the column still sums to the line total (2 x 899.99).
    assert sorted(r.quantity for r in rows) == [1, 1]
    assert round(sum(r.total_cost for r in rows), 2) == 1799.98
    # Shipping is ORDER-LEVEL (the payload's shippingAndHandling, 59.96 — NOT the per-line 29.98),
    # repeated on EVERY shipment row so the API and agent writers agree on this field.
    assert by_shipment["1"].shipping == 59.96
    assert by_shipment["2"].shipping == 59.96


def test_identical_truncated_descriptions_do_not_collide_on_the_key(details):
    """Four different watch SKUs share an identical truncated description. Appending the item number
    keeps each row's (item_name, shipment) key unique so they don't overwrite each other on upsert."""
    rows = _rows_for(build_order_items(details, "p"), "1399000003")
    assert len(rows) == 4
    keys = {(r.item_name, r.shipment) for r in rows}
    assert len(keys) == 4, "each SKU must land on a distinct upsert key"
    assert all(r.status == "delivered" for r in rows)
    for sku in ("1847785", "1847783", "1847778", "1847774"):
        assert any(f"(Item #{sku})" in r.item_name for r in rows)


def test_multi_box_order_numbers_shipments_by_package(details):
    """Order 1399000003 shipped in two boxes: three watches share tracking ...0001, one watch has its
    own tracking ...0006. The shipment number must reflect the physical box, not be 'Shipment 1' for
    all four."""
    rows = _rows_for(build_order_items(details, "p"), "1399000003")
    by_sku = {r.item_name.split("Item #")[1].rstrip(")"): r for r in rows}
    same_box = {"1847783", "1847778", "1847774"}  # all tracking ...0001
    box_shipments = {by_sku[s].shipment for s in same_box}
    assert len(box_shipments) == 1, "watches in the same box share one shipment number"
    assert by_sku["1847785"].shipment not in box_shipments, "the separately-boxed watch is a new shipment"
    assert len({r.shipment for r in rows}) == 2, "exactly two physical shipments in this order"


def test_fee_line_is_dropped_but_the_real_item_is_kept(details):
    rows = _rows_for(build_order_items(details, "p"), "1399000005")
    assert len(rows) == 1
    assert "AirPods" in rows[0].item_name
    assert rows[0].status == "delivered"


def test_membership_only_order_yields_no_rows(details):
    assert _rows_for(build_order_items(details, "p"), "1399000002") == []


def test_brand_new_cancelled_order_is_ignored_at_discovery(details):
    # Not in the open set -> a fully-cancelled order is skipped entirely.
    assert _rows_for(build_order_items(details, "p"), "1399000001") == []


def test_recorded_order_that_became_cancelled_is_emitted(details):
    rows = _rows_for(
        build_order_items(details, "p", known_open_ids={"1399000001"}), "1399000001"
    )
    # Only the physical Dell line survives; the two e-delivery lines are dropped.
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "cancelled"
    assert row.shipment == "1"
    assert row.tracking_number == ""
    assert "(Item #2045014)" in row.item_name
    assert "ReshipCo" in row.delivery_address


def test_order_level_discount_is_netted_into_cost_per_item(details):
    """Costco reports a discount as a per-LINE `discountAmount` against that line's (price x
    quantity) total, not a reduced `price` field. cost_per_item must reflect what was actually
    paid: (1499.99 x 2 - 1000.00) / 2 = 999.99, not the raw 1499.99 list price."""
    rows = _rows_for(build_order_items(details, "p"), "1399000008")
    assert len(rows) == 1
    row = rows[0]
    assert row.cost_per_item == 999.99
    assert row.quantity == 2
    assert row.total_cost == 1999.98  # quantity * discounted cost_per_item


def test_digital_line_discount_does_not_leak_onto_physical_rows(details):
    """The order's two 1-cent digital lines are ALSO discounted (to $0) via their own
    discountAmount, and the digital lines are dropped from the ledger entirely. That digital
    discount must never be attributed to the physical item — each SKU's discount comes only from
    its own line(s), not a shared order-level pool. If it leaked, the physical row's cost_per_item
    would be lower than 999.99."""
    rows = _rows_for(build_order_items(details, "p"), "1399000008")
    assert len(rows) == 1
    assert rows[0].cost_per_item == 999.99
    assert "Microsoft" not in rows[0].item_name and "McAfee" not in rows[0].item_name


def test_every_row_has_the_costco_shipment_label_populated(details):
    items = build_order_items(details, "p", known_open_ids={"1399000001"})
    assert items, "fixture should produce rows"
    assert all(r.shipment.isdigit() for r in items)
    assert all(r.order_date and r.order_date[4] == "-" for r in items)


# --- A re-labelled package must not become a second shipment -------------------------------------
# Costco can re-issue a NEW tracking number for the SAME physical carton when a package is delayed
# (Amazon does this too — the design notes). Shipment is part of the upsert key, so keying on the carrier
# label would both append a phantom row AND, because the numbering is a sort, renumber packages
# already written to the sheet. `packageNumber` is the carton's own id, so it survives the re-label.

_SPLIT_ORDER = "1399000004"          # one SKU, two UPS boxes, same shippedDate
_PKG_A = "00009999990181363460"      # tracking ...047 -> Shipment 1
_PKG_B = "00009999990181363477"      # tracking ...056 -> Shipment 2


def _split_order_line(details):
    """Deep copy of the fixture, plus the Dell line whose `shipment` list holds the two boxes."""
    copied = json.loads(json.dumps(details))
    detail = next(d for d in copied if d["orderNumber"] == _SPLIT_ORDER)
    line = next(
        li for st in detail["shipToAddress"] for li in st["orderLineItems"]
        if li["itemNumber"] == "1953694"
    )
    return copied, line


def _package(line, package_number):
    return next(p for p in line["shipment"] if p["packageNumber"] == package_number)


def test_a_re_issued_tracking_number_on_the_same_package_is_not_a_second_shipment(details):
    """Both labels present at once (the state the page shows mid-relabel) is still ONE carton."""
    copied, line = _split_order_line(details)
    dead = _package(line, _PKG_B)
    reissued = dict(dead, trackingNumber="1Z999TST0000000007",
                    shippedDate="2026-06-27T09:00:00", deliveredDate=None)
    line["shipment"].append(reissued)

    rows = _rows_for(build_order_items(copied, "p"), _SPLIT_ORDER)
    assert len(rows) == 2, "the re-labelled carton must not add a third row"
    assert set(_by_shipment(rows)) == {"1", "2"}
    # The LIVE label wins — submitting the dead number to a buying group is not undoable.
    assert _by_shipment(rows)["2"].tracking_number == "1Z999TST0000000007"
    assert _by_shipment(rows)["1"].tracking_number == "1Z999TST0000000002"
    # Quantity still sums to the line total, so cost is neither doubled nor lost.
    assert round(sum(r.total_cost for r in rows), 2) == 1799.98


def test_a_re_issued_label_does_not_renumber_the_other_packages(details):
    """The regression with the big blast radius: the old key sorted by tracking STRING, so a re-issued
    number that sorts earlier stole Shipment 1 and pushed the untouched carton to Shipment 2 — mis-keying
    rows already on the sheet. Numbering by packageNumber pins each carton where it was."""
    copied, line = _split_order_line(details)
    # A re-label on carton B whose tracking sorts BEFORE carton A's ...047.
    _package(line, _PKG_B)["trackingNumber"] = "1A000000000000000000"

    rows = _rows_for(build_order_items(copied, "p"), _SPLIT_ORDER)
    by_tracking = {r.tracking_number: r.shipment for r in rows}
    assert by_tracking["1Z999TST0000000002"] == "1", "the untouched carton keeps its shipment number"
    assert by_tracking["1A000000000000000000"] == "2", "the re-labelled carton keeps ITS number too"


def test_a_package_without_a_package_number_still_groups_by_tracking(details):
    """Tracking stays the fallback, so a package Costco reports with no packageNumber still gets a row
    rather than vanishing from the ledger."""
    copied, line = _split_order_line(details)
    for package in line["shipment"]:
        package.pop("packageNumber", None)

    rows = _rows_for(build_order_items(copied, "p"), _SPLIT_ORDER)
    assert len(rows) == 2
    assert {r.tracking_number for r in rows} == {"1Z999TST0000000002", "1Z999TST0000000003"}
    assert set(_by_shipment(rows)) == {"1", "2"}


def test_shipment_numbering_follows_package_number_not_the_tracking_string(details):
    """Pin the key itself: swapping ONLY the tracking numbers between the two cartons must swap which
    tracking number each shipment carries, without moving the shipment numbers themselves."""
    copied, line = _split_order_line(details)
    a, b = _package(line, _PKG_A), _package(line, _PKG_B)
    a["trackingNumber"], b["trackingNumber"] = b["trackingNumber"], a["trackingNumber"]

    rows = _rows_for(build_order_items(copied, "p"), _SPLIT_ORDER)
    by_shipment = _by_shipment(rows)
    assert by_shipment["1"].tracking_number == "1Z999TST0000000003"
    assert by_shipment["2"].tracking_number == "1Z999TST0000000002"


def test_an_unshipped_package_still_lands_in_the_trailing_bucket(details):
    """Only packages that actually shipped consume a number; a carton with no tracking yet must not
    claim one (and must not raise on the shipment lookup)."""
    copied, line = _split_order_line(details)
    _package(line, _PKG_B)["trackingNumber"] = ""

    rows = _rows_for(build_order_items(copied, "p"), _SPLIT_ORDER)
    assert len(rows) == 1, "one shipped carton, so one row"
    assert rows[0].shipment == "1"
    assert rows[0].tracking_number == "1Z999TST0000000002"



class TestAnUnparseableDetailIsAShapeChange:
    def _first(self, details):
        import copy
        return copy.deepcopy(details[0])

    def test_a_renamed_line_items_key_raises(self, details):
        from scrapers.costco_mapping import PayloadShapeError, build_order_items
        d = self._first(details)
        for shipto in d["shipToAddress"]:
            shipto["orderLineItemsRENAMED"] = shipto.pop("orderLineItems", [])
        with pytest.raises(PayloadShapeError, match="orderLineItems") as info:
            build_order_items([d])
        assert info.value.payload is d

    def test_a_missing_ship_to_list_or_order_number_raises(self, details):
        from scrapers.costco_mapping import PayloadShapeError, build_order_items
        d = self._first(details)
        d.pop("shipToAddress")
        with pytest.raises(PayloadShapeError, match="shipToAddress"):
            build_order_items([d])
        d = self._first(details)
        d.pop("orderNumber")
        with pytest.raises(PayloadShapeError, match="orderNumber"):
            build_order_items([d])


# --- purchased Shop Cards ---------------------
def _shop_card_detail(order_id="1399000011", price=100.0, qty=1, discount=0.0):
    return {
        "orderNumber": order_id,
        "orderPlacedDate": "2026-04-24T16:46:07.377",
        "status": "Shipped",
        "shippingAndHandling": 0.0,
        "orderPayment": [{"paymentType": "Visa", "cardNumber": "4351", "totalCharged": price * qty}],
        "shipToAddress": [{
            "firstName": "A", "lastName": "N", "line1": "x", "city": "Sampletown",
            "state": "CA", "postalCode": "90000", "countryCode": "US",
            "orderLineItems": [{
                "itemNumber": "9999",
                "itemDescription": f"Costco Shop Card, Digital, ${int(price)} Shop Card",
                "price": price, "quantity": qty, "discountAmount": discount,
                "isFeeItem": False, "carrierItemCategory": "Digital",
                "orderedShipMethod": "EDG", "itemStatus": {}, "shipment": [],
            }],
        }],
    }


def test_a_purchased_shop_card_gets_its_own_paid_row():
    """The Amazon 1i model ported: a Shop Card bought on a card with an explicit Costco rate in
    cards.json is inventory funding — its own row, `paid` with a real $0 payout/$0 insurance,
    order date as payout AND delivery date, Gift Card tag (deliberately unrouted)."""
    rows = build_order_items([_shop_card_detail()], "profile-alpha",
                             keep_digital_last4s=frozenset({"4351"}))
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "paid"
    assert r.buying_group == "Gift Card"
    assert (r.payout_amount, r.insurance) == (0.0, 0.0)
    assert r.payout_date == "2026-04-24" and r.delivery_date == "2026-04-24"
    assert r.quantity == 1 and r.cost_per_item == 100.0 and r.total_cost == 100.0


def test_a_shop_card_on_an_unlisted_card_is_personal_and_dropped():
    """Same gate as both Amazons: no explicit Costco rate on the paying card ->
    personal spending, no ledger row."""
    assert build_order_items([_shop_card_detail()], "profile-alpha") == []
    assert build_order_items([_shop_card_detail()], "profile-alpha",
                             keep_digital_last4s=frozenset({"4335"})) == []


def test_other_digital_lines_are_still_dropped_alongside_a_shop_card():
    detail = _shop_card_detail()
    detail["shipToAddress"][0]["orderLineItems"].append({
        "itemNumber": "8888", "itemDescription": "Microsoft 365 Personal Subscription (e-delivery)",
        "price": 0.01, "quantity": 1, "discountAmount": 0.01, "isFeeItem": False,
        "carrierItemCategory": "Digital", "orderedShipMethod": "EDG", "itemStatus": {}, "shipment": [],
    })
    rows = build_order_items([detail], "profile-alpha", keep_digital_last4s=frozenset({"4351"}))
    assert [r.item_name for r in rows] == ["Costco Shop Card, Digital, $100 Shop Card"]
