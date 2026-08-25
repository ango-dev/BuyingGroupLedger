"""Offline tests for scripts/apply_sheet_formats.py.

WHY THIS SCRIPT EXISTS AT ALL, and why it needs tests: presentation on this sheet is bound to a column
POSITION, not to a column name, and `scripts/reorder_sheet.py` moves values without moving either the
cell number formats or the TABLE COLUMN TYPES. After the 2026-08-25 reorder that left a PERCENT type
on Payout Amount (a $631 payout rendered as "63100%") and moved the BOOLEAN checkbox off
Tracking Submitted onto Delivery Address.

The table type is the one that matters: it OVERRIDES the cell number format, so writing a PERCENT
format under a CURRENCY column does nothing at all -- silently, with the API returning success.
"""

import pytest

from scripts.apply_sheet_formats import (
    CURRENCY_COLUMNS,
    PERCENT_COLUMNS,
    TABLE_COLUMN_TYPES,
    plan_formats,
    plan_table_columns,
)
from sheets.ledger_sync import HEADER


def _table(**types):
    """A table whose column types are given by NAME, as the live API reports them."""
    return {"tableId": "1", "columnProperties": [
        {"columnIndex": HEADER.index(name), "columnName": name, "columnType": t}
        for name, t in types.items()
    ]}


class TestTheMapMatchesTheSchema:
    def test_every_named_column_exists(self):
        for name in (*CURRENCY_COLUMNS, *PERCENT_COLUMNS, *TABLE_COLUMN_TYPES):
            assert name in HEADER, f"{name} is not a column — the map has drifted from the schema"

    def test_the_date_columns_are_never_typed(self):
        """Order Date is in the upsert key and MUST stay plain ISO text.

        Typing it as DATE makes Sheets store a serial, which changes the key and duplicates the row on
        its next re-check — the design notes, and it cost a migration to undo the first time.
        """
        for name in ("Order Date", "Delivery Date", "Payout Date"):
            assert name not in TABLE_COLUMN_TYPES
            assert name not in CURRENCY_COLUMNS and name not in PERCENT_COLUMNS

    def test_the_money_columns_are_currency_in_both_maps(self):
        # The cell format is the fallback for if the Table is ever deleted, so the two must agree.
        for name in CURRENCY_COLUMNS:
            assert TABLE_COLUMN_TYPES[name] == "CURRENCY"
        for name in PERCENT_COLUMNS:
            assert TABLE_COLUMN_TYPES[name] == "PERCENT"


class TestTableColumnTypes:
    def test_types_follow_the_column_name_not_its_position(self):
        # The whole point: the plan is derived from HEADER, so it is correct after ANY reorder.
        columns = plan_table_columns(_table(), list(HEADER))
        by_name = {HEADER[c["columnIndex"]]: c.get("columnType") for c in columns}
        assert by_name["Cashback Rate"] == "PERCENT"
        assert by_name["Payout Amount"] == "CURRENCY"
        assert by_name["Tracking Submitted"] == "BOOLEAN"
        assert by_name["COGS"] == "CURRENCY"

    def test_a_type_stranded_on_the_wrong_column_is_cleared(self):
        """The exact damage a reorder does: a type left behind on whatever inherits the letter."""
        stranded = _table(**{"Delivery Address": "BOOLEAN", "Tracking Number": "CURRENCY"})

        columns = plan_table_columns(stranded, list(HEADER))

        by_name = {HEADER[c["columnIndex"]]: c.get("columnType") for c in columns}
        assert by_name["Delivery Address"] is None
        assert by_name["Tracking Number"] is None
        assert by_name["Tracking Submitted"] == "BOOLEAN"  # and it goes back where it belongs

    def test_the_status_dropdown_rule_is_carried_over_never_rebuilt(self):
        """It holds the status vocabulary. Regenerating it here would put a second copy of STATUSES
        in the codebase, free to drift from models.order."""
        rule = {"condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": "paid"}]}}
        table = _table(Status="DROPDOWN")
        table["columnProperties"][0]["dataValidationRule"] = rule

        columns = plan_table_columns(table, list(HEADER))

        status = columns[HEADER.index("Status")]
        assert status["columnType"] == "DROPDOWN"
        assert status["dataValidationRule"] == rule

    def test_every_column_is_described_so_the_update_is_total(self):
        # updateTable REPLACES columnProperties, so a column left out would lose its type.
        columns = plan_table_columns(_table(), list(HEADER))
        assert [c["columnIndex"] for c in columns] == list(range(len(HEADER)))


class TestCellFormats:
    def test_money_columns_get_currency_and_the_rate_gets_percent(self):
        plan = plan_formats(list(HEADER))
        by_name = {name: fmt["type"] for name, _i, fmt in plan["set"]}
        assert by_name["Total Cost"] == "CURRENCY"
        assert by_name["COGS"] == "CURRENCY"
        assert by_name["Cashback Rate"] == "PERCENT"

    def test_unmapped_columns_are_cleared_not_left_alone(self):
        """After a reorder a column may carry a format inherited from its letter's previous occupant;
        "leave it alone" would keep a tracking number formatted as currency forever."""
        cleared = {name for name, _i in plan_formats(list(HEADER))["clear"]}
        assert "Tracking Number" in cleared and "Card" in cleared
        assert "Total Cost" not in cleared

    def test_set_and_clear_together_cover_every_column_exactly_once(self):
        plan = plan_formats(list(HEADER))
        touched = [i for _n, i, _f in plan["set"]] + [i for _n, i in plan["clear"]]
        assert sorted(touched) == list(range(len(HEADER)))

    def test_a_missing_column_is_reported_rather_than_silently_skipped(self):
        plan = plan_formats([h for h in HEADER if h != "COGS"])
        assert "COGS" in plan["missing"]
