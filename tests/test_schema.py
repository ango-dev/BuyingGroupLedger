"""Guards against column drift between the three places that describe the ledger's shape.

Rows are written to the sheet POSITIONALLY from column A. If FIELDNAMES, HEADER, and the CSV writer
ever disagree on length or order, every row silently lands in the wrong columns — the worst failure
this codebase can have, and one no runtime error would announce. These tests are the tripwire.
"""

import csv

from models.order import FIELDNAMES, STATUSES, TERMINAL_STATUSES, OrderItem
from output.csv_writer import write_csv
from sheets.ledger_sync import HEADER


def test_header_and_fieldnames_have_matching_length():
    assert len(HEADER) == len(FIELDNAMES), (
        "HEADER (sheets/ledger_sync.py) and FIELDNAMES (models/order.py) describe the same columns "
        "positionally. Adding a column means appending to BOTH."
    )


def test_column_order_is_pinned():
    """The exact column order, pinned in full — changing it is a MIGRATION, not an edit.

    Rows are written to the sheet positionally from column A, so reordering FIELDNAMES/HEADER without
    rewriting the rows already on the sheet silently scrambles every one of them, and moves the cells
    the Total Profit formula points at. This test is the tripwire: if you meant to reorder, update the
    lists here too AND run `python -m scripts.reorder_sheet --apply` against the live sheet.

    Adding a column is the cheap case — APPEND it to both lists and to the end of both lists here;
    existing rows just gain a trailing blank and no migration is needed.
    """
    assert FIELDNAMES == [
        "order_date", "status", "retailer", "item_name", "shipment", "quantity", "order_id",
        "tracking_number", "tracking_submitted", "delivery_date", "buying_group", "cost_per_item",
        "total_cost", "shipping", "card_name", "cashback_rate", "cogs", "insurance",
        "payout_amount", "payout_date", "total_profit", "profile_label", "order_url",
        "tracking_url", "receipt_url", "delivery_address", "card_last4", "last_scraped_at",
    ]
    assert HEADER == [
        "Order Date", "Status", "Retailer", "Item Name", "Shipment", "Quantity", "Order ID",
        "Tracking Number", "Tracking Submitted", "Delivery Date", "Buying Group", "Cost Per Item",
        "Total Cost", "Shipping", "Card", "Cashback Rate", "COGS", "Insurance", "Payout Amount",
        "Payout Date", "Total Profit", "Profile", "Order Link", "Tracking Link", "Receipt Link",
        "Delivery Address", "Card Last 4", "Last Scraped At",
    ]


def test_fieldnames_match_order_item_fields():
    """FIELDNAMES must name real OrderItem fields, plus last_scraped_at which csv_writer injects."""
    model_fields = set(OrderItem.model_fields)
    named = set(FIELDNAMES)
    assert named - model_fields == {"last_scraped_at"}, (
        "FIELDNAMES contains a name that is not an OrderItem field (and is not the injected "
        "last_scraped_at)."
    )
    assert model_fields - named == set(), "An OrderItem field is missing from FIELDNAMES."


def test_csv_writer_emits_exactly_fieldnames(tmp_path):
    """The CSV header is what sync_csv_to_sheet reads by name, so it must be FIELDNAMES verbatim."""
    item = OrderItem(retailer="Amazon", order_id="1", order_date="2026-08-08", item_name="Thing")

    path = write_csv([item], output_dir=tmp_path)

    with path.open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == FIELDNAMES


def test_statuses_vocabulary_is_what_the_rollup_expects():
    # load_order_state's rollup keys off these exact lowercase strings.
    assert STATUSES == ("ordered", "shipped", "delivered", "cancelled", "paid", "return")


def test_every_status_is_either_terminal_or_a_scraped_lifecycle_state():
    """The vocabulary splits cleanly in two, and the split is what decides re-check cost.

    Anything not in TERMINAL_STATUSES keeps its order in load_order_state's open list, so it gets
    re-read on every run forever. Adding a status without deciding which side it lands on is the
    expensive mistake this pins.
    """
    scraped_lifecycle = {"ordered", "shipped"}

    assert set(STATUSES) == scraped_lifecycle | set(TERMINAL_STATUSES)
    assert not scraped_lifecycle & set(TERMINAL_STATUSES)


def test_hand_entered_statuses_are_terminal():
    """"paid" and "return" are hand-entered only — no scraper emits them and nothing transitions a
    row into them, so if they were non-terminal nothing would ever correct the order back out of the
    re-check list."""
    for status in ("paid", "return"):
        assert status in TERMINAL_STATUSES
