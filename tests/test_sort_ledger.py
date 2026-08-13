"""Newest-first ordering of the ledger.

Two things make this more than a cosmetic sort, and both are what these tests actually guard:

1. **Rows may only move AFTER sync_csv_to_sheet has finished writing.** Sync caches each matched row's
   NUMBER from its pre-sync snapshot and writes updates to `A{row_number}`; reordering while those are
   in flight would put every update on the wrong row, silently. Hence sorting is a separate step, and
   appends still go to the bottom (a sort is a total ordering, so where a row is inserted can't change
   where it ends up).

2. **The Total Profit formula is position-bound.** It uses same-row relative references, so a row that
   moves needs the formula for its NEW position. scripts/audit_sheet.py's check_profit_formula_literal
   fails any row whose stored formula isn't exactly _profit_formula(row_number) — that's the live
   tripwire, and `test_every_moved_row_gets_the_formula_for_its_new_position` is its offline twin.
"""

import pytest

from models.order import FIELDNAMES
from sheets import ledger_sync
from sheets.ledger_sync import HEADER, _profit_formula, sort_ledger_by_date_desc

from tests.test_ledger_sync import FakeWorksheet, row, sheet, write_csv_file  # noqa: F401


def seeded(**values):
    """A full-width row with sane defaults, so tests only state what they're about."""
    base = dict(retailer="Best Buy", item_name="Widget", shipment=1, status="ordered")
    base.update(values)
    out = row(**{k: v for k, v in base.items() if k != "shipment"})
    out[FIELDNAMES.index("shipment")] = base["shipment"]  # int, as the sheet stores it
    return out


def dates_on(ws):
    i = FIELDNAMES.index("order_date")
    return [r[i] for r in ws.data_rows()]


class TestOrdering:
    def test_rows_end_up_newest_first(self, sheet):
        sheet.rows = [
            list(HEADER),
            seeded(order_id="A", order_date="2026-08-02"),
            seeded(order_id="B", order_date="2026-08-11"),
            seeded(order_id="C", order_date="2026-08-06"),
        ]

        sort_ledger_by_date_desc(sheet)

        assert dates_on(sheet) == ["2026-08-11", "2026-08-06", "2026-08-02"]

    def test_an_orders_shipments_stay_together_and_in_order(self, sheet):
        # The reason Order ID + Shipment are tie-breakers: without them these three rows could be
        # scattered among the other same-day orders.
        sheet.rows = [
            list(HEADER),
            seeded(order_id="BBY-1", order_date="2026-08-10", shipment=3),
            seeded(order_id="OTHER", order_date="2026-08-10"),
            seeded(order_id="BBY-1", order_date="2026-08-10", shipment=1),
            seeded(order_id="BBY-1", order_date="2026-08-10", shipment=2),
        ]

        sort_ledger_by_date_desc(sheet)

        oid, ship = FIELDNAMES.index("order_id"), FIELDNAMES.index("shipment")
        assert [(r[oid], r[ship]) for r in sheet.data_rows()] == [
            ("BBY-1", 1), ("BBY-1", 2), ("BBY-1", 3), ("OTHER", 1),
        ]

    def test_iso_dates_sort_chronologically_as_text(self, sheet):
        # Order Date is plain ISO text (this is why the date columns were kept as text) — lexicographic
        # order IS chronological order, including across month/year boundaries.
        sheet.rows = [list(HEADER)] + [
            seeded(order_id=str(n), order_date=d)
            for n, d in enumerate(["2025-12-31", "2026-01-01", "2026-08-09", "2026-08-10"])
        ]

        sort_ledger_by_date_desc(sheet)

        assert dates_on(sheet) == ["2026-08-10", "2026-08-09", "2026-01-01", "2025-12-31"]

    def test_sorting_is_idempotent(self, sheet):
        sheet.rows = [
            list(HEADER),
            seeded(order_id="A", order_date="2026-08-02"),
            seeded(order_id="B", order_date="2026-08-11"),
        ]

        sort_ledger_by_date_desc(sheet)
        first = [list(r) for r in sheet.data_rows()]
        sort_ledger_by_date_desc(sheet)

        assert [list(r) for r in sheet.data_rows()] == first

    def test_the_sort_targets_the_right_columns_and_range(self, sheet):
        sheet.rows = [
            list(HEADER),
            seeded(order_id="A", order_date="2026-08-02"),
            seeded(order_id="B", order_date="2026-08-11"),
        ]

        sort_ledger_by_date_desc(sheet)

        call = sheet.sort_calls[-1]
        assert call["specs"] == (
            (HEADER.index("Order Date") + 1, "des"),
            (HEADER.index("Order ID") + 1, "asc"),
            (HEADER.index("Shipment") + 1, "asc"),
        )
        # An EXPLICIT range, ending at the last DATA row — an unranged sort would drag the sheet's
        # trailing blank rows through the data block.
        assert call["range"] == f"A2:{ledger_sync._col_letter(len(HEADER) - 1)}3"


class TestFormulasFollowTheRows:
    def test_every_moved_row_gets_the_formula_for_its_new_position(self, sheet):
        # The load-bearing assertion. _profit_formula emits same-row refs, so after a reorder each row
        # must carry the formula for the row it now OCCUPIES, not the one it came from.
        sheet.rows = [
            list(HEADER),
            seeded(order_id="A", order_date="2026-08-02"),
            seeded(order_id="B", order_date="2026-08-11"),
            seeded(order_id="C", order_date="2026-08-06"),
        ]

        sort_ledger_by_date_desc(sheet)

        tp = FIELDNAMES.index("total_profit")
        for offset, r in enumerate(sheet.data_rows()):
            row_number = offset + 2
            assert r[tp] == _profit_formula(row_number)

    def test_formulas_are_written_user_entered(self, sheet):
        # Anything else stores the formula as literal text.
        sheet.rows = [
            list(HEADER),
            seeded(order_id="A", order_date="2026-08-02"),
            seeded(order_id="B", order_date="2026-08-11"),
        ]

        sort_ledger_by_date_desc(sheet)

        assert sheet.batch_input_options[-1] == "USER_ENTERED"


class TestGuardsAndNoOps:
    def test_a_misordered_header_is_refused(self, sheet):
        # Sorting addresses columns positionally, so a differently-ordered sheet would sort the wrong
        # ones. Same posture as sync_csv_to_sheet's guard: refuse rather than corrupt.
        sheet.rows = [list(reversed(HEADER)), seeded(order_id="A", order_date="2026-08-02")]

        with pytest.raises(RuntimeError, match="different ORDER"):
            sort_ledger_by_date_desc(sheet)

        assert sheet.sort_calls == []

    def test_empty_sheet_is_a_no_op(self, sheet):
        sheet.rows = [list(HEADER)]

        result = sort_ledger_by_date_desc(sheet)

        assert result["already_sorted"] is True
        assert sheet.sort_calls == []

    def test_a_single_row_is_not_sorted(self, sheet):
        # Nothing to reorder — and skipping avoids a pointless API round trip.
        sheet.rows = [list(HEADER), seeded(order_id="A", order_date="2026-08-02")]

        result = sort_ledger_by_date_desc(sheet)

        assert result == {"sorted_rows": 1, "already_sorted": True}
        assert sheet.sort_calls == []

    def test_blank_order_id_rows_are_not_counted_as_data(self, sheet):
        # Matches sync_csv_to_sheet's rule: a row with no Order ID isn't a ledger row. It must not
        # extend the sort range, or the range would cover trailing junk.
        sheet.rows = [
            list(HEADER),
            seeded(order_id="A", order_date="2026-08-02"),
            seeded(order_id="B", order_date="2026-08-11"),
            [""] * len(HEADER),
        ]

        sort_ledger_by_date_desc(sheet)

        assert sheet.sort_calls[-1]["range"].endswith("3")


class TestSyncReportsWhatItDid:
    """main.run_scrape only re-sorts when rows were APPENDED — an update rewrites a row in place and
    can't change the order, so the common re-check run skips the sort entirely."""

    def test_an_append_is_reported(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1"),
        )

        result = ledger_sync.sync_csv_to_sheet(path)

        assert result["appended"] == 1 and result["updated"] == 0

    def test_an_update_only_sync_reports_no_appends(self, sheet, tmp_path):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="1",
                 status="shipped", tracking_number="1Z1"),
        )

        result = ledger_sync.sync_csv_to_sheet(path)

        assert result["appended"] == 0 and result["updated"] == 1


class TestDryRunPreviewMatchesReality:
    """scripts/sort_ledger.py shows the resulting order before you --apply. A preview that disagreed
    with the real sort would be worse than no preview, so it's pinned against the actual outcome."""

    def _rows(self):
        return [
            seeded(order_id="BBY-1", order_date="2026-08-10", shipment=2),
            seeded(order_id="AAA-9", order_date="2026-08-02"),
            seeded(order_id="BBY-1", order_date="2026-08-10", shipment=1),
            seeded(order_id="ZZZ-1", order_date="2026-08-11"),
        ]

    def test_preview_order_equals_what_the_sort_actually_does(self, sheet):
        from scripts.sort_ledger import plan_sort

        rows = self._rows()
        preview = plan_sort(list(HEADER), [list(r) for r in rows])["ordered"]

        sheet.rows = [list(HEADER)] + [list(r) for r in rows]
        sort_ledger_by_date_desc(sheet)

        # Compare the ORDER, not whole rows: the real sort also re-stamps Total Profit, so the rows
        # legitimately differ in that one cell. Identity here is (date, order id, shipment).
        def identity(rs):
            date_i, oid_i, ship_i = (FIELDNAMES.index(f) for f in
                                     ("order_date", "order_id", "shipment"))
            return [(r[date_i], r[oid_i], r[ship_i]) for r in rs]

        assert identity(preview) == identity(sheet.data_rows())

    def test_preview_flags_an_already_sorted_sheet(self, sheet):
        from scripts.sort_ledger import plan_sort

        rows = self._rows()
        ordered = plan_sort(list(HEADER), [list(r) for r in rows])["ordered"]

        assert plan_sort(list(HEADER), ordered)["already_sorted"] is True

    def test_preview_excludes_blank_order_id_rows(self, sheet):
        from scripts.sort_ledger import plan_sort

        plan = plan_sort(list(HEADER), self._rows() + [[""] * len(HEADER)])

        assert len(plan["ordered"]) == 4
        assert plan["non_ledger_rows"] == 1


class TestRunScrapeTriggersTheSort:
    """The wiring in main.run_scrape. Covered here because a live run can't reliably exercise it: it
    only fires when a scrape APPENDS, and a routine re-check run is all updates (every live validation
    this was built against reported '0 appended')."""

    def _run(self, monkeypatch, sync_result, sort_raises=False):
        import main

        calls = {"sorted": 0, "alerts": []}

        def fake_sort():
            calls["sorted"] += 1
            if sort_raises:
                raise RuntimeError("sheets API exploded")

        monkeypatch.setattr(main, "sync_csv_to_sheet", lambda path: sync_result)
        monkeypatch.setattr(main, "sort_ledger_by_date_desc", fake_sort)
        monkeypatch.setattr(main, "write_csv", lambda items: __import__("pathlib").Path("orders.csv"))
        monkeypatch.setattr(main, "_classify_and_drop_personal", lambda items, label: items)
        monkeypatch.setattr(main, "_tag_cards", lambda items, label: None)
        monkeypatch.setattr(main, "alert", lambda s, b: calls["alerts"].append(s))

        class FakeScraper:
            retailer_name = "Amazon"

            class profile:
                label = "profile-1"

            def scrape(self):
                return [object()]

        main.run_scrape(FakeScraper())
        return calls

    def test_an_append_triggers_the_sort(self, monkeypatch):
        calls = self._run(monkeypatch, {"updated": 0, "appended": 1})

        assert calls["sorted"] == 1

    def test_an_update_only_sync_does_not_sort(self, monkeypatch):
        # The whole point of the condition: a re-check run rewrites rows in place and can't reorder
        # anything, so it shouldn't pay a full re-stamp of every formula.
        calls = self._run(monkeypatch, {"updated": 7, "appended": 0})

        assert calls["sorted"] == 0

    def test_a_sort_failure_alerts_but_does_not_lose_the_scrape(self, monkeypatch):
        # The rows are already written by the time the sort runs; failing loudly here would turn a
        # cosmetic problem into a lost run.
        calls = self._run(monkeypatch, {"updated": 0, "appended": 2}, sort_raises=True)

        assert calls["sorted"] == 1
        assert any("sort failed" in a.lower() for a in calls["alerts"])
