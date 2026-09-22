"""scripts/receipt_files: orphan receipt files, missing files and twin names."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import receipt_files
from web import tax_inputs
from web.tax_inputs import YearInputs


def expense(entry_id: str, rel: str, name: str) -> dict:
    return {"id": entry_id, "date": "2026-03-03", "description": "x", "amount": 1.0, "category": "", "profile": "a",
            "email": "", "receipt": {"file": rel, "name": name}, "added_at": "2026-09-21"}


def test_the_plan_finds_orphans_missing_files_and_twins(tmp_path):
    data = tmp_path / "data"
    receipts = data / "receipts"
    (receipts / "amazon" / "2026-08").mkdir(parents=True)
    (receipts / "amazon" / "2026-08" / "111-1.pdf").write_bytes(b"a")   # linked
    (receipts / "amazon" / "2026-08" / "111-1.png").write_bytes(b"b")   # the replaced one: orphan
    (receipts / "amazon" / "2026-08" / "222-2.pdf").write_bytes(b"c")   # no row links it: orphan
    expenses = data / "expenses"
    (expenses / "2026").mkdir(parents=True)
    (expenses / "2026" / "e1_Order.pdf").write_bytes(b"d")
    (expenses / "2026" / "e2_Order.pdf").write_bytes(b"e")
    (expenses / "2026" / "old_Order.pdf").write_bytes(b"f")             # no record: orphan
    inputs = {2026: YearInputs(expenses=[expense("e1", "expenses/2026/e1_Order.pdf", "Order.pdf"),
                                         expense("e2", "expenses/2026/e2_Order.pdf", "Order.pdf"),
                                         expense("e3", "expenses/2026/e3_gone.pdf", "gone.pdf")])}
    report = receipt_files.plan(ledger_links={"/receipts/amazon/2026-08/111-1.pdf", "https://elsewhere/x.pdf"},
                                receipts_root=receipts, all_inputs=inputs, expenses_root=expenses)
    assert [p.name for p in report["orphans"]] == ["111-1.png", "222-2.pdf", "old_Order.pdf"]
    assert report["missing"] == [(2026, "e3", "expenses/2026/e3_gone.pdf")]
    assert report["twins"] == {"Order.pdf": [(2026, "e1"), (2026, "e2")]}
    # apply: the orphans go, the twins are numbered and their files follow
    path = data / tax_inputs.FILE_NAME
    tax_inputs.save_year(path, 2026, inputs[2026])
    counts = receipt_files.apply(report, path=path, data_dir=data)
    assert counts == {"deleted": 3, "numbered": 2}
    assert not (receipts / "amazon" / "2026-08" / "111-1.png").exists() and (receipts / "amazon" / "2026-08" / "111-1.pdf").exists()
    saved = json.loads(path.read_text(encoding="utf-8"))["2026"]["expenses"]
    assert [e["receipt"]["name"] for e in saved[:2]] == ["Order-0001.pdf", "Order-0002.pdf"]
    assert (expenses / "2026" / "e1_Order-0001.pdf").exists() and (expenses / "2026" / "e2_Order-0002.pdf").exists()
    again = receipt_files.plan(ledger_links={"/receipts/amazon/2026-08/111-1.pdf"}, receipts_root=receipts,
                               all_inputs=tax_inputs.load_all(path), expenses_root=expenses)
    assert again["orphans"] == [] and again["twins"] == {}
