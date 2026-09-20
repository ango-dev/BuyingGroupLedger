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


class TestUpdateCell:
    def test_key_cells_are_editable_here_and_checked(self):
        st = importer.Staging(id="b", created_at="", source_name="x", date_order="mdy", rows=[staged()])
        importer.write_staged_cell(st, "r0001-1", "order_id", "NEW-1")
        assert st.row("r0001-1").cells["order_id"] == "NEW-1"
        with pytest.raises(importer.StagingError, match="YYYY-MM-DD"):
            importer.write_staged_cell(st, "r0001-1", "order_date", "3/11/2026")
        with pytest.raises(importer.StagingError, match="Shipment must be a number"):
            importer.write_staged_cell(st, "r0001-1", "shipment", "two")

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
        commit = client.post("/tools/import/commit", follow_redirects=False)
        assert commit.status_code == 303 and "1+row%28s%29+imported" in commit.headers["location"]
        assert len(client.sheet.grid) == 4 and client.sheet.grid[3][HEADER.index("Payout Date")] == "2026-04-02"
        page = client.get("/tools/import").text
        assert page.count("data-entry-id=") == len(importer.STAGING_FIELDS)  # one row left: the open one
        assert "1 staged row(s)" in page

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
