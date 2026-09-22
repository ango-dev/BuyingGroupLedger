"""The Audit and Reconciliation pages over the SQLite ledger -- the backend the host runs. The
audit's grids come from the worksheet adapter (the CLI's own path under `db`), so a finding on
the page is exactly what `python -m scripts.audit_ledger` would print."""
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
        row(profile_label="profile-p", order_date="2026-09-08", status="shipped", tracking_submitted="True", retailer="Best Buy", item_name="MacBook",
            shipment="1", quantity="1", order_id="BBY01-1", tracking_number="5238",
            buying_group="BFMR", cost_per_item="1000", total_cost="1000", card_name="Amex",
            cashback_rate="0.04", insurance="6.4", expected_payout="1230", card_last4="4331",
            order_url="https://www.bestbuy.com/order/BBY01-1", delivery_address="1 Main St",
            last_scraped_at="2026-09-18T10:00:00+00:00"),
        # delivered with no rate: the COGS input gap the audit fails on
        row(profile_label="profile-p", order_date="2026-08-10", status="delivered", tracking_submitted="True", retailer="Amazon", item_name="Fitbit",
            shipment="1", quantity="1", order_id="111-2", tracking_number="TBA1",
            delivery_date="2026-08-13", buying_group="BFMR", cost_per_item="100", total_cost="100",
            order_url="https://www.amazon.com/o/111-2", delivery_address="1 Main St", card_name="Amex",
            card_last4="4331", receipt_url="/receipts/amazon/2026-08/111-2.pdf",
            last_scraped_at="2026-09-18T10:00:00+00:00"),
        # settled, short-paid against its commitment
        row(profile_label="profile-p", order_date="2026-08-20", status="paid", tracking_submitted="True", retailer="Costco", item_name="iPad",
            shipment="1", quantity="2", order_id="1399000017", tracking_number="1Z1",
            buying_group="BFMR", cost_per_item="200", total_cost="400", cashback_rate="0.09",
            payout_amount="500", payout_date="2026-09-01", expected_payout="520", insurance="2.4",
            order_url="https://www.costco.com/o/1399000017", delivery_address="1 Main St", card_name="Venmo",
            card_last4="4351", receipt_url="/receipts/costco/2026-08/1399000017.pdf",
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
                     settings=dataclasses.replace(settings, container_run_interval_hours=6, web_password=""))
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
        page = client.get("/audit").text
        # the title first, then the lead (description, tiles, checks), both scrolling away; then the
        # pinned wrapper with the filter bar that sticks once they have
        assert page.index("<h1>Audit</h1>") < page.index('<div class="lead">') < page.index('<div class="pinned">') < page.index('id="filters"')
        orders = client.get("/orders").text
        assert orders.index("<h1>Orders</h1>") < orders.index('<div class="pinned">') < orders.index('id="filters"') < orders.index('<details class="add-row"')
        # the add-row form is BELOW the pinned bar (the bar's div closes before it), so it scrolls
        # away with the count line
        bar = orders[orders.index('<div class="pinned">'):orders.index('<details class="add-row"')]
        assert bar.count("<div") == bar.count("</div>")
        assert '<details class="audit-results">' in page and 'class="audit-results" open' not in page  # starts closed
        # the four tiles are links that filter the rows: Fail / Warning to their checks, Pass / Flagged to all
        assert page.count('<a class="tile link') == 5 and 'href="/audit?check=' in page and 'href="/audit"' in page
        assert ">Skipped<" in page and page.index(">Pass<") < page.index(">Skipped<") < page.index(">Flagged<")
        assert 'class="tile link skipped"' in page
        # INFO checks put no row on the page (a combined box is not a problem)
        assert "duplicate_shipment_lines" not in page.split('<div class="sheet"')[1]

        ws = DbWorksheet(db)
        ws.update(range_name="A3", values=[row(
            order_date="2026-08-10", status="delivered", tracking_submitted="True", retailer="Amazon", item_name="Fitbit",
            shipment="1", quantity="1", order_id="111-2", tracking_number="TBA1",
            delivery_date="2026-08-13", buying_group="BFMR", cost_per_item="100", total_cost="100",
            card_name="Prime", cashback_rate="0.05", card_last4="0315",
            last_scraped_at="2026-09-18T10:00:00+00:00")])
        body = client.get("/audit").text
        assert "no rate resolved" not in body

    def test_the_checks_run_over_the_adapter_exactly_as_the_cli_does(self, db):
        from scripts.audit_ledger import Options, Sheet, read_grids, run_checks

        ws = DbWorksheet(db, read_only=True)
        results = {r.name: r for r in run_checks(Sheet(read_grids(ws, ws.title)), Options())}
        assert results["cogs_inputs_complete"].status == "FAIL"
        assert "profit_formula_literal" not in results  # a formula-text check, deleted with the formulas 2026-09-18
        assert not [r for r in results.values() if "Google Sheet" in r.summary]
        assert results["header_matches_schema"].status == "PASS"
        assert results["duplicate_primary_keys"].status == "PASS"


class TestReconOverTheDatabase:
    def test_the_short_paid_order_is_listed(self, client):
        body = client.get("/recon").text
        assert "<h1>Reconciliation</h1>" in body
        assert "1399000017" in body and "short-paid by $20.00" in body
        assert "BBY01-1" not in body.split('id="orders-table"')[1]


def test_a_failing_line_naming_its_order_flags_that_orders_rows(tmp_path):
    """a line
    naming rows mid-line, or naming only its order (and package), puts those rows on the page."""
    from scripts.audit_ledger import Result
    from web import audit_view

    class FakeSheet:
        header = ["Order ID", "Package ID"]
        grid = [["Order ID", "Package ID"], ["A1", "P1"], ["A1", "P2"], ["B2", ""]]

        def __init__(self, grids):
            pass

        def ledger_rows(self, grid):
            return [(n, grid[n - 1]) for n in range(2, len(grid) + 1)]

        def cell(self, grid, n, name):
            return grid[n - 1][self.header.index(name)]

        def primary_key(self, grid, n):
            return (grid[n - 1][0], "2026-01-01", f"item{n}", "1")

    class Grids:
        formatted = FakeSheet.grid

    results = [Result("shipment_numbers_contiguous", "WARN", "1 order", ["order B2: shipments [1, 3] -- 2 is missing"]),
               Result("shipping_is_cost_weighted", "FAIL", "1 package", ["order A1 package P2: the same amount on every row"]),
               Result("package_id_per_shipment", "FAIL", "1 id", ["order A1 package P1 sits under shipments ['1', '2']: rows [2, 3]"]),
               Result("key_changes", "WARN", "1", ["A1 ship 2: key changed -- was 'x' (row 40), now 'y' (row 3)"])]
    original_sheet, original_run = audit_view.Sheet, audit_view.run_checks
    audit_view.Sheet, audit_view.run_checks = FakeSheet, lambda sheet, opts: results
    try:
        report = audit_view.run_audit(Grids())
    finally:
        audit_view.Sheet, audit_view.run_checks = original_sheet, original_run
    keys = report.keys_by_check
    assert keys["shipment_numbers_contiguous"] == [("B2", "2026-01-01", "item4", "1")]
    assert keys["shipping_is_cost_weighted"] == [("A1", "2026-01-01", "item3", "1")]  # the named package only
    assert keys["package_id_per_shipment"] == [("A1", "2026-01-01", "item2", "1"), ("A1", "2026-01-01", "item3", "1")]
    assert "key_changes" not in keys and report.notes["key_changes"]  # older-snapshot rows stay a note
