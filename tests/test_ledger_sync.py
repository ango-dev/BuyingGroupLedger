"""Upsert and order-state logic, exercised against an in-memory fake worksheet.

_get_worksheet() is the single seam where this module talks to Google Sheets, so patching it is
enough to test everything else offline — no credentials, no network, no live sheet.
"""

import csv

import pytest

from models.order import FIELDNAMES
from models.warehouse import Jig, Warehouse
from sheets import ledger_sync
from sheets.ledger_sync import (
    HEADER,
    _merge_row,
    load_order_state,
    plan_buying_group_retag,
    sync_csv_to_sheet,
)


class FakeWorksheet:
    """Minimal stand-in for gspread.Worksheet covering only what ledger_sync calls."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]
        self.update_calls = 0
        # Every {"range": ..., "values": ...} dict passed to batch_update, so tests can assert on the
        # Total Profit formulas without them also having to land in self.rows.
        self.batched: list[dict] = []
        self.batch_input_options: list = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, range_name, values):
        # Real gspread writes the whole 2D `values` block starting at the range's top-left cell, so a
        # multi-row block lands on consecutive rows (that's how ledger_sync now appends).
        start = int(range_name.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        for offset, value in enumerate(values):
            idx = start - 1 + offset
            while len(self.rows) <= idx:
                self.rows.append([])
            self.rows[idx] = list(value)
        self.update_calls += 1

    def batch_update(self, data, value_input_option=None):
        # Real gspread writes each {"range", "values"} entry independently. ledger_sync only ever
        # sends single-cell ranges here (the Total Profit formula), so mirror that narrowly and
        # record the value_input_option — USER_ENTERED is what makes a formula a formula.
        self.batch_input_options.append(value_input_option)
        for entry in data:
            self.batched.append(entry)
            range_name = entry["range"]
            column = "".join(c for c in range_name if c.isalpha())
            row_index = int("".join(c for c in range_name if c.isdigit())) - 1
            col_index = 0
            for char in column:
                col_index = col_index * 26 + (ord(char) - ord("A") + 1)
            col_index -= 1
            while len(self.rows) <= row_index:
                self.rows.append([])
            row = self.rows[row_index]
            while len(row) <= col_index:
                row.append("")
            row[col_index] = entry["values"][0][0]

    def profit_formulas(self):
        """{row_number: formula} for every Total Profit cell written this sync."""
        return {
            int("".join(c for c in e["range"] if c.isdigit())): e["values"][0][0]
            for e in self.batched
        }

    def append_rows(self, rows):
        self.rows.extend([list(r) for r in rows])

    def append_row(self, row):
        self.rows.append(list(row))

    def data_rows(self):
        return self.rows[1:]


@pytest.fixture
def sheet(monkeypatch):
    """Install a fake worksheet; the test seeds it via .rows before calling into ledger_sync."""
    ws = FakeWorksheet()
    monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: ws)
    return ws


def row(**values):
    """Build a full-width sheet row from snake_case field names."""
    return [str(values.get(f, "")) for f in FIELDNAMES]


def write_csv_file(tmp_path, *records):
    path = tmp_path / "orders.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for record in records:
            writer.writerow({f: record.get(f, "") for f in FIELDNAMES})
    return path


class TestMergeRow:
    def test_blank_new_value_keeps_existing(self):
        # The whole point: a tracking-only re-check must not wipe cost/address/item name.
        assert _merge_row(["Amazon", "189.99"], ["Amazon", ""]) == ["Amazon", "189.99"]

    def test_real_new_value_overwrites(self):
        assert _merge_row(["ordered", ""], ["shipped", "1Z999"]) == ["shipped", "1Z999"]

    def test_blank_over_blank_stays_blank(self):
        assert _merge_row(["", ""], ["", ""]) == ["", ""]

    def test_whitespace_counts_as_blank(self):
        assert _merge_row(["kept"], ["   "]) == ["kept"]

    def test_short_existing_row_is_padded(self):
        # Pre-migration rows are shorter than the header; missing cells read as "".
        assert _merge_row(["a"], ["a", "new"]) == ["a", "new"]


class TestSyncUpsert:
    def test_new_row_is_appended(self, sheet, tmp_path):
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget",
                 shipment="Shipment 1", status="ordered"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 1
        assert sheet.data_rows()[0][FIELDNAMES.index("status")] == "ordered"

    def test_blank_order_id_record_is_skipped_not_orphaned(self, sheet, tmp_path):
        # A record with no Order ID can't form a valid upsert key, so writing it would append a
        # permanent orphan/duplicate (observed: the agent dropped order_id on one shipment entry).
        # It must be skipped; a valid record in the same sync still lands.
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(retailer="Best Buy", order_id="", order_date="2026-08-10", item_name="Laptop",
                 shipment="Shipment 3", status="shipped", tracking_number="TRK1"),
            dict(retailer="Best Buy", order_id="B1", order_date="2026-08-10", item_name="Laptop",
                 shipment="Shipment 3", status="shipped", tracking_number="TRK1"),
        )

        sync_csv_to_sheet(path)

        rows = sheet.data_rows()
        assert len(rows) == 1, "the blank-Order-ID record must not create a row"
        assert rows[0][FIELDNAMES.index("order_id")] == "B1"

    def test_recheck_updates_in_place_without_clobbering(self, sheet, tmp_path):
        sheet.rows = [
            list(HEADER),
            row(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget",
                shipment="Shipment 1", status="ordered", cost_per_item="189.99",
                delivery_address="123 Main St"),
        ]
        # A JOB 2 re-check: only status/tracking filled, everything else blank.
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", tracking_number="1Z999"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 1, "re-check must update, not append a duplicate"
        updated = sheet.data_rows()[0]
        assert updated[FIELDNAMES.index("status")] == "shipped"
        assert updated[FIELDNAMES.index("tracking_number")] == "1Z999"
        # Preserved and re-coerced back to a number (not the string "189.99"), so Sheets stores it
        # numerically rather than as apostrophe-prefixed text.
        assert updated[FIELDNAMES.index("cost_per_item")] == 189.99
        assert updated[FIELDNAMES.index("delivery_address")] == "123 Main St"

    def test_same_item_in_two_shipments_are_two_rows(self, sheet, tmp_path):
        """The reason Shipment is in the key at all."""
        sheet.rows = [list(HEADER)]
        common = dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget")
        path = write_csv_file(
            tmp_path,
            dict(**common, shipment="Shipment 1", status="shipped"),
            dict(**common, shipment="Shipment 2", status="ordered"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 2

    def test_differing_shipment_label_would_duplicate(self, sheet, tmp_path):
        """Why 0c matters: an inconsistent label on re-check appends instead of updating.

        This pins the failure mode the Best Buy prompt fix prevents.
        """
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment One"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 2, (
            "differing shipment labels produce two rows — this is the duplicate-row bug, pinned "
            "here so the prompt fix that avoids it stays honest"
        )

    def test_divergent_item_name_defers_to_existing_row(self, sheet, tmp_path):
        """The agent-fallback vs ss-api divergence: same order+date+shipment recorded under a
        differently-worded name must UPDATE the existing row (keeping its name), not duplicate."""
        sheet.rows = [
            list(HEADER),
            row(order_id="B1", order_date="2026-08-10", item_name="ASUS  Vivobook 15 156 FHD",
                shipment="Shipment 1", status="shipped", tracking_number="TRK1", cost_per_item="299.99"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", item_name='ASUS - Vivobook 15 15.6" FHD',
                 shipment="Shipment 1", status="delivered", delivery_date="2026-08-12"),
        )

        sync_csv_to_sheet(path)

        rows = sheet.data_rows()
        assert len(rows) == 1, "a divergent-name row must update in place, not append a duplicate"
        r = rows[0]
        assert r[FIELDNAMES.index("item_name")] == "ASUS  Vivobook 15 156 FHD", "keeps recorded name"
        assert r[FIELDNAMES.index("status")] == "delivered"
        assert r[FIELDNAMES.index("delivery_date")] == "2026-08-12"
        assert r[FIELDNAMES.index("tracking_number")] == "TRK1", "prior tracking preserved"
        assert r[FIELDNAMES.index("cost_per_item")] == 299.99

    def test_ambiguous_existing_shipment_line_is_not_reconciled(self, sheet, tmp_path):
        # Two existing rows already share the shipment line -> can't tell which to update, so a
        # divergent incoming row appends rather than mis-merging onto one of them.
        common = dict(order_id="B1", order_date="2026-08-10", shipment="Shipment 1", status="shipped")
        sheet.rows = [list(HEADER), row(item_name="Name A", **common), row(item_name="Name B", **common)]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", shipment="Shipment 1", item_name="Name C",
                 status="delivered"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 3, "ambiguous shipment line must not be reconciled"

    def test_two_incoming_for_one_shipment_line_do_not_over_reconcile(self, sheet, tmp_path):
        # Two distinct products in one shipment (two incoming rows, same shipment line) must not both
        # collapse onto a single existing row.
        sheet.rows = [
            list(HEADER),
            row(order_id="B1", order_date="2026-08-10", item_name="Existing", shipment="Shipment 1",
                status="shipped"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", item_name="Prod A", shipment="Shipment 1",
                 status="shipped"),
            dict(order_id="B1", order_date="2026-08-10", item_name="Prod B", shipment="Shipment 1",
                 status="shipped"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 3, "ambiguous (2 incoming) shipment line must not reconcile"

    def test_divergent_shipment_number_reconciles_by_tracking(self, sheet, tmp_path):
        """Costco agent-vs-API: the two paths disagree on BOTH the Shipment number and the item name
        but read the SAME tracking number. An incoming row uniquely sharing (Order ID, Tracking Number)
        with one existing row updates it in place — keeping the recorded name AND shipment — instead of
        appending a divergent duplicate. This is the tracking-based deferral tier."""
        sheet.rows = [
            list(HEADER),
            row(order_id="C1", order_date="2026-06-23", item_name="Watch (Item #1847785)",
                shipment="Shipment 1", status="shipped", tracking_number="TRK1", cost_per_item="309.99"),
        ]
        # Agent re-check: same physical box (TRK1), but numbered Shipment 2 and named from page text.
        path = write_csv_file(
            tmp_path,
            dict(order_id="C1", order_date="2026-06-23", item_name="Apple Watch Series 11",
                 shipment="Shipment 2", status="delivered", tracking_number="TRK1",
                 delivery_date="2026-06-30"),
        )

        sync_csv_to_sheet(path)

        rows = sheet.data_rows()
        assert len(rows) == 1, "same tracking number must reconcile, not duplicate"
        r = rows[0]
        assert r[FIELDNAMES.index("item_name")] == "Watch (Item #1847785)", "keeps recorded (API) name"
        assert r[FIELDNAMES.index("shipment")] == "Shipment 1", "keeps recorded (API) shipment number"
        assert r[FIELDNAMES.index("status")] == "delivered", "status advances"
        assert r[FIELDNAMES.index("delivery_date")] == "2026-06-30"
        assert r[FIELDNAMES.index("cost_per_item")] == 309.99, "recorded cost preserved"

    def test_swapped_shipment_numbers_reconcile_by_tracking_not_by_number(self, sheet, tmp_path):
        """The critical Costco case: a 2-box order where the agent numbers the boxes in the OPPOSITE
        order from the API (top-to-bottom vs tracking-sort). Reconciliation must match each incoming
        row to its TRACKING-matched box, never to the row that merely shares the (swapped) Shipment
        number — otherwise the two boxes' tracking numbers cross. Tracking is tried before shipment."""
        api = dict(order_id="C1", order_date="2026-06-24", item_name="Dell (Item #1953694)",
                   cost_per_item="899.99", status="shipped")
        sheet.rows = [
            list(HEADER),
            row(shipment="Shipment 1", tracking_number="TRK_A", **api),
            row(shipment="Shipment 2", tracking_number="TRK_B", **api),
        ]
        # Agent re-check: same two boxes, but Shipment numbers swapped and a page-title name.
        path = write_csv_file(
            tmp_path,
            dict(order_id="C1", order_date="2026-06-24", item_name="Dell All-in-One",
                 shipment="Shipment 2", tracking_number="TRK_A", status="delivered"),
            dict(order_id="C1", order_date="2026-06-24", item_name="Dell All-in-One",
                 shipment="Shipment 1", tracking_number="TRK_B", status="delivered"),
        )

        sync_csv_to_sheet(path)

        rows = sheet.data_rows()
        assert len(rows) == 2, "swapped-number rows must reconcile by tracking, not duplicate"
        by_ship = {r[FIELDNAMES.index("shipment")]: r for r in rows}
        # Each box keeps its recorded (Shipment N, tracking) pairing — no cross-merge.
        assert by_ship["Shipment 1"][FIELDNAMES.index("tracking_number")] == "TRK_A"
        assert by_ship["Shipment 2"][FIELDNAMES.index("tracking_number")] == "TRK_B"
        assert all(r[FIELDNAMES.index("status")] == "delivered" for r in rows), "status advanced on both"
        assert all(r[FIELDNAMES.index("item_name")] == "Dell (Item #1953694)" for r in rows), "kept names"

    def test_tracking_reconcile_skips_when_tracking_is_ambiguous(self, sheet, tmp_path):
        # A box holding two distinct SKUs -> two existing rows share (order, tracking). An incoming row
        # can't be uniquely matched by tracking, so it appends rather than mis-merging onto one of them.
        common = dict(order_id="C1", order_date="2026-06-23", tracking_number="TRK1")
        sheet.rows = [
            list(HEADER),
            row(item_name="SKU A", shipment="Shipment 1", status="shipped", **common),
            row(item_name="SKU B", shipment="Shipment 1", status="shipped", **common),
        ]
        path = write_csv_file(
            tmp_path,
            dict(item_name="SKU C page name", shipment="Shipment 9", status="delivered", **common),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 3, "ambiguous tracking (2 existing) must not reconcile"

    def test_tracking_reconcile_skips_two_incoming_same_tracking(self, sheet, tmp_path):
        # Two incoming rows share one tracking number (two SKUs in one box) -> don't both collapse onto
        # a single existing row.
        sheet.rows = [
            list(HEADER),
            row(order_id="C1", order_date="2026-06-23", item_name="Existing", shipment="Shipment 1",
                status="shipped", tracking_number="TRK1"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="C1", order_date="2026-06-23", item_name="A", shipment="Shipment 2",
                 status="shipped", tracking_number="TRK1"),
            dict(order_id="C1", order_date="2026-06-23", item_name="B", shipment="Shipment 2",
                 status="shipped", tracking_number="TRK1"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 3, "two incoming for one tracking must not reconcile"

    def test_header_written_into_empty_sheet(self, sheet, tmp_path):
        sheet.rows = []
        path = write_csv_file(
            tmp_path, dict(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1")
        )

        sync_csv_to_sheet(path)

        assert sheet.rows[0] == HEADER

    def test_legacy_sheet_without_shipment_column_is_migrated(self, sheet, tmp_path):
        # A real legacy header is the CURRENT header TRUNCATED at the point that column was added —
        # every column since has been appended after it. Build it that way (not by filtering the name
        # out of HEADER, which stops being a prefix as soon as another column is appended, and the
        # migration deliberately only accepts a prefix).
        legacy_header = list(HEADER[: HEADER.index("Shipment")])
        legacy_row = [str(i) for i in range(len(legacy_header))]
        legacy_row[legacy_header.index("Order ID")] = "A1"
        legacy_row[legacy_header.index("Order Date")] = "2026-08-08"
        legacy_row[legacy_header.index("Item Name")] = "Widget"
        sheet.rows = [legacy_header, legacy_row]
        # Matches the legacy row's implicit blank Shipment cell.
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="", status="shipped"),
        )

        sync_csv_to_sheet(path)

        assert sheet.rows[0] == HEADER, "header row should be migrated to include Shipment"
        assert len(sheet.data_rows()) == 1, "legacy row should match, not duplicate"

    def test_sheet_without_buying_group_column_is_migrated(self, sheet, tmp_path):
        # A sheet that already has Shipment but predates Buying Group: the column is appended, so the
        # existing row keeps its position and gains trailing empty cells (same as the Shipment migration).
        legacy_header = list(HEADER[: HEADER.index("Buying Group")])
        legacy_row = [""] * len(legacy_header)
        legacy_row[legacy_header.index("Order ID")] = "A1"
        legacy_row[legacy_header.index("Order Date")] = "2026-08-08"
        legacy_row[legacy_header.index("Item Name")] = "Widget"
        legacy_row[legacy_header.index("Shipment")] = "Shipment 1"
        sheet.rows = [legacy_header, legacy_row]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", buying_group="BFMR"),
        )

        sync_csv_to_sheet(path)

        assert sheet.rows[0] == HEADER, "header row should be migrated to include Buying Group"
        assert len(sheet.data_rows()) == 1, "legacy row should match, not duplicate"
        assert sheet.data_rows()[0][FIELDNAMES.index("buying_group")] == "BFMR"

    def test_blank_buying_group_does_not_clobber_existing_tag(self, sheet, tmp_path):
        # A partial re-check classifies to "" (blank address); _merge_row must keep the recorded tag.
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                status="shipped", tracking_number="1Z1", buying_group="BFMR"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="delivered", delivery_date="2026-08-12", buying_group=""),
        )

        sync_csv_to_sheet(path)

        r = sheet.data_rows()[0]
        assert r[FIELDNAMES.index("buying_group")] == "BFMR", "blank re-check tag must not wipe it"
        assert r[FIELDNAMES.index("status")] == "delivered"

    def test_unrecognized_header_raises(self, sheet, tmp_path):
        sheet.rows = [["Something", "Entirely", "Different", "Shipment"]]
        path = write_csv_file(tmp_path, dict(order_id="A1", order_date="2026-08-08", item_name="W"))

        with pytest.raises(RuntimeError, match="not a recognized header"):
            sync_csv_to_sheet(path)


class TestSameKeyCollapse:
    """A single sync can carry two rows for one shipment — the CDP tracking read and the agent's
    re-read. They share an upsert key and must merge into one row, or the second clobbers the
    first (both merge against the same pre-sync snapshot, last write wins)."""

    def test_cdp_then_agent_rows_do_not_clobber_tracking(self, sheet, tmp_path):
        # Run-2 shape: the order is already recorded as 'ordered' (downgraded at discovery). The
        # sync carries CDP's read (shipped + tracking) and the agent's re-read (delivery date,
        # blank tracking). The row must end up shipped WITH the tracking number, not clobbered.
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                status="ordered", tracking_url="http://t/1", cost_per_item="189.99"),
        ]
        path = write_csv_file(
            tmp_path,
            # CDP read first (as scrape() orders recheck_items before agent_items)
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", tracking_number="1Z999"),
            # agent re-read second: same shipment, no tracking, but a delivery date
            dict(order_id="A1", order_date="2026-08-08", item_name="Widget", shipment="Shipment 1",
                 status="shipped", delivery_date="2026-08-10"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 1, "the two half-rows must collapse to one"
        r = sheet.data_rows()[0]
        assert r[FIELDNAMES.index("status")] == "shipped"
        assert r[FIELDNAMES.index("tracking_number")] == "1Z999", "CDP tracking must survive the agent row"
        assert r[FIELDNAMES.index("delivery_date")] == "2026-08-10", "agent delivery date must survive too"
        assert r[FIELDNAMES.index("cost_per_item")] == 189.99, "prior static data preserved"

    def test_agent_row_order_does_not_matter(self, sheet, tmp_path):
        # Same as above but agent row first — collapse must be order-independent for status.
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget",
                 shipment="Shipment 1", status="ordered", delivery_date="2026-08-10"),
            dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget",
                 shipment="Shipment 1", status="shipped", tracking_number="1Z999"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 1
        r = sheet.data_rows()[0]
        assert r[FIELDNAMES.index("status")] == "shipped", "furthest-along status wins regardless of order"
        assert r[FIELDNAMES.index("tracking_number")] == "1Z999"

    def test_further_along_status_wins(self, sheet, tmp_path):
        # delivered outranks shipped when one shipment's two reads disagree in a single sync.
        sheet.rows = [list(HEADER)]
        path = write_csv_file(
            tmp_path,
            dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget",
                 shipment="Shipment 1", status="shipped", tracking_number="1Z999"),
            dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget",
                 shipment="Shipment 1", status="delivered", delivery_date="2026-08-10"),
        )

        sync_csv_to_sheet(path)

        r = sheet.data_rows()[0]
        assert r[FIELDNAMES.index("status")] == "delivered"
        assert r[FIELDNAMES.index("tracking_number")] == "1Z999"

    def test_distinct_shipments_are_not_collapsed(self, sheet, tmp_path):
        # Different shipment labels are different keys — must stay two rows.
        sheet.rows = [list(HEADER)]
        common = dict(retailer="Amazon", order_id="A1", order_date="2026-08-08", item_name="Widget")
        path = write_csv_file(
            tmp_path,
            dict(**common, shipment="Shipment 1", status="shipped", tracking_number="1Z1"),
            dict(**common, shipment="Shipment 2", status="ordered"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 2


class TestUndisclosedSplit:
    """A shipment whose tracking number changes to a DIFFERENT non-blank value = the order shipped in
    more than one box but the retailer surfaces only one number. Instead of overwriting (losing the
    first box), append a new box row with Quantity '*' and alert — idempotently."""

    @pytest.fixture
    def alerts(self, monkeypatch):
        calls = []
        monkeypatch.setattr("alerts.notifier.alert", lambda subject, body: calls.append((subject, body)))
        return calls

    def test_changed_tracking_appends_new_box_row_and_alerts(self, sheet, tmp_path, alerts):
        # Existing (agent-written) box; incoming (ss-api) reports the SAME shipment line under a
        # different item-name wording AND a different tracking number -> hidden 2nd box.
        sheet.rows = [
            list(HEADER),
            row(order_id="B1", order_date="2026-08-10", item_name="HP - 14 Laptop",
                shipment="Shipment 1", status="shipped", tracking_number="086084", quantity="15"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", item_name="HP  14 Laptop", shipment="Shipment 1",
                 status="shipped", tracking_number="128095", quantity="15"),
        )

        sync_csv_to_sheet(path)

        rows = {r[FIELDNAMES.index("shipment")]: r for r in sheet.data_rows()}
        assert set(rows) == {"Shipment 1", "Shipment 2"}, "the new box must be appended, not overwrite"
        # Original box untouched.
        assert rows["Shipment 1"][FIELDNAMES.index("tracking_number")] == "086084"
        assert rows["Shipment 1"][FIELDNAMES.index("quantity")] == "15"
        # New box row: new tracking, quantity placeholder, recorded name kept.
        assert rows["Shipment 2"][FIELDNAMES.index("tracking_number")] == "128095"
        assert rows["Shipment 2"][FIELDNAMES.index("quantity")] == "*"
        assert rows["Shipment 2"][FIELDNAMES.index("total_cost")] == ""
        assert rows["Shipment 2"][FIELDNAMES.index("item_name")] == "HP - 14 Laptop"
        assert len(alerts) == 1 and "Split shipment" in alerts[0][0]

    def test_idempotent_once_new_box_has_its_own_row(self, sheet, tmp_path, alerts):
        # After a split, the API keeps reporting the new number against the old shipment line. It must
        # update the box that already owns that number, not append another duplicate.
        sheet.rows = [
            list(HEADER),
            row(order_id="B1", order_date="2026-08-10", item_name="HP - 14 Laptop",
                shipment="Shipment 1", status="shipped", tracking_number="086084", quantity="9"),
            row(order_id="B1", order_date="2026-08-10", item_name="HP - 14 Laptop",
                shipment="Shipment 2", status="shipped", tracking_number="128095", quantity="6"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", item_name="HP - 14 Laptop", shipment="Shipment 1",
                 status="delivered", tracking_number="128095", quantity="15"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 2, "must not re-duplicate a box whose number already has a row"
        rows = {r[FIELDNAMES.index("shipment")]: r for r in sheet.data_rows()}
        assert rows["Shipment 2"][FIELDNAMES.index("status")] == "delivered", "the owning box updates"
        assert rows["Shipment 1"][FIELDNAMES.index("tracking_number")] == "086084", "other box untouched"
        assert alerts == []

    def test_unchanged_tracking_does_not_trigger(self, sheet, tmp_path, alerts):
        sheet.rows = [
            list(HEADER),
            row(order_id="B1", order_date="2026-08-10", item_name="Laptop", shipment="Shipment 1",
                status="shipped", tracking_number="086084", quantity="15"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", item_name="Laptop", shipment="Shipment 1",
                 status="delivered", tracking_number="086084"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 1
        assert sheet.data_rows()[0][FIELDNAMES.index("status")] == "delivered"
        assert alerts == []

    def test_blank_to_value_tracking_is_a_normal_ship_not_a_split(self, sheet, tmp_path, alerts):
        # ordered -> shipped fills a previously-blank tracking number; that's not a split.
        sheet.rows = [
            list(HEADER),
            row(order_id="B1", order_date="2026-08-10", item_name="Laptop", shipment="Shipment 1",
                status="ordered", tracking_number="", quantity="15"),
        ]
        path = write_csv_file(
            tmp_path,
            dict(order_id="B1", order_date="2026-08-10", item_name="Laptop", shipment="Shipment 1",
                 status="shipped", tracking_number="128095"),
        )

        sync_csv_to_sheet(path)

        assert len(sheet.data_rows()) == 1
        assert sheet.data_rows()[0][FIELDNAMES.index("tracking_number")] == "128095"
        assert alerts == []


class TestLoadOrderState:
    def test_order_is_delivered_only_when_every_shipment_is(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="delivered", tracking_number="1Z1", profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 2",
                status="shipped", tracking_number="1Z2", profile_label="p1"),
        ]

        state = load_order_state("p1")

        assert state["delivered_ids"] == []
        assert [o["order_id"] for o in state["open_orders"]] == ["A1"]
        assert state["open_orders"][0]["status"] == "shipped"

    def test_all_shipments_delivered_rolls_up(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="delivered", profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="X", shipment="Shipment 2",
                status="delivered", profile_label="p1"),
        ]

        state = load_order_state("p1")

        assert state["delivered_ids"] == ["A1"]
        assert state["open_orders"] == []

    def test_retailer_scopes_state_on_a_multi_retailer_profile(self, sheet):
        """A profile hosting several retailers (e.g. profile-alpha = Best Buy + Amazon Business) must
        NOT leak one retailer's open orders into another's re-check. Without the retailer filter the
        agent fallback re-reads the other retailer's order_url and re-emits it under the wrong retailer,
        corrupting the ledger (retailer isn't in the upsert key). Regression for the live bug where an
        Amazon Business forced-agent run relabeled Best Buy rows."""
        sheet.rows = [
            list(HEADER),
            row(retailer="Best Buy", order_id="BBY01-1", order_date="2026-08-08", item_name="PS5",
                shipment="Shipment 1", status="ordered", tracking_url="http://fedex/1", profile_label="p1"),
            row(retailer="Amazon Business", order_id="114-1", order_date="2026-08-08", item_name="Switch",
                shipment="Shipment 1", status="ordered", tracking_url="http://amz/1", profile_label="p1"),
        ]

        biz = load_order_state("p1", retailer="Amazon Business")
        assert [o["order_id"] for o in biz["open_orders"]] == ["114-1"]

        bby = load_order_state("p1", retailer="Best Buy")
        assert [o["order_id"] for o in bby["open_orders"]] == ["BBY01-1"]

        # No retailer arg = old behavior (both, unscoped) — single-retailer profiles are unaffected.
        both = load_order_state("p1")
        assert {o["order_id"] for o in both["open_orders"]} == {"BBY01-1", "114-1"}

    def test_each_shipment_keeps_its_own_tracking_and_items(self, sheet):
        """The core of the per-shipment shape: a split order has one tracking page per shipment,
        and collapsing them to one URL is what forced the old code to skip multi-shipment orders."""
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="shipped", tracking_number="1Z-ONE", tracking_url="http://t/1",
                profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="X", shipment="Shipment 2",
                status="ordered", tracking_url="http://t/2", profile_label="p1"),
        ]

        shipments = load_order_state("p1")["open_orders"][0]["shipments"]

        assert [s["shipment"] for s in shipments] == ["Shipment 1", "Shipment 2"]
        assert shipments[0]["tracking_number"] == "1Z-ONE"
        assert shipments[0]["tracking_url"] == "http://t/1"
        assert shipments[0]["item_names"] == ["W"]
        assert shipments[0]["status"] == "shipped"
        assert shipments[1]["tracking_number"] == ""
        assert shipments[1]["tracking_url"] == "http://t/2"
        assert shipments[1]["item_names"] == ["X"]
        assert shipments[1]["status"] == "ordered"

    def test_items_in_the_same_shipment_group_together(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="shipped", tracking_number="1Z1", profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="X", shipment="Shipment 1",
                status="shipped", tracking_number="1Z1", profile_label="p1"),
        ]

        shipments = load_order_state("p1")["open_orders"][0]["shipments"]

        assert len(shipments) == 1
        assert shipments[0]["item_names"] == ["W", "X"]

    def test_needs_agent_true_while_a_shipment_lacks_tracking(self, sheet):
        # An untracked shipment hasn't shipped, so the order can still split — only a fresh read
        # of the order-details page can see that.
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="ordered", profile_label="p1"),
        ]

        assert load_order_state("p1")["open_orders"][0]["needs_agent"] is True

    def test_needs_agent_false_once_everything_is_tracked(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="shipped", tracking_number="1Z1", profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="X", shipment="Shipment 2",
                status="shipped", tracking_number="1Z2", profile_label="p1"),
        ]

        assert load_order_state("p1")["open_orders"][0]["needs_agent"] is False

    def test_delivered_shipment_without_tracking_does_not_force_the_agent(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="delivered", profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="X", shipment="Shipment 2",
                status="shipped", tracking_number="1Z2", profile_label="p1"),
        ]

        assert load_order_state("p1")["open_orders"][0]["needs_agent"] is False

    def test_legacy_blank_shipment_groups_like_any_other(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="",
                status="shipped", tracking_number="1Z1", profile_label="p1"),
        ]

        shipments = load_order_state("p1")["open_orders"][0]["shipments"]

        assert len(shipments) == 1
        assert shipments[0]["shipment"] == ""

    def test_since_drops_old_delivered_orders_from_the_skip_list(self, sheet):
        """The skip list rides in the prompt on every agent step; the agent never scans back
        past the window, so old delivered orders are pure dead weight."""
        sheet.rows = [
            list(HEADER),
            row(order_id="OLD", order_date="2026-01-01", item_name="W", shipment="Shipment 1",
                status="delivered", profile_label="p1"),
            row(order_id="NEW", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="delivered", profile_label="p1"),
        ]

        assert set(load_order_state("p1")["delivered_ids"]) == {"OLD", "NEW"}
        assert load_order_state("p1", since="2026-08-07")["delivered_ids"] == ["NEW"]

    def test_since_never_drops_open_orders(self, sheet):
        # Open orders are re-checked regardless of age — only delivered ones get trimmed.
        sheet.rows = [
            list(HEADER),
            row(order_id="OLD", order_date="2026-01-01", item_name="W", shipment="Shipment 1",
                status="shipped", tracking_number="1Z1", profile_label="p1"),
        ]

        state = load_order_state("p1", since="2026-08-07")

        assert [o["order_id"] for o in state["open_orders"]] == ["OLD"]

    def test_other_profiles_are_filtered_out(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="ordered", profile_label="p1"),
            row(order_id="B2", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="ordered", profile_label="p2"),
        ]

        assert [o["order_id"] for o in load_order_state("p1")["open_orders"]] == ["A1"]

    def test_blank_status_is_treated_as_open(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="", profile_label="p1"),
        ]

        state = load_order_state("p1")

        assert state["delivered_ids"] == []
        assert state["open_orders"][0]["status"] == "ordered"

    def test_read_failure_fails_soft(self, monkeypatch):
        def boom():
            raise RuntimeError("sheets is down")

        monkeypatch.setattr(ledger_sync, "_get_worksheet", boom)

        # Must not raise: a transient Sheets outage should degrade to "treat everything as new",
        # not abort the run.
        assert load_order_state("p1") == {"delivered_ids": [], "cancelled_ids": [], "open_orders": []}


class TestCancelledOrders:
    def test_cancelled_order_is_terminal_and_skipped(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="cancelled", profile_label="p1"),
        ]

        state = load_order_state("p1")

        assert state["cancelled_ids"] == ["A1"]
        assert state["delivered_ids"] == []
        assert state["open_orders"] == [], "a cancelled order must not be re-checked"

    def test_cancelled_order_id_reaches_the_skip_list(self, sheet, monkeypatch):
        # base.py combines delivered + cancelled + open into the agent's discovery skip list.
        from datetime import datetime, timezone

        from models.profile import ProfileConfig
        from scrapers.bestbuy import BestBuyScraper

        # Use today's date so the order stays inside the scraper's lookback window regardless of when
        # the suite runs — _load_order_state() trims cancelled orders older than that window (`since`),
        # so a hardcoded date silently falls out of range once the real clock passes it.
        today = datetime.now(timezone.utc).date().isoformat()
        sheet.rows = [
            list(HEADER),
            row(retailer="Best Buy", order_id="CANCELLED1", order_date=today, item_name="W",
                shipment="Shipment 1", status="cancelled", profile_label="p1"),
        ]
        # Wide lookback so the fixed order date can't fall outside the discovery window and get
        # trimmed as the real "today" advances — this test is about skip-list assembly, not date
        # windowing (which its sibling tests cover with an explicit `since`).
        scraper = BestBuyScraper(
            ProfileConfig(label="p1", profile_id="x", retailers=["bestbuy"]), lookback_days=100_000
        )

        state = scraper._load_order_state()
        skip = (list(state["delivered_ids"]) + list(state["cancelled_ids"])
                + [o["order_id"] for o in state["open_orders"]])

        assert "CANCELLED1" in skip

    def test_since_trims_old_cancelled_orders(self, sheet):
        sheet.rows = [
            list(HEADER),
            row(order_id="OLD", order_date="2026-01-01", item_name="W", shipment="Shipment 1",
                status="cancelled", profile_label="p1"),
            row(order_id="NEW", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="cancelled", profile_label="p1"),
        ]

        assert load_order_state("p1", since="2026-08-07")["cancelled_ids"] == ["NEW"]

    def test_partial_cancel_with_delivered_rest_is_terminal(self, sheet):
        # One shipment cancelled, the other delivered -> nothing left to track.
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="delivered", profile_label="p1"),
            row(order_id="A1", order_date="2026-08-08", item_name="X", shipment="Shipment 2",
                status="cancelled", profile_label="p1"),
        ]

        state = load_order_state("p1")

        assert state["open_orders"] == []
        assert "A1" in state["delivered_ids"]


class TestNumericCoercionOnMerge:
    def test_preserved_quantity_is_written_back_as_a_number(self, sheet, tmp_path):
        """The '1-as-text bug: a re-check leaves quantity blank, so _merge_row preserves the value
        read from the sheet (a string). It must be re-coerced to a number before writing, or Sheets
        stores it as text and shows a leading apostrophe."""
        sheet.rows = [
            list(HEADER),
            row(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="ordered", quantity="1", cost_per_item="899.99"),
        ]
        # Re-check: quantity/cost blank, only status changes.
        path = write_csv_file(
            tmp_path,
            dict(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                 status="shipped"),
        )

        sync_csv_to_sheet(path)

        written = sheet.data_rows()[0]
        assert written[FIELDNAMES.index("quantity")] == 1, "preserved quantity must be int, not '1'"
        assert isinstance(written[FIELDNAMES.index("quantity")], int)
        assert written[FIELDNAMES.index("cost_per_item")] == 899.99


class TestPlanBuyingGroupRetag:
    """plan_buying_group_retag is READ-ONLY (the caller script decides whether/how to apply it), so
    these tests work off raw header/rows, never a sheet write."""

    warehouses = [
        Warehouse(buying_group="BFMR", jigs=[Jig(zip="10001")]),
        Warehouse(buying_group="Personal", jigs=[Jig(zip="94103")]),
    ]

    def test_new_tag_is_planned_as_an_update(self):
        rows = [row(order_id="A1", order_date="2026-08-08", item_name="W",
                    delivery_address="123 Main St, New York NY 10001", buying_group="")]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["updates"] == [(2, "A1", "W", "", "BFMR")]
        assert plan["deletions"] == []
        assert plan["group_counts"] == {"BFMR": 1}

    def test_personal_row_is_planned_for_deletion_not_update(self):
        rows = [row(order_id="A1", order_date="2026-08-08", item_name="W",
                    delivery_address="5 Home St, SF CA 94103", buying_group="")]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["updates"] == []
        assert len(plan["deletions"]) == 1
        row_number, oid, name, addr, old_tag = plan["deletions"][0]
        assert (row_number, oid, name) == (2, "A1", "W")
        assert "94103" in addr

    def test_already_correctly_tagged_row_is_unchanged(self):
        rows = [row(order_id="A1", order_date="2026-08-08", item_name="W",
                    delivery_address="123 Main St, New York NY 10001", buying_group="BFMR")]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["updates"] == []
        assert plan["unchanged"] == 1
        assert plan["group_counts"] == {"BFMR": 1}

    def test_blank_address_row_is_left_alone(self):
        # No address to classify from -> "" -> not a real change, old tag (if any) stands.
        rows = [row(order_id="A1", order_date="2026-08-08", item_name="W",
                    delivery_address="", buying_group="BFMR")]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["updates"] == []
        assert plan["deletions"] == []
        assert plan["unchanged"] == 1

    def test_blank_order_id_row_is_skipped(self):
        rows = [row(order_id="", order_date="2026-08-08", item_name="W",
                    delivery_address="123 Main St, New York NY 10001")]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["updates"] == []
        assert plan["deletions"] == []
        assert plan["unchanged"] == 0

    def test_unclassified_address_counts_but_is_not_deleted(self):
        rows = [row(order_id="A1", order_date="2026-08-08", item_name="W",
                    delivery_address="99 Nowhere Rd, ZZ 00000", buying_group="")]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["deletions"] == []
        assert plan["updates"] == [(2, "A1", "W", "", "Unclassified")]
        assert plan["group_counts"] == {"Unclassified": 1}

    def test_needs_header_migration_flag(self):
        legacy_header = list(HEADER[: HEADER.index("Buying Group")])
        rows = [[""] * len(legacy_header)]

        plan = plan_buying_group_retag(legacy_header, rows, self.warehouses)

        assert plan["needs_header_migration"] is True

    def test_row_numbers_account_for_the_header_row(self):
        rows = [
            row(order_id="A1", order_date="2026-08-08", item_name="First"),
            row(order_id="A2", order_date="2026-08-08", item_name="Second",
                delivery_address="123 Main St, New York NY 10001"),
        ]

        plan = plan_buying_group_retag(HEADER, rows, self.warehouses)

        assert plan["updates"][0][0] == 3, "second data row is sheet row 3 (row 1 is the header)"
