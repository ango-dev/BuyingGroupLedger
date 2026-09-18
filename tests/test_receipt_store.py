"""The receipt store is a directory beside the ledger (receipts/store.py, 2026-09-18: OCI removed)
and the links it writes are dashboard-relative; plus the one-time migration off the bucket."""
from __future__ import annotations

import dataclasses

import pytest

from receipts import store
from scripts import migrate_receipts_local as mig

KEY = "receipts/bestbuy/2026-09/BBY01-1.pdf"


@pytest.fixture
def on(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "settings", dataclasses.replace(
        store.settings, receipt_capture_enabled=True, receipts_dir=str(tmp_path / "receipts")))
    return tmp_path / "receipts"


class TestInertWhenOff:
    def test_off_means_no_op_everywhere(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "settings", dataclasses.replace(
            store.settings, receipt_capture_enabled=False, receipts_dir=str(tmp_path)))
        assert not store.is_configured() and store.missing_settings() == []
        assert store.exists(KEY) is False and store.put(KEY, b"x", "pdf") == "" and store.link_for(KEY) == ""
        assert not list(tmp_path.rglob("*"))


class TestTheDirectory:
    def test_put_exists_link_and_path(self, on):
        assert store.exists(KEY) is False
        link = store.put(KEY, b"%PDF-1.4 x", "pdf")
        assert link == "/receipts/bestbuy/2026-09/BBY01-1.pdf"
        assert store.exists(KEY) and store.path_for(KEY) == on / "bestbuy" / "2026-09" / "BBY01-1.pdf"
        assert store.path_for(KEY).read_bytes() == b"%PDF-1.4 x"
        assert store.path_for_link(link) == store.path_for(KEY)
        assert store.path_for_link("https://elsewhere/x.pdf") is None and store.path_for_link("") is None
        assert store.link_for("/receipts/amazon/2026-08/1.png") == "/receipts/amazon/2026-08/1.png"
        assert not list(on.rglob("*.part"))

    def test_an_empty_body_is_refused(self, on):
        with pytest.raises(store.ReceiptStoreError, match="empty"):
            store.put(KEY, b"", "pdf")

    def test_a_climbing_key_is_refused(self, on):
        for bad in ("receipts/../../etc/passwd", "receipts/a/..\\b/c.pdf", "", "receipts/./x"):
            with pytest.raises(store.ReceiptStoreError):
                store.path_for(bad)
        assert store.exists("receipts/../x") is False and store.delete("receipts/../x") is False
        assert store.path_for_link("/receipts/../secret") is None

    def test_stored_keys_and_delete(self, on):
        store.put(KEY, b"a", "pdf")
        store.put("receipts/amazon/2026-08/113-1.png", b"b", "png")
        (on / "_probe.txt").parent.mkdir(exist_ok=True)
        (on / "_probe.txt").write_bytes(b"x")  # not <retailer>/<month>/<file>
        assert store.stored_keys() == ["receipts/amazon/2026-08/113-1.png", KEY]
        assert store.stored_keys("bestbuy") == [KEY]
        assert store.delete(KEY) is True and store.delete(KEY) is False and store.stored_keys("bestbuy") == []

    def test_a_relative_dir_is_under_the_repo_root(self, monkeypatch):
        monkeypatch.setattr(store, "settings", dataclasses.replace(store.settings, receipts_dir="data/receipts"))
        assert store.receipts_dir() == store.ROOT / "data" / "receipts"


class TestTheMigrationOffOci:
    HEADER = ["Order ID", "Receipt Link"]

    def test_the_key_is_read_off_the_par_link(self):
        assert mig.key_from_link("https://objectstorage.us-x.oraclecloud.com/p/TOK/n/ns/b/b/o/receipts/bestbuy/2026-09/BBY01-1.pdf") \
            == "receipts/bestbuy/2026-09/BBY01-1.pdf"
        assert mig.key_from_link("/receipts/bestbuy/2026-09/BBY01-1.pdf") is None
        assert mig.key_from_link("https://drive.example/file/abc") is None
        assert mig.key_from_link("https://host/o/receipts/only/two.pdf") is None

    def test_plan_groups_rows_by_link_and_reports_the_odd_ones(self):
        grid = [self.HEADER,
                ["O1", "https://h/o/receipts/bestbuy/2026-09/O1.pdf"],
                ["O1", "https://h/o/receipts/bestbuy/2026-09/O1.pdf"],
                ["O2", "/receipts/amazon/2026-08/O2.pdf"],
                ["O3", "https://drive.example/x"],
                ["O4", ""], ["", "https://h/o/receipts/x/y/z.pdf"]]
        moves, notes = mig.plan_migration(grid)
        assert moves == {"https://h/o/receipts/bestbuy/2026-09/O1.pdf": ("receipts/bestbuy/2026-09/O1.pdf", [2, 3])}
        assert len(notes) == 1 and "O3" in notes[0]

    def test_apply_downloads_stores_and_rewrites(self, on):
        from ledger_db.store import LedgerDb
        from ledger_db.worksheet import DbWorksheet
        from models.order import FIELDNAMES
        from sheets.ledger_sync import HEADER

        ws = DbWorksheet(LedgerDb(on.parent / "ledger.sqlite3"))
        row = lambda oid, ship, link: [{"order_id": oid, "order_date": "2026-09-01", "item_name": "x",  # noqa: E731
                                        "shipment": ship, "receipt_url": link}.get(f, "") for f in FIELDNAMES]
        ws.update(range_name="A2", values=[row("O1", "1", "https://h/o/receipts/bestbuy/2026-09/O1.pdf"),
                                           row("O1", "2", "https://h/o/receipts/bestbuy/2026-09/O1.pdf"),
                                           row("O2", "1", "https://h/o/receipts/costco/2026-09/O2.pdf")])
        moves, _notes = mig.plan_migration(ws.get_all_values())
        fetched = []

        def fetch_fn(url):
            fetched.append(url)
            if "O2" in url:
                raise OSError("403")
            return b"%PDF-1.4 O1"

        moved, failures = mig.apply_migration(ws, moves, fetch_fn=fetch_fn)
        assert moved == 1 and len(failures) == 1 and "O2" in failures[0]
        links = [r[HEADER.index("Receipt Link")] for r in ws.get_all_values()[1:]]
        assert links == ["/receipts/bestbuy/2026-09/O1.pdf", "/receipts/bestbuy/2026-09/O1.pdf",
                         "https://h/o/receipts/costco/2026-09/O2.pdf"]
        assert store.path_for("receipts/bestbuy/2026-09/O1.pdf").read_bytes() == b"%PDF-1.4 O1"
        # a re-run only touches what is left, and never re-downloads what is stored
        moves2, _ = mig.plan_migration(ws.get_all_values())
        assert list(moves2) == ["https://h/o/receipts/costco/2026-09/O2.pdf"]
        assert fetched == ["https://h/o/receipts/bestbuy/2026-09/O1.pdf", "https://h/o/receipts/costco/2026-09/O2.pdf"]
