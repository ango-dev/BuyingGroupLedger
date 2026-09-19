"""Hand-edited cells are protected (ledger_db/hand_edits, 2026-09-18): the dashboard's writer
records what it writes, and the scraper upsert, the order-level reproration and the buying-group
sync all leave those cells alone."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ledger_db import hand_edits
from ledger_db.store import LedgerDb
from ledger_db.worksheet import DbWorksheet
from models.order import FIELDNAMES
from ledger import sync as ledger_sync
from ledger.sync import HEADER, _COL
from web.ledger_writer import LedgerCellWriter

KEY = {"order_id": "X1", "order_date": "2026-09-01", "item_name": "Thing", "shipment": "1"}
KEYT = ("X1", "2026-09-01", "Thing", "1")


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
    ws = DbWorksheet(db)
    ws.update(range_name="A2", values=[row(order_id="X1", order_date="2026-09-01", item_name="Thing",
                                           shipment="1", status="ordered", retailer="Costco",
                                           quantity="1", cost_per_item="100", total_cost="100",
                                           cashback_rate="0.04", card_last4="4351")])
    return ws


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


class TestTheRecord:
    def test_record_protect_and_forget(self, db):
        assert hand_edits.protected(db) == {}
        hand_edits.record(db, KEY, "cashback_rate", 0.06)
        hand_edits.record(db, KEY, "cashback_rate", 0.07)  # again: same cell, new value
        hand_edits.record(db, KEY, "insurance", "")
        assert hand_edits.protected(db) == {KEYT: {"cashback_rate", "insurance"}}
        assert [e["value"] for e in hand_edits.entries(db) if e["field"] == "cashback_rate"] == ["0.07"]
        assert hand_edits.forget(db, KEY, "insurance") == 1
        assert hand_edits.protected(db) == {KEYT: {"cashback_rate"}}
        assert hand_edits.forget(db, KEY) == 1 and hand_edits.protected(db) == {}

    def test_key_fields_and_unknown_fields_are_never_recorded(self, db):
        hand_edits.record(db, KEY, "order_id", "Y")
        hand_edits.record(db, KEY, "not_a_field", "Y")
        assert hand_edits.protected(db) == {}

    def test_keys_are_normalised_and_read_off_a_row(self, db):
        hand_edits.record(db, {"order_id": " X1 ", "order_date": "2026-09-01", "item_name": "Thing",
                               "shipment": 1}, "insurance", 5)
        assert hand_edits.key_of_row(row(order_id="X1", order_date="2026-09-01", item_name="Thing",
                                         shipment=1)) == KEYT
        assert hand_edits.protected_fields(DbWorksheet(db)) == {KEYT: {"insurance"}}
        assert hand_edits.protected_fields(object()) == {}  # a fake worksheet: nothing recorded

    def test_forget_by_order(self, db):
        hand_edits.record(db, KEY, "cashback_rate", 0.06)
        hand_edits.record(db, {**KEY, "shipment": "2"}, "cashback_rate", 0.06)
        hand_edits.record(db, {**KEY, "shipment": "2"}, "insurance", 1)
        assert hand_edits.forget_order(db, "X1", "cashback_rate") == 2
        assert hand_edits.forget_order(db, "X1") == 1


class TestThePreviousValue:
    def test_an_old_table_gains_the_column_on_open(self, tmp_path):
        import sqlite3

        from ledger_db.store import LedgerDb

        path = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "hand_edits" ("order_id" TEXT NOT NULL, "order_date" TEXT NOT NULL, '
                     '"item_name" TEXT NOT NULL, "shipment" TEXT NOT NULL, "field" TEXT NOT NULL, "value" TEXT, '
                     '"edited_at" TEXT NOT NULL, PRIMARY KEY ("order_id", "order_date", "item_name", "shipment", "field"))')
        conn.execute('INSERT INTO "hand_edits" VALUES ("X1", "2026-09-01", "T", "1", "insurance", "2", "2026-09-18T00:00:00+00:00")')
        conn.commit()
        conn.close()
        db = LedgerDb(path)
        with db.connect() as c:
            assert "previous" in {r["name"] for r in c.execute('PRAGMA table_info("hand_edits")')}
        assert hand_edits.previous_value(db, ("X1", "2026-09-01", "T", "1"), "insurance") == ""  # unknown before: treated as blank
        assert hand_edits.previous_value(db, ("X1", "2026-09-01", "T", "1"), "cogs") is None


class TestTheWriterRecords:
    def test_a_cell_edit_a_bulk_edit_and_a_deletion(self, ws, db, logs_dir):
        writer = LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir)
        writer.write_cell(KEY, "cashback_rate", "6%", expected="0.04")
        assert hand_edits.protected(db) == {KEYT: {"cashback_rate"}}
        assert hand_edits.previous_value(db, KEY, "cashback_rate") == "0.04"  # what the run had written
        writer.write_cell(KEY, "cashback_rate", "7%", expected="0.06")
        assert hand_edits.previous_value(db, KEY, "cashback_rate") == "0.04"  # the FIRST edit's before, kept
        writer.write_cells([KEY, {**KEY, "order_id": "missing"}], "insurance", "2.5")
        assert hand_edits.protected(db) == {KEYT: {"cashback_rate", "insurance"}}
        assert hand_edits.previous_value(db, KEY, "insurance") == ""  # the cell was blank before
        # clearing a cell that was BLANK before the hand edit clears it, and releases it
        cleared = writer.write_cell(KEY, "insurance", "", expected="2.5")
        assert cleared["value"] == "" and cleared["restored"] is False
        assert hand_edits.protected(db) == {KEYT: {"cashback_rate"}}
        # clearing a cell the run had filled puts the run's value BACK, and releases it
        back = writer.write_cell(KEY, "cashback_rate", "", expected="0.07")
        assert back["value"] == "0.04" and back["restored"] is True
        assert hand_edits.protected(db) == {}
        assert LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir).write_cell(KEY, "cashback_rate", "0.05", expected="0.04")
        # the bulk path restores the same way
        assert hand_edits.previous_value(db, KEY, "cashback_rate") == "0.04"
        writer.write_cells([KEY], "cashback_rate", "")
        assert hand_edits.protected(db) == {}
        assert ws.get_all_values()[1][FIELDNAMES.index("cashback_rate")] == "0.04"
        writer.write_cell(KEY, "cashback_rate", "0.05")
        writer.remove_rows([KEY])
        assert hand_edits.protected(db) == {}

    def test_a_hand_added_row_is_protected_where_it_was_typed(self, ws, db, logs_dir):
        writer = LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir)
        writer.add_row({"order_id": "H1", "order_date": "2026-09-02", "item_name": "Typed",
                        "shipment": "1", "status": "ordered", "quantity": "1", "cost_per_item": "50",
                        "retailer": "Costco"})
        fields = hand_edits.protected(db).get(("H1", "2026-09-02", "Typed", "1"), set())
        assert {"status", "quantity", "cost_per_item", "retailer"} <= fields
        assert not fields & {"order_id", "order_date", "item_name", "shipment"}


class TestTheRunKeepsThem:
    def test_the_upsert_keeps_a_hand_typed_rate_and_still_moves_the_status(self, ws, db, tmp_path,
                                                                            monkeypatch, logs_dir):
        monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: ws)
        LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir).write_cell(KEY, "cashback_rate", "0.06")
        # the next run re-reads the order: the card's configured rate and a new status
        csv_path = write_csv_file(tmp_path / "orders.csv", {
            **KEY, "status": "shipped", "retailer": "Costco", "quantity": "1", "cost_per_item": "100",
            "total_cost": "100", "cashback_rate": "0.04", "tracking_number": "1Z1", "card_last4": "4351",
        })
        result = ledger_sync.sync_csv_to_ledger(csv_path)
        assert result["updated"] == 1
        stored = db.fetch_rows()[0]
        assert stored["cashback_rate"] == 0.06, "typed by hand: the run does not get a say"
        assert stored["status"] == "shipped" and stored["tracking_number"] == "1Z1"
        assert stored["cogs"] == 94.0  # COGS follows the kept rate

    def test_merge_row_keeps_protected_fields_even_against_a_real_value(self):
        old = row(order_id="X1", status="ordered", cashback_rate="0.06", tracking_number="")
        new = row(order_id="X1", status="shipped", cashback_rate="0.04", tracking_number="1Z1")
        merged = ledger_sync._merge_row(old, new, protected={"cashback_rate"})
        by = dict(zip(FIELDNAMES, merged))
        assert by["cashback_rate"] == "0.06" and by["status"] == "shipped" and by["tracking_number"] == "1Z1"
        assert dict(zip(FIELDNAMES, ledger_sync._merge_row(old, new)))["cashback_rate"] == "0.04"

    def test_the_reproration_skips_a_hand_typed_share(self, ws, db, logs_dir):
        ws.update(range_name="A3", values=[row(order_id="X1", order_date="2026-09-01", item_name="Other",
                                               shipment="2", status="ordered", quantity="1",
                                               cost_per_item="300", total_cost="300")])
        LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir).write_cell(KEY, "shipping", "9.99")
        ledger_sync._reprorate_order_level(ws, {"X1"}, {"shipping": {"X1": 40.0}})
        by_shipment = {r["shipment"]: r for r in db.fetch_rows()}
        assert by_shipment[1]["shipping"] == 9.99      # typed: kept
        assert by_shipment[2]["shipping"] == 30.0      # 300/400 of 40

    def test_the_sync_drops_a_hand_typed_payout_from_its_writes(self, ws, db, logs_dir):
        import sync_tracking

        LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir).write_cell(KEY, "payout_amount", "123")
        plan = {"key_by_row": {2: KEYT, 3: ("Y1", "d", "i", "1")}}
        writes = {2: {"Actual Payout": 130.0, "Insurance": 1.5, "Payout Date": "2026-09-10"},
                  3: {"Actual Payout": 50.0}}
        kept = sync_tracking._drop_protected(writes, plan, ws)
        assert kept == {2: {"Insurance": 1.5, "Payout Date": "2026-09-10"}, 3: {"Actual Payout": 50.0}}
        assert sync_tracking._drop_protected(writes, plan, object()) is writes  # no record: untouched

    def test_the_plan_carries_every_rows_key(self):
        from sync_tracking import plan_tracking_submissions

        plan = plan_tracking_submissions(list(HEADER), [
            row(order_id="O1", order_date="2026-08-01", item_name="Widget", shipment="1", status="shipped",
                tracking_number="T1", quantity="1", total_cost="100", buying_group="BFMR"),
            row(order_id="O2", order_date="2026-08-02", item_name="Gadget", shipment="1", status="ordered",
                quantity="1", total_cost="100", buying_group="BFMR"),
        ])
        assert plan["key_by_row"] == {2: ("O1", "2026-08-01", "Widget", "1"), 3: ("O2", "2026-08-02", "Gadget", "1")}


class TestThePageShowsIt:
    def test_a_protected_cell_is_marked(self, ws, db, logs_dir, tmp_path):
        import dataclasses
        from datetime import datetime, timezone

        from fastapi.testclient import TestClient

        from config.settings import settings
        from web.app import create_app
        from web.ledger_reader import DbReader

        LedgerCellWriter(opener=lambda: ws, logs_dir=logs_dir).write_cell(KEY, "cashback_rate", "0.06")
        (logs_dir / "failures").mkdir()
        app = create_app(DbReader(db), logs_dir=logs_dir, failures_dir=logs_dir / "failures",
                         clock=lambda: datetime(2026, 9, 18, 12, tzinfo=timezone.utc),
                         settings=dataclasses.replace(settings, container_run_interval_hours=6))
        body = TestClient(app).get("/orders").text
        i = body.index('class="num edit hand"')
        assert 'data-field="cashback_rate"' in body[i:i + 80]
        assert "typed by hand: runs keep this value" in body
        assert body.count(' hand"') == 1


class TestTheCli:
    def test_list_and_forget(self, db, monkeypatch, capsys):
        import dataclasses

        import config.settings as cs
        from scripts.hand_edits import main

        monkeypatch.setattr(cs, "settings", dataclasses.replace(cs.settings,
                                                                ledger_db_path=str(db.path)))
        assert main([]) == 0 and "No hand-edited cells" in capsys.readouterr().out
        hand_edits.record(db, KEY, "cashback_rate", 0.06)
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "order X1" in out and "cashback_rate = '0.06'" in out
        assert main(["--forget", "X1", "--field", "nope"]) == 2
        assert main(["--forget", "X1", "--field", "cashback_rate"]) == 0
        assert "Released 1" in capsys.readouterr().out and hand_edits.protected(db) == {}
