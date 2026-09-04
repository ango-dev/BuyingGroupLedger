"""Offline tests for the Best Buy ss-api order payload -> ledger-row mapping.

Runs against captured `/profile/ss/api/v1/orders/<id>` responses (tests/fixtures/bestbuy_orders.json),
trimmed to the fields the live path reads and PII-scrubbed. This is the layer most likely to drift when
Best Buy changes its order model, and it's fully provable without a live call. Fixture states covered:
shipped single-shipment (multi-unit), ordered/not-shipped, delivered + still-ordered split, a 5-way
split, and a cancelled+digital order.
"""

import json
from pathlib import Path

import pytest

from scrapers.bestbuy_mapping import build_order_items

FIXTURE = Path(__file__).parent / "fixtures" / "bestbuy_orders.json"


@pytest.fixture(scope="module")
def payloads() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _rows_for(items, order_id):
    return [it for it in items if it.order_id == order_id]


def _by_shipment(rows):
    return {r.shipment: r for r in rows}


def test_shipped_single_shipment_sums_units(payloads):
    rows = _rows_for(build_order_items(payloads, "profile-alpha"), "BBY01-809900000004")
    assert len(rows) == 1  # 7 identical ASUS units in one shipment collapse to one row
    row = rows[0]
    assert row.retailer == "Best Buy"
    assert row.profile_label == "profile-alpha"
    assert row.shipment == "1"
    assert row.status == "shipped"
    assert row.quantity == 7
    assert row.cost_per_item == 299.99
    assert row.total_cost == 2099.93  # computed 7 * 299.99, matches the order total
    assert row.tracking_number == "529900000007"
    assert row.tracking_url.startswith("https://www.fedex.com/fedextrack/")
    assert row.card_last4 == "4321"
    assert "Reshipburg" in row.delivery_address  # the reship warehouse, not the personal CA address
    assert row.order_url == "https://www.bestbuy.com/profile/ss/orders/order-details/BBY01-809900000004/view"
    assert row.order_date == "2026-08-10"


def test_gift_card_and_sales_tax_are_order_level(payloads):
    """Order 0 carries `price.totalSalesTax` 8.25 and a giftCard tender of 40.00 beside the AMEX
    one. Both land on every row as the ORDER total (prorated at sync, netted by the COGS formula);
    the credit tender's own totalNetAmount must NOT count toward the gift-card figure."""
    row = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000004")[0]
    assert row.sales_tax == 8.25
    assert row.gift_card == 40.00
    assert row.total_cost == 2099.93  # gross -- the gift card does not scale the cost


def test_no_gift_tender_is_a_detected_zero_and_zero_tax_is_real(payloads):
    # Order 1 has only the AMEX tender (totalNetAmount 1599.96) and a real $0.00 tax line: tenders
    # present without a gift one IS the "no gift card" detection -> a real 0.0.
    row = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000006")[0]
    assert row.gift_card == 0.0
    assert row.sales_tax == 0.0


def test_a_payload_without_the_new_price_keys_emits_blank_tax(payloads):
    # An order captured before totalSalesTax was read: the absent KEY comes back None, not 0 (a
    # shape we can't see through). Its credit tender still proves "no gift card" -> 0.0.
    row = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000002")[0]
    assert row.sales_tax is None
    assert row.gift_card == 0.0


def test_an_empty_payments_list_leaves_gift_card_blank():
    from scrapers.bestbuy_mapping import _gift_card_total

    assert _gift_card_total([]) is None
    assert _gift_card_total(None) is None


def test_digital_lines_are_dropped(payloads):
    joined = " | ".join(it.item_name for it in build_order_items(payloads, "p"))
    for banned in ("Discord", "Game Pass", "Norton", "Apple Arcade", "Apple News",
                   "Apple TV", "Apple Music", "Trend Micro"):
        assert banned not in joined, f"{banned!r} is a digital line and must not produce a ledger row"


def test_ordered_not_yet_shipped(payloads):
    rows = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000006")
    assert len(rows) == 1  # 4 Samsung units, one shipment, not shipped yet
    row = rows[0]
    assert row.status == "ordered"
    assert row.quantity == 4
    assert row.tracking_number == ""
    assert row.delivery_date == ""  # blank while only ordered, even though an ETA exists
    assert row.shipment == "1"
    assert row.cost_per_item == 399.99
    assert row.total_cost == 1599.96


def test_delivered_and_ordered_split_numbers_shipments(payloads):
    rows = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000002")
    by_shipment = _by_shipment(rows)
    assert set(by_shipment) == {"1", "2", "3"}
    # Shipment 1: three delivered MacBooks collapse to one row.
    s1 = by_shipment["1"]
    assert s1.status == "delivered"
    assert s1.quantity == 3
    assert s1.cost_per_item == 1134.0
    assert s1.tracking_number == "529900000001"
    assert s1.delivery_date == "2026-08-07"  # carrier delivered date
    # Shipments 2 & 3: the qty-2 PS5 line, pre-split into two shipments, both still ordered.
    for label in ("2", "3"):
        assert by_shipment[label].status == "ordered"
        assert by_shipment[label].quantity == 1
        assert by_shipment[label].tracking_number == ""
    assert round(sum(r.total_cost for r in rows), 2) == 5201.98  # matches the order total


def test_shipping_on_every_shipment(payloads):
    # Order-level shipping is repeated on every shipment row (matches the agent path's convention).
    rows = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000002")
    assert all(r.shipping == 0.0 for r in rows)


def test_five_way_split_each_shipment_has_its_own_tracking(payloads):
    rows = _rows_for(build_order_items(payloads, "p"), "BBY01-809900000005")
    assert len(rows) == 5
    assert {r.shipment for r in rows} == {str(i) for i in range(1, 6)}
    trackings = {r.tracking_number for r in rows}
    assert len(trackings) == 5, "each shipment carries its own distinct tracking number"
    assert all(r.status == "shipped" for r in rows)
    assert all(r.quantity == 1 for r in rows)


def test_brand_new_cancelled_order_is_ignored_at_discovery(payloads):
    assert _rows_for(build_order_items(payloads, "p"), "BBY01-809900000001") == []


def test_recorded_order_that_became_cancelled_is_emitted(payloads):
    rows = _rows_for(
        build_order_items(payloads, "p", known_open_ids={"BBY01-809900000001"}),
        "BBY01-809900000001",
    )
    assert len(rows) == 1  # only the physical (cancelled) PNY line; the digital lines stay dropped
    row = rows[0]
    assert row.status == "cancelled"
    assert row.shipment == "1"
    assert row.tracking_number == ""
    assert "PNY" in row.item_name
    # Quantity is left blank so the cancelled re-check doesn't zero the recorded quantity.
    assert row.quantity is None


def test_every_row_is_well_formed(payloads):
    items = build_order_items(payloads, "p", known_open_ids={"BBY01-809900000001"})
    assert items
    for it in items:
        assert it.shipment.isdigit()
        assert it.order_date and it.order_date[4] == "-"
        assert it.order_url.endswith("/view")
        assert it.retailer == "Best Buy"



class TestAnUnparseablePayloadIsAShapeChange:
    """Returning [] for a payload the mapping cannot read looks exactly like 'nothing new' (the
    silent failure found live on Amazon, 2026-08-29). It must raise instead, carrying the payload."""

    def _first(self, payloads):
        import copy
        return copy.deepcopy(payloads[0])

    def test_a_renamed_items_key_raises(self, payloads):
        from scrapers.bestbuy_mapping import PayloadShapeError
        p = self._first(payloads)
        p["order"]["itemsRENAMED"] = p["order"].pop("items")
        with pytest.raises(PayloadShapeError, match="no `items`") as info:
            build_order_items([p])
        assert info.value.payload is p

    def test_a_renamed_fulfillment_groups_key_raises(self, payloads):
        from scrapers.bestbuy_mapping import PayloadShapeError
        p = self._first(payloads)
        p["order"]["groups"] = {"fulfillmentGroupsRENAMED": p["order"]["groups"]["fulfillmentGroups"]}
        with pytest.raises(PayloadShapeError, match="fulfillmentGroups"):
            build_order_items([p])

    def test_a_missing_order_object_or_id_raises(self, payloads):
        from scrapers.bestbuy_mapping import PayloadShapeError
        with pytest.raises(PayloadShapeError, match="no `order`"):
            build_order_items([{"data": {}}])
        p = self._first(payloads)
        p["order"].pop("userOrderId")
        with pytest.raises(PayloadShapeError, match="userOrderId"):
            build_order_items([p])

    def test_an_empty_fulfillment_group_list_is_not_a_shape_change(self, payloads):
        p = self._first(payloads)
        p["order"]["groups"]["fulfillmentGroups"] = []
        assert build_order_items([p]) == []


def test_the_live_confirmed_gift_tender_shape(payloads):
    """CONFIRMED LIVE on BBY03-809900000009 (the user's deliberate gift-card order):
    the gift tender is type 'giftCard' with totalNetAmount carrying what the card paid, while the
    IN-FLIGHT credit tender reads amount 0 / chargedAmount 0 with only totalNetAmount truthful —
    exactly the field choice _gift_card_total banked on. A KEYED card also has no creditCardNumber;
    its last-4 rides displayCreditCardNumber. And the order id prefix is BBY03, not BBY01."""
    payload = json.loads(json.dumps(payloads[0]))  # deep copy of a real shipped order
    payload["order"]["userOrderId"] = "BBY03-809900000009"
    payload["order"]["payments"] = [
        {"type": "creditCard", "cardEntryType": "KEYED", "amount": 0, "chargedAmount": 0.0,
         "requestedAmount": 0.43, "authorizedAmount": 0.43, "totalNetAmount": 0.43,
         "displayCreditCardNumber": "4341"},
        {"type": "giftCard", "amount": 5.0, "chargedAmount": 5.0, "totalNetAmount": 5.0,
         "displaySvcNumber": "8973"},
    ]
    rows = _rows_for(build_order_items([payload], "profile-alpha"), "BBY03-809900000009")
    assert rows, "a BBY03- order id must map like any BBY01- one"
    assert rows[0].gift_card == 5.0
    assert rows[0].card_last4 == "4341"
