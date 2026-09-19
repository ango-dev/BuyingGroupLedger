"""The SQLite ledger behind a worksheet face (ledger_db/worksheet.py), and the opener that routes
every writer to it.

Offline: the adapter is exercised directly, then the REAL upsert (ledger.sync
.sync_csv_to_ledger), the sort, the dashboard's cell writer and the tracking sync's read all run
against it unchanged -- which is the whole point of the adapter.
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

import pytest

from config.settings import Settings
from ledger_db.store import LedgerDb
from ledger_db.worksheet import DbWorksheet, ReadOnly, parse_a1
from models.order import FIELDNAMES
from ledger import sync as ledger_sync
from ledger.sync import HEADER


def row(**values):
    return [values.get(f, "") for f in FIELDNAMES]


def write_csv_file(path: Path, *records) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for record in records:
            writer.writerow({f: record.get(f, "") for f in FIELDNAMES})
    return path


@pytest.fixture
def db(tmp_path):
    return LedgerDb(tmp_path / "ledger.sqlite3")


@pytest.fixture
def ws(db):
    return DbWorksheet(db)


def col(field: str) -> str:
    return ledger_sync._COL[field]


OID = FIELDNAMES.index("order_id")


class TestTheGrid:
    def test_an_empty_database_is_a_header_row(self, ws):
        assert ws.get_all_values() == [list(HEADER)]
        assert ws.col_count == len(HEADER) and ws.row_count > 1 and ws.title == "ledger.sqlite3"

    def test_parse_a1(self):
        assert parse_a1("A12") == (12, 1)
        assert parse_a1("AB3") == (3, 28)
        assert parse_a1("Orders!C4:C4") == (4, 3)
        with pytest.raises(ValueError):
            parse_a1("nope")

    def test_update_writes_a_block_and_persists_it(self, ws, db):
        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="shipped",
                                              quantity=2, total_cost=100.0, cashback_rate=0.04)])
        grid = ws.get_all_values()
        assert grid[1][FIELDNAMES.index("order_id")] == "X1"
        assert grid[1][FIELDNAMES.index("quantity")] == "2"
        # the file holds it, typed, and a fresh adapter sees the same grid
        stored = db.fetch_rows()
        assert stored[0]["order_id"] == "X1" and stored[0]["quantity"] == 2
        assert stored[0]["total_cost"] == 100.0 and stored[0]["sheet_row"] == 2
        assert DbWorksheet(db).get_all_values() == grid

    def test_the_formula_columns_are_computed_never_stored(self, ws, db):
        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="paid",
                                              total_cost=1000.0, cashback_rate=0.04, insurance=6.4,
                                              payout_amount=1230.0)])
        # the sync stamps formula TEXT into the two columns; it is ignored, the number is live
        ws.batch_update([{"range": f"{col('cogs')}2", "values": [["=IF(1,2,3)"]]},
                         {"range": f"{col('total_profit')}2", "values": [["=X2-Y2"]]}],
                        value_input_option="USER_ENTERED")
        grid = ws.get_all_values()
        assert grid[1][FIELDNAMES.index("cogs")] == "960"
        assert grid[1][FIELDNAMES.index("total_profit")] == "263.6"
        assert db.fetch_rows()[0]["cogs"] == 960.0 and db.fetch_rows()[0]["total_profit"] == 263.6
        stored = ws.get_values(value_render_option="UNFORMATTED_VALUE")
        assert stored[1][FIELDNAMES.index("total_profit")] == 263.6

    def test_none_skips_and_user_entered_blank_clears(self, ws):
        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="shipped",
                                              tracking_number="1Z999", total_cost=50.0)])
        tracking = FIELDNAMES.index("tracking_number")
        full = [None] * len(FIELDNAMES)
        full[FIELDNAMES.index("status")] = "delivered"
        ws.update(range_name="A2", values=[full])  # RAW: None leaves every other cell alone
        assert ws.get_all_values()[1][tracking] == "1Z999"
        assert ws.get_all_values()[1][FIELDNAMES.index("status")] == "delivered"
        ws.batch_update([{"range": f"{col('tracking_number')}2", "values": [[""]]}],
                        value_input_option="USER_ENTERED")
        assert ws.get_all_values()[1][tracking] == ""

    def test_formatted_and_unformatted_reads(self, ws):
        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="shipped",
                                              tracking_submitted="TRUE", total_cost="$1,299.99",
                                              cashback_rate="4%")])
        formatted = ws.get_values(value_render_option="FORMATTED_VALUE")[1]
        stored = ws.get_values(value_render_option="UNFORMATTED_VALUE")[1]
        assert formatted[FIELDNAMES.index("tracking_submitted")] == "TRUE"
        assert stored[FIELDNAMES.index("tracking_submitted")] is True
        assert formatted[FIELDNAMES.index("total_cost")] == "1299.99"
        assert stored[FIELDNAMES.index("total_cost")] == 1299.99
        assert stored[FIELDNAMES.index("cashback_rate")] == 0.04
        assert stored[OID] == "X1" and formatted[FIELDNAMES.index("payout_amount")] == ""

    def test_a_blank_cell_reads_as_an_empty_string_in_every_render(self, ws):
        """A blank cell reads as "" whatever the render option (the worksheet contract); the readers do
        str(cell).strip(). The first run after the cutover (2026-09-18) read None here, turned it
        into the text "None", and handed six tracking-less rows to BFMR as the package "None"."""
        from ledger_db.worksheet import ValueRenderOption

        from sync_tracking import plan_tracking_submissions

        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="ordered",
                                              buying_group="BFMR", total_cost=50.0, quantity=1)])
        stored = ws.get_values(value_render_option=ValueRenderOption.unformatted)
        tracking = FIELDNAMES.index("tracking_number")
        assert stored[1][tracking] == "" and stored[1][FIELDNAMES.index("payout_amount")] == ""
        assert "None" not in [str(v) for v in stored[1]]
        assert "None" not in [str(v) for v in ws.get_all_values()[1]]
        assert "None" not in [str(v) for v in ws.data_rows()[0]]
        # the real planner sees a row with no tracking number, not a package called "None"
        plan = plan_tracking_submissions(stored[0], stored[1:])
        assert plan["skipped_no_tracking"] == 1 and plan["rows_by_tracking"] == {}

    def test_sort_is_the_grid_sort_with_blanks_last(self, ws):
        ws.update(range_name="A2", values=[
            row(order_id="B", order_date="2026-08-01", item_name="b", shipment="1", status="paid"),
            row(order_id="A", order_date="2026-09-01", item_name="a", shipment="2", status="paid"),
            row(order_id="A", order_date="2026-09-01", item_name="a", shipment="1", status="paid"),
        ])
        date = FIELDNAMES.index("order_date") + 1
        oid = FIELDNAMES.index("order_id") + 1
        ship = FIELDNAMES.index("shipment") + 1
        ws.sort((date, "des"), (oid, "asc"), (ship, "asc"), range="A2:AB4")
        assert [(r[OID], r[FIELDNAMES.index("shipment")]) for r in ws.get_all_values()[1:]] == [
            ("A", "1"), ("A", "2"), ("B", "1")]
        assert [r["sheet_row"] for r in ws.db.fetch_rows()] == [2, 3, 4]

    def test_delete_rows_and_rows_without_an_order_id(self, ws, db):
        ws.update(range_name="A2", values=[
            row(order_id="A", order_date="2026-09-01", item_name="a", shipment="1", status="paid"),
            row(order_id="B", order_date="2026-09-01", item_name="b", shipment="1", status="paid"),
        ])
        ws.delete_rows(2)
        assert [r[OID] for r in ws.get_all_values()[1:]] == ["B"]
        assert db.fetch_rows()[0]["sheet_row"] == 2
        with pytest.raises(ValueError):
            ws.delete_rows(1)
        # a note row (no Order ID) is not a ledger row and is never stored
        ws.update(range_name="A3", values=[row(item_name="-- a note --")])
        assert len(db.fetch_rows()) == 1 and len(ws.get_all_values()) == 2

    def test_read_only_refuses_every_write(self, db):
        DbWorksheet(db).update(range_name="A2", values=[row(order_id="A", order_date="2026-09-01",
                                                            item_name="a", shipment="1", status="paid")])
        view = DbWorksheet(db, read_only=True)
        assert view.get_all_values()[1][OID] == "A"
        for call in (lambda: view.update(range_name="A2", values=[["Z"]]),
                     lambda: view.batch_update([{"range": "A2", "values": [["Z"]]}]),
                     lambda: view.delete_rows(2), lambda: view.sort((1, "asc")),
                     lambda: view.append_row(["Z"])):
            with pytest.raises(ReadOnly):
                call()
        assert DbWorksheet(db).get_all_values()[1][OID] == "A"


class TestTheWritersRunOnIt:
    """The real money-path code, unchanged, against the adapter."""

    @pytest.fixture
    def on_db(self, db, monkeypatch):
        ws = DbWorksheet(db)
        monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: ws)
        return ws

    def test_the_upsert_appends_updates_and_stamps_computed_formulas(self, on_db, db, tmp_path):
        csv_path = write_csv_file(tmp_path / "orders.csv",
                                  {"order_id": "X1", "order_date": "2026-09-01", "item_name": "Thing",
                                   "shipment": "1", "status": "ordered", "retailer": "Costco",
                                   "quantity": "1", "cost_per_item": "100", "total_cost": "100",
                                   "cashback_rate": "0.04", "tracking_submitted": "FALSE"})
        result = ledger_sync.sync_csv_to_ledger(csv_path)
        assert result["appended"] == 1 and result["updated"] == 0
        stored = db.fetch_rows()
        assert len(stored) == 1 and stored[0]["status"] == "ordered" and stored[0]["cogs"] == 96.0
        assert stored[0]["total_profit"] is None  # no payout yet

        # a re-check: the row is updated in place, blanks never overwrite, the payout hand-typed
        # meanwhile survives, and Total Profit appears
        on_db.batch_update([{"range": f"{col('payout_amount')}2", "values": [[130]]}])
        csv_path = write_csv_file(tmp_path / "orders.csv",
                                  {"order_id": "X1", "order_date": "2026-09-01", "item_name": "Thing",
                                   "shipment": "1", "status": "shipped", "retailer": "Costco",
                                   "tracking_number": "1Z999"})
        result = ledger_sync.sync_csv_to_ledger(csv_path)
        assert result["appended"] == 0 and result["updated"] == 1
        stored = db.fetch_rows()
        assert len(stored) == 1
        assert stored[0]["status"] == "shipped" and stored[0]["tracking_number"] == "1Z999"
        assert stored[0]["total_cost"] == 100.0 and stored[0]["payout_amount"] == 130.0
        assert stored[0]["total_profit"] == 34.0  # 130 - 96 - 0

    def test_the_sort_reorders_the_file(self, on_db, db, tmp_path):
        csv_path = write_csv_file(
            tmp_path / "orders.csv",
            {"order_id": "OLD", "order_date": "2026-08-01", "item_name": "a", "shipment": "1",
             "status": "delivered", "retailer": "Costco", "total_cost": "10"},
            {"order_id": "NEW", "order_date": "2026-09-01", "item_name": "b", "shipment": "1",
             "status": "ordered", "retailer": "Costco", "total_cost": "20"})
        ledger_sync.sync_csv_to_ledger(csv_path)
        result = ledger_sync.sort_ledger_by_date_desc()
        assert result["sorted_rows"] == 2 and result["already_sorted"] is False
        assert [r["order_id"] for r in db.fetch_rows()] == ["NEW", "OLD"]
        assert [r["sheet_row"] for r in db.fetch_rows()] == [2, 3]

    def test_the_dashboard_writer_edits_a_cell_by_key(self, db, tmp_path):
        from web.ledger_writer import LedgerCellWriter

        ws = DbWorksheet(db)
        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="shipped",
                                              total_cost=100.0)])
        writer = LedgerCellWriter(opener=lambda: DbWorksheet(db), logs_dir=tmp_path)
        key = {"order_id": "X1", "order_date": "2026-09-01", "item_name": "Thing", "shipment": "1"}
        writer.write_cell(key, "status", "delivered", expected="shipped")
        assert db.fetch_rows()[0]["status"] == "delivered"
        writer.add_row({"order_id": "X2", "order_date": "2026-09-02", "item_name": "Other",
                        "shipment": "1", "status": "ordered", "retailer": "Costco", "quantity": "1",
                        "cost_per_item": "5"})
        assert [r["order_id"] for r in db.fetch_rows()] == ["X1", "X2"]
        writer.remove_rows([key])
        assert [r["order_id"] for r in db.fetch_rows()] == ["X2"]

    def test_the_tracking_sync_reads_typed_values(self, db):
        from ledger_db.worksheet import ValueRenderOption

        ws = DbWorksheet(db)
        ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01",
                                              item_name="Thing", shipment="1", status="shipped",
                                              tracking_number="1Z999", tracking_submitted=False,
                                              total_cost=100.0, buying_group="BFMR")])
        grid = ws.get_values(value_render_option=ValueRenderOption.unformatted)
        assert grid[0] == list(HEADER)
        assert grid[1][FIELDNAMES.index("tracking_submitted")] is False
        assert grid[1][FIELDNAMES.index("total_cost")] == 100.0


class TestTheFlag:
    """Every opener hands out the adapter over `database.path` (the only backend since 2026-09-18)."""

    def test_get_worksheet_hands_out_the_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger_sync, "settings", dataclasses.replace(
            ledger_sync.settings, ledger_db_path=str(tmp_path / "l.sqlite3")))
        ws = ledger_sync._get_worksheet()
        assert isinstance(ws, DbWorksheet) and not ws.read_only
        assert ws.db.path == tmp_path / "l.sqlite3"

    def test_the_readonly_opener_hands_out_a_readonly_adapter_under_db(self, tmp_path, monkeypatch):
        import scripts.audit_ledger as audit
        from config import settings as settings_module

        monkeypatch.setattr(settings_module, "settings", dataclasses.replace(
            settings_module.settings, ledger_db_path=str(tmp_path / "l.sqlite3")))
        ws, title = audit.open_ledger_readonly()
        assert isinstance(ws, DbWorksheet) and ws.read_only and title == "l.sqlite3"
        ws2, title2 = audit.open_ledger_readonly()
        assert isinstance(ws2, DbWorksheet) and ws2.read_only and title2 == title

    def test_the_dashboard_reads_the_database(self, tmp_path):
        from web.ledger_reader import DbReader, reader_from_settings

        settings = dataclasses.replace(Settings(), web_ledger_source="db",
                                       ledger_db_path=str(tmp_path / "l.sqlite3"))
        reader = reader_from_settings(settings)
        assert isinstance(reader, DbReader) and reader.db.path == tmp_path / "l.sqlite3"

    def test_the_dashboard_writer_opens_the_adapter_under_db(self, tmp_path, monkeypatch):
        from config import settings as settings_module
        from web import ledger_writer

        monkeypatch.setattr(settings_module, "settings", dataclasses.replace(
            settings_module.settings, ledger_db_path=str(tmp_path / "l.sqlite3")))
        assert isinstance(ledger_writer._open_for_writing(), DbWorksheet)

