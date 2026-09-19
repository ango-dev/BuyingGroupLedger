import re
"""The Total Profit cell is a LIVE sheet formula, not a scraped value. Shipping, by contrast, is a
Python-computed NUMBER written once per sync (see TestShippingReproration below) — not a formula.

    Total Profit = Actual Payout + Cashback - Total Cost - Shipping - Insurance
    Cashback     = (Total Cost + Shipping) * Cashback Rate

Insurance and Actual Payout are typed in by hand (the BFMR/MaxOutDeals step fills them later), so a
value computed at scrape time would be stale the moment either is entered — and a delivered row is
terminal, never re-scraped, so it would stay stale forever. Total Profit therefore stays a live
formula. Shipping doesn't have that problem (it depends only on scraped data, which is only ever
current as of the last re-scrape anyway), so its cost-weighted split is computed once in Python by
ledger.sync._reprorate_order_level and written as a plain number — see that function's docstring
for why a live SUMIF-based formula (the original design, briefly a separate "Prorated Shipping"
column) was dropped in favor of this.

These tests share test_ledger_sync's FakeWorksheet + helpers rather than re-deriving them, so the
fake stays a single stand-in for gspread.
"""

from models.order import FIELDNAMES
from ledger import sync as ledger_sync
from ledger.sync import HEADER, sync_csv_to_ledger

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
        # (Expected Payout moved before Actual Payout on 2026-09-18: V->W, X->Y, deliberately)
        # here rather than quietly producing wrong money on the sheet.
        assert ledger_sync._cogs_formula(7) == (
            '=IF(B7="cancelled","",IF(M7="","",IFERROR((M7-Y7*L7-P7+N7+O7-Q7)*(1-S7)+Q7,"")))'
        )
        assert ledger_sync._profit_formula(7) == (
            '=IF(B7="cancelled","",IF(W7="","",IFERROR(W7-T7-U7,"")))'
        )

    def test_profit_is_algebraically_what_it_always_was(self):
        """THE REGRESSION PROOF for splitting COGS out of the profit formula.

        Total Profit used to be `payout + (cost+ship)*rate - cost - ship - insurance`, computed in one
        cell. It is now `payout - COGS - insurance`, where `COGS = (cost+ship)*(1-rate)`. Those are the
        same number for every input — expand the second and you get the first — and this evaluates
        both to prove it rather than asserting it in a comment. If a future edit to either formula
        breaks the identity, every historical profit figure on the sheet silently changes.
        """
        for cost, ship, rate, ins, payout in [
            (798.0, 0.0, 0.04, 0.0, 820.0),
            (1709.91, 12.5, 0.135, 3.86, 1800.0),
            (299.0, 0.0, 0.0, 0.0, 299.0),
            (-399.99, 0.0, 0.05, 0.0, -387.0),   # a return: both sides go negative
            (100.0, 5.0, 0.25, 2.5, 0.0),        # a zero payout is still a real number, not blank
        ]:
            cogs = (cost + ship) * (1 - rate)
            new = payout - cogs - ins
            old = payout + (cost + ship) * rate - cost - ship - ins
            assert round(new, 9) == round(old, 9), (cost, ship, rate, ins, payout)

    def test_a_netted_return_equals_the_old_two_row_bookkeeping(self):
        """Method 2's regression proof. The old sheets booked a partial return as a second negative
        row; the COGS formula now nets `Return Qty x Cost Per Item` out of the original row. For the
        same inputs the single netted row must equal the SUM of the old pair -- the live example is
        3 iPads at 399.99, one returned: 8.23 either way."""
        for qty, unit, ship, rate, ins, paid, clawed, returned in [
            (3, 399.99, 0.0, 0.05, 5.79, 1161.0, 387.0, 1),
            (8, 299.0, 0.0, 0.135, 0.0, 2392.0, 299.0, 1),
            (5, 100.0, 10.0, 0.02, 1.5, 505.0, 202.0, 2),
        ]:
            cost = qty * unit
            old_pair = (paid - (cost + ship) * (1 - rate) - ins) + (-clawed + returned * unit * (1 - rate))
            netted_cogs = (cost - returned * unit + ship) * (1 - rate)
            single = (paid - clawed) - netted_cogs - ins
            assert round(single, 9) == round(old_pair, 9), (qty, unit, returned)

    def test_formula_gift_card_netting_equals_the_old_cost_scaling(self):
        """The regression proof for moving gift-card netting out of the Amazon mappings and into
        the COGS formula. The old `_net_gift_card` scaled every row's
        cost basis by `(basis - gc) / basis` and let `(Total Cost + Shipping) * (1 - rate)` do the
        rest; the formula now subtracts the row's cost-weighted Gift Card share directly. Same
        number, row by row — scaling by cost IS cost-weighted proration."""
        for costs, ship_total, gc_total, rate in [
            ([100.0], 0.0, 40.0, 0.05),               # one row, part-paid by card
            ([100.0, 300.0], 40.0, 50.0, 0.02),       # multi-row: shares must reconcile per row
            ([798.0, 202.0], 0.0, 1000.0, 0.135),     # card covers the whole order -> COGS 0
        ]:
            basis = sum(costs) + ship_total
            factor = max(0.0, basis - gc_total) / basis
            for cost in costs:
                # Both shares use the Total Cost weight _reprorate_order_level applies. The scaled
                # shipping share divides out to the same weight (ship_share is itself proportional
                # to cost), which is exactly why the per-row equivalence is exact, not approximate.
                weight = cost / sum(costs)
                ship_share = ship_total * weight
                gc_share = gc_total * weight
                old = (cost * factor + ship_share * factor) * (1 - rate)
                new = (cost - gc_share + ship_share) * (1 - rate)
                assert round(new, 9) == round(old, 9), (cost, gc_total, rate)

    def test_rewards_used_keep_the_full_cost_but_earn_no_cashback(self):
        """Amazon rewards SPENT on an order. Unlike a gift card they are NOT
        netted out of the cost — the user nets every Amazon reward from COGS at year end outside the
        sheet, so netting here too would count it twice. They only leave the cashback basis: the
        card earns nothing on dollars it never paid. A blank cell computes the old number exactly."""
        cost, rate = 48.28, 0.05
        as_gift_card = (cost - cost) * (1 - rate)                 # the 09-07 treatment: cost 0
        as_rewards = (cost - cost) * (1 - rate) + cost            # the column's treatment: cost 48.28
        assert round(as_rewards, 9) == cost and as_gift_card == 0.0
        # A partial redemption: $13.70 of $15.48 -> cost stays 15.48, cashback only on the $1.78.
        cost, rewards = 15.48, 13.70
        cogs = (cost - rewards) * (1 - rate) + rewards
        assert round(cost - cogs, 9) == round((cost - rewards) * rate, 9)
        # Blank (0) rewards -> identical to the formula before the column existed.
        assert (cost - 0) * (1 - rate) + 0 == cost * (1 - rate)

    def test_the_pre_tax_cap_is_obsolete_now_that_tax_is_recorded(self):
        """The real case the old cap existed for: a $14.04 gift card against a $12.85 order with
        $1.19 tax. The old scheme capped the reduction at the pre-tax basis so cost floored at 0;
        with Sales Tax in the COGS basis the same numbers land at exactly 0 with no cap at all —
        Amazon never lets a gift card exceed the grand total."""
        cost, tax, gc, rate = 12.85, 1.19, 14.04, 0.03
        old_capped = max(0.0, (cost - min(gc, cost)) ) * (1 - rate)   # cap: gc floored at pre-tax basis
        new = (cost - gc + 0.0 + tax) * (1 - rate)
        assert round(new, 9) == 0.0 == round(old_capped, 9)

    def test_a_cancelled_row_reports_no_cost_and_no_profit(self):
        # A cancelled order was refunded, so it must not reach the year-end cost side. Both formulas
        # short-circuit on Status, read through _COL so a reorder can't leave them pointing at the
        # wrong column.
        status = ledger_sync._col_letter(HEADER.index("Status"))
        assert ledger_sync._cogs_formula(7).startswith(f'=IF({status}7="cancelled","",')
        assert ledger_sync._profit_formula(7).startswith(f'=IF({status}7="cancelled","",')

    def test_formula_reads_the_intended_columns(self):
        # The self-checking half of the pin above: assert by HEADER NAME, so the intent survives a
        # future append even though the letters would change.
        profit = ledger_sync._profit_formula(7)
        for name in ("COGS", "Insurance", "Actual Payout", "Status"):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}7" in profit, f"{name} ({letter}) missing from the profit formula"
        # The cost side now lives in COGS, so profit must NOT re-derive it — two copies of the same
        # arithmetic is exactly what would drift.
        def refs(formula):
            return set(re.findall(r"[A-Z]+7", formula))  # whole cell references ("G7" is not in "AG7")

        for name in ("Card", "Payout Date", "Order ID", "Total Cost", "Shipping", "Cashback Rate"):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}7" not in refs(profit), f"{name} should not be part of the profit math"

        cogs = ledger_sync._cogs_formula(7)
        for name in ("Total Cost", "Shipping", "Cashback Rate", "Status", "Return Qty",
                     "Cost Per Item", "Gift Card", "Sales Tax", "Rewards Used"):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}7" in cogs, f"{name} ({letter}) missing from the COGS formula"
        # Insurance is an EXPENSE, not part of the cost of the goods. Order ID: no SUMIF here —
        # Shipping already holds this row's final cost-weighted share by the time this runs.
        for name in ("Insurance", "Actual Payout", "Order ID", "Card"):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}7" not in refs(cogs), f"{name} should not be part of the COGS math"

    def test_blank_payout_leaves_the_cell_blank(self):
        # Not 0: an un-paid-out row would otherwise show a large fake loss and poison a column sum.
        payout = ledger_sync._col_letter(HEADER.index("Actual Payout"))
        assert f'IF({payout}7="","",' in ledger_sync._profit_formula(7)
        # COGS deliberately does NOT gate on payout: the cost was incurred whether or not the buying
        # group has paid yet, and the year-end cost side has to count it.
        assert f'IF({payout}7="","",' not in ledger_sync._cogs_formula(7)


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

        sync_csv_to_ledger(path)

        # Row 2 was updated in place, row 3 appended — both need the formula.
        assert set(sheet.profit_formulas()) == {2, 3}
        assert sheet.profit_formulas()[3] == ledger_sync._profit_formula(3)

    def test_written_with_user_entered_so_it_is_a_formula(self, sheet, tmp_path):
        # The data rows are written RAW on purpose (USER_ENTERED would reinterpret a long numeric
        # tracking number into scientific notation). Only this one narrow column may use USER_ENTERED.
        # (No Shipping figure is sent here, so _reprorate_order_level never fires and never adds a
        # competing RAW batch_update call — see TestShippingReproration for that path.)
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1"),
        )

        sync_csv_to_ledger(path)

        assert sheet.batch_input_options == ["USER_ENTERED"]
        assert all(e["values"][0][0].startswith("=") for e in sheet.batched)

    def test_formula_lands_in_the_total_profit_column(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1"),
        )

        sync_csv_to_ledger(path)

        # BOTH derived columns are stamped, in one batch — Total Profit reads the COGS cell, so
        # stamping one without the other would leave a live formula pointing at a frozen number.
        written = {e["range"] for e in sheet.batched}
        for name, field in (("COGS", "cogs"), ("Total Profit", "total_profit")):
            letter = ledger_sync._col_letter(HEADER.index(name))
            assert f"{letter}2" in written, f"{name} formula was not written"
            assert sheet.data_rows()[0][FIELDNAMES.index(field)].startswith("=")

    def test_nothing_synced_means_no_formula_call(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(tmp_path, dict(order_id="", item_name="Orphan"))  # blank id -> skipped

        sync_csv_to_ledger(path)

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
            sync_csv_to_ledger(path)

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

        sync_csv_to_ledger(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("total_profit")] == ledger_sync._profit_formula(2)


class TestShippingReproration:
    """_reprorate_order_level (ledger/sync.py) rewrites every row of a touched order's Shipping
    cell to its own cost-weighted share of the order's raw shipping total — replacing the raw
    order-level number every scraper/agent emits, in place, as a plain number (not a formula)."""

    def test_single_row_order_keeps_the_full_shipping_total(self, sheet, tmp_path):
        # One row IS the whole order, so its "share" is 100% of the total — just coerced to a real
        # number rather than left as the text a RAW write would otherwise store.
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="19.99"),
        )

        sync_csv_to_ledger(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("shipping")] == 19.99

    def test_multi_row_order_splits_by_total_cost_not_evenly(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="40.00"),
            dict(order_id="A1", order_date="2026-08-08", item_name="Gadget", shipment="Shipment 2",
                 quantity="1", cost_per_item="300.00", total_cost="300.00", shipping="40.00"),
        )

        sync_csv_to_ledger(path)

        rows = sheet.data_rows()
        assert rows[0][FIELDNAMES.index("shipping")] == 10.0   # 40 * 100/400
        assert rows[1][FIELDNAMES.index("shipping")] == 30.0   # 40 * 300/400

    def test_a_sibling_row_not_in_this_syncs_csv_is_still_reprorated(self, sheet, tmp_path):
        """The classic Best Buy undisclosed-split case: only the NEW box shows up in a given sync
        (the retailer surfaces one rotating tracking number at a time), but the order's ORIGINAL row
        must still be re-split now that a second box is known — not left at its old (now wrong) full
        total. _reprorate_order_level re-derives from EVERY row of the order currently on the sheet, not
        just the ones this sync's CSV happened to include."""
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="40.00"),
        ]
        # This sync only ever mentions the NEW second box — row 2 above is never in this CSV.
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Gadget", shipment="Shipment 2",
                 quantity="1", cost_per_item="300.00", total_cost="300.00", shipping="40.00"),
        )

        sync_csv_to_ledger(path)

        rows = sheet.data_rows()
        assert rows[0][FIELDNAMES.index("shipping")] == 10.0   # the untouched sibling, re-split
        assert rows[1][FIELDNAMES.index("shipping")] == 30.0   # the newly appended box

    def test_a_partial_recheck_with_no_shipping_figure_leaves_the_split_alone(self, sheet, tmp_path):
        """A tracking-only re-check sends blank shipping (nothing new to report). With no raw total
        to re-derive from this sync, the order's already-correct split must be left exactly as is —
        not zeroed out or collapsed onto the one touched row."""
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="10.0"),
            row(order_id="A1", order_date="2026-08-08", item_name="Gadget", shipment="2",
                quantity="1", cost_per_item="300.00", total_cost="300.00", shipping="30.0"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", tracking_number="1Z1"),  # no shipping/cost figures at all
        )

        sync_csv_to_ledger(path)

        rows = sheet.data_rows()
        assert rows[0][FIELDNAMES.index("shipping")] == "10.0"
        assert rows[1][FIELDNAMES.index("shipping")] == "30.0"

    def test_an_order_with_zero_total_cost_everywhere_splits_to_zero_not_an_error(self, sheet, tmp_path):
        # Every row still "ordered" (no cost yet) -> the SUMIF-equivalent denominator is 0; guard
        # against a ZeroDivisionError rather than letting the sync crash on an in-progress order.
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 shipping="12.00"),
        )

        sync_csv_to_ledger(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("shipping")] == 0.0

    def test_gift_card_and_sales_tax_split_by_total_cost_like_shipping(self, sheet, tmp_path):
        """The two 2026-08-30 columns ride the same order-level contract as Shipping: every row of
        an order arrives carrying the order TOTAL, and each cell ends up holding that row's
        cost-weighted share — which is what the COGS formula reads."""
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="40.00",
                 gift_card="50.00", sales_tax="8.00"),
            dict(order_id="A1", order_date="2026-08-08", item_name="Gadget", shipment="Shipment 2",
                 quantity="1", cost_per_item="300.00", total_cost="300.00", shipping="40.00",
                 gift_card="50.00", sales_tax="8.00"),
        )

        sync_csv_to_ledger(path)

        rows = sheet.data_rows()
        assert rows[0][FIELDNAMES.index("gift_card")] == 12.5   # 50 * 100/400
        assert rows[1][FIELDNAMES.index("gift_card")] == 37.5   # 50 * 300/400
        assert rows[0][FIELDNAMES.index("sales_tax")] == 2.0    # 8 * 100/400
        assert rows[1][FIELDNAMES.index("sales_tax")] == 6.0    # 8 * 300/400

    def test_a_sync_reporting_only_some_order_level_fields_leaves_the_others_alone(self, sheet, tmp_path):
        """Each order-level field skips independently: a re-check that reports shipping but no
        gift-card figure must not zero out a Gift Card split a previous sync already wrote."""
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="40.00",
                gift_card="12.5", sales_tax="2.0"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                 quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="40.00"),
        )

        sync_csv_to_ledger(path)

        assert len(sheet.data_rows()) == 1, "the re-check must match the existing row, not append"
        r = sheet.data_rows()[0]
        assert r[FIELDNAMES.index("shipping")] == 40.0
        # Untouched by this sync's reprorate (no figure sent) — carried through the row rewrite as
        # the number the merge coerces it to, not zeroed and not re-split.
        assert r[FIELDNAMES.index("gift_card")] == 12.5
        assert r[FIELDNAMES.index("sales_tax")] == 2.0

    def test_reprorate_writes_raw_not_user_entered(self, sheet, tmp_path):
        # Shipping is a plain number, never a formula — USER_ENTERED would risk Sheets reinterpreting
        # it (harmless for a currency amount, but RAW is still the correct, deliberate choice here).
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 quantity="1", cost_per_item="100.00", total_cost="100.00", shipping="19.99"),
        )

        sync_csv_to_ledger(path)

        assert "RAW" in sheet.batch_input_options


class TestProfitColumnsUpsert:
    """Insurance / Payout Date / Actual Payout are typed in by the user, so a re-scrape must not wipe
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

        sync_csv_to_ledger(path)

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

        sync_csv_to_ledger(path)

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

        sync_csv_to_ledger(path)

        assert sheet.data_rows()[0][FIELDNAMES.index("cashback_rate")] == 0.015
