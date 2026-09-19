"""The Audit and Reconciliation pages over the SQLite ledger -- the backend the host runs. The
audit's grids come from the worksheet adapter (the CLI's own path under `db`), so a finding on
the page is exactly what `python -m scripts.audit_sheet` would print."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ledger_db.store import LedgerDb  # noqa: E402
from ledger_db.worksheet import DbWorksheet  # noqa: E402
from models.order import FIELDNAMES  # noqa: E402
from web.app import create_app  # noqa: E402
from web.ledger_reader import DbReader  # noqa: E402

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def row(**values):
    return [values.get(f, "") for f in FIELDNAMES]


@pytest.fixture
def db(tmp_path):
    db = LedgerDb(tmp_path / "ledger.sqlite3")
    DbWorksheet(db).update(range_name="A2", values=[
        # open, committed, complete
        row(order_date="2026-09-08", status="shipped", retailer="Best Buy", item_name="MacBook",
            shipment="1", quantity="1", order_id="BBY01-1", tracking_number="5238",
            buying_group="BFMR", cost_per_item="1000", total_cost="1000", card_name="Amex",
            cashback_rate="0.04", insurance="6.4", expected_payout="1230", card_last4="4331",
            last_scraped_at="2026-09-18T10:00:00+00:00"),
        # delivered with no rate: the COGS input gap the audit fails on
        row(order_date="2026-08-10", status="delivered", retailer="Amazon", item_name="Fitbit",
            shipment="1", quantity="1", order_id="111-2", tracking_number="TBA1",
            delivery_date="2026-08-13", buying_group="BFMR", cost_per_item="100", total_cost="100",
            last_scraped_at="2026-09-18T10:00:00+00:00"),
        # settled, short-paid against its commitment
        row(order_date="2026-08-20", status="paid", retailer="Costco", item_name="iPad",
            shipment="1", quantity="2", order_id="1399000017", tracking_number="1Z1",
            buying_group="BFMR", cost_per_item="200", total_cost="400", cashback_rate="0.09",
            payout_amount="500", payout_date="2026-09-01", expected_payout="520",
            last_scraped_at="2026-09-18T10:00:00+00:00"),
    ])
    return db


@pytest.fixture
def client(db, tmp_path):
    from config.settings import settings

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "failures").mkdir()
    (logs / ".last_run").write_text("2026-09-18T09:00:00Z", encoding="utf-8")
    app = create_app(DbReader(db), logs_dir=logs, failures_dir=logs / "failures",
                     clock=lambda: NOW,
                     settings=dataclasses.replace(settings, container_run_interval_hours=6))
    return TestClient(app)


class TestAuditOverTheDatabase:
    def test_the_data_checks_run(self, client):
        body = client.get("/audit").text
        assert "<h1>Audit</h1>" in body
        assert "111-2" in body and "no rate resolved" in body and "<b>cogs_inputs_complete</b>" in body
        assert "BBY01-1" not in body.split('id="orders-table"')[1]  # a clean row is not listed
        assert "Google Sheet" not in body and "profit_formula_literal" not in body

    def test_the_report_follows_a_write_through_the_adapter(self, client, db):
        assert "111-2" in client.get("/audit").text
        ws = DbWorksheet(db)
        ws.update(range_name="A3", values=[row(
            order_date="2026-08-10", status="delivered", retailer="Amazon", item_name="Fitbit",
            shipment="1", quantity="1", order_id="111-2", tracking_number="TBA1",
            delivery_date="2026-08-13", buying_group="BFMR", cost_per_item="100", total_cost="100",
            card_name="Prime", cashback_rate="0.05", card_last4="0315",
            last_scraped_at="2026-09-18T10:00:00+00:00")])
        body = client.get("/audit").text
        assert "no rate resolved" not in body

    def test_the_checks_run_over_the_adapter_exactly_as_the_cli_does(self, db):
        from scripts.audit_sheet import Options, Sheet, read_grids, run_checks

        ws = DbWorksheet(db, read_only=True)
        results = {r.name: r for r in run_checks(Sheet(read_grids(ws, ws.title)), Options())}
        assert results["cogs_inputs_complete"].status == "FAIL"
        assert "profit_formula_literal" not in results  # a Sheet check; gone with the Sheet
        assert not [r for r in results.values() if "Google Sheet" in r.summary]
        assert results["header_matches_schema"].status == "PASS"
        assert results["duplicate_primary_keys"].status == "PASS"


class TestReconOverTheDatabase:
    def test_the_short_paid_order_is_listed(self, client):
        body = client.get("/recon").text
        assert "<h1>Reconciliation</h1>" in body
        assert "1399000017" in body and "short-paid by $20.00" in body
        assert "BBY01-1" not in body.split('id="orders-table"')[1]
