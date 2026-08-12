"""Sheet DISPLAY formatting must never corrupt the value underneath.

The round trip that makes this a real bug rather than a theoretical one:
`get_all_values()` returns FORMATTED text -> `_merge_row` preserves an existing cell whenever the
incoming value is blank (exactly what a partial re-check sends) -> the preserved value is written
straight back. So a percent-formatted Cashback Rate read as "4%" used to be rewritten as literal TEXT,
which breaks the Total Profit formula's arithmetic; a currency-formatted Total Cost as "$3,402.00" would
stop the column summing. Formatting a column is a readability choice a user makes in the sheet, and it
must survive a re-check untouched.
"""

from unittest.mock import patch

import pytest

from models.order import FIELDNAMES
from sheets import ledger_sync
from sheets.ledger_sync import (
    HEADER,
    _coerce,
    _parse_display_number,
    load_order_state,
    sheet_date_to_iso,
    sync_csv_to_sheet,
)

from tests.test_ledger_sync import FakeWorksheet, row, write_csv_file  # noqa: F401

# 2026-08-06 as Google Sheets stores a real date: days since 1899-12-30. This is what an UNFORMATTED
# read returns once the user formats the Order Date column as a Date (which converts the text).
SERIAL_2026_08_06 = 46240


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


class TestSheetDateToIso:
    def test_serial_becomes_iso(self):
        assert sheet_date_to_iso(SERIAL_2026_08_06) == "2026-08-06"

    def test_iso_text_passes_through(self):
        assert sheet_date_to_iso("2026-08-06") == "2026-08-06"

    def test_us_display_text_is_parsed_as_a_fallback(self):
        # Only reached if something reads FORMATTED; serials are preferred because "8/6/2026" is
        # genuinely ambiguous across locales.
        assert sheet_date_to_iso("8/6/2026") == "2026-08-06"

    def test_unrecognized_text_is_returned_unchanged(self):
        # Degrade to plain string comparison rather than losing the value.
        assert sheet_date_to_iso("sometime") == "sometime"

    def test_float_serial_is_handled(self):
        assert sheet_date_to_iso(float(SERIAL_2026_08_06)) == "2026-08-06"


class TestDateFormattedColumnStillMatches:
    """The bug this prevents: formatting Order Date as a Date converts the stored ISO text into a real
    date. Order Date is part of the UPSERT KEY, so the key then misses and a row with no tracking
    number to reconcile against APPENDS A DUPLICATE instead of updating."""

    def _sheet_with_date_serial(self, **extra):
        """A sheet row whose Order Date cell is a real date (a serial), as an unformatted read sees it."""
        seeded = row(order_id="A1", item_name="Widget", shipment="1", status="ordered", **extra)
        seeded[FIELDNAMES.index("order_date")] = SERIAL_2026_08_06
        return FakeWorksheet(rows=[list(HEADER), seeded])

    def test_row_with_no_tracking_number_updates_instead_of_duplicating(self, tmp_path):
        ws = self._sheet_with_date_serial()
        path = write_csv_file(
            tmp_path,
            # A scraper always emits ISO, never the sheet's display form.
            dict(order_id="A1", order_date="2026-08-06", item_name="Widget", shipment="1",
                 status="cancelled"),
        )

        with patch.object(ledger_sync, "_get_worksheet", lambda: ws):
            sync_csv_to_sheet(path)

        assert len(ws.data_rows()) == 1, "a Date-formatted Order Date must not append a duplicate"
        assert ws.data_rows()[0][FIELDNAMES.index("status")] == "cancelled"

    def test_the_date_cell_stays_a_date_and_is_not_rewritten_as_text(self, tmp_path):
        # Otherwise every sync would silently strip the user's date formatting.
        ws = self._sheet_with_date_serial()
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-06", item_name="Widget", shipment="1",
                 status="shipped", tracking_number="1Z1"),
        )

        with patch.object(ledger_sync, "_get_worksheet", lambda: ws):
            sync_csv_to_sheet(path)

        assert ws.data_rows()[0][FIELDNAMES.index("order_date")] == SERIAL_2026_08_06

    def test_a_genuinely_different_date_still_overwrites(self, tmp_path):
        ws = self._sheet_with_date_serial()
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-09", item_name="Widget", shipment="1"),
        )

        with patch.object(ledger_sync, "_get_worksheet", lambda: ws):
            sync_csv_to_sheet(path)

        dates = [r[FIELDNAMES.index("order_date")] for r in ws.data_rows()]
        assert SERIAL_2026_08_06 not in dates or "2026-08-09" in dates

    def test_load_order_state_reports_an_iso_date(self, tmp_path, monkeypatch):
        # `since` trimming compares against ISO, and the date is handed to the agent as the order's.
        ws = self._sheet_with_date_serial()
        monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: ws)

        state = load_order_state()

        assert state["open_orders"][0]["order_date"] == "2026-08-06"

    def test_since_window_still_trims_correctly_with_date_cells(self, tmp_path, monkeypatch):
        ws = FakeWorksheet(rows=[list(HEADER)])
        base = row(order_id="A1", item_name="W", shipment="1", status="delivered")
        base[FIELDNAMES.index("order_date")] = SERIAL_2026_08_06
        ws.rows.append(base)
        monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: ws)

        assert load_order_state(since="2026-08-01")["delivered_ids"] == ["A1"]
        assert load_order_state(since="2026-09-01")["delivered_ids"] == []
