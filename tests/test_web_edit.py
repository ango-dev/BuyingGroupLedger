"""Editing on the Orders page: web/ledger_writer.py (the ONE ledger-write path) and the routes over
it -- one cell, the same cell across selected rows, a new row, deleted rows -- over a fake
worksheet that records writes and refuses every other method. Plus the run-lock gate: while a
scheduled run holds logs/.run.lock, every write is refused."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from models.order import FIELDNAMES  # noqa: E402
from ledger.sync import HEADER, _COL, _cogs_formula, _profit_formula  # noqa: E402
from web import ledger_writer  # noqa: E402
from web.app import create_app  # noqa: E402
from web.ledger_reader import SnapshotReader, rows_from_grid  # noqa: E402
from web.ledger_writer import (  # noqa: E402
    ConflictError, EditError, RunInProgress, LedgerCellWriter, run_in_progress, validate,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def row(**values) -> list[str]:
    return [str(values.get(f, "")) for f in FIELDNAMES]


def _col_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


class SheetFake:
    """get_values, update, batch_update, delete_rows, row_count/add_rows; anything else is an
    AssertionError. Writes are applied to the grid so a re-read sees them."""

    title = "Orders"

    def __init__(self, grid):
        self.grid = [list(r) for r in grid]
        self.writes: list[tuple] = []
        self.batches: list[tuple] = []
        self.deleted: list[int] = []
        self.row_count = 1000
        self.added_rows = 0

    def get_values(self, range_name=None, value_render_option=None, **kwargs):
        assert value_render_option is not None
        return [list(r) for r in self.grid]

    def _put(self, range_name, values):
        column = "".join(c for c in range_name if c.isalpha())
        n = int("".join(c for c in range_name if c.isdigit()))
        start = _col_index(column)
        while len(self.grid) < n:
            self.grid.append([""] * len(HEADER))
        r = self.grid[n - 1]
        for offset, v in enumerate(values[0]):
            while len(r) <= start + offset:
                r.append("")
            if v is None:
                continue  # RAW None = leave the cell alone
            r[start + offset] = "TRUE" if v is True else "FALSE" if v is False else str(v)

    def update(self, range_name, values, value_input_option=None):
        self.writes.append((range_name, values, value_input_option))
        self._put(range_name, values)

    def batch_update(self, data, value_input_option=None):
        self.batches.append((data, value_input_option))
        for entry in data:
            self._put(entry["range"], entry["values"])

    def delete_rows(self, start, end=None):
        self.deleted.append(start)
        del self.grid[start - 1]

    def add_rows(self, count):
        self.row_count += count
        self.added_rows += count

    def __getattr__(self, name):
        raise AssertionError(f"the writer called worksheet.{name}() -- not allowed")


class GridReader:
    """A dashboard reader over a SheetFake: one formatted read per load, as the db reader does
    over the file. The routes under test never write through it."""

    backend = "grid"

    def __init__(self, sheet):
        self.sheet = sheet

    def load(self, force: bool = False):
        grid = self.sheet.get_values(value_render_option="FORMATTED_VALUE")
        return rows_from_grid(grid, backend=self.backend, source="Ledger / Orders")

    def health(self) -> dict:
        return {"backend": self.backend}


KEY = {"order_id": "BBY01-1", "order_date": "2026-09-08", "item_name": "MacBook", "shipment": "1"}
KEY2 = {"order_id": "1399000017", "order_date": "2026-08-20", "item_name": "iPad", "shipment": "1"}


@pytest.fixture
def sheet():
    return SheetFake([
        list(HEADER),
        row(order_date="2026-09-08", status="shipped", retailer="Best Buy", item_name="MacBook",
            shipment="1", quantity="1", order_id="BBY01-1", tracking_number="5238",
            buying_group="BFMR", cost_per_item="1000", total_cost="1000", cashback_rate="0.04",
            insurance="6.4", payout_amount="1230", delivery_date="2026-09-12"),
        row(order_date="2026-08-20", status="paid", retailer="Costco", item_name="iPad",
            shipment="1", quantity="2", order_id="1399000017", total_cost="400",
            payout_amount="500", payout_date="2026-09-01"),
        # A trailing row whose only content is the checkbox column's materialised FALSE: the
        # append must land ON it, not after it (ledger_sync._last_occupied_row).
        row(tracking_submitted="False"),
    ])


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def writer(sheet, logs_dir):
    return LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir)


class TestValidate:
    def test_key_and_formula_columns_are_refused(self):
        for field in ("order_id", "order_date", "item_name", "shipment"):
            with pytest.raises(EditError, match="part of the row's key"):
                validate(field, "x")
        for field in ("cogs", "total_profit"):
            with pytest.raises(EditError, match="derived column"):
                validate(field, "1")
        with pytest.raises(EditError, match="not editable"):
            validate("last_scraped_at", "x")

    def test_numbers_dates_status_and_checkbox(self):
        assert validate("payout_amount", "$1,230.00") == 1230.0
        assert validate("quantity", "3") == 3
        assert validate("cashback_rate", "4%") == 0.04
        assert validate("payout_date", "2026-09-01") == "2026-09-01"
        assert validate("status", "Paid") == "paid"
        assert validate("tracking_submitted", "yes") is True
        assert validate("insurance", "") == ""
        with pytest.raises(EditError, match="must be a number"):
            validate("payout_amount", "twelve")
        with pytest.raises(EditError, match="YYYY-MM-DD"):
            validate("payout_date", "Sept 1")
        with pytest.raises(EditError, match="Status must be one of"):
            validate("status", "done")
        with pytest.raises(EditError, match="TRUE or FALSE"):
            validate("tracking_submitted", "maybe")

    def test_editable_fields_are_everything_else(self):
        assert set(ledger_writer.EDITABLE_FIELDS) == set(FIELDNAMES) - {
            "order_id", "order_date", "item_name", "shipment", "cogs", "total_profit",
            "last_scraped_at"}


class TestLedgerCellWriter:
    def test_writes_a_number_raw_on_the_row_found_by_key(self, writer, sheet):
        result = writer.write_cell(KEY, "insurance", "7.25", expected="6.4")
        assert result == {"row_number": 2, "field": "insurance", "value": 7.25, "restored": False}
        assert sheet.writes == [(f"{_COL['insurance']}2", [[7.25]], "RAW")]

    def test_a_blank_clears_with_user_entered(self, writer, sheet):
        writer.write_cell(KEY, "insurance", "   ", expected="6.4")
        assert sheet.writes == [(f"{_COL['insurance']}2", [[""]], "USER_ENTERED")]

    def test_dates_stay_text_and_checkboxes_become_booleans(self, writer, sheet):
        writer.write_cell(KEY, "payout_date", "2026-09-20", expected="")
        writer.write_cell(KEY, "tracking_submitted", "TRUE", expected="")
        assert sheet.writes[0][1] == [["2026-09-20"]] and sheet.writes[0][2] == "RAW"
        assert sheet.writes[1][1] == [[True]]

    def test_a_changed_cell_is_a_conflict_and_nothing_is_written(self, writer, sheet):
        with pytest.raises(ConflictError, match="now reads '6.4'"):
            writer.write_cell(KEY, "insurance", "7", expected="5")
        assert sheet.writes == []

    def test_a_missing_row_is_a_conflict(self, writer, sheet):
        with pytest.raises(ConflictError, match="no longer on the ledger"):
            writer.write_cell({**KEY, "order_id": "nope"}, "insurance", "7")
        assert sheet.writes == []

    def test_a_duplicated_key_is_refused(self, writer, sheet):
        sheet.grid.append(list(sheet.grid[1]))
        with pytest.raises(EditError, match="2 rows share that key"):
            writer.write_cell(KEY, "insurance", "7")
        assert sheet.writes == []

    def test_a_foreign_header_is_refused(self, writer, sheet):
        sheet.grid[0] = list(reversed(HEADER))
        with pytest.raises(EditError, match="header"):
            writer.write_cell(KEY, "insurance", "7")

    def test_the_shipment_key_accepts_the_old_spelling(self, writer, sheet):
        writer.write_cell({**KEY, "shipment": "Shipment 1"}, "insurance", "1", expected="6.4")
        assert len(sheet.writes) == 1

    def test_no_expected_skips_the_conflict_check(self, writer, sheet):
        writer.write_cell(KEY, "insurance", "1")
        assert len(sheet.writes) == 1


class TestBulkEdit:
    def test_one_read_one_batch_for_every_located_row(self, writer, sheet):
        result = writer.write_cells([KEY, KEY2], "buying_group", "MOD")
        assert result == {"written": 2, "errors": [], "field": "buying_group", "value": "MOD"}
        assert len(sheet.batches) == 1
        data, option = sheet.batches[0]
        assert option == "RAW"
        assert [d["range"] for d in data] == [f"{_COL['buying_group']}2", f"{_COL['buying_group']}3"]
        assert sheet.grid[1][FIELDNAMES.index("buying_group")] == "MOD"

    def test_a_blank_clears_with_user_entered_and_a_bad_key_is_reported_not_fatal(self, writer, sheet):
        result = writer.write_cells([KEY, {**KEY, "order_id": "nope"}], "insurance", "")
        assert result["written"] == 1 and len(result["errors"]) == 1
        assert "nope" in result["errors"][0]
        assert sheet.batches[0][1] == "USER_ENTERED"

    def test_validation_and_emptiness(self, writer, sheet):
        with pytest.raises(EditError, match="no rows selected"):
            writer.write_cells([], "insurance", "1")
        with pytest.raises(EditError, match="derived column"):
            writer.write_cells([KEY], "cogs", "1")
        assert sheet.batches == []


class TestAppendRow:
    def test_lands_after_the_last_occupied_row_with_formulas_and_computed_total(self, writer, sheet):
        result = writer.add_row({"order_id": "NEW-1", "order_date": "2026-09-17",
                                    "item_name": "Thing", "quantity": "2", "cost_per_item": "$10.50",
                                    "retailer": "Costco", "buying_group": "BFMR"})
        # Row 4 held only a materialised FALSE checkbox, so the append lands ON it.
        assert result == {"row_number": 4, "key": {"order_id": "NEW-1", "order_date": "2026-09-17",
                                                    "item_name": "Thing", "shipment": "1"}}
        rng, values, option = sheet.writes[0]
        assert rng == "A4" and option == "RAW"
        written = dict(zip(FIELDNAMES, values[0]))
        assert written["shipment"] == 1 and written["status"] == "ordered"
        assert written["quantity"] == 2 and written["cost_per_item"] == 10.5
        assert written["total_cost"] == 21.0
        assert written["insurance"] is None  # blank -> None, never "" (keeps the column format)
        assert written["cogs"] is None and written["total_profit"] is None
        data, option = sheet.batches[0]
        assert option == "USER_ENTERED"
        assert data == [{"range": f"{_COL['cogs']}4", "values": [[_cogs_formula(4)]]},
                        {"range": f"{_COL['total_profit']}4", "values": [[_profit_formula(4)]]}]

    def test_grows_the_grid_when_the_append_would_fall_off_it(self, writer, sheet):
        sheet.row_count = 3
        writer.add_row({"order_id": "NEW-1", "order_date": "2026-09-17", "item_name": "T"})
        assert sheet.added_rows > 0 and sheet.row_count >= 4

    def test_requirements_and_duplicates(self, writer, sheet):
        with pytest.raises(EditError, match="Order ID is required"):
            writer.add_row({"order_date": "2026-09-17", "item_name": "T"})
        with pytest.raises(EditError, match="YYYY-MM-DD"):
            writer.add_row({"order_id": "X", "order_date": "17/09/2026", "item_name": "T"})
        with pytest.raises(EditError, match="Item Name is required"):
            writer.add_row({"order_id": "X", "order_date": "2026-09-17"})
        with pytest.raises(EditError, match="Shipment must be a number"):
            writer.add_row({"order_id": "X", "order_date": "2026-09-17", "item_name": "T",
                               "shipment": "one"})
        with pytest.raises(EditError, match="Status must be one of"):
            writer.add_row({"order_id": "X", "order_date": "2026-09-17", "item_name": "T",
                               "status": "done"})
        with pytest.raises(EditError, match="already on the ledger"):
            writer.add_row(KEY)
        assert sheet.writes == []


class TestDeleteRows:
    def test_deletes_bottom_up(self, writer, sheet):
        result = writer.remove_rows([KEY, KEY2])
        assert result == {"deleted": 2, "row_numbers": [3, 2]}
        assert sheet.deleted == [3, 2]
        assert [r[FIELDNAMES.index("order_id")] for r in sheet.grid[1:]] == [""]

    def test_all_or_nothing(self, writer, sheet):
        with pytest.raises(ConflictError):
            writer.remove_rows([KEY, {**KEY, "order_id": "nope"}])
        assert sheet.deleted == []
        with pytest.raises(EditError, match="no rows selected"):
            writer.remove_rows([])


class TestRunLockGate:
    def test_lock_semantics_match_main(self, logs_dir):
        import main

        assert ledger_writer.LOCK_STALE_SECONDS == main._LOCK_STALE_SECONDS
        assert ledger_writer.LOCK_FILE_NAME == main._LOCK_FILE.name
        assert run_in_progress(logs_dir) is False
        lock = logs_dir / ".run.lock"
        lock.write_text("1 2", encoding="utf-8")
        assert run_in_progress(logs_dir) is True
        old = time.time() - ledger_writer.LOCK_STALE_SECONDS - 1
        os.utime(lock, (old, old))
        assert run_in_progress(logs_dir) is False  # a stale lock is a crashed run, not a live one

    def test_every_write_is_refused_while_a_run_is_live(self, writer, sheet, logs_dir):
        (logs_dir / ".run.lock").write_text("1 2", encoding="utf-8")
        for call in (lambda: writer.write_cell(KEY, "insurance", "1"),
                     lambda: writer.write_cells([KEY], "insurance", "1"),
                     lambda: writer.add_row({"order_id": "X", "order_date": "2026-09-17",
                                                "item_name": "T"}),
                     lambda: writer.remove_rows([KEY])):
            with pytest.raises(RunInProgress, match="scheduled run is in progress"):
                call()
        assert sheet.writes == [] and sheet.batches == [] and sheet.deleted == []


# --------------------------------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------------------------------


class TestOrdersRoutes:
    def _client(self, sheet, tmp_path, logs_dir, writer=None):
        from config.settings import settings

        reader = GridReader(sheet)
        app = create_app(reader, logs_dir=logs_dir, failures_dir=tmp_path,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6),
                         writer=writer or LedgerCellWriter(opener=lambda: sheet, logs_dir=logs_dir))
        return TestClient(app)

    def test_the_orders_page_is_wide_editable_and_coloured_by_status(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders").text
        assert '<body class="wide">' in body
        assert 'class="grid ledger sheetlike"' in body
        assert body.count("<th ") == len(FIELDNAMES) + 1  # every column plus the row-number handle
        assert 'data-field="insurance"' in body and 'class="num edit"' in body
        order_id_td = re.search(r'<td class="([^"]*)"\s+data-field="order_id"', body)
        assert order_id_td and "edit" not in order_id_td.group(1)  # a key column: never editable
        cogs_td = re.search(r'<td class="([^"]*)"\s+data-field="cogs"', body)
        assert cogs_td and "edit" not in cogs_td.group(1)  # a formula: never editable
        assert '<tr class="status-shipped' in body and '<tr class="status-paid' in body
        assert "works like a spreadsheet" in body
        assert '<p class="muted small lead">Every row of the ledger' in body  # a description, as the other pages have
        assert "/static/edit.js" in body
        # The row tools.
        assert 'name="sel"' in body and 'id="sel-all"' in body
        assert 'class="sel"' not in body  # the row number is the handle; no checkbox column
        assert 'hx-post="/orders/bulk"' not in body and 'id="delete-selected" class="danger" hidden' in body
        assert 'id="bulkbar"' not in body  # no bar: the count line carries the counter and the hint
        assert 'id="sel-count">0</span> selected' in body
        # the how-it-works prose is the hint pill's tooltip -- a hidden table tooltip.js shows -- not
        # text on the line
        assert 'class="hint-mark"' in body and "· works like a spreadsheet" not in body
        assert 'data-tip-from="table-hints"' in body and '<div id="table-hints" hidden>' in body
        assert '<div class="muted count">' in body  # a div: the hidden blocks inside keep the pill on the right
        assert 'data-tip-from="tip-cell-date"' in body and '<div id="tip-cell-date" hidden>' in body  # a cell's how-to
        assert "Ctrl+Z / Ctrl+Y" in body
        assert '<td colspan="2">Rows</td>' in body  # the hint pill's table (the switch, the cells and the heartbeat have theirs too)
        assert "Delete or Backspace removes the selected rows from the ledger (asked once)" in body
        # the editor's kind per cell: a calendar on dates, previous answers on choice columns
        assert re.search(r'data-field="payout_date"[^>]*data-kind="date"', body)
        assert re.search(r'data-field="profile_label"[^>]*data-kind="choice"', body)
        assert re.search(r'data-field="insurance"[^>]*data-kind="text"', body)
        # Tracking Submitted is a real checkbox, and a check cell, never a text editor
        assert re.search(r'data-field="tracking_submitted"[^>]*data-kind="check"', body)
        assert 'type="checkbox" class="cell-check"' in body and "☑" not in body and "☐" not in body
        # an open row's delivery date is the retailer's estimate and says so
        est = re.search(r"data-field=\"delivery_date\"[^>]*>\s*2026-09-12 <span class=\"tag committed\" title=\"the retailer's estimated delivery date[^\"]*\">est\.</span>", body)
        assert est, "the shipped row's delivery date should carry the est. tag"
        choices = re.search(r'<script type="application/json" id="cell-choices">(.*?)</script>', body, re.S)
        parsed = json.loads(choices.group(1))
        assert choices and "Costco" in parsed["values"]["retailer"]
        assert isinstance(parsed["card_pairs"], list)
        assert 'class="cell-upload" data-order-id="BBY01-1"' in body
        assert 'name="order_date" data-date' in body
        # the Add-a-row form is the page's own design throughout: no native datalists, the choice
        # fields open the column's answers, Card Name / Card Last 4 narrow each other
        assert "<datalist" not in body and 'list="retailers"' not in body
        assert 'name="retailer" value="" data-choices="retailer"' in body
        assert 'name="card_name" value="" data-choices="card_name" data-pair="card_last4"' in body
        assert 'name="card_last4" value="" data-choices="card_last4" data-pair="card_name"' in body
        assert 'class="actions"' in body and 'class="span-6"' in body
        assert 'name="status" value="ordered" data-choices="status" data-options="ordered,shipped,delivered,cancelled,paid,return"' in body
        # every date-like editable uses the page's own picker: no native date / month controls anywhere
        assert 'type="month"' not in body and 'type="date"' not in body
        assert 'name="month" data-month' in body and 'name="paid" data-month' in body
        # The buttons live INSIDE the form that owns the selection (live: "no rows selected").
        bulk_form = body[body.index('<form id="bulk"'):body.index("</form>", body.index('<form id="bulk"'))]
        assert 'hx-post="/orders/delete"' in bulk_form and 'name="sel"' in bulk_form
        assert 'enctype="multipart/form-data"' in body and 'name="receipt_file"' in body
        assert 'action="/orders/add"' in body
        assert 'name="field"' not in body  # no field picker: a range fill sets one value on many rows

    def test_an_edit_writes_the_ledger_and_returns_the_fresh_cell(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "9.5",
                                                      "expected": "6.4"})
        assert response.status_code == 200
        assert sheet.writes == [(f"{_COL['insurance']}2", [[9.5]], "RAW")]
        assert "$9.50" in response.text and "data-error" not in response.text
        assert 'data-raw="9.5"' in response.text
        assert "$9.50" in client.get("/orders").text  # the reader re-read after the write

    def test_a_refused_edit_comes_back_in_the_cell_with_the_reason(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/cell", data={**KEY, "field": "payout_date",
                                                      "value": "yesterday", "expected": ""})
        assert response.status_code == 200
        assert "YYYY-MM-DD" in response.text and 'data-error=' in response.text
        assert sheet.writes == []
        conflict = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "1",
                                                      "expected": "0"})
        assert "now reads" in conflict.text and sheet.writes == []
        formula = client.post("/orders/cell", data={**KEY, "field": "cogs", "value": "1"})
        assert "not editable" in formula.text

    def test_the_keep_my_edits_switch_is_on_for_orders_and_off_for_audit_and_rides_the_write(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        page = client.get("/orders").text
        assert 'id="protect-edits" checked' in page
        assert 'data-tip-from="protect-hints"' in page and '<div id="protect-hints" hidden>' in page  # a table tip
        audit = client.get("/audit").text
        assert 'id="protect-edits">' in audit and 'id="protect-edits" checked' not in audit
        # the route passes `protect` through: the write lands, and the activity log says it was a correction
        response = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "2", "expected": "6.4", "protect": "0"})
        assert response.status_code == 200 and "data-error" not in response.text
        events = (logs_dir / "activity.jsonl").read_text(encoding="utf-8")
        assert "a correction; runs may overwrite it" in events

    def test_a_choice_cell_write_refreshes_the_dropdown_out_of_band(self, sheet, tmp_path, logs_dir):
        """a value typed and then changed back must not linger in the dropdown --
        the list is re-rendered from the ledger with every choice-cell write; a text column's write
        carries no list."""
        client = self._client(sheet, tmp_path, logs_dir)
        choice = client.post("/orders/cell", data={**KEY, "field": "profile_label", "value": "profile-new", "expected": ""})
        assert 'id="cell-choices" hx-swap-oob="true"' in choice.text and "profile-new" in choice.text
        plain = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "2", "expected": ""})
        assert "hx-swap-oob" not in plain.text

    def test_the_bulk_edit_route_is_gone(self, sheet, tmp_path, logs_dir):
        # 2026-09-18: the grid's range fill (select a range, type, Enter) replaced the field/value bar.
        client = self._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/bulk", data={"sel": [json.dumps(KEY)], "field": "insurance", "value": "1"})
        assert response.status_code in (404, 405) and sheet.batches == []

    def test_delete_selected(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/delete", data={"sel": [json.dumps(KEY2)]})
        assert response.status_code == 200 and "Deleted 1 row(s)" in response.text
        assert sheet.deleted == [3]
        assert "1399000017" not in response.text and "BBY01-1" in response.text
        # the filtered table comes back with the page's filters still applied
        response = client.post("/orders/delete", data={"sel": [json.dumps(KEY)], "retailer": "Costco"})
        assert "Deleted 1 row(s)" in response.text and response.text.count('<tr class="status-') == 0
        empty = client.post("/orders/delete", data={})
        assert empty.status_code == 200 and "no rows selected" in empty.text

    def test_add_row_redirects_to_the_new_order(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/add", data={"order_id": "NEW-1", "order_date": "2026-09-17",
                                                     "item_name": "Thing", "quantity": "1",
                                                     "cost_per_item": "5"}, follow_redirects=False)
        assert response.status_code == 303
        assert "q=NEW-1" in response.headers["location"]
        assert sheet.writes[0][0] == "A4"
        page = client.get(response.headers["location"]).text
        assert "Added NEW-1 at row 4" in page and "NEW-1" in page

    def test_add_row_errors_keep_the_form_open_with_the_values(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/add", data={"order_id": "NEW-1", "order_date": "bad",
                                                     "item_name": "Thing"})
        assert response.status_code == 400
        assert "YYYY-MM-DD" in response.text
        assert '<details class="add-row" open>' in response.text
        assert 'value="NEW-1"' in response.text
        assert sheet.writes == []

    def test_writes_are_refused_while_a_run_is_live(self, sheet, tmp_path, logs_dir):
        client = self._client(sheet, tmp_path, logs_dir)
        (logs_dir / ".run.lock").write_text("1 2", encoding="utf-8")
        cell = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "1"})
        assert "scheduled run is in progress" in cell.text
        gone = client.post("/orders/delete", data={"sel": [json.dumps(KEY)]})
        assert "scheduled run is in progress" in gone.text
        added = client.post("/orders/add", data={"order_id": "N", "order_date": "2026-09-17",
                                                  "item_name": "T"})
        assert added.status_code == 423
        assert sheet.writes == [] and sheet.batches == [] and sheet.deleted == []

    def test_the_snapshot_backend_is_view_only(self, tmp_path, logs_dir):
        import csv

        path = tmp_path / "ledger_backup_20260917T000000Z.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            w = csv.writer(handle)
            w.writerow(HEADER)
            w.writerow(row(order_id="X", order_date="2026-09-01", item_name="T", shipment="1",
                           status="shipped", total_cost="1"))
        from config.settings import settings

        app = create_app(SnapshotReader(path), logs_dir=logs_dir, failures_dir=tmp_path,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6))
        client = TestClient(app)
        body = client.get("/orders").text
        assert "view only (snapshot backend)" in body and 'class="num edit"' not in body
        assert 'name="sel"' not in body and 'action="/orders/add"' not in body
        cell = client.post("/orders/cell", data={"order_id": "X", "order_date": "2026-09-01",
                                                 "item_name": "T", "shipment": "1",
                                                 "field": "insurance", "value": "1"})
        assert "editing is off" in cell.text
        assert "editing is off" in client.post("/orders/delete", data={"sel": ["{}"]}).text
        assert client.post("/orders/add", data={"order_id": "N"}).status_code == 409


class TestTheWritePathIsSingular:
    def test_only_ledger_writer_names_the_write_scope_or_a_write_method(self):
        """web/ may write the ledger in exactly one file. Everything else in web/ is scanned by
        tests/test_web.py's read-only guarantee; this pins the exemption to that one file and
        what it may do: locate, update, batch-update, delete rows -- never create a tab, never
        append_row (rows land where the upsert's own append would)."""
        root = Path(__file__).resolve().parents[1] / "web"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name == "ledger_writer.py":
                assert "worksheet.update(" in text
                assert "worksheet.delete_rows(" in text and "worksheet.batch_update(" in text
                assert "append_row(" not in text.replace("def append_row(", "").replace(
                    "writer.add_row(", "")
                assert "add_worksheet" not in text
            else:
                for token in ("worksheet.update(", "delete_rows(", "batch_update("):
                    assert token not in text, f"{path.name} mentions {token}"


class TestReceiptUpload:
    @pytest.fixture
    def storage(self, monkeypatch):
        from web import receipts_upload

        stored = {}
        monkeypatch.setattr(receipts_upload.store, "is_configured", lambda: True)

        def put(key, body, ext):
            stored[key] = (body, ext)
            return f"https://par.example/o/{key}"

        monkeypatch.setattr(receipts_upload.store, "put", put)
        return stored

    def test_store_receipt_files_under_the_capture_key_and_returns_the_par_link(self, storage):
        from web.receipts_upload import store_receipt

        link = store_receipt(retailer="Amazon Business", order_id="111-1", order_date="2026-09-17",
                             filename="IMG_0042.JPEG", data=b"jpegbytes")
        assert link == "https://par.example/o/receipts/amazon-business/2026-09/111-1.jpg"
        assert storage["receipts/amazon-business/2026-09/111-1.jpg"] == (b"jpegbytes", "jpg")
        assert store_receipt(retailer="Best Buy", order_id="BBY01-1", order_date="2026-09-17",
                             filename="r.pdf", data=b"%PDF").endswith("receipts/bestbuy/2026-09/BBY01-1.pdf")
        assert store_receipt(retailer="Woot!", order_id="W1", order_date="", filename="a.png",
                             data=b"x").endswith("receipts/woot/unknown/W1.png")

    def test_refusals(self, storage):
        from web.receipts_upload import UploadError, store_receipt

        with pytest.raises(UploadError, match="must be one of"):
            store_receipt(retailer="Amazon", order_id="1", order_date="2026-09-17",
                          filename="notes.docx", data=b"x")
        with pytest.raises(UploadError, match="empty"):
            store_receipt(retailer="Amazon", order_id="1", order_date="2026-09-17",
                          filename="a.pdf", data=b"")
        with pytest.raises(UploadError, match="Order ID is required"):
            store_receipt(retailer="Amazon", order_id="", order_date="2026-09-17",
                          filename="a.pdf", data=b"x")
        assert storage == {}

    def test_unconfigured_storage_says_so(self):
        from web.receipts_upload import UploadError, store_receipt

        with pytest.raises(UploadError, match="is off"):
            store_receipt(retailer="Amazon", order_id="1", order_date="2026-09-17",
                          filename="a.pdf", data=b"x")

    def test_add_row_with_a_photo_stores_it_and_links_the_row(self, storage, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/add",
                               data={"order_id": "NEW-1", "order_date": "2026-09-17",
                                     "item_name": "Thing", "retailer": "Costco"},
                               files={"receipt_file": ("receipt.jpg", b"jpegbytes", "image/jpeg")},
                               follow_redirects=False)
        assert response.status_code == 303
        written = dict(zip(FIELDNAMES, sheet.writes[0][1][0]))
        assert written["receipt_url"] == "https://par.example/o/receipts/costco/2026-09/NEW-1.jpg"
        assert "receipts/costco/2026-09/NEW-1.jpg" in storage

    def test_add_row_with_a_bad_file_writes_nothing(self, storage, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/add",
                               data={"order_id": "NEW-1", "order_date": "2026-09-17", "item_name": "T"},
                               files={"receipt_file": ("notes.docx", b"x", "application/octet-stream")})
        assert response.status_code == 400 and "receipt not uploaded" in response.text
        assert sheet.writes == [] and storage == {}

    def test_upload_for_an_existing_order_links_every_row(self, storage, sheet, tmp_path, logs_dir):
        sheet.grid.append(row(order_date="2026-09-08", status="ordered", retailer="Best Buy",
                              item_name="MacBook", shipment="2", quantity="1", order_id="BBY01-1"))
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        page = client.get("/orders/BBY01-1").text
        assert 'action="/orders/BBY01-1/receipt"' in page
        response = client.post("/orders/BBY01-1/receipt",
                               files={"receipt_file": ("r.pdf", b"%PDF", "application/pdf")},
                               follow_redirects=False)
        assert response.status_code == 303 and "linked+on+2+row" in response.headers["location"]
        data, option = sheet.batches[0]
        assert option == "RAW" and len(data) == 2
        assert all(d["values"] == [["https://par.example/o/receipts/bestbuy/2026-09/BBY01-1.pdf"]] for d in data)
        after = client.get(response.headers["location"]).text
        assert "Receipt stored and linked on 2 row(s)" in after
        assert "receipts/bestbuy/2026-09/BBY01-1.pdf" in after

    def test_upload_from_the_table_answers_with_the_table(self, storage, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/BBY01-1/receipt", data={"next": "table", "retailer": "Best Buy"},
                               files={"receipt_file": ("r.pdf", b"%PDF", "application/pdf")})
        assert response.status_code == 200 and "Receipt stored and linked on 1 row(s)" in response.text
        assert 'class="sheet"' in response.text and "<html" not in response.text
        assert response.text.count('<tr class="status-') == 1  # the posted filter still applies
        failed = client.post("/orders/BBY01-1/receipt", data={"next": "table"})
        assert failed.status_code == 200 and "choose a file first" in failed.text and 'class="sheet"' in failed.text

    def test_upload_without_a_file_or_for_an_unknown_order(self, storage, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        response = client.post("/orders/BBY01-1/receipt", data={"x": "1"}, follow_redirects=False)
        assert response.status_code == 303 and "choose+a+file" in response.headers["location"]
        assert client.post("/orders/nope/receipt", files={"receipt_file": ("r.pdf", b"x", "application/pdf")}).status_code == 404


class TestSelectionValueSurvivesTheBrowser:
    def test_the_row_key_in_the_checkbox_value_parses_after_html_unescaping(self, sheet, tmp_path, logs_dir):
        """`tojson` output is marked safe and keeps its double quotes, so the
        attribute ended at the first quote and every delete said "no rows selected"."""
        import html as html_module

        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders").text
        values = re.findall(r'<input type="checkbox" name="sel" value="([^"]*)"', body)
        assert len(values) == 2
        keys = [json.loads(html_module.unescape(v)) for v in values]
        assert keys[0] == KEY and keys[1] == KEY2
        # And the round trip through the route deletes exactly that row.
        response = client.post("/orders/delete", data={"sel": [html_module.unescape(values[1])]})
        assert "Deleted 1 row(s)" in response.text and sheet.deleted == [3]


class TestCardsView:
    def test_cards_view_is_paginated_and_each_card_deletes_all_of_its_rows(self, sheet, tmp_path, logs_dir):
        import html as html_module

        for n in range(30):  # enough orders for two pages at 24
            sheet.grid.insert(2, row(order_date=f"2026-08-{(n % 28) + 1:02d}", status="paid",
                                     retailer="Costco", item_name=f"Thing {n}", shipment="1",
                                     order_id=f"ORD-{n:03d}", total_cost="10", payout_amount="12",
                                     payout_date="2026-09-01"))
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders", params={"view": "cards"}).text
        assert '<body class="wide">' in body and '<dialog id="confirm"' in body
        assert body.count('<article class="card') == 24
        assert "1–24 of 32 order(s)" in body
        assert 'class="pager"' in body and "page 1 of 2" in body
        assert 'id="bulkbar"' not in body  # the cards have their own delete
        assert 'name="view" value="cards" checked' in body
        # Cards carry the same editable cells as the table, and the Status cell wears the row colour.
        assert 'class="num edit"' in body and 'data-field="insurance"' in body
        assert 'data-field="status"' in body and '<table class="grid card-rows">' in body
        assert 'data-param="sort"' in body and 'name="sort" value="total_profit"' in body
        first_card = body[body.index('<article'):body.index('</article>')]
        assert '<dd class="num pos">$' in body  # a positive profit reads green, a negative red
        assert '<dt>Status</dt><dd><span class="tag status-' in first_card  # a coloured box in the facts
        assert '<details class="card-edit">' in first_card and '<ol class="items' in first_card

        page2 = client.get("/orders", params={"view": "cards", "page": "2"},
                           headers={"HX-Request": "true"}).text
        assert "<html" not in page2 and page2.count('<article class="card') == 8

        twelve = client.get("/orders", params={"view": "cards", "per": "12"}).text
        assert twelve.count('<article class="card') == 12 and "page 1 of 3" in twelve

        # The two-row order's card carries both keys; deleting it removes both rows.
        sheet.grid.append(row(order_date="2026-09-08", status="ordered", retailer="Best Buy",
                              item_name="MacBook", shipment="2", order_id="BBY01-1"))
        client.get("/orders", params={"refresh": "1"})
        body = client.get("/orders", params={"view": "cards", "q": "BBY01-1"}).text
        card = body[body.index('<article class="card'):body.index("</article>")]
        assert "all 2 of its row(s)" in card
        values = re.findall(r'name="sel" value="([^"]*)"', card)
        assert len(values) == 2
        keys = [json.loads(html_module.unescape(v)) for v in values]
        assert {k["shipment"] for k in keys} == {"1", "2"}
        response = client.post("/orders/delete", data={"sel": [html_module.unescape(v) for v in values],
                                                       "view": "cards", "q": "BBY01-1"})
        assert "Deleted 2 row(s)" in response.text and len(sheet.deleted) == 2
        assert "No orders match." in response.text  # re-rendered as cards, under the same filter

    def test_confirmations_go_through_the_page_dialog(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders").text
        assert '<dialog id="confirm"' in body and 'id="confirm-ok"' in body
        js = (Path(__file__).resolve().parents[1] / "web" / "static" / "edit.js").read_text(encoding="utf-8")
        assert 'addEventListener("htmx:confirm"' in js and "issueRequest(true)" in js


class TestViewMemory:
    def test_the_chosen_view_and_page_size_are_remembered_for_links_that_do_not_say(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        assert '<article class="card' not in client.get("/orders").text  # the default is the table

        chosen = client.get("/orders", params={"view": "cards", "per": "12", "remember": "1"})
        assert chosen.cookies.get("ledger-view") == "cards" and chosen.cookies.get("ledger-per") == "12"

        # An overview link names only a filter; it opens in the remembered view.
        body = client.get("/orders", params={"status": "shipped"}).text
        assert '<article class="card' in body and 'name="per" value="12" checked' in body
        assert 'name="view" value="cards" checked' in body
        # htmx swaps follow it too, and an explicit choice overrides and updates the memory.
        assert '<article class="card' in client.get("/orders", headers={"HX-Request": "true"}).text
        back = client.get("/orders", params={"view": "table", "remember": "1"})
        assert back.cookies.get("ledger-view") == "table"
        assert '<article class="card' not in client.get("/orders").text

    def test_the_bare_orders_page_replays_the_remembered_filters_and_sort(self, sheet, tmp_path, logs_dir):
        """set up the filters or sorts, and the default page remembers them."""
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        chosen = client.get("/orders", params={"status": "shipped", "sort": "total_cost", "dir": "asc",
                                               "q": "", "page": "2", "remember": "1"})
        cookie = chosen.cookies.get("ledger-filters")
        assert "status=shipped" in cookie and "sort=total_cost" in cookie and "page=" not in cookie
        body = client.get("/orders").text  # the nav link: no query at all
        assert 'name="status" value="shipped" checked' in body
        assert 'name="sort" value="total_cost"' in body and 'name="dir" value="asc"' in body
        # a link that names a filter is taken as it is, not merged with the memory
        body = client.get("/orders", params={"status": "paid"}).text
        assert 'name="status" value="paid" checked' in body and 'value="shipped" checked' not in body
        # ... and, having no `remember` mark (it is not the form), it does not rewrite the memory
        assert 'value="shipped" checked' in client.get("/orders").text
        assert "remember=" not in chosen.cookies.get("ledger-filters") and "page=" not in chosen.cookies.get("ledger-filters")
        # clearing the filters is remembered too
        client.get("/orders", params={"q": "", "sort": "order_date", "dir": "desc", "remember": "1"})
        assert 'value="shipped" checked' not in client.get("/orders").text

    def test_reset_forgets_the_remembered_filters(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        client.get("/orders", params={"status": "shipped", "view": "cards", "remember": "1"})
        assert 'value="shipped" checked' in client.get("/orders").text
        assert 'href="/orders?reset=1"' in client.get("/orders").text
        reset = client.get("/orders", params={"reset": "1"}, follow_redirects=False)
        assert reset.status_code == 303 and reset.headers["location"] == "/orders"
        body = client.get("/orders").text
        assert 'value="shipped" checked' not in body
        assert 'name="view" value="cards" checked' in body  # the view is a layout preference: kept

    def test_a_bad_cookie_is_ignored(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        client.cookies.set("ledger-view", "grid")
        client.cookies.set("ledger-per", "7")
        body = client.get("/orders").text
        assert '<article class="card' not in body and 'name="per" value="24" checked' in body


class TestOrderPageEditing:
    def test_the_order_page_rows_are_editable_like_the_table(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders/BBY01-1").text
        assert 'class="num edit"' in body and 'data-field="insurance"' in body
        assert 'data-order-id="BBY01-1"' in body and "works like a spreadsheet" in body
        assert 'data-tip-from="shipment-hints"' in body and '<td colspan="2">Rows</td>' not in body  # no row tools here
        cogs_td = re.search(r'<td class="([^"]*)"\s+data-field="cogs"', body)
        assert cogs_td and "edit" not in cogs_td.group(1)
        # An edit made from the order page lands exactly as one from the table.
        response = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "8",
                                                      "expected": "6.4"})
        assert response.status_code == 200 and sheet.writes[0][0] == f"{_COL['insurance']}2"
        assert "$8.00" in client.get("/orders/BBY01-1").text

    def test_the_snapshot_backend_order_page_is_view_only(self, tmp_path, logs_dir):
        import csv

        path = tmp_path / "ledger_backup_20260917T000000Z.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            w = csv.writer(handle)
            w.writerow(HEADER)
            w.writerow(row(order_id="X", order_date="2026-09-01", item_name="T", shipment="1",
                           status="shipped", total_cost="1"))
        from config.settings import settings

        app = create_app(SnapshotReader(path), logs_dir=logs_dir, failures_dir=tmp_path,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6))
        body = TestClient(app).get("/orders/X").text
        assert 'data-field="insurance"' in body and 'class="num edit"' not in body

    def test_link_cells_carry_a_pencil_and_blank_ones_an_add_affordance(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders").text
        # every link column is editable: a filled cell shows its link plus the pencil, a blank
        # one the "add" hint; both open the same inline editor (edit.js)
        assert 'data-field="order_url"' in body
        cell = body[body.index('data-field="order_url"'):]
        cell = cell[:cell.index("</td>")]
        assert ("cell-edit" in cell and "↗" in cell) or "add ↗" in cell
        cards = client.get("/orders", params={"view": "cards"}).text
        assert 'data-field="order_url"' in cards and 'data-field="delivery_address"' in cards

    def test_the_card_delete_is_at_the_bottom_right(self, sheet, tmp_path, logs_dir):
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        body = client.get("/orders", params={"view": "cards"}).text
        card = body[body.index('<article class="card'):body.index("</article>")]
        footer = card[card.index("<footer>"):card.index("</footer>")]
        assert "✕ Delete order" in footer and 'hx-post="/orders/delete"' in footer
        assert 'hx-post="/orders/delete"' not in card[:card.index("<footer>")]
        # the footer's links are buttons of the same cut as Delete order
        links = footer[footer.index('<span class="links">'):footer.index("</span>")]
        assert links.count('class="button small"') == links.count("<a ") >= 1
        assert ">Details<" in links and "details" not in links

    def test_no_page_rule_can_hide_the_card_footer(self):
        # `body.wide footer` (0,1,2) once outranked `.card footer` (0,1,1) and hid every card's
        # footer, Delete button and links included. The page footer is gone (2026-09-18); no rule
        # may match a card's <footer> from above.
        css = (Path(__file__).resolve().parents[1] / "web" / "static" / "style.css").read_text(encoding="utf-8")
        assert "body.wide footer {" not in css and "\nfooter {" not in css


class TestNoNestedForms:
    def test_a_cards_delete_form_is_never_inside_the_bulk_form(self, sheet, tmp_path, logs_dir):
        """The first card's delete did nothing but GET /orders?sel=...: its
        <form> was nested in #bulk, so the browser dropped the tag and the button submitted the
        outer form natively. The bulk form wraps the table view only."""
        import re

        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        cards = client.get("/orders", params={"view": "cards"}).text
        assert 'id="bulk"' not in cards
        table = client.get("/orders", params={"view": "table"}).text  # the cookie remembers cards
        assert 'id="bulk"' in table
        for body in (cards, table):
            depth = 0
            for tag in re.findall(r"<form\b|</form>", body):
                depth += 1 if tag.startswith("<form") else -1
                assert 0 <= depth <= 1, "a <form> is nested inside another <form>"
            assert depth == 0
        # every card's delete is its own htmx form, the first one included
        first = cards[cards.index('<article class="card'):cards.index("</article>")]
        assert '<form class="card-delete" hx-post="/orders/delete"' in first


class TestTheDropdowns:
    def test_no_native_select_is_left_on_the_orders_or_settings_page(self, sheet, tmp_path, logs_dir):
        """every select-one dropdown uses the page's own dropdown design."""
        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        for url, params in (("/orders", {}), ("/orders", {"view": "cards"}), ("/settings", {})):
            body = client.get(url, params=params).text
            assert "<select" not in body, url
            assert 'class="multi single' in body, url
        body = client.get("/orders", params={"view": "cards"}).text
        # a single-choice dropdown submits like a select: one radio per option, the pick checked
        assert 'name="dir" value="desc" checked' in body and 'name="dir" value="asc"' in body
        assert '<span class="summary-value">newest / highest first</span>' in body


class TestTheFilePickers:
    def test_every_file_input_is_a_drop_zone(self, sheet, tmp_path, logs_dir):
        """click to open the file dialog OR drag and drop, in the page's own
        design; the order page's receipt uploads as soon as it is chosen."""
        import re

        client = TestOrdersRoutes()._client(sheet, tmp_path, logs_dir)
        order = client.get("/orders/BBY01-1").text
        zone = order[order.index('<form method="post" action="/orders/BBY01-1/receipt"'):]
        zone = zone[:zone.index("</form>")]
        assert 'class="dropzone ' in zone and 'data-autosubmit="1"' in zone
        assert 'name="receipt_file"' in zone and "Drop a file here" in zone
        add = client.get("/orders").text
        assert 'name="receipt_file"' in add and "data-autosubmit" not in add[add.index('name="receipt_file"') - 200:add.index('name="receipt_file"')]
        settings = client.get("/settings").text
        assert 'name="archive"' in settings and "Drop a file here" in settings
        for body in (order, add, settings):
            for m in re.finditer(r'<input type="file"', body):
                before = body[max(0, m.start() - 160):m.start()]
                assert 'class="dropzone' in before, "a bare file input"
