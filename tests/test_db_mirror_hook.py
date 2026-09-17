"""main.run_db_mirror -- the scheduled run's last step copies the Sheet into the SQLite file.

Read-only on the Sheet (a worksheet fake that allows only get_values), never fails the run, alerts
when it fails, and can be switched off. The conftest safety net blocks the real opener, so the
tests inject a fake through the same name.
"""

from __future__ import annotations

import dataclasses

import pytest

import main
from ledger_db.store import LedgerDb
from models.order import FIELDNAMES
from sheets.ledger_sync import HEADER


def row(**values) -> list[str]:
    return [str(values.get(f, "")) for f in FIELDNAMES]


class ReadOnlyWorksheet:
    title = "Orders"

    def __init__(self, grid):
        self.grid = grid

    def get_values(self, range_name=None, value_render_option=None, **kwargs):
        assert value_render_option is not None
        return [list(r) for r in self.grid]

    def __getattr__(self, name):
        raise AssertionError(f"run_db_mirror called worksheet.{name}() -- only get_values is allowed")


@pytest.fixture
def settings_at(tmp_path, monkeypatch):
    def apply(**overrides):
        values = {"ledger_db_path": str(tmp_path / "ledger.sqlite3"),
                  "ledger_db_mirror_after_run": True, **overrides}
        patched = dataclasses.replace(main.settings, **values)
        monkeypatch.setattr(main, "settings", patched)
        return patched
    return apply


def test_mirrors_the_sheet_into_the_configured_file_read_only(monkeypatch, settings_at, tmp_path):
    import scripts.audit_sheet as audit

    grid = [list(HEADER),
            row(order_id="A1", order_date="2026-09-01", status="shipped", item_name="Thing",
                shipment="1", quantity="1", total_cost="100", cashback_rate="0.05",
                payout_amount="120"),
            row(order_id="B2", order_date="2026-08-01", status="paid", item_name="Other",
                shipment="1", total_cost="50", payout_amount="60", payout_date="2026-08-20")]
    opened = []
    monkeypatch.setattr(audit, "open_worksheet_readonly",
                        lambda: (opened.append(1), (ReadOnlyWorksheet(grid), "Ledger"))[1])
    alerts = []
    monkeypatch.setattr(main, "alert", lambda subject, body: alerts.append(subject))
    settings = settings_at()

    main.run_db_mirror()

    assert opened == [1] and alerts == []
    db = LedgerDb(settings.ledger_db_path)
    rows = db.fetch_rows()
    assert [r["order_id"] for r in rows] == ["A1", "B2"]
    assert rows[0]["cogs"] == 95.0 and rows[0]["total_profit"] == 25.0
    assert db.last_mirror()["backend"] == "sheet" and db.last_mirror()["source"] == "Ledger / Orders"


def test_a_failure_alerts_and_never_raises(monkeypatch, settings_at):
    import scripts.audit_sheet as audit

    def boom():
        raise RuntimeError("credentials revoked")

    monkeypatch.setattr(audit, "open_worksheet_readonly", boom)
    alerts = []
    monkeypatch.setattr(main, "alert", lambda subject, body: alerts.append((subject, body)))
    settings_at()

    main.run_db_mirror()  # must not raise

    assert alerts and alerts[0][0] == "Ledger DB mirror failed"
    assert "Sheet itself is unaffected" in alerts[0][1]


def test_switched_off_does_nothing(monkeypatch, settings_at):
    import scripts.audit_sheet as audit

    monkeypatch.setattr(audit, "open_worksheet_readonly",
                        lambda: pytest.fail("must not open the sheet when disabled"))
    settings = settings_at(ledger_db_mirror_after_run=False)

    main.run_db_mirror()

    assert not LedgerDb(settings.ledger_db_path).path.exists()


def test_it_is_the_last_step_of_the_run(monkeypatch):
    """After the sync and the auto-reply, so the copy carries this run's payouts too."""
    order = []
    monkeypatch.setattr(main, "load_profiles_for_retailer", lambda key: [])
    monkeypatch.setattr(main, "run_buying_group_sync", lambda: order.append("sync"))
    monkeypatch.setattr(main, "run_bfmr_email_autoreply", lambda: order.append("autoreply"))
    monkeypatch.setattr(main, "run_db_mirror", lambda: order.append("mirror"))

    main.main(["costco"])

    assert order == ["sync", "autoreply", "mirror"]


def test_the_setting_has_its_config_home():
    from config.settings import ENV_TO_CONFIG

    assert ENV_TO_CONFIG["LEDGER_DB_MIRROR_AFTER_RUN"] == "database.mirror_after_run"
