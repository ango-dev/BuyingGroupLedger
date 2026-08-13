"""The 2026-08-12 column reorder: the guard that stops a mis-ordered sheet being scrambled, the
Shipment relabelling, and the migration that rewrites existing rows.

The failure this guards against is the worst one this codebase has: rows are written POSITIONALLY from
column A, so a sheet whose columns are in a different order than FIELDNAMES would be overwritten with
values in the wrong cells — no exception, no log, just silently wrong money.
"""

import pytest

from models.order import FIELDNAMES, OrderItem, normalize_shipment, shipment_label
from scripts.reorder_sheet import plan_reorder
from sheets.ledger_sync import HEADER, sync_csv_to_sheet

from tests.test_ledger_sync import row, sheet, write_csv_file  # noqa: F401  (sheet is a fixture)

# The pre-reorder column order, as the live sheet actually held it before 2026-08-12.
OLD_HEADER = [
    "Retailer", "Profile", "Order ID", "Order Date", "Status", "Order Link", "Tracking Number",
    "Tracking Link", "Delivery Date", "Delivery Address", "Item Name", "Quantity", "Cost Per Item",
    "Shipping", "Total Cost", "Card Last 4", "Last Scraped At", "Shipment", "Buying Group",
    "Card", "Cashback Rate", "Insurance", "Payout Date", "Payout Amount", "Total Profit",
]


def old_row(**values):
    return [str(values.get(name, "")) for name in OLD_HEADER]


class TestShipmentLabel:
    def test_label_is_a_bare_number(self):
        assert shipment_label(1) == "1"
        assert shipment_label(12) == "12"

    @pytest.mark.parametrize(
        "raw,expected",
        [("Shipment 1", "1"), ("shipment 2", "2"), ("Shipment  3", "3"), ("SHIPMENT 4", "4"),
         ("5", "5"), ("", ""), ("  ", "")],
    )
    def test_normalize_strips_the_redundant_word(self, raw, expected):
        assert normalize_shipment(raw) == expected

    def test_unrecognized_label_is_kept_not_discarded(self):
        # An odd label is still a usable upsert key; dropping it would merge two shipments into one row.
        assert normalize_shipment("Box A") == "Box A"

    def test_order_item_normalizes_whatever_a_producer_sends(self):
        # The agent-fallback prompts still say "Shipment 1"; Shipment is part of the upsert key, so an
        # agent re-check must not create a second row for a shipment the API recorded as "1".
        item = OrderItem(retailer="Amazon", order_id="A1", order_date="2026-08-12",
                         item_name="W", shipment="Shipment 2")
        assert item.shipment == "2"


class TestOrderMismatchGuard:
    def test_sheet_in_the_old_order_is_refused_not_scrambled(self, sheet, tmp_path):
        sheet.rows = [
            list(OLD_HEADER),
            old_row(**{"Order ID": "A1", "Order Date": "2026-08-08", "Item Name": "Widget",
                       "Shipment": "1", "Total Cost": "199.99"}),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                 status="shipped"),
        )

        with pytest.raises(RuntimeError, match="different ORDER"):
            sync_csv_to_sheet(path)

        # The point of raising: the existing row is untouched, not overwritten with shuffled values.
        assert sheet.data_rows()[0][OLD_HEADER.index("Total Cost")] == "199.99"

    def test_error_points_at_the_migration_script(self, sheet, tmp_path):
        sheet.rows = [list(OLD_HEADER)]
        path = write_csv_file(tmp_path, dict(order_id="A1", order_date="d", item_name="W"))

        with pytest.raises(RuntimeError, match="scripts.reorder_sheet"):
            sync_csv_to_sheet(path)

    def test_correctly_ordered_sheet_still_syncs(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1"),
        )

        sync_csv_to_sheet(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("order_id")] == "A1"


class TestPlanReorder:
    def test_values_follow_their_column_name_not_their_position(self):
        old = old_row(**{"Order ID": "A1", "Item Name": "Widget", "Total Cost": "199.99",
                         "Card Last 4": "4321", "Buying Group": "BFMR", "Shipment": "Shipment 2"})

        plan = plan_reorder(list(OLD_HEADER), [old])

        new = plan["new_rows"][0]
        assert new[HEADER.index("Order ID")] == "A1"
        assert new[HEADER.index("Item Name")] == "Widget"
        # Coerced to a real number (_coerce), same as a normal sync_csv_to_sheet write — not left as
        # the text gspread/the RAW write would otherwise silently freeze it as.
        assert new[HEADER.index("Total Cost")] == 199.99
        assert new[HEADER.index("Card Last 4")] == "4321"  # NOT numeric-coerced: a leading zero is real data
        assert new[HEADER.index("Buying Group")] == "BFMR"

    def test_shipment_prefix_is_stripped_during_migration(self):
        # Shipment is part of the upsert key: leaving "Shipment 2" would duplicate against the "2"
        # that every scraper now emits. Also coerced to a real int, like _coerce("shipment", ...) does
        # on a normal write — the migration must not silently downgrade it to text.
        plan = plan_reorder(list(OLD_HEADER), [old_row(**{"Shipment": "Shipment 2"})])

        assert plan["new_rows"][0][HEADER.index("Shipment")] == 2
        assert plan["shipment_relabelled"] == 1

    def test_already_correct_sheet_is_a_no_op(self):
        plan = plan_reorder(list(HEADER), [row(order_id="A1", shipment="1")])

        assert plan["already_correct"] is True
        assert plan["shipment_relabelled"] == 0

    def test_missing_columns_are_added_blank(self):
        short = OLD_HEADER[: OLD_HEADER.index("Buying Group")]
        plan = plan_reorder(list(short), [[""] * len(short)])

        assert "Buying Group" in plan["added"]
        assert len(plan["new_rows"][0]) == len(HEADER)

    def test_unknown_sheet_columns_are_reported_not_silently_dropped(self):
        header = list(OLD_HEADER) + ["My Notes"]
        plan = plan_reorder(header, [old_row() + ["keep me"]])

        assert plan["dropped"] == ["My Notes"]

    def test_hand_written_formulas_outside_total_profit_are_flagged(self):
        # The migration writes RAW, which would turn a user's own formula into literal text.
        old = old_row(**{"Order ID": "A1", "Delivery Address": "=A1&B1"})
        plan = plan_reorder(list(OLD_HEADER), [old])

        assert plan["stray_formulas"] == [(2, "Delivery Address")]

    def test_total_profit_formula_is_not_flagged_as_stray(self):
        # It's exempt: the migration re-stamps it after the rewrite.
        old = old_row(**{"Order ID": "A1", "Total Profit": '=IF(X2="","",1)'})
        plan = plan_reorder(list(OLD_HEADER), [old])

        assert plan["stray_formulas"] == []
