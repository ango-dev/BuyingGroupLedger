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
    neutralise_coercing_types_request,
    plan_stale_checkbox_padding,
    plan_table_columns,
)
from sheets.ledger_sync import HEADER, _col_letter


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


class TestStaleCheckboxPadding:
    """An empty cell under a BOOLEAN column materialises a real False all the way down the table.

    Move that column in a reorder and reorder_sheet rewrites only the DATA block, so every row below
    it keeps the False in the column the checkbox used to occupy. It used to clear itself by luck --
    the vacated column normally became untyped, and clearing a type clears its values. The lifecycle
    reorder put Total Profit (CURRENCY) there instead, and a TYPED column does not clear them.

    Not cosmetic: ledger_sync._last_occupied_row only forgives a row whose SOLE content is a checkbox
    False, so two of them made the append anchor jump from row 43 to 984 -- the next scraped order
    would have landed ~940 rows below the ledger.
    """

    def _grid(self, stale_col=None, rows_below=3):
        header = list(HEADER)
        data = [""] * len(header)
        data[header.index("Order ID")] = "A1"
        grid = [header, data]
        for _ in range(rows_below):
            pad = [""] * len(header)
            pad[header.index("Tracking Submitted")] = "FALSE"   # where it belongs now
            if stale_col is not None:
                pad[header.index(stale_col)] = "FALSE"          # left behind by the move
            grid.append(pad)
        return grid, header

    def test_padding_left_in_a_vacated_column_is_found(self):
        grid, header = self._grid(stale_col="Total Profit")

        ranges = plan_stale_checkbox_padding(grid, header, "Tracking Submitted")

        letter = _col_letter(header.index("Total Profit"))
        assert ranges == [f"{letter}3:{letter}5"], ranges

    def test_the_checkbox_in_its_CURRENT_column_is_left_alone(self):
        # That padding is expected and _last_occupied_row already forgives it.
        grid, header = self._grid(stale_col=None)

        assert plan_stale_checkbox_padding(grid, header, "Tracking Submitted") == []

    def test_it_never_reaches_into_the_data_block(self):
        """A real row is rewritten by the reorder, so anything in it is data. Clearing into the block
        would delete a genuine value."""
        grid, header = self._grid(stale_col="Total Profit")

        ranges = plan_stale_checkbox_padding(grid, header, "Tracking Submitted")

        first_row = int(ranges[0].split(":")[0].lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        assert first_row == 3, "must start below the last row carrying an Order ID"

    def test_one_range_per_column_never_one_per_cell(self):
        # ~940 single-cell requests would be an absurd payload.
        grid, header = self._grid(stale_col="Total Profit", rows_below=200)

        assert len(plan_stale_checkbox_padding(grid, header, "Tracking Submitted")) == 1

    def test_a_clean_sheet_needs_no_clearing(self):
        header = list(HEADER)
        data = [""] * len(header)
        data[header.index("Order ID")] = "A1"

        assert plan_stale_checkbox_padding([header, data], header, "Tracking Submitted") == []


class TestNeutralisingCoercingTypesBeforeAWrite:
    """A table column TYPE does not just display a value -- a numeric one PARSES what is written into
    it. `reorder_sheet` moves values while the types are still on their OLD letters, which is the
    definition of the migration window.

    Live on 2026-08-31: `Card Last 4` moved into a position still typed PERCENT, so writing the text
    "0315" stored the NUMBER 766 and the leading zero was lost on 28 rows. RAW input, no error raised,
    and the result still looked like a card number. Only audit_sheet's card_last4_is_text caught it.
    """

    def _table(self, **types):
        return {"tableId": "1", "columnProperties": [
            {"columnIndex": HEADER.index(n), "columnName": n, "columnType": t}
            for n, t in types.items()
        ]}

    def _types(self, request):
        cols = request["updateTable"]["table"]["columnProperties"]
        return {HEADER[c["columnIndex"]]: c.get("columnType") for c in cols}

    def test_numeric_types_are_stripped(self):
        req = neutralise_coercing_types_request(
            self._table(**{"Gift Card": "PERCENT", "Total Cost": "CURRENCY"}), list(HEADER))

        types = self._types(req)
        assert types["Gift Card"] is None and types["Total Cost"] is None

    def test_the_checkbox_type_is_stripped_too(self):
        # BOOLEAN parses as well: a stray string written under it becomes TRUE/FALSE.
        req = neutralise_coercing_types_request(
            self._table(**{"Tracking Submitted": "BOOLEAN"}), list(HEADER))

        assert self._types(req)["Tracking Submitted"] is None

    def test_the_status_dropdown_and_its_rule_survive(self):
        """DROPDOWN validates rather than parses, so it cannot damage a value -- and clearing it would
        discard the dataValidationRule holding the status vocabulary, which nothing else stores."""
        rule = {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "paid"}]}}
        # Paired with a coercing type: with ONLY a dropdown there is nothing to clear and the helper
        # correctly returns None, so the preservation would never be exercised.
        table = self._table(Status="DROPDOWN", **{"Total Cost": "CURRENCY"})
        table["columnProperties"][0]["dataValidationRule"] = rule

        req = neutralise_coercing_types_request(table, list(HEADER))

        col = next(c for c in req["updateTable"]["table"]["columnProperties"]
                   if c["columnIndex"] == HEADER.index("Status"))
        assert col["columnType"] == "DROPDOWN"
        assert col["dataValidationRule"] == rule

    def test_every_column_is_described_so_the_update_is_total(self):
        # updateTable REPLACES columnProperties; a column left out would lose whatever it had.
        req = neutralise_coercing_types_request(self._table(**{"Total Cost": "CURRENCY"}), list(HEADER))
        cols = req["updateTable"]["table"]["columnProperties"]
        assert [c["columnIndex"] for c in cols] == list(range(len(HEADER)))

    def test_nothing_to_clear_is_a_no_op(self):
        assert neutralise_coercing_types_request(self._table(), list(HEADER)) is None
        assert neutralise_coercing_types_request({}, list(HEADER)) is None

    def test_apply_sheet_formats_puts_them_all_back(self):
        """The pair has to be complete: whatever this strips, plan_table_columns must restore."""
        stripped = set(TABLE_COLUMN_TYPES) - {"Status"}
        restored = {HEADER[c["columnIndex"]] for c in plan_table_columns({}, list(HEADER))
                    if c.get("columnType")}
        assert stripped <= restored
