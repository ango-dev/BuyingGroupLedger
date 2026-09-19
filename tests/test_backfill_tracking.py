"""scripts/backfill_tracking.py -- the pure planner: order-number join against BFMR's tracker."""

from scripts.backfill_tracking import plan_backfill
from ledger.sync import HEADER


def _row(order_id, shipment="1", tracking="", status="delivered", group="BFMR"):
    row = [""] * len(HEADER)
    row[HEADER.index("Order ID")] = order_id
    row[HEADER.index("Shipment")] = shipment
    row[HEADER.index("Tracking Number")] = tracking
    row[HEADER.index("Status")] = status
    row[HEADER.index("Buying Group")] = group
    return row


def test_one_number_fills_every_row_of_the_order_and_n_numbers_fill_one_each():
    grid = [list(HEADER), _row("A", "1"), _row("A", "1"), _row("B", "1"), _row("B", "2")]
    bfmr = [{"order_id": "A", "tracking_number": "TA"},
            {"order_id": "B", "tracking_number": "TB1"}, {"order_id": "B", "tracking_number": "TB2"}]
    writes, ambiguous = plan_backfill(grid, bfmr)
    assert writes == [(2, "A", "TA"), (3, "A", "TA"), (4, "B", "TB1"), (5, "B", "TB2")] and ambiguous == []


def test_mismatched_counts_and_unknown_orders_are_reported_not_guessed():
    grid = [list(HEADER), _row("A", "1"), _row("C", "1")]
    bfmr = [{"order_id": "A", "tracking_number": "T1"}, {"order_id": "A", "tracking_number": "T2"}]
    writes, ambiguous = plan_backfill(grid, bfmr)
    assert writes == [] and len(ambiguous) == 2
    assert "assign by hand" in ambiguous[0] and "no tracking number" in ambiguous[1]


def test_tracked_cancelled_and_non_bfmr_rows_are_left_alone():
    grid = [list(HEADER), _row("A", tracking="HAVE"), _row("B", status="cancelled"), _row("C", group="MOD")]
    bfmr = [{"order_id": o, "tracking_number": "T"} for o in "ABC"]
    assert plan_backfill(grid, bfmr) == ([], [])


def test_a_sibling_row_with_one_number_fills_the_orders_blank_rows():
    grid = [list(HEADER), _row("A", "1", tracking="TA"), _row("A", "2"), _row("A", "3"),
            _row("B", "1", tracking="TB1"), _row("B", "2", tracking="TB2"), _row("B", "3")]
    writes, ambiguous = plan_backfill(grid, [])
    assert writes == [(3, "A", "TA"), (4, "A", "TA")]
    assert len(ambiguous) == 1 and "B" in ambiguous[0]
