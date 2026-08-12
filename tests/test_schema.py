"""Guards against column drift between the three places that describe the ledger's shape.

Rows are written to the sheet POSITIONALLY from column A. If FIELDNAMES, HEADER, and the CSV writer
ever disagree on length or order, every row silently lands in the wrong columns — the worst failure
this codebase can have, and one no runtime error would announce. These tests are the tripwire.
"""

import csv

from models.order import FIELDNAMES, STATUSES, OrderItem
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
        "order_date", "status", "profile_label", "retailer", "item_name", "quantity", "order_id",
        "tracking_number", "shipment", "delivery_date",
        "cost_per_item", "shipping", "total_cost", "card_name", "cashback_rate",
        "delivery_address", "buying_group", "insurance", "payout_amount", "payout_date",
        "total_profit",
        "order_url", "tracking_url", "card_last4", "last_scraped_at",
    ]
    assert HEADER == [
        "Order Date", "Status", "Profile", "Retailer", "Item Name", "Quantity", "Order ID",
        "Tracking Number", "Shipment", "Delivery Date",
        "Cost Per Item", "Shipping", "Total Cost", "Card", "Cashback Rate",
        "Delivery Address", "Buying Group", "Insurance", "Payout Amount", "Payout Date",
        "Total Profit",
        "Order Link", "Tracking Link", "Card Last 4", "Last Scraped At",
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
    assert STATUSES == ("ordered", "shipped", "delivered", "cancelled")
