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
        "total_cost", "shipping", "sales_tax", "gift_card", "rewards_used", "card_name",
        "cashback_rate", "cogs", "insurance", "payout_amount", "payout_date", "return_quantity",
        "return_date", "total_profit", "profile_label", "order_url", "tracking_url",
        "receipt_url", "delivery_address", "card_last4", "package_id", "last_scraped_at",
        "expected_payout",
    ]
    assert HEADER == [
        "Order Date", "Status", "Retailer", "Item Name", "Shipment", "Quantity", "Order ID",
        "Tracking Number", "Tracking Submitted", "Delivery Date", "Buying Group", "Cost Per Item",
        "Total Cost", "Shipping", "Sales Tax", "Gift Card", "Rewards Used", "Card",
        "Cashback Rate", "COGS", "Insurance", "Payout Amount", "Payout Date", "Return Qty",
        "Return Date", "Total Profit", "Profile", "Order Link", "Tracking Link", "Receipt Link",
        "Delivery Address", "Card Last 4", "Package ID", "Last Scraped At", "Expected Payout",
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
    assert STATUSES == ("ordered", "shipped", "delivered", "cancelled", "paid", "return", "superseded")


def test_retired_and_money_free_statuses_are_terminal():
    """A retired row is a closed record and a money-free row must never be re-read: both sets
    only make sense inside TERMINAL_STATUSES. `superseded` is both; `cancelled` is money-free but
    not retired (a cancelled row still feeds the open-purchase alert)."""
    from models.order import MONEY_FREE_STATUSES, RETIRED_STATUSES

    assert set(RETIRED_STATUSES) <= set(TERMINAL_STATUSES)
    assert set(MONEY_FREE_STATUSES) <= set(TERMINAL_STATUSES)
    assert "superseded" in RETIRED_STATUSES and "superseded" in MONEY_FREE_STATUSES
    assert "cancelled" in MONEY_FREE_STATUSES and "cancelled" not in RETIRED_STATUSES


def test_every_money_free_status_has_a_blank_field_list():
    """The vocabulary and the blanking table must stay in step, and a superseded row blanks
    strictly MORE than a cancelled one (Quantity, the multiplier that double-counted the box)."""
    from models.order import MONEY_FREE_STATUSES
    from sheets.ledger_sync import _BLANK_FIELDS_BY_STATUS

    assert set(_BLANK_FIELDS_BY_STATUS) == set(MONEY_FREE_STATUSES)
    assert set(_BLANK_FIELDS_BY_STATUS["superseded"]) > set(_BLANK_FIELDS_BY_STATUS["cancelled"])
    assert "quantity" in _BLANK_FIELDS_BY_STATUS["superseded"]
    assert "quantity" not in _BLANK_FIELDS_BY_STATUS["cancelled"]


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


def test_rewards_used_defaults_to_a_real_zero():
    """the column reads 0 by default, not blank. A scraper that knows nothing about
    rewards (Best Buy, Costco, a history import) therefore writes 0; the Amazon mappings override it
    with the amount, or with None only when the amount could not be read."""
    item = OrderItem(retailer="Costco", order_id="1", order_date="2026-09-08", item_name="Thing")
    assert item.rewards_used == 0.0
    assert OrderItem(retailer="Amazon", order_id="1", order_date="2026-09-08", item_name="Thing",
                     rewards_used="").rewards_used is None  # a blank CSV cell stays "unknown"


def test_package_id_defaults_blank_and_stays_text():
    """Package ID (2026-09-09; beside Card Last 4 since 2026-09-10): blank means unknown and never blocks a match, and the value is TEXT —
    a Costco packageNumber keeps its leading zeros through the model and through the sheet
    coercion, or the id on the sheet would no longer equal what the mapping emits."""
    from sheets.ledger_sync import _coerce

    item = OrderItem(retailer="Costco", order_id="1", order_date="2026-09-09", item_name="Thing")
    assert item.package_id == ""
    carton = "00009999990206101794"
    kept = OrderItem(retailer="Costco", order_id="1", order_date="2026-09-09", item_name="Thing",
                     package_id=carton)
    assert kept.package_id == carton
    assert kept.model_dump()["package_id"] == carton
    assert _coerce("package_id", carton) == carton
    assert FIELDNAMES.index("package_id") == FIELDNAMES.index("card_last4") + 1
    assert HEADER.index("Package ID") == HEADER.index("Card Last 4") + 1
