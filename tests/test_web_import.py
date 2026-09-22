"""Tools > Import (web/importer.py): a CSV mapped onto the ledger, the rows that are not complete
kept on a staging sheet under data/imports/, imported once they are."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from config.settings import settings  # noqa: E402
from diagnostics import activity  # noqa: E402
from ledger.sync import HEADER  # noqa: E402
from scripts.audit_ledger import mandatory_gaps  # noqa: E402
from web import importer  # noqa: E402
from web.app import create_app  # noqa: E402
from web.ledger_writer import LedgerCellWriter, RunInProgress  # noqa: E402
from test_web_edit import GridReader, SheetFake, row  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def complete_cells(**over) -> dict:
    """Every cell a paid row must carry to land (the audit's rule), as staging text."""
    cells = {f: "" for f in importer.STAGING_FIELDS}
    cells.update(order_id="111-0000001-0000001", order_date="2026-03-11", item_name="Widget", shipment="1",
                 status="paid", retailer="Amazon", quantity="2", cost_per_item="50", total_cost="100",
                 profile_label="alpha", order_url="https://amazon.com/o/1", delivery_address="1 Main St",
                 card_name="Prime Visa", card_last4="0315", buying_group="BFMR", tracking_number="1Z1",
                 payout_amount="120", payout_date="2026-04-01", receipt_url="/receipts/amazon/2026-03/x.pdf",
                 insurance="2", tracking_submitted="TRUE")
    cells.update(over)
    return cells


def staged(**over) -> importer.StagedRow:
    return importer.StagedRow(id=over.pop("id", "r0001-1"), source_row=1, cells=complete_cells(**over))


@pytest.fixture
def sheet():
    return SheetFake([list(HEADER),
                      row(order_date="2026-08-20", status="paid", retailer="Costco", item_name="iPad", shipment="1",
                          quantity="2", order_id="1399000017", total_cost="400", cost_per_item="200",
                          payout_amount="500", payout_date="2026-09-01", tracking_number="529900000009")])


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "failures").mkdir()
    return d


@pytest.fixture
def client(sheet, tmp_path, logs_dir):
    writer = LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir)
    app = create_app(GridReader(sheet), logs_dir=logs_dir, failures_dir=logs_dir / "failures", backup_dir=tmp_path / "b",
                     repo_root_dir=tmp_path, clock=lambda: NOW,
                     settings=dataclasses.replace(settings, container_run_interval_hours=6, web_password=""), writer=writer)
    c = TestClient(app)
    c.sheet, c.data_dir, c.logs_dir = sheet, tmp_path / "data", logs_dir
    return c


CSV = ("Order Number,Date,Item,Qty,Total,Status,Store,Tracking,Paid,Payout Date,Profile,Link,Address,Card,Group,Receipt,Insurance,Submitted\n"
       "111-0000001-0000001,3/11/2026,Widget,2,100,paid,Amazon,1Z1,120,4/1/2026,alpha,https://a/1,1 Main St,Prime Visa 0315,BFMR,/receipts/a/x.pdf,2,yes\n"
       "111-0000002-0000002,3/12/2026,Gadget,1,50,paid,Amazon,1Z2,60,,alpha,https://a/2,1 Main St,Prime Visa 0315,BFMR,/receipts/a/y.pdf,1,yes\n"
       "111-0000003-0000003,3/13/2026,Gizmo,1,30,shipped,Amazon,1Z3,,,alpha,https://a/3,1 Main St,Prime Visa 0315,BFMR,,,\n")
MAPPING = {"map.0": "order_id", "map.1": "order_date", "map.2": "item_name", "map.3": "quantity", "map.4": "total_cost",
           "map.5": "status", "map.6": "retailer", "map.7": "tracking_number", "map.8": "payout_amount", "map.9": "payout_date",
           "map.10": "profile_label", "map.11": "order_url", "map.12": "delivery_address", "map.13": "card_name",
           "map.14": "buying_group", "map.15": "receipt_url", "map.16": "insurance", "map.17": "tracking_submitted"}


def upload_and_run(client, csv_text=CSV, mapping=MAPPING, date_order=""):
    assert client.post("/tools/import/upload", files={"source": ("old.csv", csv_text.encode(), "text/csv")},
                       follow_redirects=False).status_code == 303
    assert client.post("/tools/import/map", data={**mapping, "date_order": date_order, "profile": ""},
                       follow_redirects=False).headers["location"] == "/tools/import/preview"
    return client.post("/tools/import/run", follow_redirects=False)


# --------------------------------------------------------------------------------------------------
# The rule, shared with the audit
# --------------------------------------------------------------------------------------------------


class TestMandatoryGaps:
    def test_a_paid_row_needs_its_stage_cells(self):
        missing, unticked = mandatory_gaps(complete_cells(payout_date="", insurance="", tracking_submitted=""))
        assert missing == ["COGS", "Payout Date", "Insurance"] and unticked == ["Tracking Submitted"]  # COGS: the audit's, the importer drops it
        assert importer.gaps_for(importer.StagedRow(id="x", source_row=1, cells=complete_cells(payout_date="", insurance="", tracking_submitted=""))) == ["Payout Date", "Insurance", "Tracking Submitted (not ticked)"]

    def test_a_complete_row_has_no_gaps_but_the_formulas(self):
        missing, unticked = mandatory_gaps(complete_cells())
        assert missing == ["COGS"] and unticked == []  # the ledger computes COGS: the importer leaves it out
        assert importer.gaps_for(staged()) == [] and importer.gap_fields(staged()) == set()

    def test_a_cancelled_row_needs_identity_only(self):
        cells = {f: "" for f in importer.STAGING_FIELDS}
        cells.update(order_id="X", order_date="2026-01-01", item_name="Thing", shipment="1", status="cancelled", retailer="Amazon")
        assert mandatory_gaps(cells) == ([], [])
        cells["retailer"] = ""
        assert mandatory_gaps(cells) == (["Retailer"], [])

    def test_a_gift_card_row_is_exempt_from_the_package_cells(self):
        cells = complete_cells(item_name="Amazon eGift card", tracking_number="", delivery_address="", buying_group="",
                               tracking_submitted="", status="delivered", payout_amount="", payout_date="", insurance="")
        missing, unticked = mandatory_gaps(cells)
        assert "Tracking Number" not in missing and "Delivery Address" not in missing and "Delivery Date" not in missing
        assert unticked == []  # no group: nothing to submit
        assert missing == ["COGS", "Buying Group"]


# --------------------------------------------------------------------------------------------------
# Staging rows from a file
# --------------------------------------------------------------------------------------------------


class TestStageRows:
    def test_nothing_is_invented(self):
        rows = importer.stage_rows([{"Order": "", "Date": "", "Item": "Widget", "Qty": "", "Status": ""}],
                                   {"Order": "order_id", "Date": "order_date", "Item": "item_name", "Qty": "quantity", "Status": "status"},
                                   date_order="mdy")
        c = rows[0].cells
        assert c["order_id"] == "" and c["status"] == "" and c["quantity"] == "" and c["order_date"] == ""
        assert rows[0].id == "r0001-1" and rows[0].source_row == 1 and rows[0].warnings == []

    def test_a_date_or_status_that_does_not_parse_stays_blank_with_a_warning(self):
        rows = importer.stage_rows([{"Date": "Mar 11", "Status": "whatever", "Item": "x"}],
                                   {"Date": "order_date", "Status": "status", "Item": "item_name"}, date_order="mdy")
        assert rows[0].cells["order_date"] == "" and rows[0].cells["status"] == ""
        assert any("Order Date 'Mar 11' not understood" in w for w in rows[0].warnings)
        assert any("Status 'whatever' not understood" in w for w in rows[0].warnings)

    def test_dates_follow_the_chosen_order_and_money_is_normalised(self):
        rows = importer.stage_rows([{"Date": "3/11/2026", "Total": "$1,234.50", "Qty": "2", "Card": "Triple Cash 4351", "Ins": "-2.5", "Sub": "yes"}],
                                   {"Date": "order_date", "Total": "total_cost", "Qty": "quantity", "Card": "card_name", "Ins": "insurance", "Sub": "tracking_submitted"},
                                   date_order="dmy")
        c = rows[0].cells
        assert c["order_date"] == "2026-11-03"  # day/month
        assert c["cost_per_item"] == "617.25" and c["total_cost"] == "1234.50"  # from the total and the quantity
        assert c["card_name"] == "Triple Cash" and c["card_last4"] == "4351"
        assert c["insurance"] == "2.50" and c["tracking_submitted"] == "TRUE"

    def test_several_tracking_numbers_become_one_row_per_box(self):
        rows = importer.stage_rows([{"Order": "A1", "Tracking": "1Z1, 1Z2", "Qty": "3", "Unit": "10", "Ins": "3", "Paid": "33"}],
                                   {"Order": "order_id", "Tracking": "tracking_number", "Qty": "quantity", "Unit": "cost_per_item", "Ins": "insurance", "Paid": "payout_amount"},
                                   date_order="mdy")
        assert [r.id for r in rows] == ["r0001-1", "r0001-2"]
        assert [r.cells["shipment"] for r in rows] == ["1", "2"]
        assert [r.cells["quantity"] for r in rows] == ["2", "1"]
        assert [r.cells["total_cost"] for r in rows] == ["20", "10"]
        assert [r.cells["insurance"] for r in rows] == ["2", "1"] and [r.cells["payout_amount"] for r in rows] == ["22", "11"]
        assert any("split into one row per box" in w for w in rows[0].warnings)

    def test_a_split_without_a_quantity_per_box_leaves_the_per_box_cells_blank(self):
        """two tracking numbers with a blank Quantity put
        the order's Insurance and payout whole on BOTH box rows (the money doubled); a Quantity
        smaller than the box count made a qty-0, $0 box row that passed as complete. Neither box's
        share is knowable, so those cells stay blank for the user, and the warning says why."""
        blank_qty = importer.stage_rows([{"Order": "A1", "Tracking": "1Z5\n1Z6", "Ins": "2", "Paid": "20", "Total": "50", "Unit": "25"}],
                                        {"Order": "order_id", "Tracking": "tracking_number", "Ins": "insurance", "Paid": "payout_amount",
                                         "Total": "total_cost", "Unit": "cost_per_item"}, date_order="mdy")
        assert [r.cells["tracking_number"] for r in blank_qty] == ["1Z5", "1Z6"]
        for r in blank_qty:
            assert (r.cells["quantity"], r.cells["total_cost"], r.cells["insurance"], r.cells["payout_amount"]) == ("", "", "", "")
            assert r.cells["cost_per_item"] == "25"  # a per-unit figure is still true of every box
            assert any("Quantity is blank for 2 boxes" in w for w in r.warnings)
        short = importer.stage_rows([{"Order": "A1", "Tracking": "1Z5, 1Z6", "Qty": "1", "Unit": "10", "Paid": "12"}],
                                    {"Order": "order_id", "Tracking": "tracking_number", "Qty": "quantity", "Unit": "cost_per_item", "Paid": "payout_amount"},
                                    date_order="mdy")
        assert [r.cells["quantity"] for r in short] == ["", ""] and [r.cells["payout_amount"] for r in short] == ["", ""]
        assert any("Quantity is 1 for 2 boxes" in w for w in short[0].warnings)
        assert importer.gaps_for(importer.StagedRow(id="x", source_row=1, cells=complete_cells(quantity="", total_cost="", status="paid")))[:1] == ["Quantity"]

    def test_a_profit_column_that_disagrees_is_a_warning(self):
        rows = importer.stage_rows([{"Paid": "120", "Total": "100", "Rate": "0.05", "Profit": "40"}],
                                   {"Paid": "payout_amount", "Total": "total_cost", "Rate": "cashback_rate", "Profit": "source_profit"}, date_order="mdy")
        assert any("disagrees" in w for w in rows[0].warnings)

    def test_order_level_amounts_are_prorated_by_total_cost(self):
        a = staged(id="a", order_id="O1", shipment="1", total_cost="100", shipping="9")
        b = staged(id="b", order_id="O1", shipment="2", total_cost="300", shipping="9", item_name="Other")
        other = staged(id="c", order_id="O2", shipping="9")
        assert importer.prorate_order_level([a, b, other]) == ["O1"]
        assert a.cells["shipping"] == "2.25" and b.cells["shipping"] == "6.75" and other.cells["shipping"] == "9"


# --------------------------------------------------------------------------------------------------
# Classifying, editing, importing
# --------------------------------------------------------------------------------------------------


class TestClassify:
    def test_every_bucket(self, sheet):
        index = importer.ledger_index(GridReader(sheet).load().rows)
        complete = staged(id="ok")
        incomplete = staged(id="gap", payout_date="", order_id="111-0000009-0000009")
        dup = staged(id="dup", order_id="1399000017", order_date="2026-08-20", item_name="iPad", shipment="1")
        twice = staged(id="twice")  # the same key as `complete`
        open_row = staged(id="open", status="shipped", order_id="111-0000005-0000005")
        near = staged(id="near", order_id="1399000017", item_name="iPad Case")  # the ledger's order, another item
        held = staged(id="held", order_id="111-0000006-0000006", tracking_number="529900000009")  # the ledger's number
        buckets = importer.classify([complete, incomplete, dup, twice, open_row, near, held], index)
        assert {k: [r.id for r in v] for k, v in buckets.items()} == {
            "complete": ["ok"], "incomplete": ["gap"], "duplicate": ["dup", "twice"], "staged_open": ["open"], "staged_near": ["near", "held"]}
        assert open_row.note.startswith("open order") and near.note.startswith("the ledger already holds")

    def test_an_order_this_batch_imported_does_not_hold_its_own_remaining_rows(self, sheet):
        """item 1 of an order landed on the first run put the
        order on the ledger, so item 2 -- filled in later -- was a near duplicate of its sibling for
        ever. The batch's own imported orders are exempt; a stranger's order is still held."""
        index = importer.ledger_index(GridReader(sheet).load().rows)
        index["orders"].add("111-0000001-0000001")  # item 1 landed: the ledger holds the order now
        first = staged(id="first", item_name="First")
        first.status = "imported"
        second = staged(id="second", item_name="Second", shipment="2")
        stranger = staged(id="stranger", order_id="1399000017", item_name="iPad Case")
        buckets = importer.classify([first, second, stranger], index)
        assert [r.id for r in buckets["complete"]] == ["second"] and [r.id for r in buckets["staged_near"]] == ["stranger"]

    def test_import_anyway_lifts_the_open_and_near_holds_but_never_the_duplicate_refusal(self, sheet):
        index = importer.ledger_index(GridReader(sheet).load().rows)
        open_row = staged(id="open", status="shipped", order_id="111-0000005-0000005", payout_amount="", payout_date="", insurance="")
        near = staged(id="near", order_id="1399000017", item_name="iPad Case")
        held = staged(id="held", order_id="111-0000006-0000006", tracking_number="529900000009", payout_date="")
        dup = staged(id="dup", order_id="1399000017", order_date="2026-08-20", item_name="iPad", shipment="1")
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=[open_row, near, held, dup])
        assert [r.id for r in importer.accept_rows(st, ["open", "near", "held", "dup", "nope"])] == ["open", "near", "held", "dup"]
        buckets = importer.classify(st.rows, index)
        assert {k: [r.id for r in v] for k, v in buckets.items()} == {
            "complete": ["open", "near"], "incomplete": ["held"], "duplicate": ["dup"], "staged_open": [], "staged_near": []}
        assert near.note == "" and held.note == ""
        importer.accept_rows(st, ["near"], on=False)
        assert [r.id for r in importer.classify(st.rows, index)["staged_near"]] == ["near"]
        again = importer.Staging.from_json(st.to_json())  # the mark is kept on disk
        assert [r.accepted for r in again.rows] == [True, False, True, True]

    def test_dropping_rows_never_drops_an_import_record(self):
        done = staged(id="done")
        done.status = "imported"
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=[done, staged(id="s", order_id="X")])
        assert [r.id for r in importer.remove_rows(st, ["done", "s"])] == ["s"]
        assert [r.id for r in st.rows] == ["done"]


class TestUpdateCell:
    def test_key_cells_are_editable_here_and_checked(self):
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=[staged()])
        importer.write_staged_cell(st, "r0001-1", "order_id", "NEW-1")
        assert st.row("r0001-1").cells["order_id"] == "NEW-1"
        with pytest.raises(importer.StagingError, match="YYYY-MM-DD"):
            importer.write_staged_cell(st, "r0001-1", "order_date", "3/11/2026")
        with pytest.raises(importer.StagingError, match="real calendar date"):
            importer.write_staged_cell(st, "r0001-1", "order_date", "2026-13-45")
        with pytest.raises(importer.StagingError, match="real calendar date"):
            importer.write_staged_cell(st, "r0001-1", "payout_date", "2026-02-31")
        with pytest.raises(importer.StagingError, match="Shipment must be a number"):
            importer.write_staged_cell(st, "r0001-1", "shipment", "two")

    def test_a_key_cell_may_not_be_edited_onto_a_key_the_ledger_or_the_sheet_holds(self, sheet):
        """re-keyed onto an existing key, the row was marked a
        duplicate at the commit and left the sheet for good. Refused at the cell instead."""
        index = importer.ledger_index(GridReader(sheet).load().rows)
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy",
                              rows=[staged(id="a", order_id="1399000017", order_date="2026-08-20", item_name="iPad Case"),
                                    staged(id="b", order_id="X", item_name="Thing", shipment="1"),
                                    staged(id="c", order_id="X", item_name="Thing", shipment="2")])
        with pytest.raises(importer.StagingError, match="ledger already holds a row with this key"):
            importer.write_staged_cell(st, "a", "item_name", "iPad", index=index)
        assert st.row("a").cells["item_name"] == "iPad Case"  # the old key stands
        with pytest.raises(importer.StagingError, match="row 1 of the sheet already has this key"):
            importer.write_staged_cell(st, "c", "shipment", "1", index=index)
        importer.write_staged_cell(st, "c", "shipment", "3", index=index)  # a free key is fine
        importer.write_staged_cell(st, "a", "item_name", "iPad", index=None)  # no index: the ledger is not consulted
        assert st.row("a").cells["item_name"] == "iPad"

    def test_other_cells_go_through_the_ledgers_validation_and_total_cost_follows(self):
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=[staged()])
        with pytest.raises(importer.StagingError, match="Status must be one of"):
            importer.write_staged_cell(st, "r0001-1", "status", "bogus")
        with pytest.raises(importer.StagingError, match="computed"):
            importer.write_staged_cell(st, "r0001-1", "total_cost", "5")
        importer.write_staged_cell(st, "r0001-1", "quantity", "3")
        assert st.row("r0001-1").cells["total_cost"] == "150"
        importer.write_staged_cell(st, "r0001-1", "tracking_submitted", "false")
        assert st.row("r0001-1").cells["tracking_submitted"] == "FALSE"
        with pytest.raises(importer.StagingError, match="changed meanwhile"):
            importer.write_staged_cell(st, "r0001-1", "retailer", "Costco", expected="Best Buy")
        with pytest.raises(KeyError):
            importer.write_staged_cell(st, "nope", "retailer", "Costco")


class TestImportComplete:
    def test_complete_rows_land_duplicates_are_marked_and_gaps_stay(self, sheet, logs_dir, tmp_path):
        writer = LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir)
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy",
                              rows=[staged(id="ok"), staged(id="gap", payout_date="", order_id="111-0000009-0000009"),
                                    staged(id="dup", order_id="1399000017", order_date="2026-08-20", item_name="iPad", shipment="1")])
        saves = []
        result = importer.import_complete(st, writer, importer.ledger_index(GridReader(sheet).load().rows),
                                          save=lambda s: saves.append(len(s.staged)), clock=lambda: NOW)
        assert [r.id for r in result.imported] == ["ok"] and [r.id for r in result.duplicates] == ["dup"]
        assert result.remaining == 1 and result.notice() == "1 row(s) imported; 1 already on the ledger, skipped; 1 staged"
        assert st.row("ok").status == "imported" and st.row("ok").ledger_row == 3 and st.row("ok").imported_at == NOW.isoformat(timespec="seconds")
        assert st.row("gap").status == "staged" and st.row("dup").status == "duplicate"
        assert len(sheet.grid) == 3 and sheet.grid[2][HEADER.index("Item Name")] == "Widget"
        assert sheet.grid[2][HEADER.index("Tracking Submitted")] == "TRUE" and sheet.grid[2][HEADER.index("Total Cost")] == "100.0"
        assert saves and saves[-1] == 1  # saved after every row

    def test_a_run_holding_the_lock_stops_the_import(self, sheet, logs_dir):
        (logs_dir / ".run.lock").write_text("pid 1", encoding="utf-8")
        writer = LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir)
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=[staged()])
        with pytest.raises(RunInProgress):
            importer.import_complete(st, writer, importer.ledger_index([]), save=lambda s: None, clock=lambda: NOW)
        assert len(sheet.grid) == 2 and st.row("r0001-1").status == "staged"


class TestStore:
    def test_mapping_from_form_refuses_a_target_used_twice_and_unknown_columns(self):
        with pytest.raises(importer.StagingError, match="mapped from two columns"):
            importer.mapping_from_form(["A", "B"], {"map.0": "order_id", "map.1": "order_id"})
        with pytest.raises(importer.StagingError, match="not a ledger column"):
            importer.mapping_from_form(["A"], {"map.0": "cogs"})
        with pytest.raises(importer.StagingError, match="at least one"):
            importer.mapping_from_form(["A"], {"map.0": ""})
        assert importer.mapping_from_form(["A", "B"], {"map.0": "order_id", "map.1": ""}) == {"A": "order_id"}

    def test_a_batch_round_trips_and_only_one_is_live(self, tmp_path):
        batch = importer.new_batch(tmp_path, "old.csv", b"Order,Item\n1,x\n", clock=lambda: NOW)
        assert batch.id == "20260919T120000Z" and batch.source_name == "old.csv"
        assert importer.live_batch(tmp_path).id == batch.id and importer.staged_count(tmp_path) == 0
        with pytest.raises(importer.StagingError, match="already in progress"):
            importer.new_batch(tmp_path, "again.csv", b"a\n1\n", clock=lambda: NOW)
        done = staged(id="r0002-1")
        done.status = "imported"
        st = importer.Staging(id=batch.id, created_at="now", source_name="old.csv", date_order="mdy", rows=[staged(), done])
        batch.save_staging(st)
        again = batch.load_staging()
        assert [r.id for r in again.rows] == ["r0001-1", "r0002-1"] and again.rows[1].status == "imported"
        assert importer.staged_count(tmp_path) == 1
        assert importer.staging_csv(again).splitlines()[0] == ",".join(HEADER) and len(importer.staging_csv(again).splitlines()) == 2
        batch.discard()
        assert importer.live_batch(tmp_path) is None

    def test_new_batch_refuses_what_it_cannot_read(self, tmp_path):
        with pytest.raises(importer.StagingError, match="not UTF-8"):
            importer.new_batch(tmp_path, "x.csv", b"\xff\xfe\x00bad", clock=lambda: NOW)
        with pytest.raises(importer.StagingError, match="no header"):
            importer.new_batch(tmp_path, "x.csv", b"\n", clock=lambda: NOW)
        # two columns under one name would read each other's cells
        with pytest.raises(importer.StagingError, match="both named 'Date' \\(columns 1 and 3\\)"):
            importer.new_batch(tmp_path, "x.csv", b"Date,Item,Date \n1,x,2\n", clock=lambda: NOW)
        # a cell past the csv module's field limit was a 500 on the map page (bug 6)
        with pytest.raises(importer.StagingError, match="far too long"):
            importer.new_batch(tmp_path, "x.csv", b"Order,Note\n1,\"" + b"x" * 140_000 + b"\"\n", clock=lambda: NOW)
        assert importer.live_batch(tmp_path) is None  # nothing was kept

    def test_a_column_named_as_the_ledger_names_it_wins_its_suggestion(self, tmp_path):
        batch = importer.new_batch(tmp_path, "x.csv", b"Paid,Payout Date,Item\n120,4/1/2026,Widget\n", clock=lambda: NOW)
        suggested = {c["header"]: c["suggested"] for c in importer.source_columns(batch)}
        assert suggested["Payout Date"] == "payout_date" and suggested["Paid"] != "payout_date"  # the alias yields to the exact name

    def test_the_template_is_the_ledgers_header(self):
        assert importer.template_csv() == ",".join(HEADER) + "\n"


# --------------------------------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------------------------------


class TestImportPages:
    def test_the_landing_page_offers_the_upload_and_the_template(self, client):
        body = client.get("/tools/import").text
        assert 'action="/tools/import/upload"' in body and 'name="source"' in body and 'href="/tools/import/template.csv"' in body
        assert '<a href="/tools/import"' in body and "Run importer" in body  # the Tools menu
        template = client.get("/tools/import/template.csv")
        assert template.headers["content-type"].startswith("text/csv") and template.text.lstrip("﻿") == ",".join(HEADER) + "\n"

    def test_the_whole_flow_lands_the_complete_row_and_stages_the_rest(self, client):
        assert client.get("/tools/import/map", follow_redirects=False).status_code == 303  # nothing uploaded
        assert client.post("/tools/import/upload", files={"source": ("old.csv", CSV.encode(), "text/csv")},
                           follow_redirects=False).headers["location"] == "/tools/import/map"
        mapping = client.get("/tools/import/map").text
        assert 'name="map.0"' in mapping and "month/day/year" in mapping  # suggested from the headers, detected from the dates
        assert client.post("/tools/import/map", data={**MAPPING, "date_order": "", "profile": ""}, follow_redirects=False).status_code == 303
        preview = client.get("/tools/import/preview").text
        for text in ("imports", "staged", "staged: open", "Payout Date"):
            assert text in preview
        run = client.post("/tools/import/run", follow_redirects=False)
        assert run.status_code == 303 and "1+row%28s%29+imported" in run.headers["location"] and "2+staged" in run.headers["location"]
        assert len(client.sheet.grid) == 3 and client.sheet.grid[2][HEADER.index("Order ID")] == "111-0000001-0000001"
        page = client.get("/tools/import").text
        assert 'data-cell-url="/tools/import/cell"' in page and page.count('data-entry-id="r0002-1"') > 0
        assert 'class="chip gap">Payout Date</span>' in page and "open order" in page
        assert 'data-field="payout_date" data-entry-id="r0002-1" data-raw="" tabindex="0" data-kind="date"' in page
        assert "staged rows still to import" in page  # the nav badge
        events = activity.read(client.logs_dir / "activity.jsonl")
        assert [e["kind"] for e in events] == ["import", "import"]
        staging = json.loads((client.data_dir / "imports" / "20260919T120000Z" / "staging.json").read_text(encoding="utf-8"))
        assert [r["status"] for r in staging["rows"]] == ["imported", "staged", "staged"]

    def test_filling_the_gap_then_importing_moves_the_row_to_the_ledger(self, client):
        upload_and_run(client)
        cell = client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "payout_date", "value": "2026-04-02", "expected": ""})
        assert cell.status_code == 200 and 'data-raw="2026-04-02"' in cell.text and "data-error" not in cell.text and ' gap' not in cell.text
        stale = client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "payout_date", "value": "2026-04-03", "expected": ""})
        assert 'data-error="the cell changed meanwhile' in stale.text and 'data-raw="2026-04-02"' in stale.text
        bad = client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "status", "value": "bogus"})
        assert "Status must be one of" in bad.text
        # a key cell edited onto the ledger's own key is refused with the key named (bug 7)
        rekey = client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "order_id", "value": "1399000017"})
        assert 'data-raw="1399000017"' in rekey.text and "data-error" not in rekey.text  # a different date: no collision
        client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "order_date", "value": "2026-08-20"})
        collide = client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "item_name", "value": "iPad"})
        assert "ledger already holds a row with this key (1399000017 / 2026-08-20 / iPad / shipment 1)" in collide.text
        assert 'data-raw="Gadget"' in collide.text
        client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "order_date", "value": "2026-03-12"})
        client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "order_id", "value": "111-0000002-0000002"})
        commit = client.post("/tools/import/commit", follow_redirects=False)
        assert commit.status_code == 303 and "1+row%28s%29+imported" in commit.headers["location"]
        assert len(client.sheet.grid) == 4 and client.sheet.grid[3][HEADER.index("Payout Date")] == "2026-04-02"
        page = client.get("/tools/import").text
        assert page.count("data-entry-id=") == len(importer.STAGING_FIELDS)  # one row left: the open one
        assert "1 staged row(s)" in page

    def test_the_second_item_of_an_order_follows_the_first(self, client):
        """The review's reproduction (2026-09-21, importer bug 1): two rows of one order, the second
        lacking Payout Date. First lands on the run; Second, filled in, must land on the commit --
        it used to stay "check: near duplicate" of its own sibling with no way out."""
        two = (CSV.splitlines()[0] + "\n"
               + "111-0000007-0000007,3/11/2026,First,1,50,paid,Amazon,1Z7,60,4/1/2026,alpha,https://a/7,1 Main St,Prime Visa 0315,BFMR,/receipts/a/7.pdf,1,yes\n"
               + "111-0000007-0000007,3/11/2026,Second,1,40,paid,Amazon,1Z8,50,,alpha,https://a/7,1 Main St,Prime Visa 0315,BFMR,/receipts/a/7.pdf,1,yes\n")
        run = upload_and_run(client, two)
        assert "1+row%28s%29+imported" in run.headers["location"] and "1+staged" in run.headers["location"]
        page = client.get("/tools/import").text
        assert "check: near duplicate" not in page and 'class="chip gap">Payout Date</span>' in page
        client.post("/tools/import/cell", data={"entry_id": "r0002-1", "field": "payout_date", "value": "2026-04-02"})
        commit = client.post("/tools/import/commit", follow_redirects=False)
        assert "1+row%28s%29+imported" in commit.headers["location"] and "0+staged" in commit.headers["location"]
        assert [g[HEADER.index("Item Name")] for g in client.sheet.grid[2:]] == ["First", "Second"]

    def test_import_anyway_on_a_held_row_and_its_undo(self, client):
        """A near duplicate (the ledger's order 1399000017 under another item) and an open order are
        holds with an escape hatch: the row's Import anyway (htmx: the <tr> comes back re-rendered)
        lifts the hold, undo puts it back, and the commit lands the accepted row."""
        held = (CSV.splitlines()[0] + "\n"
                + "1399000017,8/20/2026,iPad Case,1,30,paid,Costco,529900000010,40,9/1/2026,alpha,https://c/1,1 Main St,Visa 0315,BFMR,/receipts/c/z.pdf,1,yes\n"
                + CSV.splitlines()[3] + "\n")
        upload_and_run(client, held)
        page = client.get("/tools/import").text
        assert page.count(">Import anyway</button>") == 2 and 'hx-vals=\'{"id": "r0001-1"}\'' in page
        assert "check: near duplicate" in page and ">open order<" in page
        swapped = client.post("/tools/import/rows/accept", data={"id": "r0001-1"}, headers={"HX-Request": "true"})
        assert swapped.status_code == 200 and swapped.text.lstrip().startswith("<tr>")
        assert ">import anyway</span>" in swapped.text and ">undo</button>" in swapped.text and "near duplicate" not in swapped.text
        assert swapped.text.count("data-entry-id=") == len(importer.STAGING_FIELDS) and 'aria-label="select row 1"' in swapped.text
        assert json.loads((client.data_dir / "imports" / "20260919T120000Z" / "staging.json").read_text(encoding="utf-8"))["rows"][0]["accepted"] is True
        back = client.post("/tools/import/rows/accept", data={"id": "r0001-1", "undo": "1"}, headers={"HX-Request": "true"})
        assert "check: near duplicate" in back.text and ">Import anyway</button>" in back.text
        assert "check: near duplicate" in client.get("/tools/import").text  # the hold is back on disk too
        assert client.post("/tools/import/rows/accept", data={"id": "nope"}, headers={"HX-Request": "true"}).status_code == 404
        plain = client.post("/tools/import/rows/accept", data={"id": "r0001-1"}, follow_redirects=False)
        assert plain.status_code == 303 and plain.headers["location"] == "/tools/import"
        commit = client.post("/tools/import/commit", follow_redirects=False)
        assert "1+row%28s%29+imported" in commit.headers["location"] and "1+staged" in commit.headers["location"]
        assert client.sheet.grid[2][HEADER.index("Item Name")] == "iPad Case"
        events = activity.read(client.logs_dir / "activity.jsonl")
        assert [e["summary"] for e in events if "anyway" in e["summary"] or "held" in e["summary"]] == [
            "Import: row 1 marked import anyway", "Import: row 1 held again", "Import: row 1 marked import anyway"]

    def test_a_duplicate_key_is_skipped_and_said(self, client):
        dup = CSV.splitlines()[0] + "\n" + "1399000017,8/20/2026,iPad,2,400,paid,Costco,529900000009,500,9/1/2026,alpha,https://c/1,1 Main St,Visa 0315,BFMR,/receipts/c/z.pdf,3,yes\n"
        run = upload_and_run(client, dup)
        assert "1+already+on+the+ledger%2C+skipped" in run.headers["location"] and len(client.sheet.grid) == 2

    def test_a_run_holding_the_lock_answers_423_and_writes_nothing(self, client):
        (client.logs_dir / ".run.lock").write_text("pid 1", encoding="utf-8")
        run = upload_and_run(client)
        assert run.status_code == 423 and "scheduled run is in progress" in run.text and len(client.sheet.grid) == 2

    def test_dropping_rows_discarding_and_a_second_upload(self, client):
        upload_and_run(client)
        assert client.post("/tools/import/upload", files={"source": ("again.csv", b"a\n1\n", "text/csv")}, follow_redirects=False).status_code == 409
        drop = client.post("/tools/import/rows/delete", data={"sel": ["r0002-1"]}, follow_redirects=False)
        assert drop.status_code == 303 and "1+row%28s%29+dropped" in drop.headers["location"]
        assert client.get("/tools/import/staging.csv").text.count("\n") == 2  # header + the open row
        assert client.post("/tools/import/discard", follow_redirects=False).status_code == 303
        assert not (client.data_dir / "imports" / "20260919T120000Z").exists()
        assert 'action="/tools/import/upload"' in client.get("/tools/import").text

    def test_a_bad_mapping_re_renders_with_the_reason(self, client):
        client.post("/tools/import/upload", files={"source": ("old.csv", CSV.encode(), "text/csv")}, follow_redirects=False)
        response = client.post("/tools/import/map", data={"map.0": "order_id", "map.1": "order_id"})
        assert response.status_code == 400 and "mapped from two columns" in response.text
        assert 'action="/tools/import/upload"' not in client.get("/tools/import").text  # the upload waits
        assert "waiting for its column mapping" in client.get("/tools/import").text

    def test_a_snapshot_backend_stages_everything(self, tmp_path, logs_dir):
        from web.ledger_reader import SnapshotReader

        data = tmp_path / "snap"
        data.mkdir()
        (data / "ledger_backup_20260917T000000Z.csv").write_text(",".join(HEADER) + "\n", encoding="utf-8")
        app = create_app(SnapshotReader(data_dir=data), logs_dir=logs_dir, failures_dir=logs_dir / "failures", backup_dir=tmp_path / "b",
                         repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6, web_password=""))
        client = TestClient(app)
        run = upload_and_run(client)
        assert "editing+is+off" in run.headers["location"] and "3+row%28s%29+staged" in run.headers["location"]
        assert client.post("/tools/import/commit", follow_redirects=False).status_code == 409


# --------------------------------------------------------------------------------------------------
# At scale: the commit saves in batches over one grid, the sheet
# shows one page at a time, and the nav badge does not parse the sheet on every render
# --------------------------------------------------------------------------------------------------


class TestAtScale:
    def test_the_commit_saves_every_batch_and_reads_the_grid_once(self, sheet, logs_dir):
        writer = LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir)
        rows = [staged(id=f"r{i:04d}-1", order_id=f"111-{i:07d}-0000001", item_name=f"W{i}") for i in range(1, 121)]
        rows.append(staged(id="dup", order_id="1399000017", order_date="2026-08-20", item_name="iPad", shipment="1"))
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=rows)
        saves, reads = [], []
        original = sheet.get_values

        def counted(*args, **kwargs):
            reads.append(1)
            return original(*args, **kwargs)

        sheet.get_values = counted
        result = importer.import_complete(st, writer, importer.ledger_index(GridReader(sheet).load().rows),
                                          save=lambda s: saves.append(len(s.staged)), clock=lambda: NOW)
        assert len(result.imported) == 120 and len(result.duplicates) == 1 and result.remaining == 0
        assert len(sheet.grid) == 122 and sheet.grid[-1][HEADER.index("Item Name")] == "W120"
        assert len(saves) <= 1 + 120 // importer.SAVE_EVERY + 1 and saves[-1] == 0  # batches, and once at the end
        assert len(reads) <= 2  # the grid is read once for the whole commit, not once per row
        # a second commit finds nothing left and writes nothing
        assert importer.import_complete(st, writer, importer.ledger_index([]), save=lambda s: None, clock=lambda: NOW).imported == []

    def test_a_refusal_mid_way_is_saved_where_it_happened(self, sheet, logs_dir):
        writer = LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir)
        rows = [staged(id="a", order_id="A"), staged(id="b", order_id="B", shipment="x"), staged(id="c", order_id="C")]  # b: refused by the ledger
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=rows)
        saves = []
        result = importer.import_complete(st, writer, importer.ledger_index([]), save=lambda s: saves.append([r.status for r in s.rows]), clock=lambda: NOW)
        assert [r.id for r in result.imported] == ["a", "c"] and [r.id for r, _ in result.refused] == ["b"]
        assert saves[-1] == ["imported", "staged", "imported"] and "refused by the ledger" in st.row("b").note

    def test_the_staging_sheet_is_one_page_at_a_time(self, client):
        many = CSV.splitlines()[0] + "\n" + "".join(
            f"111-{i:07d}-0000001,3/11/2026,Widget {i},2,100,paid,Amazon,1Z{i},120,,alpha,https://a/{i},1 Main St,Prime Visa 0315,BFMR,/receipts/a/{i}.pdf,2,yes\n"
            for i in range(1, 231))
        run = upload_and_run(client, many)
        assert "230+staged" in run.headers["location"]
        page = client.get("/tools/import").text
        rownums = '<td class="rownum muted"'
        assert page.count(rownums) == 100 and "1–100 of 230 staged row(s)" in page  # the default page size
        assert 'id="iper-form"' in page and 'href="/tools/import?ipage=2"' in page and "page 1 of 3" in page
        assert 'aria-label="select row 1"' in page and 'aria-label="select row 101"' not in page
        second = client.get("/tools/import", params={"ipage": "2"}).text
        assert "101–200 of 230" in second and 'aria-label="select row 101"' in second and 'data-entry-id="r0101-1"' in second
        assert 'name="sel" value="r0101-1"' in second
        big = client.get("/tools/import", params={"iper": "500"})
        assert big.text.count(rownums) == 230 and big.cookies.get("import-per") == "500"
        assert client.get("/tools/import").text.count(rownums) == 230  # remembered
        assert client.get("/tools/import", params={"iper": "7"}).text.count(rownums) == 230  # not a preset: the remembered one
        every = client.get("/tools/import", params={"iper": "0"}).text
        assert every.count(rownums) == 230 and "230 staged row(s)" in every and "of 230" not in every
        # the commit takes every complete row, whatever page is showing
        for i in range(1, 231):
            client.post("/tools/import/cell", data={"entry_id": f"r{i:04d}-1", "field": "payout_date", "value": "2026-04-02"}) if i <= 3 else None
        commit = client.post("/tools/import/commit", follow_redirects=False)
        assert "3+row%28s%29+imported" in commit.headers["location"] and "227+staged" in commit.headers["location"]
        assert client.get("/tools/import/staging.csv").text.count("\n") == 228  # every staged row, not a page

    def test_the_nav_badge_does_not_parse_the_sheet_when_it_has_not_changed(self, client, monkeypatch):
        upload_and_run(client)
        loads = []
        original = importer.Batch.load_staging

        def counted(self):
            loads.append(1)
            return original(self)

        monkeypatch.setattr(importer.Batch, "load_staging", counted)
        assert importer.staged_count(client.data_dir) == 2
        first = len(loads)
        assert importer.staged_count(client.data_dir) == 2 and len(loads) == first  # cached on the sheet's stamp
        client.get("/")
        assert len(loads) == first
