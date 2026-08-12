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


def test_new_columns_are_appended_at_the_end():
    """Columns are only ever APPENDED, never inserted or reordered.

    A mid-list insert misaligns every pre-existing sheet row. It also breaks the header migration in
    sync_csv_to_sheet, which only rewrites a header that is a strict PREFIX of the current HEADER —
    an older sheet must look like today's columns truncated at the right.

    So this pins the historical tail in the exact order it grew: ... Shipment, Buying Group, then the
    profit-accounting block. Adding a column means appending to BOTH lists and extending this tail.
    """
    tail_fields = [
        "shipment", "buying_group",
        "card_name", "cashback_rate", "insurance", "payout_date", "payout_amount", "total_profit",
    ]
    tail_headers = [
        "Shipment", "Buying Group",
        "Card", "Cashback Rate", "Insurance", "Payout Date", "Payout Amount", "Total Profit",
    ]
    assert FIELDNAMES[-len(tail_fields):] == tail_fields
    assert HEADER[-len(tail_headers):] == tail_headers


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
