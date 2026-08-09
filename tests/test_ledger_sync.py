"""Upsert and order-state logic, exercised against an in-memory fake worksheet.

_get_worksheet() is the single seam where this module talks to Google Sheets, so patching it is
enough to test everything else offline — no credentials, no network, no live sheet.
"""

import csv

import pytest

from models.order import FIELDNAMES
from sheets import ledger_sync
from sheets.ledger_sync import HEADER, _merge_row, load_order_state, sync_csv_to_sheet


class FakeWorksheet:
    """Minimal stand-in for gspread.Worksheet covering only what ledger_sync calls."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]
        self.update_calls = 0

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, range_name, values):
        row_number = int(range_name.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        while len(self.rows) < row_number:
            self.rows.append([])
        self.rows[row_number - 1] = list(values[0])
        self.update_calls += 1

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

    def test_header_written_into_empty_sheet(self, sheet, tmp_path):
        sheet.rows = []
        path = write_csv_file(
            tmp_path, dict(order_id="A1", order_date="2026-08-08", item_name="W", shipment="Shipment 1")
        )

        sync_csv_to_sheet(path)

        assert sheet.rows[0] == HEADER

    def test_legacy_sheet_without_shipment_column_is_migrated(self, sheet, tmp_path):
        legacy_header = [h for h in HEADER if h != "Shipment"]
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

    def test_unrecognized_header_raises(self, sheet, tmp_path):
        sheet.rows = [["Something", "Entirely", "Different", "Shipment"]]
        path = write_csv_file(tmp_path, dict(order_id="A1", order_date="2026-08-08", item_name="W"))

        with pytest.raises(RuntimeError, match="not a recognized header"):
            sync_csv_to_sheet(path)


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
        from models.profile import ProfileConfig
        from scrapers.bestbuy import BestBuyScraper

        sheet.rows = [
            list(HEADER),
            row(order_id="CANCELLED1", order_date="2026-08-08", item_name="W", shipment="Shipment 1",
                status="cancelled", profile_label="p1"),
        ]
        scraper = BestBuyScraper(ProfileConfig(label="p1", profile_id="x", retailers=["bestbuy"]))

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
