"""The SQLite ledger (ledger_db/): schema derived from the ledger's own definitions, the
replace-in-one-transaction rule, and the dashboard's reader over it. All offline, over temporary
files; rows are seeded through the worksheet adapter, as every writer seeds them."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.order import FIELDNAMES
from ledger.sync import HEADER, _cogs_formula, _profit_formula
from ledger_db.store import KEY_FIELDS, LedgerDb, columns, ledger_rows_ddl, sql_type
from ledger_db.worksheet import DbWorksheet
from web.ledger_reader import DbReader


def row(**values) -> list[str]:
    return [str(values.get(f, "")) for f in FIELDNAMES]


def write_snapshot(path: Path, *rows: list[str], header=HEADER) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


ROWS = [
    row(order_date="2026-09-08", status="shipped", retailer="Best Buy", item_name="MacBook",
        shipment="1", quantity="1", order_id="BBY01-1", tracking_number="5238", tracking_submitted="True",
        buying_group="BFMR", cost_per_item="$1,000.00", total_cost="$1,000.00", shipping="0",
        cashback_rate="4%", insurance="6.4", payout_amount="1230", profile_label="profile-alpha",
        package_id="00009999990206101794", cogs=_cogs_formula(2), total_profit=_profit_formula(2)),
    row(order_date="2026-08-20", status="paid", retailer="Costco", item_name="iPad (Item #1)",
        shipment="1", quantity="2", order_id="1399000017", buying_group="MOD", cost_per_item="200",
        total_cost="400", cashback_rate="0.09", payout_amount="500", payout_date="2026-09-01",
        cogs="364", total_profit="136"),
    row(order_date="2026-08-15", status="cancelled", retailer="Costco", item_name="IPAD (Item #1)",
        shipment="1", quantity="2", order_id="1399000019", buying_group="BFMR"),
]


def seed(db: LedgerDb, *rows: list[str]) -> None:
    """Write `rows` through the worksheet adapter, exactly as the upsert would (a row without an
    Order ID is not a ledger row and is dropped on the way)."""
    DbWorksheet(db).update(range_name="A2", values=[list(r) for r in rows])


@pytest.fixture
def db(tmp_path):
    return LedgerDb(tmp_path / "ledger.sqlite3")


@pytest.fixture
def seeded(db):
    seed(db, *ROWS, row(item_name="a note with no Order ID"))
    return db


class TestSchema:
    def test_columns_are_fieldnames_in_order_plus_bookkeeping(self):
        names = [name for name, _ in columns()]
        assert names[: len(FIELDNAMES)] == FIELDNAMES
        assert names[len(FIELDNAMES):] == ["sheet_row", "mirrored_at"]

    def test_types_come_from_ledger_syncs_field_sets(self):
        assert sql_type("quantity") == "INTEGER" and sql_type("shipment") == "INTEGER"
        assert sql_type("tracking_submitted") == "INTEGER"
        assert sql_type("total_cost") == "REAL" and sql_type("cashback_rate") == "REAL"
        assert sql_type("cogs") == "REAL" and sql_type("total_profit") == "REAL"
        assert sql_type("order_date") == "TEXT" and sql_type("card_last4") == "TEXT"
        assert sql_type("package_id") == "TEXT"  # leading zeros survive

    def test_primary_key_is_the_upsert_key(self):
        assert KEY_FIELDS == ("order_id", "order_date", "item_name", "shipment")
        assert 'PRIMARY KEY ("order_id", "order_date", "item_name", "shipment")' in ledger_rows_ddl()

    def test_a_table_lacking_the_newest_column_keeps_its_rows_and_gains_it(self, tmp_path):
        """The file IS the ledger under `db`: a column appended to the schema (Expected Payout,
        2026-09-18) must never cost a row. The old table is copied into the new column order."""
        path = tmp_path / "old.sqlite3"
        old = [name for name, _ in columns() if name != "expected_payout"]
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE "ledger_rows" (' + ", ".join(f'"{n}" TEXT' for n in old) + ")")
            conn.execute(
                'INSERT INTO "ledger_rows" ("order_id", "order_date", "item_name", "shipment", '
                '"total_cost", "sheet_row") VALUES ("x", "2026-09-01", "Widget", "1", 12.5, 2)')

        db = LedgerDb(path)
        with db.connect() as conn:
            names = [r[1] for r in conn.execute('PRAGMA table_info("ledger_rows")')]
        assert names == [name for name, _ in columns()]
        rows = db.fetch_rows()
        assert len(rows) == 1 and rows[0]["order_id"] == "x" and rows[0]["total_cost"] == 12.5
        assert rows[0]["expected_payout"] is None

    def test_a_reordered_schema_carries_every_row_across_by_name(self, tmp_path):
        """Expected Payout moved from last to beside Actual Payout (2026-09-18): the file IS the
        ledger, so the table is rebuilt in the new order with every value under its own name."""
        path = tmp_path / "old.sqlite3"
        names = [name for name, _ in columns()]
        old = [n for n in names if n != "expected_payout"]
        old.insert(old.index("sheet_row"), "expected_payout")  # the old position: last field
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE "ledger_rows" (' + ", ".join(f'"{n}" TEXT' for n in old) + ")")
            conn.execute('INSERT INTO "ledger_rows" ("order_id", "order_date", "item_name", "shipment", '
                         '"payout_amount", "expected_payout", "sheet_row") VALUES ("x", "d", "i", "1", 500, 520, 2)')
        db = LedgerDb(path)
        with db.connect() as conn:
            assert [r[1] for r in conn.execute('PRAGMA table_info("ledger_rows")')] == names
        rows = db.fetch_rows()
        assert rows[0]["payout_amount"] == 500.0 and rows[0]["expected_payout"] == 520.0

    def test_an_empty_table_with_a_foreign_column_is_rebuilt(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE "ledger_rows" ("order_id" TEXT, "old_column" TEXT)')
        db = LedgerDb(path)
        with db.connect() as conn:
            names = [r[1] for r in conn.execute('PRAGMA table_info("ledger_rows")')]
        assert names == [name for name, _ in columns()] and db.row_count() == 0

    def test_a_populated_table_with_a_foreign_column_is_refused_not_dropped(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE "ledger_rows" ("order_id" TEXT, "old_column" TEXT)')
            conn.execute('INSERT INTO "ledger_rows" VALUES ("x", "y")')
        import pytest

        with pytest.raises(RuntimeError, match="old_column"):
            with LedgerDb(path).connect():
                pass
        with sqlite3.connect(path) as conn:  # untouched
            assert conn.execute('SELECT COUNT(*) FROM "ledger_rows"').fetchone()[0] == 1

    def test_relative_paths_are_under_the_repo_root(self):
        from ledger_db.store import ROOT

        assert LedgerDb("data/x.sqlite3").path == ROOT / "data" / "x.sqlite3"




class TestStore:
    def test_a_write_replaces_everything_in_one_go(self, seeded):
        assert seeded.row_count() == 3
        rows = seeded.fetch_rows()
        assert [r["order_id"] for r in rows] == ["BBY01-1", "1399000017", "1399000019"]
        # A second write of a smaller grid leaves nothing from the first behind.
        ws = DbWorksheet(seeded)
        ws.delete_rows(3, 4)
        ws.update(range_name="A2", values=[ROWS[1]])
        assert [r["order_id"] for r in seeded.fetch_rows()] == ["1399000017"]

    def test_a_failed_write_leaves_the_previous_copy_intact(self, seeded):
        # Two records with the same primary key violate the constraint mid-transaction.
        bad = [{"order_id": "dup", "order_date": "d", "item_name": "i", "shipment": 1}] * 2
        with pytest.raises(sqlite3.IntegrityError):
            seeded.replace_rows(bad, backend="test", source="test")
        assert seeded.row_count() == 3

    def test_fetch_returns_typed_values_in_grid_order(self, seeded):
        rows = seeded.fetch_rows()
        assert [r["sheet_row"] for r in rows] == [2, 3, 4]
        assert rows[0]["total_cost"] == 1000.0 and rows[0]["tracking_submitted"] == 1
        assert rows[0]["cashback_rate"] == 0.04 and rows[0]["quantity"] == 1
        assert rows[0]["package_id"] == "00009999990206101794"
        assert rows[2]["total_cost"] is None

    def test_health(self, db):
        assert db.health()["db_exists"] is False
        seed(db, *ROWS)
        health = db.health()
        assert health["db_exists"] is True and health["db_rows"] == 3


class TestDbReader:
    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    def test_serves_the_file_typed_and_computed(self, seeded):
        reader = DbReader(seeded, ttl_seconds=300, clock=self.Clock())
        snapshot = reader.load()
        assert snapshot.backend == "db" and len(snapshot.rows) == 3
        assert snapshot.source == str(seeded.path)
        assert snapshot.rows[0].total_cost == 1000.0
        assert snapshot.rows[0].tracking_submitted is True
        assert snapshot.rows[0].cogs == 960.0 and snapshot.rows[0].is_committed
        assert snapshot.rows[1].is_settled and snapshot.rows[1].profit == 136.0
        assert snapshot.rows[2].is_money_free and snapshot.rows[2].cogs is None
        assert snapshot.rows[0].text("package_id") == "00009999990206101794"

    def test_every_load_sees_the_latest_write(self, seeded):
        reader = DbReader(seeded, ttl_seconds=300, clock=self.Clock())
        assert len(reader.load().rows) == 3
        ws = DbWorksheet(seeded)
        ws.delete_rows(3, 4)
        ws.update(range_name="A2", values=[ROWS[1]])
        assert [r.order_id for r in reader.load().rows] == ["1399000017"]
        assert reader.refresh() is False  # nothing upstream to pull from

    def test_health(self, seeded):
        reader = DbReader(seeded, ttl_seconds=300, clock=self.Clock())
        health = reader.health()
        assert health["backend"] == "db" and health["db_exists"] is True
        assert health["db_rows"] == 3 and health["db_cache_ttl_seconds"] == 300.0


class TestFactory:
    def test_db_source_serves_the_file_named_by_settings(self, tmp_path):
        import dataclasses

        from config.settings import settings
        from web.ledger_reader import SnapshotReader, reader_from_settings

        base = dataclasses.replace(settings, web_ledger_source="db",
                                   ledger_db_path=str(tmp_path / "x.sqlite3"),
                                   web_ledger_cache_ttl_seconds=120)
        reader = reader_from_settings(base)
        assert isinstance(reader, DbReader)
        assert reader.ttl_seconds == 120.0 and reader.db.path == tmp_path / "x.sqlite3"

        dev = reader_from_settings(base, source="snapshot", snapshot_path="data/ledger_backup_x.csv")
        assert isinstance(dev, SnapshotReader)
