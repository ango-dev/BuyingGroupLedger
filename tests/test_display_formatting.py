"""Sheet DISPLAY formatting must never corrupt the value underneath.

The round trip that makes this a real bug rather than a theoretical one:
`get_all_values()` returns FORMATTED text -> `_merge_row` preserves an existing cell whenever the
incoming value is blank (exactly what a partial re-check sends) -> the preserved value is written
straight back. So a percent-formatted Cashback Rate read as "4%" used to be rewritten as literal TEXT,
which breaks the Total Profit formula's arithmetic; a currency-formatted Total Cost as "$3,402.00"
would stop the column summing. Formatting a column is a readability choice a user makes in the sheet,
and it must survive a re-check untouched.

Scope note: this covers the NUMERIC columns only. Date columns are deliberately NOT handled — they are
stored as plain ISO text and must stay that way; formatting one as a Date breaks the
upsert key, and the fix for that was removed by request.
"""

from unittest.mock import patch

import pytest

from models.order import FIELDNAMES
from sheets import ledger_sync
from sheets.ledger_sync import HEADER, _coerce, _parse_display_number, sync_csv_to_sheet

from tests.test_ledger_sync import FakeWorksheet, row, write_csv_file  # noqa: F401


class TestParseDisplayNumber:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0.04", 0.04),
            ("4%", 0.04),          # percent-formatted rate
            ("13%", 0.13),
            ("1.5%", 0.015),
            ("$3,402.00", 3402.0),  # currency-formatted cost
            ("3,402", 3402.0),      # thousands separator
            ("$1.23", 1.23),
            ("€9.99", 9.99),
            ("(1.23)", -1.23),      # accounting-style negative
            ("($5.00)", -5.0),
            ("-2.5", -2.5),
            ("  7 ", 7.0),
            (42, 42),               # already numeric (an unformatted read)
            (0.04, 0.04),
        ],
    )
    def test_display_formatting_is_seen_through(self, text, expected):
        assert _parse_display_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "   ", "Amex Business Gold", "-", "n/a", "*"])
    def test_non_numbers_are_left_alone(self, text):
        assert _parse_display_number(text) is None

    def test_coerce_keeps_a_non_number_verbatim(self):
        # The undisclosed-split marker: Quantity "*" must survive, not become 0.
        assert _coerce("quantity", "*") == "*"

    def test_coerce_returns_quantity_as_an_int(self):
        assert _coerce("quantity", "3") == 3
        assert isinstance(_coerce("quantity", "3"), int)


class TestFormattedSheetSurvivesARecheck:
    """The end-to-end case: a percent/currency-formatted sheet, then a partial re-check that sends
    blanks for those columns."""

    def _recheck(self, tmp_path, **existing):
        ws = FakeWorksheet(rows=[
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                **existing),
        ])
        path = write_csv_file(
            tmp_path,
            # A tracking-only re-check: no card, no cost — every money column blank.
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                 status="shipped", tracking_number="1Z1"),
        )
        with patch.object(ledger_sync, "_get_worksheet", lambda: ws):
            sync_csv_to_sheet(path)
        return ws.data_rows()[0]

    def test_percent_formatted_rate_stays_numeric(self, tmp_path):
        updated = self._recheck(tmp_path, cashback_rate="4%")

        value = updated[FIELDNAMES.index("cashback_rate")]
        assert value == pytest.approx(0.04)
        assert not isinstance(value, str), "a text rate breaks the Total Profit formula"

    def test_currency_formatted_cost_stays_numeric(self, tmp_path):
        updated = self._recheck(tmp_path, total_cost="$3,402.00")

        value = updated[FIELDNAMES.index("total_cost")]
        assert value == pytest.approx(3402.0)
        assert not isinstance(value, str), "a text cost stops the column summing"

    def test_the_whole_money_block_survives(self, tmp_path):
        updated = self._recheck(
            tmp_path, cost_per_item="$1,134.00", shipping="$0.00", total_cost="$3,402.00",
            cashback_rate="4%", insurance="$4.50", payout_amount="$3,600.00",
        )

        for field, expected in [("cost_per_item", 1134.0), ("shipping", 0.0), ("total_cost", 3402.0),
                                ("cashback_rate", 0.04), ("insurance", 4.5),
                                ("payout_amount", 3600.0)]:
            value = updated[FIELDNAMES.index(field)]
            assert value == pytest.approx(expected), f"{field} came back as {value!r}"

    def test_a_real_incoming_value_still_wins_over_the_formatted_one(self, tmp_path):
        # Preservation is only for blanks; a fresh scrape must still overwrite.
        ws = FakeWorksheet(rows=[
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                cashback_rate="4%"),
        ])
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                 card_last4="4335", card_name="Venmo Visa", cashback_rate="0.13"),
        )
        with patch.object(ledger_sync, "_get_worksheet", lambda: ws):
            sync_csv_to_sheet(path)

        assert ws.data_rows()[0][FIELDNAMES.index("cashback_rate")] == pytest.approx(0.13)


class TestAddressIsOneLine:
    """An agent copies the address block off the page verbatim, so it can arrive with real newlines
    in it — which makes the sheet row tall and ragged. The deterministic parsers already comma-join,
    so normalizing in the model keeps both paths writing the same shape."""

    def _address(self, raw):
        from models.order import OrderItem
        return OrderItem(retailer="Amazon", order_id="A1", order_date="2026-08-11",
                         item_name="W", delivery_address=raw).delivery_address

    def test_newlines_become_comma_separated(self):
        raw = "BuyForMeRetail B999999\nTHIRTEEN Sample Drive\nB999999\nTestville, NH 03050-0000\nUnited States"
        assert self._address(raw) == (
            "BuyForMeRetail B999999, THIRTEEN Sample Drive, B999999, "
            "Testville, NH 03050-0000, United States"
        )

    def test_windows_line_endings_too(self):
        assert self._address("A\r\nB") == "A, B"

    def test_a_line_already_ending_in_a_comma_does_not_double_up(self):
        assert self._address("Name,\nStreet") == "Name, Street"

    def test_blank_lines_are_dropped(self):
        assert self._address("A\n\n\nB\n") == "A, B"

    def test_internal_whitespace_is_collapsed(self):
        assert self._address("123   Main    St") == "123 Main St"

    def test_an_already_flat_address_is_untouched(self):
        flat = "Test Buyer, 100 Reship Rd, Unit C00, Reshipburg DE 19700"
        assert self._address(flat) == flat

    def test_blank_stays_blank(self):
        # A partial re-check sends no address; it must stay blank so _merge_row preserves the old one.
        assert self._address("") == ""

    def test_classification_is_unaffected_by_the_change(self):
        from config.warehouses import classify_address
        from models.warehouse import Jig, Warehouse

        warehouses = [Warehouse(buying_group="BFMR", jigs=[Jig(street="Sample Drive", zip="03050")])]
        multiline = "BuyForMeRetail\nTHIRTEEN Sample Drive\nTestville, NH 03050"
        assert classify_address(multiline, warehouses) == "BFMR"
        assert classify_address(self._address(multiline), warehouses) == "BFMR"
