"""The receipt store is a directory beside the ledger (receipts/store.py, 2026-09-18: OCI removed)
and the links it writes are dashboard-relative."""
from __future__ import annotations

import dataclasses

import pytest

from receipts import store

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

