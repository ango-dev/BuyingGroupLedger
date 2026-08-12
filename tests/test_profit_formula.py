"""The Total Profit cell is a LIVE sheet formula, not a scraped value.

    Total Profit = Payout Amount + Cashback - Total Cost - Shipping - Insurance
    Cashback     = (Total Cost + Shipping) * Cashback Rate

Insurance and Payout Amount are typed in by hand (the BFMR/MaxOutDeals step fills them later), so a
value computed at scrape time would be stale the moment either is entered — and a delivered row is
terminal, never re-scraped, so it would stay stale forever.

These tests share test_ledger_sync's FakeWorksheet + helpers rather than re-deriving them, so the
fake stays a single stand-in for gspread.
"""

from models.order import FIELDNAMES
from sheets import ledger_sync
from sheets.ledger_sync import HEADER, sync_csv_to_sheet

from tests.test_ledger_sync import row, sheet, write_csv_file  # noqa: F401  (sheet is a fixture)


class TestFormulaShape:
    def test_column_letters_are_derived_from_fieldnames_not_hardcoded(self):
        # If this drifts, the formula silently reads the wrong columns and every profit number is
        # wrong while still looking like a number — the whole risk of a positional schema.
        for field, letter in ledger_sync._COL.items():
            index = 0
            for char in letter:
                index = index * 26 + (ord(char) - ord("A") + 1)
            assert FIELDNAMES[index - 1] == field

    def test_col_letter_handles_the_two_letter_rollover(self):
        assert [ledger_sync._col_letter(i) for i in (0, 25, 26, 27)] == ["A", "Z", "AA", "AB"]

    def test_formula_shape_is_pinned(self):
        # Pinned literally so an accidental column insert (which shifts every letter) fails loudly
        # here rather than quietly producing wrong money on the sheet.
        assert ledger_sync._profit_formula(7) == (
            '=IF(X7="","",IFERROR(LET(s,IFERROR(N7*O7/SUMIF($C$2:$C,$C7,$O$2:$O),0),'
            'X7+(O7+s)*U7-O7-s-V7),""))'
        )

    def test_formula_reads_the_intended_columns(self):
        # The self-checking half of the pin above: assert by HEADER NAME, so the intent survives a
        # future append even though the letters would change.
        formula = ledger_sync._profit_formula(7)
        for name in ("Order ID", "Shipping", "Total Cost", "Cashback Rate", "Insurance",
                     "Payout Amount"):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}7" in formula, f"{name} ({letter}) missing from the profit formula"
        # Card and Payout Date are descriptive only — they must NOT appear in the arithmetic.
        for name in ("Card", "Payout Date"):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}7" not in formula, f"{name} should not be part of the profit math"

    def test_shipping_is_prorated_across_an_orders_rows(self):
        # Every retailer repeats the ORDER-level shipping total on each of the order's rows, so
        # subtracting column Shipping as-is would charge a 3-row order three times over (and credit
        # cashback on it three times), making the column's SUM wrong.
        ship = ledger_sync._col_letter(HEADER.index("Shipping"))
        cost = ledger_sync._col_letter(HEADER.index("Total Cost"))
        oid = ledger_sync._col_letter(HEADER.index("Order ID"))
        assert (
            f"{ship}7*{cost}7/SUMIF(${oid}$2:${oid},${oid}7,${cost}$2:${cost})"
            in ledger_sync._profit_formula(7)
        )

    def test_blank_payout_leaves_the_cell_blank(self):
        # Not 0: an un-paid-out row would otherwise show a large fake loss and poison a column sum.
        payout = ledger_sync._col_letter(HEADER.index("Payout Amount"))
        assert ledger_sync._profit_formula(7).startswith(f'=IF({payout}7="","",')


class TestFormulaIsWritten:
    def test_formula_is_written_for_updated_and_appended_rows(self, sheet, tmp_path):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", tracking_number="1Z1"),
            dict(order_id="A1", order_date="2026-08-08", item_name="Gadget", shipment="Shipment 2",
                 status="ordered"),
        )

        sync_csv_to_sheet(path)

        # Row 2 was updated in place, row 3 appended — both need the formula.
        assert set(sheet.profit_formulas()) == {2, 3}
        assert sheet.profit_formulas()[3] == ledger_sync._profit_formula(3)

    def test_written_with_user_entered_so_it_is_a_formula(self, sheet, tmp_path):
        # The data rows are written RAW on purpose (USER_ENTERED would reinterpret a long numeric
        # tracking number into scientific notation). Only this one narrow column may use USER_ENTERED.
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1"),
        )

        sync_csv_to_sheet(path)

        assert sheet.batch_input_options == ["USER_ENTERED"]
        assert all(e["values"][0][0].startswith("=") for e in sheet.batched)

    def test_formula_lands_in_the_total_profit_column(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1"),
        )

        sync_csv_to_sheet(path)

        expected_col = ledger_sync._col_letter(HEADER.index("Total Profit"))
        assert sheet.batched[0]["range"] == f"{expected_col}2"
        assert sheet.data_rows()[0][FIELDNAMES.index("total_profit")].startswith("=")

    def test_nothing_synced_means_no_formula_call(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(tmp_path, dict(order_id="", item_name="Orphan"))  # blank id -> skipped

        sync_csv_to_sheet(path)

        assert sheet.batched == []

    def test_a_failed_formula_write_does_not_lose_the_scraped_row(self, sheet, tmp_path, caplog):
        # The row data is the irreplaceable part; the formula can be re-stamped on the next sync.
        def boom(*args, **kwargs):
            raise RuntimeError("Sheets API down")

        sheet.batch_update = boom
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1"),
        )

        with caplog.at_level("ERROR"):
            sync_csv_to_sheet(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("order_id")] == "A1"
        assert "Total Profit formula" in caplog.text

    def test_stale_evaluated_value_is_replaced_by_the_formula_again(self, sheet, tmp_path):
        # get_all_values() returns a formula cell's EVALUATED text, so _merge_row carries that number
        # forward and the RAW row write would freeze it. The re-stamp is what undoes that.
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                total_profit="41.99"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", tracking_number="1Z1"),
        )

        sync_csv_to_sheet(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("total_profit")] == ledger_sync._profit_formula(2)


class TestProfitColumnsUpsert:
    """Insurance / Payout Date / Payout Amount are typed in by the user, so a re-scrape must not wipe
    them. They ride the same blank-never-overwrites rule that protects item name and cost."""

    def test_hand_entered_values_survive_a_rescrape(self, sheet, tmp_path):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                insurance="4.50", payout_date="2026-08-20", payout_amount="1299.00"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="delivered"),
        )

        sync_csv_to_sheet(path)

        recorded = sheet.data_rows()[0]
        assert recorded[FIELDNAMES.index("insurance")] == 4.50
        assert recorded[FIELDNAMES.index("payout_date")] == "2026-08-20"
        assert recorded[FIELDNAMES.index("payout_amount")] == 1299.00
        assert recorded[FIELDNAMES.index("status")] == "delivered"

    def test_card_columns_are_preserved_on_a_partial_recheck(self, sheet, tmp_path):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                card_name="Freedom", cashback_rate="0.015"),
        ]
        # A partial re-check carries no card_last4, so tag_cards leaves both card columns blank.
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", tracking_number="1Z1"),
        )

        sync_csv_to_sheet(path)

        recorded = sheet.data_rows()[0]
        assert recorded[FIELDNAMES.index("card_name")] == "Freedom"
        assert recorded[FIELDNAMES.index("cashback_rate")] == 0.015

    def test_cashback_rate_is_written_as_a_number_not_text(self, sheet, tmp_path):
        # Text would break the formula's arithmetic (Sheets can't multiply a string).
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 card_name="Freedom", cashback_rate="0.015"),
        )

        sync_csv_to_sheet(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("cashback_rate")] == 0.015
