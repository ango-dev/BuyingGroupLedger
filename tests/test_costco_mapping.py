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


def test_every_row_has_the_costco_shipment_label_populated(details):
    items = build_order_items(details, "p", known_open_ids={"1399000001"})
    assert items, "fixture should produce rows"
    assert all(r.shipment.isdigit() for r in items)
    assert all(r.order_date and r.order_date[4] == "-" for r in items)
