"""The SQLite copy of the ledger (ledger_db/): schema derived from the ledger's own definitions,
the mirror's typing, the replace-in-one-transaction rule, and the two writers (the script and the
dashboard's `db` backend). All offline, over CSV snapshots and temporary files."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.order import FIELDNAMES
from sheets.ledger_sync import HEADER, _cogs_formula, _profit_formula
from ledger_db.mirror import mirror_snapshot, typed_record
from ledger_db.store import KEY_FIELDS, LedgerDb, columns, ledger_rows_ddl, sql_type
from web.ledger_reader import DbReader, LedgerRow, SnapshotReader


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


@pytest.fixture
def snapshot_path(tmp_path):
    return write_snapshot(tmp_path / "sheet_backup_20260917T000000Z.csv", *ROWS,
                          row(item_name="a note with no Order ID"))


@pytest.fixture
def db(tmp_path):
    return LedgerDb(tmp_path / "ledger.sqlite3")


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


class TestTyping:
    def test_typed_record_uses_the_upserts_own_coercion(self):
        r = LedgerRow(cells=dict(zip(FIELDNAMES, ROWS[0])), row_number=2)
        rec = typed_record(r)
        assert rec["total_cost"] == 1000.0 and rec["cost_per_item"] == 1000.0
        assert rec["cashback_rate"] == 0.04
        assert rec["quantity"] == 1 and isinstance(rec["quantity"], int)
        assert rec["shipment"] == 1
        assert rec["tracking_submitted"] is True
        assert rec["package_id"] == "00009999990206101794"
        assert rec["sheet_row"] == 2

    def test_formula_columns_store_the_computed_number(self):
        r = LedgerRow(cells=dict(zip(FIELDNAMES, ROWS[0])), row_number=2)
        rec = typed_record(r)
        assert rec["cogs"] == 960.0
        assert rec["total_profit"] == round(1230 - 960 - 6.4, 2)

    def test_blank_and_money_free_cells_store_null(self):
        r = LedgerRow(cells=dict(zip(FIELDNAMES, ROWS[2])), row_number=4)
        rec = typed_record(r)
        assert rec["total_cost"] is None and rec["cogs"] is None and rec["total_profit"] is None
        assert rec["tracking_submitted"] is None


class TestMirror:
    def test_mirror_replaces_everything_in_one_go_and_logs_the_run(self, snapshot_path, db):
        snapshot = SnapshotReader(snapshot_path).load()
        summary = mirror_snapshot(snapshot, db)

        assert summary["rows"] == 3 and summary["skipped"] == 1 and summary["header_ok"] is True
        assert db.row_count() == 3
        last = db.last_mirror()
        assert last["rows"] == 3 and last["backend"] == "snapshot" and last["skipped"] == 1
        assert last["source"] == str(snapshot_path)

        # A second mirror of a smaller source leaves nothing from the first behind.
        smaller = SnapshotReader(write_snapshot(snapshot_path.parent / "s2.csv", ROWS[1])).load()
        mirror_snapshot(smaller, db)
        rows = db.fetch_rows()
        assert [r["order_id"] for r in rows] == ["1399000017"]
        assert db.last_mirror()["id"] == 2

    def test_a_failed_write_leaves_the_previous_copy_intact(self, snapshot_path, db):
        mirror_snapshot(SnapshotReader(snapshot_path).load(), db)
        # Two records with the same primary key violate the constraint mid-transaction.
        bad = [{"order_id": "dup", "order_date": "d", "item_name": "i", "shipment": 1}] * 2
        with pytest.raises(sqlite3.IntegrityError):
            db.replace_rows(bad, backend="test", source="test")
        assert db.row_count() == 3

    def test_fetch_returns_typed_values_in_sheet_order(self, snapshot_path, db):
        mirror_snapshot(SnapshotReader(snapshot_path).load(), db)
        rows = db.fetch_rows()
        assert [r["sheet_row"] for r in rows] == [2, 3, 4]
        assert rows[0]["total_cost"] == 1000.0 and rows[0]["tracking_submitted"] == 1
        assert rows[0]["package_id"] == "00009999990206101794"
        assert rows[2]["total_cost"] is None

    def test_health(self, snapshot_path, db):
        assert db.health()["db_exists"] is False
        mirror_snapshot(SnapshotReader(snapshot_path).load(), db)
        health = db.health()
        assert health["db_exists"] is True and health["db_rows"] == 3
        assert health["db_last_mirror"]["rows"] == 3


class TestDbReader:
    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    def test_serves_the_db_and_refreshes_from_upstream_on_the_interval(self, snapshot_path, db):
        upstream = SnapshotReader(snapshot_path)
        loads = []
        original = upstream.load
        upstream.load = lambda force=False: (loads.append(force), original(force))[1]
        clock = self.Clock()
        reader = DbReader(db, upstream=upstream, ttl_seconds=300, clock=clock)

        snapshot = reader.load()
        assert snapshot.backend == "db" and len(snapshot.rows) == 3
        assert loads == [True]
        assert snapshot.rows[0].total_cost == 1000.0
        assert snapshot.rows[0].tracking_submitted is True
        assert snapshot.rows[0].cogs == 960.0 and snapshot.rows[0].is_committed
        assert snapshot.rows[1].is_settled and snapshot.rows[1].profit == 136.0
        assert snapshot.rows[2].is_money_free and snapshot.rows[2].cogs is None
        assert snapshot.rows[0].text("package_id") == "00009999990206101794"
        assert "mirrored from" in snapshot.source

        clock.now = 100
        reader.load()
        assert loads == [True]  # inside the interval: served from the file
        clock.now = 301
        reader.load()
        assert loads == [True, True]
        reader.load(force=True)
        assert loads == [True, True, True]

    def test_a_fresh_copy_on_disk_is_served_without_re_mirroring(self, snapshot_path, db):
        mirror_snapshot(SnapshotReader(snapshot_path).load(), db)  # "another process" filled it
        upstream = SnapshotReader(snapshot_path)
        loads = []
        upstream.load = lambda force=False: loads.append(force)
        reader = DbReader(db, upstream=upstream, ttl_seconds=300)

        snapshot = reader.load()
        assert len(snapshot.rows) == 3 and loads == []

    def test_a_stale_copy_on_disk_is_re_mirrored(self, snapshot_path, db):
        mirror_snapshot(SnapshotReader(snapshot_path).load(), db)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        with db.connect() as conn:
            conn.execute('UPDATE "mirror_runs" SET "at" = ?', (old,))
        upstream = SnapshotReader(snapshot_path)
        reader = DbReader(db, upstream=upstream, ttl_seconds=300)

        reader.load()
        assert db.last_mirror()["id"] == 2

    def test_no_upstream_serves_the_file_as_is(self, snapshot_path, db):
        mirror_snapshot(SnapshotReader(snapshot_path).load(), db)
        reader = DbReader(db, upstream=None)
        assert len(reader.load().rows) == 3
        assert reader.health()["db_mirror_upstream"] is None

    def test_health(self, snapshot_path, db):
        reader = DbReader(db, upstream=SnapshotReader(snapshot_path), ttl_seconds=300,
                          clock=self.Clock())
        health = reader.health()
        assert health["backend"] == "db" and health["db_exists"] is False
        reader.load()
        health = reader.health()
        assert health["db_rows"] == 3 and health["db_mirror_age_seconds"] == 0.0
        assert health["db_mirror_upstream"] == "snapshot"


class TestMirrorScript:
    def test_from_snapshot_into_a_named_db(self, snapshot_path, tmp_path, capsys):
        from scripts.mirror_sheet_to_db import main

        target = tmp_path / "out.sqlite3"
        assert main(["--from-snapshot", str(snapshot_path), "--db", str(target), "--force"]) == 0
        assert LedgerDb(target).row_count() == 3
        assert "Mirrored 3 row(s) from snapshot" in capsys.readouterr().err

    def test_default_path_comes_from_settings(self, monkeypatch, snapshot_path, tmp_path):
        import dataclasses

        import scripts.mirror_sheet_to_db as script
        from config import settings as settings_module

        monkeypatch.setattr(settings_module, "settings", dataclasses.replace(
            settings_module.settings, ledger_db_path=str(tmp_path / "from_settings.sqlite3")))
        assert script.main(["--from-snapshot", str(snapshot_path), "--force"]) == 0
        assert (tmp_path / "from_settings.sqlite3").is_file()


class TestFactory:
    def test_db_source_mirrors_from_the_sheet_unless_a_snapshot_is_named(self, tmp_path):
        import dataclasses

        from config.settings import settings
        from web.ledger_reader import SheetReader, reader_from_settings

        base = dataclasses.replace(settings, web_ledger_source="db", ledger_backend="sheet",
                                   ledger_db_path=str(tmp_path / "x.sqlite3"),
                                   web_sheet_cache_ttl_seconds=120)
        reader = reader_from_settings(base)
        assert isinstance(reader, DbReader) and isinstance(reader.upstream, SheetReader)
        assert reader.ttl_seconds == 120.0 and reader.db.path == tmp_path / "x.sqlite3"

        dev = reader_from_settings(base, snapshot_path="data/sheet_backup_x.csv")
        assert isinstance(dev.upstream, SnapshotReader)
