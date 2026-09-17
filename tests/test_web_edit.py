"""Cell editing on the Orders page: web/ledger_writer.py (the ONE sheet-write path) and
POST /orders/cell, over a fake worksheet that records writes and refuses every other method."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from models.order import FIELDNAMES  # noqa: E402
from sheets.ledger_sync import HEADER, _COL  # noqa: E402
from web import ledger_writer  # noqa: E402
from web.app import create_app  # noqa: E402
from web.ledger_reader import SheetReader, SnapshotReader  # noqa: E402
from web.ledger_writer import ConflictError, EditError, SheetCellWriter, validate  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def row(**values) -> list[str]:
    return [str(values.get(f, "")) for f in FIELDNAMES]


class SheetFake:
    """get_values + update only; any other method is an AssertionError."""

    title = "Orders"

    def __init__(self, grid):
        self.grid = [list(r) for r in grid]
        self.writes: list[tuple] = []

    def get_values(self, range_name=None, value_render_option=None, **kwargs):
        assert value_render_option is not None
        return [list(r) for r in self.grid]

    def update(self, range_name, values, value_input_option=None):
        self.writes.append((range_name, values, value_input_option))
        column = "".join(c for c in range_name if c.isalpha())
        n = int("".join(c for c in range_name if c.isdigit()))
        index = 0
        for ch in column:
            index = index * 26 + (ord(ch) - ord("A") + 1)
        r = self.grid[n - 1]
        while len(r) <= index - 1:
            r.append("")
        v = values[0][0]
        r[index - 1] = "TRUE" if v is True else "FALSE" if v is False else str(v)

    def __getattr__(self, name):
        raise AssertionError(f"the writer called worksheet.{name}() -- only get_values/update are allowed")


KEY = {"order_id": "BBY01-1", "order_date": "2026-09-08", "item_name": "MacBook", "shipment": "1"}


@pytest.fixture
def sheet():
    return SheetFake([
        list(HEADER),
        row(order_date="2026-09-08", status="shipped", retailer="Best Buy", item_name="MacBook",
            shipment="1", quantity="1", order_id="BBY01-1", tracking_number="5238",
            buying_group="BFMR", cost_per_item="1000", total_cost="1000", cashback_rate="0.04",
            insurance="6.4", payout_amount="1230"),
        row(order_date="2026-08-20", status="paid", retailer="Costco", item_name="iPad",
            shipment="1", quantity="2", order_id="1399000017", total_cost="400",
            payout_amount="500", payout_date="2026-09-01"),
    ])


@pytest.fixture
def writer(sheet):
    return SheetCellWriter(opener=lambda: sheet)


class TestValidate:
    def test_key_and_formula_columns_are_refused(self):
        for field in ("order_id", "order_date", "item_name", "shipment"):
            with pytest.raises(EditError, match="part of the row's key"):
                validate(field, "x")
        for field in ("cogs", "total_profit"):
            with pytest.raises(EditError, match="sheet formula"):
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


class TestSheetCellWriter:
    def test_writes_a_number_raw_on_the_row_found_by_key(self, writer, sheet):
        result = writer.write_cell(KEY, "insurance", "7.25", expected="6.4")
        assert result == {"row_number": 2, "field": "insurance", "value": 7.25}
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
        with pytest.raises(ConflictError, match="no longer on the sheet"):
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


class TestOrdersCellRoute:
    def _client(self, sheet, tmp_path, writer=None, reader=None):
        from config.settings import settings

        reader = reader or SheetReader(ttl_seconds=300, opener=lambda: (sheet, "Ledger"))
        app = create_app(reader, logs_dir=tmp_path, failures_dir=tmp_path,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6),
                         writer=writer or SheetCellWriter(opener=lambda: sheet))
        return TestClient(app)

    def test_the_orders_page_is_wide_editable_and_coloured_by_status(self, sheet, tmp_path):
        client = self._client(sheet, tmp_path)
        body = client.get("/orders").text
        assert '<body class="wide">' in body
        assert 'class="grid ledger sheetlike"' in body
        assert body.count("<th ") == len(FIELDNAMES) + 1  # every column, plus the row number
        assert 'data-field="insurance"' in body and 'class="num edit"' in body
        import re

        order_id_td = re.search(r'<td class="([^"]*)"\s+data-field="order_id"', body)
        assert order_id_td and "edit" not in order_id_td.group(1)  # a key column: never editable
        cogs_td = re.search(r'<td class="([^"]*)"\s+data-field="cogs"', body)
        assert cogs_td and "edit" not in cogs_td.group(1)  # a formula: never editable
        assert '<tr class="status-shipped' in body and '<tr class="status-paid' in body
        assert "double-click a cell to edit" in body
        assert "/static/edit.js" in body

    def test_an_edit_writes_the_sheet_and_returns_the_fresh_cell(self, sheet, tmp_path):
        client = self._client(sheet, tmp_path)
        response = client.post("/orders/cell", data={**KEY, "field": "insurance", "value": "9.5",
                                                      "expected": "6.4"})
        assert response.status_code == 200
        assert sheet.writes == [(f"{_COL['insurance']}2", [[9.5]], "RAW")]
        assert "$9.50" in response.text and "data-error" not in response.text
        assert 'data-raw="9.5"' in response.text
        # The reader re-read after the write: the page now shows the new value.
        assert "$9.50" in client.get("/orders").text

    def test_a_refused_edit_comes_back_in_the_cell_with_the_reason(self, sheet, tmp_path):
        client = self._client(sheet, tmp_path)
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

    def test_the_snapshot_backend_is_view_only(self, tmp_path):
        import csv

        path = tmp_path / "sheet_backup_20260917T000000Z.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            w = csv.writer(handle)
            w.writerow(HEADER)
            w.writerow(row(order_id="X", order_date="2026-09-01", item_name="T", shipment="1",
                           status="shipped", total_cost="1"))
        from config.settings import settings

        app = create_app(SnapshotReader(path), logs_dir=tmp_path, failures_dir=tmp_path,
                         backup_dir=tmp_path / "b", repo_root_dir=tmp_path, clock=lambda: NOW,
                         settings=dataclasses.replace(settings, container_run_interval_hours=6))
        client = TestClient(app)
        body = client.get("/orders").text
        assert "view only (snapshot backend)" in body and 'class="num edit"' not in body
        response = client.post("/orders/cell", data={"order_id": "X", "order_date": "2026-09-01",
                                                      "item_name": "T", "shipment": "1",
                                                      "field": "insurance", "value": "1"})
        assert "editing is off" in response.text


class TestTheWritePathIsSingular:
    def test_only_ledger_writer_names_the_write_scope_or_a_write_method(self):
        """web/ may write the Sheet in exactly one file. Everything else in web/ is scanned by
        tests/test_web.py's read-only guarantee; this pins the exemption to that one file."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "web"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name == "ledger_writer.py":
                assert "SCOPES" in text and "worksheet.update(" in text
                assert "append_row" not in text and "batch_update" not in text
                assert "add_worksheet" not in text  # an edit never creates a tab
            else:
                assert "worksheet.update(" not in text, path.name
