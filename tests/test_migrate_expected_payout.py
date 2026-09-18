"""scripts/migrate_expected_payout: the one-time move of open rows' commitments out of Payout
Amount into Expected Payout (2026-09-18), planned over a grid and applied through the worksheet
face -- here the SQLite adapter, the path the `db` backend takes."""
from __future__ import annotations

import pytest

from ledger_db.store import LedgerDb
from ledger_db.worksheet import DbWorksheet
from models.order import FIELDNAMES
from scripts.migrate_expected_payout import apply_migration, main, plan_migration
from sheets.ledger_sync import HEADER


def row(**values):
    return [values.get(f, "") for f in FIELDNAMES]


def grid(*rows):
    return [list(HEADER), *rows]


class TestPlan:
    def test_an_open_row_with_a_dateless_payout_moves(self):
        moves, notes = plan_migration(grid(
            row(order_id="O1", status="shipped", payout_amount="1230"),
            row(order_id="O2", status="delivered", payout_amount="$97.50"),
        ))
        assert moves == [(2, "O1", 1230.0), (3, "O2", 97.5)] and notes == []

    def test_settled_money_free_and_zero_rows_stay(self):
        moves, notes = plan_migration(grid(
            row(order_id="O1", status="paid", payout_amount="500", payout_date="2026-09-01"),
            row(order_id="O2", status="paid", payout_amount="330"),           # MOD: dateless paid
            row(order_id="O3", status="return", payout_amount="0"),
            row(order_id="O4", status="cancelled", payout_amount="12"),
            row(order_id="O5", status="shipped", payout_amount="0"),           # a zero is no commitment
            row(order_id="O6", status="shipped"),                              # nothing there
            row(order_id="", status="shipped", payout_amount="99"),            # not a ledger row
        ))
        assert moves == [] and notes == []

    def test_a_row_already_carrying_a_different_commitment_is_reported_not_moved(self):
        moves, notes = plan_migration(grid(
            row(order_id="O1", status="shipped", payout_amount="1230", expected_payout="1100"),
            row(order_id="O2", status="shipped", payout_amount="1230", expected_payout="1230"),
        ))
        assert moves == [(3, "O2", 1230.0)]  # equal: the payout still needs clearing
        assert len(notes) == 1 and "O1" in notes[0] and "left alone" in notes[0]

    def test_a_ledger_without_the_column_is_refused_with_advice(self):
        header = [h for h in HEADER if h != "Expected Payout"]
        with pytest.raises(ValueError, match="Expected Payout"):
            plan_migration([header, row(order_id="O1", status="shipped", payout_amount="1")[: len(header)]])

    def test_an_empty_grid_plans_nothing(self):
        assert plan_migration([]) == ([], [])


class TestApply:
    @pytest.fixture
    def ws(self, tmp_path):
        ws = DbWorksheet(LedgerDb(tmp_path / "ledger.sqlite3"))
        ws.update(range_name="A2", values=[
            row(order_date="2026-09-08", status="shipped", item_name="MacBook", shipment="1",
                order_id="O1", total_cost="1000", cashback_rate="0.04", insurance="6.4",
                payout_amount="1230"),
            row(order_date="2026-08-20", status="paid", item_name="iPad", shipment="1",
                order_id="O2", total_cost="400", payout_amount="500", payout_date="2026-09-01"),
        ])
        return ws

    def test_the_commitment_moves_and_the_payout_clears(self, ws):
        moves, notes = plan_migration(ws.get_all_values())
        assert moves == [(2, "O1", 1230.0)] and notes == []
        apply_migration(ws, moves)
        values = ws.get_all_values()
        by = dict(zip(HEADER, values[1]))
        assert by["Expected Payout"] == "1230" and by["Actual Payout"] == ""
        assert by["Total Profit"] == "", "no payout, no profit -- the commitment is not income"
        settled = dict(zip(HEADER, values[2]))
        assert settled["Actual Payout"] == "500" and settled["Expected Payout"] == ""
        # Running it again finds nothing.
        assert plan_migration(ws.get_all_values()) == ([], [])

    def test_the_cli_is_a_dry_run_unless_applied(self, ws, monkeypatch, capsys):
        import scripts.migrate_expected_payout as module

        monkeypatch.setattr(module, "_get_worksheet", lambda: ws)
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "row 2: order O1: Actual Payout 1,230.00 -> Expected Payout" in out and "Dry run" in out
        assert dict(zip(HEADER, ws.get_all_values()[1]))["Actual Payout"] == "1230"
        assert main(["--apply"]) == 0
        assert "Moved 1 commitment(s)" in capsys.readouterr().out
        assert dict(zip(HEADER, ws.get_all_values()[1]))["Expected Payout"] == "1230"
        assert main([]) == 0 and "Nothing to move" in capsys.readouterr().out
